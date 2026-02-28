from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
from pathlib import Path
import threading
import time

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.models.settings import DiscoveryMethodEnum, Settings
from app.services.job_service import create_job, job_exists_for_source
from app.services.optimization_service import is_hdr_video, probe_video_height
from app.services.realtime_service import broker
from app.workers.queue import is_queue_paused

try:
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
except ImportError:  # pragma: no cover - dependency-driven behavior
    FileSystemEventHandler = object  # type: ignore[assignment]
    Observer = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)
MEDIA_SUFFIXES = {'.mkv', '.mp4'}
STABILITY_WINDOW_SECONDS = 60


@dataclass
class PendingFileState:
    last_size: int | None = None
    size_unchanged_since: float | None = None


class _DiscoveryEventHandler(FileSystemEventHandler):
    def __init__(self, manager: 'DiscoveryManager') -> None:
        self._manager = manager

    def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        self._manager.record_event(Path(event.src_path))

    def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        self._manager.record_event(Path(event.src_path))

    def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        self._manager.record_event(Path(event.dest_path))


class DiscoveryManager:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._observer = None
        self._watched_paths: set[str] = set()
        self._pending_events: dict[str, PendingFileState] = {}
        self._pending_lock = threading.Lock()
        self._next_interval_scan_at = datetime.utcnow()
        self._scan_requested = threading.Event()

    def request_scan(self) -> None:
        """Signal the worker to run a discovery scan on the next iteration."""
        self._scan_requested.set()

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name='discovery-worker', daemon=True)
        self._thread.start()
        return self._thread

    def stop(self) -> None:
        self._stop_event.set()
        self._stop_observer()

    def record_event(self, path: Path) -> None:
        if path.suffix.lower() not in MEDIA_SUFFIXES:
            return

        with self._pending_lock:
            self._pending_events[str(path)] = PendingFileState()

    def _run(self) -> None:
        logger.info('Auto-discovery worker started')
        while not self._stop_event.is_set():
            if is_queue_paused():
                time.sleep(2)
                continue

            db = SessionLocal()
            try:
                settings = _get_or_create_settings(db)
                if not settings.auto_discovery_enabled:
                    self._stop_observer()
                    time.sleep(2)
                    continue

                enabled_libraries = _get_enabled_libraries(db)
                if settings.discovery_method == DiscoveryMethodEnum.interval:
                    self._stop_observer()
                    self._run_interval_scan_if_due(db, enabled_libraries, settings.discovery_interval_minutes)
                else:
                    self._ensure_watcher(enabled_libraries)
                    self._drain_stable_events(db, enabled_libraries)
            except Exception:
                logger.exception('Auto-discovery worker iteration failed')
            finally:
                db.close()

            time.sleep(2)

        logger.info('Auto-discovery worker stopped')

    def _run_interval_scan_if_due(self, db: Session, libraries: list[Library], interval_minutes: int) -> None:
        now = datetime.utcnow()
        immediate = self._scan_requested.is_set()
        self._scan_requested.clear()
        if not immediate and now < self._next_interval_scan_at:
            return

        reason = 'immediate (job complete)' if immediate else 'interval'
        logger.info('Running auto-discovery scan across %s enabled libraries (%s)', len(libraries), reason)
        queued_jobs = scan_enabled_libraries(db, libraries)
        logger.info('Auto-discovery scan complete; queued %s job(s)', len(queued_jobs))
        self._next_interval_scan_at = now + timedelta(minutes=max(interval_minutes, 1))

        # After each scan, reconcile any download jobs whose torrent completed
        # in qBittorrent while the app was idle.  This runs before the normal
        # queue picks up fresh work so already-finished downloads are imported
        # immediately rather than going back through the search pipeline.
        from app.services.download_monitor_service import run_scan_recovery
        recovery = run_scan_recovery(db)
        if recovery.get('imported', 0):
            logger.info('Post-scan recovery imported %s completed download(s)', recovery['imported'])

    def _ensure_watcher(self, libraries: list[Library]) -> None:
        target_paths = {library.path for library in libraries if Path(library.path).exists()}

        if Observer is None:
            logger.warning('watchdog is unavailable; watcher discovery method cannot be started')
            return

        if self._observer is None:
            self._observer = Observer()
            self._observer.start()
            logger.info('Started filesystem watcher for auto-discovery')

        if target_paths == self._watched_paths:
            return

        self._stop_observer()
        if not target_paths:
            return

        self._observer = Observer()
        handler = _DiscoveryEventHandler(self)
        for path in sorted(target_paths):
            self._observer.schedule(handler, path, recursive=True)
        self._observer.start()
        self._watched_paths = target_paths
        logger.info('Auto-discovery watcher monitoring %s library path(s)', len(self._watched_paths))

    def _stop_observer(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        self._watched_paths = set()

    def _drain_stable_events(self, db: Session, libraries: list[Library]) -> None:
        enabled_by_path = {library.path: library for library in libraries}
        queued_count = 0
        now_monotonic = time.monotonic()

        with self._pending_lock:
            pending_paths = list(self._pending_events.keys())

        for raw_path in pending_paths:
            candidate = Path(raw_path)
            if not candidate.exists() or not candidate.is_file():
                with self._pending_lock:
                    self._pending_events.pop(raw_path, None)
                continue

            state = self._pending_events.get(raw_path)
            if state is None:
                continue

            size = candidate.stat().st_size
            if state.last_size != size:
                state.last_size = size
                state.size_unchanged_since = now_monotonic
                continue

            if state.size_unchanged_since is None:
                state.size_unchanged_since = now_monotonic
                continue

            if now_monotonic - state.size_unchanged_since < STABILITY_WINDOW_SECONDS:
                continue

            queued_job = queue_path_if_eligible(db, candidate, enabled_by_path)
            with self._pending_lock:
                self._pending_events.pop(raw_path, None)
            if queued_job is not None:
                queued_count += 1

        if queued_count:
            logger.info('Auto-discovery watcher queued %s job(s) from stable file events', queued_count)


def _get_or_create_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if settings is None:
        settings = Settings()
        db.add(settings)
        db.commit()
        db.refresh(settings)
    return settings


def _get_enabled_libraries(db: Session) -> list[Library]:
    return db.query(Library).filter(Library.enabled.is_(True)).order_by(Library.id.asc()).all()


def _get_or_create_library_profile(db: Session, library: Library) -> LibraryProfile:
    if library.profile:
        return library.profile

    profile = LibraryProfile(library_id=library.id)
    db.add(profile)
    db.commit()
    db.refresh(profile)
    return profile


def scan_library(db: Session, library: Library, include_disabled: bool = False) -> list:
    if not include_disabled and not library.enabled:
        return []

    profile = _get_or_create_library_profile(db, library)
    created_jobs = []
    library_path = Path(library.path)

    for media_file in library_path.rglob('*'):
        if not media_file.is_file() or media_file.suffix.lower() not in MEDIA_SUFFIXES:
            continue

        job = _queue_file_if_eligible(db, media_file, library, profile)
        if job is not None:
            created_jobs.append(job)

    return created_jobs


def scan_enabled_libraries(db: Session, libraries: list[Library] | None = None) -> list:
    target_libraries = libraries or _get_enabled_libraries(db)
    created_jobs = []
    for library in target_libraries:
        created_jobs.extend(scan_library(db, library))
    return created_jobs


def queue_path_if_eligible(db: Session, media_file: Path, enabled_by_path: dict[str, Library]):
    library = _match_library(media_file, enabled_by_path)
    if library is None:
        return None

    profile = _get_or_create_library_profile(db, library)
    return _queue_file_if_eligible(db, media_file, library, profile)


def _match_library(media_file: Path, enabled_by_path: dict[str, Library]) -> Library | None:
    matched_library = None
    matched_length = -1
    for library_path, library in enabled_by_path.items():
        try:
            media_file.relative_to(Path(library_path))
        except ValueError:
            continue

        if len(library_path) > matched_length:
            matched_library = library
            matched_length = len(library_path)

    return matched_library


def _cancel_queued_encode_for_source(db: Session, source_path: str, library_id: int) -> None:
    """Cancel any queued or paused encoding job for the given source so it
    doesn't race against an incoming download job."""
    existing = (
        db.query(Job)
        .filter(
            Job.input_path == source_path,
            Job.library_id == library_id,
            Job.status.in_(['queued', 'paused']),
        )
        .first()
    )
    if existing:
        from datetime import datetime as _dt
        existing.status = 'cancelled'
        existing.completed_at = _dt.utcnow()
        db.commit()


def _queue_file_if_eligible(db: Session, media_file: Path, library: Library, profile: LibraryProfile):
    if media_file.stem.endswith(profile.output_suffix):
        return None

    container = str(profile.container.value if hasattr(profile.container, 'value') else profile.container).lower().strip('.')
    output_path = media_file.with_name(f'{media_file.stem}{profile.output_suffix}.{container}')
    if output_path.exists():
        return None

    # Broader sibling check: a downloaded release may have a different stem
    # (e.g. different release group) so the exact-path check above misses it.
    # Scan siblings that share the same title prefix (everything before the
    # first '[') and end with the output suffix — catches cases like
    # "...-MainFrame-1080p.mkv" sitting beside "...-BEN.mp4".
    bracket_pos = media_file.stem.find('[')
    if bracket_pos > 0:
        title_prefix = media_file.stem[:bracket_pos].rstrip(' ._-')
        if title_prefix:
            for sibling in media_file.parent.iterdir():
                if (sibling != media_file
                        and sibling.stem.startswith(title_prefix)
                        and sibling.stem.endswith(profile.output_suffix)
                        and sibling.suffix.lower() == f'.{container}'):
                    return None

    source_path = str(media_file)
    download_enabled = getattr(profile, 'download_enabled', False)

    if download_enabled:
        from app.services.download_monitor_service import (
            can_attempt_download,
            create_download_job,
            download_job_exists_for_source,
        )
        # If a download job is already active for this file, nothing more to do.
        if download_job_exists_for_source(db, source_path):
            return None
        # Mirror the encoding-path guard: skip if a non-retryable encoding job
        # (complete, queued, paused, or running) already exists for this source.
        # Prevents redundant downloads when a file was previously encoded or is
        # currently being encoded.
        if job_exists_for_source(db, source_path, library_id=library.id):
            return None
        # In download-enabled mode, enqueue the download job as soon as the
        # source is eligible and no conflicting rows exist. This avoids silent
        # drops when ffprobe/metadata checks fail for unusual source files.
        if can_attempt_download(db):
            _cancel_queued_encode_for_source(db, source_path, library.id)
            create_download_job(db, source_path, library, profile)
            return None
    else:
        # Encoding-only mode: skip the (expensive) ffprobe if a job already exists.
        if job_exists_for_source(db, source_path, library_id=library.id):
            return None

    source_resolution = probe_video_height(source_path)
    source_is_hdr = is_hdr_video(source_path)

    if profile.hdr_only:
        if not source_is_hdr:
            return None
    else:
        if source_resolution is None:
            return None

        minimum_source_resolution = int(getattr(profile, 'minimum_source_resolution', 2160) or 2160)
        if source_resolution < minimum_source_resolution:
            return None

        if source_resolution <= profile.target_resolution:
            return None

    if download_enabled:
        # Prowlarr / client not ready – fall through to queuing an encoding job.
        if job_exists_for_source(db, source_path, library_id=library.id):
            return None

    job = create_job(
        db,
        source_path,
        library_id=library.id,
        profile=profile,
        source_resolution=source_resolution,
        source_is_hdr=source_is_hdr,
        status='paused' if not library.enabled else 'queued',
    )
    broker.publish_job_update(
        {
            'id': job.id,
            'status': job.status,
            'source_path': job.source_path,
            'output_path': job.output_path,
            'retry_count': job.retry_count,
            'cancel_requested': job.cancel_requested,
            'progress_percent': job.progress_percent,
            'fps': job.fps,
            'eta_seconds': job.eta_seconds,
            'encoder_used': job.encoder_used,
            'codec_used': job.codec_used,
            'hwaccel_used': job.hwaccel_used,
            'used_fallback': job.used_fallback,
            'fallback_reason': job.fallback_reason,
            'error_message': job.error_message,
            'source_resolution': job.source_resolution,
            'source_is_hdr': job.source_is_hdr,
            'library_id': job.library_id,
            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
        },
        throttle_progress=False,
    )
    broker.publish_system_event('discovery_job_queued', source_path=source_path)
    return job


_manager = DiscoveryManager()


def start_discovery_worker() -> threading.Thread:
    return _manager.start()


def stop_discovery_worker() -> None:
    _manager.stop()


def trigger_immediate_scan() -> None:
    """Request an out-of-schedule discovery scan.

    Called by the download monitor after a job completes so that the next
    eligible file is picked up promptly instead of waiting for the next
    configured interval.
    """
    _manager.request_scan()
