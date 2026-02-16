import os
from pathlib import Path
import secrets

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status
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
)
from app.models.settings import Settings
from app.services import notification_service
from app.services.job_service import cancel_job, create_job, get_job, job_exists_for_source, list_jobs, retry_job
from app.services.monitoring_service import get_system_metrics
from app.services.optimization_service import is_hdr_video, probe_video_height
from app.services.realtime_service import broker, expected_ws_token, next_message

router = APIRouter()
MEDIA_ROOT = Path('/media')
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


class NotificationTriggerSettings(BaseModel):
    job_failed: bool = True
    job_complete: bool = False
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
    codec: CodecEnum
    container: ContainerEnum
    audio_mode: AudioModeEnum
    bitrate_mode: BitrateModeEnum
    bitrate_mbps: int | None
    crf: int | None
    speed_preset: SpeedPresetEnum
    hdr_only: bool
    max_workers: int
    schedule_start_hour: int
    schedule_end_hour: int
    output_suffix: str
    av1_fallback_codec: CodecEnum

    @classmethod
    def from_orm_profile(cls, profile: LibraryProfile):
        return cls(
            target_resolution=profile.target_resolution,
            codec=profile.codec,
            container=profile.container,
            audio_mode=profile.audio_mode,
            bitrate_mode=profile.bitrate_mode,
            bitrate_mbps=profile.bitrate_mbps,
            crf=profile.crf,
            speed_preset=profile.speed_preset,
            hdr_only=profile.hdr_only,
            max_workers=profile.max_workers,
            schedule_start_hour=profile.schedule_start_hour,
            schedule_end_hour=profile.schedule_end_hour,
            output_suffix=profile.output_suffix,
            av1_fallback_codec=profile.av1_fallback_codec,
        )


class LibraryProfileUpdateRequest(BaseModel):
    target_resolution: int | None = Field(default=None, ge=1)
    codec: CodecEnum | None = None
    container: ContainerEnum | None = None
    audio_mode: AudioModeEnum | None = None
    bitrate_mode: BitrateModeEnum | None = None
    bitrate_mbps: int | None = Field(default=None, ge=1)
    crf: int | None = Field(default=None, ge=1)
    speed_preset: SpeedPresetEnum | None = None
    hdr_only: bool | None = None
    max_workers: int | None = Field(default=None, ge=1)
    schedule_start_hour: int | None = Field(default=None, ge=0, le=23)
    schedule_end_hour: int | None = Field(default=None, ge=0, le=23)
    output_suffix: str | None = None
    av1_fallback_codec: CodecEnum | None = None

    @field_validator('av1_fallback_codec')
    @classmethod
    def fallback_codec_must_not_be_av1(cls, value: CodecEnum | None) -> CodecEnum | None:
        if value == CodecEnum.av1:
            raise ValueError('av1_fallback_codec must be hevc or h264')
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
    for field_name, value in updates.items():
        setattr(profile, field_name, value)

    db.commit()
    db.refresh(profile)
    broker.publish_library_update('profile_updated', {'library_id': library_id})
    return LibraryProfileResponse.from_orm_profile(profile)


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


def _scan_library(db: Session, library: Library) -> list:
    profile = _get_or_create_library_profile(db, library)
    created_jobs = []
    library_path = Path(library.path)

    for media_file in library_path.rglob('*'):
        if not media_file.is_file() or media_file.suffix.lower() not in {'.mkv', '.mp4'}:
            continue
        if media_file.stem.endswith(profile.output_suffix):
            continue

        output_path = media_file.with_name(f'{media_file.stem}{profile.output_suffix}.{media_file.suffix.lstrip(".")}')
        if output_path.exists():
            continue

        source_path = str(media_file)
        if job_exists_for_source(db, source_path, library_id=library.id):
            continue

        height = probe_video_height(source_path)
        if height is None or height < 2000:
            continue

        if profile.hdr_only and not is_hdr_video(source_path):
            continue

        created_jobs.append(create_job(db, source_path, library_id=library.id, profile=profile))

    return created_jobs


@router.post('/libraries/{library_id}/scan', response_model=ScanResponse)
def scan_library_jobs(library_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ScanResponse:
    library = _get_library_or_404(db, library_id)
    jobs = _scan_library(db, library)
    payload = [JobResponse.from_orm_job(job) for job in jobs]
    notification_service.register_scan_batch([job.id for job in jobs])
    for item in payload:
        broker.publish_job_update(item.model_dump(), throttle_progress=False)
    return ScanResponse(created_jobs=payload)


@router.post('/scan', response_model=ScanResponse)
def scan_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ScanResponse:
    created_jobs = []
    libraries = db.query(Library).filter(Library.enabled.is_(True)).order_by(Library.id.asc()).all()
    for library in libraries:
        created_jobs.extend(_scan_library(db, library))

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
