from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
from pathlib import Path
import re
import threading
import time

from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.models.discovery_index import DiscoveryFileIndex
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.models.settings import DiscoveryMethodEnum, Settings, clamp_scan_probe_workers
from app.services.job_service import create_job, job_exists_for_source, has_completed_job_for_identity, media_identity_key
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
_NEAR_4K_HEIGHT_FLOOR = 2000
_active_library_scans: dict[int, int] = {}
_active_library_scans_lock = threading.Lock()


@dataclass
class PendingFileState:
    last_size: int | None = None
    size_unchanged_since: float | None = None


@dataclass(frozen=True)
class MediaFileState:
    path: Path
    source_path: str
    file_size_bytes: int
    file_mtime_ns: int


@dataclass(frozen=True)
class DiscoveryProbeCandidate:
    media_file: Path
    source_path: str


@dataclass(frozen=True)
class DiscoveryProbeResult:
    source_resolution: int | None
    source_is_hdr: bool | None
    has_existing_target_sdr_sibling: bool = False


@dataclass(frozen=True)
class DiscoveryProbePlan:
    hdr_required: bool
    minimum_source_resolution: int
    target_resolution: int


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
        self._next_interval_scan_at = datetime.now(UTC)
        self._scan_requested = threading.Event()
        self._watcher_recovery_completed = False

    def request_scan(self) -> None:
        """Signal the worker to run a discovery scan on the next iteration."""
        self._scan_requested.set()

    def start(self) -> threading.Thread:
        if self._thread and self._thread.is_alive():
            return self._thread

        self._stop_event.clear()
        self._scan_requested.clear()
        self._watcher_recovery_completed = False
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
                    self._watcher_recovery_completed = False
                    self._stop_observer()
                    self._run_interval_scan_if_due(db, enabled_libraries, settings.discovery_interval_minutes)
                else:
                    self._ensure_watcher(enabled_libraries)
                    self._run_watcher_recovery_if_needed(db, enabled_libraries)
                    self._drain_stable_events(db, enabled_libraries)
            except Exception:
                logger.exception('Auto-discovery worker iteration failed')
            finally:
                db.close()

            time.sleep(2)

        logger.info('Auto-discovery worker stopped')

    def _run_interval_scan_if_due(self, db: Session, libraries: list[Library], interval_minutes: int) -> None:
        now = datetime.now(UTC)
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

    def _run_watcher_recovery_if_needed(self, db: Session, libraries: list[Library]) -> None:
        reason: str | None = None
        if not self._watcher_recovery_completed:
            reason = 'startup'
        elif self._scan_requested.is_set():
            reason = 'requested'

        if reason is None:
            return

        self._scan_requested.clear()
        summary = run_watcher_recovery(db, libraries, reason=reason)
        self._watcher_recovery_completed = True
        logger.info(
            'Watcher recovery complete (%s): changed=%s queued=%s indexed=%s pruned=%s',
            reason,
            summary.get('changed_files', 0),
            summary.get('queued_jobs', 0),
            summary.get('indexed_files', 0),
            summary.get('pruned_files', 0),
        )


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


def is_library_scan_active(library_id: int) -> bool:
    with _active_library_scans_lock:
        return _active_library_scans.get(int(library_id), 0) > 0


class _LibraryScanTracker:
    def __init__(self, library: Library) -> None:
        self._library = library

    def __enter__(self) -> None:
        should_publish = False
        with _active_library_scans_lock:
            library_id = int(self._library.id)
            previous = _active_library_scans.get(library_id, 0)
            _active_library_scans[library_id] = previous + 1
            should_publish = previous == 0
        if should_publish:
            broker.publish_system_event('library_scan_started', library_id=self._library.id)

    def __exit__(self, exc_type, exc, tb) -> None:
        should_publish = False
        with _active_library_scans_lock:
            library_id = int(self._library.id)
            current = _active_library_scans.get(library_id, 0)
            if current <= 1:
                _active_library_scans.pop(library_id, None)
                should_publish = True
            else:
                _active_library_scans[library_id] = current - 1
        if should_publish:
            broker.publish_system_event('library_scan_completed', library_id=self._library.id)


def scan_library(db: Session, library: Library, include_disabled: bool = False) -> list:
    if not include_disabled and not library.enabled:
        return []
    with _LibraryScanTracker(library):
        profile = _get_or_create_library_profile(db, library)
        created_jobs = []
        library_path = Path(library.path)
        settings = _get_or_create_settings(db)
        workers = _effective_scan_probe_workers(getattr(settings, 'scan_probe_workers', 1))
        probe_plan = _build_probe_plan(profile)
        candidates: list[DiscoveryProbeCandidate] = []
        media_states = list(_iter_media_file_states(library_path))

        for media_state in media_states:
            candidate = _prepare_probe_candidate(db, media_state.path, library, profile)
            if candidate is not None:
                candidates.append(candidate)

        if workers == 1:
            probed_candidates = [
                (candidate, _probe_candidate_metadata(candidate, probe_plan))
                for candidate in candidates
            ]
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='scan-probe') as executor:
                probe_results = list(executor.map(lambda candidate: _probe_candidate_metadata(candidate, probe_plan), candidates))
            probed_candidates = list(zip(candidates, probe_results))

        for candidate, probe_result in probed_candidates:
            job = _finalize_candidate_with_probe(db, candidate, probe_result, library, profile)
            if job is not None:
                created_jobs.append(job)

        _sync_library_discovery_index(db, library, profile, media_states)
        return created_jobs


def scan_enabled_libraries(db: Session, libraries: list[Library] | None = None) -> list:
    target_libraries = libraries or _get_enabled_libraries(db)
    created_jobs = []
    for library in target_libraries:
        created_jobs.extend(scan_library(db, library))
    return created_jobs


def run_watcher_recovery(
    db: Session,
    libraries: list[Library] | None = None,
    *,
    reason: str = 'startup',
) -> dict[str, int]:
    target_libraries = libraries or _get_enabled_libraries(db)
    changed_files = 0
    queued_jobs = 0
    indexed_files = 0
    pruned_files = 0

    for library in target_libraries:
        profile = _get_or_create_library_profile(db, library)
        signature = _discovery_signature(profile)
        existing_rows = (
            db.query(DiscoveryFileIndex)
            .filter(DiscoveryFileIndex.library_id == library.id)
            .all()
        )
        existing_by_path = {row.source_path: row for row in existing_rows}

        media_states = list(_iter_media_file_states(Path(library.path)))
        changed_states: list[MediaFileState] = []
        for state in media_states:
            row = existing_by_path.get(state.source_path)
            if row is None:
                changed_states.append(state)
                continue
            if (
                int(row.file_size_bytes or 0) != state.file_size_bytes
                or int(row.file_mtime_ns or 0) != state.file_mtime_ns
                or str(row.discovery_signature or '') != signature
            ):
                changed_states.append(state)

        _, pruned = _sync_library_discovery_index(db, library, profile, media_states)
        pruned_files += pruned
        indexed_files += len(media_states)
        changed_files += len(changed_states)

        for state in changed_states:
            job = _queue_file_if_eligible(db, state.path, library, profile)
            if job is not None:
                queued_jobs += 1

    logger.info(
        'Watcher recovery (%s): libraries=%s changed=%s queued=%s indexed=%s pruned=%s',
        reason,
        len(target_libraries),
        changed_files,
        queued_jobs,
        indexed_files,
        pruned_files,
    )
    return {
        'changed_files': changed_files,
        'queued_jobs': queued_jobs,
        'indexed_files': indexed_files,
        'pruned_files': pruned_files,
    }


def queue_path_if_eligible(db: Session, media_file: Path, enabled_by_path: dict[str, Library]):
    library = _match_library(media_file, enabled_by_path)
    if library is None:
        return None

    profile = _get_or_create_library_profile(db, library)
    job = _queue_file_if_eligible(db, media_file, library, profile)
    media_state = _media_file_state_from_path(media_file)
    if media_state is not None:
        _upsert_discovery_index_entry(db, library, profile, media_state)
    return job


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
    """Remove any queued or paused placeholder encode job for the given source
    so it doesn't briefly appear alongside the routed download job."""
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
        placeholder_job_id = existing.id
        db.delete(existing)
        db.commit()
        broker.publish_system_event('job_removed', job_id=placeholder_job_id)


def _blocking_encode_job_exists_for_download_route(db: Session, source_path: str, library_id: int) -> bool:
    """Return True when an existing encode job should block download routing.

    Queued/paused encode rows are treated as stale placeholders in download mode
    and are intentionally ignored so they can be cancelled/replaced.
    """
    candidates = (
        db.query(Job)
        .filter(
            Job.input_path == source_path,
            Job.library_id == library_id,
            ~Job.status.in_(['failed', 'skipped', 'cancelled']),
        )
        .all()
    )
    for job in candidates:
        if job.status in {'queued', 'paused'}:
            continue
        if job.status != 'complete':
            return True
        output = Path(job.output_path) if job.output_path else None
        if output and output.exists():
            return True
    return False


def _release_matches_target_resolution_label(path: Path, target_resolution: int) -> bool:
    stem_lower = path.stem.lower()
    target = int(target_resolution)
    if f'{target}p' in stem_lower:
        return True
    if target == 1080 and re.search(r'\b2k\b', stem_lower):
        return True
    if target == 2160 and '4k' in stem_lower:
        return True
    if re.search(rf'\b\d{{3,4}}x{target}\b', stem_lower):
        return True
    if re.search(rf'\b{target}i\b', stem_lower):
        return True
    return False


def _meets_minimum_source_resolution(media_file: Path, source_resolution: int | None, minimum_source_resolution: int) -> bool:
    """Accept strict min resolution, plus near-4K/labelled-4K variants for UHD libraries."""
    if source_resolution is not None and source_resolution >= minimum_source_resolution:
        return True

    if int(minimum_source_resolution) >= 2160:
        # Real-world UHD releases can report slightly below 2160 after crop/matte.
        if source_resolution is not None and source_resolution >= _NEAR_4K_HEIGHT_FLOOR:
            return True
        if _release_matches_target_resolution_label(media_file, 2160):
            return True

    return False


def _candidate_title_prefix(path: Path) -> str:
    stem = path.stem
    # Prefer content before metadata blocks like [...] or {...}
    for marker in ('[', '{'):
        idx = stem.find(marker)
        if idx > 0:
            return stem[:idx].rstrip(' ._-')
    # Otherwise trim from year onward if available.
    spaced = re.sub(r'[._-]+', ' ', stem).strip()
    year_match = re.search(r'\b(19|20)\d{2}\b', spaced)
    if year_match and year_match.start() > 0:
        return re.sub(r'[\s()\[\]{}._-]+$', '', spaced[:year_match.start()]).strip()
    return stem


def _normalized_title_key(text: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', str(text or '').lower())


def _is_same_release_family(source_file: Path, candidate: Path) -> bool:
    source_prefix = _normalized_title_key(_candidate_title_prefix(source_file))
    candidate_prefix = _normalized_title_key(_candidate_title_prefix(candidate))
    if not source_prefix or not candidate_prefix:
        return False
    return source_prefix in candidate_prefix or candidate_prefix in source_prefix


def _has_existing_target_sdr_sibling(media_file: Path, target_resolution: int) -> bool:
    """Return True when a sibling file already satisfies the target SDR output."""
    for sibling in media_file.parent.iterdir():
        if sibling == media_file or not sibling.is_file() or sibling.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if not _is_same_release_family(media_file, sibling):
            continue

        matches_target = _release_matches_target_resolution_label(sibling, target_resolution)
        if not matches_target:
            sibling_height = probe_video_height(str(sibling))
            matches_target = sibling_height is not None and abs(int(sibling_height) - target_resolution) <= 32
        if not matches_target:
            continue

        if is_hdr_video(str(sibling)):
            continue
        return True
    return False


def _has_existing_target_identity_sibling(media_file: Path, profile: LibraryProfile) -> bool:
    """Return True when another file for the same media already satisfies target output."""
    identity_key = media_identity_key(str(media_file))
    if not identity_key:
        return False

    target_resolution = int(getattr(profile, 'target_resolution', 1080) or 1080)
    output_suffix = str(getattr(profile, 'output_suffix', '') or '')
    for sibling in media_file.parent.iterdir():
        if sibling == media_file or not sibling.is_file() or sibling.suffix.lower() not in MEDIA_SUFFIXES:
            continue
        if media_identity_key(str(sibling)) != identity_key:
            continue
        if output_suffix and sibling.stem.endswith(output_suffix):
            return True
        if _release_matches_target_resolution_label(sibling, target_resolution):
            return True
    return False


def _effective_scan_probe_workers(value: int | None) -> int:
    return clamp_scan_probe_workers(value)


def _build_probe_plan(profile: LibraryProfile) -> DiscoveryProbePlan:
    return DiscoveryProbePlan(
        hdr_required=bool(getattr(profile, 'hdr_only', False) or getattr(profile, 'tone_map_hdr', False)),
        minimum_source_resolution=int(getattr(profile, 'minimum_source_resolution', 2160) or 2160),
        target_resolution=int(getattr(profile, 'target_resolution', 1080) or 1080),
    )


def _discovery_signature(profile: LibraryProfile) -> str:
    payload = {
        'download_enabled': bool(getattr(profile, 'download_enabled', False)),
        'output_suffix': str(getattr(profile, 'output_suffix', '') or ''),
        'container': str(getattr(profile, 'container', '') or ''),
        'target_resolution': int(getattr(profile, 'target_resolution', 1080) or 1080),
        'minimum_source_resolution': int(getattr(profile, 'minimum_source_resolution', 2160) or 2160),
        'hdr_only': bool(getattr(profile, 'hdr_only', False)),
        'tone_map_hdr': bool(getattr(profile, 'tone_map_hdr', False)),
    }
    return json.dumps(payload, sort_keys=True, separators=(',', ':'))


def _iter_media_file_states(library_path: Path):
    if not library_path.exists():
        return

    stack = [library_path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                            continue
                        if not entry.is_file(follow_symlinks=False):
                            continue
                        path = Path(entry.path)
                        if path.suffix.lower() not in MEDIA_SUFFIXES:
                            continue
                        stat = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    yield MediaFileState(
                        path=path,
                        source_path=str(path),
                        file_size_bytes=int(getattr(stat, 'st_size', 0) or 0),
                        file_mtime_ns=int(getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1_000_000_000)) or 0),
                    )
        except OSError:
            continue


def _media_file_state_from_path(media_file: Path) -> MediaFileState | None:
    try:
        stat = media_file.stat()
    except OSError:
        return None
    return MediaFileState(
        path=media_file,
        source_path=str(media_file),
        file_size_bytes=int(getattr(stat, 'st_size', 0) or 0),
        file_mtime_ns=int(getattr(stat, 'st_mtime_ns', int(stat.st_mtime * 1_000_000_000)) or 0),
    )


def _sync_library_discovery_index(
    db: Session,
    library: Library,
    profile: LibraryProfile,
    media_states: list[MediaFileState],
) -> tuple[dict[str, DiscoveryFileIndex], int]:
    signature = _discovery_signature(profile)
    existing_rows = (
        db.query(DiscoveryFileIndex)
        .filter(DiscoveryFileIndex.library_id == library.id)
        .all()
    )
    existing_by_path = {row.source_path: row for row in existing_rows}
    seen_paths = {state.source_path for state in media_states}
    now = datetime.now(UTC)

    for state in media_states:
        row = existing_by_path.get(state.source_path)
        if row is None:
            row = DiscoveryFileIndex(
                library_id=library.id,
                source_path=state.source_path,
            )
            db.add(row)
            existing_by_path[state.source_path] = row
        row.file_size_bytes = state.file_size_bytes
        row.file_mtime_ns = state.file_mtime_ns
        row.discovery_signature = signature
        row.last_seen_at = now

    pruned = 0
    for stale_path, row in list(existing_by_path.items()):
        if stale_path in seen_paths:
            continue
        db.delete(row)
        existing_by_path.pop(stale_path, None)
        pruned += 1

    db.commit()
    return existing_by_path, pruned


def _upsert_discovery_index_entry(
    db: Session,
    library: Library,
    profile: LibraryProfile,
    media_state: MediaFileState,
) -> None:
    row = (
        db.query(DiscoveryFileIndex)
        .filter(
            DiscoveryFileIndex.library_id == library.id,
            DiscoveryFileIndex.source_path == media_state.source_path,
        )
        .first()
    )
    if row is None:
        row = DiscoveryFileIndex(
            library_id=library.id,
            source_path=media_state.source_path,
        )
        db.add(row)
    row.file_size_bytes = media_state.file_size_bytes
    row.file_mtime_ns = media_state.file_mtime_ns
    row.discovery_signature = _discovery_signature(profile)
    row.last_seen_at = datetime.now(UTC)
    db.commit()


def _prepare_probe_candidate(
    db: Session,
    media_file: Path,
    library: Library,
    profile: LibraryProfile,
) -> DiscoveryProbeCandidate | None:
    if media_file.stem.endswith(profile.output_suffix):
        return None

    container = str(profile.container.value if hasattr(profile.container, 'value') else profile.container).lower().strip('.')
    output_path = media_file.with_name(f'{media_file.stem}{profile.output_suffix}.{container}')
    if output_path.exists():
        return None
    if _has_existing_target_identity_sibling(media_file, profile):
        return None

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
        from app.services.download_monitor_service import download_job_exists_for_source

        try:
            has_download_job = download_job_exists_for_source(db, source_path, library_id=library.id)
        except TypeError:
            # Some tests monkeypatch the older two-argument helper shape.
            has_download_job = download_job_exists_for_source(db, source_path)
        if has_download_job:
            return None
        if _blocking_encode_job_exists_for_download_route(db, source_path, library.id):
            return None
    else:
        if job_exists_for_source(db, source_path, library_id=library.id):
            return None
    
    if has_completed_job_for_identity(db, str(media_file), library.id):
        return None

    return DiscoveryProbeCandidate(media_file=media_file, source_path=source_path)


def _probe_candidate_metadata(candidate: DiscoveryProbeCandidate, probe_plan: DiscoveryProbePlan) -> DiscoveryProbeResult:
    if probe_plan.hdr_required:
        source_is_hdr = is_hdr_video(candidate.source_path)
        if not source_is_hdr:
            return DiscoveryProbeResult(source_resolution=None, source_is_hdr=False)

        source_resolution = probe_video_height(candidate.source_path)
        has_existing_target_sdr_sibling = False
        if _meets_minimum_source_resolution(candidate.media_file, source_resolution, probe_plan.minimum_source_resolution):
            has_existing_target_sdr_sibling = _has_existing_target_sdr_sibling(candidate.media_file, probe_plan.target_resolution)
        return DiscoveryProbeResult(
            source_resolution=source_resolution,
            source_is_hdr=True,
            has_existing_target_sdr_sibling=has_existing_target_sdr_sibling,
        )

    source_resolution = probe_video_height(candidate.source_path)
    return DiscoveryProbeResult(source_resolution=source_resolution, source_is_hdr=None)


def _finalize_candidate_with_probe(
    db: Session,
    candidate: DiscoveryProbeCandidate,
    probe_result: DiscoveryProbeResult,
    library: Library,
    profile: LibraryProfile,
):
    source_path = candidate.source_path
    download_enabled = getattr(profile, 'download_enabled', False)
    route_to_download = False
    source_resolution = probe_result.source_resolution
    source_is_hdr = probe_result.source_is_hdr
    hdr_required = bool(getattr(profile, 'hdr_only', False) or getattr(profile, 'tone_map_hdr', False))
    minimum_source_resolution = int(getattr(profile, 'minimum_source_resolution', 2160) or 2160)

    if hdr_required:
        if not source_is_hdr:
            return None
        if not _meets_minimum_source_resolution(candidate.media_file, source_resolution, minimum_source_resolution):
            return None
        if probe_result.has_existing_target_sdr_sibling:
            return None
    else:
        if source_resolution is None:
            return None
        if not _meets_minimum_source_resolution(candidate.media_file, source_resolution, minimum_source_resolution):
            return None
        if source_resolution <= profile.target_resolution:
            return None

    if download_enabled:
        from app.services.download_monitor_service import (
            can_attempt_download,
            create_download_job,
            download_job_exists_for_source,
            recover_completed_artifact_for_source,
        )

        if recover_completed_artifact_for_source(db, source_path, library, profile):
            _cancel_queued_encode_for_source(db, source_path, library.id)
            broker.publish_system_event(
                'discovery_download_imported',
                source_path=source_path,
                library_id=library.id,
            )
            return None
        try:
            has_download_job = download_job_exists_for_source(db, source_path, library_id=library.id)
        except TypeError:
            has_download_job = download_job_exists_for_source(db, source_path)
        if has_download_job:
            return None
        if can_attempt_download(db):
            download_job = create_download_job(db, source_path, library, profile)
            route_to_download = True
            if download_job is None:
                logger.info(
                    'Discovery: download route already satisfied for %r (library_id=%s)',
                    source_path,
                    library.id,
                )
            else:
                logger.info('Discovery: created download job for %r (library_id=%s)', source_path, library.id)
        else:
            logger.info(
                'Discovery: download mode enabled but Prowlarr/download client unavailable; '
                'queuing encode directly for %r',
                source_path,
            )
        if route_to_download:
            _cancel_queued_encode_for_source(db, source_path, library.id)
            broker.publish_system_event('discovery_download_routed', source_path=source_path, library_id=library.id)
            return None
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
            'encode_duration_seconds': job.encode_duration_seconds,
            'completed_at': job.completed_at.isoformat() if job.completed_at else None,
        },
        throttle_progress=False,
    )
    broker.publish_system_event('discovery_job_queued', source_path=source_path)
    return job


def _queue_file_if_eligible(db: Session, media_file: Path, library: Library, profile: LibraryProfile):
    candidate = _prepare_probe_candidate(db, media_file, library, profile)
    if candidate is None:
        return None
    probe_result = _probe_candidate_metadata(candidate, _build_probe_plan(profile))
    return _finalize_candidate_with_probe(db, candidate, probe_result, library, profile)


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
