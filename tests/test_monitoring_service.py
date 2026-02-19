import os
import threading
from io import StringIO
from types import SimpleNamespace

from app.core.database import SessionLocal
from app.models.job import Job
from app.services import monitoring_service


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


def test_get_gpu_metrics_parses_intel_gpu_top_output(monkeypatch):
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
    mock_stdout = '{"engines": {"Render/3D": {"busy": 0.0}, "Video": {"busy": 0.0}}}\n'

    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(mock_stdout),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 0.0
    assert metrics['gpu_render_percent'] == 0.0


def test_get_gpu_metrics_defaults_when_command_fails(monkeypatch):
    def fake_popen(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(monitoring_service.subprocess, 'Popen', fake_popen)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}


def test_get_gpu_metrics_defaults_when_process_outputs_nothing(monkeypatch):
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *args, **kwargs: _FakePopen(''),
    )
    def _raise_not_found(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(monitoring_service.subprocess, 'run', _raise_not_found)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}


def test_get_intel_gpu_metrics_sysfs_returns_none_when_paths_absent(monkeypatch):
    """Returns None when no engine directories exist under /sys/class/drm/."""
    monkeypatch.setattr(monitoring_service.glob, 'glob', lambda _: [])
    result = monitoring_service._get_intel_gpu_metrics_sysfs()
    assert result is None


def test_get_gpu_metrics_uses_sysfs_when_available(monkeypatch):
    """sysfs-based metrics are preferred over intel_gpu_top."""
    monkeypatch.setattr(
        monitoring_service,
        '_get_intel_gpu_metrics_sysfs',
        lambda: {'gpu_video_percent': 55.0, 'gpu_render_percent': 10.0},
    )
    popen_called = []
    monkeypatch.setattr(
        monitoring_service.subprocess,
        'Popen',
        lambda *a, **k: popen_called.append(True) or _FakePopen(''),
    )

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 55.0
    assert metrics['gpu_render_percent'] == 10.0
    assert not popen_called, 'intel_gpu_top should not be invoked when sysfs succeeds'


def test_get_gpu_metrics_falls_back_to_intel_gpu_top_when_sysfs_absent(monkeypatch):
    """intel_gpu_top is tried when sysfs engine paths do not exist."""
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


def test_get_gpu_metrics_prefers_freq_when_sysfs_is_stuck_at_zero(monkeypatch):
    """Continue probing when sysfs exists but reports 0% during active QSV workloads."""
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
        baseline_active = db.query(Job).filter(~Job.status.in_(monitoring_service.TERMINAL_STATUSES)).count()
        queued = Job(input_path='/media/active.mkv', status='queued')
        done = Job(input_path='/media/done.mkv', status='complete')
        db.add_all([queued, done])
        db.commit()

        metrics = monitoring_service.get_system_metrics(db)

        db.delete(queued)
        db.delete(done)
        db.commit()

    assert metrics == {
        'gpu_video_percent': 12.0,
        'gpu_render_percent': 34.0,
        'cpu_percent': 56.0,
        'ram_percent': 78.0,
        'active_jobs': baseline_active + 1,
    }
