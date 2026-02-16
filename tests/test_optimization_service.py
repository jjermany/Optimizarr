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
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda _: False)

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


def test_build_encoder_command_prefers_qsv_and_scales(monkeypatch):
    profile = {
        'codec': 'h264',
        'bitrate_mode': 'cbr',
        'speed_preset': 'fast',
        'audio_mode': 'copy',
        'container': 'mkv',
        'target_resolution': 1080,
        'bitrate_mbps': 8,
        'crf': 22,
    }

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 2160)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda name: name == 'h264_qsv')

    command = optimization_service.build_encoder_command('/media/in.mkv', '/media/out.mkv', profile)

    assert command[:8] == ['ffmpeg', '-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv', '-i', '/media/in.mkv', '-vf']
    assert 'scale_qsv=-2:1080' in command
    assert '-c:v' in command and 'h264_qsv' in command
    assert '-b:v' in command and '8M' in command
    assert '-maxrate' in command and '10M' in command
    assert '-bufsize' in command and '16M' in command


def test_build_encoder_command_software_hevc_vbr_crf(monkeypatch):
    profile = {
        'codec': 'hevc',
        'bitrate_mode': 'vbr_crf',
        'speed_preset': 'slow',
        'audio_mode': 'aac',
        'container': 'mp4',
        'target_resolution': 1080,
        'bitrate_mbps': 8,
        'crf': 19,
    }

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1080)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda _: False)

    command = optimization_service.build_encoder_command('/media/in.mkv', '/media/out.mp4', profile)

    assert command[:3] == ['ffmpeg', '-i', '/media/in.mkv']
    assert 'scale=' not in ' '.join(command)
    assert '-c:v' in command and 'libx265' in command
    assert '-preset' in command and 'slow' in command
    assert '-crf' in command and '19' in command
    assert '-c:a' in command and 'aac' in command
    assert '-b:a' in command and '192k' in command


def test_build_encoder_command_av1_uses_svt_when_qsv_unavailable(monkeypatch):
    profile = {
        'codec': 'av1',
        'bitrate_mode': 'vbr_crf',
        'speed_preset': 'medium',
        'audio_mode': 'eac3',
        'container': 'mkv',
        'target_resolution': 720,
        'bitrate_mbps': 5,
        'crf': 30,
    }

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1080)

    def fake_encoder_available(name: str) -> bool:
        return name == 'libsvtav1'

    monkeypatch.setattr(optimization_service, '_encoder_available', fake_encoder_available)

    command = optimization_service.build_encoder_command('/media/in.mkv', '/media/out.mkv', profile)

    assert '-vf' in command and 'scale=-2:720' in command
    assert '-c:v' in command and 'libsvtav1' in command
    assert '-preset' in command and '6' in command
    assert '-crf' in command and '30' in command
    assert '-c:a' in command and 'eac3' in command
    assert '-b:a' in command and '768k' in command
