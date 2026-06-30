from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import re

from sqlalchemy.orm import Session

from app.models.download_job import DownloadJob
from app.models.job import Job
from app.models.library import Library, LibraryProfile
from app.models.settings import Settings
from app.services import optimization_service
from app.services.job_timing_service import reset_encode_timing, stop_encode_timing

TERMINAL_STATUSES = {'complete', 'failed', 'skipped', 'cancelled'}


def _profile_snapshot(profile: LibraryProfile | None) -> str | None:
    if profile is None:
        return None

    return json.dumps(
        {
            'target_resolution': profile.target_resolution,
            'minimum_source_resolution': profile.minimum_source_resolution,
            'codec': profile.codec.value,
            'container': profile.container.value,
            'audio_mode': profile.audio_mode.value,
            'bitrate_mode': profile.bitrate_mode.value,
            'bitrate_mbps': profile.bitrate_mbps,
            'crf': profile.crf,
            'speed_preset': profile.speed_preset.value,
            'hdr_only': profile.hdr_only,
            'tone_map_hdr': profile.tone_map_hdr,
            'max_workers': profile.max_workers,
            'schedule_enabled': profile.schedule_enabled,
            'schedule_start_hour': profile.schedule_start_hour,
            'schedule_end_hour': profile.schedule_end_hour,
            'schedule_policy': profile.schedule_policy.value,
            'output_suffix': profile.output_suffix,
            'output_conflict_policy': profile.output_conflict_policy.value,
            'av1_fallback_codec': profile.av1_fallback_codec.value,
            'preferred_video_encoder': profile.preferred_video_encoder.value,
        }
    )


def create_job(
    db: Session,
    source_path: str,
    library_id: int | None = None,
    profile: LibraryProfile | None = None,
    source_resolution: int | None = None,
    source_is_hdr: bool | None = None,
    status: str = 'queued',
) -> Job:
    existing = get_existing_job_for_source(
        db, source_path, library_id=library_id)
    if existing is not None:
        changed = False
        if existing.library_id is None and library_id is not None:
            existing.library_id = library_id
            changed = True
        if existing.profile_snapshot_json is None and profile is not None:
            existing.profile_snapshot_json = _profile_snapshot(profile)
            changed = True
        if existing.source_resolution is None and source_resolution is not None:
            existing.source_resolution = source_resolution
            changed = True
        if existing.source_is_hdr is None and source_is_hdr is not None:
            existing.source_is_hdr = source_is_hdr
            changed = True
        if changed:
            db.commit()
            db.refresh(existing)
        return existing

    job = Job(
        input_path=source_path,
        status=status,
        library_id=library_id,
        profile_snapshot_json=_profile_snapshot(profile),
        source_resolution=source_resolution,
        source_is_hdr=source_is_hdr,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def refresh_queued_job_snapshots(db: Session, library_id: int, profile: LibraryProfile) -> int:
    """Re-snapshot the profile onto every queued job for a library.

    Called after a library profile is updated so that jobs waiting in the queue
    pick up the new settings (e.g. speed_preset, codec, bitrate) rather than
    the stale snapshot that was taken when they were originally created.

    Returns the number of jobs updated.
    """
    snapshot = _profile_snapshot(profile)
    updated = (
        db.query(Job)
        .filter(Job.library_id == library_id, Job.status == 'queued')
        .all()
    )
    for job in updated:
        job.profile_snapshot_json = snapshot
    if updated:
        db.commit()
    return len(updated)


def job_exists_for_source(db: Session, source_path: str, library_id: int | None = None) -> bool:
    return get_existing_job_for_source(db, source_path, library_id=library_id) is not None


def _path_parts(path_value: str) -> list[str]:
    return [part for part in re.split(r'[\\/]+', str(path_value or '').strip()) if part]


def _normalize_identity_text(value: str) -> str:
    return re.sub(r'[^a-z0-9]+', '', value.lower())


def _strip_release_suffix(value: str) -> str:
    stripped = re.sub(r'\[[^\]]+\]', ' ', value)
    stripped = re.sub(
        r'\([^\)]*(?:2160p|1080p|720p|480p|x264|x265|h264|h265|hevc|av1|web|bluray|remux)[^\)]*\)',
        ' ',
        stripped,
        flags=re.IGNORECASE,
    )
    stripped = re.sub(
        r'\b(?:2160p|1080p|720p|480p|x264|x265|h264|h265|hevc|av1|web|bluray|remux|web-dl|webrip|hdrip|brrip|bdrip|atmos|truehd)\b',
        ' ',
        stripped,
        flags=re.IGNORECASE,
    )
    return stripped.replace('.', ' ').replace('_', ' ').strip()


def media_identity_key(source_path: str) -> str | None:
    parts = _path_parts(source_path)
    if not parts:
        return None

    stem = re.sub(r'\.[^.\\/]+$', '', parts[-1])
    cleaned_stem = _strip_release_suffix(stem)
    episode_match = re.search(
        r'\bS(\d{1,2})\s*E(\d{1,3})\b', cleaned_stem, flags=re.IGNORECASE)
    if episode_match:
        title = cleaned_stem[:episode_match.start()].strip(' ._-')
        if (not title or re.fullmatch(r'(?:season\s*)?\d{1,2}', title, flags=re.IGNORECASE)) and len(parts) >= 3:
            title = parts[-3]
        normalized_title = _normalize_identity_text(title)
        if normalized_title:
            season = int(episode_match.group(1))
            episode = int(episode_match.group(2))
            return f'tv:{normalized_title}:s{season:02d}:e{episode:03d}'

    candidates = [_strip_release_suffix(stem)]
    if len(parts) >= 2:
        candidates.append(_strip_release_suffix(parts[-2]))

    for candidate in candidates:
        paren_match = re.search(r'\(((?:19|20)\d{2})\)', candidate)
        year_match = paren_match or re.search(r'\b((?:19|20)\d{2})\b', candidate)
        if not year_match:
            continue
        title = candidate[:year_match.start()].strip(' ._-')
        normalized_title = _normalize_identity_text(title)
        if normalized_title:
            return f'movie:{normalized_title}:{year_match.group(1)}'

    return None


def _job_blocks_identity_dedupe(job: Job) -> bool:
    return job.status not in TERMINAL_STATUSES


def get_existing_job_for_source(db: Session, source_path: str, library_id: int | None = None) -> Job | None:
    # Completed jobs normally block re-queuing, but stale "complete" rows whose
    # output no longer exists are treated as retryable.
    _RETRYABLE_STATUSES = {'failed', 'skipped', 'cancelled'}
    candidates = (
        db.query(Job)
        .filter(Job.input_path == source_path, ~Job.status.in_(_RETRYABLE_STATUSES))
        .order_by(Job.created_at.asc(), Job.id.asc())
        .all()
    )
    for job in candidates:
        if job.status != 'complete':
            return job
        output = Path(job.output_path) if job.output_path else None
        if output and output.exists():
            return job

    identity_key = media_identity_key(source_path)
    if not identity_key or library_id is None:
        return None

    identity_candidates = (
        db.query(Job)
        .filter(
            Job.input_path != source_path,
            Job.library_id == library_id,
            ~Job.status.in_(TERMINAL_STATUSES),
        )
        .order_by(Job.created_at.asc(), Job.id.asc())
        .all()
    )
    for job in identity_candidates:
        if _job_blocks_identity_dedupe(job) and media_identity_key(job.input_path) == identity_key:
            return job
    return None


def get_job(db: Session, job_id: int) -> Job | None:
    return db.query(Job).filter(Job.id == job_id).first()


def delete_job(db: Session, job_id: int) -> bool:
    job = get_job(db, job_id)
    if not job:
        return False

    settings = _get_settings(db)
    if job.status not in TERMINAL_STATUSES:
        job.cancel_requested = True
        optimization_service.stop_active_ffmpeg(job.id)
        optimization_service.delete_workspace(settings, job.id)
        stop_encode_timing(job)

    db.delete(job)
    db.commit()
    return True


def list_jobs(db: Session) -> list[Job]:
    return db.query(Job).order_by(Job.created_at.desc()).all()


def _get_settings(db: Session) -> Settings:
    settings = db.query(Settings).first()
    if settings:
        return settings

    settings = Settings()
    db.add(settings)
    db.commit()
    db.refresh(settings)
    return settings


def _probe_partial_duration(workspace: Path) -> float | None:
    return optimization_service.recover_resume_position(workspace)


def cancel_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status in TERMINAL_STATUSES:
        return job

    was_running = job.status == 'running'
    if job.status in {'running', 'starting', 'preflight'}:
        optimization_service.stop_active_ffmpeg(job.id)

    if job.status in {'queued', 'running', 'starting', 'preflight'}:
        job.status = 'queued'
        job.error_message = None
        job.eta_seconds = None
        job.fps = None
        job.cancel_requested = False
        job.completed_at = None
        if was_running:
            stop_encode_timing(job)

    db.commit()
    db.refresh(job)
    return job


def retry_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status not in {'failed', 'cancelled'}:
        return job

    settings = _get_settings(db)
    if not job.resume_position_seconds:
        workspace = Path(settings.workspace_root) / str(job.id)
        partial_duration = _probe_partial_duration(workspace)
        if partial_duration and partial_duration > 0:
            job.resume_position_seconds = partial_duration
        else:
            job.progress_percent = 0

    job.status = 'queued'
    job.retry_count = 0
    job.fps = None
    job.eta_seconds = None
    job.output_path = None
    job.error_message = None
    job.cancel_requested = False
    job.completed_at = None
    db.commit()
    db.refresh(job)
    return job


def pause_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status != 'running':
        return job

    # Capture the current encode position before terminating FFmpeg so we can
    # resume from that offset instead of re-encoding from the beginning.
    current_position = optimization_service.get_active_position(job_id)
    optimization_service.stop_active_ffmpeg(job_id)

    job.status = 'paused'
    job.cancel_requested = False
    job.eta_seconds = None
    if current_position is not None and current_position > 0:
        job.resume_position_seconds = current_position
    stop_encode_timing(job)
    db.commit()
    db.refresh(job)
    return job


def resume_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status != 'paused':
        return job

    # Do NOT delete the partial output — it will be used to resume encoding from
    # the saved position rather than starting over from the beginning.
    job.status = 'queued'
    job.fps = None
    job.eta_seconds = None
    job.error_message = None
    job.cancel_requested = False
    job.completed_at = None
    # resume_position_seconds and progress_percent are intentionally preserved so
    # optimize_video knows where to seek and the UI shows existing progress.
    db.commit()
    db.refresh(job)
    return job


def abort_job(db: Session, job_id: int) -> Job | None:
    job = get_job(db, job_id)
    if not job:
        return None

    if job.status in TERMINAL_STATUSES:
        return job

    settings = _get_settings(db)
    optimization_service.stop_active_ffmpeg(job_id)
    was_running = job.status == 'running'
    # Keep the workspace while a running worker is winding down so an in-flight
    # ffmpeg process does not continue writing into a deleted directory.
    # The worker cooperatively observes cancel_requested and finalizes state.
    job.cancel_requested = True
    if job.status == 'queued':
        optimization_service.delete_workspace(settings, job_id)
        job.status = 'cancelled'
        job.progress_percent = 0
        job.fps = None
        job.eta_seconds = None
        job.output_path = None
        job.error_message = 'Aborted by user'
        job.cancel_requested = False
        job.completed_at = datetime.now(UTC)
        stop_encode_timing(job)
    elif job.status in {'starting', 'preflight', 'running'}:
        job.status = 'aborting'
        job.error_message = 'Aborting by user request'
        job.eta_seconds = None
        job.completed_at = None
        if was_running:
            stop_encode_timing(job)
    else:
        optimization_service.delete_workspace(settings, job_id)
        job.status = 'cancelled'
        job.error_message = 'Aborted by user'
        job.progress_percent = 0
        job.fps = None
        job.eta_seconds = None
        job.output_path = None
        job.completed_at = datetime.now(UTC)
        job.cancel_requested = False
        stop_encode_timing(job)
    db.commit()
    db.refresh(job)
    return job


def discard_progress_and_requeue(db: Session, job_id: int) -> Job | None:
    """Stop the job (if running), wipe its partial workspace and progress
    data, then return it to the queue so it restarts from the beginning.

    This is offered as an alternative to a full abort when the job is paused
    and has partial progress – the user can choose to keep the item in the
    queue rather than removing it entirely.
    """
    job = get_job(db, job_id)
    if not job:
        return None

    settings = _get_settings(db)
    optimization_service.stop_active_ffmpeg(job_id)
    optimization_service.delete_workspace(settings, job_id)

    job.status = 'queued'
    job.progress_percent = 0
    job.resume_position_seconds = None
    job.fps = None
    job.eta_seconds = None
    job.output_path = None
    job.error_message = None
    job.cancel_requested = False
    job.completed_at = None
    job.retry_count = 0
    reset_encode_timing(job, clear_duration=True)
    db.commit()
    db.refresh(job)
    return job


def prune_job_history(db: Session, retention_days: int) -> int:
    if retention_days <= 0:
        return 0

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    stale_jobs = (
        db.query(Job)
        .filter(Job.status.in_(TERMINAL_STATUSES), Job.completed_at.is_not(None), Job.completed_at < cutoff)
        .all()
    )

    deleted_count = len(stale_jobs)
    for stale_job in stale_jobs:
        db.delete(stale_job)

    if deleted_count:
        db.commit()

    return deleted_count


def has_completed_job_for_identity(db: Session, source_path: str, library_id: int) -> bool:
    identity_key = media_identity_key(source_path)
    if not identity_key:
        return False

    # Check for completed encode jobs
    encode_jobs = (
        db.query(Job)
        .filter(
            Job.library_id == library_id,
            Job.status == 'complete'
        )
        .all()
    )
    for job in encode_jobs:
        if media_identity_key(job.input_path) == identity_key:
            if job.output_path and Path(job.output_path).exists():
                return True

    # Check for completed download jobs
    download_jobs = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.library_id == library_id,
            DownloadJob.status == 'complete'
        )
        .all()
    )
    for job in download_jobs:
        if media_identity_key(job.source_file_path) == identity_key:
            if job.imported_file_path and Path(job.imported_file_path).exists():
                return True

    return False


def cleanup_optimized_outputs(db: Session) -> tuple[int, list[int]]:
    terminal_jobs = db.query(Job).filter(Job.status.in_(
        TERMINAL_STATUSES), Job.output_path.is_not(None)).all()

    removed_files = 0
    removed_job_ids: list[int] = []
    for job in terminal_jobs:
        output_path = str(job.output_path or '').strip()
        if not output_path:
            continue

        candidate = Path(output_path)
        if not candidate.exists() or not candidate.is_file():
            continue

        candidate.unlink(missing_ok=True)
        if not candidate.exists():
            removed_files += 1
            removed_job_ids.append(job.id)
            job.output_path = None

    if removed_job_ids:
        db.commit()

    return removed_files, removed_job_ids


def cleanup_replaced_optimized_outputs(
    db: Session,
    *,
    library_id: int | None,
    source_path: str,
    keep_output_path: str | None,
    current_job_id: int | None = None,
    current_download_job_id: int | None = None,
) -> tuple[int, list[int], list[int]]:
    """Remove older optimized outputs for the same media after an upgrade wins.

    Radarr/Sonarr upgrades can replace the source path, leaving prior optimized
    outputs behind with a different stem. This runs only after the new output or
    import has completed, and only deletes files recorded as older Optimizarr
    outputs/imports.
    """
    identity_key = media_identity_key(source_path)
    if library_id is None or not identity_key:
        return 0, [], []

    keep_path = Path(keep_output_path).resolve() if keep_output_path else None
    removed_files = 0
    affected_job_ids: list[int] = []
    affected_download_job_ids: list[int] = []

    completed_jobs = (
        db.query(Job)
        .filter(
            Job.library_id == library_id,
            Job.status == 'complete',
            Job.output_path.is_not(None),
        )
        .order_by(Job.completed_at.desc(), Job.id.desc())
        .all()
    )
    for job in completed_jobs:
        if current_job_id is not None and job.id == current_job_id:
            continue
        if media_identity_key(job.input_path) != identity_key:
            continue

        output_path_value = str(job.output_path or '').strip()
        if not output_path_value:
            continue
        output_path = Path(output_path_value)

        try:
            if keep_path is not None and output_path.resolve() == keep_path:
                continue
        except OSError:
            pass

        # Never delete a library source file masquerading as an output record.
        input_path = Path(str(job.input_path or '').strip())
        try:
            is_recorded_source = output_path.resolve() == input_path.resolve()
        except OSError:
            is_recorded_source = output_path == input_path
        if is_recorded_source:
            continue
        if not output_path.exists() or not output_path.is_file():
            job.output_path = None
            continue

        output_path.unlink(missing_ok=True)
        if output_path.exists():
            continue

        removed_files += 1
        affected_job_ids.append(job.id)
        job.output_path = None

    completed_download_jobs = (
        db.query(DownloadJob)
        .filter(
            DownloadJob.library_id == library_id,
            DownloadJob.status == 'complete',
            DownloadJob.imported_file_path.is_not(None),
        )
        .order_by(DownloadJob.completed_at.desc(), DownloadJob.id.desc())
        .all()
    )
    for download_job in completed_download_jobs:
        if current_download_job_id is not None and download_job.id == current_download_job_id:
            continue
        if media_identity_key(download_job.source_file_path) != identity_key:
            continue

        imported_path_value = str(download_job.imported_file_path or '').strip()
        if not imported_path_value:
            continue
        imported_path = Path(imported_path_value)

        try:
            if keep_path is not None and imported_path.resolve() == keep_path:
                continue
        except OSError:
            pass

        source_file_path = Path(str(download_job.source_file_path or '').strip())
        try:
            is_recorded_source = imported_path.resolve() == source_file_path.resolve()
        except OSError:
            is_recorded_source = imported_path == source_file_path
        if is_recorded_source:
            continue
        if not imported_path.exists() or not imported_path.is_file():
            download_job.imported_file_path = None
            continue

        imported_path.unlink(missing_ok=True)
        if imported_path.exists():
            continue

        removed_files += 1
        affected_download_job_ids.append(download_job.id)
        download_job.imported_file_path = None

    return removed_files, affected_job_ids, affected_download_job_ids


def _cleanup_versioned_optimized_siblings(library_path: Path) -> int:
    removed_files = 0
    versioned_pattern = re.compile(r'^(?P<base>.+)-v\d+$', flags=re.IGNORECASE)

    for candidate in library_path.rglob('*'):
        if not candidate.is_file():
            continue
        match = versioned_pattern.match(candidate.stem)
        if not match:
            continue

        canonical = candidate.with_name(f'{match.group("base")}{candidate.suffix}')
        if not canonical.exists() or not canonical.is_file():
            continue

        candidate.unlink(missing_ok=True)
        if not candidate.exists():
            removed_files += 1

    return removed_files


def cleanup_duplicate_optimized_outputs(db: Session) -> tuple[int, list[int]]:
    libraries = db.query(Library).all()

    removed_files = 0
    affected_library_ids: list[int] = []
    affected_library_ids_seen: set[int] = set()

    for library in libraries:
        profile = library.profile
        if profile is None:
            continue

        library_path = Path(str(library.path or '').strip())
        if not library_path.exists() or not library_path.is_dir():
            continue

        versioned_removed = _cleanup_versioned_optimized_siblings(library_path)
        if versioned_removed:
            removed_files += versioned_removed
            if library.id not in affected_library_ids_seen:
                affected_library_ids.append(library.id)
                affected_library_ids_seen.add(library.id)

        artifact_groups: dict[str, list[dict]] = {}
        completed_encode_jobs = (
            db.query(Job)
            .filter(
                Job.library_id == library.id,
                Job.status == 'complete',
                Job.output_path.is_not(None),
            )
            .all()
        )
        for job in completed_encode_jobs:
            output_path = Path(str(job.output_path or '').strip())
            if not output_path.exists() or not output_path.is_file():
                continue
            if output_path == Path(str(job.input_path or '').strip()):
                continue
            identity_key = media_identity_key(job.input_path)
            if not identity_key:
                continue
            artifact_groups.setdefault(identity_key, []).append({
                'path': output_path,
                'completed_at': job.completed_at,
                'kind': 'encode',
                'record': job,
            })

        completed_download_jobs = (
            db.query(DownloadJob)
            .filter(
                DownloadJob.library_id == library.id,
                DownloadJob.status == 'complete',
                DownloadJob.imported_file_path.is_not(None),
            )
            .all()
        )
        for download_job in completed_download_jobs:
            imported_path = Path(
                str(download_job.imported_file_path or '').strip())
            if not imported_path.exists() or not imported_path.is_file():
                continue
            if imported_path == Path(str(download_job.source_file_path or '').strip()):
                continue
            # Use the original source path for identity, not the imported path,
            # so downloaded artifacts can be grouped with encoded ones from the
            # same source for duplicate cleanup.
            identity_key = media_identity_key(str(download_job.source_file_path or ''))
            if not identity_key:
                continue
            artifact_groups.setdefault(identity_key, []).append({
                'path': imported_path,
                'completed_at': download_job.completed_at,
                'kind': 'download',
                'record': download_job,
            })

        for artifacts in artifact_groups.values():
            unique_by_path = {
                str(artifact['path']): artifact for artifact in artifacts}
            if len(unique_by_path) <= 1:
                continue

            def sort_key(artifact: dict) -> tuple:
                # Keep the highest resolution artifact, falling back to largest
                # file size, then most recent.
                if artifact['kind'] == 'encode' and artifact['record'] and getattr(artifact['record'], 'source_resolution', None):
                    resolution = int(artifact['record'].source_resolution)
                else:
                    probed = optimization_service.probe_video_height(str(artifact['path']))
                    resolution = probed if probed is not None else 0

                try:
                    size = artifact['path'].stat().st_size
                except OSError:
                    size = 0
                return (
                    resolution,
                    size,
                    artifact['completed_at'] is not None,
                    artifact['completed_at'],
                    str(artifact['path']),
                )

            sorted_artifacts = sorted(unique_by_path.values(), key=sort_key, reverse=True)
            keep_path = str(sorted_artifacts[0]['path'])
            for artifact in unique_by_path.values():
                artifact_path = Path(artifact['path'])
                if str(artifact_path) == keep_path:
                    continue
                artifact_path.unlink(missing_ok=True)
                if artifact_path.exists():
                    continue
                removed_files += 1
                if artifact['kind'] == 'encode':
                    artifact['record'].output_path = None
                else:
                    artifact['record'].imported_file_path = None
                if library.id not in affected_library_ids_seen:
                    affected_library_ids.append(library.id)
                    affected_library_ids_seen.add(library.id)

    db.commit()

    return removed_files, affected_library_ids


def abort_all_jobs(db: Session) -> list[Job]:
    targets = db.query(Job).filter(~Job.status.in_(TERMINAL_STATUSES)).all()
    if not targets:
        return []

    settings = _get_settings(db)
    now = datetime.now(UTC)
    for job in targets:
        optimization_service.stop_active_ffmpeg(job.id)
        optimization_service.delete_workspace(settings, job.id)
        job.status = 'cancelled'
        job.progress_percent = 0
        job.fps = None
        job.eta_seconds = None
        job.output_path = None
        job.error_message = 'Aborted by user'
        job.cancel_requested = False
        job.completed_at = now
        stop_encode_timing(job)

    db.commit()
    for job in targets:
        db.refresh(job)
    return targets


def remove_all_terminal_jobs(db: Session) -> list[int]:
    terminal_jobs = db.query(Job).filter(
        Job.status.in_(TERMINAL_STATUSES)).all()
    if not terminal_jobs:
        return []

    removed_job_ids = [job.id for job in terminal_jobs]
    for job in terminal_jobs:
        db.delete(job)
    db.commit()
    return removed_job_ids


def cancel_all_queued_jobs(db: Session) -> list[Job]:
    targets = db.query(Job).filter(Job.status == 'queued').all()
    if not targets:
        return []

    now = datetime.now(UTC)
    for job in targets:
        job.status = 'cancelled'
        job.completed_at = now
        stop_encode_timing(job)
    db.commit()
    for job in targets:
        db.refresh(job)
    return targets
