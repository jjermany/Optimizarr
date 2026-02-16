from types import SimpleNamespace

from app.core.database import SessionLocal
from app.models.job import Job
from app.services import monitoring_service


def test_get_gpu_metrics_parses_intel_gpu_top_output(monkeypatch):
    mock_stdout = '{"engines": {"Render/3D": {"busy": 67.5}, "Video": {"busy": 21.25}}}'

    def fake_run(*args, **kwargs):
        return SimpleNamespace(stdout=mock_stdout)

    monkeypatch.setattr(monitoring_service.subprocess, 'run', fake_run)

    metrics = monitoring_service.get_gpu_metrics()

    assert metrics['gpu_video_percent'] == 21.25
    assert metrics['gpu_render_percent'] == 67.5


def test_get_gpu_metrics_defaults_when_command_fails(monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(monitoring_service.subprocess, 'run', fake_run)

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
