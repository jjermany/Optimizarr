import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# VS Code pytest discovery imports conftest before running any tests.
# Default to writable local paths so local dev environments don't require /config.
_test_id = os.getenv('PYTEST_XDIST_WORKER') or str(os.getpid())
os.environ.setdefault('PLEX_OPTIMIZER_DB_PATH', f'/tmp/optimizarr-test-{_test_id}.db')
os.environ.setdefault('OPTIMIZARR_LOG_DIR', f'/tmp/optimizarr-logs-{_test_id}')
os.environ.setdefault('OPTIMIZARR_SECRETS_KEY_PATH', f'/tmp/optimizarr-secrets-{_test_id}.key')
os.environ.setdefault('OPTIMIZARR_BOOTSTRAP_TOKEN', 'test-bootstrap-token')
os.environ.setdefault('MEDIA_ROOT', '/tmp')
os.environ.setdefault('OPTIMIZARR_WORKSPACE_ROOT_BASE', '/tmp')

Path(os.environ['MEDIA_ROOT']).mkdir(parents=True, exist_ok=True)
Path(os.environ['OPTIMIZARR_WORKSPACE_ROOT_BASE']).mkdir(parents=True, exist_ok=True)

from app.core.database import init_db


init_db()
