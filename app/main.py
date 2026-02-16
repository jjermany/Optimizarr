from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.api.routes import router
from app.core.database import SessionLocal, init_db
from app.core.logging_config import configure_logging
from app.services.discovery_service import start_discovery_worker, stop_discovery_worker
from app.services import notification_service
from app.services.notification_service import start_notification_worker, stop_notification_worker
from app.services.optimization_service import refresh_encoder_cache
from app.services.recovery_service import run_startup_recovery
from app.services.realtime_service import broker
from app.workers.queue import start_worker, stop_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    logger.info('Starting Optimizarr application')
    init_db()
    with SessionLocal() as db:
        summary = run_startup_recovery(db)
    refresh_encoder_cache()
    broker.start()
    for job_id in summary.get('interrupted_job_ids', []):
        broker.publish_system_event('job_interrupted', job_id=job_id)
        notification_service.enqueue_job_interrupted(job_id=job_id)
    recovered_jobs = summary.get('recovered_jobs', 0)
    requeued_jobs = summary.get('requeued_jobs', 0)
    cleaned_workspaces = summary.get('cleaned_workspaces', 0)
    broker.publish_system_event(
        'recovery_summary',
        trigger='startup',
        recovered_jobs=recovered_jobs,
        requeued_jobs=requeued_jobs,
        cleaned_workspaces=cleaned_workspaces,
    )
    notification_service.enqueue_recovery_ran(
        trigger='startup',
        recovered_jobs=recovered_jobs,
        requeued_jobs=requeued_jobs,
        cleaned_workspaces=cleaned_workspaces,
    )
    notification_thread = start_notification_worker()
    worker_thread = start_worker()
    discovery_thread = start_discovery_worker()
    try:
        yield
    finally:
        logger.info('Shutting down Optimizarr application')
        stop_worker()
        stop_notification_worker()
        stop_discovery_worker()
        broker.stop()
        worker_thread.join(timeout=5)
        notification_thread.join(timeout=5)
        discovery_thread.join(timeout=5)
        logger.info('Optimizarr shutdown complete')


app = FastAPI(title='plex-optimizer', lifespan=lifespan)
app.include_router(router)
app.include_router(router, prefix='/api')
