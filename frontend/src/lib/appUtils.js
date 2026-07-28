import QRCode from 'qrcode';

import { buildUnifiedQueueItems } from '../queueSorting';

export const WS_PATH = '/ws';
export const FALLBACK_AFTER_MS = 5000;
export const REALTIME_BATCH_MS = 250;
export const FALLBACK_POLL_MS = 10000;
export const ACTIVE_QUEUE_POLL_MS = 1000;
export const METRICS_POLL_MS = 10000;
export const QUEUE_RECONCILE_POLL_MS = 15000;
export const RECONNECT_BASE_DELAY_MS = 1000;
export const RECONNECT_MAX_DELAY_MS = 30000;
export const JOBS_PAGE_SIZE = 50;
export const HISTORY_PAGE_SIZE = 25;
export const LOGS_PAGE_SIZE = 25;
export const JOBS_UI_PREFS_KEY = 'optimizarr.jobsUiPrefs.v1';
export const LIBRARIES_UI_PREFS_KEY = 'optimizarr.librariesUiPrefs.v1';
export const SETTINGS_UI_PREFS_KEY = 'optimizarr.settingsUiPrefs.v1';
export const PROFILE_SECTIONS_DEFAULT = {
  details: false,
  processing: false,
  plex: false,
  download: false,
};

export const SETTINGS_SECTIONS_DEFAULT = {
  account: false,
  general: false,
  notifications: false,
  prowlarr: false,
  qbittorrent: false,
  sabnzbd: false,
  plex: false,
};

export const PAGE_KEYS = {
  dashboard: 'Dashboard',
  libraries: 'Libraries',
  jobs: 'Jobs',
  logs: 'Logs',
  settings: 'Settings',
};

export const QUALITY_PRESETS = {
  efficiency: {
    label: 'Efficiency (smaller files)',
    profile: {
      codec: 'av1',
      av1_fallback_codec: 'hevc',
      bitrate_mode: 'vbr_crf',
      crf: 28,
      speed_preset: 'slow',
      target_resolution: 1080,
      container: 'mkv',
      audio_mode: 'copy',
    },
  },
  balanced: {
    label: 'Balanced',
    profile: {
      codec: 'hevc',
      bitrate_mode: 'vbr_crf',
      crf: 23,
      speed_preset: 'medium',
      target_resolution: 1080,
      container: 'mkv',
      audio_mode: 'copy',
      av1_fallback_codec: 'hevc',
    },
  },
  speed: {
    label: 'Fast Encode',
    profile: {
      codec: 'h264',
      bitrate_mode: 'cbr',
      bitrate_mbps: 12,
      speed_preset: 'fast',
      target_resolution: 720,
      container: 'mp4',
      audio_mode: 'aac',
      av1_fallback_codec: 'h264',
    },
  },
};

export const TARGET_RESOLUTION_PRESETS = [2160, 1440, 1080, 720];
export const MIN_SOURCE_RESOLUTION_PRESETS = [2160, 1440, 1080];
export const CODEC_LABELS = {
  h264: 'H.264',
  hevc: 'HEVC',
  av1: 'AV1',
};

export function progressFromJob(job) {
  const normalized = job.status?.toLowerCase();
  const reported = Number(job.progress_percent);
  if (Number.isFinite(reported) && reported >= 0) {
    return Math.max(0, Math.min(100, Math.round(reported)));
  }
  if (normalized === 'complete' || normalized === 'completed') return 100;
  if (normalized === 'running' || normalized === 'processing') return 0;
  if (normalized === 'failed') return 100;
  if (normalized === 'canceled' || normalized === 'cancelled') return 100;
  return 0;
}

export function formatHour(hour) {
  return `${String(hour).padStart(2, '0')}:00`;
}

export function parseHour(timeValue) {
  const [hour] = timeValue.split(':');
  return Number(hour);
}

export const ACTIVE_STATUSES = new Set(['starting', 'running', 'preflight', 'aborting']);
export const PAUSED_STATUSES = new Set(['paused', 'paused_schedule']);
export const QUEUED_STATUSES = new Set(['pending', 'queued', 'created']);
export const TERMINAL_STATUSES = new Set(['complete', 'failed', 'skipped', 'cancelled']);

// Download-job status buckets
export const ACTIVE_DL_STATUSES = new Set(['pending', 'checking', 'searching', 'queued', 'paused', 'downloading', 'repairing', 'unpacking', 'moving', 'stalled', 'importing', 'needs_review', 'waiting_encode']);
export const TERMINAL_DL_STATUSES = new Set(['complete', 'file_deleted', 'failed', 'timed_out', 'fallback_queued']);
export const QUEUE_DEDUPE_DL_STATUSES = new Set([...ACTIVE_DL_STATUSES, 'complete', 'fallback_queued']);
export const LOG_REFRESH_SYSTEM_EVENTS = new Set([
  'all_jobs_aborted',
  'cleanup_summary',
  'download_job_removed',
  'download_job_reset',
  'download_job_retried',
  'duplicate_optimized_cleanup_summary',
  'history_purged',
  'job_aborted',
  'job_cancelled',
  'job_discarded',
  'job_paused',
  'job_removed',
  'job_resumed',
  'job_retried',
  'job_started',
  'library_scan_completed',
  'library_scan_summary',
  'optimized_cleanup_summary',
  'queue_clear_summary',
  'queue_paused',
  'queue_resumed',
  'queued_jobs_cancelled',
  'recovery_summary',
]);

export function isActiveEncodeStatus(status) {
  return ACTIVE_STATUSES.has(status?.toLowerCase());
}

export function mergeJobsWithUpdate(previousJobs, nextJob) {
  const existingIndex = previousJobs.findIndex((job) => job.id === nextJob.id);
  if (existingIndex === -1) {
    return {
      jobs: [nextJob, ...previousJobs],
      resetToFirstPage: isActiveEncodeStatus(nextJob.status),
    };
  }

  const previousJob = previousJobs[existingIndex];
  const updatedJob = { ...previousJob, ...nextJob };
  const jobs = [...previousJobs];
  jobs[existingIndex] = updatedJob;

  return {
    jobs,
    resetToFirstPage: !isActiveEncodeStatus(previousJob.status) && isActiveEncodeStatus(updatedJob.status),
  };
}

export function mergeDownloadJobsWithUpdate(previousJobs, nextDownloadJob) {
  const nextStatus = String(nextDownloadJob?.status ?? '').toLowerCase();
  const existingIndex = previousJobs.findIndex((job) => job.id === nextDownloadJob?.id);
  if (existingIndex === -1) {
    return [...previousJobs, normalizeDownloadJob(nextDownloadJob)];
  }

  const previousJob = previousJobs[existingIndex];
  const previousStatus = String(previousJob?.status ?? '').toLowerCase();

  // Websocket events can arrive out of order around import completion.
  // Never allow a terminal row to regress back into an active download state.
  if (TERMINAL_DL_STATUSES.has(previousStatus) && ACTIVE_DL_STATUSES.has(nextStatus)) {
    return previousJobs;
  }

  const mergedJob = { ...previousJob, ...nextDownloadJob };
  if (
    String(previousJob?.client_type ?? '').toLowerCase() === 'sabnzbd'
    && previousJob?.client_queue_position != null
    && nextDownloadJob?.client_queue_position == null
  ) {
    mergedJob.client_queue_position = previousJob.client_queue_position;
  }

  const updated = [...previousJobs];
  updated[existingIndex] = normalizeDownloadJob(mergedJob);
  return updated;
}

export function removeJobById(previousJobs, jobId) {
  return previousJobs.filter((job) => job.id !== jobId);
}

export function isLibraryEncodeJob(library, job) {
  // Prefer library_id match (accurate); fall back to path prefix for legacy data
  if (job.library_id != null) return job.library_id === library.id;
  return job.source_path === library.path || job.source_path?.startsWith(`${library.path}/`);
}

export function isActiveLibraryEncodeQueueJob(library, job) {
  const status = job.status?.toLowerCase();
  if (!status || TERMINAL_STATUSES.has(status)) return false;
  return isLibraryEncodeJob(library, job);
}

export function libraryQueueCount(library, jobs, downloadJobs) {
  const libraryEncodeItems = jobs.filter((job) => isActiveLibraryEncodeQueueJob(library, job));
  const libraryDownloadItems = downloadJobs.filter((job) => {
    const status = String(job.status ?? '').toLowerCase();
    return ACTIVE_DL_STATUSES.has(status) && job.library_id === library.id;
  });
  const libraryDedupeSourcePaths = new Set(
    downloadJobs
      .filter((job) => job.library_id === library.id && QUEUE_DEDUPE_DL_STATUSES.has(String(job.status ?? '').toLowerCase()))
      .map((job) => String(job.source_file_path ?? '').trim())
      .filter(Boolean),
  );

  return buildUnifiedQueueItems({
    encodeItems: libraryEncodeItems,
    downloadItems: libraryDownloadItems,
    sortOption: 'default',
    extractTitleYear,
    pinActiveFirst: true,
    dedupeSourcePaths: libraryDedupeSourcePaths,
  }).length;
}

export function shouldShowDownloadElapsed(status) {
  return ['checking', 'searching', 'downloading', 'repairing', 'unpacking', 'moving', 'stalled', 'importing'].includes(String(status ?? '').toLowerCase());
}

export function jobSortRank(job) {
  const status = job.status?.toLowerCase();
  if (ACTIVE_STATUSES.has(status)) return 0;
  if (PAUSED_STATUSES.has(status)) return 1;
  if (QUEUED_STATUSES.has(status)) {
    // Queued jobs that were previously paused (have a saved resume position)
    // mirror the backend's priority boost and appear just below paused jobs.
    return (job.resume_position_seconds > 0) ? 1.5 : 2;
  }
  return 3;
}

export function compareActiveJobsDefault(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  if (jobSortRank(a) === 2) return a.id - b.id;
  return b.id - a.id;
}

export function compareActiveJobsNewest(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  return b.id - a.id;
}

export function compareActiveJobsOldest(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  return a.id - b.id;
}

export function compareActiveJobsYearNewest(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  const yearA = extractTitleYear(a.source_path).year;
  const yearB = extractTitleYear(b.source_path).year;
  if (yearA && yearB) return Number(yearB) - Number(yearA);
  if (yearA) return -1;
  if (yearB) return 1;
  return b.id - a.id;
}

export function compareActiveJobsYearOldest(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  const yearA = extractTitleYear(a.source_path).year;
  const yearB = extractTitleYear(b.source_path).year;
  if (yearA && yearB) return Number(yearA) - Number(yearB);
  if (yearA) return -1;
  if (yearB) return 1;
  return a.id - b.id;
}

export function compareDownloadHistoryJobsByOption(a, b, sortOption) {
  if (sortOption === 'year_newest' || sortOption === 'year_oldest') {
    const yearA = extractTitleYear(a.source_file_path).year;
    const yearB = extractTitleYear(b.source_file_path).year;
    if (yearA && yearB) {
      return sortOption === 'year_newest'
        ? Number(yearB) - Number(yearA)
        : Number(yearA) - Number(yearB);
    }
    if (yearA) return -1;
    if (yearB) return 1;
  }

  const completedA = Date.parse(a.completed_at || '') || 0;
  const completedB = Date.parse(b.completed_at || '') || 0;
  if (completedA !== completedB) return completedB - completedA;

  const createdA = Date.parse(a.created_at || '') || 0;
  const createdB = Date.parse(b.created_at || '') || 0;
  if (createdA !== createdB) return createdB - createdA;

  return b.id - a.id;
}

export function historyItemPath(item) {
  return item._historyType === 'download' ? item.source_file_path : item.source_path;
}

export function historyItemYear(item) {
  return extractTitleYear(historyItemPath(item)).year;
}

export function historyItemCompletedTimestamp(item) {
  return Date.parse(item.completed_at || '') || 0;
}

export function historyItemCreatedTimestamp(item) {
  return Date.parse(item.created_at || '') || 0;
}

export function compareHistoryItemsByOption(a, b, sortOption) {
  if (sortOption === 'year_newest' || sortOption === 'year_oldest') {
    const yearA = historyItemYear(a);
    const yearB = historyItemYear(b);
    if (yearA && yearB) {
      return sortOption === 'year_newest'
        ? Number(yearB) - Number(yearA)
        : Number(yearA) - Number(yearB);
    }
    if (yearA) return -1;
    if (yearB) return 1;
  }

  const completedDelta = historyItemCompletedTimestamp(b) - historyItemCompletedTimestamp(a);
  if (completedDelta !== 0) return completedDelta;

  const createdDelta = historyItemCreatedTimestamp(b) - historyItemCreatedTimestamp(a);
  if (createdDelta !== 0) return createdDelta;

  if (a._historyType !== b._historyType) return a._historyType.localeCompare(b._historyType);
  return b.id - a.id;
}

export function buildUnifiedHistoryItems(encodeJobs, downloadJobs) {
  return [
    ...encodeJobs.map((job) => ({ ...job, _historyType: 'encode' })),
    ...downloadJobs.map((job) => ({ ...job, _historyType: 'download' })),
  ];
}

export function formatResolution(height) {
  return Number.isInteger(height) ? `${height}p` : 'Unknown';
}

export function formatHdrIndicator(sourceIsHdr) {
  if (sourceIsHdr === true) return 'HDR';
  if (sourceIsHdr === false) return 'SDR';
  return 'Unknown';
}

export function formatEta(etaSeconds) {
  if (etaSeconds == null || etaSeconds < 0) return null;
  if (etaSeconds === 0) return 'Done';
  const h = Math.floor(etaSeconds / 3600);
  const m = Math.floor((etaSeconds % 3600) / 60);
  const s = etaSeconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function formatDownloadSpeed(bytesPerSecond) {
  const speed = Number(bytesPerSecond);
  if (!Number.isFinite(speed) || speed <= 0) return null;
  if (speed >= 1024 * 1024 * 1024) return `${(speed / (1024 * 1024 * 1024)).toFixed(2)} GB/s`;
  if (speed >= 1024 * 1024) return `${(speed / (1024 * 1024)).toFixed(2)} MB/s`;
  if (speed >= 1024) return `${(speed / 1024).toFixed(1)} KB/s`;
  return `${Math.round(speed)} B/s`;
}

export function clampMetricPercent(value) {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return 0;
  return Math.max(0, Math.min(100, parsed));
}

export function formatGpuPercent(metrics) {
  return `${Math.round(Math.max(
    clampMetricPercent(metrics?.gpu_video_percent),
    clampMetricPercent(metrics?.gpu_render_percent),
  ))}%`;
}

export function formatDownloadRetry(job) {
  const retryCount = Number(job?.retry_count);
  const maxRetries = Number(job?.max_retries);
  if (!Number.isFinite(retryCount) || !Number.isFinite(maxRetries)) return null;
  if (retryCount <= 0 || maxRetries <= 0) return null;
  return `Retry ${Math.min(retryCount, maxRetries)}/${maxRetries}`;
}

export function formatDownloadClient(clientType) {
  if (clientType === 'qbittorrent') return 'qBittorrent';
  if (clientType === 'sabnzbd') return 'SABnzbd';
  return null;
}

export function formatHistoryCompletedAt(value) {
  if (!value) return '—';
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    year: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
    timeZoneName: 'short',
  });
}

export function formatLogCreatedAt(value) {
  if (!value) return 'Unknown time';
  return new Date(value).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    second: '2-digit',
    hour12: true,
  });
}

export function formatLogDetailValue(value) {
  if (Array.isArray(value)) return value.length ? value.join(', ') : 'none';
  if (value === true) return 'true';
  if (value === false) return 'false';
  if (value == null || value === '') return 'none';
  return String(value);
}

export const LOG_EVENT_LABELS = {
  cleanup_started: 'Workspace Cleanup Started',
  cleanup_summary: 'Workspace Cleanup',
  duplicate_optimized_cleanup_started: 'Duplicate Cleanup Started',
  duplicate_optimized_cleanup_summary: 'Duplicate Cleanup',
  library_scan_started: 'Library Scan Started',
  library_scan_summary: 'Library Scan',
  optimized_cleanup_started: 'Optimized Cleanup Started',
  optimized_cleanup_summary: 'Optimized Cleanup',
  queue_clear_summary: 'Queue Cleared',
  queue_paused: 'Queue Paused',
  queue_resumed: 'Queue Resumed',
  recovery_started: 'Recovery Started',
  recovery_summary: 'Recovery',
  all_jobs_aborted: 'Abort All Jobs',
  history_purged: 'History Purged',
  queued_jobs_cancelled: 'Queued Jobs Cancelled',
  job_aborted: 'Job Aborted',
  job_cancelled: 'Job Cancelled',
  job_discarded: 'Job Progress Discarded',
  job_paused: 'Job Paused',
  job_removed: 'Job Removed',
  job_resumed: 'Job Resumed',
  job_retried: 'Job Retried',
  job_started: 'Job Started',
  download_job_removed: 'Download Removed',
  download_job_reset: 'Download Reset',
  download_job_retried: 'Download Retried',
};

export function formatLogEventType(eventType) {
  const normalized = String(eventType ?? '');
  if (LOG_EVENT_LABELS[normalized]) return LOG_EVENT_LABELS[normalized];
  return normalized
    .split('_')
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(' ') || 'Event';
}

export function logMatchesSearch(log, search) {
  const needle = String(search ?? '').trim().toLowerCase();
  if (!needle) return true;
  const haystack = [
    log.event_type,
    formatLogEventType(log.event_type),
    log.severity,
    log.message,
    ...Object.entries(log.details ?? {}).flatMap(([key, value]) => [key, formatLogDetailValue(value)]),
  ].join(' ').toLowerCase();
  return haystack.includes(needle);
}

export function logSeverityRowClass(severity) {
  const normalized = String(severity ?? '').toLowerCase();
  if (normalized === 'error') return 'border-l-red-500/80 bg-red-950/[0.08]';
  if (normalized === 'warning' || normalized === 'warn') return 'border-l-amber-400/80 bg-amber-950/[0.07]';
  if (normalized === 'success') return 'border-l-emerald-400/80 bg-emerald-950/[0.07]';
  return 'border-l-cyan-400/55';
}

export function logSeverityTextClass(severity) {
  const normalized = String(severity ?? '').toLowerCase();
  if (normalized === 'error') return 'text-red-300';
  if (normalized === 'warning' || normalized === 'warn') return 'text-amber-300';
  if (normalized === 'success') return 'text-emerald-300';
  return 'text-cyan-300';
}

export function getElapsedSeconds(createdAt, nowMs = Date.now()) {
  if (!createdAt) return null;
  const createdAtMs = Date.parse(createdAt);
  if (!Number.isFinite(createdAtMs)) return null;
  return Math.max(0, Math.floor((nowMs - createdAtMs) / 1000));
}

export function estimateDownloadEtaSeconds(downloadJob, nowMs = Date.now()) {
  const elapsedSeconds = getElapsedSeconds(downloadJob.download_started_at ?? downloadJob.created_at, nowMs);
  const reportedProgress = Number(downloadJob.progress_percent);

  if (elapsedSeconds == null || !Number.isFinite(reportedProgress) || reportedProgress <= 0) {
    return null;
  }

  const progress = Math.min(100, reportedProgress);
  if (progress >= 100) return null;

  return Math.max(0, Math.round(elapsedSeconds * ((100 - progress) / progress)));
}

export function getDownloadEtaSeconds(downloadJob, nowMs = Date.now()) {
  const status = String(downloadJob?.status ?? '').toLowerCase();
  const isComplete = status === 'complete';
  const reportedEta = Number(downloadJob?.eta_seconds);
  if (Number.isFinite(reportedEta) && reportedEta >= 0) {
    if (reportedEta > 0) return Math.round(reportedEta);
    if (reportedEta === 0 && isComplete) return 0;
  }
  const estimated = estimateDownloadEtaSeconds(downloadJob, nowMs);
  if (estimated === 0 && !isComplete) return null;
  return estimated;
}

export function downloadJobMatchesSearch(downloadJob, search, libraryById = {}) {
  if (!search) return true;
  const lower = search.toLowerCase();
  const { title, year } = extractTitleYear(downloadJob.source_file_path);
  const libName = downloadJob.library_id != null ? (libraryById[downloadJob.library_id]?.name ?? '') : '';
  return (
    title.toLowerCase().includes(lower)
    || (year && year.includes(lower))
    || libName.toLowerCase().includes(lower)
    || downloadJob.source_file_path?.toLowerCase().includes(lower)
    || String(downloadJob.id).includes(lower)
    || String(downloadJob.status ?? '').toLowerCase().includes(lower)
    || String(downloadJob.error_message ?? '').toLowerCase().includes(lower)
  );
}

export function buildFallbackHistoryByEncodeJobId(downloadJobs) {
  const byEncodeJobId = {};
  for (const dj of downloadJobs) {
    if (String(dj?.status ?? '').toLowerCase() !== 'fallback_queued') continue;
    if (dj?.encode_job_id == null) continue;
    const encodeJobId = Number(dj?.encode_job_id);
    if (!Number.isFinite(encodeJobId)) continue;
    byEncodeJobId[encodeJobId] = dj;
  }
  return byEncodeJobId;
}

export async function buildQrCodeDataUrl(value) {
  const trimmed = String(value ?? '').trim();
  if (!trimmed) return '';
  return QRCode.toDataURL(trimmed, {
    errorCorrectionLevel: 'M',
    margin: 1,
    width: 320,
  });
}

export function formatElapsed(seconds) {
  if (seconds == null || seconds < 0) return '—';
  if (seconds === 0) return '0s';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

export function normalizeDownloadJob(job) {
  if (!job || typeof job !== 'object') return job;
  const normalized = { ...job };
  normalized.status = String(job.status ?? '').toLowerCase();
  if (job.id != null) {
    const asNumber = Number(job.id);
    normalized.id = Number.isFinite(asNumber) ? asNumber : job.id;
  }
  if (job.library_id != null) {
    const asNumber = Number(job.library_id);
    normalized.library_id = Number.isFinite(asNumber) ? asNumber : job.library_id;
  }
  const progress = Number(job.progress_percent);
  const speed = Number(job.download_speed_bps);
  const hasEta = job.eta_seconds != null && Number(job.eta_seconds) >= 0;
  const queuePosition = Number(normalized.client_queue_position);
  const isSabQueuedBehindHead = (
    String(normalized.client_type ?? '').toLowerCase() === 'sabnzbd'
    && Number.isFinite(queuePosition)
    && queuePosition > 0
  );
  if (isSabQueuedBehindHead && normalized.status === 'downloading') {
    normalized.status = 'queued';
    normalized.eta_seconds = null;
    normalized.download_speed_bps = 0;
  }
  if (
    normalized.status === 'queued'
    && !isSabQueuedBehindHead
    && (
      (Number.isFinite(speed) && speed > 0)
      || hasEta
      || (Number.isFinite(progress) && progress > 0 && progress < 100)
    )
  ) {
    normalized.status = 'downloading';
  }
  return normalized;
}

export function normalizeTitleSegment(value) {
  return String(value || '').replace(/[._]/g, ' ').trim();
}

export function parseYearFromSegment(value) {
  const spaced = normalizeTitleSegment(value);
  const parenMatch = spaced.match(/\(((19|20)\d{2})\)/);
  if (parenMatch) {
    const title = spaced.slice(0, spaced.indexOf(parenMatch[0])).replace(/\s+$/, '').trim();
    return { title: title || spaced, year: parenMatch[1] };
  }
  const yearMatch = spaced.match(/\b((19|20)\d{2})\b/);
  if (yearMatch) {
    const yearIdx = spaced.indexOf(yearMatch[0]);
    const title = spaced.slice(0, yearIdx).replace(/[\s\-]+$/, '').trim();
    return { title: title || spaced, year: yearMatch[1] };
  }
  return { title: spaced, year: null };
}

export function looksLikeTvContainerSegment(value) {
  const normalized = normalizeTitleSegment(value).toLowerCase();
  return /^season\s*\d+$/i.test(normalized)
    || /^series\s*\d+$/i.test(normalized)
    || /^s\d+$/i.test(normalized)
    || normalized === 'specials';
}

export function looksLikeTvEpisodeSegment(value) {
  const normalized = normalizeTitleSegment(value).toLowerCase();
  return /\bs\d{1,2}e\d{1,3}\b/i.test(normalized)
    || /\b\d{1,2}x\d{1,3}\b/i.test(normalized);
}

export function extractEpisodeCode(value) {
  const normalized = normalizeTitleSegment(value);
  const standardMatch = normalized.match(/\b(S\d{1,2}E\d{1,3})\b/i);
  if (standardMatch) return standardMatch[1].toUpperCase();

  const altMatch = normalized.match(/\b(\d{1,2})x(\d{1,3})\b/i);
  if (!altMatch) return null;
  const season = altMatch[1].padStart(2, '0');
  const episode = altMatch[2].padStart(2, '0');
  return `S${season}E${episode}`;
}

export function extractTitleYear(filePath) {
  const pathParts = String(filePath || '').split('/').filter(Boolean);
  const fileName = pathParts[pathParts.length - 1] || '';
  const stem = fileName.replace(/\.[^.]+$/, '');
  const isTvEpisodeFile = looksLikeTvEpisodeSegment(stem);
  const parsedFile = parseYearFromSegment(stem);
  if (parsedFile.year && !isTvEpisodeFile) {
    return parsedFile;
  }

  const directParent = pathParts[pathParts.length - 2] || '';
  const showDirectory = looksLikeTvContainerSegment(directParent)
    ? (pathParts[pathParts.length - 3] || '')
    : directParent;
  if (showDirectory) {
    const parsedParent = parseYearFromSegment(showDirectory);
    if (parsedParent.year) {
      return { title: isTvEpisodeFile ? normalizeTitleSegment(stem) : parsedFile.title, year: parsedParent.year };
    }
  }

  if (isTvEpisodeFile) {
    return { title: normalizeTitleSegment(stem), year: null };
  }

  return parsedFile;
}

export function getDisplayTitle(filePath) {
  const pathParts = String(filePath || '').split('/').filter(Boolean);
  const fileName = pathParts[pathParts.length - 1] || '';
  const stem = fileName.replace(/\.[^.]+$/, '');
  if (!looksLikeTvEpisodeSegment(stem)) {
    return extractTitleYear(filePath).title || '—';
  }

  const directParent = pathParts[pathParts.length - 2] || '';
  const showDirectory = looksLikeTvContainerSegment(directParent)
    ? (pathParts[pathParts.length - 3] || '')
    : directParent;
  const parsedShow = parseYearFromSegment(showDirectory);
  const episodeCode = extractEpisodeCode(stem);
  if (parsedShow.title && episodeCode) {
    return `${parsedShow.title} ${episodeCode}`;
  }
  if (parsedShow.title) {
    return parsedShow.title;
  }
  return extractTitleYear(filePath).title || '—';
}


export function sortJobsByOption(jobs, sortOption, fallbackSort) {
  const comparatorWithPinnedInProgress = (left, right, comparator) => {
    const leftActive = ACTIVE_STATUSES.has(left.status?.toLowerCase());
    const rightActive = ACTIVE_STATUSES.has(right.status?.toLowerCase());
    if (leftActive !== rightActive) return leftActive ? -1 : 1;
    return comparator(left, right);
  };

  if (sortOption === 'newest') {
    return [...jobs].sort((a, b) => comparatorWithPinnedInProgress(a, b, compareActiveJobsNewest));
  }
  if (sortOption === 'oldest') {
    return [...jobs].sort((a, b) => comparatorWithPinnedInProgress(a, b, compareActiveJobsOldest));
  }
  if (sortOption === 'year_newest') {
    return [...jobs].sort((a, b) => comparatorWithPinnedInProgress(a, b, compareActiveJobsYearNewest));
  }
  if (sortOption === 'year_oldest') {
    return [...jobs].sort((a, b) => comparatorWithPinnedInProgress(a, b, compareActiveJobsYearOldest));
  }
  return [...jobs].sort((a, b) => comparatorWithPinnedInProgress(a, b, fallbackSort));
}

export function validateLibraryDraft(draft, libraryEnabled) {
  const errors = {};
  if (!libraryEnabled) return errors;

  if (!Number.isInteger(draft.target_resolution) || draft.target_resolution < 1) {
    errors.target_resolution = 'Target resolution must be a positive integer.';
  }
  if (!Number.isInteger(draft.minimum_source_resolution) || draft.minimum_source_resolution < 1) {
    errors.minimum_source_resolution = 'Minimum source resolution must be a positive integer.';
  } else if (!draft.hdr_only && Number.isInteger(draft.target_resolution) && draft.minimum_source_resolution <= draft.target_resolution) {
    errors.minimum_source_resolution = 'Minimum source resolution must be higher than target resolution.';
  }
  if (draft.bitrate_mode === 'vbr_crf') {
    if (!Number.isInteger(draft.crf) || draft.crf < 18 || draft.crf > 30) {
      errors.crf = 'CRF must be between 18 and 30.';
    }
  }
  if (draft.bitrate_mode === 'cbr') {
    if (!Number.isInteger(draft.bitrate_mbps) || draft.bitrate_mbps < 1) {
      errors.bitrate_mbps = 'Bitrate must be at least 1 Mbps.';
    }
  }
  if (!Number.isInteger(draft.max_workers) || draft.max_workers < 1) {
    errors.max_workers = 'Max workers must be at least 1.';
  }
  if (draft.schedule_enabled) {
    if (!Number.isInteger(draft.schedule_start_hour) || draft.schedule_start_hour < 0 || draft.schedule_start_hour > 23) {
      errors.schedule_start_hour = 'Schedule start must be between 0 and 23.';
    }
    if (!Number.isInteger(draft.schedule_end_hour) || draft.schedule_end_hour < 0 || draft.schedule_end_hour > 23) {
      errors.schedule_end_hour = 'Schedule end must be between 0 and 23.';
    }
  }
  if (draft.av1_fallback_codec === 'av1') {
    errors.av1_fallback_codec = 'AV1 fallback must be HEVC or H.264.';
  }
  const availableDownloadFallbackCodecs = getAvailableDownloadFallbackCodecs(draft.download_codec, draft.codec);
  if (draft.download_fallback_codec && !availableDownloadFallbackCodecs.includes(draft.download_fallback_codec)) {
    errors.download_fallback_codec = 'Download fallback codec must be compatible with the selected download codec preference.';
  }
  return errors;
}

export function getAvailableDownloadFallbackCodecs(downloadCodec, encodeCodec) {
  const primaryCodec = String(downloadCodec || encodeCodec || '').toLowerCase();
  if (primaryCodec === 'av1') return ['hevc', 'h264'];
  if (primaryCodec === 'hevc') return ['h264'];
  return [];
}

export function validateLibraryForm(draft) {
  const errors = {};
  if (!draft.name?.trim()) errors.name = 'Library name is required.';
  if (!draft.path?.trim()) {
    errors.path = 'Library path is required.';
  } else if (!draft.path.startsWith('/')) {
    errors.path = 'Library path must be absolute.';
  }
  return errors;
}

export function libraryDetailsHaveChanges(library, baseline) {
  if (!library || !baseline) return false;
  return library.name !== baseline.name || library.path !== baseline.path;
}

export function libraryProfileHasChanges(profile, baseline) {
  if (!profile || !baseline) return false;
  const keys = new Set([...Object.keys(profile), ...Object.keys(baseline)]);
  return [...keys].some((key) => !Object.is(profile[key], baseline[key]));
}

export function buildLibraryProfileOverview(profile, integrations = {}) {
  if (!profile) return { items: [], warnings: [] };

  const target = Number(profile.target_resolution) || 1080;
  const minimum = Number(profile.minimum_source_resolution) || 2160;
  const codec = CODEC_LABELS[profile.codec] ?? String(profile.codec || 'auto').toUpperCase();
  const hdrRequired = Boolean(profile.hdr_only || profile.tone_map_hdr);
  const items = [
    {
      label: 'Eligible media',
      value: hdrRequired
        ? `HDR sources at ${minimum}p or higher`
        : `Sources above ${target}p, starting at ${minimum}p`,
    },
    {
      label: 'Output',
      value: `${target}p ${codec}${profile.tone_map_hdr ? ' SDR (tone-mapped)' : ''}`,
    },
    {
      label: 'Route',
      value: profile.download_enabled
        ? `Search for a replacement first; encode after ${Number(profile.download_timeout_minutes) || 60} minutes`
        : 'Encode locally',
    },
    {
      label: 'Schedule',
      value: profile.schedule_enabled === false
        ? 'Any time'
        : `${String(Number(profile.schedule_start_hour) || 0).padStart(2, '0')}:00–${String(Number(profile.schedule_end_hour) || 0).padStart(2, '0')}:00`,
    },
  ];

  const warnings = [];
  if (profile.download_enabled && !integrations.prowlarrEnabled) {
    warnings.push('Prowlarr is disabled. Optimizarr will encode locally instead of searching.');
  }
  if (profile.download_enabled && !integrations.qbittorrentEnabled && !integrations.sabnzbdEnabled) {
    warnings.push('No download client is enabled. Optimizarr will encode locally instead of downloading.');
  }
  return { items, warnings };
}

export function settingsValuesEqual(left, right) {
  if (Object.is(left, right)) return true;
  if (left === null || right === null || typeof left !== 'object' || typeof right !== 'object') return false;
  if (Array.isArray(left) !== Array.isArray(right)) return false;
  const leftKeys = Object.keys(left).sort();
  const rightKeys = Object.keys(right).sort();
  if (leftKeys.length !== rightKeys.length || leftKeys.some((key, index) => key !== rightKeys[index])) return false;
  return leftKeys.every((key) => settingsValuesEqual(left[key], right[key]));
}

export function buildDashboardOperationalStatus({ queuePaused, libraries = [], queueCount = 0, workingCount = 0 }) {
  if (libraries.length === 0) {
    return { tone: 'setup', title: 'Add your first library', detail: 'Optimizarr needs a media library before it can discover work.', action: 'libraries' };
  }
  if (queuePaused) {
    return { tone: 'paused', title: 'New jobs are paused', detail: 'Current work may finish, but no new queue items will start.', action: 'jobs' };
  }
  const enabledCount = libraries.filter((library) => library.enabled).length;
  if (enabledCount === 0) {
    return { tone: 'paused', title: 'All libraries are disabled', detail: 'Enable a library to resume discovery and optimization.', action: 'libraries' };
  }
  if (workingCount > 0) {
    return { tone: 'working', title: `Processing ${workingCount} item${workingCount === 1 ? '' : 's'}`, detail: `${queueCount} total item${queueCount === 1 ? '' : 's'} currently in the queue.`, action: 'jobs' };
  }
  if (queueCount > 0) {
    return { tone: 'waiting', title: `${queueCount} item${queueCount === 1 ? '' : 's'} waiting`, detail: 'Items may be waiting for their schedule, download client, or an available worker.', action: 'jobs' };
  }
  return { tone: 'idle', title: 'Watching for eligible media', detail: `${enabledCount} enabled ${enabledCount === 1 ? 'library is' : 'libraries are'} ready for discovery.`, action: 'libraries' };
}

export function buildPaginationItems(currentPage, totalPages) {
  const total = Math.max(1, Number(totalPages) || 1);
  const current = Math.min(total, Math.max(1, Number(currentPage) || 1));
  if (total <= 7) return Array.from({ length: total }, (_, index) => index + 1);

  const pages = [...new Set([1, total, current - 1, current, current + 1].filter((page) => page >= 1 && page <= total))]
    .sort((left, right) => left - right);
  const items = [];
  for (const page of pages) {
    const previous = items[items.length - 1];
    if (typeof previous === 'number' && page - previous > 1) items.push(`ellipsis-${previous}-${page}`);
    items.push(page);
  }
  return items;
}

// ── Small reusable UI primitives ─────────────────────────────────────────────

export function loadJobsUiPrefs() {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(JOBS_UI_PREFS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function loadLibrariesUiPrefs() {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(LIBRARIES_UI_PREFS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}

export function loadSettingsUiPrefs() {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(SETTINGS_UI_PREFS_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : {};
  } catch {
    return {};
  }
}
