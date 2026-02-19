from __future__ import annotations

import glob
import json
import os
import select
import subprocess
import time
from typing import Any

import psutil
from sqlalchemy.orm import Session

from app.models.job import Job

TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}


def _safe_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _extract_percent(stats: Any, preferred_labels: tuple[str, ...]) -> float:
    if isinstance(stats, dict):
        engine_values = []
        for key, value in stats.items():
            key_lower = str(key).lower()
            if isinstance(value, dict):
                if any(label in key_lower for label in preferred_labels):
                    busy_value = _safe_float(value.get('busy'))
                    if busy_value:
                        engine_values.append(busy_value)
                nested_value = _extract_percent(value, preferred_labels)
                if nested_value:
                    engine_values.append(nested_value)
            elif any(label in key_lower for label in preferred_labels):
                parsed_value = _safe_float(value)
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
            ready, _, _ = select.select([process.stdout], [], [], remaining)
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
            'gpu_video_percent': gpu_util,
            'gpu_render_percent': gpu_util,
        }
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError, IndexError):
        return None


def _get_intel_gpu_metrics_sysfs() -> dict[str, float] | None:
    """Read Intel GPU engine utilisation from sysfs (kernel ≥ 5.11).

    Works inside Docker containers without SYS_ADMIN or perf_event_open because
    the engine busy_time_ms counters under /sys/class/drm/ are world-readable.
    Two readings 500 ms apart are used to derive the utilisation percentage.
    Returns None if the engine sysfs hierarchy is absent on this host.
    """
    engine_dirs = glob.glob('/sys/class/drm/card*/engine/*')
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
        if any(lbl in name for lbl in ('vcs', 'vd', 'video')):
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

    for card in sorted(glob.glob('/sys/class/drm/card*')):
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
    default_metrics = {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}

    # 1. Sysfs engine busy_time_ms — accurate per-engine, no special caps needed.
    sysfs = _get_intel_gpu_metrics_sysfs()
    if sysfs is not None:
        return sysfs

    # 2. GT clock frequency — best proxy for Intel VDEnc/QSV which runs in
    #    sub-millisecond bursts that perf counters miss.  No CAP_PERFMON needed.
    freq = _get_intel_gpu_metrics_freq()
    if freq is not None:
        return freq

    # 3. intel_gpu_top engine % — accurate for 3D/Compute, requires CAP_PERFMON.
    intel = _get_intel_gpu_metrics()
    if intel is not None:
        return intel

    # 4. NVIDIA via nvidia-smi.
    nvidia = _get_nvidia_gpu_metrics()
    if nvidia is not None:
        return nvidia

    return default_metrics


def get_system_metrics(db: Session) -> dict[str, float | int]:
    gpu_metrics = get_gpu_metrics()
    active_jobs = db.query(Job).filter(~Job.status.in_(TERMINAL_STATUSES)).count()

    return {
        **gpu_metrics,
        'cpu_percent': psutil.cpu_percent(interval=0.1),
        'ram_percent': psutil.virtual_memory().percent,
        'active_jobs': active_jobs,
    }
