from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.core.database import Base, engine
from app.workers.queue import start_worker, stop_worker


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    worker_thread = start_worker()
    try:
        yield
    finally:
        stop_worker()
        worker_thread.join(timeout=1)


app = FastAPI(title='plex-optimizer', lifespan=lifespan)
app.include_router(router)
