from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Callable


FPS_REGEX = re.compile(r"fps\s*=\s*(?P<fps>[0-9]*\.?[0-9]+)")
_ENCODER_CACHE: dict[str, bool] = {}


@dataclass
class OptimizationMetrics:
    input_path: str
    output_path: str
    status: str
    skipped_reason: str | None = None
    height: int | None = None
    duration_seconds: float | None = None
    processed_seconds: float = 0.0
    fps: float | None = None
    return_code: int | None = None


def _output_path_for(input_path: str) -> str:
    source = Path(input_path)
    return str(source.with_name(f"{source.stem}-1080p.mkv"))


def _encoder_available(encoder_name: str) -> bool:
    if encoder_name in _ENCODER_CACHE:
        return _ENCODER_CACHE[encoder_name]

    try:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True, check=False)
    except OSError:
        _ENCODER_CACHE[encoder_name] = False
        return False

    if result.returncode != 0:
        _ENCODER_CACHE[encoder_name] = False
        return False

    available = bool(re.search(rf"\b{re.escape(encoder_name)}\b", result.stdout))
    _ENCODER_CACHE[encoder_name] = available
    return available


def _audio_args(audio_mode: str) -> list[str]:
    if audio_mode == 'copy':
        return ['-c:a', 'copy']
    if audio_mode == 'aac':
        return ['-c:a', 'aac', '-b:a', '192k']
    if audio_mode == 'ac3':
        return ['-c:a', 'ac3', '-b:a', '640k']
    if audio_mode == 'eac3':
        return ['-c:a', 'eac3', '-b:a', '768k']
    return ['-c:a', 'copy']


def _software_preset(speed_preset: str) -> str:
    return {
        'fast': 'veryfast',
        'medium': 'medium',
        'slow': 'slow',
    }.get(speed_preset, 'medium')


def _qsv_preset(speed_preset: str) -> str:
    # These are conservative values supported by modern Intel QSV encoders.
    return {
        'fast': 'faster',
        'medium': 'medium',
        'slow': 'slow',
    }.get(speed_preset, 'medium')


def _svt_av1_preset(speed_preset: str) -> str:
    return {
        'fast': '8',
        'medium': '6',
        'slow': '4',
    }.get(speed_preset, '6')


def _derive_qsv_bitrate_from_crf(crf: int, target_height: int) -> int:
    # QSV quality controls are not consistently CRF-equivalent across codecs/drivers.
    # We emulate CRF by deriving a VBR target bitrate using a simple model:
    # - baseline: CRF 23 @ 1080p ~= 8 Mbps
    # - every 6 CRF points roughly doubles/halves target bitrate
    # - scale by pixel ratio against 1080p (height^2 approximation for similar AR)
    base_bitrate = 8.0
    quality_multiplier = 2 ** ((23 - crf) / 6.0)
    resolution_multiplier = (max(360, target_height) / 1080.0) ** 2
    derived = int(round(base_bitrate * quality_multiplier * resolution_multiplier))
    return max(1, min(80, derived))


def _video_rate_args(codec_impl: str, bitrate_mode: str, bitrate_mbps: int, crf: int, target_height: int) -> list[str]:
    if bitrate_mode == 'cbr':
        return [
            '-b:v',
            f'{bitrate_mbps}M',
            '-maxrate',
            f'{bitrate_mbps + 2}M',
            '-bufsize',
            f'{bitrate_mbps * 2}M',
        ]

    if codec_impl in {'libx264', 'libx265', 'libsvtav1'}:
        return ['-crf', str(crf)]

    if codec_impl in {'h264_qsv', 'hevc_qsv', 'av1_qsv'}:
        derived = _derive_qsv_bitrate_from_crf(crf, target_height)
        return ['-rc:v', 'vbr', '-b:v', f'{derived}M', '-maxrate', f'{derived + 2}M', '-bufsize', f'{derived * 2}M']

    return ['-crf', str(crf)]


def build_encoder_command(input_path: str, output_path: str, profile: dict[str, Any]) -> list[str]:
    codec = str(profile.get('codec', 'h264')).lower()
    bitrate_mode = str(profile.get('bitrate_mode', 'cbr')).lower()
    speed_preset = str(profile.get('speed_preset', 'medium')).lower()
    audio_mode = str(profile.get('audio_mode', 'copy')).lower()
    target_height = int(profile.get('target_resolution', 1080) or 1080)
    bitrate_mbps = int(profile.get('bitrate_mbps', 8) or 8)
    crf = int(profile.get('crf', 23) or 23)

    source_height = _probe_height(input_path)
    should_scale = source_height is not None and source_height > target_height

    use_qsv = False
    video_encoder = 'libx264'
    video_preset_args: list[str] = []

    if codec == 'h264':
        if _encoder_available('h264_qsv'):
            use_qsv = True
            video_encoder = 'h264_qsv'
            video_preset_args = ['-preset', _qsv_preset(speed_preset)]
        else:
            video_encoder = 'libx264'
            video_preset_args = ['-preset', _software_preset(speed_preset)]
    elif codec == 'hevc':
        if _encoder_available('hevc_qsv'):
            use_qsv = True
            video_encoder = 'hevc_qsv'
            video_preset_args = ['-preset', _qsv_preset(speed_preset)]
        else:
            video_encoder = 'libx265'
            video_preset_args = ['-preset', _software_preset(speed_preset)]
    elif codec == 'av1':
        if _encoder_available('av1_qsv'):
            use_qsv = True
            video_encoder = 'av1_qsv'
            video_preset_args = ['-preset', _qsv_preset(speed_preset)]
        elif _encoder_available('libsvtav1'):
            video_encoder = 'libsvtav1'
            video_preset_args = ['-preset', _svt_av1_preset(speed_preset)]
        else:
            video_encoder = 'libx265'
            video_preset_args = ['-preset', _software_preset(speed_preset)]

    command = ['ffmpeg', '-i', input_path]
    if use_qsv:
        command = ['ffmpeg', '-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv', '-i', input_path]

    if should_scale:
        scale_filter = f'scale_qsv=-2:{target_height}' if use_qsv else f'scale=-2:{target_height}'
        command.extend(['-vf', scale_filter])

    command.extend(['-c:v', video_encoder])
    command.extend(video_preset_args)
    command.extend(_video_rate_args(video_encoder, bitrate_mode, bitrate_mbps, crf, target_height))
    command.extend(_audio_args(audio_mode))
    command.extend(['-progress', 'pipe:1', '-nostats', output_path])
    return command


def _run_ffprobe_value(input_path: str, entry: str) -> str | None:
    command = [
        'ffprobe',
        '-v',
        'error',
        '-select_streams',
        'v:0',
        '-show_entries',
        entry,
        '-of',
        'default=noprint_wrappers=1:nokey=1',
        input_path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None

    value = result.stdout.strip().splitlines()
    if not value:
        return None
    return value[-1].strip()


def _probe_height(input_path: str) -> int | None:
    value = _run_ffprobe_value(input_path, 'stream=height')
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def probe_video_height(input_path: str) -> int | None:
    return _probe_height(input_path)


def _probe_duration_seconds(input_path: str) -> float | None:
    value = _run_ffprobe_value(input_path, 'format=duration')
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def is_hdr_video(input_path: str) -> bool:
    transfer = _run_ffprobe_value(input_path, 'stream=color_transfer')
    primaries = _run_ffprobe_value(input_path, 'stream=color_primaries')

    normalized_transfer = (transfer or '').strip().lower()
    normalized_primaries = (primaries or '').strip().lower()

    return normalized_transfer in {'smpte2084', 'arib-std-b67'} or normalized_primaries == 'bt2020'


def _build_ffmpeg_command(input_path: str, output_path: str, bitrate_mbps: int) -> list[str]:
    return build_encoder_command(
        input_path,
        output_path,
        {
            'codec': 'h264',
            'bitrate_mode': 'cbr',
            'speed_preset': 'medium',
            'audio_mode': 'copy',
            'container': 'mkv',
            'target_resolution': 1080,
            'bitrate_mbps': bitrate_mbps,
            'crf': 23,
        },
    )


def _profile_from_settings(settings: Any) -> dict[str, Any]:
    profile_snapshot_json = getattr(settings, 'profile_snapshot_json', None)
    if profile_snapshot_json:
        try:
            parsed = json.loads(profile_snapshot_json)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    return {
        'codec': 'h264',
        'bitrate_mode': 'cbr',
        'speed_preset': 'medium',
        'audio_mode': 'copy',
        'container': 'mkv',
        'target_resolution': int(getattr(settings, 'target_resolution', 1080) or 1080),
        'bitrate_mbps': int(getattr(settings, 'bitrate_mbps', 8) or 8),
        'crf': 23,
    }


def optimize_video(
    input_path: str,
    settings,
    progress_callback: Callable[[dict[str, float | int | None]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> OptimizationMetrics:
    output_path = _output_path_for(input_path)
    metrics = OptimizationMetrics(input_path=input_path, output_path=output_path, status='pending')

    if Path(output_path).exists():
        metrics.status = 'skipped'
        metrics.skipped_reason = 'output_exists'
        return metrics

    height = _probe_height(input_path)
    metrics.height = height
    if height is None:
        metrics.status = 'failed'
        metrics.skipped_reason = 'ffprobe_failed'
        return metrics

    if height < 2000:
        metrics.status = 'skipped'
        metrics.skipped_reason = 'source_height_below_threshold'
        return metrics

    duration = _probe_duration_seconds(input_path)
    metrics.duration_seconds = duration

    ffmpeg_command = build_encoder_command(input_path, output_path, _profile_from_settings(settings))

    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError:
        metrics.status = 'failed'
        metrics.skipped_reason = 'ffmpeg_unavailable'
        return metrics

    current_fps: float | None = None
    processed_seconds = 0.0
    was_cancelled = False
    started_at = time.monotonic()

    assert process.stdout is not None
    for raw_line in process.stdout:
        if should_cancel and should_cancel():
            was_cancelled = True
            process.terminate()
            break

        line = raw_line.strip()
        if not line:
            continue

        if line.startswith('fps='):
            try:
                current_fps = float(line.split('=', maxsplit=1)[1].strip())
            except ValueError:
                current_fps = current_fps
        else:
            fps_match = FPS_REGEX.search(line)
            if fps_match:
                current_fps = float(fps_match.group('fps'))

        if line.startswith('out_time_ms='):
            try:
                out_time_ms = int(line.split('=', maxsplit=1)[1].strip())
                processed_seconds = out_time_ms / 1_000_000
            except ValueError:
                processed_seconds = processed_seconds

        if progress_callback:
            progress_percent = 0
            eta_seconds: int | None = None
            if duration and duration > 0:
                progress_percent = max(0, min(99, int((processed_seconds / duration) * 100)))
                remaining = max(0.0, duration - processed_seconds)
                elapsed = max(0.1, time.monotonic() - started_at)
                processing_rate = processed_seconds / elapsed
                if processing_rate > 0:
                    eta_seconds = int(remaining / processing_rate)
                else:
                    eta_seconds = int(remaining)

            progress_callback(
                {
                    'progress_percent': progress_percent,
                    'fps': current_fps,
                    'eta_seconds': eta_seconds,
                }
            )

    process.wait()

    metrics.processed_seconds = processed_seconds
    metrics.fps = current_fps
    metrics.return_code = process.returncode
    if was_cancelled:
        metrics.status = 'cancelled'
    else:
        metrics.status = 'complete' if process.returncode == 0 else 'failed'

    if progress_callback:
        progress_callback(
            {
                'progress_percent': 100 if metrics.status == 'complete' else min(99, int(metrics.processed_seconds)),
                'fps': current_fps,
                'eta_seconds': 0 if metrics.status == 'complete' else None,
            }
        )

    return metrics
