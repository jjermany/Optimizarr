from pathlib import Path

from app.services import optimization_service


class DummySettings:
    bitrate_mbps = 8


class DummyPopen:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_optimize_video_skips_when_output_exists(tmp_path):
    input_path = tmp_path / 'movie.mkv'
    input_path.write_text('placeholder')
    (tmp_path / 'movie-1080p.mkv').write_text('already optimized')

    metrics = optimization_service.optimize_video(str(input_path), DummySettings())

    assert metrics.status == 'skipped'
    assert metrics.skipped_reason == 'output_exists'


def test_optimize_video_skips_low_height(monkeypatch, tmp_path):
    input_path = tmp_path / 'movie.mkv'
    input_path.write_text('placeholder')

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1080)

    metrics = optimization_service.optimize_video(str(input_path), DummySettings())

    assert metrics.status == 'skipped'
    assert metrics.skipped_reason == 'source_height_below_threshold'


def test_optimize_video_runs_and_reports_progress(monkeypatch, tmp_path):
    input_path = tmp_path / 'movie.mkv'
    input_path.write_text('placeholder')

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 2160)
    monkeypatch.setattr(optimization_service, '_probe_duration_seconds', lambda _: 100.0)

    def fake_popen(*args, **kwargs):
        return DummyPopen(
            [
                'fps=59.9\n',
                'out_time_ms=50000000\n',
                'progress=continue\n',
                'out_time_ms=100000000\n',
                'progress=end\n',
            ],
            returncode=0,
        )

    monkeypatch.setattr(optimization_service.subprocess, 'Popen', fake_popen)

    updates = []
    metrics = optimization_service.optimize_video(
        str(input_path),
        DummySettings(),
        progress_callback=lambda payload: updates.append(payload),
    )

    assert metrics.status == 'complete'
    assert metrics.output_path.endswith('-1080p.mkv')
    assert metrics.fps == 59.9
    assert metrics.processed_seconds == 100.0
    assert updates
    assert any(update['progress_percent'] == 50 for update in updates)
    assert updates[-1]['progress_percent'] == 100


def test_is_hdr_video_detects_hdr_transfer(monkeypatch):
    def fake_probe(_input, entry):
        if entry == 'stream=color_transfer':
            return 'smpte2084'
        if entry == 'stream=color_primaries':
            return 'bt709'
        return None

    monkeypatch.setattr(optimization_service, '_run_ffprobe_value', fake_probe)

    assert optimization_service.is_hdr_video('/media/movie.mkv') is True


def test_is_hdr_video_detects_sdr(monkeypatch):
    def fake_probe(_input, entry):
        if entry == 'stream=color_transfer':
            return 'bt709'
        if entry == 'stream=color_primaries':
            return 'bt709'
        return None

    monkeypatch.setattr(optimization_service, '_run_ffprobe_value', fake_probe)

    assert optimization_service.is_hdr_video('/media/movie.mkv') is False
