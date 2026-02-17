from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import psutil
from sqlalchemy.orm import Session

from app.models.job import Job

TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}


def _safe_float(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def _extract_percent(stats: Any, preferred_labels: tuple[str, ...]) -> float:
    if isinstance(stats, dict):
        engine_values = []
        for key, value in stats.items():
            key_lower = str(key).lower()
            if isinstance(value, dict):
                if any(label in key_lower for label in preferred_labels):
                    busy_value = _safe_float(value.get('busy'))
                    if busy_value:
                        engine_values.append(busy_value)
                nested_value = _extract_percent(value, preferred_labels)
                if nested_value:
                    engine_values.append(nested_value)
            elif any(label in key_lower for label in preferred_labels):
                parsed_value = _safe_float(value)
                if parsed_value:
                    engine_values.append(parsed_value)
        if engine_values:
            return max(engine_values)

    if isinstance(stats, list):
        values = [_extract_percent(item, preferred_labels) for item in stats]
        values = [value for value in values if value]
        if values:
            return max(values)

    return 0.0


def _extract_last_json_blob(raw_output: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    cursor = 0
    last_json = {}

    while cursor < len(raw_output):
        try:
            obj, index = decoder.raw_decode(raw_output, cursor)
            if isinstance(obj, dict):
                last_json = obj
            cursor = index
        except json.JSONDecodeError:
            cursor += 1

    return last_json


def get_gpu_metrics() -> dict[str, float]:
    default_metrics = {'gpu_video_percent': 0.0, 'gpu_render_percent': 0.0}

    # intel_gpu_top -J streams JSON objects indefinitely and buffers stdout, so
    # subprocess.run(timeout=...) kills the process before the buffer is flushed —
    # leaving exc.stdout empty.  Using Popen with line-by-line reading means we
    # receive each chunk as it arrives without waiting for a buffer flush on kill.
    try:
        process = subprocess.Popen(
            ['intel_gpu_top', '-J', '-s', '250'],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except (FileNotFoundError, PermissionError):
        return default_metrics

    raw_stdout = ''
    deadline = time.monotonic() + 2.0
    try:
        assert process.stdout is not None
        while time.monotonic() < deadline:
            line = process.stdout.readline()
            if not line:
                break
            raw_stdout += line
    finally:
        try:
            process.kill()
            process.wait(timeout=1)
        except Exception:
            pass

    payload = _extract_last_json_blob(raw_stdout)
    if not payload:
        return default_metrics

    return {
        'gpu_video_percent': _extract_percent(payload, ('video',)),
        'gpu_render_percent': _extract_percent(payload, ('render', '3d')),
    }


def get_system_metrics(db: Session) -> dict[str, float | int]:
    gpu_metrics = get_gpu_metrics()
    active_jobs = db.query(Job).filter(~Job.status.in_(TERMINAL_STATUSES)).count()

    return {
        **gpu_metrics,
        'cpu_percent': psutil.cpu_percent(interval=0.1),
        'ram_percent': psutil.virtual_memory().percent,
        'active_jobs': active_jobs,
    }
