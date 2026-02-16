from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
from typing import Callable


FPS_REGEX = re.compile(r"fps\s*=\s*(?P<fps>[0-9]*\.?[0-9]+)")


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
    return [
        'ffmpeg',
        '-hwaccel',
        'qsv',
        '-hwaccel_output_format',
        'qsv',
        '-i',
        input_path,
        '-vf',
        'scale_qsv=1920:1080',
        '-c:v',
        'h264_qsv',
        '-b:v',
        f'{bitrate_mbps}M',
        '-maxrate',
        f'{bitrate_mbps + 2}M',
        '-bufsize',
        '16M',
        '-c:a',
        'copy',
        '-progress',
        'pipe:1',
        '-nostats',
        output_path,
    ]


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

    bitrate_mbps = int(getattr(settings, 'bitrate_mbps', 8))
    ffmpeg_command = _build_ffmpeg_command(input_path, output_path, bitrate_mbps)

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
