from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path
from threading import Event, Thread

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.core.database import SessionLocal, init_db
from app.core.logging_config import configure_logging
from app.services.discovery_service import start_discovery_worker, stop_discovery_worker, trigger_immediate_scan
from app.services.download_monitor_service import register_job_complete_callback, run_download_startup_recovery, start_download_monitor, stop_download_monitor
from app.services import auth_service, event_log_service, notification_service
from app.services.notification_service import start_notification_worker, stop_notification_worker
from app.services.optimization_service import refresh_encoder_cache
from app.services.recovery_service import run_startup_recovery, run_workspace_cleanup
from app.services.realtime_service import broker
from app.models.settings import Settings
from app.workers.queue import pause_queue, start_worker, stop_worker

logger = logging.getLogger(__name__)
CLEANUP_INTERVAL_SECONDS = 6 * 60 * 60

# Built frontend lives at /app/static when running inside the container
FRONTEND_DIST = Path(__file__).resolve().parent.parent / 'static'


def _is_test_runtime() -> bool:
    return 'PYTEST_CURRENT_TEST' in os.environ


def _cleanup_loop(stop_event: Event) -> None:
    logger.info('Workspace cleanup worker started')
    while not stop_event.wait(CLEANUP_INTERVAL_SECONDS):
        with SessionLocal() as db:
            summary = run_workspace_cleanup(db)
            cleaned_workspaces = summary.get('cleaned_workspaces', 0)
            event_log_service.record_event(
                db,
                'cleanup_summary',
                'Scheduled workspace cleanup completed',
                details={
                    'trigger': 'scheduled',
                    'cleaned_workspaces': cleaned_workspaces,
                },
            )
        logger.info(
            'Workspace cleanup completed; cleaned_workspaces=%s',
            cleaned_workspaces,
        )
        broker.publish_system_event(
            'cleanup_summary',
            trigger='scheduled',
            cleaned_workspaces=cleaned_workspaces,
        )

    logger.info('Workspace cleanup worker stopped')


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    logger.info('Starting Optimizarr application')
    init_db()
    with SessionLocal() as db:
        auth_service.purge_expired_sessions(db)
        persisted_settings = db.query(Settings).first()
        initial_queue_paused = bool(persisted_settings and persisted_settings.queue_paused)
        summary = run_startup_recovery(db)
        cleanup_summary = run_workspace_cleanup(db)
        download_recovery_summary = run_download_startup_recovery(db)
    if initial_queue_paused:
        pause_queue(reason='manual')
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
        download_imported=download_recovery_summary.get('imported', 0),
        download_reset=download_recovery_summary.get('reset_to_searching', 0),
        download_linked=download_recovery_summary.get('linked_jobs', 0),
        download_adopted=download_recovery_summary.get('adopted_queue_jobs', 0),
    )
    with SessionLocal() as db:
        event_log_service.record_event(
            db,
            'recovery_summary',
            'Startup recovery completed',
            details={
                'trigger': 'startup',
                'recovered_jobs': recovered_jobs,
                'requeued_jobs': requeued_jobs,
                'cleaned_workspaces': cleaned_workspaces,
                'download_imported': download_recovery_summary.get('imported', 0),
                'download_reset': download_recovery_summary.get('reset_to_searching', 0),
                'download_linked': download_recovery_summary.get('linked_jobs', 0),
                'download_adopted': download_recovery_summary.get('adopted_queue_jobs', 0),
            },
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
    with SessionLocal() as db:
        event_log_service.record_event(
            db,
            'cleanup_summary',
            'Startup workspace cleanup completed',
            details={
                'trigger': 'startup',
                'cleaned_workspaces': cleanup_summary.get('cleaned_workspaces', 0),
            },
        )
    # After a download job completes/fails, immediately trigger a discovery scan
    # so the next eligible file is picked up without waiting for the interval.
    register_job_complete_callback(trigger_immediate_scan)

    if _is_test_runtime():
        worker_thread = start_worker()
        try:
            yield
        finally:
            logger.info('Shutting down Optimizarr application')
            stop_worker()
            broker.stop()
            worker_thread.join(timeout=5)
            logger.info('Optimizarr shutdown complete')
        return

    cleanup_stop_event = Event()
    cleanup_thread = Thread(target=_cleanup_loop, args=(cleanup_stop_event,), name='cleanup-worker', daemon=True)
    cleanup_thread.start()
    notification_thread = start_notification_worker()
    worker_thread = start_worker()
    discovery_thread = start_discovery_worker()
    download_monitor_thread = start_download_monitor()
    try:
        yield
    finally:
        logger.info('Shutting down Optimizarr application')
        stop_worker()
        stop_notification_worker()
        stop_discovery_worker()
        stop_download_monitor()
        cleanup_stop_event.set()
        broker.stop()
        worker_thread.join(timeout=5)
        notification_thread.join(timeout=5)
        discovery_thread.join(timeout=5)
        download_monitor_thread.join(timeout=5)
        cleanup_thread.join(timeout=5)
        logger.info('Optimizarr shutdown complete')


app = FastAPI(title='plex-optimizer', lifespan=lifespan)
app.include_router(router)
app.include_router(router, prefix='/api')

# Serve the built React SPA for all non-API paths.
# html=True makes StaticFiles return index.html for any path not found on disk,
# which is the standard behaviour needed for client-side SPA routing.
if FRONTEND_DIST.exists():
    app.mount('/', StaticFiles(directory=str(FRONTEND_DIST), html=True), name='frontend')
