from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.settings import Settings
from app.services.job_service import cancel_job, create_job, get_job, list_jobs, retry_job
from app.services.optimization_service import is_hdr_video

router = APIRouter()
MEDIA_ROOT = Path('/media')


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


@router.post('/jobs', response_model=JobResponse, status_code=201)
@router.post('/api/jobs', response_model=JobResponse, status_code=201, include_in_schema=False)
def create_optimization_job(payload: JobCreateRequest, db: Session = Depends(get_db)) -> JobResponse:
    job = create_job(db, payload.source_path)
    return JobResponse.from_orm_job(job)


@router.get('/jobs', response_model=list[JobResponse])
@router.get('/api/jobs', response_model=list[JobResponse], include_in_schema=False)
def get_jobs(db: Session = Depends(get_db)) -> list[JobResponse]:
    return [JobResponse.from_orm_job(job) for job in list_jobs(db)]


@router.get('/jobs/{job_id}', response_model=JobResponse)
@router.get('/api/jobs/{job_id}', response_model=JobResponse, include_in_schema=False)
def get_job_by_id(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return JobResponse.from_orm_job(job)


@router.post('/jobs/scan', response_model=ScanResponse)
def scan_jobs(db: Session = Depends(get_db)) -> ScanResponse:
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
def cancel_job_endpoint(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = cancel_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return JobResponse.from_orm_job(job)


@router.post('/jobs/{job_id}/retry', response_model=JobResponse)
def retry_job_endpoint(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = retry_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return JobResponse.from_orm_job(job)
