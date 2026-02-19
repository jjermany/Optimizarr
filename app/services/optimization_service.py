from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
from threading import Lock
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

FPS_REGEX = re.compile(r"fps\s*=\s*(?P<fps>[0-9]*\.?[0-9]+)")
_ENCODER_CACHE: dict[str, bool] = {}
_SUPPORTED_ENCODERS = {
    'h264_qsv', 'hevc_qsv', 'av1_qsv',
    'h264_vaapi', 'hevc_vaapi', 'av1_vaapi',
    'libsvtav1', 'libx264', 'libx265',
}
_QSV_ERROR_PATTERN = re.compile(
    r'(qsv|mfx|vaapi|dri|renderD|hw_device|hwdevice|hwaccel|opencl)'
    r'.*(init|error|failed|device|open|create|alloc|load|unavailable|unsupported|permission|access)',
    re.IGNORECASE,
)
_FFMPEG_ERROR_PATTERN = re.compile(r'\b(error|failed|invalid|cannot|unable|no such|permission denied)\b', re.IGNORECASE)
# HDR-to-SDR software tone mapping filter chain.
# Converts HDR10/HLG (BT.2020 / PQ / HLG) to SDR BT.709 using zscale + tonemap.
# Used as a fallback when VAAPI hardware tone mapping is unavailable.
_HDR_TONEMAP_FILTERS = [
    'zscale=t=linear:npl=100',
    'format=gbrpf32le',
    'zscale=p=bt709',
    'tonemap=hable:desat=0',
    'zscale=t=bt709:m=bt709:r=tv',
    'format=yuv420p',
]
# HDR-to-SDR hardware tone mapping via Intel VEBOX (tonemap_vaapi).
# Requires the input to already be in a VAAPI surface (hardware decode).
# Produces NV12 output in VAAPI memory, ready for VAAPI encode.
# Used only for HDR10 and HLG (static metadata) where VEBOX is reliable.
# DV and HDR10+ use VAAPI hw-decode + hwdownload + CPU zscale instead —
# see the 'dolby_vision'/'hdr10plus' branch in _build_command_with_selection.
_VAAPI_TONEMAP_FILTER = 'tonemap_vaapi=format=nv12:p=bt709:t=bt709:m=bt709'
# HDR-to-SDR GPU tone mapping via libplacebo (Vulkan backend).
# Operates on CPU-side p010le frames (after hwdownload) but offloads the heavy
# tone-mapping maths to the GPU via Vulkan — far faster than the CPU zscale chain
# for 4K content.  bt.2390 is the ITU-recommended EETF; produces excellent
# highlight roll-off compared with Hable.  Used for DV/HDR10+ when available.
_LIBPLACEBO_TONEMAP_FILTER = (
    'libplacebo=tonemapping=bt.2390'
    ':colorspace=bt709:color_primaries=bt709:color_trc=bt709'
    ':format=nv12'
)
ENCODER_OPTIONS_BY_CODEC = {
    # VAAPI is listed first: it uses the iHD driver directly (same as Plex) and
    # works without the Intel oneVPL GPU runtime that QSV requires.  QSV remains
    # as a secondary option so it can still be selected explicitly or used if a
    # future image ships a working VPL runtime.
    'h264': ['h264_vaapi', 'h264_qsv', 'libx264'],
    'hevc': ['hevc_vaapi', 'hevc_qsv', 'libx265'],
    'av1': ['av1_vaapi', 'av1_qsv', 'libsvtav1'],
}
_ACTIVE_FFMPEG_LOCK = Lock()
_ACTIVE_FFMPEG_PROCESSES: dict[int, subprocess.Popen] = {}
_ACTIVE_FFMPEG_POSITIONS: dict[int, float] = {}
# When QSV fails at runtime (MFX session error), try the VAAPI equivalent before
# giving up.  VAAPI uses the iHD driver directly — no VPL runtime needed.
_QSV_VAAPI_FALLBACK: dict[str, str] = {
    'h264_qsv': 'h264_vaapi',
    'hevc_qsv': 'hevc_vaapi',
    'av1_qsv': 'av1_vaapi',
}
# When VAAPI tonemap_vaapi (VEBOX) fails for HDR10/HLG content (unexpected driver
# or bitstream edge case), try the QSV equivalent before falling back to CPU zscale.
# DV and HDR10+ skip tonemap_vaapi entirely and never reach this path — they are
# handled proactively with VAAPI hw-decode + hwdownload + CPU zscale.
_VAAPI_QSV_FALLBACK: dict[str, str] = {
    'h264_vaapi': 'h264_qsv',
    'hevc_vaapi': 'hevc_qsv',
    'av1_vaapi': 'av1_qsv',
}


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
    use_vaapi: bool = False
    is_explicit_preference: bool = False
    hw_decode: bool = False
    # When True, skip vpp_qsv tonemap and fall back to CPU (zscale) tone mapping.
    # Set after a vpp_qsv tonemap failure; QSV encoding is preserved, only the
    # tone-mapping step moves to CPU.
    force_sw_tonemap: bool = False


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


def get_active_position(job_id: int) -> float | None:
    """Return the most recently reported encode position (seconds) for an active job."""
    with _ACTIVE_FFMPEG_LOCK:
        return _ACTIVE_FFMPEG_POSITIONS.get(job_id)


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
        logger.error('ffmpeg not found or not executable — encoder cache could not be populated')
        return

    if result.returncode != 0:
        logger.error('ffmpeg -encoders exited with code %s; encoder cache not populated', result.returncode)
        return

    encoder_lines = result.stdout
    for name in _SUPPORTED_ENCODERS:
        _ENCODER_CACHE[name] = bool(re.search(rf"\b{re.escape(name)}\b", encoder_lines))

    available = [n for n, ok in _ENCODER_CACHE.items() if ok]
    unavailable = [n for n, ok in _ENCODER_CACHE.items() if not ok]
    logger.info('Encoder cache refreshed — available: %s; unavailable: %s', available, unavailable)


def _encoder_available(encoder_name: str) -> bool:
    if not _ENCODER_CACHE:
        refresh_encoder_cache()
    if encoder_name not in _ENCODER_CACHE:
        return False
    return _ENCODER_CACHE[encoder_name]


_LIBPLACEBO_AVAILABLE: bool | None = None


def _libplacebo_available() -> bool:
    """Return True if the FFmpeg build includes the libplacebo filter (Vulkan GPU tonemap)."""
    global _LIBPLACEBO_AVAILABLE
    if _LIBPLACEBO_AVAILABLE is None:
        try:
            result = subprocess.run(
                ['ffmpeg', '-hide_banner', '-filters'],
                capture_output=True, text=True, check=False,
            )
            _LIBPLACEBO_AVAILABLE = 'libplacebo' in result.stdout
        except OSError:
            _LIBPLACEBO_AVAILABLE = False
        logger.info(
            'libplacebo filter: %s',
            'available (GPU tonemap enabled for DV/HDR10+)' if _LIBPLACEBO_AVAILABLE
            else 'unavailable (falling back to CPU zscale for DV/HDR10+)',
        )
    return _LIBPLACEBO_AVAILABLE


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
        logger.warning('No known encoder candidates for codec %r', codec)
        return None

    preferred_encoder = str(profile.get('preferred_video_encoder', 'auto')).lower()
    if preferred_encoder and preferred_encoder != 'auto':
        if preferred_encoder in candidates:
            logger.debug('Using explicitly preferred encoder %r for codec %r', preferred_encoder, codec)
            return EncoderSelection(
                codec=codec,
                encoder=preferred_encoder,
                use_qsv=preferred_encoder.endswith('_qsv'),
                use_vaapi=preferred_encoder.endswith('_vaapi'),
                is_explicit_preference=True,
            )
        logger.warning('Preferred encoder %r is not a valid candidate for codec %r; falling back to auto', preferred_encoder, codec)

    for candidate in candidates:
        if _encoder_available(candidate):
            logger.debug('Auto-selected encoder %r for codec %r', candidate, codec)
            return EncoderSelection(
                codec=codec,
                encoder=candidate,
                use_qsv=candidate.endswith('_qsv'),
                use_vaapi=candidate.endswith('_vaapi'),
                hw_decode=candidate.endswith('_vaapi'),
            )

    logger.warning('No available encoder found for codec %r (candidates checked: %s)', codec, candidates)
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

    if codec_impl in {'h264_qsv', 'hevc_qsv', 'av1_qsv', 'h264_vaapi', 'hevc_vaapi', 'av1_vaapi'}:
        # Neither QSV nor VAAPI support -crf; derive an equivalent VBR bitrate target.
        derived = _derive_qsv_bitrate_from_crf(crf, target_height)
        return ['-b:v', f'{derived}M', '-maxrate', f'{derived + 2}M', '-bufsize', f'{derived * 2}M']

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
    elif selection.encoder in {'h264_vaapi', 'hevc_vaapi', 'av1_vaapi'}:
        # VAAPI quality is governed by bitrate/QP, not a preset flag.
        video_preset_args = []
    elif selection.encoder == 'libsvtav1':
        video_preset_args = ['-preset', _svt_av1_preset(speed_preset)]
    else:
        video_preset_args = ['-preset', _software_preset(speed_preset)]

    hw_device = str(profile.get('qsv_device') or '/dev/dri/renderD128').strip()
    apply_tonemap = bool(profile.get('source_is_hdr')) and bool(profile.get('tone_map_hdr'))

    if selection.use_vaapi and selection.hw_decode and not apply_tonemap:
        # Full GPU pipeline (SDR): VAAPI hardware decode keeps frames in GPU memory.
        # Eliminates the costly CPU decode + format=nv12 + hwupload step.
        command = [
            'ffmpeg',
            '-hwaccel', 'vaapi',
            '-hwaccel_device', hw_device,
            '-hwaccel_output_format', 'vaapi',
            '-i', input_path,
        ]
    elif selection.use_vaapi and selection.hw_decode and apply_tonemap:
        # Full GPU pipeline (HDR): hardware decode so tonemap_vaapi (Intel VEBOX) can
        # operate directly on VAAPI surfaces without a CPU round-trip.
        command = [
            'ffmpeg',
            '-hwaccel', 'vaapi',
            '-hwaccel_device', hw_device,
            '-hwaccel_output_format', 'vaapi',
            '-i', input_path,
        ]
    elif selection.use_vaapi:
        # Software decode fallback — used when hw_decode is off (e.g. after hw tonemap
        # failed and the job was retried with hw_decode=False).
        command = ['ffmpeg', '-vaapi_device', hw_device, '-i', input_path]
    elif selection.use_qsv:
        # All QSV paths (HDR vpp_qsv, HDR CPU-tonemap fallback, SDR) use software
        # decode via the VAAPI→QSV bridge.  The VAAPI device is needed for QSV
        # device enumeration; actual decoding happens on the CPU so we never hit
        # VEBOX or tonemap_vaapi here.
        command = [
            'ffmpeg',
            '-init_hw_device', f'vaapi=va:{hw_device}',
            '-init_hw_device', 'qsv=qs@va',
            '-filter_hw_device', 'qs',
            '-i', input_path,
        ]
    else:
        command = ['ffmpeg', '-i', input_path]

    if selection.use_vaapi and selection.hw_decode and not apply_tonemap:
        # Full GPU pipeline (SDR): frames already in VAAPI surfaces — only scale if needed.
        filters = [f'scale_vaapi=-2:{target_height}'] if should_scale else []
        if filters:
            command.extend(['-vf', ','.join(filters)])
    elif selection.use_vaapi and selection.hw_decode and apply_tonemap:
        hdr_format = profile.get('source_hdr_format')
        if hdr_format in ('dolby_vision', 'hdr10plus'):
            # DV/HDR10+ carry dynamic/complex metadata that tonemap_vaapi (VEBOX)
            # handles unreliably.  Keep the fast VAAPI HW decoder, then hwdownload
            # to CPU.  Prefer libplacebo (Vulkan GPU tonemap — fast) when the FFmpeg
            # build includes it; fall back to the CPU zscale chain otherwise.
            if _libplacebo_available():
                filters = ['hwdownload,format=p010le', _LIBPLACEBO_TONEMAP_FILTER, 'hwupload']
            else:
                filters = ['hwdownload,format=p010le'] + list(_HDR_TONEMAP_FILTERS) + ['format=nv12', 'hwupload']
        else:
            # HDR10/HLG: static metadata only — VEBOX (tonemap_vaapi) handles it well.
            # Full GPU pipeline: hardware decode → tonemap_vaapi → VAAPI encode.
            filters = [_VAAPI_TONEMAP_FILTER]
        if should_scale:
            filters.append(f'scale_vaapi=-2:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_vaapi:
        # Software decode fallback: CPU tone mapping → NV12 → hwupload → VAAPI encode.
        filters = []
        if apply_tonemap:
            filters.extend(_HDR_TONEMAP_FILTERS)
        filters.extend(['format=nv12', 'hwupload'])
        if should_scale:
            filters.append(f'scale_vaapi=-2:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_qsv and apply_tonemap and not selection.force_sw_tonemap:
        # QSV GPU tone mapping via Intel VPP (vpp_qsv=tonemap=1).
        # SW decode preserves 10-bit HDR pixel data; hwupload moves frames to QSV
        # surfaces in their native bit depth. vpp_qsv reads the stream's HDR
        # metadata and performs tone mapping entirely within the QSV pipeline —
        # no VEBOX (tonemap_vaapi) involved, so DV/HDR10+ content is handled.
        filters = ['hwupload=extra_hw_frames=64', 'vpp_qsv=tonemap=1']
        if should_scale:
            filters.append(f'scale_qsv=-1:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_qsv and apply_tonemap:
        # QSV encode with CPU tone mapping — used when vpp_qsv=tonemap=1 failed
        # (e.g. driver too old / pre-11th-gen Intel).
        # SW decode → CPU zscale/tonemap → NV12 → hwupload → QSV encode.
        filters = list(_HDR_TONEMAP_FILTERS)
        filters.extend(['format=nv12', 'hwupload=extra_hw_frames=64'])
        if should_scale:
            filters.append(f'scale_qsv=-1:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_qsv:
        # SDR path: software decode → format=nv12 → hwupload → scale_qsv → QSV encode.
        filters = ['format=nv12', 'hwupload=extra_hw_frames=64']
        if should_scale:
            filters.append(f'scale_qsv=-1:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    else:
        filters = []
        if apply_tonemap:
            filters.extend(_HDR_TONEMAP_FILTERS)
        if should_scale:
            filters.append(f'scale=-2:{target_height}')
        if filters:
            command.extend(['-vf', ','.join(filters)])

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
    job_tag = f'job {job_id}' if job_id is not None else 'adhoc'
    logger.info('[%s] Running FFmpeg command: %s', job_tag, ' '.join(ffmpeg_command))
    try:
        process = subprocess.Popen(
            ffmpeg_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        logger.error('[%s] Failed to launch FFmpeg: %s', job_tag, exc)
        return None, 0.0, None, False, []

    if job_id is not None:
        with _ACTIVE_FFMPEG_LOCK:
            _ACTIVE_FFMPEG_PROCESSES[job_id] = process
            _ACTIVE_FFMPEG_POSITIONS[job_id] = 0.0

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
                if job_id is not None:
                    with _ACTIVE_FFMPEG_LOCK:
                        _ACTIVE_FFMPEG_POSITIONS[job_id] = processed_seconds
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
            _ACTIVE_FFMPEG_POSITIONS.pop(job_id, None)

    rc = process.returncode
    if rc != 0 and not was_cancelled:
        logger.error('[%s] FFmpeg exited with return code %s', job_tag, rc)
        # Log all lines that look like errors first, then a tail of the full output
        error_lines = [l for l in output_lines if _FFMPEG_ERROR_PATTERN.search(l)]
        if error_lines:
            logger.error('[%s] FFmpeg error lines:\n%s', job_tag, '\n'.join(error_lines))
        tail = output_lines[-40:] if len(output_lines) > 40 else output_lines
        logger.error('[%s] FFmpeg output (last %d lines):\n%s', job_tag, len(tail), '\n'.join(tail))
    elif rc == 0:
        logger.info('[%s] FFmpeg completed successfully (processed %.1fs)', job_tag, processed_seconds)

    return rc, processed_seconds, current_fps, was_cancelled, output_lines


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


def _run_ffprobe_stream_json(input_path: str) -> dict[str, Any] | None:
    command = [
        'ffprobe',
        '-v',
        'error',
        '-select_streams',
        'v:0',
        '-show_entries',
        'stream=color_transfer,color_primaries,color_space,color_range,pix_fmt,bits_per_raw_sample,profile,side_data_list',
        '-of',
        'json',
        input_path,
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError:
        return None

    if result.returncode != 0:
        return None

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def is_hdr_video(input_path: str) -> bool:
    # Plex surfaces HDR flags from stream metadata (transfer/primaries/colorspace and Dolby Vision side-data).
    hdr_transfer_markers = {'smpte2084', 'arib-std-b67'}
    hdr_space_markers = {'bt2020nc', 'bt2020c', 'bt2020ncl', 'bt2020cl'}
    hdr_primaries_markers = {'bt2020'}
    dolby_vision_markers = {'dovi configuration record', 'dolby vision rpu metadata', 'dolby vision'}

    payload = _run_ffprobe_stream_json(input_path)
    if payload:
        streams = payload.get('streams', [])
        if isinstance(streams, list) and streams:
            stream = streams[0] if isinstance(streams[0], dict) else {}
            transfer = str(stream.get('color_transfer', '')).strip().lower()
            primaries = str(stream.get('color_primaries', '')).strip().lower()
            color_space = str(stream.get('color_space', '')).strip().lower()
            profile = str(stream.get('profile', '')).strip().lower()
            pixel_format = str(stream.get('pix_fmt', '')).strip().lower()
            bit_depth_raw = str(stream.get('bits_per_raw_sample', '0') or '0').strip() or '0'
            try:
                bit_depth = int(bit_depth_raw)
            except ValueError:
                bit_depth = 0

            if transfer in hdr_transfer_markers:
                return True
            if primaries in hdr_primaries_markers and (transfer or color_space in hdr_space_markers or bit_depth >= 10):
                return True
            if color_space in hdr_space_markers and (transfer in hdr_transfer_markers or bit_depth >= 10):
                return True
            if 'dvhe' in profile or 'dvh1' in profile:
                return True
            if 'yuv420p10' in pixel_format and primaries in hdr_primaries_markers and transfer in hdr_transfer_markers:
                return True

            side_data_list = stream.get('side_data_list', [])
            if isinstance(side_data_list, list):
                for side_data in side_data_list:
                    if not isinstance(side_data, dict):
                        continue
                    side_data_type = str(side_data.get('side_data_type', '')).lower()
                    if side_data_type in {'mastering display metadata', 'content light level metadata'}:
                        return True
                    if side_data_type in dolby_vision_markers:
                        return True

    transfer = _run_ffprobe_value(input_path, 'stream=color_transfer')
    primaries = _run_ffprobe_value(input_path, 'stream=color_primaries')
    color_space = _run_ffprobe_value(input_path, 'stream=color_space')

    normalized_transfer = (transfer or '').strip().lower()
    normalized_primaries = (primaries or '').strip().lower()
    normalized_color_space = (color_space or '').strip().lower()

    return (
        normalized_transfer in hdr_transfer_markers
        or normalized_primaries in hdr_primaries_markers
        or normalized_color_space in hdr_space_markers
    )


def _detect_hdr_format(input_path: str) -> str | None:
    """Return the specific HDR format of the primary video stream.

    Returns one of: 'dolby_vision', 'hdr10plus', 'hdr10', 'hlg', or None (SDR).

    Detection priority (highest first):
      1. Dolby Vision — dvhe/dvh1 codec profile, or DOVI side-data record.
      2. HDR10+       — SMPTE 2094-40 dynamic metadata side-data.
      3. HLG          — arib-std-b67 transfer function.
      4. HDR10        — smpte2084 (PQ) transfer function without DV/HDR10+.
    """
    _dv_markers = {'dovi configuration record', 'dolby vision rpu metadata', 'dolby vision'}

    payload = _run_ffprobe_stream_json(input_path)
    if payload:
        streams = payload.get('streams', [])
        if isinstance(streams, list) and streams:
            stream = streams[0] if isinstance(streams[0], dict) else {}
            transfer = str(stream.get('color_transfer', '')).strip().lower()
            profile = str(stream.get('profile', '')).strip().lower()

            # 1. Dolby Vision — codec profile is the fastest indicator.
            if 'dvhe' in profile or 'dvh1' in profile:
                return 'dolby_vision'

            has_dv = False
            has_hdr10plus = False
            side_data_list = stream.get('side_data_list', [])
            if isinstance(side_data_list, list):
                for side_data in side_data_list:
                    if not isinstance(side_data, dict):
                        continue
                    sdt = str(side_data.get('side_data_type', '')).lower()
                    if sdt in _dv_markers:
                        has_dv = True
                    # HDR10+ uses SMPTE ST 2094-40; ffprobe reports the full
                    # string which may vary slightly across versions.
                    if 'smpte2094-40' in sdt or 'hdr10plus' in sdt or 'hdr10+' in sdt:
                        has_hdr10plus = True

            if has_dv:
                return 'dolby_vision'
            if has_hdr10plus:
                return 'hdr10plus'

            # 3. HLG
            if transfer == 'arib-std-b67':
                return 'hlg'
            # 4. HDR10 (PQ without DV/HDR10+ side data)
            if transfer == 'smpte2084':
                return 'hdr10'

    # Scalar fallback for minimal ffprobe output.
    transfer = (_run_ffprobe_value(input_path, 'stream=color_transfer') or '').strip().lower()
    if transfer == 'arib-std-b67':
        return 'hlg'
    if transfer == 'smpte2084':
        return 'hdr10'

    return None


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
        'qsv_device': os.getenv('QSV_DEVICE', '/dev/dri/renderD128'),
    }


def _build_resume_command(
    input_path: str,
    resume_position_seconds: float,
    output_path: str,
    profile: dict[str, Any],
    selection: 'EncoderSelection',
) -> list[str]:
    """Build an FFmpeg command that seeks into the input and encodes the remaining portion."""
    seek_args = ['-ss', str(resume_position_seconds)]

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
    elif selection.encoder in {'h264_vaapi', 'hevc_vaapi', 'av1_vaapi'}:
        video_preset_args = []
    elif selection.encoder == 'libsvtav1':
        video_preset_args = ['-preset', _svt_av1_preset(speed_preset)]
    else:
        video_preset_args = ['-preset', _software_preset(speed_preset)]

    hw_device = str(profile.get('qsv_device') or '/dev/dri/renderD128').strip()
    apply_tonemap = bool(profile.get('source_is_hdr')) and bool(profile.get('tone_map_hdr'))

    if selection.use_vaapi and selection.hw_decode:
        # Both SDR and HDR hardware paths use VAAPI hardware decode.
        # tonemap_vaapi (VEBOX) and scale_vaapi operate on VAAPI surfaces produced here.
        command = [
            'ffmpeg',
            '-hwaccel', 'vaapi',
            '-hwaccel_device', hw_device,
            '-hwaccel_output_format', 'vaapi',
        ] + seek_args + ['-i', input_path]
    elif selection.use_vaapi:
        command = ['ffmpeg', '-vaapi_device', hw_device] + seek_args + ['-i', input_path]
    elif selection.use_qsv:
        # All QSV paths use SW decode via the VAAPI→QSV bridge (same as SDR).
        # vpp_qsv handles GPU tone mapping without VAAPI hw decode.
        command = [
            'ffmpeg',
            '-init_hw_device', f'vaapi=va:{hw_device}',
            '-init_hw_device', 'qsv=qs@va',
            '-filter_hw_device', 'qs',
        ] + seek_args + ['-i', input_path]
    else:
        command = ['ffmpeg'] + seek_args + ['-i', input_path]

    if selection.use_vaapi and selection.hw_decode and not apply_tonemap:
        filters = [f'scale_vaapi=-2:{target_height}'] if should_scale else []
        if filters:
            command.extend(['-vf', ','.join(filters)])
    elif selection.use_vaapi and selection.hw_decode and apply_tonemap:
        hdr_format = profile.get('source_hdr_format')
        if hdr_format in ('dolby_vision', 'hdr10plus'):
            # DV/HDR10+: keep VAAPI HW decode, download to CPU, then GPU tonemap via
            # libplacebo (Vulkan) if available; CPU zscale otherwise.
            if _libplacebo_available():
                filters = ['hwdownload,format=p010le', _LIBPLACEBO_TONEMAP_FILTER, 'hwupload']
            else:
                filters = ['hwdownload,format=p010le'] + list(_HDR_TONEMAP_FILTERS) + ['format=nv12', 'hwupload']
        else:
            # HDR10/HLG: static metadata — VEBOX handles it reliably.
            filters = [_VAAPI_TONEMAP_FILTER]
        if should_scale:
            filters.append(f'scale_vaapi=-2:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_vaapi:
        filters = []
        if apply_tonemap:
            filters.extend(_HDR_TONEMAP_FILTERS)
        filters.extend(['format=nv12', 'hwupload'])
        if should_scale:
            filters.append(f'scale_vaapi=-2:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_qsv and apply_tonemap and not selection.force_sw_tonemap:
        # QSV GPU tone mapping via Intel VPP.  SW decode keeps native 10-bit HDR
        # frames; hwupload pushes them into QSV surfaces; vpp_qsv=tonemap=1 converts
        # to SDR fully on-GPU without involving VEBOX / tonemap_vaapi.
        filters = ['hwupload=extra_hw_frames=64', 'vpp_qsv=tonemap=1']
        if should_scale:
            filters.append(f'scale_qsv=-1:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_qsv and apply_tonemap:
        # CPU tone-map fallback (vpp_qsv unsupported on this host).
        filters = list(_HDR_TONEMAP_FILTERS)
        filters.extend(['format=nv12', 'hwupload=extra_hw_frames=64'])
        if should_scale:
            filters.append(f'scale_qsv=-1:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    elif selection.use_qsv:
        # SDR path: software decode → format=nv12 → hwupload → scale_qsv → QSV encode.
        filters = ['format=nv12', 'hwupload=extra_hw_frames=64']
        if should_scale:
            filters.append(f'scale_qsv=-1:{target_height}')
        command.extend(['-vf', ','.join(filters)])
    else:
        filters = []
        if apply_tonemap:
            filters.extend(_HDR_TONEMAP_FILTERS)
        if should_scale:
            filters.append(f'scale=-2:{target_height}')
        if filters:
            command.extend(['-vf', ','.join(filters)])

    command.extend(['-c:v', selection.encoder])
    command.extend(video_preset_args)
    command.extend(_video_rate_args(selection.encoder, bitrate_mode, bitrate_mbps, crf, target_height))
    command.extend(_audio_args(audio_mode))
    command.extend(['-progress', 'pipe:1', '-nostats', output_path])
    return command


def _concat_partial_and_resume(
    workspace_path: Path,
    partial_path: Path,
    resume_path: Path,
    combined_path: Path,
    job_id: int | None,
) -> bool:
    """Concatenate partial and resume segments into a single output file using stream copy."""
    concat_list_path = workspace_path / 'concat_list.txt'
    try:
        concat_list_path.write_text(
            f"file '{partial_path}'\nfile '{resume_path}'\n"
        )
    except OSError as exc:
        logger.error('[job %s] Failed to write concat list: %s', job_id, exc)
        return False

    concat_cmd = [
        'ffmpeg', '-y',
        '-f', 'concat',
        '-safe', '0',
        '-i', str(concat_list_path),
        '-c', 'copy',
        str(combined_path),
    ]
    job_tag = f'job {job_id}' if job_id is not None else 'adhoc'
    logger.info('[%s] Concatenating partial and resume segments', job_tag)
    try:
        result = subprocess.run(concat_cmd, capture_output=True, text=True, check=False)
    except OSError as exc:
        logger.error('[%s] Failed to run FFmpeg concat: %s', job_tag, exc)
        return False

    if result.returncode != 0:
        logger.error('[%s] FFmpeg concat failed (rc=%s):\n%s', job_tag, result.returncode, result.stderr[-2000:])
        return False

    logger.info('[%s] Concat succeeded: %r', job_tag, str(combined_path))
    return True


def optimize_video(
    input_path: str,
    settings,
    job_id: int | None = None,
    progress_callback: Callable[[dict[str, float | int | None]], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    encoder_selected_callback: Callable[[str, bool], None] | None = None,
    resume_position_seconds: float | None = None,
) -> OptimizationMetrics:
    job_tag = f'job {job_id}' if job_id is not None else 'adhoc'
    profile = _profile_from_settings(settings)
    workspace_path = _job_workspace_path(settings, job_id)
    final_output_path, partial_output_path = _output_paths(input_path, profile, workspace_path)
    metrics = OptimizationMetrics(input_path=input_path, output_path=str(final_output_path), status='pending')

    logger.info(
        '[%s] Starting optimization: file=%r codec=%r encoder_pref=%r bitrate_mode=%r',
        job_tag, input_path,
        profile.get('codec', 'h264'),
        profile.get('preferred_video_encoder', 'auto'),
        profile.get('bitrate_mode', 'cbr'),
    )

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

    hdr_only = bool(profile.get('hdr_only'))
    if not hdr_only:
        minimum_source_resolution = int(profile.get('minimum_source_resolution') or 0)
        target_resolution = int(profile.get('target_resolution') or 1080)
        if minimum_source_resolution and height < minimum_source_resolution:
            metrics.status = 'skipped'
            metrics.skipped_reason = 'source_height_below_threshold'
            return metrics
        if height <= target_resolution:
            metrics.status = 'skipped'
            metrics.skipped_reason = 'source_height_below_target'
            return metrics

    # Check whether we can resume from a previously saved partial output.
    # A resume is valid when a resume position is given AND the partial file still exists.
    existing_partial = next(workspace_path.glob('output.partial.*'), None) if workspace_path.exists() else None
    can_resume = (
        resume_position_seconds is not None
        and resume_position_seconds > 0
        and existing_partial is not None
        and existing_partial.exists()
    )

    if can_resume:
        logger.info(
            '[%s] Resuming from %.1fs — partial file exists at %r',
            job_tag, resume_position_seconds, str(existing_partial),
        )
        # Workspace already has the partial; no clean needed.
    else:
        if resume_position_seconds is not None:
            logger.info(
                '[%s] Resume position %.1fs requested but no partial output found; encoding from scratch',
                job_tag, resume_position_seconds,
            )
            resume_position_seconds = None
        try:
            _ensure_clean_workspace(workspace_path)
        except OSError:
            metrics.status = 'failed'
            metrics.skipped_reason = 'workspace_prepare_failed'
            metrics.error_message = 'workspace_prepare_failed'
            return metrics

    duration = _probe_duration_seconds(input_path)
    metrics.duration_seconds = duration

    if 'source_is_hdr' not in profile:
        profile['source_is_hdr'] = is_hdr_video(input_path)
    if 'source_hdr_format' not in profile:
        profile['source_hdr_format'] = (
            _detect_hdr_format(input_path) if profile['source_is_hdr'] else None
        )
    if profile['source_is_hdr'] and bool(profile.get('tone_map_hdr')):
        logger.info(
            '[%s] HDR source detected (%s) — tone mapping to SDR will be applied',
            job_tag, profile['source_hdr_format'] or 'hdr',
        )
        if profile['source_hdr_format'] in ('dolby_vision', 'hdr10plus'):
            _tonemap_method = (
                'libplacebo (Vulkan/GPU)' if _libplacebo_available()
                else 'CPU zscale (libplacebo unavailable)'
            )
            logger.info(
                '[%s] %s detected — using VAAPI hw-decode + %s for tone mapping '
                '(tonemap_vaapi/VEBOX unreliable for DV/HDR10+; skipping that path entirely)',
                job_tag, profile['source_hdr_format'], _tonemap_method,
            )
    elif profile['source_is_hdr']:
        logger.info(
            '[%s] HDR source detected (%s) — preserving HDR (tone mapping disabled)',
            job_tag, profile['source_hdr_format'] or 'hdr',
        )

    selection = _select_encoder(profile)

    if not selection and str(profile.get('codec', 'h264')).lower() == 'av1':
        logger.error('[%s] AV1 encoding requested but no AV1 encoder is available on this host', job_tag)
        metrics.status = 'failed'
        metrics.skipped_reason = 'optimization_failed'
        metrics.error_message = 'AV1 not supported on this host.'
        return metrics
    if not selection:
        profile_codec = str(profile.get('codec', 'h264')).lower()
        fallback_encoder = 'libx264' if profile_codec == 'h264' else 'libx265'
        logger.warning(
            '[%s] No preferred encoder available for codec %r; falling back to software encoder %r',
            job_tag, profile_codec, fallback_encoder,
        )
        selection = EncoderSelection(codec=profile_codec, encoder=fallback_encoder, use_qsv=False)
        metrics.used_fallback = True
        metrics.fallback_reason = f'no_{profile_codec}_hw_encoder'
    else:
        logger.info('[%s] Encoder selected: %r (codec=%r, hwaccel=%s)', job_tag, selection.encoder, selection.codec, 'qsv' if selection.use_qsv else 'vaapi' if selection.use_vaapi else 'none')

    if encoder_selected_callback:
        encoder_selected_callback(selection.encoder, selection.use_qsv or selection.use_vaapi)

    if can_resume:
        # Encode only the remaining portion into a separate segment file.
        container = _container_from_profile(profile)
        resume_segment_path = workspace_path / f'output.resume.{container}'
        ffmpeg_command = _build_resume_command(
            input_path, resume_position_seconds, str(resume_segment_path), profile, selection,
        )
        # Wrap the progress callback to offset progress by the already-encoded portion.
        resume_progress_callback = progress_callback
        if progress_callback and duration and duration > 0:
            resume_start_pct = int((resume_position_seconds / duration) * 100)
            remaining_pct_span = 100 - resume_start_pct
            _orig_cb = progress_callback
            def resume_progress_callback(update: dict, _start=resume_start_pct, _span=remaining_pct_span, _cb=_orig_cb) -> None:  # type: ignore[misc]
                pct = int(update.get('progress_percent') or 0)
                update['progress_percent'] = min(99, _start + int(pct * _span / 100))
                _cb(update)
        remaining_duration = (duration - resume_position_seconds) if duration else None
        return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
            job_id,
            ffmpeg_command,
            remaining_duration,
            resume_progress_callback,
            should_cancel,
        )
    else:
        ffmpeg_command = _build_command_with_selection(input_path, str(partial_output_path), profile, selection)
        return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
            job_id,
            ffmpeg_command,
            duration,
            progress_callback,
            should_cancel,
        )

    # If the QSV+HDR vpp_qsv path failed (driver too old, unsupported content, etc.)
    # retry with the same QSV encoder but use CPU (zscale) tone mapping instead.
    # This is slower than vpp_qsv but still keeps QSV encoding; it is better than
    # dropping to VAAPI entirely when QSV encoding itself is healthy.
    # Skip this slow CPU path when a VAAPI encoder is available — VAAPI uses GPU
    # tone mapping (tonemap_vaapi) and will be faster; let the VAAPI fallback below
    # handle it instead.
    _apply_tonemap = bool(profile.get('source_is_hdr')) and bool(profile.get('tone_map_hdr'))
    _vaapi_fallback_available = bool(_QSV_VAAPI_FALLBACK.get(selection.encoder) and _encoder_available(_QSV_VAAPI_FALLBACK.get(selection.encoder, '')))
    if (
        return_code is not None and return_code != 0 and not was_cancelled
        and selection.use_qsv and _apply_tonemap and not selection.force_sw_tonemap
        and not _vaapi_fallback_available
    ):
        sw_tonemap_selection = EncoderSelection(
            codec=selection.codec,
            encoder=selection.encoder,
            use_qsv=True,
            use_vaapi=False,
            force_sw_tonemap=True,
            is_explicit_preference=selection.is_explicit_preference,
        )
        logger.warning(
            '[%s] QSV vpp_qsv tonemap failed (rc=%s); retrying with CPU tone mapping + %r encode',
            job_tag, return_code, selection.encoder,
        )
        if not can_resume:
            try:
                _ensure_clean_workspace(workspace_path)
            except OSError:
                pass
        if can_resume:
            ffmpeg_command = _build_resume_command(
                input_path, resume_position_seconds, str(resume_segment_path), profile, sw_tonemap_selection,
            )
            return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                job_id, ffmpeg_command, remaining_duration, resume_progress_callback, should_cancel,
            )
        else:
            ffmpeg_command = _build_command_with_selection(
                input_path, str(partial_output_path), profile, sw_tonemap_selection,
            )
            return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                job_id, ffmpeg_command, duration, progress_callback, should_cancel,
            )
        selection = sw_tonemap_selection
        metrics.used_fallback = True
        metrics.fallback_reason = 'qsv_vpp_tonemap_failed_sw_tonemap'

    if return_code is not None and return_code != 0 and not was_cancelled and selection.use_qsv:
        vaapi_encoder = _QSV_VAAPI_FALLBACK.get(selection.encoder)
        if vaapi_encoder and _encoder_available(vaapi_encoder):
            logger.warning(
                '[%s] QSV encoder %r failed (rc=%s); retrying with VAAPI encoder %r '
                '(same iGPU, no VPL runtime required)',
                job_tag, selection.encoder, return_code, vaapi_encoder,
            )
            if not can_resume:
                try:
                    _ensure_clean_workspace(workspace_path)
                except OSError:
                    pass
            vaapi_selection = EncoderSelection(
                codec=selection.codec, encoder=vaapi_encoder, use_qsv=False, use_vaapi=True,
                hw_decode=True,  # mirrors _select_encoder: all VAAPI encoders hw-decode via iHD
            )
            if encoder_selected_callback:
                encoder_selected_callback(vaapi_encoder, True)
            if can_resume:
                ffmpeg_command = _build_resume_command(
                    input_path, resume_position_seconds, str(resume_segment_path), profile, vaapi_selection,
                )
                return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                    job_id,
                    ffmpeg_command,
                    remaining_duration,
                    resume_progress_callback,
                    should_cancel,
                )
            else:
                ffmpeg_command = _build_command_with_selection(
                    input_path, str(partial_output_path), profile, vaapi_selection,
                )
                return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                    job_id,
                    ffmpeg_command,
                    duration,
                    progress_callback,
                    should_cancel,
                )
            selection = vaapi_selection
            metrics.used_fallback = True
            metrics.fallback_reason = 'qsv_failed_vaapi_fallback'

    # If VAAPI hw-decode + tonemap_vaapi failed (unusual DV/HDR10+ bitstream, driver
    # limitation, etc.), try the QSV equivalent first.  vpp_qsv=tonemap=1 (Intel VPP)
    # handles DV/HDR10+ dynamic metadata more robustly than VEBOX on the same iGPU,
    # avoiding a fall-back to the slow CPU zscale chain wherever possible.
    _vaapi_tonemap_failed = False
    if (
        return_code is not None and return_code != 0 and not was_cancelled
        and selection.use_vaapi and selection.hw_decode and _apply_tonemap
    ):
        _vaapi_tonemap_failed = True
        qsv_tonemap_encoder = _VAAPI_QSV_FALLBACK.get(selection.encoder)
        if qsv_tonemap_encoder and _encoder_available(qsv_tonemap_encoder):
            logger.warning(
                '[%s] VAAPI tonemap_vaapi failed (rc=%s); retrying with %r + vpp_qsv=tonemap=1 '
                '(Intel VPP — handles DV/HDR10+ dynamic metadata on the same iGPU)',
                job_tag, return_code, qsv_tonemap_encoder,
            )
            if not can_resume:
                try:
                    _ensure_clean_workspace(workspace_path)
                except OSError:
                    pass
            qsv_tonemap_selection = EncoderSelection(
                codec=selection.codec, encoder=qsv_tonemap_encoder, use_qsv=True, use_vaapi=False,
            )
            if encoder_selected_callback:
                encoder_selected_callback(qsv_tonemap_encoder, True)
            if can_resume:
                ffmpeg_command = _build_resume_command(
                    input_path, resume_position_seconds, str(resume_segment_path), profile, qsv_tonemap_selection,
                )
                return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                    job_id, ffmpeg_command, remaining_duration, resume_progress_callback, should_cancel,
                )
            else:
                ffmpeg_command = _build_command_with_selection(
                    input_path, str(partial_output_path), profile, qsv_tonemap_selection,
                )
                return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                    job_id, ffmpeg_command, duration, progress_callback, should_cancel,
                )
            selection = qsv_tonemap_selection
            metrics.used_fallback = True
            metrics.fallback_reason = 'vaapi_tonemap_failed_qsv_vpp_fallback'

    # If VAAPI hardware decode failed, retry with software decode + hwupload.
    # Triggers on QSV-pattern hw errors (non-tonemap) or when tonemap_vaapi failed
    # and either QSV was unavailable or the QSV intermediate fallback also failed.
    # When the intermediate fallback switched selection to QSV, look up the original
    # VAAPI encoder via _QSV_VAAPI_FALLBACK for the sw-decode retry.
    _needs_sw_decode_retry = (
        return_code is not None and return_code != 0 and not was_cancelled
        and (
            (selection.use_vaapi and selection.hw_decode and _has_qsv_error(output_lines))
            or _vaapi_tonemap_failed
        )
    )
    if _needs_sw_decode_retry:
        _vaapi_sw_encoder = (
            selection.encoder if selection.use_vaapi
            else _QSV_VAAPI_FALLBACK.get(selection.encoder)
        )
        if _vaapi_sw_encoder:
            sw_decode_selection = EncoderSelection(
                codec=selection.codec, encoder=_vaapi_sw_encoder, use_qsv=False, use_vaapi=True, hw_decode=False,
            )
            _sw_reason = (
                'VAAPI tone mapping failed' if _vaapi_tonemap_failed
                else 'VAAPI hardware decode failed'
            )
            logger.warning(
                '[%s] %s (rc=%s); retrying with software decode + hwupload',
                job_tag, _sw_reason, return_code,
            )
            if not can_resume:
                try:
                    _ensure_clean_workspace(workspace_path)
                except OSError:
                    pass
            if can_resume:
                ffmpeg_command = _build_resume_command(
                    input_path, resume_position_seconds, str(resume_segment_path), profile, sw_decode_selection,
                )
                return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                    job_id, ffmpeg_command, remaining_duration, resume_progress_callback, should_cancel,
                )
            else:
                ffmpeg_command = _build_command_with_selection(
                    input_path, str(partial_output_path), profile, sw_decode_selection,
                )
                return_code, processed_seconds, current_fps, was_cancelled, output_lines = _run_ffmpeg(
                    job_id, ffmpeg_command, duration, progress_callback, should_cancel,
                )
            selection = sw_decode_selection
            metrics.used_fallback = True
            metrics.fallback_reason = (
                'vaapi_tonemap_failed_swdecode_fallback' if _vaapi_tonemap_failed
                else 'vaapi_hwdecode_failed_swdecode_fallback'
            )

    if return_code is None:
        logger.error('[%s] FFmpeg could not be launched (binary missing or not executable)', job_tag)
        metrics.status = 'failed'
        metrics.skipped_reason = 'ffmpeg_unavailable'
        metrics.error_message = 'ffmpeg_unavailable'
        return metrics

    metrics.processed_seconds = processed_seconds
    metrics.fps = current_fps
    metrics.return_code = return_code
    metrics.encoder_used = selection.encoder
    metrics.codec_used = selection.codec
    metrics.hwaccel_used = selection.use_qsv or selection.use_vaapi

    if was_cancelled:
        metrics.status = 'cancelled'
    elif return_code == 0:
        if can_resume:
            # Concatenate the saved partial and the newly encoded resume segment.
            container = _container_from_profile(profile)
            combined_path = workspace_path / f'output.combined.{container}'
            concat_ok = _concat_partial_and_resume(
                workspace_path, existing_partial, resume_segment_path, combined_path, job_id,
            )
            if not concat_ok:
                metrics.status = 'failed'
                metrics.skipped_reason = 'concat_failed'
                metrics.error_message = 'concat_failed'
            else:
                committed = _commit_output_file(combined_path, final_output_path, job_id)
                if committed:
                    logger.info('[%s] Resume encode complete; output committed to %r', job_tag, str(final_output_path))
                    metrics.status = 'complete'
                    shutil.rmtree(workspace_path, ignore_errors=True)
                else:
                    logger.error('[%s] Failed to commit concat output %r -> %r', job_tag, str(combined_path), str(final_output_path))
                    metrics.status = 'failed'
                    metrics.skipped_reason = 'commit_failed'
                    metrics.error_message = 'commit_failed'
        else:
            committed = _commit_output_file(partial_output_path, final_output_path, job_id)
            if committed:
                logger.info('[%s] Encoding complete; output committed to %r', job_tag, str(final_output_path))
                metrics.status = 'complete'
                shutil.rmtree(workspace_path, ignore_errors=True)
            else:
                logger.error('[%s] Failed to commit output file %r -> %r', job_tag, str(partial_output_path), str(final_output_path))
                metrics.status = 'failed'
                metrics.skipped_reason = 'commit_failed'
                metrics.error_message = 'commit_failed'
    else:
        metrics.status = 'failed'
        if selection.use_qsv and _has_qsv_error(output_lines):
            logger.error(
                '[%s] QSV encoding failed (encoder=%r, return_code=%s); '
                'check FFmpeg output above for hardware/driver details',
                job_tag, selection.encoder, return_code,
            )
            metrics.skipped_reason = 'qsv_encode_failed'
            metrics.error_message = 'qsv_encode_failed'
        elif selection.use_qsv:
            # FFmpeg failed with QSV encoder but no QSV-specific pattern was found —
            # could be a device, driver, or filter error. Log prominently.
            logger.error(
                '[%s] FFmpeg failed with QSV encoder %r (return_code=%s) but no QSV error pattern matched; '
                'check FFmpeg output above — possible device or driver issue',
                job_tag, selection.encoder, return_code,
            )
            metrics.skipped_reason = 'qsv_encode_failed'
            metrics.error_message = 'qsv_encode_failed'
        elif selection.codec == 'av1':
            logger.error('[%s] AV1 encoding failed (encoder=%r, return_code=%s)', job_tag, selection.encoder, return_code)
            metrics.skipped_reason = 'av1_encode_failed'
            metrics.error_message = 'av1_encode_failed'
        else:
            logger.error(
                '[%s] Encoding failed (encoder=%r, return_code=%s)',
                job_tag, selection.encoder, return_code,
            )
    if metrics.status == 'failed':
        if not metrics.skipped_reason:
            metrics.skipped_reason = 'optimization_failed'
        if not metrics.error_message:
            metrics.error_message = 'optimization_failed'
        logger.error(
            '[%s] Job marked as failed: reason=%r encoder=%r hwaccel=%s',
            job_tag, metrics.skipped_reason, metrics.encoder_used, metrics.hwaccel_used,
        )

    if progress_callback:
        progress_callback(
            {
                'progress_percent': 100 if metrics.status == 'complete' else min(99, int(metrics.processed_seconds)),
                'fps': current_fps,
                'eta_seconds': 0 if metrics.status == 'complete' else None,
            }
        )

    return metrics
