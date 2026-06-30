import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# VS Code pytest discovery imports conftest before running any tests.
# Default to writable local paths so local dev environments don't require /config.
_test_id = os.getenv('PYTEST_XDIST_WORKER') or str(os.getpid())
_temp_root = Path(tempfile.gettempdir()).resolve()
os.environ.setdefault('PLEX_OPTIMIZER_DB_PATH', str(_temp_root / f'optimizarr-test-{_test_id}.db'))
os.environ.setdefault('OPTIMIZARR_LOG_DIR', str(_temp_root / f'optimizarr-logs-{_test_id}'))
os.environ.setdefault('OPTIMIZARR_SECRETS_KEY_PATH', str(_temp_root / f'optimizarr-secrets-{_test_id}.key'))
os.environ.setdefault('OPTIMIZARR_BOOTSTRAP_TOKEN', 'test-bootstrap-token')
os.environ.setdefault('OPTIMIZARR_TEST_START_WORKER', '0')
os.environ.setdefault('OPTIMIZARR_WEBSOCKET_KEEPALIVE_SECONDS', '0.05')
os.environ.setdefault('MEDIA_ROOT', str(_temp_root))
os.environ.setdefault('OPTIMIZARR_WORKSPACE_ROOT_BASE', str(_temp_root))

Path(os.environ['MEDIA_ROOT']).mkdir(parents=True, exist_ok=True)
Path(os.environ['OPTIMIZARR_WORKSPACE_ROOT_BASE']).mkdir(parents=True, exist_ok=True)

from app.core.database import init_db


init_db()


@pytest.fixture(autouse=True)
def reset_service_runtime_state():
    from app.services import download_monitor_service
    from app.workers import queue

    queue.stop_worker()
    queue.stop_event.clear()
    with queue._pool_lock:
        queue._active_workers.clear()
    queue._manager_thread = None
    queue._last_workers_allowed = None
    queue._queue_paused = False

    download_monitor_service.stop_download_monitor()
    download_monitor_service._stop_event.clear()
    download_monitor_service._wake_event.clear()
    download_monitor_service._scan_recovery_event.clear()
    download_monitor_service._download_queue_stopped = False
    download_monitor_service._download_queue_stop_reason = ''
    download_monitor_service._tagged_job_ids.clear()
    download_monitor_service._categorized_sab_job_ids.clear()
    download_monitor_service._qbt_strike_state.clear()
    download_monitor_service._startup_grace_until = None

    yield

    queue.stop_worker()
    queue.stop_event.clear()
    with queue._pool_lock:
        queue._active_workers.clear()
    queue._manager_thread = None
    queue._last_workers_allowed = None
    queue._queue_paused = False

    download_monitor_service.stop_download_monitor()
    download_monitor_service._stop_event.clear()
    download_monitor_service._wake_event.clear()
    download_monitor_service._scan_recovery_event.clear()
    download_monitor_service._download_queue_stopped = False
    download_monitor_service._download_queue_stop_reason = ''
    download_monitor_service._tagged_job_ids.clear()
    download_monitor_service._categorized_sab_job_ids.clear()
    download_monitor_service._qbt_strike_state.clear()
    download_monitor_service._startup_grace_until = None
