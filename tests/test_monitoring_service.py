import os
import threading
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

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics == {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}


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
