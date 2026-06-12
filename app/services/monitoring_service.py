from __future__ import annotations

import glob
import json
import os
import select
import subprocess
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import psutil
from sqlalchemy.orm import Session

from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.job import Job

TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}
ACTIVE_ENCODE_STATUSES = {'starting', 'running', 'preflight'}
ACTIVE_DOWNLOAD_STATUSES = {
    DownloadJobStatus.searching.value,
    DownloadJobStatus.queued.value,
    DownloadJobStatus.downloading.value,
    DownloadJobStatus.moving.value,
    DownloadJobStatus.importing.value,
}
QMMD_METRICS_URL_ENV = 'OPTIMIZARR_QMMD_METRICS_URL'
QMMD_AUTO_DISCOVERY_ENV = 'OPTIMIZARR_QMMD_AUTO_DISCOVERY'
QMMD_DEFAULT_PORT = 9000
_QMMD_DISCOVERED_METRICS_URL: str | None = None


def _safe_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip().rstrip('%').strip()
        try:
            return float(stripped)
        except ValueError:
            return 0.0
    return 0.0


def _clamp_percent(value: Any) -> float:
    return min(100.0, max(0.0, _safe_float(value)))


def _has_nonzero_gpu_activity(metrics: dict[str, float]) -> bool:
    return any(
        _safe_float(value) > 0.0
        for key, value in metrics.items()
        if key.endswith('_percent')
    )


def _normalize_gpu_metrics(metrics: dict[str, float]) -> dict[str, float]:
    return {
        'gpu_video_percent': _clamp_percent(metrics.get('gpu_video_percent')),
        'gpu_render_percent': _clamp_percent(metrics.get('gpu_render_percent')),
    }


def _extract_percent(stats: Any, preferred_labels: tuple[str, ...]) -> float:
    if isinstance(stats, dict):
        engine_values = []
        for key, value in stats.items():
            key_lower = str(key).lower()
            if isinstance(value, dict):
                if any(label in key_lower for label in preferred_labels):
                    busy_value = _clamp_percent(value.get('busy'))
                    if busy_value:
                        engine_values.append(busy_value)
                nested_value = _extract_percent(value, preferred_labels)
                if nested_value:
                    engine_values.append(nested_value)
            elif any(label in key_lower for label in preferred_labels):
                parsed_value = _clamp_percent(value)
                if parsed_value:
                    engine_values.append(parsed_value)
        if engine_values:
            return max(engine_values)

    if isinstance(stats, list):
        values = [_extract_percent(item, preferred_labels) for item in stats]
        values = [value for value in values if value]
        if values:
            return max(values)

    return 0.0


def _extract_last_json_blob(raw_output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    cursor = 0
    last_json = {}

    while cursor < len(raw_output):
        try:
            obj, index = decoder.raw_decode(raw_output, cursor)
            if isinstance(obj, dict):
                last_json = obj
            cursor = index
        except json.JSONDecodeError:
            cursor += 1

    return last_json


def _parse_prometheus_labels(raw_labels: str) -> dict[str, str]:
    labels: dict[str, str] = {}
    current = ''
    in_quotes = False
    escape = False
    parts: list[str] = []

    for char in raw_labels:
        if escape:
            current += char
            escape = False
            continue
        if char == '\\':
            current += char
            escape = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            current += char
            continue
        if char == ',' and not in_quotes:
            parts.append(current)
            current = ''
            continue
        current += char
    if current:
        parts.append(current)

    for part in parts:
        if '=' not in part:
            continue
        key, value = part.split('=', 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace(r'\"', '"').replace(r'\\', '\\')
        if key:
            labels[key] = value
    return labels


def _iter_prometheus_samples(raw_text: str, metric_name: str) -> list[tuple[dict[str, str], float]]:
    samples: list[tuple[dict[str, str], float]] = []
    prefix = f'{metric_name}{{'

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        labels: dict[str, str] = {}
        value_text = ''
        if line.startswith(prefix):
            label_end = line.find('}')
            if label_end == -1:
                continue
            labels = _parse_prometheus_labels(line[len(prefix):label_end])
            value_text = line[label_end + 1:].strip().split(None, 1)[0]
        elif line.startswith(f'{metric_name} '):
            value_text = line[len(metric_name):].strip().split(None, 1)[0]
        else:
            continue
        value = _safe_float(value_text)
        samples.append((labels, value))
    return samples


def _docker_host_gateway_ip() -> str | None:
    try:
        with open('/proc/net/route') as route_file:
            for line in route_file.readlines()[1:]:
                fields = line.split()
                if len(fields) < 3 or fields[1] != '00000000':
                    continue
                gateway_hex = fields[2]
                octets = [
                    str(int(gateway_hex[index:index + 2], 16))
                    for index in range(6, -1, -2)
                ]
                return '.'.join(octets)
    except OSError:
        return None
    return None


def _qmmd_candidate_urls() -> list[str]:
    configured = (os.getenv(QMMD_METRICS_URL_ENV) or '').strip()
    if configured:
        return [url.strip() for url in configured.split(',') if url.strip()]

    auto_discovery = (os.getenv(QMMD_AUTO_DISCOVERY_ENV, 'true') or '').strip().lower()
    if auto_discovery in {'0', 'false', 'no', 'off'}:
        return []

    hosts = ['host.docker.internal']
    gateway_ip = _docker_host_gateway_ip()
    if gateway_ip:
        hosts.append(gateway_ip)
    if '172.17.0.1' not in hosts:
        hosts.append('172.17.0.1')

    return [f'http://{host}:{QMMD_DEFAULT_PORT}/metrics' for host in hosts]


def _fetch_qmmd_metrics_text(metrics_url: str, timeout: float) -> str | None:
    try:
        with urlopen(metrics_url, timeout=timeout) as response:
            return response.read(512 * 1024).decode('utf-8', errors='replace')
    except (OSError, URLError, TimeoutError, ValueError):
        return None


def _parse_qmmd_gpu_metrics(raw_text: str) -> dict[str, float] | None:
    if 'qmmd_gpu_' not in raw_text:
        return None

    video = 0.0
    render = 0.0
    for labels, ratio in _iter_prometheus_samples(raw_text, 'qmmd_gpu_engine_utilization_ratio'):
        engine = labels.get('engine', '').lower()
        pct = _clamp_percent(ratio * 100.0)
        if any(lbl in engine for lbl in ('vcs', 'vecs', 'vd', 'video')):
            video = max(video, pct)
        elif any(lbl in engine for lbl in ('rcs', 'ccs', 'render', '3d')):
            render = max(render, pct)

    if video == 0.0 and render == 0.0:
        return None

    return {
        'gpu_video_percent': video,
        'gpu_render_percent': render,
    }


def _get_qmmd_gpu_metrics() -> dict[str, float] | None:
    """Try qmassa/qmmd Prometheus metrics from config or Docker host discovery."""
    global _QMMD_DISCOVERED_METRICS_URL

    configured = bool((os.getenv(QMMD_METRICS_URL_ENV) or '').strip())
    candidate_urls = _qmmd_candidate_urls()
    if _QMMD_DISCOVERED_METRICS_URL and not configured:
        candidate_urls = [_QMMD_DISCOVERED_METRICS_URL]
    if not candidate_urls:
        return None

    timeout = 1.5 if configured else 0.35
    for metrics_url in candidate_urls:
        raw_text = _fetch_qmmd_metrics_text(metrics_url, timeout=timeout)
        if raw_text is None:
            continue
        metrics = _parse_qmmd_gpu_metrics(raw_text)
        if metrics is None:
            continue
        if not configured:
            _QMMD_DISCOVERED_METRICS_URL = metrics_url
        return metrics

    if not configured:
        _QMMD_DISCOVERED_METRICS_URL = None
    return None


def _intel_gpu_top_raw() -> dict[str, str]:
    """Run intel_gpu_top -J for 2 seconds and return raw stdout + stderr.

    Used by the debug endpoint only — not in the hot metrics path.
    """
    try:
        process = subprocess.Popen(
            ['stdbuf', '-oL', 'intel_gpu_top', '-J', '-s', '250'],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError:
        return {'stdout': '', 'stderr': '', 'error': 'intel_gpu_top not found'}
    except PermissionError as exc:
        return {'stdout': '', 'stderr': '', 'error': f'permission error: {exc}'}

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    deadline = time.monotonic() + 2.5
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            readable, _, _ = select.select([process.stdout, process.stderr], [], [], remaining)
            if not readable:
                break
            for fd in readable:
                line = fd.readline()
                if not line:
                    break
                if fd is process.stdout:
                    stdout_chunks.append(line)
                else:
                    stderr_chunks.append(line)
    finally:
        try:
            process.kill()
            process.wait(timeout=1)
        except Exception:
            pass

    return {
        'stdout': ''.join(stdout_chunks),
        'stderr': ''.join(stderr_chunks),
        'error': '',
    }


def _get_intel_gpu_metrics() -> dict[str, float] | None:
    """Try Intel GPU metrics via intel_gpu_top. Returns None if unavailable."""
    # intel_gpu_top -J streams JSON objects indefinitely.  When stdout is a pipe
    # (not a tty) the C library switches to full block-buffering, so no data is
    # flushed before we SIGKILL the process — leaving raw_stdout empty.
    # stdbuf -oL forces line-buffering so each JSON line is flushed immediately.
    # select() is used instead of a bare readline() so the 2-second deadline is
    # enforced even if the process stalls between writes.
    try:
        process = subprocess.Popen(
            ['stdbuf', '-oL', 'intel_gpu_top', '-J', '-s', '250'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, PermissionError):
        return None

    raw_stdout = ''
    deadline = time.monotonic() + 2.0
    try:
        assert process.stdout is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                ready, _, _ = select.select([process.stdout], [], [], remaining)
            except (OSError, ValueError):
                # Windows select() only supports sockets, but the tests and the
                # real intel_gpu_top probe both use pipe-backed stdout.
                raw_stdout = process.stdout.read()
                break
            if not ready:
                break
            line = process.stdout.readline()
            if not line:
                break
            raw_stdout += line
    finally:
        try:
            process.kill()
            process.wait(timeout=1)
        except Exception:
            pass

    payload = _extract_last_json_blob(raw_stdout)
    if not payload:
        return None

    # Only trust this output if intel_gpu_top produced recognisable engine data.
    # Checking for the "engines" key avoids incorrectly falling back to NVIDIA
    # (or the 0% default) when the Intel GPU is merely idle.
    if 'engines' not in payload:
        return None

    video = _extract_percent(payload, ('video',))
    render = _extract_percent(payload, ('render', '3d'))

    return {
        'gpu_video_percent': video,
        'gpu_render_percent': render,
    }


def _get_nvidia_gpu_metrics() -> dict[str, float] | None:
    """Try NVIDIA GPU metrics via nvidia-smi. Returns None if unavailable."""
    try:
        result = subprocess.run(
            [
                'nvidia-smi',
                '--query-gpu=utilization.gpu,utilization.memory',
                '--format=csv,noheader,nounits',
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().split('\n')
        if not lines or not lines[0].strip():
            return None
        parts = lines[0].split(',')
        gpu_util = float(parts[0].strip())
        return {
            'gpu_video_percent': _clamp_percent(gpu_util),
            'gpu_render_percent': _clamp_percent(gpu_util),
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def _find_drm_card_sysfs_paths() -> list[str]:
    """Resolve DRM card sysfs paths, working around Docker sysfs filtering.

    Docker containers with --device=/dev/dri have the /dev/dri/card* device
    nodes available but may not expose /sys/class/drm symlinks.  Linux always
    publishes the true sysfs path at /sys/dev/char/{major}:{minor}, so we can
    locate the card's sysfs directory from the device file even when the
    /sys/class/drm symlink layer is absent inside the container.

    Some Docker configurations only pass through renderD nodes (not card nodes),
    so we also try /dev/dri/renderD* and navigate to the sibling card directory.
    """
    paths: list[str] = []

    # Primary: card* device nodes — direct major:minor resolution.
    for dev in sorted(glob.glob('/dev/dri/card*')):
        try:
            st = os.stat(dev)
            link = f'/sys/dev/char/{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}'
            resolved = os.path.realpath(link)
            if os.path.isdir(resolved):
                paths.append(resolved)
        except OSError:
            pass

    # Secondary: renderD* device nodes — resolve to the parent drm dir then
    # find card* siblings.  Used when Docker config exposes only renderD nodes.
    if not paths:
        for dev in sorted(glob.glob('/dev/dri/renderD*')):
            try:
                st = os.stat(dev)
                link = f'/sys/dev/char/{os.major(st.st_rdev)}:{os.minor(st.st_rdev)}'
                render_path = os.path.realpath(link)
                drm_dir = os.path.dirname(render_path)
                for card in sorted(glob.glob(f'{drm_dir}/card*')):
                    if card not in paths:
                        paths.append(card)
            except OSError:
                pass

    # Fallback: standard /sys/class/drm symlinks (works on host or fully
    # privileged containers).
    if not paths:
        paths = sorted(glob.glob('/sys/class/drm/card*'))
    return paths


def _get_intel_gpu_metrics_sysfs() -> dict[str, float] | None:
    """Read Intel GPU engine utilisation from sysfs (kernel ≥ 5.11).

    Works inside Docker containers without SYS_ADMIN or perf_event_open because
    the engine busy_time_ms counters under /sys/class/drm/ are world-readable.
    Two readings 500 ms apart are used to derive the utilisation percentage.
    Returns None if the engine sysfs hierarchy is absent on this host.
    """
    card_paths = _find_drm_card_sysfs_paths()
    engine_dirs = [
        d
        for card in card_paths
        for d in (
            glob.glob(f'{card}/engine/*') +         # single-GT (pre-Alder Lake i915)
            glob.glob(f'{card}/gt/gt*/engine/*')    # multi-GT (Alder Lake+, Arrow Lake, xe)
        )
    ]
    if not engine_dirs:
        return None

    def _read_busy() -> dict[str, int]:
        result: dict[str, int] = {}
        for d in engine_dirs:
            path = os.path.join(d, 'busy_time_ms')
            try:
                with open(path) as f:
                    result[os.path.basename(d).lower()] = int(f.read().strip())
            except (OSError, ValueError):
                pass
        return result

    t0 = time.monotonic()
    before = _read_busy()
    if not before:
        return None

    time.sleep(0.5)

    after = _read_busy()
    elapsed_ms = (time.monotonic() - t0) * 1000.0

    video_pct = 0.0
    render_pct = 0.0

    for name, busy_after in after.items():
        busy_before = before.get(name, busy_after)
        delta = max(0, busy_after - busy_before)
        pct = min(100.0, 100.0 * delta / elapsed_ms)
        if any(lbl in name for lbl in ('vcs', 'vecs', 'vd', 'video')):
            video_pct = max(video_pct, pct)
        elif any(lbl in name for lbl in ('rcs', 'ccs', 'render')):
            render_pct = max(render_pct, pct)

    return {'gpu_video_percent': video_pct, 'gpu_render_percent': render_pct}


def _get_intel_gpu_metrics_freq() -> dict[str, float] | None:
    """Estimate GPU utilisation from GT clock frequency (Intel iGPU/dGPU).

    Intel VDEnc (the hardware engine behind QSV) executes each video frame in
    sub-millisecond bursts that are far too brief for perf-event counters to
    register — intel_gpu_top shows near-zero even during active encoding.
    The GPU clock governor, however, ramps the GT frequency up to a sustained
    level while work is queued and holds it there, making the current clock a
    reliable activity indicator.

    Reads /sys/class/drm/card*/gt/gt*/rps_act_freq_mhz (xe driver and newer
    i915 with multi-GT, e.g. Alder/Raptor/Meteor Lake).  Falls back to the
    older flat /sys/class/drm/card*/gt_act_freq_mhz path for single-GT i915.

    The percentage is: (act_mhz - min_mhz) / (max_mhz - min_mhz) * 100,
    clamped to [0, 100].  Takes the maximum across all GT tiles on a card so
    that a card with a dedicated media GT (gt0 doing VDEnc at 1400 MHz) is
    not masked by an idle display GT (gt1 at 100 MHz).
    """

    def _read_int(path: str) -> int | None:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    def _pct(act: int, min_f: int, max_f: int) -> float:
        freq_range = max_f - min_f
        if freq_range <= 0:
            return 0.0
        return min(100.0, max(0.0, 100.0 * (act - min_f) / freq_range))

    def _first_int(paths: list[str]) -> int | None:
        for path in paths:
            value = _read_int(path)
            if value is not None:
                return value
        return None

    for card in _find_drm_card_sysfs_paths():
        # New per-GT nested paths — preferred; present on xe driver and
        # newer i915 (kernel 6.x) with multi-GT topology.
        gt_dirs = sorted(glob.glob(f'{card}/gt/gt*'))
        if gt_dirs:
            best = 0.0
            found = False
            for gt_dir in gt_dirs:
                act = _first_int([
                    f'{gt_dir}/rps_act_freq_mhz',
                    f'{gt_dir}/act_freq_mhz',
                    f'{gt_dir}/act_mhz',
                ])
                min_f = _first_int([
                    f'{gt_dir}/rps_min_freq_mhz',
                    f'{gt_dir}/min_freq_mhz',
                    f'{gt_dir}/min_mhz',
                ])
                max_f = _first_int([
                    f'{gt_dir}/rps_max_freq_mhz',
                    f'{gt_dir}/max_freq_mhz',
                    f'{gt_dir}/max_mhz',
                ])
                if act is None or min_f is None or max_f is None:
                    continue
                found = True
                best = max(best, _pct(act, min_f, max_f))
            if found:
                return {'gpu_video_percent': best, 'gpu_render_percent': best}

        # Older flat i915 paths (single-GT, kernel ≤ 5.x style).
        act = _first_int([
            f'{card}/gt_act_freq_mhz',
            f'{card}/act_freq_mhz',
            f'{card}/act_mhz',
        ])
        min_f = _first_int([
            f'{card}/gt_min_freq_mhz',
            f'{card}/min_freq_mhz',
            f'{card}/min_mhz',
        ])
        max_f = _first_int([
            f'{card}/gt_max_freq_mhz',
            f'{card}/max_freq_mhz',
            f'{card}/max_mhz',
        ])
        if act is not None and min_f is not None and max_f is not None:
            return {'gpu_video_percent': _pct(act, min_f, max_f),
                    'gpu_render_percent': _pct(act, min_f, max_f)}

    return None


def get_gpu_metrics() -> dict[str, float]:
    default_metrics = {
        'gpu_video_percent': 0.0,
        'gpu_render_percent': 0.0,
    }

    # 1. qmassa/qmmd Prometheus metrics — best source for newer Intel Arc/xe
    #    systems when the optional exporter URL is configured.
    qmmd = _get_qmmd_gpu_metrics()
    if qmmd is not None:
        qmmd = _normalize_gpu_metrics(qmmd)
        if _has_nonzero_gpu_activity(qmmd):
            return qmmd

    # 2. intel_gpu_top engine % — accurate per-engine load and power telemetry
    #    when CAP_PERFMON is available.  Preferred over sysfs because it matches
    #    the common GPU Statistics tooling and exposes Intel's own rail readings.
    intel = _get_intel_gpu_metrics()
    if intel is not None:
        intel = _normalize_gpu_metrics(intel)
        if _has_nonzero_gpu_activity(intel):
            return intel

    # 3. Sysfs engine busy_time_ms — accurate per-engine, no special caps needed.
    sysfs = _get_intel_gpu_metrics_sysfs()
    if sysfs is not None:
        sysfs = _normalize_gpu_metrics(sysfs)
        if _has_nonzero_gpu_activity(sysfs):
            return sysfs

    # Keep the best available zero-activity reading as the idle fallback.
    # Prefer qmmd, then intel_gpu_top, then sysfs, then the generic default.
    if qmmd is not None:
        default_metrics = qmmd
    elif intel is not None:
        default_metrics = intel
    elif sysfs is not None:
        default_metrics = sysfs

    # 4. GT clock frequency — last-resort proxy when neither sysfs nor
    #    intel_gpu_top are available.  Reports clock ratio, not true utilisation.
    freq = _get_intel_gpu_metrics_freq()
    if freq is not None:
        freq = _normalize_gpu_metrics(freq)
        if _has_nonzero_gpu_activity(freq):
            return freq

    # 5. NVIDIA via nvidia-smi.
    nvidia = _get_nvidia_gpu_metrics()
    if nvidia is not None:
        return _normalize_gpu_metrics(nvidia)

    return default_metrics


def get_system_metrics(db: Session) -> dict[str, float | int]:
    gpu_metrics = get_gpu_metrics()
    active_encode_jobs = db.query(Job).filter(Job.status.in_(ACTIVE_ENCODE_STATUSES)).count()
    active_download_jobs = db.query(DownloadJob).filter(DownloadJob.status.in_(ACTIVE_DOWNLOAD_STATUSES)).count()
    active_jobs = active_encode_jobs + active_download_jobs

    return {
        **gpu_metrics,
        'cpu_percent': psutil.cpu_percent(interval=0.1),
        'ram_percent': psutil.virtual_memory().percent,
        'active_jobs': active_jobs,
    }
