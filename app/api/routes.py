import os
import logging
from datetime import timezone
from pathlib import Path
from threading import Lock
import time
import secrets
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import SessionLocal, get_db
from app.core.security import (
    BOOTSTRAP_TOKEN_HEADER_NAME,
    get_bootstrap_token,
    is_safe_same_origin,
    media_root,
    normalize_http_host_without_port,
    normalize_http_origin_with_optional_port,
    normalize_path_within_root,
    normalize_smtp_host,
    normalize_workspace_root,
    workspace_root_base,
)
from app.models.auth import AdminUser
from app.models.library import (
    AudioModeEnum,
    BitrateModeEnum,
    CodecEnum,
    ContainerEnum,
    DownloadQualityProfileEnum,
    Library,
    LibraryProfile,
    SpeedPresetEnum,
    SchedulePolicyEnum,
    OutputConflictPolicyEnum,
    PreferredEncoderEnum,
)
from app.models.download_job import DownloadJob, DownloadJobStatus
from app.models.prowlarr_settings import ProwlarrSettings
from app.models.qbittorrent_settings import QBittorrentSettings
from app.models.sabnzbd_settings import SabnzbdSettings
from app.models.settings import DiscoveryMethodEnum, QueueSortEnum, Settings, clamp_scan_probe_workers

from app.services import auth_service, discovery_service, download_client_service, event_log_service, notification_service, plex_service, prowlarr_service
from app.services.job_service import (
    abort_all_jobs,
    abort_job,
    cancel_all_queued_jobs,
    cancel_job,
    cleanup_duplicate_optimized_outputs,
    cleanup_optimized_outputs,
    create_job,
    delete_job,
    discard_progress_and_requeue,
    get_job,
    list_jobs,
    pause_job,
    refresh_queued_job_snapshots,
    remove_all_terminal_jobs,
    resume_job,
    retry_job,
)
from app.services.monitoring_service import (
    QMMD_AUTO_DISCOVERY_ENV,
    QMMD_METRICS_URL_ENV,
    _extract_last_json_blob,
    _find_drm_card_sysfs_paths,
    _get_intel_gpu_metrics,
    _get_intel_gpu_metrics_freq,
    _get_intel_gpu_metrics_sysfs,
    _get_nvidia_gpu_metrics,
    _get_qmmd_gpu_metrics,
    _qmmd_candidate_urls,
    _intel_gpu_top_raw,
    get_system_metrics,
)
from app.services.realtime_service import broker, next_message
from app.services.discovery_service import scan_enabled_libraries, scan_library
from app.services.recovery_service import requeue_interrupted_job, run_startup_recovery, run_workspace_cleanup
from app.workers import queue as worker_queue

router = APIRouter()
MEDIA_ROOT = media_root()
WORKSPACE_ROOT_BASE = workspace_root_base()
BRANDING_ROOTS = (
    MEDIA_ROOT / 'Logo',
    Path(__file__).resolve().parents[2] / 'media' / 'Logo',
)
APP_VERSION = os.getenv('OPTIMIZARR_VERSION', '0.1.0')
CSRF_COOKIE_NAME = 'optimizarr_csrf'
CSRF_HEADER_NAME = 'x-csrf-token'
SETUP_ALLOWED_PATHS = {
    '/health',
    '/version',
    '/favicon.ico',
    '/auth/status',
    '/auth/bootstrap',
    '/auth/totp/secret',
}
SETUP_ALLOWED_PREFIXES = ('/branding/',)
LOGIN_RATE_LIMIT_WINDOW_SECONDS = 5 * 60
LOGIN_RATE_LIMIT_MAX_ATTEMPTS = 5
_login_attempts_by_key: dict[str, list[float]] = {}
_login_attempts_lock = Lock()
logger = logging.getLogger(__name__)


def _is_test_runtime() -> bool:
    return 'PYTEST_CURRENT_TEST' in os.environ


def _normalize_api_path(path: str) -> str:
    if path == '/api':
        return '/'
    if path.startswith('/api/'):
        return path[4:]
    return path


def _setup_mode_path_allowed(path: str) -> bool:
    normalized = _normalize_api_path(path)
    if normalized in SETUP_ALLOWED_PATHS:
        return True
    return any(normalized.startswith(prefix) for prefix in SETUP_ALLOWED_PREFIXES)


def _csrf_required(request: Request) -> bool:
    if request.method.upper() not in {'POST', 'PUT', 'PATCH', 'DELETE'}:
        return False
    normalized = _normalize_api_path(request.url.path)
    if normalized in {'/auth/login', '/auth/bootstrap'}:
        return False
    return True


def _request_origin(request: Request) -> str:
    origin = (request.headers.get('origin') or '').strip()
    if origin:
        return origin.rstrip('/')
    referer = (request.headers.get('referer') or '').strip()
    if not referer:
        return ''
    parsed = urlsplit(referer)
    if not parsed.scheme or not parsed.netloc:
        return ''
    return f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')


def _expected_origin(request: Request) -> str:
    return f'{request.url.scheme}://{request.url.netloc}'.rstrip('/')


def _require_same_origin_browser_request(request: Request) -> None:
    origin = _request_origin(request)
    if not origin:
        return
    if not is_safe_same_origin(origin, _expected_origin(request)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Cross-origin request rejected')


def _require_bootstrap_token(request: Request, provided_token: str | None) -> None:
    expected = get_bootstrap_token()
    candidate = (provided_token or request.headers.get(BOOTSTRAP_TOKEN_HEADER_NAME) or '').strip()
    if not candidate or not secrets.compare_digest(candidate, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='Valid setup token required')


def _session_cookie_secure(request: Request) -> bool:
    policy = (os.getenv('OPTIMIZARR_SESSION_COOKIE_SECURE', 'auto') or 'auto').strip().lower()
    if policy in {'1', 'true', 'yes', 'on'}:
        return True
    if policy in {'0', 'false', 'no', 'off'}:
        return False
    return request.url.scheme == 'https'


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    response.set_cookie(
        key=auth_service.SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        samesite='lax',
        secure=_session_cookie_secure(request),
        max_age=auth_service.SESSION_MAX_AGE_SECONDS,
        path='/',
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(auth_service.SESSION_COOKIE_NAME, path='/', httponly=True, samesite='lax')
    response.delete_cookie(CSRF_COOKIE_NAME, path='/', httponly=False, samesite='lax')


def _csrf_cookie_secure(request: Request) -> bool:
    policy = (os.getenv('OPTIMIZARR_SESSION_COOKIE_SECURE', 'auto') or 'auto').strip().lower()
    if policy in {'1', 'true', 'yes', 'on'}:
        return True
    if policy in {'0', 'false', 'no', 'off'}:
        return False
    return request.url.scheme == 'https'


def _set_csrf_cookie_for_request(response: Response, request: Request) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        key=CSRF_COOKIE_NAME,
        value=token,
        httponly=False,
        samesite='lax',
        secure=_csrf_cookie_secure(request),
        max_age=auth_service.SESSION_MAX_AGE_SECONDS,
        path='/',
    )
    return token


def _session_user_from_request(db: Session, request: Request) -> AdminUser | None:
    token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    return auth_service.get_user_from_session_token(db, token)


def require_ui_auth(request: Request, db: Session = Depends(get_db)) -> AdminUser | None:
    if not auth_service.has_admin_user(db):
        if _is_test_runtime():
            return None
        if not _setup_mode_path_allowed(request.url.path):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Admin setup required')
        return None

    user = _session_user_from_request(db, request)
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')

    if _csrf_required(request):
        cookie_token = request.cookies.get(CSRF_COOKIE_NAME) or ''
        header_token = request.headers.get(CSRF_HEADER_NAME, '') or ''
        if not cookie_token or not header_token or not secrets.compare_digest(cookie_token, header_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='CSRF validation failed')
    return user


def _ws_user_or_unauthorized(websocket: WebSocket) -> None:
    with SessionLocal() as db:
        if not auth_service.has_admin_user(db):
            if _is_test_runtime():
                return
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail='Admin setup required')
        token = websocket.cookies.get(auth_service.SESSION_COOKIE_NAME)
        user = auth_service.get_user_from_session_token(db, token)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')


def _login_rate_limit_key(request: Request, username: str) -> str:
    client_host = request.client.host if request.client else 'unknown'
    return f'{client_host}:{auth_service.normalize_username(username).lower()}'


def _prune_login_attempts(now_monotonic: float) -> None:
    threshold = now_monotonic - LOGIN_RATE_LIMIT_WINDOW_SECONDS
    to_delete: list[str] = []
    for key, attempts in _login_attempts_by_key.items():
        kept = [ts for ts in attempts if ts >= threshold]
        if kept:
            _login_attempts_by_key[key] = kept
        else:
            to_delete.append(key)
    for key in to_delete:
        _login_attempts_by_key.pop(key, None)


def _record_failed_login_attempt(key: str) -> None:
    now_mono = time.monotonic()
    with _login_attempts_lock:
        _prune_login_attempts(now_mono)
        _login_attempts_by_key.setdefault(key, []).append(now_mono)


def _clear_login_attempts(key: str) -> None:
    with _login_attempts_lock:
        _login_attempts_by_key.pop(key, None)


def _login_retry_after_seconds(key: str) -> int | None:
    now_mono = time.monotonic()
    with _login_attempts_lock:
        _prune_login_attempts(now_mono)
        attempts = _login_attempts_by_key.get(key, [])
        if len(attempts) < LOGIN_RATE_LIMIT_MAX_ATTEMPTS:
            return None
        oldest_relevant = attempts[-LOGIN_RATE_LIMIT_MAX_ATTEMPTS]
        retry_after = int((oldest_relevant + LOGIN_RATE_LIMIT_WINDOW_SECONDS) - now_mono)
        return max(retry_after, 1)


@router.get('/branding/{asset_name}')
def get_branding_asset(asset_name: str) -> FileResponse:
    asset_variants = {
        'logo': ['logo.png', 'logo.svg', 'logo.webp', 'logo.jpg', 'logo.jpeg', 'Logo.png', 'Logo.svg'],
        'icon': ['dynamic-icon.png', 'dynamic-icon.svg', 'icon.png', 'icon.svg'],
        'dynamic-icon': ['dynamic-icon.png', 'dynamic-icon.svg'],
    }

    candidates = asset_variants.get(asset_name)
    if not candidates:
        raise HTTPException(status_code=404, detail='Branding asset not found')

    for candidate in candidates:
        for root in BRANDING_ROOTS:
            path = root / candidate
            if path.exists() and path.is_file():
                return FileResponse(path)

    raise HTTPException(status_code=404, detail='Branding asset not found')

@router.get('/favicon.ico', include_in_schema=False)
def get_favicon() -> FileResponse:
    return get_branding_asset('icon')


class AuthStatusResponse(BaseModel):
    setup_required: bool
    authenticated: bool
    username: str | None = None
    two_factor_enabled: bool | None = None


class AuthUserResponse(BaseModel):
    username: str
    two_factor_enabled: bool


class AccountUpdateRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    username: str | None = Field(default=None, min_length=3)
    new_password: str | None = Field(default=None, min_length=12)


class EnableTwoFactorRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    totp_secret: str = Field(..., min_length=16)
    totp_code: str = Field(..., min_length=6, max_length=12)


class DisableTwoFactorRequest(BaseModel):
    current_password: str = Field(..., min_length=1)
    totp_code: str = Field(..., min_length=6, max_length=12)


class TotpSecretRequest(BaseModel):
    username: str = Field(..., min_length=1)


class TotpSecretResponse(BaseModel):
    secret: str
    otpauth_url: str


class AuthBootstrapRequest(BaseModel):
    username: str = Field(..., min_length=3)
    password: str = Field(..., min_length=12)
    enable_two_factor: bool = False
    totp_secret: str | None = None
    totp_code: str | None = None
    bootstrap_token: str = Field(..., min_length=1)


class AuthLoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)
    otp_code: str | None = None


@router.get('/auth/status', response_model=AuthStatusResponse)
def auth_status(request: Request, db: Session = Depends(get_db)) -> AuthStatusResponse:
    setup_required = not auth_service.has_admin_user(db)
    if setup_required:
        return AuthStatusResponse(setup_required=True, authenticated=False)

    user = _session_user_from_request(db, request)
    if user is None:
        configured_user = db.query(AdminUser).first()
        return AuthStatusResponse(
            setup_required=False,
            authenticated=False,
            two_factor_enabled=bool(configured_user.two_factor_enabled) if configured_user is not None else False,
        )
    return AuthStatusResponse(
        setup_required=False,
        authenticated=True,
        username=user.username,
        two_factor_enabled=user.two_factor_enabled,
    )


@router.post('/auth/totp/secret', response_model=TotpSecretResponse)
def create_totp_secret(
    payload: TotpSecretRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> TotpSecretResponse:
    # Setup mode can generate secrets before the admin user exists.
    if auth_service.has_admin_user(db) and _session_user_from_request(db, request) is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')

    username = auth_service.normalize_username(payload.username)
    secret = auth_service.generate_totp_secret()
    return TotpSecretResponse(
        secret=secret,
        otpauth_url=auth_service.totp_provisioning_uri(secret, username),
    )


@router.post('/auth/bootstrap', response_model=AuthUserResponse, status_code=201)
def bootstrap_auth(
    payload: AuthBootstrapRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthUserResponse:
    if auth_service.has_admin_user(db):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Admin user already configured')
    _require_same_origin_browser_request(request)
    _require_bootstrap_token(request, payload.bootstrap_token)

    try:
        if payload.enable_two_factor:
            if not payload.totp_secret:
                raise ValueError('TOTP secret is required when two-factor authentication is enabled')
            if not payload.totp_code or not auth_service.verify_totp_code(payload.totp_secret, payload.totp_code):
                raise ValueError('Invalid two-factor code')

        user = auth_service.create_admin_user(
            db,
            username=payload.username,
            password=payload.password,
            two_factor_enabled=payload.enable_two_factor,
            totp_secret=payload.totp_secret,
        )
        token, _session = auth_service.create_session(db, user)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc

    _set_session_cookie(response, token, request)
    _set_csrf_cookie_for_request(response, request)
    return AuthUserResponse(username=user.username, two_factor_enabled=user.two_factor_enabled)


@router.post('/auth/login', response_model=AuthUserResponse)
def login(
    payload: AuthLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> AuthUserResponse:
    if not auth_service.has_admin_user(db):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Admin setup required')

    rate_key = _login_rate_limit_key(request, payload.username)
    retry_after = _login_retry_after_seconds(rate_key)
    if retry_after is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail='Too many login attempts. Try again later.',
            headers={'Retry-After': str(retry_after)},
        )

    user = auth_service.get_user_by_username(db, payload.username)
    if not user or not auth_service.verify_password(payload.password, user.password_hash):
        _record_failed_login_attempt(rate_key)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid credentials')

    if user.two_factor_enabled:
        if not payload.otp_code:
            _record_failed_login_attempt(rate_key)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Two-factor code required')
        if not user.totp_secret or not auth_service.verify_totp_code(user.totp_secret, payload.otp_code):
            _record_failed_login_attempt(rate_key)
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid two-factor code')

    _clear_login_attempts(rate_key)
    token, _session = auth_service.create_session(db, user)
    _set_session_cookie(response, token, request)
    _set_csrf_cookie_for_request(response, request)
    return AuthUserResponse(username=user.username, two_factor_enabled=user.two_factor_enabled)


@router.post('/auth/logout')
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict[str, str]:
    token = request.cookies.get(auth_service.SESSION_COOKIE_NAME)
    auth_service.revoke_session(db, token)
    _clear_session_cookie(response)
    return {'status': 'ok'}


@router.get('/auth/account', response_model=AuthUserResponse)
def get_auth_account(current_user: AdminUser | None = Depends(require_ui_auth)) -> AuthUserResponse:
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')
    return AuthUserResponse(username=current_user.username, two_factor_enabled=current_user.two_factor_enabled)


@router.post('/auth/account', response_model=AuthUserResponse)
def update_auth_account(
    payload: AccountUpdateRequest,
    db: Session = Depends(get_db),
    current_user: AdminUser | None = Depends(require_ui_auth),
) -> AuthUserResponse:
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')

    if not auth_service.verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid current password')

    has_any_update = False

    if payload.username is not None:
        normalized_username = auth_service.normalize_username(payload.username)
        if len(normalized_username) < 3:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Username must be at least 3 characters long')
        existing = (
            db.query(AdminUser)
            .filter(AdminUser.username == normalized_username, AdminUser.id != current_user.id)
            .first()
        )
        if existing is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Username already exists')
        if normalized_username != current_user.username:
            current_user.username = normalized_username
            has_any_update = True

    if payload.new_password is not None:
        if len(payload.new_password) < 12:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Password must be at least 12 characters long')
        current_user.password_hash = auth_service.hash_password(payload.new_password)
        has_any_update = True

    if not has_any_update:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='No account changes provided')

    db.commit()
    db.refresh(current_user)
    return AuthUserResponse(username=current_user.username, two_factor_enabled=current_user.two_factor_enabled)


@router.post('/auth/account/2fa/enable', response_model=AuthUserResponse)
def enable_auth_account_two_factor(
    payload: EnableTwoFactorRequest,
    db: Session = Depends(get_db),
    current_user: AdminUser | None = Depends(require_ui_auth),
) -> AuthUserResponse:
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')
    if current_user.two_factor_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Two-factor authentication is already enabled')
    if not auth_service.verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid current password')
    if not auth_service.verify_totp_code(payload.totp_secret, payload.totp_code):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid two-factor code')

    current_user.two_factor_enabled = True
    current_user.totp_secret = payload.totp_secret
    db.commit()
    db.refresh(current_user)
    return AuthUserResponse(username=current_user.username, two_factor_enabled=current_user.two_factor_enabled)


@router.post('/auth/account/2fa/disable', response_model=AuthUserResponse)
def disable_auth_account_two_factor(
    payload: DisableTwoFactorRequest,
    db: Session = Depends(get_db),
    current_user: AdminUser | None = Depends(require_ui_auth),
) -> AuthUserResponse:
    if current_user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Authentication required')
    if not current_user.two_factor_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail='Two-factor authentication is not enabled')
    if not auth_service.verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid current password')
    if not current_user.totp_secret or not auth_service.verify_totp_code(current_user.totp_secret, payload.totp_code):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail='Invalid two-factor code')

    current_user.two_factor_enabled = False
    current_user.totp_secret = None
    db.commit()
    db.refresh(current_user)
    return AuthUserResponse(username=current_user.username, two_factor_enabled=current_user.two_factor_enabled)


class JobCreateRequest(BaseModel):
    source_path: str = Field(..., examples=['/data/media/in/movie.mkv'])


class JobResponse(BaseModel):
    id: int
    status: str
    source_path: str
    output_path: str | None = None
    retry_count: int
    cancel_requested: bool
    progress_percent: int
    fps: float | None = None
    eta_seconds: int | None = None
    encoder_used: str | None = None
    codec_used: str | None = None
    hwaccel_used: bool | None = None
    used_fallback: bool | None = None
    fallback_reason: str | None = None
    error_message: str | None = None
    source_resolution: int | None = None
    source_is_hdr: bool | None = None
    library_id: int | None = None
    encode_duration_seconds: int | None = None
    completed_at: str | None = None

    @classmethod
    def from_orm_job(cls, job):
        return cls(
            id=job.id,
            status=job.status,
            source_path=job.source_path,
            output_path=job.output_path,
            retry_count=job.retry_count,
            cancel_requested=job.cancel_requested,
            progress_percent=job.progress_percent,
            fps=job.fps,
            eta_seconds=job.eta_seconds,
            encoder_used=job.encoder_used,
            codec_used=job.codec_used,
            hwaccel_used=job.hwaccel_used,
            used_fallback=job.used_fallback,
            fallback_reason=job.fallback_reason,
            error_message=job.error_message,
            source_resolution=job.source_resolution,
            source_is_hdr=job.source_is_hdr,
            library_id=job.library_id,
            encode_duration_seconds=job.encode_duration_seconds,
            completed_at=job.completed_at.replace(tzinfo=timezone.utc).isoformat() if job.completed_at else None,
        )


class ScanResponse(BaseModel):
    created_jobs: list[JobResponse]


class MetricsResponse(BaseModel):
    gpu_video_percent: float
    gpu_render_percent: float
    cpu_percent: float
    ram_percent: float
    active_jobs: int


class SettingsResponse(BaseModel):
    enable_optimizer: bool
    target_resolution: int
    bitrate_mbps: int
    keep_original: bool
    max_workers: int
    scan_interval_minutes: int
    process_hdr_only: bool
    history_retention_days: int
    auto_discovery_enabled: bool
    discovery_method: DiscoveryMethodEnum
    discovery_interval_minutes: int
    queue_sort: QueueSortEnum
    workspace_root: str
    scan_probe_workers: int
    requeue_interrupted_jobs: bool
    cleanup_workspaces_on_startup: bool
    duplicate_cleanup_enabled: bool
    duplicate_cleanup_interval_hours: int
    min_free_gb: int
    qbt_strike_check_interval_seconds: int
    qbt_metadata_max_strikes: int
    qbt_stalled_max_strikes: int
    qbt_slow_min_speed_bps: int
    qbt_slow_max_strikes: int
    qbt_slow_ignore_private: bool

    @classmethod
    def from_orm_settings(cls, settings: Settings):
        return cls(
            enable_optimizer=settings.enable_optimizer,
            target_resolution=settings.target_resolution,
            bitrate_mbps=settings.bitrate_mbps,
            keep_original=settings.keep_original,
            max_workers=settings.max_workers,
            scan_interval_minutes=settings.scan_interval_minutes,
            process_hdr_only=settings.process_hdr_only,
            history_retention_days=settings.history_retention_days,
            auto_discovery_enabled=settings.auto_discovery_enabled,
            discovery_method=settings.discovery_method,
            discovery_interval_minutes=settings.discovery_interval_minutes,
            queue_sort=settings.queue_sort,
            workspace_root=settings.workspace_root,
            scan_probe_workers=clamp_scan_probe_workers(settings.scan_probe_workers),
            requeue_interrupted_jobs=settings.requeue_interrupted_jobs,
            cleanup_workspaces_on_startup=settings.cleanup_workspaces_on_startup,
            duplicate_cleanup_enabled=settings.duplicate_cleanup_enabled,
            duplicate_cleanup_interval_hours=settings.duplicate_cleanup_interval_hours,
            min_free_gb=settings.min_free_gb,
            qbt_strike_check_interval_seconds=settings.qbt_strike_check_interval_seconds,
            qbt_metadata_max_strikes=settings.qbt_metadata_max_strikes,
            qbt_stalled_max_strikes=settings.qbt_stalled_max_strikes,
            qbt_slow_min_speed_bps=settings.qbt_slow_min_speed_bps,
            qbt_slow_max_strikes=settings.qbt_slow_max_strikes,
            qbt_slow_ignore_private=settings.qbt_slow_ignore_private,
        )


class SettingsUpdateRequest(BaseModel):
    enable_optimizer: bool | None = None
    target_resolution: int | None = Field(default=None, ge=1)
    bitrate_mbps: int | None = Field(default=None, ge=1)
    keep_original: bool | None = None
    max_workers: int | None = Field(default=None, ge=1)
    scan_interval_minutes: int | None = Field(default=None, ge=1)
    process_hdr_only: bool | None = None
    history_retention_days: int | None = Field(default=None, ge=1)
    auto_discovery_enabled: bool | None = None
    discovery_method: DiscoveryMethodEnum | None = None
    discovery_interval_minutes: int | None = Field(default=None, ge=1)
    queue_sort: QueueSortEnum | None = None
    workspace_root: str | None = Field(default=None, min_length=1)
    scan_probe_workers: int | None = Field(default=None, ge=1)
    requeue_interrupted_jobs: bool | None = None
    cleanup_workspaces_on_startup: bool | None = None
    duplicate_cleanup_enabled: bool | None = None
    duplicate_cleanup_interval_hours: int | None = Field(default=None, ge=1)
    min_free_gb: int | None = Field(default=None, ge=1)
    qbt_strike_check_interval_seconds: int | None = Field(default=None, ge=1)
    qbt_metadata_max_strikes: int | None = Field(default=None, ge=0)
    qbt_stalled_max_strikes: int | None = Field(default=None, ge=0)
    qbt_slow_min_speed_bps: int | None = Field(default=None, ge=0)
    qbt_slow_max_strikes: int | None = Field(default=None, ge=0)
    qbt_slow_ignore_private: bool | None = None

    @field_validator('workspace_root')
    @classmethod
    def validate_workspace_root(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_workspace_root(value)



class RecoveryResponse(BaseModel):
    recovered_jobs: int
    requeued_jobs: int
    cleaned_workspaces: int


class CleanupResponse(BaseModel):
    cleaned_workspaces: int


class OptimizedCleanupResponse(BaseModel):
    deleted_files: int
    affected_job_ids: list[int]


class DuplicateOptimizedCleanupResponse(BaseModel):
    deleted_files: int
    affected_library_ids: list[int]


class ClearEventLogsResponse(BaseModel):
    deleted_logs: int


class EventLogResponse(BaseModel):
    id: int
    event_type: str
    severity: str
    message: str
    details: dict[str, object] = Field(default_factory=dict)
    created_at: str

    @classmethod
    def from_orm_log(cls, log):
        details = {}
        if log.details_json:
            try:
                import json
                parsed = json.loads(log.details_json)
                if isinstance(parsed, dict):
                    details = parsed
            except (TypeError, ValueError):
                details = {}
        return cls(
            id=log.id,
            event_type=log.event_type,
            severity=log.severity,
            message=log.message,
            details=details,
            created_at=log.created_at.replace(tzinfo=timezone.utc).isoformat() if log.created_at else '',
        )


def _record_log_event(
    db: Session,
    event_type: str,
    message: str,
    *,
    severity: str = 'info',
    details: dict | None = None,
) -> None:
    event_log_service.record_event(
        db,
        event_type,
        message,
        severity=severity,
        details=details,
    )


def _enabled_libraries(db: Session) -> list[Library]:
    return db.query(Library).filter(Library.enabled.is_(True)).order_by(Library.name.asc()).all()


def _library_names_for_ids(db: Session, library_ids: list[int]) -> list[str]:
    if not library_ids:
        return []
    libraries = db.query(Library).filter(Library.id.in_(library_ids)).all()
    names_by_id = {library.id: library.name for library in libraries}
    return [names_by_id[library_id] for library_id in library_ids if library_id in names_by_id]


class AbortAllJobsResponse(BaseModel):
    aborted_job_ids: list[int]


class RemoveAllJobsResponse(BaseModel):
    removed_job_ids: list[int]
    removed_download_job_ids: list[int] = []


class CancelAllQueuedResponse(BaseModel):
    cancelled_job_ids: list[int]


class ClearQueueResponse(BaseModel):
    removed_job_ids: list[int]
    removed_download_job_ids: list[int]


class NotificationTriggerSettings(BaseModel):
    job_complete: bool = True
    job_failed: bool = True
    job_interrupted: bool = True
    low_disk_pause: bool = True
    recovery_ran: bool = True
    batch_complete: bool = True


class NotificationSettingsResponse(BaseModel):
    smtp_host: str
    smtp_port: int
    smtp_user: str
    smtp_password: str
    smtp_tls: bool
    from_email: str
    to_emails: list[str]
    notify_on: NotificationTriggerSettings


class NotificationSettingsUpdateRequest(BaseModel):
    smtp_host: str | None = None
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_tls: bool | None = None
    from_email: str | None = None
    to_emails: list[str] | None = None
    notify_on: NotificationTriggerSettings | None = None

    @field_validator('smtp_host')
    @classmethod
    def validate_smtp_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_smtp_host(value)


class PlexSettingsResponse(BaseModel):
    enabled: bool
    host: str
    port: int
    token: str


class PlexSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    token: str | None = None

    @field_validator('host')
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_http_host_without_port(value)


class PlexLibrarySection(BaseModel):
    id: str
    name: str
    type: str


class ProwlarrSettingsResponse(BaseModel):
    enabled: bool
    host: str
    api_key: str


class ProwlarrSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    host: str | None = None
    api_key: str | None = None

    @field_validator('host')
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_http_origin_with_optional_port(value)


class QBittorrentSettingsResponse(BaseModel):
    enabled: bool
    host: str
    port: int
    username: str
    password: str


class QBittorrentSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    username: str | None = None
    password: str | None = None

    @field_validator('host')
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_http_host_without_port(value)


class SabnzbdSettingsResponse(BaseModel):
    enabled: bool
    host: str
    port: int
    api_key: str


class SabnzbdSettingsUpdateRequest(BaseModel):
    enabled: bool | None = None
    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    api_key: str | None = None

    @field_validator('host')
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_http_host_without_port(value)


class DownloadJobResponse(BaseModel):
    id: int
    library_id: int | None = None
    source_file_path: str
    search_query: str | None = None
    release_name: str | None = None
    indexer_id: int | None = None
    indexer_name: str | None = None
    selected_release_key: str | None = None
    failed_release_keys: str | None = None
    retry_count: int = 0
    max_retries: int = 5
    download_hash: str | None = None
    client_type: str | None = None
    status: str
    progress_percent: int
    eta_seconds: int | None = None
    download_speed_bps: int | None = None
    client_queue_position: int | None = None
    downloaded_file_path: str | None = None
    imported_file_path: str | None = None
    error_message: str | None = None
    encode_job_id: int | None = None
    created_at: str | None = None
    download_started_at: str | None = None
    completed_at: str | None = None

    @classmethod
    def from_orm(cls, dj: DownloadJob, *, client_queue_position: int | None = None):
        from datetime import timezone
        return cls(
            id=dj.id,
            library_id=dj.library_id,
            source_file_path=dj.source_file_path,
            search_query=dj.search_query,
            release_name=dj.release_name,
            indexer_id=dj.indexer_id,
            indexer_name=dj.indexer_name,
            selected_release_key=dj.selected_release_key,
            failed_release_keys=dj.failed_release_keys,
            retry_count=dj.retry_count,
            max_retries=dj.max_retries,
            download_hash=dj.download_hash,
            client_type=dj.client_type,
            status=dj.status,
            progress_percent=dj.progress_percent,
            eta_seconds=dj.eta_seconds,
            download_speed_bps=dj.download_speed_bps,
            client_queue_position=client_queue_position,
            downloaded_file_path=dj.downloaded_file_path,
            imported_file_path=dj.imported_file_path,
            error_message=dj.error_message,
            encode_job_id=dj.encode_job_id,
            created_at=dj.created_at.replace(tzinfo=timezone.utc).isoformat() if dj.created_at else None,
            download_started_at=dj.download_started_at.replace(tzinfo=timezone.utc).isoformat() if dj.download_started_at else None,
            completed_at=dj.completed_at.replace(tzinfo=timezone.utc).isoformat() if dj.completed_at else None,
        )


def _sab_queue_positions_by_nzo(db: Session) -> dict[str, int]:
    sab = download_client_service.get_or_create_sab_settings(db)
    if not sab.enabled:
        return {}
    try:
        return {
            str(item.get('nzo_id') or '').strip(): int(item.get('index'))
            for item in download_client_service.get_sab_queue_items(sab)
            if str(item.get('nzo_id') or '').strip() and item.get('index') is not None
        }
    except Exception:
        return {}


def _download_job_client_queue_position(dj: DownloadJob, sab_positions: dict[str, int]) -> int | None:
    if dj.client_type != 'sabnzbd' or not dj.download_hash:
        return None
    return sab_positions.get(str(dj.download_hash).strip())


class LibraryBaseRequest(BaseModel):
    name: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    enabled: bool = True

    @field_validator('path')
    @classmethod
    def validate_media_path(cls, value: str) -> str:
        return normalize_path_within_root(
            value,
            root=MEDIA_ROOT,
            must_exist=True,
            must_be_dir=True,
        )


class LibraryCreateRequest(LibraryBaseRequest):
    pass


class LibraryUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    path: str | None = None
    enabled: bool | None = None

    @field_validator('path')
    @classmethod
    def validate_optional_media_path(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return LibraryBaseRequest.validate_media_path(value)


class LibraryResponse(BaseModel):
    id: int
    name: str
    path: str
    enabled: bool
    scanning: bool = False

    @classmethod
    def from_orm_library(cls, library: Library):
        return cls(
            id=library.id,
            name=library.name,
            path=library.path,
            enabled=library.enabled,
            scanning=discovery_service.is_library_scan_active(library.id),
        )


class LibraryProfileResponse(BaseModel):
    target_resolution: int
    minimum_source_resolution: int
    codec: CodecEnum
    container: ContainerEnum
    audio_mode: AudioModeEnum
    bitrate_mode: BitrateModeEnum
    bitrate_mbps: int | None
    crf: int | None
    speed_preset: SpeedPresetEnum
    hdr_only: bool
    tone_map_hdr: bool
    max_workers: int
    schedule_enabled: bool
    schedule_start_hour: int
    schedule_end_hour: int
    schedule_policy: SchedulePolicyEnum
    output_suffix: str
    output_conflict_policy: OutputConflictPolicyEnum
    av1_fallback_codec: CodecEnum
    preferred_video_encoder: PreferredEncoderEnum
    plex_library_id: str | None = None
    download_enabled: bool = False
    download_timeout_minutes: int = 60
    download_codec: CodecEnum | None = None
    download_fallback_codec: CodecEnum | None = None
    download_quality_profile: DownloadQualityProfileEnum = DownloadQualityProfileEnum.any

    @classmethod
    def from_orm_profile(cls, profile: LibraryProfile):
        return cls(
            target_resolution=profile.target_resolution,
            minimum_source_resolution=profile.minimum_source_resolution,
            codec=profile.codec,
            container=profile.container,
            audio_mode=profile.audio_mode,
            bitrate_mode=profile.bitrate_mode,
            bitrate_mbps=profile.bitrate_mbps,
            crf=profile.crf,
            speed_preset=profile.speed_preset,
            hdr_only=profile.hdr_only,
            tone_map_hdr=profile.tone_map_hdr,
            max_workers=profile.max_workers,
            schedule_enabled=profile.schedule_enabled,
            schedule_start_hour=profile.schedule_start_hour,
            schedule_end_hour=profile.schedule_end_hour,
            schedule_policy=profile.schedule_policy,
            output_suffix=profile.output_suffix,
            output_conflict_policy=profile.output_conflict_policy,
            av1_fallback_codec=profile.av1_fallback_codec,
            preferred_video_encoder=profile.preferred_video_encoder,
            plex_library_id=profile.plex_library_id,
            download_enabled=getattr(profile, 'download_enabled', False),
            download_timeout_minutes=getattr(profile, 'download_timeout_minutes', 60),
            download_codec=getattr(profile, 'download_codec', None),
            download_fallback_codec=getattr(profile, 'download_fallback_codec', None),
            download_quality_profile=getattr(profile, 'download_quality_profile', DownloadQualityProfileEnum.any),
        )


class EncoderAvailabilityResponse(BaseModel):
    codec: CodecEnum
    available_encoders: list[PreferredEncoderEnum]


class EncodersResponse(BaseModel):
    encoders: list[EncoderAvailabilityResponse]


class LibraryProfileUpdateRequest(BaseModel):
    target_resolution: int | None = Field(default=None, ge=1)
    minimum_source_resolution: int | None = Field(default=None, ge=1)
    codec: CodecEnum | None = None
    container: ContainerEnum | None = None
    audio_mode: AudioModeEnum | None = None
    bitrate_mode: BitrateModeEnum | None = None
    bitrate_mbps: int | None = Field(default=None, ge=1)
    crf: int | None = Field(default=None, ge=1)
    speed_preset: SpeedPresetEnum | None = None
    hdr_only: bool | None = None
    tone_map_hdr: bool | None = None
    max_workers: int | None = Field(default=None, ge=1)
    schedule_enabled: bool | None = None
    schedule_start_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_end_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_policy: SchedulePolicyEnum | None = None
    output_suffix: str | None = None
    output_conflict_policy: OutputConflictPolicyEnum | None = None
    av1_fallback_codec: CodecEnum | None = None
    preferred_video_encoder: PreferredEncoderEnum | None = None
    plex_library_id: str | None = None
    download_enabled: bool | None = None
    download_timeout_minutes: int | None = Field(default=None, ge=1)
    download_codec: CodecEnum | None = None
    download_fallback_codec: CodecEnum | None = None
    download_quality_profile: DownloadQualityProfileEnum | None = None

    @field_validator('av1_fallback_codec')
    @classmethod
    def fallback_codec_must_not_be_av1(cls, value: CodecEnum | None) -> CodecEnum | None:
        if value == CodecEnum.av1:
            raise ValueError('av1_fallback_codec must be hevc or h264')
        return value

    @field_validator('preferred_video_encoder')
    @classmethod
    def preferred_encoder_matches_codec(cls, value: PreferredEncoderEnum | None, info):
        if value is None or value == PreferredEncoderEnum.auto:
            return value

        codec = info.data.get('codec')
        if codec is None:
            return value

        allowed = {
            CodecEnum.h264: {PreferredEncoderEnum.h264_qsv, PreferredEncoderEnum.h264_vaapi, PreferredEncoderEnum.libx264},
            CodecEnum.hevc: {PreferredEncoderEnum.hevc_qsv, PreferredEncoderEnum.hevc_vaapi, PreferredEncoderEnum.libx265},
            CodecEnum.av1: {PreferredEncoderEnum.av1_qsv, PreferredEncoderEnum.av1_vaapi, PreferredEncoderEnum.libsvtav1},
        }
        if value not in allowed.get(codec, set()):
            raise ValueError('preferred_video_encoder is incompatible with selected codec')
        return value



def _get_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _get_library_or_404(db: Session, library_id: int) -> Library:
    library = db.query(Library).filter(Library.id == library_id).first()
    if not library:
        raise HTTPException(status_code=404, detail='Library not found')
    return library


def _get_or_create_library_profile(db: Session, library: Library) -> LibraryProfile:
    if library.profile:
        return library.profile

    profile = LibraryProfile(library_id=library.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def _validate_resolution_constraints(
    target_resolution: int,
    minimum_source_resolution: int,
    hdr_only: bool,
    tone_map_hdr: bool = False,
) -> None:
    if not hdr_only and not tone_map_hdr and minimum_source_resolution <= target_resolution:
        raise HTTPException(
            status_code=422,
            detail='minimum_source_resolution must be greater than target_resolution',
        )


@router.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}


class DirListResponse(BaseModel):
    path: str
    parent: str | None
    dirs: list[str]


@router.get('/fs/dirs', response_model=DirListResponse)
def list_dirs(
    path: str | None = Query(default=None),
    _: None = Depends(require_ui_auth),
) -> DirListResponse:
    target = MEDIA_ROOT.resolve()
    if path:
        try:
            candidate = normalize_path_within_root(
                path,
                root=MEDIA_ROOT,
                must_exist=True,
                must_be_dir=True,
            )
            target = Path(candidate)
        except ValueError:
            target = MEDIA_ROOT.resolve()

    dirs = sorted(
        p.name for p in target.iterdir()
        if p.is_dir() and not p.name.startswith('.')
    )
    parent = None
    if target != MEDIA_ROOT.resolve():
        parent = str(target.parent)
    return DirListResponse(path=str(target), parent=parent, dirs=dirs)


@router.get('/version')
def version() -> dict[str, str]:
    return {'version': APP_VERSION}


@router.get('/settings', response_model=SettingsResponse)
def get_settings(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> SettingsResponse:
    settings = _get_settings(db)
    return SettingsResponse.from_orm_settings(settings)


@router.post('/settings', response_model=SettingsResponse)
def update_settings(
    payload: SettingsUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> SettingsResponse:
    settings = _get_settings(db)

    updates = payload.model_dump(exclude_none=True)
    if 'scan_probe_workers' in updates:
        updates['scan_probe_workers'] = clamp_scan_probe_workers(updates['scan_probe_workers'])
    for field_name, value in updates.items():
        setattr(settings, field_name, value)

    db.commit()
    db.refresh(settings)
    broker.publish_notification('settings_updated')
    return SettingsResponse.from_orm_settings(settings)


@router.get('/notifications/settings', response_model=NotificationSettingsResponse)
def get_notification_settings(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> NotificationSettingsResponse:
    settings = notification_service.get_or_create_notification_settings(db)
    return NotificationSettingsResponse(**notification_service.settings_to_payload(settings))


@router.put('/notifications/settings', response_model=NotificationSettingsResponse)
def update_notification_settings(
    payload: NotificationSettingsUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> NotificationSettingsResponse:
    settings = notification_service.update_settings(db, payload.model_dump(exclude_none=True))
    return NotificationSettingsResponse(**notification_service.settings_to_payload(settings))


@router.post('/notifications/test', status_code=202)
def send_test_notification(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    notification_service.enqueue_test_email()
    return {'status': 'queued'}


@router.get('/plex/settings', response_model=PlexSettingsResponse)
def get_plex_settings(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> PlexSettingsResponse:
    settings = plex_service.get_or_create_plex_settings(db)
    return PlexSettingsResponse(**plex_service.settings_to_payload(settings))


@router.put('/plex/settings', response_model=PlexSettingsResponse)
def update_plex_settings(
    payload: PlexSettingsUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> PlexSettingsResponse:
    settings = plex_service.update_settings(db, payload.model_dump(exclude_none=True))
    return PlexSettingsResponse(**plex_service.settings_to_payload(settings))


@router.post('/plex/test', status_code=200)
def test_plex_connection_endpoint(_: None = Depends(require_ui_auth)) -> dict:
    return plex_service.test_plex_connection()


@router.get('/plex/libraries', response_model=list[PlexLibrarySection])
def get_plex_libraries(_: None = Depends(require_ui_auth)) -> list[PlexLibrarySection]:
    sections = plex_service.fetch_plex_libraries()
    return [PlexLibrarySection(**s) for s in sections]


@router.get('/libraries', response_model=list[LibraryResponse])
def get_libraries(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> list[LibraryResponse]:
    libraries = db.query(Library).order_by(Library.name.asc()).all()
    return [LibraryResponse.from_orm_library(library) for library in libraries]


@router.post('/libraries', response_model=LibraryResponse, status_code=201)
def create_library(
    payload: LibraryCreateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> LibraryResponse:
    existing = db.query(Library).filter(Library.path == payload.path).first()
    if existing:
        raise HTTPException(status_code=409, detail='Library path already exists')

    library = Library(name=payload.name, path=payload.path, enabled=payload.enabled)
    db.add(library)
    db.commit()
    db.refresh(library)
    broker.publish_library_update('created', LibraryResponse.from_orm_library(library).model_dump())

    profile = LibraryProfile(library_id=library.id)
    db.add(profile)
    db.commit()
    db.refresh(library)

    return LibraryResponse.from_orm_library(library)


@router.put('/libraries/{library_id}', response_model=LibraryResponse)
def update_library(
    library_id: int,
    payload: LibraryUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> LibraryResponse:
    library = _get_library_or_404(db, library_id)

    updates = payload.model_dump(exclude_none=True)
    if 'path' in updates:
        existing = db.query(Library).filter(Library.path == updates['path'], Library.id != library_id).first()
        if existing:
            raise HTTPException(status_code=409, detail='Library path already exists')

    for field_name, value in updates.items():
        setattr(library, field_name, value)

    db.commit()
    db.refresh(library)
    payload = LibraryResponse.from_orm_library(library)
    broker.publish_library_update('updated', payload.model_dump())
    return payload


@router.delete('/libraries/{library_id}', status_code=204)
def delete_library(library_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> None:
    library = _get_library_or_404(db, library_id)
    payload = LibraryResponse.from_orm_library(library).model_dump()
    db.delete(library)
    db.commit()
    broker.publish_library_update('deleted', payload)


@router.get('/libraries/{library_id}/profile', response_model=LibraryProfileResponse)
def get_library_profile(
    library_id: int,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> LibraryProfileResponse:
    library = _get_library_or_404(db, library_id)
    profile = _get_or_create_library_profile(db, library)
    return LibraryProfileResponse.from_orm_profile(profile)


@router.put('/libraries/{library_id}/profile', response_model=LibraryProfileResponse)
def update_library_profile(
    library_id: int,
    payload: LibraryProfileUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> LibraryProfileResponse:
    library = _get_library_or_404(db, library_id)
    profile = _get_or_create_library_profile(db, library)

    updates = payload.model_dump(exclude_none=True)

    target_resolution = int(updates.get('target_resolution', profile.target_resolution))
    minimum_source_resolution = int(updates.get('minimum_source_resolution', profile.minimum_source_resolution))
    hdr_only = bool(updates.get('hdr_only', profile.hdr_only))
    tone_map_hdr = bool(updates.get('tone_map_hdr', profile.tone_map_hdr))
    _validate_resolution_constraints(target_resolution, minimum_source_resolution, hdr_only, tone_map_hdr)

    for field_name, value in updates.items():
        setattr(profile, field_name, value)

    db.commit()
    db.refresh(profile)
    refresh_queued_job_snapshots(db, library_id, profile)
    broker.publish_library_update('profile_updated', {'library_id': library_id})
    return LibraryProfileResponse.from_orm_profile(profile)



@router.get('/encoders', response_model=EncodersResponse)
def get_encoders(_: None = Depends(require_ui_auth)) -> EncodersResponse:
    from app.services.optimization_service import available_encoders_by_codec

    available = available_encoders_by_codec()
    payload = [
        EncoderAvailabilityResponse(codec=CodecEnum(codec), available_encoders=[PreferredEncoderEnum(item) for item in encoders])
        for codec, encoders in available.items()
    ]
    return EncodersResponse(encoders=payload)


@router.get('/metrics', response_model=MetricsResponse)
def metrics(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> MetricsResponse:
    return MetricsResponse(**get_system_metrics(db))


@router.get('/debug/gpu')
def debug_gpu(_: None = Depends(require_ui_auth)) -> dict:
    """Diagnostic endpoint: run each GPU detection method and report raw results.

    Useful for debugging why GPU% shows 0 on the dashboard.
    Hit GET /api/debug/gpu and inspect the JSON response.
    """
    import glob as _glob
    import os as _os

    def _read_int(path: str) -> int | None:
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    # ── sysfs engine dirs + files present ─────────────────────────────────────
    _card_paths = _find_drm_card_sysfs_paths()
    engine_dirs = [d for card in _card_paths for d in _glob.glob(f'{card}/engine/*')]
    sysfs_busy_files: list[str] = []
    engine_dir_contents: dict[str, list[str]] = {}
    for d in engine_dirs:
        try:
            files = sorted(_os.listdir(d))
        except OSError:
            files = []
        engine_dir_contents[d] = files
        if 'busy_time_ms' in files:
            sysfs_busy_files.append(_os.path.join(d, 'busy_time_ms'))

    sysfs_result = _get_intel_gpu_metrics_sysfs()

    # ── GT frequency (Intel iGPU — better proxy for QSV load than engine %) ───
    freq_info: dict[str, dict] = {}
    for card in _card_paths:
        entry: dict = {}
        # Old i915 flat paths
        for name, path in [
            ('act_mhz',  f'{card}/gt_act_freq_mhz'),
            ('cur_mhz',  f'{card}/gt_cur_freq_mhz'),
            ('min_mhz',  f'{card}/gt_min_freq_mhz'),
            ('max_mhz',  f'{card}/gt_max_freq_mhz'),
            ('RP0_mhz',  f'{card}/gt_RP0_freq_mhz'),
            ('RPn_mhz',  f'{card}/gt_RPn_freq_mhz'),
        ]:
            v = _read_int(path)
            if v is not None:
                entry[name] = v
        # New xe / newer-i915 nested paths
        for gt in _glob.glob(f'{card}/gt/gt*'):
            gt_name = _os.path.basename(gt)
            gt_entry: dict = {}
            for name, fname in [
                ('act_mhz', 'rps_act_freq_mhz'),
                ('cur_mhz', 'rps_cur_freq_mhz'),
                ('min_mhz', 'rps_min_freq_mhz'),
                ('max_mhz', 'rps_max_freq_mhz'),
            ]:
                v = _read_int(f'{gt}/{fname}')
                if v is not None:
                    gt_entry[name] = v
            if gt_entry:
                entry[gt_name] = gt_entry
        if entry:
            freq_info[_os.path.basename(card)] = entry

    # ── GT frequency metric (new primary method) ──────────────────────────────
    freq_result = _get_intel_gpu_metrics_freq()

    # ── intel_gpu_top (parsed + raw last sample) ───────────────────────────────
    intel_result = _get_intel_gpu_metrics()
    raw = _intel_gpu_top_raw()
    last_blob = _extract_last_json_blob(raw['stdout'])

    # ── nvidia-smi ─────────────────────────────────────────────────────────────
    nvidia_result = _get_nvidia_gpu_metrics()

    return {
        'drm_card_sysfs_paths': _card_paths,
        'qmassa_qmmd': {
            'metrics_url_env': QMMD_METRICS_URL_ENV,
            'auto_discovery_env': QMMD_AUTO_DISCOVERY_ENV,
            'metrics_url_configured': bool((_os.getenv(QMMD_METRICS_URL_ENV) or '').strip()),
            'candidate_urls': _qmmd_candidate_urls(),
            'result': _get_qmmd_gpu_metrics(),
        },
        'sysfs': {
            'engine_dir_contents': engine_dir_contents,
            'busy_time_ms_files_found': sysfs_busy_files,
            'result': sysfs_result,
        },
        'gt_frequency': {
            'raw_sysfs': freq_info,
            'parsed_result': freq_result,
        },
        'intel_gpu_top': {
            'parsed_result': intel_result,
            'last_sample_engines': last_blob.get('engines'),
            'last_sample_rc6_pct': last_blob.get('rc6', {}).get('value'),
            'last_sample_period_ms': last_blob.get('period', {}).get('duration'),
            'raw_stderr': raw['stderr'][:512],
            'launch_error': raw['error'],
        },
        'nvidia_smi': {
            'result': nvidia_result,
        },
    }


@router.post('/jobs', response_model=JobResponse, status_code=201)
@router.post('/api/jobs', response_model=JobResponse, status_code=201, include_in_schema=False)
def create_optimization_job(
    payload: JobCreateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> JobResponse:
    job = create_job(db, payload.source_path)
    if job.status == 'skipped':
        return JobResponse.from_orm_job(job)
    notification_service.register_scan_batch([job.id])
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    return response


@router.get('/jobs', response_model=list[JobResponse])
@router.get('/api/jobs', response_model=list[JobResponse], include_in_schema=False)
def get_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> list[JobResponse]:
    jobs = [JobResponse.from_orm_job(job) for job in list_jobs(db)]
    return jobs


@router.get('/jobs/{job_id}', response_model=JobResponse)
@router.get('/api/jobs/{job_id}', response_model=JobResponse, include_in_schema=False)
def get_job_by_id(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    return response


@router.delete('/jobs/{job_id}', status_code=204)
def delete_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> None:
    deleted = delete_job(db, job_id)
    if not deleted:
        raise HTTPException(status_code=404, detail='Job not found')
    _record_log_event(
        db,
        'job_removed',
        f'Job {job_id} was removed',
        details={'job_id': job_id},
    )
    broker.publish_system_event('job_removed', job_id=job_id)


@router.post('/libraries/{library_id}/scan', response_model=ScanResponse)
def scan_library_jobs(library_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ScanResponse:
    library = _get_library_or_404(db, library_id)
    _record_log_event(
        db,
        'library_scan_started',
        f'Library scan started for {library.name}',
        details={'library_name': library.name},
    )
    worker_queue.pause_queue(reason='manual_scan')
    try:
        jobs = scan_library(db, library, include_disabled=True)
        payload = [JobResponse.from_orm_job(job) for job in jobs]
        notification_service.register_scan_batch([job.id for job in jobs], library_name=library.name)
        _record_log_event(
            db,
            'library_scan_summary',
            f'Library scan completed for {library.name}',
            details={
                'library_name': library.name,
                'created_jobs': len(jobs),
                'job_ids': [job.id for job in jobs],
            },
        )
        return ScanResponse(created_jobs=payload)
    finally:
        worker_queue.resume_queue(reason='manual_scan')


@router.post('/scan', response_model=ScanResponse)
def scan_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ScanResponse:
    libraries = _enabled_libraries(db)
    library_names = [library.name for library in libraries]
    _record_log_event(
        db,
        'library_scan_started',
        'All enabled libraries scan started',
        details={'library_names': library_names},
    )
    worker_queue.pause_queue(reason='manual_scan')
    try:
        created_jobs = scan_enabled_libraries(db)
        payload = [JobResponse.from_orm_job(job) for job in created_jobs]
        notification_service.register_scan_batch([job.id for job in created_jobs])
        _record_log_event(
            db,
            'library_scan_summary',
            'All enabled libraries scan completed',
            details={
                'library_names': library_names,
                'created_jobs': len(created_jobs),
                'job_ids': [job.id for job in created_jobs],
            },
        )
        return ScanResponse(created_jobs=payload)
    finally:
        worker_queue.resume_queue(reason='manual_scan')


def _promote_encode_to_download(db: Session, job, *, cancel_job_first: bool = False) -> bool:
    """If the job's library has download_enabled and Prowlarr/a client are ready,
    cancel the encoding job (optionally stopping ffmpeg first) and create a
    download (search) job for the same source file.

    Returns True if a download job was created, False otherwise.
    """
    from app.services.download_monitor_service import (
        can_attempt_download,
        create_download_job,
        download_job_exists_for_source,
        _publish_download_job,
        _wake_event,
    )

    if not job or not job.library_id or not job.source_path:
        logger.info('Promote encode->download skipped: missing job/library/source (job_id=%s)', getattr(job, 'id', None))
        return False

    library = db.query(Library).filter(Library.id == job.library_id).first()
    if library is None or library.profile is None:
        logger.info('Promote encode->download skipped: library/profile missing for job %s', job.id)
        return False

    profile = library.profile
    if not getattr(profile, 'download_enabled', False):
        logger.info('Promote encode->download skipped: download_enabled is false for library %s job %s', library.id, job.id)
        return False

    if not can_attempt_download(db):
        logger.info('Promote encode->download skipped: download route unavailable for job %s', job.id)
        return False

    active_blocker_statuses = {
        DownloadJobStatus.pending.value,
        DownloadJobStatus.searching.value,
        DownloadJobStatus.queued.value,
        DownloadJobStatus.paused.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
        DownloadJobStatus.complete.value,
    }
    reusable_statuses = {
        DownloadJobStatus.waiting_encode.value,
        DownloadJobStatus.fallback_queued.value,
        DownloadJobStatus.failed.value,
        DownloadJobStatus.timed_out.value,
    }
    existing_jobs = (
        db.query(DownloadJob)
        .filter(DownloadJob.source_file_path == job.source_path)
        .order_by(DownloadJob.id.desc())
        .all()
    )
    if any(dj.status in active_blocker_statuses for dj in existing_jobs):
        logger.info('Promote encode->download skipped: active/complete download job already exists for source %r', job.source_path)
        return False
    reusable_download_job = next((dj for dj in existing_jobs if dj.status in reusable_statuses), None)

    if cancel_job_first:
        from app.services.optimization_service import stop_active_ffmpeg, delete_workspace
        from app.services.job_service import _get_settings
        from app.services.job_timing_service import stop_encode_timing
        settings = _get_settings(db)
        stop_active_ffmpeg(job.id)
        delete_workspace(settings, job.id)
        from datetime import datetime as _dt
        job.status = 'cancelled'
        job.progress_percent = 0
        job.fps = None
        job.eta_seconds = None
        job.output_path = None
        job.cancel_requested = False
        job.completed_at = _dt.now(timezone.utc)
        stop_encode_timing(job)
        db.commit()

    if reusable_download_job is not None:
        from datetime import datetime as _dt

        reusable_download_job.status = DownloadJobStatus.pending.value
        reusable_download_job.error_message = None
        reusable_download_job.search_query = None
        reusable_download_job.release_name = None
        reusable_download_job.indexer_id = None
        reusable_download_job.indexer_name = None
        reusable_download_job.selected_release_key = None
        reusable_download_job.failed_release_keys = None
        reusable_download_job.retry_count = 0
        reusable_download_job.max_retries = 5
        reusable_download_job.download_hash = None
        reusable_download_job.client_type = None
        reusable_download_job.progress_percent = 0
        reusable_download_job.eta_seconds = None
        reusable_download_job.download_speed_bps = None
        reusable_download_job.downloaded_file_path = None
        reusable_download_job.imported_file_path = None
        reusable_download_job.encode_job_id = job.id
        reusable_download_job.download_started_at = None
        reusable_download_job.completed_at = None
        reusable_download_job.created_at = _dt.now(timezone.utc)
        db.commit()
        db.refresh(reusable_download_job)
        _publish_download_job(reusable_download_job)
        _wake_event.set()
        logger.info(
            'Promote encode->download: reused terminal download job %s for source %r',
            reusable_download_job.id,
            job.source_path,
        )
        return True

    if download_job_exists_for_source(db, job.source_path):
        logger.info('Promote encode->download skipped: download job already exists for source %r', job.source_path)
        return False

    create_download_job(db, job.source_path, library, profile)
    return True


@router.post('/jobs/{job_id}/cancel', response_model=JobResponse)
def cancel_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = cancel_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    _promote_encode_to_download(db, job)
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_cancelled',
        f'Job {response.id} was cancelled or returned to queue',
        details={'job_id': response.id, 'status': response.status},
    )
    return response


@router.post('/jobs/{job_id}/requeue', response_model=JobResponse)
def requeue_interrupted_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = requeue_interrupted_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_retried',
        f'Interrupted job {response.id} was requeued',
        details={'job_id': response.id, 'status': response.status},
    )
    return response


@router.post('/jobs/{job_id}/retry', response_model=JobResponse)
def retry_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    # Look up the job before retry_job mutates it so we can check source path.
    original = get_job(db, job_id)
    if not original:
        raise HTTPException(status_code=404, detail='Job not found')
    if _promote_encode_to_download(db, original):
        # A download job was created; return the (still failed/cancelled) encoding
        # job so the UI updates correctly without re-queuing it for encoding.
        db.refresh(original)
        response = JobResponse.from_orm_job(original)
        broker.publish_job_update(response.model_dump(), throttle_progress=False)
        _record_log_event(
            db,
            'job_retried',
            f'Job {response.id} retry was routed to download search',
            details={'job_id': response.id, 'status': response.status, 'route': 'download'},
        )
        return response
    job = retry_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_retried',
        f'Job {response.id} was retried',
        details={'job_id': response.id, 'status': response.status},
    )
    return response


@router.post('/jobs/{job_id}/pause', response_model=JobResponse)
def pause_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = pause_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_paused',
        f'Job {response.id} was paused',
        details={'job_id': response.id, 'status': response.status},
    )
    broker.publish_system_event('job_paused', job_id=response.id)
    return response


@router.post('/jobs/{job_id}/start', response_model=JobResponse)
def start_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    existing = get_job(db, job_id)
    if not existing:
        raise HTTPException(status_code=404, detail='Job not found')

    if existing.status in {'paused', 'paused_schedule'}:
        existing = resume_job(db, job_id)
        if not existing:
            raise HTTPException(status_code=404, detail='Job not found')

    started, reason = worker_queue.start_queued_job(job_id, manual=True)
    if not started:
        raise HTTPException(status_code=409, detail=reason or 'Job cannot be started')

    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')

    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_started',
        f'Job {response.id} was started',
        details={'job_id': response.id, 'status': response.status},
    )
    return response


@router.post('/jobs/{job_id}/resume', response_model=JobResponse)
def resume_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = resume_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_resumed',
        f'Job {response.id} was resumed',
        details={'job_id': response.id, 'status': response.status},
    )
    broker.publish_system_event('job_resumed', job_id=response.id)
    return response



@router.post('/jobs/abort-all', response_model=AbortAllJobsResponse)
def abort_all_jobs_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> AbortAllJobsResponse:
    jobs = abort_all_jobs(db)
    for job in jobs:
        response = JobResponse.from_orm_job(job)
        broker.publish_job_update(response.model_dump(), throttle_progress=False)
        broker.publish_system_event('job_aborted', job_id=response.id)
    _record_log_event(
        db,
        'all_jobs_aborted',
        f'Aborted {len(jobs)} active job{"s" if len(jobs) != 1 else ""}',
        severity='warning' if jobs else 'info',
        details={'aborted_job_ids': [job.id for job in jobs], 'aborted_jobs': len(jobs)},
    )
    return AbortAllJobsResponse(aborted_job_ids=[job.id for job in jobs])


@router.post('/jobs/remove-all', response_model=RemoveAllJobsResponse)
def remove_all_jobs_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> RemoveAllJobsResponse:
    removed_job_ids = remove_all_terminal_jobs(db)
    for job_id in removed_job_ids:
        broker.publish_system_event('job_removed', job_id=job_id)
    # Also remove terminal download jobs so "Clear History" clears the full
    # history list — both encode and download entries.
    _TERMINAL_DL = {
        DownloadJobStatus.complete.value,
        DownloadJobStatus.failed.value,
        DownloadJobStatus.timed_out.value,
        DownloadJobStatus.fallback_queued.value,
    }
    removed_download_job_ids = [
        row[0] for row in db.query(DownloadJob.id).filter(DownloadJob.status.in_(_TERMINAL_DL)).all()
    ]
    if removed_download_job_ids:
        db.query(DownloadJob).filter(DownloadJob.id.in_(removed_download_job_ids)).delete(synchronize_session=False)
    db.commit()
    _record_log_event(
        db,
        'history_purged',
        f'Removed {len(removed_job_ids) + len(removed_download_job_ids)} history item{"s" if (len(removed_job_ids) + len(removed_download_job_ids)) != 1 else ""}',
        details={
            'removed_job_ids': removed_job_ids,
            'removed_download_job_ids': removed_download_job_ids,
            'removed_jobs': len(removed_job_ids),
            'removed_download_jobs': len(removed_download_job_ids),
        },
    )
    return RemoveAllJobsResponse(
        removed_job_ids=removed_job_ids,
        removed_download_job_ids=removed_download_job_ids,
    )


@router.post('/jobs/cancel-all-queued', response_model=CancelAllQueuedResponse)
def cancel_all_queued_jobs_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> CancelAllQueuedResponse:
    jobs = cancel_all_queued_jobs(db)
    for job in jobs:
        response = JobResponse.from_orm_job(job)
        broker.publish_job_update(response.model_dump(), throttle_progress=False)
        broker.publish_system_event('job_cancelled', job_id=response.id)
    _record_log_event(
        db,
        'queued_jobs_cancelled',
        f'Cancelled {len(jobs)} queued job{"s" if len(jobs) != 1 else ""}',
        severity='warning' if jobs else 'info',
        details={'cancelled_job_ids': [job.id for job in jobs], 'cancelled_jobs': len(jobs)},
    )
    return CancelAllQueuedResponse(cancelled_job_ids=[job.id for job in jobs])


@router.post('/queue/clear', response_model=ClearQueueResponse)
def clear_queue_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ClearQueueResponse:
    from app.services.job_service import _get_settings
    from app.services.optimization_service import delete_workspace
    from app.services.optimization_service import stop_active_ffmpeg
    from app.models.job import Job

    queue_was_paused = worker_queue.is_queue_paused()
    if not queue_was_paused:
        worker_queue.pause_queue(reason='manual')
    settings = _get_settings(db)

    active_encode_statuses = {'queued', 'starting', 'preflight', 'running', 'paused', 'paused_schedule', 'interrupted', 'aborting'}
    encode_jobs = db.query(Job).filter(Job.status.in_(active_encode_statuses)).all()
    removed_job_ids: list[int] = []
    for job in encode_jobs:
        job.cancel_requested = True
        stop_active_ffmpeg(job.id)
        delete_workspace(settings, job.id)
        removed_job_ids.append(job.id)
        db.delete(job)

    active_download_statuses = {
        DownloadJobStatus.pending.value,
        DownloadJobStatus.searching.value,
        DownloadJobStatus.queued.value,
        DownloadJobStatus.paused.value,
        DownloadJobStatus.downloading.value,
        DownloadJobStatus.moving.value,
        DownloadJobStatus.stalled.value,
        DownloadJobStatus.importing.value,
        DownloadJobStatus.waiting_encode.value,
    }
    removed_download_job_ids = [
        row[0] for row in db.query(DownloadJob.id).filter(DownloadJob.status.in_(active_download_statuses)).all()
    ]
    if removed_download_job_ids:
        db.query(DownloadJob).filter(DownloadJob.id.in_(removed_download_job_ids)).delete(synchronize_session=False)

    db.commit()

    for job_id in removed_job_ids:
        broker.publish_system_event('job_removed', job_id=job_id)
    for download_job_id in removed_download_job_ids:
        broker.publish_system_event('download_job_removed', download_job_id=download_job_id)

    if not queue_was_paused:
        worker_queue.resume_queue(reason='manual')

    _record_log_event(
        db,
        'queue_clear_summary',
        f'Cleared {len(removed_job_ids) + len(removed_download_job_ids)} active queue item{"s" if (len(removed_job_ids) + len(removed_download_job_ids)) != 1 else ""}',
        severity='warning' if removed_job_ids or removed_download_job_ids else 'info',
        details={
            'removed_job_ids': removed_job_ids,
            'removed_download_job_ids': removed_download_job_ids,
            'removed_jobs': len(removed_job_ids),
            'removed_download_jobs': len(removed_download_job_ids),
        },
    )
    return ClearQueueResponse(
        removed_job_ids=removed_job_ids,
        removed_download_job_ids=removed_download_job_ids,
    )


@router.post('/jobs/{job_id}/abort', response_model=JobResponse)
def abort_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = abort_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_aborted',
        f'Job {response.id} was aborted',
        severity='warning',
        details={'job_id': response.id, 'status': response.status},
    )
    broker.publish_system_event('job_aborted', job_id=response.id)
    return response


@router.post('/jobs/{job_id}/discard-progress', response_model=JobResponse)
def discard_progress_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    """Wipe partial progress for a paused/running job.

    In download-enabled libraries this cancels the encoding job and creates a
    download (Prowlarr search) job instead, so the item is searched for rather
    than re-encoded from scratch.  In encoding-only libraries the original
    behaviour is preserved: discard progress and return to queue.
    """
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    if _promote_encode_to_download(db, job, cancel_job_first=True):
        db.refresh(job)
        response = JobResponse.from_orm_job(job)
        broker.publish_job_update(response.model_dump(), throttle_progress=False)
        _record_log_event(
            db,
            'job_discarded',
            f'Job {response.id} progress was discarded and routed to download search',
            severity='warning',
            details={'job_id': response.id, 'status': response.status, 'route': 'download'},
        )
        return response
    job = discard_progress_and_requeue(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    _record_log_event(
        db,
        'job_discarded',
        f'Job {response.id} progress was discarded',
        severity='warning',
        details={'job_id': response.id, 'status': response.status},
    )
    return response


@router.post('/recovery/run', response_model=RecoveryResponse)
def run_recovery_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> RecoveryResponse:
    event_log_service.record_event(
        db,
        'recovery_started',
        'Manual recovery started',
        details={'trigger': 'manual'},
    )
    summary = run_startup_recovery(db)
    for job_id in summary.get('interrupted_job_ids', []):
        broker.publish_system_event('job_interrupted', job_id=job_id)
        notification_service.enqueue_job_interrupted(job_id=job_id)
    recovered_jobs = summary.get('recovered_jobs', 0)
    requeued_jobs = summary.get('requeued_jobs', 0)
    cleaned_workspaces = summary.get('cleaned_workspaces', 0)
    broker.publish_system_event(
        'recovery_summary',
        trigger='manual',
        recovered_jobs=recovered_jobs,
        requeued_jobs=requeued_jobs,
        cleaned_workspaces=cleaned_workspaces,
    )
    notification_service.enqueue_recovery_ran(
        trigger='manual',
        recovered_jobs=recovered_jobs,
        requeued_jobs=requeued_jobs,
        cleaned_workspaces=cleaned_workspaces,
    )
    event_log_service.record_event(
        db,
        'recovery_summary',
        'Manual recovery completed',
        details={
            'trigger': 'manual',
            'recovered_jobs': recovered_jobs,
            'requeued_jobs': requeued_jobs,
            'cleaned_workspaces': cleaned_workspaces,
        },
    )
    return RecoveryResponse(recovered_jobs=recovered_jobs, requeued_jobs=requeued_jobs, cleaned_workspaces=cleaned_workspaces)


@router.get('/logs', response_model=list[EventLogResponse])
def get_event_logs(
    limit: int = Query(default=200, ge=1, le=1000),
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> list[EventLogResponse]:
    return [EventLogResponse.from_orm_log(log) for log in event_log_service.list_events(db, limit=limit)]


@router.delete('/logs', response_model=ClearEventLogsResponse)
def clear_event_logs(
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> ClearEventLogsResponse:
    deleted_logs = event_log_service.clear_events(db)
    broker.publish_system_event('logs_cleared', deleted_logs=deleted_logs)
    return ClearEventLogsResponse(deleted_logs=deleted_logs)


@router.post('/cleanup/run', response_model=CleanupResponse)
def run_cleanup_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> CleanupResponse:
    event_log_service.record_event(
        db,
        'cleanup_started',
        'Manual workspace cleanup started',
        details={'trigger': 'manual'},
    )
    summary = run_workspace_cleanup(db)
    cleaned_workspaces = summary.get('cleaned_workspaces', 0)
    broker.publish_system_event(
        'cleanup_summary',
        trigger='manual',
        cleaned_workspaces=cleaned_workspaces,
    )
    event_log_service.record_event(
        db,
        'cleanup_summary',
        'Manual workspace cleanup completed',
        details={
            'trigger': 'manual',
            'cleaned_workspaces': cleaned_workspaces,
        },
    )
    return CleanupResponse(cleaned_workspaces=cleaned_workspaces)


@router.post('/cleanup/optimized', response_model=OptimizedCleanupResponse)
def run_optimized_cleanup_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> OptimizedCleanupResponse:
    event_log_service.record_event(
        db,
        'optimized_cleanup_started',
        'Optimized output cleanup started',
    )
    deleted_files, affected_job_ids = cleanup_optimized_outputs(db)
    broker.publish_system_event(
        'optimized_cleanup_summary',
        deleted_files=deleted_files,
        affected_jobs=len(affected_job_ids),
    )
    event_log_service.record_event(
        db,
        'optimized_cleanup_summary',
        f'Optimized output cleanup removed {deleted_files} file{"s" if deleted_files != 1 else ""}',
        details={
            'deleted_files': deleted_files,
            'affected_job_ids': affected_job_ids,
            'affected_jobs': len(affected_job_ids),
        },
    )
    return OptimizedCleanupResponse(deleted_files=deleted_files, affected_job_ids=affected_job_ids)


@router.post('/cleanup/optimized/duplicates', response_model=DuplicateOptimizedCleanupResponse)
def run_duplicate_optimized_cleanup_endpoint(
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> DuplicateOptimizedCleanupResponse:
    event_log_service.record_event(
        db,
        'duplicate_optimized_cleanup_started',
        'Duplicate optimized cleanup started',
    )

    def publish_progress(progress_percent: int, message: str) -> None:
        broker.publish_system_event(
            'duplicate_optimized_cleanup_progress',
            progress_percent=max(0, min(100, int(progress_percent))),
            message=message,
        )

    deleted_files, affected_library_ids = cleanup_duplicate_optimized_outputs(db, progress_callback=publish_progress)
    affected_library_names = _library_names_for_ids(db, affected_library_ids)
    for library_id in affected_library_ids:
        plex_service.trigger_scan_after_job(library_id)
    broker.publish_system_event(
        'duplicate_optimized_cleanup_summary',
        deleted_files=deleted_files,
        affected_libraries=len(affected_library_ids),
    )
    for library_id in affected_library_ids:
        broker.publish_library_update('updated', {'id': library_id})
    event_log_service.record_event(
        db,
        'duplicate_optimized_cleanup_summary',
        f'Duplicate optimized cleanup removed {deleted_files} file{"s" if deleted_files != 1 else ""}',
        details={
            'deleted_files': deleted_files,
            'affected_library_names': affected_library_names,
            'affected_libraries': len(affected_library_ids),
        },
    )
    return DuplicateOptimizedCleanupResponse(deleted_files=deleted_files, affected_library_ids=affected_library_ids)



@router.get('/queue/status')
def queue_status_endpoint(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    return {'status': 'paused' if worker_queue.is_queue_paused() else 'running'}

@router.post('/queue/pause')
def pause_queue_endpoint(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    worker_queue.pause_queue(reason='manual')
    broker.publish_notification('queue_paused')
    broker.publish_system_event('queue_paused', reason='manual')
    with SessionLocal() as db:
        _record_log_event(
            db,
            'queue_paused',
            'Queue was paused manually',
            severity='warning',
            details={'reason': 'manual'},
        )
    return {'status': 'paused'}


@router.post('/queue/resume')
def resume_queue_endpoint(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    worker_queue.resume_queue(reason='manual')
    broker.publish_notification('queue_resumed')
    broker.publish_system_event('queue_resumed', reason='manual')
    with SessionLocal() as db:
        _record_log_event(
            db,
            'queue_resumed',
            'Queue was resumed manually',
            details={'reason': 'manual'},
        )
    return {'status': 'running'}


@router.get('/prowlarr/settings', response_model=ProwlarrSettingsResponse)
def get_prowlarr_settings(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ProwlarrSettingsResponse:
    settings = prowlarr_service.get_or_create_prowlarr_settings(db)
    return ProwlarrSettingsResponse(**prowlarr_service.settings_to_payload(settings))


@router.put('/prowlarr/settings', response_model=ProwlarrSettingsResponse)
def update_prowlarr_settings(
    payload: ProwlarrSettingsUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> ProwlarrSettingsResponse:
    settings = prowlarr_service.update_settings(db, payload.model_dump(exclude_none=True))
    return ProwlarrSettingsResponse(**prowlarr_service.settings_to_payload(settings))


@router.post('/prowlarr/test', status_code=200)
def test_prowlarr_connection(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> dict:
    settings = prowlarr_service.get_or_create_prowlarr_settings(db)
    return prowlarr_service.test_connection(settings)


@router.get('/download-client/qbittorrent', response_model=QBittorrentSettingsResponse)
def get_qbt_settings(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> QBittorrentSettingsResponse:
    s = download_client_service.get_or_create_qbt_settings(db)
    return QBittorrentSettingsResponse(**download_client_service.qbt_settings_to_payload(s))


@router.put('/download-client/qbittorrent', response_model=QBittorrentSettingsResponse)
def update_qbt_settings(
    payload: QBittorrentSettingsUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> QBittorrentSettingsResponse:
    s = download_client_service.update_qbt_settings(db, payload.model_dump(exclude_none=True))
    return QBittorrentSettingsResponse(**download_client_service.qbt_settings_to_payload(s))


@router.post('/download-client/qbittorrent/test', status_code=200)
def test_qbt_connection(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> dict:
    s = download_client_service.get_or_create_qbt_settings(db)
    return download_client_service.test_qbt_connection(s)


@router.get('/download-client/sabnzbd', response_model=SabnzbdSettingsResponse)
def get_sab_settings(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> SabnzbdSettingsResponse:
    s = download_client_service.get_or_create_sab_settings(db)
    return SabnzbdSettingsResponse(**download_client_service.sab_settings_to_payload(s))


@router.put('/download-client/sabnzbd', response_model=SabnzbdSettingsResponse)
def update_sab_settings(
    payload: SabnzbdSettingsUpdateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> SabnzbdSettingsResponse:
    s = download_client_service.update_sab_settings(db, payload.model_dump(exclude_none=True))
    return SabnzbdSettingsResponse(**download_client_service.sab_settings_to_payload(s))


@router.post('/download-client/sabnzbd/test', status_code=200)
def test_sab_connection(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> dict:
    s = download_client_service.get_or_create_sab_settings(db)
    return download_client_service.test_sab_connection(s)


@router.get('/download-jobs', response_model=list[DownloadJobResponse])
def list_download_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> list[DownloadJobResponse]:
    jobs = db.query(DownloadJob).order_by(DownloadJob.created_at.desc()).all()
    sab_positions = _sab_queue_positions_by_nzo(db)
    return [
        DownloadJobResponse.from_orm(
            dj,
            client_queue_position=_download_job_client_queue_position(dj, sab_positions),
        )
        for dj in jobs
    ]


@router.get('/download-jobs/{job_id}', response_model=DownloadJobResponse)
def get_download_job(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> DownloadJobResponse:
    dj = db.query(DownloadJob).filter(DownloadJob.id == job_id).first()
    if not dj:
        raise HTTPException(status_code=404, detail='Download job not found')
    sab_positions = _sab_queue_positions_by_nzo(db)
    return DownloadJobResponse.from_orm(
        dj,
        client_queue_position=_download_job_client_queue_position(dj, sab_positions),
    )


@router.post('/download-jobs/{job_id}/cancel', response_model=DownloadJobResponse)
def cancel_download_job(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> DownloadJobResponse:
    from datetime import datetime as _dt
    from app.services.download_monitor_service import _publish_download_job

    dj = db.query(DownloadJob).filter(DownloadJob.id == job_id).first()
    if not dj:
        raise HTTPException(status_code=404, detail='Download job not found')
    terminal = {DownloadJobStatus.complete.value, DownloadJobStatus.fallback_queued.value, DownloadJobStatus.failed.value, DownloadJobStatus.timed_out.value}
    if dj.status in terminal:
        raise HTTPException(status_code=409, detail='Download job is already in a terminal state')

    # If the job is currently tracked in an external download client, remove it
    # there first so reset really stops the in-progress transfer.
    if dj.download_hash and dj.client_type == 'qbittorrent':
        qbt = download_client_service.get_or_create_qbt_settings(db)
        if qbt.enabled and not download_client_service.remove_qbt_torrent(qbt, dj.download_hash, delete_files=True):
            raise HTTPException(status_code=502, detail='Failed to remove torrent from qBittorrent')
    elif dj.download_hash and dj.client_type == 'sabnzbd':
        sab = download_client_service.get_or_create_sab_settings(db)
        if sab.enabled and not download_client_service.remove_sab_job(sab, dj.download_hash, delete_files=True):
            raise HTTPException(status_code=502, detail='Failed to remove item from SABnzbd')

    dj.status = DownloadJobStatus.pending.value
    dj.error_message = None
    dj.search_query = None
    dj.release_name = None
    dj.indexer_id = None
    dj.indexer_name = None
    dj.selected_release_key = None
    dj.failed_release_keys = None
    dj.retry_count = 0
    dj.max_retries = 5
    dj.download_hash = None
    dj.client_type = None
    dj.progress_percent = 0
    dj.eta_seconds = None
    dj.download_speed_bps = None
    dj.downloaded_file_path = None
    dj.imported_file_path = None
    dj.encode_job_id = None
    dj.download_started_at = None
    dj.completed_at = None
    dj.created_at = _dt.now(timezone.utc)
    db.commit()
    db.refresh(dj)
    _publish_download_job(dj)
    _record_log_event(
        db,
        'download_job_reset',
        f'Download job {dj.id} was reset',
        severity='warning',
        details={'download_job_id': dj.id, 'status': dj.status, 'source_file_path': dj.source_file_path},
    )
    broker.publish_system_event('download_job_reset', download_job_id=dj.id)
    return DownloadJobResponse.from_orm(dj)


@router.delete('/download-jobs', status_code=204)
def delete_all_download_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> None:
    """Delete all download jobs regardless of state."""
    removed_ids = [row[0] for row in db.query(DownloadJob.id).all()]
    if removed_ids:
        db.query(DownloadJob).filter(DownloadJob.id.in_(removed_ids)).delete(synchronize_session=False)
    db.commit()
    for download_job_id in removed_ids:
        broker.publish_system_event('download_job_removed', download_job_id=download_job_id)
    _record_log_event(
        db,
        'download_job_removed',
        f'Removed {len(removed_ids)} download job{"s" if len(removed_ids) != 1 else ""}',
        severity='warning' if removed_ids else 'info',
        details={'removed_download_job_ids': removed_ids, 'removed_download_jobs': len(removed_ids)},
    )


@router.delete('/download-jobs/{job_id}', status_code=204)
def delete_download_job(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> None:
    dj = db.query(DownloadJob).filter(DownloadJob.id == job_id).first()
    if dj is None:
        raise HTTPException(status_code=404, detail='Download job not found')
    db.delete(dj)
    db.commit()
    _record_log_event(
        db,
        'download_job_removed',
        f'Download job {job_id} was removed',
        details={'download_job_id': job_id},
    )
    broker.publish_system_event('download_job_removed', download_job_id=job_id)


@router.post('/download-jobs/{job_id}/retry', response_model=DownloadJobResponse)
def retry_download_job(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> DownloadJobResponse:
    dj = db.query(DownloadJob).filter(DownloadJob.id == job_id).first()
    if not dj:
        raise HTTPException(status_code=404, detail='Download job not found')
    retryable = {DownloadJobStatus.failed.value, DownloadJobStatus.timed_out.value, DownloadJobStatus.stalled.value}
    if dj.status not in retryable:
        raise HTTPException(status_code=409, detail='Only failed, timed_out, or stalled download jobs can be retried')
    from datetime import datetime as _dt
    dj.status = DownloadJobStatus.pending.value
    dj.error_message = None
    dj.search_query = None
    dj.release_name = None
    dj.indexer_id = None
    dj.indexer_name = None
    dj.selected_release_key = None
    dj.failed_release_keys = None
    dj.retry_count = 0
    dj.max_retries = 5
    dj.download_hash = None
    dj.client_type = None
    dj.progress_percent = 0
    dj.eta_seconds = None
    dj.download_speed_bps = None
    dj.completed_at = None
    dj.encode_job_id = None
    dj.created_at = _dt.now(timezone.utc)
    db.commit()
    db.refresh(dj)
    from app.services.download_monitor_service import _publish_download_job
    _publish_download_job(dj)
    _record_log_event(
        db,
        'download_job_retried',
        f'Download job {dj.id} was retried',
        details={'download_job_id': dj.id, 'status': dj.status, 'source_file_path': dj.source_file_path},
    )
    broker.publish_system_event('download_job_retried', download_job_id=dj.id)
    return DownloadJobResponse.from_orm(dj)


@router.get('/download-queue/status')
def get_download_queue_status(_: None = Depends(require_ui_auth)) -> dict:
    from app.services.download_monitor_service import (
        get_download_queue_stop_reason,
        is_download_queue_stopped,
    )
    return {
        'stopped': is_download_queue_stopped(),
        'reason': get_download_queue_stop_reason(),
    }


@router.post('/download-queue/resume')
def resume_download_queue_endpoint(_: None = Depends(require_ui_auth)) -> dict:
    from app.services.download_monitor_service import resume_download_queue
    resume_download_queue()
    with SessionLocal() as db:
        _record_log_event(
            db,
            'queue_resumed',
            'Download queue was resumed',
            details={'queue': 'download'},
        )
    broker.publish_system_event('queue_resumed', queue='download')
    return {'status': 'resumed'}


@router.get('/auth/ws-token')
def get_ws_token(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    # Backwards-compatible endpoint for older frontends; websocket auth now uses
    # the same session cookie as the REST API.
    raise HTTPException(status_code=404, detail='WebSocket token not required')


@router.websocket('/ws')
async def websocket_events(websocket: WebSocket) -> None:
    try:
        _ws_user_or_unauthorized(websocket)
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    subscription = broker.subscribe()

    try:
        broker.publish_notification('websocket_client_connected')
        while True:
            keepalive_seconds = float(os.getenv('OPTIMIZARR_WEBSOCKET_KEEPALIVE_SECONDS', '15'))
            event = await next_message(subscription, timeout_seconds=keepalive_seconds)
            if event is None:
                await websocket.send_json({'type': 'notification', 'data': {'message': 'keepalive', 'level': 'debug'}})
                continue
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(subscription.client_id)
