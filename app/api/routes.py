from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.services.job_service import create_job, get_job, list_jobs
from app.workers.queue import enqueue_job

router = APIRouter()


class JobCreateRequest(BaseModel):
    source_path: str = Field(..., examples=['/media/in/movie.mkv'])


class JobResponse(BaseModel):
    id: int
    status: str
    source_path: str
    output_path: str | None = None

    @classmethod
    def from_orm_job(cls, job):
        return cls(
            id=job.id,
            status=job.status,
            source_path=job.source_path,
            output_path=job.output_path,
        )


@router.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}


@router.post('/api/jobs', response_model=JobResponse, status_code=201)
def create_optimization_job(payload: JobCreateRequest, db: Session = Depends(get_db)) -> JobResponse:
    job = create_job(db, payload.source_path)
    enqueue_job(job.id)
    return JobResponse.from_orm_job(job)


@router.get('/api/jobs', response_model=list[JobResponse])
def get_jobs(db: Session = Depends(get_db)) -> list[JobResponse]:
    return [JobResponse.from_orm_job(job) for job in list_jobs(db)]


@router.get('/api/jobs/{job_id}', response_model=JobResponse)
def get_job_by_id(job_id: int, db: Session = Depends(get_db)) -> JobResponse:
    job = get_job(db, job_id)
    if not job:
        raise HTTPException(status_code=404, detail='Job not found')
    return JobResponse.from_orm_job(job)
