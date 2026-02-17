import os
from pathlib import Path
import secrets

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.library import (
    AudioModeEnum,
    BitrateModeEnum,
    CodecEnum,
    ContainerEnum,
    Library,
    LibraryProfile,
    SpeedPresetEnum,
    SchedulePolicyEnum,
    OutputConflictPolicyEnum,
    PreferredEncoderEnum,
)
from app.models.settings import DiscoveryMethodEnum, Settings
from app.services import notification_service
from app.services.job_service import (
    abort_all_jobs,
    abort_job,
    cancel_job,
    cleanup_optimized_outputs,
    create_job,
    delete_job,
    get_job,
    list_jobs,
    pause_job,
    remove_all_terminal_jobs,
    resume_job,
    retry_job,
)
from app.services.monitoring_service import get_system_metrics
from app.services.realtime_service import broker, expected_ws_token, next_message
from app.services.discovery_service import scan_enabled_libraries, scan_library
from app.services.recovery_service import run_startup_recovery, run_workspace_cleanup
from app.workers import queue as worker_queue

router = APIRouter()
MEDIA_ROOT = Path('/media')
BRANDING_ROOTS = (
    MEDIA_ROOT / 'Logo',
    Path(__file__).resolve().parents[2] / 'media' / 'Logo',
)
APP_VERSION = os.getenv('OPTIMIZARR_VERSION', '0.1.0')
security = HTTPBasic(auto_error=False)


def require_ui_auth(credentials: HTTPBasicCredentials | None = Depends(security)) -> None:
    username = os.getenv('OPTIMIZARR_UI_USERNAME')
    password = os.getenv('OPTIMIZARR_UI_PASSWORD')

    if not username and not password:
        return

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required',
            headers={'WWW-Authenticate': 'Basic'},
        )

    username_ok = secrets.compare_digest(credentials.username, username or '')
    password_ok = secrets.compare_digest(credentials.password, password or '')
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid credentials',
            headers={'WWW-Authenticate': 'Basic'},
        )


def _ws_token_or_unauthorized(token: str | None) -> None:
    required_token = expected_ws_token()
    if required_token is None:
        return

    if not token or not secrets.compare_digest(token, required_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='Invalid websocket token')


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


class JobCreateRequest(BaseModel):
    source_path: str = Field(..., examples=['/media/in/movie.mkv'])


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
    schedule_start_hour: int
    schedule_end_hour: int
    global_quiet_enabled: bool
    global_quiet_start_hour: int
    global_quiet_end_hour: int
    process_hdr_only: bool
    history_retention_days: int
    auto_discovery_enabled: bool
    discovery_method: DiscoveryMethodEnum
    discovery_interval_minutes: int
    workspace_root: str
    requeue_interrupted_jobs: bool
    cleanup_workspaces_on_startup: bool
    min_free_gb: int

    @classmethod
    def from_orm_settings(cls, settings: Settings):
        return cls(
            enable_optimizer=settings.enable_optimizer,
            target_resolution=settings.target_resolution,
            bitrate_mbps=settings.bitrate_mbps,
            keep_original=settings.keep_original,
            max_workers=settings.max_workers,
            scan_interval_minutes=settings.scan_interval_minutes,
            schedule_start_hour=settings.schedule_start_hour,
            schedule_end_hour=settings.schedule_end_hour,
            global_quiet_enabled=settings.global_quiet_enabled,
            global_quiet_start_hour=settings.global_quiet_start_hour,
            global_quiet_end_hour=settings.global_quiet_end_hour,
            process_hdr_only=settings.process_hdr_only,
            history_retention_days=settings.history_retention_days,
            auto_discovery_enabled=settings.auto_discovery_enabled,
            discovery_method=settings.discovery_method,
            discovery_interval_minutes=settings.discovery_interval_minutes,
            workspace_root=settings.workspace_root,
            requeue_interrupted_jobs=settings.requeue_interrupted_jobs,
            cleanup_workspaces_on_startup=settings.cleanup_workspaces_on_startup,
            min_free_gb=settings.min_free_gb,
        )


class SettingsUpdateRequest(BaseModel):
    enable_optimizer: bool | None = None
    target_resolution: int | None = Field(default=None, ge=1)
    bitrate_mbps: int | None = Field(default=None, ge=1)
    keep_original: bool | None = None
    max_workers: int | None = Field(default=None, ge=1)
    scan_interval_minutes: int | None = Field(default=None, ge=1)
    schedule_start_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_end_hour: int | None = Field(default=None, ge=0, le=23)
    global_quiet_enabled: bool | None = None
    global_quiet_start_hour: int | None = Field(default=None, ge=0, le=23)
    global_quiet_end_hour: int | None = Field(default=None, ge=0, le=23)
    process_hdr_only: bool | None = None
    history_retention_days: int | None = Field(default=None, ge=1)
    auto_discovery_enabled: bool | None = None
    discovery_method: DiscoveryMethodEnum | None = None
    discovery_interval_minutes: int | None = Field(default=None, ge=1)
    workspace_root: str | None = Field(default=None, min_length=1)
    requeue_interrupted_jobs: bool | None = None
    cleanup_workspaces_on_startup: bool | None = None
    min_free_gb: int | None = Field(default=None, ge=1)



class RecoveryResponse(BaseModel):
    recovered_jobs: int
    requeued_jobs: int
    cleaned_workspaces: int


class CleanupResponse(BaseModel):
    cleaned_workspaces: int


class OptimizedCleanupResponse(BaseModel):
    deleted_files: int
    affected_job_ids: list[int]


class AbortAllJobsResponse(BaseModel):
    aborted_job_ids: list[int]


class RemoveAllJobsResponse(BaseModel):
    removed_job_ids: list[int]

class NotificationTriggerSettings(BaseModel):
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


class LibraryBaseRequest(BaseModel):
    name: str = Field(..., min_length=1)
    path: str = Field(..., min_length=1)
    enabled: bool = True

    @field_validator('path')
    @classmethod
    def validate_media_path(cls, value: str) -> str:
        candidate = Path(value)
        if not candidate.is_absolute():
            raise ValueError('path must be an absolute path')

        media_root = MEDIA_ROOT.resolve()
        resolved_candidate = candidate.resolve(strict=False)

        if not resolved_candidate.is_relative_to(media_root):
            raise ValueError('path must be under /media')
        if not candidate.exists():
            raise ValueError('path must exist')

        return str(candidate)


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

    @classmethod
    def from_orm_library(cls, library: Library):
        return cls(id=library.id, name=library.name, path=library.path, enabled=library.enabled)


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
    max_workers: int
    schedule_enabled: bool
    schedule_start_hour: int
    schedule_end_hour: int
    schedule_policy: SchedulePolicyEnum
    output_suffix: str
    output_conflict_policy: OutputConflictPolicyEnum
    av1_fallback_codec: CodecEnum
    preferred_video_encoder: PreferredEncoderEnum

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
            max_workers=profile.max_workers,
            schedule_enabled=profile.schedule_enabled,
            schedule_start_hour=profile.schedule_start_hour,
            schedule_end_hour=profile.schedule_end_hour,
            schedule_policy=profile.schedule_policy,
            output_suffix=profile.output_suffix,
            output_conflict_policy=profile.output_conflict_policy,
            av1_fallback_codec=profile.av1_fallback_codec,
            preferred_video_encoder=profile.preferred_video_encoder,
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
    max_workers: int | None = Field(default=None, ge=1)
    schedule_enabled: bool | None = None
    schedule_start_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_end_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_policy: SchedulePolicyEnum | None = None
    output_suffix: str | None = None
    output_conflict_policy: OutputConflictPolicyEnum | None = None
    av1_fallback_codec: CodecEnum | None = None
    preferred_video_encoder: PreferredEncoderEnum | None = None

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
            CodecEnum.h264: {PreferredEncoderEnum.h264_qsv, PreferredEncoderEnum.libx264},
            CodecEnum.hevc: {PreferredEncoderEnum.hevc_qsv, PreferredEncoderEnum.libx265},
            CodecEnum.av1: {PreferredEncoderEnum.av1_qsv, PreferredEncoderEnum.libsvtav1},
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


def _output_path_for(source_path: Path) -> Path:
    return source_path.with_name(f'{source_path.stem}-1080p.mkv')


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


def _validate_resolution_constraints(target_resolution: int, minimum_source_resolution: int, hdr_only: bool) -> None:
    if not hdr_only and minimum_source_resolution <= target_resolution:
        raise HTTPException(
            status_code=422,
            detail='minimum_source_resolution must be greater than target_resolution',
        )


@router.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}


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
    _validate_resolution_constraints(target_resolution, minimum_source_resolution, hdr_only)

    for field_name, value in updates.items():
        setattr(profile, field_name, value)

    db.commit()
    db.refresh(profile)
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


@router.post('/jobs', response_model=JobResponse, status_code=201)
@router.post('/api/jobs', response_model=JobResponse, status_code=201, include_in_schema=False)
def create_optimization_job(
    payload: JobCreateRequest,
    _: None = Depends(require_ui_auth),
    db: Session = Depends(get_db),
) -> JobResponse:
    job = create_job(db, payload.source_path)
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


@router.post('/libraries/{library_id}/scan', response_model=ScanResponse)
def scan_library_jobs(library_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ScanResponse:
    library = _get_library_or_404(db, library_id)
    worker_queue.pause_queue(reason='manual_scan')
    jobs = scan_library(db, library)
    payload = [JobResponse.from_orm_job(job) for job in jobs]
    notification_service.register_scan_batch([job.id for job in jobs], library_name=library.name)
    for item in payload:
        broker.publish_job_update(item.model_dump(), throttle_progress=False)
    return ScanResponse(created_jobs=payload)


@router.post('/scan', response_model=ScanResponse)
def scan_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ScanResponse:
    worker_queue.pause_queue(reason='manual_scan')
    created_jobs = scan_enabled_libraries(db)

    payload = [JobResponse.from_orm_job(job) for job in created_jobs]
    notification_service.register_scan_batch([job.id for job in created_jobs])
    for item in payload:
        broker.publish_job_update(item.model_dump(), throttle_progress=False)
    return ScanResponse(created_jobs=payload)


@router.post('/jobs/{job_id}/cancel', response_model=JobResponse)
def cancel_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = cancel_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    return response


@router.post('/jobs/{job_id}/retry', response_model=JobResponse)
def retry_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = retry_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    return response


@router.post('/jobs/{job_id}/pause', response_model=JobResponse)
def pause_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = pause_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    broker.publish_system_event('job_paused', job_id=response.id)
    return response


@router.post('/jobs/{job_id}/start', response_model=JobResponse)
def start_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    started = worker_queue.start_queued_job(job_id)
    if not started:
        raise HTTPException(status_code=409, detail='Job cannot be started')

    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')

    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    return response


@router.post('/jobs/{job_id}/resume', response_model=JobResponse)
def resume_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = resume_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    broker.publish_system_event('job_resumed', job_id=response.id)
    return response



@router.post('/jobs/abort-all', response_model=AbortAllJobsResponse)
def abort_all_jobs_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> AbortAllJobsResponse:
    jobs = abort_all_jobs(db)
    for job in jobs:
        response = JobResponse.from_orm_job(job)
        broker.publish_job_update(response.model_dump(), throttle_progress=False)
        broker.publish_system_event('job_aborted', job_id=response.id)
    return AbortAllJobsResponse(aborted_job_ids=[job.id for job in jobs])


@router.post('/jobs/remove-all', response_model=RemoveAllJobsResponse)
def remove_all_jobs_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> RemoveAllJobsResponse:
    removed_job_ids = remove_all_terminal_jobs(db)
    for job_id in removed_job_ids:
        broker.publish_system_event('job_removed', job_id=job_id)
    return RemoveAllJobsResponse(removed_job_ids=removed_job_ids)


@router.post('/jobs/{job_id}/abort', response_model=JobResponse)
def abort_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = abort_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    response = JobResponse.from_orm_job(job)
    broker.publish_job_update(response.model_dump(), throttle_progress=False)
    broker.publish_system_event('job_aborted', job_id=response.id)
    return response



@router.post('/recovery/run', response_model=RecoveryResponse)
def run_recovery_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> RecoveryResponse:
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
    return RecoveryResponse(recovered_jobs=recovered_jobs, requeued_jobs=requeued_jobs, cleaned_workspaces=cleaned_workspaces)



@router.post('/cleanup/run', response_model=CleanupResponse)
def run_cleanup_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> CleanupResponse:
    summary = run_workspace_cleanup(db)
    cleaned_workspaces = summary.get('cleaned_workspaces', 0)
    broker.publish_system_event(
        'cleanup_summary',
        trigger='manual',
        cleaned_workspaces=cleaned_workspaces,
    )
    return CleanupResponse(cleaned_workspaces=cleaned_workspaces)


@router.post('/cleanup/optimized', response_model=OptimizedCleanupResponse)
def run_optimized_cleanup_endpoint(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> OptimizedCleanupResponse:
    deleted_files, affected_job_ids = cleanup_optimized_outputs(db)
    broker.publish_system_event(
        'optimized_cleanup_summary',
        deleted_files=deleted_files,
        affected_jobs=len(affected_job_ids),
    )
    return OptimizedCleanupResponse(deleted_files=deleted_files, affected_job_ids=affected_job_ids)

@router.post('/queue/pause')
def pause_queue_endpoint(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    worker_queue.pause_queue(reason='manual')
    broker.publish_notification('queue_paused')
    return {'status': 'paused'}


@router.post('/queue/resume')
def resume_queue_endpoint(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    worker_queue.resume_queue(reason='manual')
    broker.publish_notification('queue_resumed')
    return {'status': 'running'}


@router.get('/auth/ws-token')
def get_ws_token(_: None = Depends(require_ui_auth)) -> dict[str, str]:
    token = expected_ws_token()
    if token is None:
        raise HTTPException(status_code=404, detail='WebSocket token not required')
    return {'token': token}


@router.websocket('/ws')
async def websocket_events(websocket: WebSocket) -> None:
    try:
        _ws_token_or_unauthorized(websocket.query_params.get('token'))
    except HTTPException:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    subscription = broker.subscribe()

    try:
        broker.publish_notification('websocket_client_connected')
        while True:
            event = await next_message(subscription)
            if event is None:
                await websocket.send_json({'type': 'notification', 'data': {'message': 'keepalive', 'level': 'debug'}})
                continue
            await websocket.send_json(event)
    except WebSocketDisconnect:
        pass
    finally:
        broker.unsubscribe(subscription.client_id)
