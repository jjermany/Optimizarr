from pathlib import Path

from app.services import optimization_service


class DummySettings:
    bitrate_mbps = 8
    workspace_root = '/cache/workspaces'


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
    assert metrics.skipped_reason == 'source_height_below_target'


def test_optimize_video_runs_and_reports_progress(monkeypatch, tmp_path):
    input_path = tmp_path / 'movie.mkv'
    input_path.write_text('placeholder')

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1440)
    monkeypatch.setattr(optimization_service, '_probe_duration_seconds', lambda _: 100.0)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda _: False)
    monkeypatch.setattr(optimization_service, 'is_hdr_video', lambda _: False)

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
    monkeypatch.setattr(optimization_service, '_commit_output_file', lambda *args, **kwargs: True)

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
    monkeypatch.setattr(optimization_service, '_run_ffprobe_stream_json', lambda _: None)

    def fake_probe(_input, entry):
        if entry == 'stream=color_transfer':
            return 'smpte2084'
        if entry == 'stream=color_primaries':
            return 'bt709'
        return None

    monkeypatch.setattr(optimization_service, '_run_ffprobe_value', fake_probe)

    assert optimization_service.is_hdr_video('/media/movie.mkv') is True


def test_is_hdr_video_detects_sdr(monkeypatch):
    monkeypatch.setattr(optimization_service, '_run_ffprobe_stream_json', lambda _: None)

    def fake_probe(_input, entry):
        if entry == 'stream=color_transfer':
            return 'bt709'
        if entry == 'stream=color_primaries':
            return 'bt709'
        return None

    monkeypatch.setattr(optimization_service, '_run_ffprobe_value', fake_probe)

    assert optimization_service.is_hdr_video('/media/movie.mkv') is False


def test_is_hdr_video_detects_side_data_metadata(monkeypatch):
    monkeypatch.setattr(
        optimization_service,
        '_run_ffprobe_stream_json',
        lambda _: {
            'streams': [
                {
                    'color_transfer': 'bt709',
                    'color_primaries': 'bt709',
                    'color_space': 'bt709',
                    'side_data_list': [{'side_data_type': 'Mastering display metadata'}],
                },
            ],
        },
    )

    assert optimization_service.is_hdr_video('/media/movie.mkv') is True


def test_is_hdr_video_detects_dolby_vision_side_data(monkeypatch):
    monkeypatch.setattr(
        optimization_service,
        '_run_ffprobe_stream_json',
        lambda _: {
            'streams': [
                {
                    'color_transfer': 'bt709',
                    'color_primaries': 'bt709',
                    'color_space': 'bt709',
                    'side_data_list': [{'side_data_type': 'DOVI configuration record'}],
                },
            ],
        },
    )

    assert optimization_service.is_hdr_video('/media/movie.mkv') is True


def test_is_hdr_video_detects_bt2020_10bit(monkeypatch):
    monkeypatch.setattr(
        optimization_service,
        '_run_ffprobe_stream_json',
        lambda _: {
            'streams': [
                {
                    'color_transfer': 'bt709',
                    'color_primaries': 'bt2020',
                    'color_space': 'bt2020nc',
                    'bits_per_raw_sample': '10',
                    'side_data_list': [],
                },
            ],
        },
    )

    assert optimization_service.is_hdr_video('/media/movie.mkv') is True


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


_HDR_TONEMAP_PREFIX = (
    'zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,'
    'tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p'
)


def test_build_encoder_command_hdr_software_applies_tonemap(monkeypatch):
    profile = {
        'codec': 'h264',
        'bitrate_mode': 'cbr',
        'speed_preset': 'medium',
        'audio_mode': 'copy',
        'container': 'mkv',
        'target_resolution': 1080,
        'bitrate_mbps': 8,
        'crf': 23,
        'source_is_hdr': True,
        'tone_map_hdr': True,
    }

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1080)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda _: False)

    command = optimization_service.build_encoder_command('/media/in.mkv', '/media/out.mkv', profile)

    assert '-vf' in command
    vf = command[command.index('-vf') + 1]
    assert vf.startswith(_HDR_TONEMAP_PREFIX)
    assert 'scale=-2:' not in vf  # no scaling needed at same resolution


def test_build_encoder_command_hdr_software_applies_tonemap_and_scale(monkeypatch):
    profile = {
        'codec': 'h264',
        'bitrate_mode': 'cbr',
        'speed_preset': 'medium',
        'audio_mode': 'copy',
        'container': 'mkv',
        'target_resolution': 1080,
        'bitrate_mbps': 8,
        'crf': 23,
        'source_is_hdr': True,
        'tone_map_hdr': True,
    }

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 2160)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda _: False)

    command = optimization_service.build_encoder_command('/media/in.mkv', '/media/out.mkv', profile)

    assert '-vf' in command
    vf = command[command.index('-vf') + 1]
    assert vf.startswith(_HDR_TONEMAP_PREFIX)
    assert vf.endswith(',scale=-2:1080')


def test_build_encoder_command_hdr_vaapi_applies_tonemap(monkeypatch):
    profile = {
        'codec': 'h264',
        'bitrate_mode': 'cbr',
        'speed_preset': 'medium',
        'audio_mode': 'copy',
        'container': 'mkv',
        'target_resolution': 1080,
        'bitrate_mbps': 8,
        'crf': 23,
        'source_is_hdr': True,
        'tone_map_hdr': True,
    }

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 2160)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda name: name == 'h264_vaapi')

    command = optimization_service.build_encoder_command('/media/in.mkv', '/media/out.mkv', profile)

    # VAAPI HDR path uses hardware tonemap_vaapi (Intel VEBOX) instead of software zscale chain.
    assert '-hwaccel' in command
    assert 'vaapi' in command
    assert '-hwaccel_output_format' in command
    assert '-vf' in command
    vf = command[command.index('-vf') + 1]
    assert vf.startswith('tonemap_vaapi=')
    assert vf.endswith(',scale_vaapi=-2:1080')


def test_build_encoder_command_hdr_qsv_applies_tonemap(monkeypatch):
    profile = {
        'codec': 'h264',
        'bitrate_mode': 'cbr',
        'speed_preset': 'medium',
        'audio_mode': 'copy',
        'container': 'mkv',
        'target_resolution': 1080,
        'bitrate_mbps': 8,
        'crf': 23,
        'source_is_hdr': True,
        'tone_map_hdr': True,
    }

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 2160)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda name: name == 'h264_qsv')

    command = optimization_service.build_encoder_command('/media/in.mkv', '/media/out.mkv', profile)

    assert '-vf' in command
    vf = command[command.index('-vf') + 1]
    assert vf.startswith(_HDR_TONEMAP_PREFIX)
    assert 'format=nv12,hwupload=extra_hw_frames=64' in vf
    assert vf.endswith(',scale_qsv=-1:1080')


def test_refresh_encoder_cache_parses_ffmpeg_encoders(monkeypatch):
    class Result:
        returncode = 0
        stdout = ' V..... h264_qsv\n V..... libx264\n V..... libx265\n'

    monkeypatch.setattr(optimization_service.subprocess, 'run', lambda *args, **kwargs: Result())
    optimization_service._ENCODER_CACHE.clear()

    optimization_service.refresh_encoder_cache()

    assert optimization_service._encoder_available('h264_qsv') is True
    assert optimization_service._encoder_available('hevc_qsv') is False
    assert optimization_service._encoder_available('libx264') is True


def test_optimize_video_fails_when_av1_not_supported(monkeypatch, tmp_path):
    input_path = tmp_path / 'movie.mkv'
    input_path.write_text('placeholder')

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1440)
    monkeypatch.setattr(optimization_service, '_probe_duration_seconds', lambda _: 30.0)
    monkeypatch.setattr(optimization_service, '_select_encoder', lambda profile: None)

    class AV1Settings:
        profile_snapshot_json = '{"codec":"av1","av1_fallback_codec":"hevc"}'

    metrics = optimization_service.optimize_video(str(input_path), AV1Settings())

    assert metrics.status == 'failed'
    assert metrics.error_message == 'AV1 not supported on this host.'


def test_optimize_video_fails_when_h264_qsv_encode_fails(monkeypatch, tmp_path):
    input_path = tmp_path / 'movie.mkv'
    input_path.write_text('placeholder')

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1440)
    monkeypatch.setattr(optimization_service, '_probe_duration_seconds', lambda _: 100.0)
    monkeypatch.setattr(
        optimization_service,
        '_select_encoder',
        lambda profile: optimization_service.EncoderSelection(codec='h264', encoder='h264_qsv', use_qsv=True),
    )
    calls = {'count': 0}

    def fake_run_ffmpeg(*args, **kwargs):
        calls['count'] += 1
        return 1, 1.0, None, False, ['qsv init failed']

    monkeypatch.setattr(optimization_service, '_run_ffmpeg', fake_run_ffmpeg)
    metrics = optimization_service.optimize_video(str(input_path), DummySettings())

    assert calls['count'] == 1
    assert metrics.status == 'failed'
    assert metrics.used_fallback is False
    assert metrics.error_message == 'qsv_encode_failed'





def test_select_encoder_honors_unavailable_explicit_preference(monkeypatch):
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda name: False)

    selection = optimization_service._select_encoder({'codec': 'h264', 'preferred_video_encoder': 'h264_qsv'})

    assert selection is not None
    assert selection.encoder == 'h264_qsv'
    assert selection.use_qsv is True
    assert selection.is_explicit_preference is True


def test_optimize_video_fails_without_fallback_when_qsv_is_explicitly_selected(monkeypatch, tmp_path):
    input_path = tmp_path / 'movie.mkv'
    input_path.write_text('placeholder')

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1440)
    monkeypatch.setattr(optimization_service, '_probe_duration_seconds', lambda _: 100.0)
    monkeypatch.setattr(
        optimization_service,
        '_select_encoder',
        lambda profile: optimization_service.EncoderSelection(
            codec='h264',
            encoder='h264_qsv',
            use_qsv=True,
            is_explicit_preference=True,
        ),
    )

    calls = {'count': 0}

    def fake_run_ffmpeg(*args, **kwargs):
        calls['count'] += 1
        return 1, 1.0, None, False, ['qsv init failed']

    monkeypatch.setattr(optimization_service, '_run_ffmpeg', fake_run_ffmpeg)

    metrics = optimization_service.optimize_video(str(input_path), DummySettings())

    assert calls['count'] == 1
    assert metrics.status == 'failed'
    assert metrics.used_fallback is False
    assert metrics.error_message == 'qsv_encode_failed'

def test_optimize_video_writes_partial_in_workspace(monkeypatch, tmp_path):
    media_dir = tmp_path / 'media'
    media_dir.mkdir()
    input_path = media_dir / 'movie.mkv'
    input_path.write_text('placeholder')

    workspace_root = tmp_path / 'workspaces'

    class LocalSettings(DummySettings):
        pass

    LocalSettings.workspace_root = str(workspace_root)

    monkeypatch.setattr(optimization_service, '_probe_height', lambda _: 1440)
    monkeypatch.setattr(optimization_service, '_probe_duration_seconds', lambda _: 10.0)
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda _: False)

    captured = {}

    def fake_run_ffmpeg(job_id, command, *args, **kwargs):
        captured['job_id'] = job_id
        captured['command'] = command
        return 0, 10.0, 24.0, False, []

    monkeypatch.setattr(optimization_service, '_run_ffmpeg', fake_run_ffmpeg)
    monkeypatch.setattr(optimization_service, '_commit_output_file', lambda *args, **kwargs: True)

    metrics = optimization_service.optimize_video(str(input_path), LocalSettings(), job_id=22)

    assert metrics.status == 'complete'
    assert captured['job_id'] == 22
    assert str(workspace_root / '22' / 'output.partial.mkv') in captured['command']
    assert not (media_dir / 'output.partial.mkv').exists()
    assert not (workspace_root / '22').exists()


def test_commit_output_file_cross_filesystem_path(monkeypatch, tmp_path):
    partial = tmp_path / 'workspace' / 'output.partial.mkv'
    partial.parent.mkdir(parents=True)
    partial.write_text('data')

    destination_dir = tmp_path / 'media'
    destination_dir.mkdir()
    final = destination_dir / 'movie-1080p.mkv'

    monkeypatch.setattr(optimization_service, '_is_same_filesystem', lambda *_args, **_kwargs: False)

    assert optimization_service._commit_output_file(partial, final, 5) is True
    assert final.exists()
    assert not partial.exists()
    assert all('partial' not in p.name and 'temp' not in p.name for p in destination_dir.iterdir())


def test_select_encoder_uses_preferred_when_available(monkeypatch):
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda name: name in {'libx264', 'h264_qsv'})
    selection = optimization_service._select_encoder({'codec': 'h264', 'preferred_video_encoder': 'libx264'})
    assert selection is not None
    assert selection.encoder == 'libx264'
    assert selection.use_qsv is False


def test_select_encoder_honors_preferred_even_when_alternatives_available(monkeypatch):
    monkeypatch.setattr(optimization_service, '_encoder_available', lambda name: name == 'h264_qsv')
    selection = optimization_service._select_encoder({'codec': 'h264', 'preferred_video_encoder': 'libx264'})
    assert selection is not None
    assert selection.encoder == 'libx264'
    assert selection.is_explicit_preference is True
