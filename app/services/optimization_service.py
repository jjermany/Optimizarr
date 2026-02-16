from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Lock
import time
from typing import Any, Callable


FPS_REGEX = re.compile(r"fps\s*=\s*(?P<fps>[0-9]*\.?[0-9]+)")
_ENCODER_CACHE: dict[str, bool] = {}
_SUPPORTED_ENCODERS = {'h264_qsv', 'hevc_qsv', 'av1_qsv', 'libsvtav1', 'libx264', 'libx265'}
_QSV_ERROR_PATTERN = re.compile(r'(qsv|mfx).*(init|error|failed|device)', re.IGNORECASE)
ENCODER_OPTIONS_BY_CODEC = {
    'h264': ['h264_qsv', 'libx264'],
    'hevc': ['hevc_qsv', 'libx265'],
    'av1': ['av1_qsv', 'libsvtav1'],
}
_ACTIVE_FFMPEG_LOCK = Lock()
_ACTIVE_FFMPEG_PROCESSES: dict[int, subprocess.Popen] = {}


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
    encoder_used: str | None = None
    codec_used: str | None = None
    hwaccel_used: bool | None = None
    used_fallback: bool = False
    fallback_reason: str | None = None
    error_message: str | None = None


@dataclass
class EncoderSelection:
    codec: str
    encoder: str
    use_qsv: bool


def _output_path_for(input_path: str) -> str:
    source = Path(input_path)
    return str(source.with_name(f"{source.stem}-1080p.mkv"))


def _container_from_profile(profile: dict[str, Any]) -> str:
    container = str(profile.get('container', 'mkv')).lower().strip('.')
    return container or 'mkv'


def _workspace_root_from_settings(settings: Any) -> Path:
    configured = str(getattr(settings, 'workspace_root', '/cache/workspaces') or '/cache/workspaces')
    return Path(configured)


def _job_workspace_path(settings: Any, job_id: int | None) -> Path:
    workspace_root = _workspace_root_from_settings(settings)
    folder_name = str(job_id) if job_id is not None else 'adhoc'
    return workspace_root / folder_name


def _ensure_clean_workspace(workspace_path: Path) -> None:
    if workspace_path.exists():
        shutil.rmtree(workspace_path, ignore_errors=True)
    workspace_path.mkdir(parents=True, exist_ok=True)


def _output_paths(input_path: str, profile: dict[str, Any], workspace_path: Path) -> tuple[Path, Path]:
    source = Path(input_path)
    resolved_output_path = str(profile.get('resolved_output_path') or '').strip()
    if resolved_output_path:
        final_output_path = Path(resolved_output_path)
        container = final_output_path.suffix.lstrip('.') or _container_from_profile(profile)
    else:
        container = _container_from_profile(profile)
        output_suffix = str(profile.get('output_suffix') or '-1080p')
        final_output_path = source.with_name(f'{source.stem}{output_suffix}.{container}')
    partial_output_path = workspace_path / f'output.partial.{container}'
    return final_output_path, partial_output_path


def _resolve_conflict_target(path: Path) -> Path:
    if not path.exists():
        return path

    version = 2
    candidate = path
    while candidate.exists():
        candidate = path.with_name(f'{path.stem}-v{version}{path.suffix}')
        version += 1
    return candidate


def delete_workspace(settings: Any, job_id: int) -> None:
    workspace_path = _job_workspace_path(settings, job_id)
    shutil.rmtree(workspace_path, ignore_errors=True)


def delete_partial_output(settings: Any, job_id: int) -> None:
    workspace_path = _job_workspace_path(settings, job_id)
    if not workspace_path.exists():
        return
    for partial in workspace_path.glob('output.partial.*'):
        partial.unlink(missing_ok=True)


def stop_active_ffmpeg(job_id: int) -> bool:
    with _ACTIVE_FFMPEG_LOCK:
        process = _ACTIVE_FFMPEG_PROCESSES.get(job_id)
    if process is None:
        return False
    process.terminate()
    return True




def _is_same_filesystem(source_path: Path, destination_dir: Path) -> bool:
    try:
        return os.stat(source_path).st_dev == os.stat(destination_dir).st_dev
    except OSError:
        return False


def _commit_output_file(partial_output_path: Path, final_output_path: Path, job_id: int | None) -> bool:
    final_output_path.parent.mkdir(parents=True, exist_ok=True)

    if _is_same_filesystem(partial_output_path, final_output_path.parent):
        os.replace(partial_output_path, final_output_path)
        return True

    temp_target_name = f'.optimizarr-commit-{job_id or "job"}-{int(time.time() * 1000)}{final_output_path.suffix}'
    temp_target_path = final_output_path.parent / temp_target_name
    try:
        shutil.copy2(partial_output_path, temp_target_path)
        os.replace(temp_target_path, final_output_path)
        partial_output_path.unlink(missing_ok=True)
        return True
    except OSError:
        temp_target_path.unlink(missing_ok=True)
        return False


def refresh_encoder_cache() -> None:
    for name in _SUPPORTED_ENCODERS:
        _ENCODER_CACHE.setdefault(name, False)

    try:
        result = subprocess.run(['ffmpeg', '-hide_banner', '-encoders'], capture_output=True, text=True, check=False)
    except OSError:
        return

    if result.returncode != 0:
        return

    encoder_lines = result.stdout
    for name in _SUPPORTED_ENCODERS:
        _ENCODER_CACHE[name] = bool(re.search(rf"\b{re.escape(name)}\b", encoder_lines))


def _encoder_available(encoder_name: str) -> bool:
    if not _ENCODER_CACHE:
        refresh_encoder_cache()
    if encoder_name not in _ENCODER_CACHE:
        return False
    return _ENCODER_CACHE[encoder_name]


def available_encoders_by_codec() -> dict[str, list[str]]:
    if not _ENCODER_CACHE:
        refresh_encoder_cache()

    available: dict[str, list[str]] = {}
    for codec, candidates in ENCODER_OPTIONS_BY_CODEC.items():
        available[codec] = [name for name in candidates if _encoder_available(name)]
    return available


def _select_encoder(profile: dict[str, Any]) -> EncoderSelection | None:
    codec = str(profile.get('codec', 'h264')).lower()
    candidates = ENCODER_OPTIONS_BY_CODEC.get(codec, [])
    if not candidates:
        return None

    preferred_encoder = str(profile.get('preferred_video_encoder', 'auto')).lower()
    if preferred_encoder and preferred_encoder != 'auto':
        if preferred_encoder in candidates and _encoder_available(preferred_encoder):
            return EncoderSelection(codec=codec, encoder=preferred_encoder, use_qsv=preferred_encoder.endswith('_qsv'))

    for candidate in candidates:
        if _encoder_available(candidate):
            return EncoderSelection(codec=codec, encoder=candidate, use_qsv=candidate.endswith('_qsv'))

    return None


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


def _build_command_with_selection(
    input_path: str,
    output_path: str,
    profile: dict[str, Any],
    selection: EncoderSelection,
) -> list[str]:
    bitrate_mode = str(profile.get('bitrate_mode', 'cbr')).lower()
    speed_preset = str(profile.get('speed_preset', 'medium')).lower()
    audio_mode = str(profile.get('audio_mode', 'copy')).lower()
    target_height = int(profile.get('target_resolution', 1080) or 1080)
    bitrate_mbps = int(profile.get('bitrate_mbps', 8) or 8)
    crf = int(profile.get('crf', 23) or 23)

    source_height = _probe_height(input_path)
    should_scale = source_height is not None and source_height > target_height

    video_preset_args: list[str] = []

    if selection.encoder in {'h264_qsv', 'hevc_qsv', 'av1_qsv'}:
        video_preset_args = ['-preset', _qsv_preset(speed_preset)]
    elif selection.encoder == 'libsvtav1':
        video_preset_args = ['-preset', _svt_av1_preset(speed_preset)]
    else:
        video_preset_args = ['-preset', _software_preset(speed_preset)]

    command = ['ffmpeg', '-i', input_path]
    if selection.use_qsv:
        command = ['ffmpeg', '-hwaccel', 'qsv', '-hwaccel_output_format', 'qsv', '-i', input_path]

    if should_scale:
        scale_filter = f'scale_qsv=-2:{target_height}' if selection.use_qsv else f'scale=-2:{target_height}'
        command.extend(['-vf', scale_filter])

    command.extend(['-c:v', selection.encoder])
    command.extend(video_preset_args)
    command.extend(_video_rate_args(selection.encoder, bitrate_mode, bitrate_mbps, crf, target_height))
    command.extend(_audio_args(audio_mode))
    command.extend(['-progress', 'pipe:1', '-nostats', output_path])
    return command


def build_encoder_command(input_path: str, output_path: str, profile: dict[str, Any]) -> list[str]:
    selection = _select_encoder(profile)
    if not selection:
        codec = str(profile.get('codec', 'h264')).lower()
        fallback_encoder = 'libx264' if codec == 'h264' else 'libx265'
        selection = EncoderSelection(codec=codec, encoder=fallback_encoder, use_qsv=False)
    return _build_command_with_selection(input_path, output_path, profile, selection)


def _has_qsv_error(output_lines: list[str]) -> bool:
    return any(_QSV_ERROR_PATTERN.search(line) for line in output_lines)


def _run_ffmpeg(
    job_id: int | None,
    ffmpeg_command: list[str],
    duration: float | None,
    progress_callback: Callable[[dict[str, float | int | None]], None] | None,
    should_cancel: Callable[[], bool] | None,
) -> tuple[int | None, float, float | None, bool, list[str]]:
    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError:
        return None, 0.0, None, False, []

    if job_id is not None:
        with _ACTIVE_FFMPEG_LOCK:
            _ACTIVE_FFMPEG_PROCESSES[job_id] = process

    current_fps: float | None = None
    processed_seconds = 0.0
    was_cancelled = False
    started_at = time.monotonic()
    output_lines: list[str] = []

    assert process.stdout is not None
    for raw_line in process.stdout:
        if should_cancel and should_cancel():
            was_cancelled = True
            process.terminate()
            break

        line = raw_line.strip()
        if not line:
            continue
        output_lines.append(line)

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
                eta_seconds = int(remaining / processing_rate) if processing_rate > 0 else int(remaining)

            progress_callback({'progress_percent': progress_percent, 'fps': current_fps, 'eta_seconds': eta_seconds})

    process.wait()
    if job_id is not None:
        with _ACTIVE_FFMPEG_LOCK:
            _ACTIVE_FFMPEG_PROCESSES.pop(job_id, None)
    return process.returncode, processed_seconds, current_fps, was_cancelled, output_lines


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
    job_id: int | None = None,
    progress_callback: Callable[[dict[str, float | int | None]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> OptimizationMetrics:
    profile = _profile_from_settings(settings)
    workspace_path = _job_workspace_path(settings, job_id)
    final_output_path, partial_output_path = _output_paths(input_path, profile, workspace_path)
    metrics = OptimizationMetrics(input_path=input_path, output_path=str(final_output_path), status='pending')

    output_conflict_policy = str(profile.get('output_conflict_policy') or 'skip').lower()
    if final_output_path.exists():
        if output_conflict_policy == 'overwrite':
            final_output_path.unlink(missing_ok=True)
        elif output_conflict_policy == 'rename':
            final_output_path = _resolve_conflict_target(final_output_path)
            metrics.output_path = str(final_output_path)
        else:
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

    try:
        _ensure_clean_workspace(workspace_path)
    except OSError:
        metrics.status = 'failed'
        metrics.skipped_reason = 'workspace_prepare_failed'
        metrics.error_message = 'workspace_prepare_failed'
        return metrics

    duration = _probe_duration_seconds(input_path)
    metrics.duration_seconds = duration
    selection = _select_encoder(profile)

    if not selection and str(profile.get('codec', 'h264')).lower() == 'av1':
        metrics.status = 'failed'
        metrics.skipped_reason = 'optimization_failed'
        metrics.error_message = 'AV1 not supported on this host.'
        return metrics
    if not selection:
        profile_codec = str(profile.get('codec', 'h264')).lower()
        fallback_encoder = 'libx264' if profile_codec == 'h264' else 'libx265'
        selection = EncoderSelection(codec=profile_codec, encoder=fallback_encoder, use_qsv=False)

    ffmpeg_command = _build_command_with_selection(input_path, str(partial_output_path), profile, selection)
    return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
        job_id,
        ffmpeg_command,
        duration,
        progress_callback,
        should_cancel,
    )
    if return_code is None:
        metrics.status = 'failed'
        metrics.skipped_reason = 'ffmpeg_unavailable'
        metrics.error_message = 'ffmpeg_unavailable'
        return metrics

    metrics.processed_seconds = processed_seconds
    metrics.fps = current_fps
    metrics.return_code = return_code
    metrics.encoder_used = selection.encoder
    metrics.codec_used = selection.codec
    metrics.hwaccel_used = selection.use_qsv

    if return_code != 0 and selection.use_qsv and selection.codec in {'h264', 'hevc'} and _has_qsv_error(output_lines):
        software_encoder = 'libx264' if selection.codec == 'h264' else 'libx265'
        fallback_selection = EncoderSelection(codec=selection.codec, encoder=software_encoder, use_qsv=False)
        fallback_command = _build_command_with_selection(input_path, str(partial_output_path), profile, fallback_selection)
        return_code, processed_seconds, current_fps, was_cancelled, _ = _run_ffmpeg(
            job_id,
            fallback_command,
            duration,
            progress_callback,
            should_cancel,
        )
        metrics.used_fallback = True
        metrics.fallback_reason = f'{selection.encoder}_failed'
        metrics.encoder_used = fallback_selection.encoder
        metrics.codec_used = fallback_selection.codec
        metrics.hwaccel_used = False
        metrics.processed_seconds = processed_seconds
        metrics.fps = current_fps
        metrics.return_code = return_code

    if return_code != 0 and selection.codec == 'av1':
        av1_fallback_codec = str(profile.get('av1_fallback_codec', 'hevc')).lower()
        if av1_fallback_codec in {'h264', 'hevc'}:
            fallback_encoder = 'libx264' if av1_fallback_codec == 'h264' else 'libx265'
            fallback_selection = EncoderSelection(codec=av1_fallback_codec, encoder=fallback_encoder, use_qsv=False)
            fallback_command = _build_command_with_selection(input_path, str(partial_output_path), profile, fallback_selection)
            return_code, processed_seconds, current_fps, was_cancelled, _ = _run_ffmpeg(
                job_id,
                fallback_command,
                duration,
                progress_callback,
                should_cancel,
            )
            metrics.used_fallback = True
            metrics.fallback_reason = f'av1_encode_failed_fallback_to_{av1_fallback_codec}'
            metrics.encoder_used = fallback_selection.encoder
            metrics.codec_used = fallback_selection.codec
            metrics.hwaccel_used = False
            metrics.processed_seconds = processed_seconds
            metrics.fps = current_fps
            metrics.return_code = return_code

    if was_cancelled:
        metrics.status = 'cancelled'
    elif return_code == 0:
        committed = _commit_output_file(partial_output_path, final_output_path, job_id)
        if committed:
            metrics.status = 'complete'
            shutil.rmtree(workspace_path, ignore_errors=True)
        else:
            metrics.status = 'failed'
            metrics.skipped_reason = 'commit_failed'
            metrics.error_message = 'commit_failed'
    else:
        metrics.status = 'failed'
    if metrics.status == 'failed':
        if not metrics.skipped_reason:
            metrics.skipped_reason = 'optimization_failed'
        if not metrics.error_message:
            metrics.error_message = 'optimization_failed'

    if progress_callback:
        progress_callback(
            {
                'progress_percent': 100 if metrics.status == 'complete' else min(99, int(metrics.processed_seconds)),
                'fps': current_fps,
                'eta_seconds': 0 if metrics.status == 'complete' else None,
            }
        )

    return metrics
