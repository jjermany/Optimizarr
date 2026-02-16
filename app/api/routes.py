from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.job_service import cancel_job, create_job, get_job, list_jobs, retry_job

router = APIRouter()


class JobCreateRequest(BaseModel):
    source_path: str = Field(..., examples=['/media/in/movie.mkv'])


class JobScanRequest(BaseModel):
    source_paths: list[str] = Field(default_factory=list)


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
def scan_jobs(payload: JobScanRequest, db: Session = Depends(get_db)) -> ScanResponse:
    jobs = [create_job(db, source_path) for source_path in payload.source_paths]
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
