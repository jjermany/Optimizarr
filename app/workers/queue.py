from queue import Empty, Queue
from threading import Event, Thread
import time

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.job import OptimizationJob

job_queue: Queue[int] = Queue()
stop_event = Event()


def enqueue_job(job_id: int) -> None:
    job_queue.put(job_id)


def _process(job_id: int, db: Session) -> None:
    job = db.query(OptimizationJob).filter(OptimizationJob.id == job_id).first()
    if not job:
        return

    job.status = 'processing'
    db.commit()

    # Placeholder for optimization pipeline.
    time.sleep(0.1)

    job.status = 'completed'
    db.commit()


def worker_loop() -> None:
    while not stop_event.is_set():
        try:
            job_id = job_queue.get(timeout=0.2)
        except Empty:
            continue

        db = SessionLocal()
        try:
            _process(job_id, db)
        finally:
            db.close()
            job_queue.task_done()


def start_worker() -> Thread:
    stop_event.clear()
    thread = Thread(target=worker_loop, name='optimizer-worker', daemon=True)
    thread.start()
    return thread


def stop_worker() -> None:
    stop_event.set()
