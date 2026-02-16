from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI

from app.api.routes import router
from app.core.database import init_db
from app.core.logging_config import configure_logging
from app.services.optimization_service import refresh_encoder_cache
from app.workers.queue import start_worker, stop_worker

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_logging()
    logger.info('Starting Optimizarr application')
    init_db()
    refresh_encoder_cache()
    worker_thread = start_worker()
    try:
        yield
    finally:
        logger.info('Shutting down Optimizarr application')
        stop_worker()
        worker_thread.join(timeout=5)
        logger.info('Optimizarr shutdown complete')


app = FastAPI(title='plex-optimizer', lifespan=lifespan)
app.include_router(router)
