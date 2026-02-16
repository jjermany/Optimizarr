from contextlib import asynccontextmanager
import logging
from threading import Event, Thread

from fastapi import FastAPI

from app.api.routes import router
from app.core.database import SessionLocal, init_db
from app.core.logging_config import configure_logging
from app.services.discovery_service import start_discovery_worker, stop_discovery_worker
from app.services import notification_service
from app.services.notification_service import start_notification_worker, stop_notification_worker
from app.services.optimization_service import refresh_encoder_cache
from app.services.recovery_service import run_startup_recovery, run_workspace_cleanup
from app.services.realtime_service import broker
from app.workers.queue import start_worker, stop_worker

logger = logging.getLogger(__name__)
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60


def _cleanup_loop(stop_event: Event) -> None:
    logger.info('Workspace cleanup worker started')
    while not stop_event.wait(CLEANUP_INTERVAL_SECONDS):
        with SessionLocal() as db:
            summary = run_workspace_cleanup(db)
        logger.info(
            'Workspace cleanup completed; cleaned_workspaces=%s',
            summary.get('cleaned_workspaces', 0),
        )
        broker.publish_system_event(
            'cleanup_summary',
            trigger='scheduled',
            cleaned_workspaces=summary.get('cleaned_workspaces', 0),
        )

    logger.info('Workspace cleanup worker stopped')


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    logger.info('Starting Optimizarr application')
    init_db()
    with SessionLocal() as db:
        summary = run_startup_recovery(db)
        cleanup_summary = run_workspace_cleanup(db)
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
    broker.publish_system_event(
        'cleanup_summary',
        trigger='startup',
        cleaned_workspaces=cleanup_summary.get('cleaned_workspaces', 0),
    )
    cleanup_stop_event = Event()
    cleanup_thread = Thread(target=_cleanup_loop, args=(cleanup_stop_event,), name='cleanup-worker', daemon=True)
    cleanup_thread.start()
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
        cleanup_stop_event.set()
        broker.stop()
        worker_thread.join(timeout=5)
        notification_thread.join(timeout=5)
        discovery_thread.join(timeout=5)
        cleanup_thread.join(timeout=5)
        logger.info('Optimizarr shutdown complete')


app = FastAPI(title='plex-optimizer', lifespan=lifespan)
app.include_router(router)
app.include_router(router, prefix='/api')
