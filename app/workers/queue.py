from datetime import datetime
from queue import Empty, Queue
from threading import Event, Thread

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.settings import Settings
from app.services.optimization_service import optimize_video

job_queue: Queue[int] = Queue()
stop_event = Event()


def enqueue_job(job_id: int) -> None:
    job_queue.put(job_id)


def _process(job_id: int, db: Session) -> None:
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        return

    settings = db.query(Settings).first()
    if not settings:
        settings = Settings()
        db.add(settings)
        db.commit()

    job.status = 'running'
    job.progress_percent = 0
    db.commit()

    def on_progress(update: dict[str, float | int | None]) -> None:
        job.progress_percent = int(update.get('progress_percent') or 0)
        job.fps = update.get('fps') if isinstance(update.get('fps'), float) else None
        eta_seconds = update.get('eta_seconds')
        job.eta_seconds = int(eta_seconds) if isinstance(eta_seconds, int) else None
        db.commit()

    metrics = optimize_video(job.input_path, settings, progress_callback=on_progress)

    job.status = metrics.status
    job.output_path = metrics.output_path
    job.fps = metrics.fps
    if metrics.status in {'complete', 'skipped', 'failed'}:
        job.completed_at = datetime.utcnow()
    if metrics.status == 'complete':
        job.progress_percent = 100
        job.eta_seconds = 0
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
