import os
from pathlib import Path
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.settings import Settings
from app.services.job_service import cancel_job, create_job, get_job, list_jobs, retry_job
from app.services.monitoring_service import get_system_metrics
from app.services.optimization_service import is_hdr_video

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


class JobCreateRequest(BaseModel):
    source_path: str = Field(..., examples=['/media/in/movie.mkv'])


class JobResponse(BaseModel):
    id: int
    status: str
    source_path: str
    output_path: str | None = None
    retry_count: int
    cancel_requested: bool

    @classmethod
    def from_orm_job(cls, job):
        return cls(
            id=job.id,
            status=job.status,
            source_path=job.source_path,
            output_path=job.output_path,
            retry_count=job.retry_count,
            cancel_requested=job.cancel_requested,
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
    process_hdr_only: bool | None = None
    history_retention_days: int | None = Field(default=None, ge=1)


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
    return SettingsResponse.from_orm_settings(settings)


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
    return JobResponse.from_orm_job(job)


@router.get('/jobs', response_model=list[JobResponse])
@router.get('/api/jobs', response_model=list[JobResponse], include_in_schema=False)
def get_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> list[JobResponse]:
    return [JobResponse.from_orm_job(job) for job in list_jobs(db)]


@router.get('/jobs/{job_id}', response_model=JobResponse)
@router.get('/api/jobs/{job_id}', response_model=JobResponse, include_in_schema=False)
def get_job_by_id(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return JobResponse.from_orm_job(job)


@router.post('/jobs/scan', response_model=ScanResponse)
def scan_jobs(_: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> ScanResponse:
    settings = _get_settings(db)
    created_jobs = []

    for media_file in MEDIA_ROOT.rglob('*'):
        if not media_file.is_file() or media_file.suffix.lower() != '.mkv':
            continue

        if media_file.stem.endswith('-1080p'):
            continue

        output_path = _output_path_for(media_file)
        if output_path.exists():
            continue

        if settings.process_hdr_only and not is_hdr_video(str(media_file)):
            continue

        created_jobs.append(create_job(db, str(media_file)))

    jobs = created_jobs
    return ScanResponse(created_jobs=[JobResponse.from_orm_job(job) for job in jobs])


@router.post('/jobs/{job_id}/cancel', response_model=JobResponse)
def cancel_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = cancel_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return JobResponse.from_orm_job(job)


@router.post('/jobs/{job_id}/retry', response_model=JobResponse)
def retry_job_endpoint(job_id: int, _: None = Depends(require_ui_auth), db: Session = Depends(get_db)) -> JobResponse:
    job = retry_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return JobResponse.from_orm_job(job)
