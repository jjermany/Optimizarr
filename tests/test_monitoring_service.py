import os
import threading
from io import StringIO
from types import SimpleNamespace

from app.core.database import SessionLocal
from app.models.download_job import DownloadJob
from app.models.job import Job
from app.services import monitoring_service


def _disable_qmmd_auto_discovery(monkeypatch):
    monkeypatch.delenv(monitoring_service.QMMD_METRICS_URL_ENV, raising=False)
    monkeypatch.setenv(monitoring_service.QMMD_AUTO_DISCOVERY_ENV, 'false')
    monkeypatch.setattr(monitoring_service, '_QMMD_DISCOVERED_METRICS_URL', None)


class _FakePopen:
    """Minimal Popen stand-in that delivers canned output via a real OS pipe.

    select.select() requires a real file descriptor, so we create an os.pipe()
    pair and write the fixture data from a background thread (mirroring how the
    real intel_gpu_top streams its JSON output).
    """

    def __init__(self, stdout_content: str) -> None:
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, 'r')

        def _writer() -> None:
            with os.fdopen(write_fd, 'w') as f:
                f.write(stdout_content)

        threading.Thread(target=_writer, daemon=True).start()

    def kill(self) -> None:  # noqa: D401
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


class _FakeUrlResponse:
    def __init__(self, content: str) -> None:
        self.content = content

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        pass

    def read(self, size: int = -1) -> bytes:
        return self.content.encode()


def test_qmmd_candidate_urls_include_docker_host_gateway(monkeypatch):
    monkeypatch.delenv(monitoring_service.QMMD_METRICS_URL_ENV, raising=False)
    monkeypatch.delenv(monitoring_service.QMMD_AUTO_DISCOVERY_ENV, raising=False)
    monkeypatch.setattr(monitoring_service, '_docker_host_gateway_ip', lambda: '172.18.0.1')

    assert monitoring_service._qmmd_candidate_urls() == [
        'http://host.docker.internal:9000/metrics',
        'http://172.18.0.1:9000/metrics',
        'http://172.17.0.1:9000/metrics',
    ]


def test_get_gpu_metrics_auto_discovers_qmmd_on_docker_host(monkeypatch):
    raw_metrics = (
        'qmmd_gpu_engine_utilization_ratio{device="0000:00:02.0",engine="vcs"} 0.25\n'
        'qmmd_gpu_engine_utilization_ratio{device="0000:00:02.0",engine="rcs"} 0.5\n'
    )
    calls = []

    monkeypatch.delenv(monitoring_service.QMMD_METRICS_URL_ENV, raising=False)
    monkeypatch.delenv(monitoring_service.QMMD_AUTO_DISCOVERY_ENV, raising=False)
    monkeypatch.setattr(monitoring_service, '_QMMD_DISCOVERED_METRICS_URL', None)
    monkeypatch.setattr(monitoring_service, '_docker_host_gateway_ip', lambda: '172.18.0.1')

    def fake_urlopen(url, timeout=1.5):
        calls.append((url, timeout))
        if url == 'http://host.docker.internal:9000/metrics':
            return _FakeUrlResponse(raw_metrics)
        raise OSError(url)

    monkeypatch.setattr(monitoring_service, 'urlopen', fake_urlopen)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {
        'gpu_video_percent': 25.0,
        'gpu_render_percent': 50.0,
    }
    assert calls == [('http://host.docker.internal:9000/metrics', 0.35)]
    assert monitoring_service._QMMD_DISCOVERED_METRICS_URL == 'http://host.docker.internal:9000/metrics'


def test_get_gpu_metrics_prefers_qmmd_when_configured(monkeypatch):
    raw_metrics = """
# TYPE qmmd_gpu_engine_utilization_ratio gauge
qmmd_gpu_engine_utilization_ratio{device="0000:03:00.0",engine="ccs"} 0.9698813172214144
qmmd_gpu_engine_utilization_ratio{device="0000:03:00.0",engine="rcs"} 0.021016510095973474
qmmd_gpu_engine_utilization_ratio{device="0000:03:00.0",engine="vcs"} 0.42
"""
    monkeypatch.setenv(monitoring_service.QMMD_METRICS_URL_ENV, 'http://127.0.0.1:9753/metrics')
    monkeypatch.setattr(
        monitoring_service,
        'urlopen',
        lambda url, timeout=1.5: _FakeUrlResponse(raw_metrics),
    )
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics',
        lambda: {'gpu_video_percent': 1.0, 'gpu_render_percent': 2.0},
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 42.0
    assert metrics['gpu_render_percent'] == 96.98813172214144


def test_get_gpu_metrics_parses_intel_gpu_top_output(monkeypatch):
    _disable_qmmd_auto_discovery(monkeypatch)
    mock_stdout = '{"engines": {"Render/3D": {"busy": 67.5}, "Video": {"busy": 21.25}}}\n'

    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 21.25
    assert metrics['gpu_render_percent'] == 67.5


def test_get_gpu_metrics_returns_zero_when_intel_gpu_idle(monkeypatch):
    """Intel GPU at 0% utilisation should return 0.0, not fall back to defaults."""
    _disable_qmmd_auto_discovery(monkeypatch)
    mock_stdout = '{"engines": {"Render/3D": {"busy": 0.0}, "Video": {"busy": 0.0}}}\n'

    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics_freq', lambda: None)
    # Keep the test focused on the Intel path; avoid subprocess.run hitting the
    # fake Popen context-manager mismatch in NVIDIA probing.
    monkeypatch.setattr(monitoring_service, '_get_nvidia_gpu_metrics', lambda: None)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 0.0
    assert metrics['gpu_render_percent'] == 0.0


def test_get_gpu_metrics_defaults_when_command_fails(monkeypatch):
    _disable_qmmd_auto_discovery(monkeypatch)
    def fake_popen(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(monitoring_service.subprocess, 'Popen', fake_popen)
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics_freq', lambda: None)
    monkeypatch.setattr(monitoring_service, '_get_nvidia_gpu_metrics', lambda: None)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}


def test_get_gpu_metrics_defaults_when_process_outputs_nothing(monkeypatch):
    _disable_qmmd_auto_discovery(monkeypatch)
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(''),
    )
    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(monitoring_service.subprocess, 'run', _raise_not_found)
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics_freq', lambda: None)
    monkeypatch.setattr(monitoring_service, '_get_nvidia_gpu_metrics', lambda: None)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}


def test_get_intel_gpu_metrics_sysfs_returns_none_when_paths_absent(monkeypatch):
    """Returns None when no engine directories exist under /sys/class/drm/."""
    monkeypatch.setattr(monitoring_service.glob, 'glob', lambda _: [])
    result = monitoring_service._get_intel_gpu_metrics_sysfs()
    assert result is None


def test_get_gpu_metrics_prefers_intel_gpu_top_when_available(monkeypatch):
    """intel_gpu_top is preferred because it matches common GPU stats tools."""
    _disable_qmmd_auto_discovery(monkeypatch)
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_sysfs',
        lambda: {'gpu_video_percent': 55.0, 'gpu_render_percent': 10.0},
    )
    mock_stdout = '{"engines": {"Video": {"busy": 30.0}, "Render/3D": {"busy": 5.0}}}\n'
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 30.0
    assert metrics['gpu_render_percent'] == 5.0


def test_get_gpu_metrics_falls_back_to_sysfs_when_intel_gpu_top_unavailable(monkeypatch):
    _disable_qmmd_auto_discovery(monkeypatch)
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_sysfs',
        lambda: {'gpu_video_percent': 55.0, 'gpu_render_percent': 10.0},
    )
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics', lambda: None)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 55.0
    assert metrics['gpu_render_percent'] == 10.0


def test_get_gpu_metrics_falls_back_to_intel_gpu_top_when_sysfs_absent(monkeypatch):
    """intel_gpu_top is tried when sysfs engine paths do not exist."""
    _disable_qmmd_auto_discovery(monkeypatch)
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics_sysfs', lambda: None)
    mock_stdout = '{"engines": {"Video": {"busy": 30.0}, "Render/3D": {"busy": 5.0}}}\n'
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 30.0
    assert metrics['gpu_render_percent'] == 5.0


def test_get_gpu_metrics_uses_intel_gpu_top_when_sysfs_absent_and_freq_is_zero(monkeypatch):
    """intel_gpu_top must be tried even when sysfs is None and freq reports 0%.

    This is the core bug: previously a ``if sysfs is None: return freq`` short-
    circuit returned 0 without ever calling intel_gpu_top, even when the caller
    supplied --cap-add=PERFMON specifically to enable it.
    """
    _disable_qmmd_auto_discovery(monkeypatch)
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics_sysfs', lambda: None)
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_freq',
        lambda: {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0},
    )
    mock_stdout = '{"engines": {"Video": {"busy": 45.0}, "Render/3D": {"busy": 12.0}}}\n'
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 45.0
    assert metrics['gpu_render_percent'] == 12.0


def test_get_gpu_metrics_prefers_freq_when_sysfs_is_stuck_at_zero(monkeypatch):
    """Continue probing when sysfs exists but reports 0% during active QSV workloads."""
    _disable_qmmd_auto_discovery(monkeypatch)
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics', lambda: None)
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_sysfs',
        lambda: {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0},
    )
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_freq',
        lambda: {'gpu_video_percent': 62.5, 'gpu_render_percent': 62.5},
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {'gpu_video_percent': 62.5, 'gpu_render_percent': 62.5}


def test_get_gpu_metrics_keeps_sysfs_zero_when_higher_signal_unavailable(monkeypatch):
    """If every richer probe is unavailable, keep sysfs 0% as the idle baseline."""
    _disable_qmmd_auto_discovery(monkeypatch)
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_sysfs',
        lambda: {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0},
    )
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics_freq', lambda: None)
    monkeypatch.setattr(monitoring_service, '_get_intel_gpu_metrics', lambda: None)
    monkeypatch.setattr(monitoring_service, '_get_nvidia_gpu_metrics', lambda: None)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}


def test_get_gpu_metrics_parses_intel_gpu_top_string_busy_values(monkeypatch):
    _disable_qmmd_auto_discovery(monkeypatch)
    mock_stdout = '{"engines": {"Render/3D": {"busy": "67.5"}, "Video": {"busy": "21.25%"}}}\n'

    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_sysfs',
        lambda: None,
    )
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_freq',
        lambda: None,
    )
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 21.25
    assert metrics['gpu_render_percent'] == 67.5


def test_get_gpu_metrics_clamps_intel_gpu_top_busy(monkeypatch):
    _disable_qmmd_auto_discovery(monkeypatch)
    mock_stdout = (
        '{"engines": {"Render/3D": {"busy": 123.4}, "Video": {"busy": "101.2%"}}}\n'
    )

    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_sysfs',
        lambda: None,
    )
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_freq',
        lambda: None,
    )
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 100.0
    assert metrics['gpu_render_percent'] == 100.0


def test_get_intel_gpu_metrics_freq_supports_alternate_gt_filenames(monkeypatch):
    monkeypatch.setattr(monitoring_service.glob, 'glob', lambda pattern: {
        '/sys/class/drm/card*': ['/sys/class/drm/card0'],
        '/sys/class/drm/card0/gt/gt*': ['/sys/class/drm/card0/gt/gt0', '/sys/class/drm/card0/gt/gt1'],
    }.get(pattern, []))

    values = {
        '/sys/class/drm/card0/gt/gt0/act_freq_mhz': '1400',
        '/sys/class/drm/card0/gt/gt0/min_freq_mhz': '550',
        '/sys/class/drm/card0/gt/gt0/max_freq_mhz': '2000',
        '/sys/class/drm/card0/gt/gt1/act_mhz': '100',
        '/sys/class/drm/card0/gt/gt1/min_mhz': '100',
        '/sys/class/drm/card0/gt/gt1/max_mhz': '1400',
    }

    def fake_open(path, *args, **kwargs):
        if path in values:
            return StringIO(values[path])
        raise FileNotFoundError(path)

    monkeypatch.setattr('builtins.open', fake_open)

    metrics = monitoring_service._get_intel_gpu_metrics_freq()

    assert metrics == {
        'gpu_video_percent': 58.62068965517241,
        'gpu_render_percent': 58.62068965517241,
    }


def test_get_system_metrics_includes_cpu_ram_and_active_jobs(monkeypatch):
    monkeypatch.setattr(
        monitoring_service,
        'get_gpu_metrics',
        lambda: {'gpu_video_percent': 12.0, 'gpu_render_percent': 34.0},
    )
    monkeypatch.setattr(monitoring_service.psutil, 'cpu_percent', lambda interval=0.1: 56.0)
    monkeypatch.setattr(monitoring_service.psutil, 'virtual_memory', lambda: SimpleNamespace(percent=78.0))

    with SessionLocal() as db:
        baseline_encode = db.query(Job).filter(Job.status.in_(monitoring_service.ACTIVE_ENCODE_STATUSES)).count()
        baseline_download = db.query(DownloadJob).filter(
            DownloadJob.status.in_(monitoring_service.ACTIVE_DOWNLOAD_STATUSES),
        ).count()
        queued = Job(input_path='/media/active.mkv', status='queued')
        running = Job(input_path='/media/processing.mkv', status='running')
        done = Job(input_path='/media/done.mkv', status='complete')
        downloading = DownloadJob(source_file_path='/downloads/a.mkv', status='downloading')
        pending_download = DownloadJob(source_file_path='/downloads/b.mkv', status='pending')
        db.add_all([queued, running, done, downloading, pending_download])
        db.commit()

        metrics = monitoring_service.get_system_metrics(db)

        db.delete(queued)
        db.delete(running)
        db.delete(done)
        db.delete(downloading)
        db.delete(pending_download)
        db.commit()

    assert metrics == {
        'gpu_video_percent': 12.0,
        'gpu_render_percent': 34.0,
        'cpu_percent': 56.0,
        'ram_percent': 78.0,
        'active_jobs': baseline_encode + baseline_download + 2,
    }

