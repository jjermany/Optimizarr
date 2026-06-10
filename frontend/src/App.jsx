import { useEffect, useId, useMemo, useRef, useState } from 'react';
import QRCode from 'qrcode';
import {
  abortAllJobs,
  abortJob,
  bootstrapAuth,
  cancelAllQueued,
  cancelJob,
  createTotpSecret,
  clearQueue,
  removeAndResetDownloadJob,
  deleteDownloadJob,
  deleteAllDownloadJobs,
  discardJobProgress,
  createLibrary,
  deleteJob,
  deleteLibrary,
  fetchQBittorrentSettings,
  fetchSabnzbdSettings,
  fetchDownloadJobs,
  fetchLibraries,
  fetchEncoders,
  fetchAccountSettings,
  fetchAuthStatus,
  fetchLibraryProfile,
  fetchJobs,
  fetchMetrics,
  fetchNotificationSettings,
  fetchPlexLibraries,
  fetchPlexSettings,
  fetchProwlarrSettings,
  fetchDirs,
  fetchQueueStatus,
  fetchSettings,
  login as loginRequest,
  logout as logoutRequest,
  pauseJob,
  pauseQueue,
  purgeHistory,
  requeueJob,
  resumeJob,
  resumeQueue,
  retryDownloadJob,
  retryJob,
  startJob,
  runCleanup,
  runDuplicateOptimizedCleanup,
  runOptimizedCleanup,
  runRecovery,
  scanLibrary,
  sendTestNotification,
  testQBittorrentConnection,
  testSabnzbdConnection,
  testPlexConnection,
  testProwlarrConnection,
  updateQBittorrentSettings,
  updateSabnzbdSettings,
  updateLibrary,
  updateLibraryProfile,
  updateNotificationSettings,
  updateAccountSettings,
  updatePlexSettings,
  updateProwlarrSettings,
  updateSettings,
  enableAccountTwoFactor,
  disableAccountTwoFactor,
} from './api';
import StatCard from './components/StatCard';
import { buildUnifiedQueueItems } from './queueSorting';
import { isWithinWindow } from './scheduleWindow';

const WS_PATH = '/ws';
const FALLBACK_AFTER_MS = 5000;
const FALLBACK_POLL_MS = 10000;
const METRICS_POLL_MS = 10000;
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const JOBS_PAGE_SIZE = 50;
const HISTORY_PAGE_SIZE = 25;
const JOBS_UI_PREFS_KEY = 'optimizarr.jobsUiPrefs.v1';
const PROFILE_SECTIONS_DEFAULT = {
  details: false,
  processing: false,
  plex: false,
  download: false,
};

const PAGE_KEYS = {
  dashboard: 'Dashboard',
  libraries: 'Libraries',
  jobs: 'Jobs',
  settings: 'Settings',
};

const QUALITY_PRESETS = {
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

const TARGET_RESOLUTION_PRESETS = [2160, 1440, 1080, 720];
const MIN_SOURCE_RESOLUTION_PRESETS = [2160, 1440, 1080];
const CODEC_LABELS = {
  h264: 'H.264',
  hevc: 'HEVC',
  av1: 'AV1',
};

function progressFromJob(job) {
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

function formatHour(hour) {
  return `${String(hour).padStart(2, '0')}:00`;
}

function parseHour(timeValue) {
  const [hour] = timeValue.split(':');
  return Number(hour);
}

const ACTIVE_STATUSES = new Set(['starting', 'running', 'preflight', 'aborting']);
const PAUSED_STATUSES = new Set(['paused', 'paused_schedule']);
const QUEUED_STATUSES = new Set(['pending', 'queued', 'created']);
const TERMINAL_STATUSES = new Set(['complete', 'failed', 'skipped', 'cancelled']);

// Download-job status buckets
const ACTIVE_DL_STATUSES = new Set(['pending', 'searching', 'queued', 'downloading', 'moving', 'stalled', 'importing', 'waiting_encode']);
const TERMINAL_DL_STATUSES = new Set(['complete', 'failed', 'timed_out', 'fallback_queued']);
const QUEUE_DEDUPE_DL_STATUSES = new Set([...ACTIVE_DL_STATUSES, 'complete', 'fallback_queued']);
const ACTIVE_ENCODE_DEDUPE_STATUSES = new Set(['starting', 'running', 'preflight', 'aborting', 'paused', 'paused_schedule']);

function isActiveEncodeStatus(status) {
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
    return [...previousJobs, nextDownloadJob];
  }

  const previousJob = previousJobs[existingIndex];
  const previousStatus = String(previousJob?.status ?? '').toLowerCase();

  // Websocket events can arrive out of order around import completion.
  // Never allow a terminal row to regress back into an active download state.
  if (TERMINAL_DL_STATUSES.has(previousStatus) && ACTIVE_DL_STATUSES.has(nextStatus)) {
    return previousJobs;
  }

  const updated = [...previousJobs];
  updated[existingIndex] = { ...previousJob, ...nextDownloadJob };
  return updated;
}

function libraryEncodeQueueCount(library, jobs) {
  return jobs.filter((job) => isActiveLibraryEncodeQueueJob(library, job)).length;
}

function normalizeQueueIdentityTitle(titleValue) {
  return String(titleValue || '')
    .replace(/[^a-zA-Z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ')
    .toLowerCase();
}

function queueIdentityKeyForPath(pathValue) {
  const { title, year } = extractTitleYear(pathValue);
  const normalizedTitle = normalizeQueueIdentityTitle(title);
  const normalizedYear = String(year || '').trim();
  if (!normalizedTitle) return '';
  return `${normalizedTitle}::${normalizedYear}`;
}

function isLibraryEncodeJob(library, job) {
  // Prefer library_id match (accurate); fall back to path prefix for legacy data
  if (job.library_id != null) return job.library_id === library.id;
  return job.source_path === library.path || job.source_path?.startsWith(`${library.path}/`);
}

function isActiveLibraryEncodeQueueJob(library, job) {
  const status = job.status?.toLowerCase();
  if (!status || TERMINAL_STATUSES.has(status)) return false;
  return isLibraryEncodeJob(library, job);
}

function buildLibraryActiveEncodeIdentitySet(library, jobs) {
  const sourcePaths = new Set();
  const titleYearKeys = new Set();
  for (const job of jobs) {
    const status = String(job?.status ?? '').toLowerCase();
    if (!ACTIVE_ENCODE_DEDUPE_STATUSES.has(status)) continue;
    if (!isLibraryEncodeJob(library, job)) continue;
    const sourcePath = String(job?.source_path ?? '').trim();
    if (sourcePath) sourcePaths.add(sourcePath.toLowerCase());
    const identityKey = queueIdentityKeyForPath(sourcePath);
    if (identityKey) titleYearKeys.add(identityKey);
  }
  return { sourcePaths, titleYearKeys };
}

function libraryDownloadQueueCount(library, downloadJobs, activeEncodeIdentities = null) {
  return downloadJobs.filter((job) => {
    const status = String(job.status ?? '').toLowerCase();
    if (!ACTIVE_DL_STATUSES.has(status)) return false;
    if (job.library_id !== library.id) return false;
    if (status !== 'waiting_encode' || !activeEncodeIdentities) return true;
    const sourcePath = String(job.source_file_path ?? '').trim();
    if (sourcePath && activeEncodeIdentities.sourcePaths.has(sourcePath.toLowerCase())) return false;
    const identityKey = queueIdentityKeyForPath(sourcePath);
    if (identityKey && activeEncodeIdentities.titleYearKeys.has(identityKey)) return false;
    return true;
  }).length;
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
  return ['searching', 'downloading', 'moving', 'stalled', 'importing'].includes(String(status ?? '').toLowerCase());
}

function jobSortRank(job) {
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

function compareActiveJobsDefault(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  if (jobSortRank(a) === 2) return a.id - b.id;
  return b.id - a.id;
}

function compareActiveJobsNewest(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  return b.id - a.id;
}

function compareActiveJobsOldest(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  return a.id - b.id;
}

function compareActiveJobsYearNewest(a, b) {
  const rankDiff = jobSortRank(a) - jobSortRank(b);
  if (rankDiff !== 0) return rankDiff;
  const yearA = extractTitleYear(a.source_path).year;
  const yearB = extractTitleYear(b.source_path).year;
  if (yearA && yearB) return Number(yearB) - Number(yearA);
  if (yearA) return -1;
  if (yearB) return 1;
  return b.id - a.id;
}

function compareActiveJobsYearOldest(a, b) {
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

function historyItemPath(item) {
  return item._historyType === 'download' ? item.source_file_path : item.source_path;
}

function historyItemYear(item) {
  return extractTitleYear(historyItemPath(item)).year;
}

function historyItemCompletedTimestamp(item) {
  return Date.parse(item.completed_at || '') || 0;
}

function historyItemCreatedTimestamp(item) {
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

function formatResolution(height) {
  return Number.isInteger(height) ? `${height}p` : 'Unknown';
}

function formatHdrIndicator(sourceIsHdr) {
  if (sourceIsHdr === true) return 'HDR';
  if (sourceIsHdr === false) return 'SDR';
  return 'Unknown';
}

function formatEta(etaSeconds) {
  if (etaSeconds == null || etaSeconds < 0) return null;
  if (etaSeconds === 0) return 'Done';
  const h = Math.floor(etaSeconds / 3600);
  const m = Math.floor((etaSeconds % 3600) / 60);
  const s = etaSeconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function formatDownloadSpeed(bytesPerSecond) {
  const speed = Number(bytesPerSecond);
  if (!Number.isFinite(speed) || speed <= 0) return null;
  if (speed >= 1024 * 1024 * 1024) return `${(speed / (1024 * 1024 * 1024)).toFixed(2)} GB/s`;
  if (speed >= 1024 * 1024) return `${(speed / (1024 * 1024)).toFixed(2)} MB/s`;
  if (speed >= 1024) return `${(speed / 1024).toFixed(1)} KB/s`;
  return `${Math.round(speed)} B/s`;
}

function formatDownloadRetry(job) {
  const retryCount = Number(job?.retry_count);
  const maxRetries = Number(job?.max_retries);
  if (!Number.isFinite(retryCount) || !Number.isFinite(maxRetries)) return null;
  if (retryCount <= 0 || maxRetries <= 0) return null;
  return `Retry ${Math.min(retryCount, maxRetries)}/${maxRetries}`;
}

function formatDownloadClient(clientType) {
  if (clientType === 'qbittorrent') return 'qBittorrent';
  if (clientType === 'sabnzbd') return 'SABnzbd';
  return null;
}

function formatHistoryCompletedAt(value) {
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

function formatElapsed(seconds) {
  if (seconds == null || seconds < 0) return '—';
  if (seconds === 0) return '0s';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = seconds % 60;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s}s`;
  return `${s}s`;
}

function normalizeDownloadJob(job) {
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
  return normalized;
}

function normalizeTitleSegment(value) {
  return String(value || '').replace(/[._]/g, ' ').trim();
}

function parseYearFromSegment(value) {
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

function looksLikeTvContainerSegment(value) {
  const normalized = normalizeTitleSegment(value).toLowerCase();
  return /^season\s*\d+$/i.test(normalized)
    || /^series\s*\d+$/i.test(normalized)
    || /^s\d+$/i.test(normalized)
    || normalized === 'specials';
}

function looksLikeTvEpisodeSegment(value) {
  const normalized = normalizeTitleSegment(value).toLowerCase();
  return /\bs\d{1,2}e\d{1,3}\b/i.test(normalized)
    || /\b\d{1,2}x\d{1,3}\b/i.test(normalized);
}

function extractEpisodeCode(value) {
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


function sortJobsByOption(jobs, sortOption, fallbackSort) {
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

function validateLibraryDraft(draft, libraryEnabled) {
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
    if (!Number.isInteger(draft.crf) || draft.crf < 1) {
      errors.crf = 'CRF must be a positive integer.';
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

function getAvailableDownloadFallbackCodecs(downloadCodec, encodeCodec) {
  const primaryCodec = String(downloadCodec || encodeCodec || '').toLowerCase();
  if (primaryCodec === 'av1') return ['hevc', 'h264'];
  if (primaryCodec === 'hevc') return ['h264'];
  return [];
}

function validateLibraryForm(draft) {
  const errors = {};
  if (!draft.name?.trim()) errors.name = 'Library name is required.';
  if (!draft.path?.trim()) {
    errors.path = 'Library path is required.';
  } else if (!draft.path.startsWith('/')) {
    errors.path = 'Library path must be absolute.';
  }
  return errors;
}

// ── Small reusable UI primitives ─────────────────────────────────────────────

function SectionCard({ children, className = '' }) {
  return (
    <div className={`relative overflow-hidden rounded-2xl border border-slate-700/70 bg-slate-900/72 p-5 shadow-xl shadow-slate-950/45 backdrop-blur-sm ${className}`}>
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-white/[0.03] via-transparent to-transparent" />
      {children}
    </div>
  );
}

function SectionTitle({ children }) {
  return (
    <h2 className="mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-slate-400">
      <span className="inline-block h-1.5 w-1.5 rounded-full bg-cyan-300/80" />
      {children}
    </h2>
  );
}

function CollapsibleSection({ title, open, onToggle, children, divider = false }) {
  return (
    <div>
      {divider && <hr className="mb-5 border-slate-800" />}
      <button
        type="button"
        onClick={onToggle}
        className="mb-4 flex w-full items-center justify-between rounded-xl border border-slate-800/70 bg-slate-950/25 px-4 py-3 text-left transition-colors hover:bg-slate-950/40"
      >
        <SectionTitle>{title}</SectionTitle>
        <span className="text-sm text-slate-400">{open ? 'Hide' : 'Show'}</span>
      </button>
      {open && children}
    </div>
  );
}

function FormField({ label, hint, error, children, span2 = false }) {
  return (
    <label className={`flex flex-col gap-1.5 ${span2 ? 'md:col-span-2' : ''}`}>
      <span className="text-sm font-medium text-slate-200">{label}</span>
      {hint && <p className="text-xs text-slate-500">{hint}</p>}
      {children}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </label>
  );
}

function TextInput({ className = '', ...props }) {
  return (
    <input
      className={`w-full rounded-xl border border-slate-600/80 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 placeholder-slate-500/90 shadow-inner shadow-black/20 outline-none transition-all duration-150 focus:border-cyan-400/70 focus:bg-slate-900 ${className}`}
      {...props}
    />
  );
}

function SelectInput({ children, className = '', ...props }) {
  return (
    <select
      className={`w-full rounded-xl border border-slate-600/80 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 shadow-inner shadow-black/20 outline-none transition-all duration-150 focus:border-cyan-400/70 focus:bg-slate-900 ${className}`}
      {...props}
    >
      {children}
    </select>
  );
}

function Btn({ variant = 'primary', size = 'md', className = '', children, ...props }) {
  const base = 'inline-flex items-center justify-center rounded-xl font-semibold transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-offset-slate-950 disabled:opacity-50 disabled:cursor-not-allowed active:scale-[0.98]';
  const sizes = {
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
    lg: 'px-5 py-2.5 text-sm',
  };
  const variants = {
    primary: 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950 shadow-lg shadow-cyan-950/40 hover:from-cyan-300 hover:to-sky-300 focus:ring-cyan-400',
    danger: 'bg-gradient-to-r from-rose-600 to-red-500 text-white shadow-lg shadow-rose-950/45 hover:from-rose-500 hover:to-red-400 focus:ring-rose-500',
    warning: 'bg-gradient-to-r from-amber-400 to-orange-400 text-slate-950 shadow-lg shadow-amber-950/40 hover:from-amber-300 hover:to-orange-300 focus:ring-amber-400',
    success: 'bg-gradient-to-r from-emerald-400 to-teal-400 text-slate-950 shadow-lg shadow-emerald-950/40 hover:from-emerald-300 hover:to-teal-300 focus:ring-emerald-400',
    secondary: 'border border-slate-600/80 bg-slate-800/85 text-slate-100 hover:border-slate-500 hover:bg-slate-700/85 focus:ring-slate-500',
    violet: 'bg-gradient-to-r from-indigo-400 to-cyan-400 text-slate-950 shadow-lg shadow-indigo-950/40 hover:from-indigo-300 hover:to-cyan-300 focus:ring-indigo-400',
    indigo: 'bg-gradient-to-r from-blue-400 to-indigo-400 text-slate-950 shadow-lg shadow-indigo-950/40 hover:from-blue-300 hover:to-indigo-300 focus:ring-indigo-400',
  };
  return (
    <button type="button" className={`${base} ${sizes[size]} ${variants[variant]} ${className}`} {...props}>
      {children}
    </button>
  );
}

function Modal({ open, title, onClose, children }) {
  useEffect(() => {
    if (!open) return undefined;
    function onKeyDown(event) {
      if (event.key === 'Escape') onClose();
    }
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button type="button" className="absolute inset-0 bg-slate-950/85" onClick={onClose} aria-label="Close dialog" />
      <div className="relative z-10 w-full max-w-md rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl shadow-black/50">
        <div className="mb-4 flex items-center justify-between gap-3">
          <h3 className="text-sm font-semibold tracking-wide text-slate-100">{title}</h3>
          <Btn variant="secondary" size="sm" onClick={onClose}>Close</Btn>
        </div>
        {children}
      </div>
    </div>
  );
}

function MobileActionMenu({ children, label = 'Actions' }) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const containerRef = useRef(null);
  const menuPanelRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;

    function handlePointerDown(event) {
      if (!containerRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }

    function handleEscape(event) {
      if (event.key === 'Escape') {
        setOpen(false);
      }
    }

    const focusTimer = window.setTimeout(() => {
      const firstAction = menuPanelRef.current?.querySelector('button');
      if (firstAction instanceof HTMLElement) firstAction.focus();
    }, 0);

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleEscape);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [open]);

  return (
    <details
      ref={containerRef}
      open={open}
      className="group mt-3"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary
        aria-controls={menuId}
        aria-expanded={open}
        aria-label={`${label} menu`}
        className="flex cursor-pointer list-none items-center justify-between rounded-lg border border-slate-700/80 bg-slate-800/70 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:bg-slate-700/80 [&::-webkit-details-marker]:hidden"
      >
        {label}
        <span className="text-slate-400 transition-transform duration-150 group-open:rotate-180">▾</span>
      </summary>
      <div
        id={menuId}
        ref={menuPanelRef}
        role="group"
        aria-label={`${label} options`}
        className="mt-2 flex flex-wrap gap-1.5 rounded-lg border border-slate-700/70 bg-slate-900/70 p-2"
        onClickCapture={(event) => {
          if (event.target.closest('button')) setOpen(false);
        }}
      >
        {children}
      </div>
    </details>
  );
}

function FallbackIndicator() {
  return (
    <span
      title="Fallback route used. Download was attempted first; encode completed the job."
      aria-label="Fallback route used"
      className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-300"
    >
      <svg viewBox="0 0 16 16" className="h-2.5 w-2.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M3.5 4.5h4a2 2 0 0 1 2 2v5" />
        <path d="M7.5 8.5 9.5 10.5 11.5 8.5" />
        <path d="M3.5 11.5h3" />
      </svg>
    </span>
  );
}

function HistoryTypeBadge({ type }) {
  const isEncode = type === 'encode';
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ${
      isEncode
        ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300'
        : 'border-indigo-500/30 bg-indigo-500/10 text-indigo-300'
    }`}
    >
      {isEncode ? 'Encode' : 'Download'}
    </span>
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <label className="flex cursor-pointer items-center gap-3">
      <div className="relative">
        <input type="checkbox" className="sr-only" checked={checked} onChange={onChange} />
        <div className={`h-6 w-11 rounded-full border transition-colors duration-200 ${checked ? 'border-cyan-400/70 bg-cyan-500/40' : 'border-slate-600 bg-slate-700/80'}`} />
        <div className={`absolute top-1 left-1 h-4 w-4 rounded-full bg-white shadow transition-transform duration-200 ${checked ? 'translate-x-5' : 'translate-x-0'}`} />
      </div>
      {label && <span className="text-sm text-slate-200">{label}</span>}
    </label>
  );
}

function StatusDot({ status }) {
  const colors = {
    online: 'bg-emerald-400 shadow-emerald-400/50',
    reconnecting: 'bg-amber-400 shadow-amber-400/50',
    offline: 'bg-red-400 shadow-red-400/50',
    connecting: 'bg-slate-400 shadow-slate-400/50',
  };
  const labels = {
    online: 'Live',
    reconnecting: 'Reconnecting',
    offline: 'Offline (polling)',
    connecting: 'Connecting',
  };
  return (
    <div role="status" aria-live="polite" aria-label={`Connection status: ${labels[status] ?? status}`} className="flex items-center gap-2 rounded-full border border-slate-700/70 bg-slate-900/70 px-2.5 py-1">
      <span className={`inline-block h-2 w-2 rounded-full shadow-sm ${colors[status] ?? colors.connecting} ${status === 'online' ? 'animate-pulse' : ''}`} />
      <span className="text-xs text-slate-400">{labels[status] ?? status}</span>
    </div>
  );
}

function DirBrowserModal({ open, initialPath, onSelect, onClose }) {
  const [current, setCurrent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  async function navigate(path) {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchDirs(path ?? undefined);
      setCurrent(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open) return;
    // Try the saved path first; if it's outside MEDIA_ROOT or doesn't exist, fall back to root
    if (initialPath) {
      fetchDirs(initialPath)
        .then((data) => { setCurrent(data); setError(null); })
        .catch(() => navigate(null));
    } else {
      navigate(null);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 shadow-2xl" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <h3 className="text-sm font-semibold text-slate-200">Browse Directories</h3>
          <button type="button" className="text-slate-400 hover:text-slate-200 text-lg leading-none" onClick={onClose}>✕</button>
        </div>

        {/* Current path */}
        <div className="flex items-center gap-2 border-b border-slate-800/60 px-4 py-2">
          {current?.parent && (
            <button type="button" onClick={() => navigate(current.parent)} className="shrink-0 rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-slate-200">
              ← Up
            </button>
          )}
          <span className="min-w-0 truncate font-mono text-xs text-slate-400" title={current?.path}>{current?.path ?? '…'}</span>
        </div>

        {/* Directory listing */}
        <div className="max-h-72 overflow-y-auto px-2 py-2">
          {loading && <p className="px-2 py-3 text-sm text-slate-400">Loading…</p>}
          {error && <p className="px-2 py-3 text-sm text-red-400">{error}</p>}
          {!loading && !error && current && current.dirs.length === 0 && (
            <p className="px-2 py-3 text-xs text-slate-500">No subdirectories here.</p>
          )}
          {!loading && !error && current && current.dirs.map((dir) => (
            <button
              key={dir}
              type="button"
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm text-slate-100 hover:bg-slate-800 active:bg-slate-700"
              onClick={() => navigate(`${current.path}/${dir}`)}
            >
              <svg className="h-4 w-4 shrink-0 text-cyan-400" fill="currentColor" viewBox="0 0 20 20"><path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/></svg>
              {dir}
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t border-slate-800 px-4 py-3">
          <Btn variant="secondary" size="sm" onClick={onClose}>Cancel</Btn>
          <Btn variant="primary" size="sm" disabled={!current} onClick={() => { onSelect(current.path); onClose(); }}>
            Select This Folder
          </Btn>
        </div>
      </div>
    </div>
  );
}

// ── Main App ─────────────────────────────────────────────────────────────────

const VALID_PAGES = new Set(Object.keys(PAGE_KEYS));

function pageFromHash() {
  const hash = window.location.hash.slice(1);
  return VALID_PAGES.has(hash) ? hash : 'dashboard';
}

function loadJobsUiPrefs() {
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

export default function App() {
  const jobsUiPrefs = loadJobsUiPrefs();
  const [activePage, setActivePage] = useState(pageFromHash);
  const [metrics, setMetrics] = useState();
  const [jobs, setJobs] = useState([]);
  const [libraries, setLibraries] = useState([]);
  const [libraryProfiles, setLibraryProfiles] = useState({});
  const [settings, setSettings] = useState();
  const [accountSettings, setAccountSettings] = useState();
  const [accountForm, setAccountForm] = useState({
    username: '',
    currentPassword: '',
    newPassword: '',
    confirmNewPassword: '',
  });
  const [savingAccountSettings, setSavingAccountSettings] = useState(false);
  const [enablingTwoFactor, setEnablingTwoFactor] = useState(false);
  const [disablingTwoFactor, setDisablingTwoFactor] = useState(false);
  const [generatingAccountTotpSecret, setGeneratingAccountTotpSecret] = useState(false);
  const [accountTwoFactorDraft, setAccountTwoFactorDraft] = useState({
    totpSecret: '',
    totpUri: '',
    totpCode: '',
    currentPassword: '',
  });
  const [accountDisableTwoFactorDraft, setAccountDisableTwoFactorDraft] = useState({
    currentPassword: '',
    totpCode: '',
  });
  const [notificationSettings, setNotificationSettings] = useState();
  const [plexSettings, setPlexSettings] = useState();
  const [plexLibraries, setPlexLibraries] = useState([]);
  const [loadingPlexLibraries, setLoadingPlexLibraries] = useState(false);
  const [savingPlexSettings, setSavingPlexSettings] = useState(false);
  const [testingPlexConnection, setTestingPlexConnection] = useState(false);
  const [prowlarrSettings, setProwlarrSettings] = useState();
  const [savingProwlarrSettings, setSavingProwlarrSettings] = useState(false);
  const [testingProwlarrConnection, setTestingProwlarrConnection] = useState(false);
  const [qbtSettings, setQbtSettings] = useState();
  const [savingQbtSettings, setSavingQbtSettings] = useState(false);
  const [testingQbtConnection, setTestingQbtConnection] = useState(false);
  const [sabSettings, setSabSettings] = useState();
  const [savingSabSettings, setSavingSabSettings] = useState(false);
  const [testingSabConnection, setTestingSabConnection] = useState(false);
  const [downloadJobs, setDownloadJobs] = useState([]);
  const [dirBrowser, setDirBrowser] = useState({ open: false, target: null, initialPath: null });
  const [selectedLibraryId, setSelectedLibraryId] = useState(null);
  const [profileDraft, setProfileDraft] = useState(null);
  const [profileSectionsOpen, setProfileSectionsOpen] = useState(() => ({
    ...PROFILE_SECTIONS_DEFAULT,
    ...(jobsUiPrefs.profileSectionsOpen ?? {}),
  }));
  const [targetResolutionCustom, setTargetResolutionCustom] = useState(false);
  const [minimumResolutionCustom, setMinimumResolutionCustom] = useState(false);
  const [profileErrors, setProfileErrors] = useState({});
  const [selectedPreset, setSelectedPreset] = useState('balanced');
  const [savingProfile, setSavingProfile] = useState(false);
  const [libraryDraft, setLibraryDraft] = useState({ name: '', path: '/data/', enabled: true });
  const [libraryFormErrors, setLibraryFormErrors] = useState({});
  const [savingLibrary, setSavingLibrary] = useState(false);
  const [deletingLibraryId, setDeletingLibraryId] = useState(null);
  const [savingSettings, setSavingSettings] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState('connecting');
  const [fallbackPollingEnabled, setFallbackPollingEnabled] = useState(false);
  const [queuePaused, setQueuePaused] = useState(false);
  const [toasts, setToasts] = useState([]);
  const [authStatus, setAuthStatus] = useState({ loading: true, setup_required: false, authenticated: false, username: null, two_factor_enabled: false });
  const [setupForm, setSetupForm] = useState({
    username: 'admin',
    bootstrapToken: '',
    password: '',
    confirmPassword: '',
    enableTwoFactor: false,
    totpSecret: '',
    totpUri: '',
    totpCode: '',
  });
  const [setupBusy, setSetupBusy] = useState(false);
  const [setupError, setSetupError] = useState(null);
  const [setupSecretBusy, setSetupSecretBusy] = useState(false);
  const [qrModal, setQrModal] = useState({ open: false, title: '', subtitle: '', secret: '', otpauthUrl: '' });
  const [qrImageDataUrl, setQrImageDataUrl] = useState('');
  const [qrImageBusy, setQrImageBusy] = useState(false);
  const [qrImageError, setQrImageError] = useState(null);
  const [loginForm, setLoginForm] = useState({ username: '', password: '', otpCode: '' });
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginError, setLoginError] = useState(null);
  const [availableEncodersByCodec, setAvailableEncodersByCodec] = useState({});
  const [jobsPage, setJobsPage] = useState(1);
  const [historyPage, setHistoryPage] = useState(1);
  const [queueSearch, setQueueSearch] = useState(() => String(jobsUiPrefs.queueSearch ?? ''));
  const [historySearch, setHistorySearch] = useState(() => String(jobsUiPrefs.historySearch ?? ''));
  const [queueSort, setQueueSort] = useState(() => {
    const val = String(jobsUiPrefs.queueSort ?? 'default');
    return val === 'oldest' ? 'default' : val;
  });
  const [historySort, setHistorySort] = useState(() => {
    const val = String(jobsUiPrefs.historySort ?? 'completed_desc');
    if (val === 'year_desc') return 'year_newest';
    if (val === 'year_asc') return 'year_oldest';
    return val;
  });
  const [historyTypeFilter, setHistoryTypeFilter] = useState(() => {
    const val = String(jobsUiPrefs.historyTypeFilter ?? 'all');
    return ['all', 'encode', 'download'].includes(val) ? val : 'all';
  });
  const [jobsView, setJobsView] = useState(() => (jobsUiPrefs.jobsView === 'history' ? 'history' : 'queue'));
  const [nowHour, setNowHour] = useState(() => new Date().getHours());
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [pendingJobActions, setPendingJobActions] = useState({});
  const [pendingDownloadActions, setPendingDownloadActions] = useState({});

  const wsRef = useRef();
  const downloadReconcileInFlightRef = useRef(false);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef();
  const fallbackTimerRef = useRef();
  const intentionallyClosedRef = useRef(false);
  const toastTimersRef = useRef({});

  // Build a lookup map: library id → library name
  const libraryById = useMemo(
    () => Object.fromEntries(libraries.map((lib) => [lib.id, lib])),
    [libraries],
  );

  // Build a lookup map: source_file_path → most-recent active download job
  const downloadJobBySource = useMemo(() => {
    const map = {};
    for (const dj of downloadJobs) {
      map[dj.source_file_path] = dj;
    }
    return map;
  }, [downloadJobs]);

  // Active queue jobs (non-terminal)
  const activeJobs = useMemo(
    () => jobs.filter((job) => !TERMINAL_STATUSES.has(job.status?.toLowerCase())),
    [jobs],
  );

  // Terminal/history jobs
  const historyJobs = useMemo(
    () => jobs.filter((job) => TERMINAL_STATUSES.has(job.status?.toLowerCase())),
    [jobs],
  );

  const sortedActiveJobs = useMemo(
    () => sortJobsByOption(activeJobs, queueSort, compareActiveJobsDefault),
    [activeJobs, queueSort],
  );

  function jobMatchesSearch(job, search) {
    if (!search) return true;
    const lower = search.toLowerCase();
    const { title, year } = extractTitleYear(job.source_path);
    const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '') : '';
    return (
      title.toLowerCase().includes(lower)
      || (year && year.includes(lower))
      || libName.toLowerCase().includes(lower)
      || job.source_path?.toLowerCase().includes(lower)
      || String(job.id).includes(lower)
    );
  }

  const filteredActiveJobs = useMemo(
    () => sortedActiveJobs.filter((job) => jobMatchesSearch(job, queueSearch)),
    [sortedActiveJobs, queueSearch, libraryById],
  );

  const fallbackHistoryByEncodeJobId = useMemo(
    () => buildFallbackHistoryByEncodeJobId(downloadJobs),
    [downloadJobs],
  );

  const terminalDownloadHistoryJobs = useMemo(
    () => downloadJobs.filter((dj) => {
      const status = String(dj.status ?? '').toLowerCase();
      return TERMINAL_DL_STATUSES.has(status) && status !== 'fallback_queued';
    }),
    [downloadJobs],
  );

  const allHistoryItems = useMemo(
    () => buildUnifiedHistoryItems(historyJobs, terminalDownloadHistoryJobs),
    [historyJobs, terminalDownloadHistoryJobs],
  );

  const filteredHistoryItems = useMemo(() => {
    return allHistoryItems
      .filter((item) => historyTypeFilter === 'all' || item._historyType === historyTypeFilter)
      .filter((item) => (
        item._historyType === 'download'
          ? downloadJobMatchesSearch(item, historySearch, libraryById)
          : jobMatchesSearch(item, historySearch)
      ));
  }, [allHistoryItems, historyTypeFilter, historySearch, libraryById]);

  const sortedHistoryItems = useMemo(
    () => [...filteredHistoryItems].sort((a, b) => compareHistoryItemsByOption(a, b, historySort)),
    [filteredHistoryItems, historySort],
  );

  const totalHistoryCount = allHistoryItems.length;
  const visibleHistoryCount = sortedHistoryItems.length;

  const activeDlQueueItems = useMemo(
    () => downloadJobs.filter((dj) => ACTIVE_DL_STATUSES.has(String(dj.status ?? '').toLowerCase())),
    [downloadJobs],
  );

  const queueDedupeSourcePaths = useMemo(() => {
    const paths = new Set();
    for (const dj of downloadJobs) {
      const status = String(dj.status ?? '').toLowerCase();
      if (!QUEUE_DEDUPE_DL_STATUSES.has(status)) continue;
      const sourcePath = String(dj.source_file_path ?? '').trim();
      if (sourcePath) paths.add(sourcePath);
    }
    return paths;
  }, [downloadJobs]);

  // Active download jobs filtered by search, tagged as 'download' type for unified queue
  const filteredDlQueueItems = useMemo(() => {
    const dlSearch = queueSearch.toLowerCase();
    return activeDlQueueItems
      .filter((dj) => {
        if (!dlSearch) return true;
        const { title, year } = extractTitleYear(dj.source_file_path);
        const libName = dj.library_id != null ? (libraryById[dj.library_id]?.name ?? '') : '';
        return (
          title.toLowerCase().includes(dlSearch)
          || (year && year.includes(dlSearch))
          || libName.toLowerCase().includes(dlSearch)
          || dj.source_file_path?.toLowerCase().includes(dlSearch)
          || String(dj.id).includes(dlSearch)
        );
      });
  }, [activeDlQueueItems, queueSearch, libraryById]);

  const unifiedAllQueueItems = useMemo(
    () => buildUnifiedQueueItems({
      encodeItems: activeJobs,
      downloadItems: activeDlQueueItems,
      sortOption: queueSort,
      extractTitleYear,
      pinActiveFirst: true,
      dedupeSourcePaths: queueDedupeSourcePaths,
    }),
    [activeJobs, activeDlQueueItems, queueSort, queueDedupeSourcePaths],
  );

  // Unified queue: encoding jobs + active download jobs using one comparator path
  const unifiedQueueItems = useMemo(
    () => buildUnifiedQueueItems({
      encodeItems: filteredActiveJobs,
      downloadItems: filteredDlQueueItems,
      sortOption: queueSort,
      extractTitleYear,
      pinActiveFirst: true,
      dedupeSourcePaths: queueDedupeSourcePaths,
    }),
    [filteredActiveJobs, filteredDlQueueItems, queueSort, queueDedupeSourcePaths],
  );

  const queueCount = unifiedAllQueueItems.length;

  // All queue items in a single list: active jobs sort to the top, rest follow.
  const paginatedQueueItems = useMemo(() => unifiedQueueItems, [unifiedQueueItems]);

  const totalJobPages = useMemo(
    () => Math.max(1, Math.ceil(paginatedQueueItems.length / JOBS_PAGE_SIZE)),
    [paginatedQueueItems.length],
  );

  const totalHistoryPages = useMemo(
    () => Math.max(1, Math.ceil(sortedHistoryItems.length / HISTORY_PAGE_SIZE)),
    [sortedHistoryItems.length],
  );

  const pagedJobs = useMemo(() => {
    const start = (jobsPage - 1) * JOBS_PAGE_SIZE;
    return paginatedQueueItems.slice(start, start + JOBS_PAGE_SIZE);
  }, [paginatedQueueItems, jobsPage]);

  const pagedHistoryItems = useMemo(() => {
    const start = (historyPage - 1) * HISTORY_PAGE_SIZE;
    return sortedHistoryItems.slice(start, start + HISTORY_PAGE_SIZE);
  }, [sortedHistoryItems, historyPage]);

  useEffect(() => {
    if (jobsPage > totalJobPages) setJobsPage(totalJobPages);
  }, [jobsPage, totalJobPages]);

  useEffect(() => {
    setJobsPage(1);
  }, [queueSort]);

  useEffect(() => {
    if (historyPage > totalHistoryPages) setHistoryPage(totalHistoryPages);
  }, [historyPage, totalHistoryPages]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(
      JOBS_UI_PREFS_KEY,
      JSON.stringify({
        queueSearch,
        historySearch,
        queueSort,
        historySort,
        historyTypeFilter,
        jobsView,
        profileSectionsOpen,
      }),
    );
  }, [queueSearch, historySearch, queueSort, historySort, historyTypeFilter, jobsView, profileSectionsOpen]);

  const selectedLibrary = useMemo(
    () => libraries.find((library) => library.id === selectedLibraryId) ?? null,
    [libraries, selectedLibraryId],
  );

  const selectedLibraryProfile = selectedLibraryId ? libraryProfiles[selectedLibraryId] : null;

  const libraryRuntimeStates = useMemo(() => {
    return libraries.map((library) => {
      const profile = libraryProfiles[library.id];
      let state = 'Running';
      if (!library.enabled) {
        state = 'Paused (library disabled)';
      } else if (queuePaused) {
        state = 'Paused (queue paused)';
      } else if (
        profile
        && profile.schedule_enabled !== false
        && !isWithinWindow(nowHour, profile.schedule_start_hour, profile.schedule_end_hour)
      ) {
        state = 'Paused by schedule';
      }
      return { library, state, queue: libraryQueueCount(library, jobs, downloadJobs) };
    });
  }, [jobs, downloadJobs, libraries, libraryProfiles, nowHour, queuePaused]);

  async function refreshAuthStatus() {
    try {
      const statusPayload = await fetchAuthStatus();
      setAuthStatus({ loading: false, ...statusPayload });
      if (statusPayload?.authenticated && statusPayload?.username) {
        setLoginForm((prev) => ({ ...prev, username: statusPayload.username }));
      }
      return statusPayload;
    } catch {
      setAuthStatus({ loading: false, setup_required: false, authenticated: false, username: null, two_factor_enabled: false });
      return null;
    }
  }

  async function handleGenerateSetupSecret() {
    const username = setupForm.username.trim();
    if (!username) {
      setSetupError('Username is required to generate a 2FA secret.');
      return;
    }
    setSetupSecretBusy(true);
    setSetupError(null);
    try {
      const payload = await createTotpSecret(username);
      setSetupForm((prev) => ({ ...prev, totpSecret: payload.secret, totpUri: payload.otpauth_url || '' }));
    } catch (error) {
      setSetupError(error.message || 'Failed to generate a TOTP secret.');
    } finally {
      setSetupSecretBusy(false);
    }
  }

  function openQrModal({ title, subtitle, secret, otpauthUrl }) {
    setQrModal({
      open: true,
      title,
      subtitle,
      secret: secret || '',
      otpauthUrl: otpauthUrl || '',
    });
  }

  function closeQrModal() {
    setQrModal({ open: false, title: '', subtitle: '', secret: '', otpauthUrl: '' });
    setQrImageDataUrl('');
    setQrImageError(null);
    setQrImageBusy(false);
  }

  async function openSetupQrCode() {
    const username = setupForm.username.trim();
    if (!username) {
      setSetupError('Username is required before creating a QR code.');
      return;
    }

    setSetupSecretBusy(true);
    setSetupError(null);
    try {
      const shouldGenerate = !setupForm.totpSecret.trim() || !setupForm.totpUri.trim();
      let secret = setupForm.totpSecret.trim();
      let otpauthUrl = setupForm.totpUri.trim();
      if (shouldGenerate) {
        const payload = await createTotpSecret(username);
        secret = payload.secret;
        otpauthUrl = payload.otpauth_url || '';
        setSetupForm((prev) => ({ ...prev, totpSecret: secret, totpUri: otpauthUrl }));
      }
      openQrModal({
        title: 'Scan QR Code',
        subtitle: 'Use your authenticator app to scan this code, then enter the current 6-digit code below.',
        secret,
        otpauthUrl,
      });
    } catch (error) {
      setSetupError(error.message || 'Failed to generate a 2FA QR code.');
    } finally {
      setSetupSecretBusy(false);
    }
  }

  async function handleBootstrapSubmit(event) {
    event.preventDefault();
    setSetupError(null);

    if (setupForm.password !== setupForm.confirmPassword) {
      setSetupError('Passwords do not match.');
      return;
    }
    if (!setupForm.bootstrapToken.trim()) {
      setSetupError('Enter the setup token from your server logs or OPTIMIZARR_BOOTSTRAP_TOKEN.');
      return;
    }
    if (setupForm.password.length < 12) {
      setSetupError('Password must be at least 12 characters.');
      return;
    }
    if (setupForm.enableTwoFactor && !setupForm.totpSecret.trim()) {
      setSetupError('Generate a 2FA secret before enabling dual-factor authentication.');
      return;
    }
    if (setupForm.enableTwoFactor && !setupForm.totpCode.trim()) {
      setSetupError('Enter a 2FA code from your authenticator app.');
      return;
    }

    setSetupBusy(true);
    try {
      await bootstrapAuth({
        username: setupForm.username.trim(),
        bootstrap_token: setupForm.bootstrapToken.trim(),
        password: setupForm.password,
        enable_two_factor: setupForm.enableTwoFactor,
        totp_secret: setupForm.enableTwoFactor ? setupForm.totpSecret.trim() : null,
        totp_code: setupForm.enableTwoFactor ? setupForm.totpCode.trim() : null,
      });
      await refreshAuthStatus();
      setSetupForm((prev) => ({ ...prev, password: '', confirmPassword: '', totpCode: '' }));
    } catch (error) {
      setSetupError(error.message || 'Failed to create admin account.');
    } finally {
      setSetupBusy(false);
    }
  }

  async function handleLoginSubmit(event) {
    event.preventDefault();
    setLoginError(null);
    setLoginBusy(true);
    try {
      await loginRequest({
        username: loginForm.username.trim(),
        password: loginForm.password,
        otp_code: loginForm.otpCode.trim() || undefined,
      });
      await refreshAuthStatus();
      setLoginForm((prev) => ({ ...prev, password: '', otpCode: '' }));
    } catch (error) {
      setLoginError(error.message || 'Login failed.');
    } finally {
      setLoginBusy(false);
    }
  }

  async function handleLogout() {
    try {
      await logoutRequest();
    } finally {
      setAuthStatus({ loading: false, setup_required: false, authenticated: false, username: null, two_factor_enabled: false });
      setConnectionStatus('offline');
      setFallbackPollingEnabled(false);
    }
  }

  async function refreshAll() {
    try {
      const [nextMetrics, nextJobs, nextSettings, nextAccountSettings, nextNotificationSettings, nextPlexSettings, nextEncoders, nextQueueStatus, nextProwlarrSettings, nextQbtSettings, nextSabSettings, nextDownloadJobs] = await Promise.all([
        fetchMetrics(),
        fetchJobs(),
        fetchSettings(),
        fetchAccountSettings(),
        fetchNotificationSettings(),
        fetchPlexSettings(),
        fetchEncoders(),
        fetchQueueStatus(),
        fetchProwlarrSettings().catch(() => null),
        fetchQBittorrentSettings().catch(() => null),
        fetchSabnzbdSettings().catch(() => null),
        fetchDownloadJobs().catch(() => []),
      ]);
      setMetrics(nextMetrics);
      setJobs(nextJobs);
      setSettings(nextSettings);
      setAccountSettings(nextAccountSettings);
      setNotificationSettings(nextNotificationSettings);
      setPlexSettings(nextPlexSettings);
      if (nextProwlarrSettings) setProwlarrSettings(nextProwlarrSettings);
      if (nextQbtSettings) setQbtSettings(nextQbtSettings);
      if (nextSabSettings) setSabSettings(nextSabSettings);
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      const encoderMap = Object.fromEntries((nextEncoders?.encoders ?? []).map((item) => [item.codec, item.available_encoders]));
      setAvailableEncodersByCodec(encoderMap);
      setQueuePaused(nextQueueStatus?.status === 'paused');
      setAuthStatus((prev) => ({
        ...prev,
        username: nextAccountSettings?.username ?? prev.username,
        two_factor_enabled: !!nextAccountSettings?.two_factor_enabled,
      }));
      if (nextPlexSettings?.enabled && nextPlexSettings?.token) {
        fetchPlexLibraries().then((sections) => setPlexLibraries(sections ?? [])).catch(() => {});
      }
    } catch (refreshError) {
      if (refreshError.status === 401) {
        await refreshAuthStatus();
        return;
      }
      pushToast(refreshError.message || 'Could not refresh data.', 'error');
    }
  }



  async function handleQueueSortChange(nextSort) {
    setQueueSort(nextSort);
    setJobsPage(1);
    try {
      const updated = await updateSettings({ queue_sort: nextSort });
      setSettings(updated);
    } catch (err) {
      pushToast(err.message || 'Failed to update queue sort order.', 'error');
    }
  }

  async function refreshLibrariesAndProfiles() {
    try {
      const nextLibraries = await fetchLibraries();
      setLibraries(nextLibraries);
      setSelectedLibraryId((prevSelectedLibraryId) => {
        if (nextLibraries.length === 0) return null;
        if (prevSelectedLibraryId == null) return nextLibraries[0].id;
        const stillExists = nextLibraries.some((library) => library.id === prevSelectedLibraryId);
        return stillExists ? prevSelectedLibraryId : nextLibraries[0].id;
      });
      const profileEntries = await Promise.all(
        nextLibraries.map(async (library) => {
          const profile = await fetchLibraryProfile(library.id);
          return [library.id, profile];
        }),
      );
      setLibraryProfiles(Object.fromEntries(profileEntries));
    } catch (refreshError) {
      pushToast(refreshError.message || 'Could not refresh libraries.', 'error');
    }
  }

  function navigate(page) {
    window.location.hash = page;
    setActivePage(page);
  }

  function pushToast(messageText, tone = 'info', options = {}) {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setToasts((prev) => [...prev, { id, message: messageText, tone }]);
    const toneDurationMs = tone === 'error'
      ? 7000
      : tone === 'warn'
        ? 6500
        : tone === 'success'
          ? 6500
          : 5000;
    const durationMs = Number.isFinite(options.durationMs) ? Math.max(1500, options.durationMs) : toneDurationMs;
    const timer = window.setTimeout(() => {
      setToasts((prev) => prev.filter((toast) => toast.id !== id));
      delete toastTimersRef.current[id];
    }, durationMs);
    toastTimersRef.current[id] = timer;
  }

  function mergeJobUpdate(nextJob) {
    let resetToFirstPage = false;
    setJobs((prevJobs) => {
      const merged = mergeJobsWithUpdate(prevJobs, nextJob);
      resetToFirstPage = merged.resetToFirstPage;
      return merged.jobs;
    });
    if (resetToFirstPage) setJobsPage(1);
  }

  function wsUrlWithToken() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const base = import.meta.env.VITE_API_BASE ?? '';
    if (base) {
      const normalizedBase = base.startsWith('http') ? base : `${window.location.origin}${base}`;
      const url = new URL(`${normalizedBase}${WS_PATH}`);
      return url.toString().replace(/^http/, 'ws');
    }
    const url = new URL(`${protocol}//${window.location.host}${WS_PATH}`);
    return url.toString();
  }


  useEffect(() => {
    if (!settings?.queue_sort) return;
    setQueueSort((prev) => (prev === settings.queue_sort ? prev : settings.queue_sort));
    setJobsPage(1);
  }, [settings?.queue_sort]);

  useEffect(() => {
    refreshAuthStatus();
  }, []);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return;
    refreshAll();
    refreshLibrariesAndProfiles();
  }, [authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  useEffect(() => {
    const timer = setInterval(() => setNowHour(new Date().getHours()), 60_000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    function onHashChange() { setActivePage(pageFromHash()); }
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, []);

  useEffect(() => {
    if (!selectedLibraryId || !selectedLibraryProfile) {
      setProfileDraft(null);
      setTargetResolutionCustom(false);
      setMinimumResolutionCustom(false);
      setProfileErrors({});
      return;
    }
    const nextDraft = {
      schedule_enabled: true,
      preferred_video_encoder: 'auto',
      hdr_only: true,
      tone_map_hdr: false,
      bitrate_mode: 'vbr_crf',
      crf: 23,
      minimum_source_resolution: 2160,
      download_codec: null,
      download_fallback_codec: null,
      ...selectedLibraryProfile,
    };
    if (nextDraft.bitrate_mode === 'vbr_crf' && !Number.isInteger(nextDraft.crf)) {
      nextDraft.crf = 23;
    }
    setProfileDraft(nextDraft);
    setTargetResolutionCustom(!TARGET_RESOLUTION_PRESETS.includes(Number(nextDraft.target_resolution)));
    setMinimumResolutionCustom(!MIN_SOURCE_RESOLUTION_PRESETS.includes(Number(nextDraft.minimum_source_resolution)));
    setProfileErrors({});
  }, [selectedLibraryId, selectedLibraryProfile]);

  useEffect(() => {
    if (!accountSettings) return;
    setAccountForm((prev) => ({
      ...prev,
      username: accountSettings.username ?? '',
    }));
  }, [accountSettings?.username]);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return undefined;
    if (!fallbackPollingEnabled) return undefined;
    const timer = setInterval(refreshAll, FALLBACK_POLL_MS);
    return () => clearInterval(timer);
  }, [fallbackPollingEnabled, authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return undefined;
    const activeDownloadCount = downloadJobs.filter((dj) => ACTIVE_DL_STATUSES.has(String(dj.status ?? '').toLowerCase())).length;
    if (activeDownloadCount <= 1) return undefined;
    if (downloadReconcileInFlightRef.current) return undefined;

    const timer = setTimeout(async () => {
      if (downloadReconcileInFlightRef.current) return;
      downloadReconcileInFlightRef.current = true;
      try {
        const updated = await fetchDownloadJobs();
        setDownloadJobs((updated ?? []).map(normalizeDownloadJob));
      } catch {
        // Keep websocket-driven state; best-effort self-heal.
      } finally {
        downloadReconcileInFlightRef.current = false;
      }
    }, 300);

    return () => clearTimeout(timer);
  }, [downloadJobs, authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  // When jobs or downloads are actively processing, poll every 5 s so status changes
  // (e.g. queued → running) are visible quickly even if a WebSocket update
  // is missed or the connection is still establishing.
  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return undefined;
    const hasActiveDownloads = downloadJobs.some((dj) => ACTIVE_DL_STATUSES.has(String(dj.status ?? '').toLowerCase()));
    if (activeJobs.length === 0 && !hasActiveDownloads) return undefined;
    const timer = setInterval(refreshAll, 5000);
    return () => clearInterval(timer);
  }, [activeJobs.length, downloadJobs, authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return undefined;
    const timer = setInterval(async () => {
      try {
        const nextMetrics = await fetchMetrics();
        if (nextMetrics) setMetrics(nextMetrics);
      } catch {
        // WebSocket is still the primary path; polling is best-effort resilience.
      }
    }, METRICS_POLL_MS);

    return () => clearInterval(timer);
  }, [authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  useEffect(() => {
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return undefined;
    intentionallyClosedRef.current = false;

    function clearTimers() {
      if (reconnectTimerRef.current) { clearTimeout(reconnectTimerRef.current); reconnectTimerRef.current = undefined; }
      if (fallbackTimerRef.current) { clearTimeout(fallbackTimerRef.current); fallbackTimerRef.current = undefined; }
    }

    function scheduleFallbackPolling() {
      if (fallbackTimerRef.current) return;
      fallbackTimerRef.current = setTimeout(() => {
        setFallbackPollingEnabled(true);
        setConnectionStatus('offline');
      }, FALLBACK_AFTER_MS);
    }

    function scheduleReconnect(connectFn) {
      if (intentionallyClosedRef.current) return;
      reconnectAttemptsRef.current += 1;
      const delay = Math.min(RECONNECT_BASE_DELAY_MS * (2 ** (reconnectAttemptsRef.current - 1)), RECONNECT_MAX_DELAY_MS);
      setConnectionStatus('reconnecting');
      reconnectTimerRef.current = setTimeout(connectFn, delay);
    }

    async function connectWebSocket() {
      try {
        const websocket = new WebSocket(wsUrlWithToken());
        wsRef.current = websocket;

        websocket.onopen = () => {
          reconnectAttemptsRef.current = 0;
          setConnectionStatus('online');
          setFallbackPollingEnabled(false);
          if (fallbackTimerRef.current) { clearTimeout(fallbackTimerRef.current); fallbackTimerRef.current = undefined; }
          refreshAll();
        };

        websocket.onmessage = (event) => {
          const payload = JSON.parse(event.data);
          if (payload.type === 'job_update') { mergeJobUpdate(payload.data); return; }
          if (payload.type === 'download_job_update') {
            setDownloadJobs((prev) => {
              const next = normalizeDownloadJob(payload.data);
              return mergeDownloadJobsWithUpdate(prev, next);
            });
            return;
          }
          if (payload.type === 'metrics_update') { setMetrics(payload.data); return; }
          if (payload.type === 'library_update') { refreshLibrariesAndProfiles(); return; }
          if (payload.type === 'notification') {
            if (payload.data?.message === 'queue_paused_low_disk') pushToast('Queue paused due to low disk.', 'warn');
            return;
          }
          if (payload.type === 'system_event') {
            if (payload.data?.event === 'job_aborted') { pushToast('Aborted job.', 'error'); return; }
            if (payload.data?.event === 'download_job_removed') {
              setDownloadJobs((prev) => prev.filter((dj) => dj.id !== payload.data?.download_job_id));
              return;
            }
            if (payload.data?.event === 'queue_paused') {
              setQueuePaused(true);
              if (payload.data?.reason === 'low_disk') { pushToast('Queue paused due to low disk.', 'warn'); return; }
              if (payload.data?.reason !== 'manual_scan') { pushToast('Queue paused.', 'warn'); }
              return;
            }
            if (payload.data?.event === 'queue_resumed') {
              setQueuePaused(false);
              return;
            }
            if (payload.data?.event === 'library_scan_started') {
              setLibraries((prev) => prev.map((library) => (
                library.id === payload.data?.library_id ? { ...library, scanning: true } : library
              )));
              return;
            }
            if (payload.data?.event === 'library_scan_completed') {
              setLibraries((prev) => prev.map((library) => (
                library.id === payload.data?.library_id ? { ...library, scanning: false } : library
              )));
              return;
            }
            if (payload.data?.event === 'recovery_summary' && payload.data?.trigger === 'startup') pushToast('Recovery ran on startup.', 'info');
          }
        };

        websocket.onerror = () => websocket.close();
        websocket.onclose = () => {
          if (intentionallyClosedRef.current) return;
          if (wsRef.current !== websocket) return;
          scheduleFallbackPolling();
          scheduleReconnect(connectWebSocket);
        };
      } catch {
        scheduleFallbackPolling();
        scheduleReconnect(connectWebSocket);
      }
    }

    connectWebSocket();

    return () => {
      intentionallyClosedRef.current = true;
      clearTimers();
      Object.values(toastTimersRef.current).forEach((timerId) => window.clearTimeout(timerId));
      toastTimersRef.current = {};
      if (wsRef.current) { wsRef.current.close(); wsRef.current = undefined; }
    };
  }, [authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  async function handleJobAction(action, jobId) {
    if (pendingJobActions[jobId]) return;
    setPendingJobActions((prev) => ({ ...prev, [jobId]: action }));
    try {
      if (action === 'cancel') mergeJobUpdate(await cancelJob(jobId));
      else if (action === 'requeue') mergeJobUpdate(await requeueJob(jobId));
      else if (action === 'retry') mergeJobUpdate(await retryJob(jobId));
      else if (action === 'pause') mergeJobUpdate(await pauseJob(jobId));
      else if (action === 'resume') mergeJobUpdate(await resumeJob(jobId));
      else if (action === 'start') mergeJobUpdate(await startJob(jobId));
      else if (action === 'abort') mergeJobUpdate(await abortJob(jobId));
      else if (action === 'discard') mergeJobUpdate(await discardJobProgress(jobId));
      else if (action === 'remove') { await deleteJob(jobId); setJobs((prev) => prev.filter((j) => j.id !== jobId)); }
    } catch (actionError) {
      pushToast(actionError.message || 'Job action failed.', 'error');
    } finally {
      setPendingJobActions((prev) => {
        const next = { ...prev };
        delete next[jobId];
        return next;
      });
    }
  }

  async function handleAbortAllJobs() {
    try {
      const result = await abortAllJobs();
      pushToast(`Aborted ${result.aborted_job_ids.length} encode job(s).`, 'success');
      await refreshAll();
    } catch (actionError) {
      pushToast(actionError.message || 'Abort all failed.', 'error');
    }
  }

  async function handleCancelAllQueued() {
    try {
      const result = await cancelAllQueued();
      pushToast(`Cancelled ${result.cancelled_job_ids.length} queued encode job(s).`, 'success');
      await refreshAll();
    } catch (actionError) {
      pushToast(actionError.message || 'Cancel all queued failed.', 'error');
    }
  }

  async function handlePurgeHistory() {
    try {
      const result = await purgeHistory();
      // Remove terminal download jobs from local state immediately — the server
      // endpoint now clears both encode history and terminal download jobs.
      setDownloadJobs((prev) => prev.filter((dj) => !TERMINAL_DL_STATUSES.has(String(dj.status ?? '').toLowerCase())));
      const removedEncodeCount = result?.removed_job_ids?.length ?? 0;
      const removedDownloadCount = result?.removed_download_job_ids?.length ?? 0;
      pushToast(`Purged ${removedEncodeCount + removedDownloadCount} history item(s).`, 'success');
      await refreshAll();
    } catch (actionError) {
      pushToast(actionError.message || 'Purge history failed.', 'error');
    }
  }

  async function handleQueueAction(action) {
    try {
      if (action === 'pause') { await pauseQueue(); setQueuePaused(true); }
      else { await resumeQueue(); setQueuePaused(false); }
      await refreshAll();
    } catch (actionError) {
      pushToast(actionError.message || 'Queue action failed.', 'error');
    }
  }

  async function handleClearQueue() {
    if (!window.confirm('Clear queue now? This removes queued and active encode items plus active download items. Your current "Pause New Jobs" setting will be preserved.')) return;
    try {
      const result = await clearQueue();
      const totalReset = (result?.removed_job_ids?.length ?? 0) + (result?.removed_download_job_ids?.length ?? 0);
      pushToast(`Queue cleared: ${totalReset} item(s) removed.`, 'success');
      await refreshAll();
    } catch (actionError) {
      pushToast(actionError.message || 'Clear queue failed.', 'error');
    }
  }

  async function handleCreateLibrary() {
    const nextErrors = validateLibraryForm(libraryDraft);
    setLibraryFormErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    setSavingLibrary(true);
    try {
      const created = await createLibrary({ name: libraryDraft.name.trim(), path: libraryDraft.path.trim(), enabled: libraryDraft.enabled });
      await refreshLibrariesAndProfiles();
      setSelectedLibraryId(created.id);
      setLibraryDraft({ name: '', path: '/data/', enabled: true });
      setLibraryFormErrors({});
      pushToast(`Added library ${created.name}.`, 'success');
    } catch (createError) {
      pushToast(createError.message || 'Could not create library.', 'error');
    } finally {
      setSavingLibrary(false);
    }
  }

  async function handleDeleteLibrary(libraryId) {
    setDeletingLibraryId(libraryId);
    try {
      await deleteLibrary(libraryId);
      const remaining = libraries.filter((library) => library.id !== libraryId);
      setLibraries(remaining);
      setSelectedLibraryId((prev) => prev !== libraryId ? prev : (remaining[0]?.id ?? null));
      pushToast('Library deleted.', 'success');
      await refreshLibrariesAndProfiles();
    } catch (deleteError) {
      pushToast(deleteError.message || 'Could not delete library.', 'error');
    } finally {
      setDeletingLibraryId(null);
    }
  }

  async function handleSaveLibraryDetails() {
    if (!selectedLibrary) return;
    const nextErrors = validateLibraryForm(selectedLibrary);
    setLibraryFormErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    setSavingLibrary(true);
    try {
      const updated = await updateLibrary(selectedLibrary.id, { name: selectedLibrary.name.trim(), path: selectedLibrary.path.trim(), enabled: selectedLibrary.enabled });
      setLibraries((prev) => prev.map((library) => (library.id === updated.id ? updated : library)));
      setLibraryFormErrors({});
      pushToast('Library details saved.', 'success');
      await refreshLibrariesAndProfiles();
    } catch (saveError) {
      pushToast(saveError.message || 'Failed to save library details.', 'error');
    } finally {
      setSavingLibrary(false);
    }
  }

  function openDirBrowser(target, initialPath) {
    setDirBrowser({ open: true, target, initialPath: initialPath || null });
  }

  function handleDirSelect(path) {
    if (dirBrowser.target === 'new') {
      setLibraryDraft((prev) => ({ ...prev, path }));
    } else {
      setLibraries((prev) => prev.map((lib) => lib.id === dirBrowser.target ? { ...lib, path } : lib));
    }
  }

  async function handleLibraryToggle(libraryId, enabled) {
    const previous = libraries;
    setLibraries((prev) => prev.map((library) => (library.id === libraryId ? { ...library, enabled } : library)));
    try {
      await updateLibrary(libraryId, { enabled });
    } catch (updateError) {
      setLibraries(previous);
      pushToast(updateError.message || 'Failed to update library state.', 'error');
    }
  }

  async function handleLibraryScan(libraryId) {
    try {
      await scanLibrary(libraryId);
      await refreshAll();
    } catch (scanError) {
      pushToast(scanError.message || 'Failed to start library scan.', 'error');
    }
  }

  async function handleSaveLibraryProfile() {
    if (!selectedLibrary || !profileDraft) return;
    const nextErrors = validateLibraryDraft(profileDraft, selectedLibrary.enabled);
    setProfileErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) return;
    setSavingProfile(true);
    try {
      const updated = await updateLibraryProfile(selectedLibrary.id, profileDraft);
      setLibraryProfiles((prev) => ({ ...prev, [selectedLibrary.id]: updated }));
      setProfileDraft({ ...updated });
      setTargetResolutionCustom(!TARGET_RESOLUTION_PRESETS.includes(Number(updated.target_resolution)));
      setMinimumResolutionCustom(!MIN_SOURCE_RESOLUTION_PRESETS.includes(Number(updated.minimum_source_resolution)));
      pushToast('Library profile saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Failed to save library profile.', 'error');
    } finally {
      setSavingProfile(false);
    }
  }

  async function saveSettings() {
    if (!settings) return;
    setSavingSettings(true);
    try {
      const updated = await updateSettings(settings);
      setSettings(updated);
      pushToast('Settings saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Failed to save settings.', 'error');
    } finally {
      setSavingSettings(false);
    }
  }

  async function saveAccountSettings() {
    if (!accountSettings) return;
    const username = accountForm.username.trim();
    const currentPassword = accountForm.currentPassword;
    const newPassword = accountForm.newPassword;
    const confirm = accountForm.confirmNewPassword;

    if (!currentPassword) {
      pushToast('Current password is required.', 'error');
      return;
    }
    if (!username) {
      pushToast('Username is required.', 'error');
      return;
    }
    if (newPassword && newPassword !== confirm) {
      pushToast('New password confirmation does not match.', 'error');
      return;
    }
    if (newPassword && newPassword.length < 12) {
      pushToast('New password must be at least 12 characters.', 'error');
      return;
    }

    const payload = { current_password: currentPassword };
    if (username !== accountSettings.username) payload.username = username;
    if (newPassword) payload.new_password = newPassword;

    setSavingAccountSettings(true);
    try {
      const updated = await updateAccountSettings(payload);
      setAccountSettings(updated);
      setAuthStatus((prev) => ({ ...prev, username: updated.username, two_factor_enabled: updated.two_factor_enabled }));
      setAccountForm((prev) => ({ ...prev, currentPassword: '', newPassword: '', confirmNewPassword: '' }));
      pushToast('Account settings updated.', 'success');
    } catch (err) {
      pushToast(err.message || 'Failed to update account settings.', 'error');
    } finally {
      setSavingAccountSettings(false);
    }
  }

  async function generateAccountTotpSecret() {
    if (!accountSettings?.username) return;
    setGeneratingAccountTotpSecret(true);
    try {
      const payload = await createTotpSecret(accountSettings.username);
      setAccountTwoFactorDraft((prev) => ({ ...prev, totpSecret: payload.secret, totpUri: payload.otpauth_url || '' }));
      pushToast('Generated 2FA secret.', 'success');
    } catch (err) {
      pushToast(err.message || 'Failed to generate 2FA secret.', 'error');
    } finally {
      setGeneratingAccountTotpSecret(false);
    }
  }

  async function openAccountQrCode() {
    if (!accountSettings?.username) return;
    setGeneratingAccountTotpSecret(true);
    try {
      const shouldGenerate = !accountTwoFactorDraft.totpSecret.trim() || !accountTwoFactorDraft.totpUri.trim();
      let secret = accountTwoFactorDraft.totpSecret.trim();
      let otpauthUrl = accountTwoFactorDraft.totpUri.trim();
      if (shouldGenerate) {
        const payload = await createTotpSecret(accountSettings.username);
        secret = payload.secret;
        otpauthUrl = payload.otpauth_url || '';
        setAccountTwoFactorDraft((prev) => ({ ...prev, totpSecret: secret, totpUri: otpauthUrl }));
      }
      openQrModal({
        title: 'Use QR Code for 2FA',
        subtitle: 'Scan this code in your authenticator app, then enter the current code and your password to enable 2FA.',
        secret,
        otpauthUrl,
      });
    } catch (err) {
      pushToast(err.message || 'Failed to generate 2FA QR code.', 'error');
    } finally {
      setGeneratingAccountTotpSecret(false);
    }
  }

  async function enableTwoFactorForAccount() {
    if (!accountTwoFactorDraft.currentPassword) {
      pushToast('Current password is required to enable 2FA.', 'error');
      return;
    }
    if (!accountTwoFactorDraft.totpSecret) {
      pushToast('Generate a TOTP secret first.', 'error');
      return;
    }
    if (!accountTwoFactorDraft.totpCode) {
      pushToast('Enter a valid authenticator code.', 'error');
      return;
    }

    setEnablingTwoFactor(true);
    try {
      const updated = await enableAccountTwoFactor({
        current_password: accountTwoFactorDraft.currentPassword,
        totp_secret: accountTwoFactorDraft.totpSecret.trim(),
        totp_code: accountTwoFactorDraft.totpCode.trim(),
      });
      setAccountSettings(updated);
      setAuthStatus((prev) => ({ ...prev, two_factor_enabled: true }));
      setAccountTwoFactorDraft({ totpSecret: '', totpUri: '', totpCode: '', currentPassword: '' });
      pushToast('Dual-factor authentication enabled.', 'success', { durationMs: 9000 });
    } catch (err) {
      pushToast(err.message || 'Failed to enable 2FA.', 'error');
    } finally {
      setEnablingTwoFactor(false);
    }
  }

  async function disableTwoFactorForAccount() {
    if (!accountDisableTwoFactorDraft.currentPassword) {
      pushToast('Current password is required to disable 2FA.', 'error');
      return;
    }
    if (!accountDisableTwoFactorDraft.totpCode) {
      pushToast('Enter your authenticator code to disable 2FA.', 'error');
      return;
    }

    setDisablingTwoFactor(true);
    try {
      const updated = await disableAccountTwoFactor({
        current_password: accountDisableTwoFactorDraft.currentPassword,
        totp_code: accountDisableTwoFactorDraft.totpCode.trim(),
      });
      setAccountSettings(updated);
      setAuthStatus((prev) => ({ ...prev, two_factor_enabled: false }));
      setAccountDisableTwoFactorDraft({ currentPassword: '', totpCode: '' });
      pushToast('Dual-factor authentication disabled.', 'success');
    } catch (err) {
      pushToast(err.message || 'Failed to disable 2FA.', 'error');
    } finally {
      setDisablingTwoFactor(false);
    }
  }

  async function saveNotificationSettings() {
    if (!notificationSettings) return;
    setSavingSettings(true);
    try {
      const updated = await updateNotificationSettings(notificationSettings);
      setNotificationSettings(updated);
      pushToast('Notification settings saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Could not save notification settings.', 'error');
    } finally {
      setSavingSettings(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    if (!qrModal.open || !qrModal.otpauthUrl) {
      setQrImageDataUrl('');
      setQrImageBusy(false);
      setQrImageError(null);
      return undefined;
    }

    setQrImageBusy(true);
    setQrImageError(null);
    buildQrCodeDataUrl(qrModal.otpauthUrl)
      .then((dataUrl) => {
        if (cancelled) return;
        setQrImageDataUrl(dataUrl);
      })
      .catch(() => {
        if (cancelled) return;
        setQrImageError('Failed to render QR code.');
      })
      .finally(() => {
        if (cancelled) return;
        setQrImageBusy(false);
      });

    return () => {
      cancelled = true;
    };
  }, [qrModal.open, qrModal.otpauthUrl]);

  async function sendNotificationTest() {
    try {
      await sendTestNotification();
      pushToast('Queued a test notification email.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Could not queue test email.', 'error');
    }
  }

  async function savePlexSettings() {
    if (!plexSettings) return;
    setSavingPlexSettings(true);
    try {
      const updated = await updatePlexSettings(plexSettings);
      setPlexSettings(updated);
      pushToast('Plex settings saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Could not save Plex settings.', 'error');
    } finally {
      setSavingPlexSettings(false);
    }
  }

  async function loadPlexLibraries() {
    setLoadingPlexLibraries(true);
    try {
      const sections = await fetchPlexLibraries();
      setPlexLibraries(sections ?? []);
    } catch (fetchError) {
      pushToast(fetchError.message || 'Could not fetch Plex library sections.', 'error');
    } finally {
      setLoadingPlexLibraries(false);
    }
  }

  async function handleTestPlexConnection() {
    setTestingPlexConnection(true);
    try {
      const result = await testPlexConnection();
      if (result?.success) {
        pushToast('Plex connection successful.', 'success');
        await loadPlexLibraries();
      } else {
        pushToast(result?.error || 'Plex connection failed.', 'error');
      }
    } catch (testError) {
      pushToast(testError.message || 'Plex connection test failed.', 'error');
    } finally {
      setTestingPlexConnection(false);
    }
  }

  async function saveProwlarrSettings() {
    if (!prowlarrSettings) return;
    setSavingProwlarrSettings(true);
    try {
      const updated = await updateProwlarrSettings(prowlarrSettings);
      setProwlarrSettings(updated);
      pushToast('Prowlarr settings saved.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not save Prowlarr settings.', 'error');
    } finally {
      setSavingProwlarrSettings(false);
    }
  }

  async function handleTestProwlarrConnection() {
    setTestingProwlarrConnection(true);
    try {
      const result = await testProwlarrConnection();
      if (result?.success) {
        pushToast(`Prowlarr connected. Found ${result.indexer_count ?? 0} indexer(s).`, 'success');
      } else {
        pushToast(result?.error || 'Prowlarr connection failed.', 'error');
      }
    } catch (err) {
      pushToast(err.message || 'Prowlarr connection test failed.', 'error');
    } finally {
      setTestingProwlarrConnection(false);
    }
  }

  async function saveQbtSettings() {
    if (!qbtSettings) return;
    setSavingQbtSettings(true);
    try {
      const updated = await updateQBittorrentSettings(qbtSettings);
      setQbtSettings(updated);
      pushToast('qBittorrent settings saved.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not save qBittorrent settings.', 'error');
    } finally {
      setSavingQbtSettings(false);
    }
  }

  async function handleTestQbtConnection() {
    setTestingQbtConnection(true);
    try {
      const result = await testQBittorrentConnection();
      if (result?.success) {
        pushToast(`qBittorrent connected. Version: ${result.version ?? 'unknown'}.`, 'success');
      } else {
        pushToast(result?.error || 'qBittorrent connection failed.', 'error');
      }
    } catch (err) {
      pushToast(err.message || 'qBittorrent connection test failed.', 'error');
    } finally {
      setTestingQbtConnection(false);
    }
  }

  async function saveSabSettings() {
    if (!sabSettings) return;
    setSavingSabSettings(true);
    try {
      const updated = await updateSabnzbdSettings(sabSettings);
      setSabSettings(updated);
      pushToast('SABnzbd settings saved.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not save SABnzbd settings.', 'error');
    } finally {
      setSavingSabSettings(false);
    }
  }

  async function handleTestSabConnection() {
    setTestingSabConnection(true);
    try {
      const result = await testSabnzbdConnection();
      if (result?.success) {
        pushToast(`SABnzbd connected. Version: ${result.version ?? 'unknown'}.`, 'success');
      } else {
        pushToast(result?.error || 'SABnzbd connection failed.', 'error');
      }
    } catch (err) {
      pushToast(err.message || 'SABnzbd connection test failed.', 'error');
    } finally {
      setTestingSabConnection(false);
    }
  }

  async function handleCancelDownloadJob(jobId) {
    if (pendingDownloadActions[jobId]) return;
    setPendingDownloadActions((prev) => ({ ...prev, [jobId]: 'remove_reset' }));
    try {
      await removeAndResetDownloadJob(jobId);
      const [nextJobs, nextDownloadJobs] = await Promise.all([fetchJobs(), fetchDownloadJobs()]);
      setJobs(nextJobs ?? []);
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      pushToast('Download removed from client and reset for a fresh search attempt.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not remove/reset download job.', 'error');
    } finally {
      setPendingDownloadActions((prev) => {
        const next = { ...prev };
        delete next[jobId];
        return next;
      });
    }
  }

  async function handleRetryDownloadJob(jobId) {
    if (pendingDownloadActions[jobId]) return;
    setPendingDownloadActions((prev) => ({ ...prev, [jobId]: 'retry' }));
    try {
      await retryDownloadJob(jobId);
      const [nextJobs, nextDownloadJobs] = await Promise.all([fetchJobs(), fetchDownloadJobs()]);
      setJobs(nextJobs ?? []);
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      pushToast('Download job re-queued for another search attempt.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not retry download job.', 'error');
    } finally {
      setPendingDownloadActions((prev) => {
        const next = { ...prev };
        delete next[jobId];
        return next;
      });
    }
  }

  async function handleDeleteDownloadJob(jobId) {
    if (pendingDownloadActions[jobId]) return;
    setPendingDownloadActions((prev) => ({ ...prev, [jobId]: 'delete' }));
    try {
      await deleteDownloadJob(jobId);
      const [nextJobs, nextDownloadJobs] = await Promise.all([fetchJobs(), fetchDownloadJobs()]);
      setJobs(nextJobs ?? []);
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      pushToast('Download job removed.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not remove download job.', 'error');
    } finally {
      setPendingDownloadActions((prev) => {
        const next = { ...prev };
        delete next[jobId];
        return next;
      });
    }
  }

  async function handleDeleteAllDownloadJobs() {
    if (!window.confirm('Remove all download jobs? This cannot be undone.')) return;
    try {
      await deleteAllDownloadJobs();
      setDownloadJobs([]);
      pushToast('All download jobs removed.', 'success');
    } catch (err) {
      pushToast(err.message || 'Could not remove download jobs.', 'error');
    }
  }

  async function handleRecoveryRun() {
    try {
      const result = await runRecovery();
      pushToast(`Recovered ${result.recovered_jobs} jobs.`, 'success');
      await refreshAll();
    } catch (recoveryError) {
      pushToast(recoveryError.message || 'Recovery failed.', 'error');
    }
  }

  async function handleCleanupRun() {
    try {
      const result = await runCleanup();
      pushToast(`Cleanup removed ${result.cleaned_workspaces} workspace(s).`, 'success');
      await refreshAll();
    } catch (cleanupError) {
      pushToast(cleanupError.message || 'Cleanup failed.', 'error');
    }
  }

  async function handleOptimizedCleanupRun() {
    try {
      const result = await runOptimizedCleanup();
      pushToast(`Deleted ${result.deleted_files} optimized file(s) from ${result.affected_job_ids.length} job(s).`, 'success');
      await refreshAll();
    } catch (cleanupError) {
      pushToast(cleanupError.message || 'Optimized cleanup failed.', 'error');
    }
  }

  async function handleDuplicateOptimizedCleanupRun() {
    try {
      const result = await runDuplicateOptimizedCleanup();
      pushToast(`Deleted ${result.deleted_files} duplicate optimized file(s) from ${result.affected_library_ids.length} librar${result.affected_library_ids.length === 1 ? 'y' : 'ies'}.`, 'success');
      await refreshAll();
    } catch (cleanupError) {
      pushToast(cleanupError.message || 'Duplicate cleanup failed.', 'error');
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  if (authStatus.loading) {
    return (
      <main className="app-shell min-h-screen p-4 text-slate-100 md:p-6">
        <div className="mx-auto flex min-h-[70vh] max-w-md items-center justify-center">
          <SectionCard className="w-full">
            <SectionTitle>Loading</SectionTitle>
            <p className="text-sm text-slate-300">Checking authentication status…</p>
          </SectionCard>
        </div>
      </main>
    );
  }

  if (authStatus.setup_required) {
    return (
      <main className="app-shell min-h-screen p-4 text-slate-100 md:p-6">
        <div className="mx-auto flex min-h-[70vh] max-w-lg items-center justify-center">
          <SectionCard className="w-full space-y-4">
            <SectionTitle>Initial Admin Setup</SectionTitle>
            <p className="text-sm text-slate-300">Create the first admin account to secure Optimizarr. Dual-factor authentication is optional and can be skipped.</p>
            <form className="space-y-3" onSubmit={handleBootstrapSubmit}>
              <FormField label="Setup Token" hint="Use OPTIMIZARR_BOOTSTRAP_TOKEN or the one-time token printed in the server logs.">
                <TextInput
                  type="password"
                  value={setupForm.bootstrapToken}
                  onChange={(event) => setSetupForm((prev) => ({ ...prev, bootstrapToken: event.target.value }))}
                  autoComplete="off"
                />
              </FormField>
              <FormField label="Admin Username">
                <TextInput
                  value={setupForm.username}
                  onChange={(event) => setSetupForm((prev) => ({ ...prev, username: event.target.value }))}
                  autoComplete="username"
                />
              </FormField>
              <FormField label="Password" hint="Minimum 12 characters.">
                <TextInput
                  type="password"
                  value={setupForm.password}
                  onChange={(event) => setSetupForm((prev) => ({ ...prev, password: event.target.value }))}
                  autoComplete="new-password"
                />
              </FormField>
              <FormField label="Confirm Password">
                <TextInput
                  type="password"
                  value={setupForm.confirmPassword}
                  onChange={(event) => setSetupForm((prev) => ({ ...prev, confirmPassword: event.target.value }))}
                  autoComplete="new-password"
                />
              </FormField>
              <label className="flex items-center gap-2 text-sm text-slate-200">
                <input
                  type="checkbox"
                  checked={setupForm.enableTwoFactor}
                  onChange={(event) => setSetupForm((prev) => ({ ...prev, enableTwoFactor: event.target.checked }))}
                />
                Enable dual-factor authentication (TOTP)
              </label>
              {setupForm.enableTwoFactor && (
                <div className="space-y-3 rounded-xl border border-slate-700/80 bg-slate-950/50 p-3">
                  <div className="flex flex-wrap gap-2">
                    <Btn variant="secondary" onClick={handleGenerateSetupSecret} disabled={setupSecretBusy} type="button">
                      {setupSecretBusy ? 'Generating…' : 'Generate 2FA Secret'}
                    </Btn>
                    <Btn variant="primary" onClick={openSetupQrCode} disabled={setupSecretBusy} type="button">
                      {setupSecretBusy ? 'Generating…' : 'Use QR Code'}
                    </Btn>
                  </div>
                  <FormField label="TOTP Secret" hint="Add this secret in your authenticator app.">
                    <TextInput
                      value={setupForm.totpSecret}
                      onChange={(event) => setSetupForm((prev) => ({ ...prev, totpSecret: event.target.value, totpUri: '' }))}
                    />
                  </FormField>
                  <FormField label="Current 2FA Code" hint="Enter the 6-digit code from your authenticator app.">
                    <TextInput
                      value={setupForm.totpCode}
                      onChange={(event) => setSetupForm((prev) => ({ ...prev, totpCode: event.target.value }))}
                      inputMode="numeric"
                    />
                  </FormField>
                </div>
              )}
              {setupError && <p className="text-sm text-red-400">{setupError}</p>}
              <Btn variant="primary" size="lg" className="w-full" disabled={setupBusy} type="submit">
                {setupBusy ? 'Creating Admin…' : 'Create Admin Account'}
              </Btn>
            </form>
            <Modal open={qrModal.open} onClose={closeQrModal} title={qrModal.title}>
              <div className="space-y-3">
                {qrModal.subtitle && <p className="text-xs text-slate-300">{qrModal.subtitle}</p>}
                <div className="rounded-xl border border-slate-700 bg-slate-950/70 p-3">
                  {qrImageBusy && <p className="text-xs text-slate-400">Rendering QR code…</p>}
                  {!qrImageBusy && qrImageDataUrl && (
                    <img src={qrImageDataUrl} alt="Authenticator setup QR code" className="mx-auto h-64 w-64 rounded-md bg-white p-2" />
                  )}
                  {qrImageError && <p className="text-xs text-red-400">{qrImageError}</p>}
                </div>
                <FormField label="Manual Secret (fallback)">
                  <TextInput readOnly value={qrModal.secret} />
                </FormField>
              </div>
            </Modal>
          </SectionCard>
        </div>
      </main>
    );
  }

  if (!authStatus.authenticated) {
    return (
      <main className="app-shell min-h-screen p-4 text-slate-100 md:p-6">
        <div className="mx-auto flex min-h-[70vh] max-w-md items-center justify-center">
          <SectionCard className="w-full space-y-4">
            <SectionTitle>Login</SectionTitle>
            <form className="space-y-3" onSubmit={handleLoginSubmit}>
              <FormField label="Username">
                <TextInput
                  value={loginForm.username}
                  onChange={(event) => setLoginForm((prev) => ({ ...prev, username: event.target.value }))}
                  autoComplete="username"
                />
              </FormField>
              <FormField label="Password">
                <TextInput
                  type="password"
                  value={loginForm.password}
                  onChange={(event) => setLoginForm((prev) => ({ ...prev, password: event.target.value }))}
                  autoComplete="current-password"
                />
              </FormField>
              {authStatus.two_factor_enabled && (
                <>
                  <FormField label="2FA Code">
                    <TextInput
                      value={loginForm.otpCode}
                      onChange={(event) => setLoginForm((prev) => ({ ...prev, otpCode: event.target.value }))}
                      inputMode="numeric"
                    />
                  </FormField>
                  <p className="text-xs text-slate-400">
                    2FA is enabled for this account, so a valid 6-digit authenticator code is required.
                  </p>
                </>
              )}
              {loginError && <p className="text-sm text-red-400">{loginError}</p>}
              <Btn variant="primary" size="lg" className="w-full" disabled={loginBusy} type="submit">
                {loginBusy ? 'Signing In…' : 'Sign In'}
              </Btn>
            </form>
          </SectionCard>
        </div>
      </main>
    );
  }

  return (
    <main className="app-shell min-h-screen p-4 text-slate-100 md:p-6">
      <div className="mx-auto max-w-7xl space-y-5">

        {/* Header */}
        <header className="relative overflow-hidden rounded-2xl border border-slate-700/70 bg-slate-900/75 px-5 py-4 shadow-2xl shadow-slate-950/60 backdrop-blur-sm">
          <div className="pointer-events-none absolute inset-0 bg-gradient-to-r from-cyan-500/10 via-transparent to-sky-400/10" />
          <div className="relative flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-3">
                <img src="/api/branding/logo" alt="Optimizarr" className="h-12 w-auto drop-shadow-md" />
                <div className="hidden sm:block">
                  <p className="text-lg font-semibold tracking-wide text-slate-100">Optimizarr Control Center</p>
                  <p className="text-xs text-slate-400">Media optimization orchestration and monitoring</p>
                </div>
              </div>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <StatusDot status={connectionStatus} />
              <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-xs text-slate-300">
                {authStatus.username}
              </span>
              <span aria-label={`Queue items: ${queueCount}`} className="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-xs text-slate-300">
                Queue {queueCount}
              </span>
              <span aria-label={`Active jobs: ${metrics?.active_jobs ?? 0}`} className="rounded-full border border-slate-700 bg-slate-900/80 px-2.5 py-1 text-xs text-slate-300">
                Active {metrics?.active_jobs ?? 0}
              </span>
              <Btn variant="secondary" size="sm" onClick={handleLogout}>
                Logout
              </Btn>
            </div>
          </div>
          <nav className="relative mt-3 flex gap-1 rounded-xl border border-slate-700/70 bg-slate-950/60 p-1">
            {Object.entries(PAGE_KEYS).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={`rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${
                  activePage === key
                    ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950 shadow-sm shadow-cyan-500/30'
                    : 'text-slate-400 hover:bg-slate-800/80 hover:text-slate-100'
                }`}
                onClick={() => navigate(key)}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {/* Directory browser modal */}
        <DirBrowserModal
          open={dirBrowser.open}
          initialPath={dirBrowser.initialPath}
          onSelect={handleDirSelect}
          onClose={() => setDirBrowser((prev) => ({ ...prev, open: false }))}
        />

        {/* Toast stack */}
        <div className="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col gap-2">
          {toasts.map((toast) => (
            <div
              key={toast.id}
              className={`animate-slide-in-right rounded-xl border px-4 py-2.5 text-sm shadow-xl backdrop-blur-sm ${
                toast.tone === 'error'
                  ? 'border-red-700/60 bg-red-950/90 text-red-200'
                  : toast.tone === 'warn'
                    ? 'border-amber-600/60 bg-amber-950/90 text-amber-200'
                    : toast.tone === 'success'
                      ? 'border-emerald-700/60 bg-emerald-950/90 text-emerald-200'
                      : 'border-cyan-700/60 bg-slate-900/95 text-cyan-200'
              }`}
            >
              {toast.message}
            </div>
          ))}
        </div>

        {/* ── Dashboard ──────────────────────────────────────────────────────── */}
        {activePage === 'dashboard' && (
          <section className="animate-fade-in space-y-5">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
              <StatCard label="GPU" value={`${Math.round(Math.max(metrics?.gpu_video_percent ?? 0, metrics?.gpu_render_percent ?? 0))}%`} />
              <StatCard label="CPU" value={`${metrics?.cpu_percent ?? 0}%`} />
              <StatCard label="RAM" value={`${metrics?.ram_percent ?? 0}%`} />
              <StatCard label="Active Jobs" value={metrics?.active_jobs ?? 0} />
              <StatCard label="Queue" value={queueCount} />
              <StatCard label="Libraries" value={libraries.length} />
            </div>

            <SectionCard>
              <SectionTitle>Library Status</SectionTitle>
              <div className="space-y-2">
                {libraryRuntimeStates.length === 0 && (
                  <p className="text-sm text-slate-500">No libraries configured yet.</p>
                )}
                {libraryRuntimeStates.map(({ library, state, queue }) => (
                  <div key={library.id} className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/40 px-4 py-3 transition-colors duration-150 hover:border-slate-700/60">
                    <div>
                      <p className="font-medium text-cyan-200">{library.name}</p>
                      <p className="mt-0.5 text-xs text-slate-500">{library.path}</p>
                    </div>
                    <div className="text-right">
                      <p className="text-sm text-slate-300">{state}</p>
                      <p className="mt-0.5 text-xs text-slate-500">Queue: {queue}</p>
                    </div>
                  </div>
                ))}
              </div>
            </SectionCard>
          </section>
        )}

        {/* ── Libraries ──────────────────────────────────────────────────────── */}
        {activePage === 'libraries' && (
          <section className="animate-fade-in grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">

            {/* Left: list + add form */}
            <div className="space-y-4">
              <SectionCard>
                <SectionTitle>Add Library</SectionTitle>
                <div className="grid gap-3 md:grid-cols-2">
                  <FormField label="Name" error={libraryFormErrors.name} span2>
                    <TextInput
                      type="text"
                      value={libraryDraft.name}
                      onChange={(e) => setLibraryDraft((prev) => ({ ...prev, name: e.target.value }))}
                      placeholder="My Movies"
                    />
                  </FormField>
                  <FormField label="Path" error={libraryFormErrors.path} span2>
                    <div className="flex gap-2">
                      <TextInput
                        type="text"
                        value={libraryDraft.path}
                        onChange={(e) => setLibraryDraft((prev) => ({ ...prev, path: e.target.value }))}
                        placeholder="/data/movies"
                      />
                      <button
                        type="button"
                        title="Browse directories"
                        className="shrink-0 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-slate-300 hover:bg-slate-700 hover:text-cyan-300 transition-colors"
                        onClick={() => openDirBrowser('new', libraryDraft.path)}
                      >
                        <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20"><path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/></svg>
                      </button>
                    </div>
                  </FormField>
                  <div className="md:col-span-2">
                    <Toggle
                      checked={libraryDraft.enabled}
                      onChange={(e) => setLibraryDraft((prev) => ({ ...prev, enabled: e.target.checked }))}
                      label="Enabled"
                    />
                  </div>
                </div>
                <Btn variant="primary" className="mt-4" disabled={savingLibrary} onClick={handleCreateLibrary}>
                  {savingLibrary ? 'Adding…' : 'Add Library'}
                </Btn>
              </SectionCard>

              <SectionCard>
                <SectionTitle>Libraries</SectionTitle>
                <div className="space-y-2">
                  {libraries.map((library) => (
                    <div
                      key={library.id}
                      className={`rounded-lg border p-3 transition-all duration-150 ${
                        selectedLibraryId === library.id
                          ? 'border-cyan-500/60 bg-cyan-950/20'
                          : 'border-slate-800/60 bg-slate-950/30 hover:border-slate-700/60'
                      }`}
                    >
                      <div className="flex items-start justify-between gap-3">
                        <button type="button" className="min-w-0 flex-1 text-left" onClick={() => setSelectedLibraryId(library.id)}>
                          <p className="font-medium text-cyan-200 truncate">{library.name}</p>
                          <p className="mt-0.5 text-xs text-slate-500 truncate">{library.path}</p>
                          <p className="mt-0.5 text-xs text-slate-400">Queue: {libraryQueueCount(library, jobs, downloadJobs)}</p>
                        </button>
                        <div className="flex shrink-0 items-center gap-2">
                          <Toggle
                            checked={library.enabled}
                            onChange={(e) => handleLibraryToggle(library.id, e.target.checked)}
                          />
                          <Btn size="sm" variant="secondary" disabled={Boolean(library.scanning)} onClick={() => handleLibraryScan(library.id)}>
                            {library.scanning ? 'Scanning…' : 'Scan'}
                          </Btn>
                          <Btn size="sm" variant="danger" disabled={deletingLibraryId === library.id} onClick={() => handleDeleteLibrary(library.id)}>
                            {deletingLibraryId === library.id ? 'Deleting…' : 'Delete'}
                          </Btn>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              </SectionCard>
            </div>

            {/* Right: profile editor */}
            <SectionCard>
              {!selectedLibrary || !profileDraft ? (
                <p className="text-sm text-slate-400">Select a library to edit its encoding profile.</p>
              ) : (
                <div className="space-y-5">
                  <CollapsibleSection
                    title="Library Details"
                    open={profileSectionsOpen.details}
                    onToggle={() => setProfileSectionsOpen((prev) => ({ ...prev, details: !prev.details }))}
                  >
                    <div className="space-y-3">
                      <FormField label="Name">
                        <TextInput
                          type="text"
                          value={selectedLibrary.name}
                          onChange={(e) => setLibraries((prev) => prev.map((lib) => lib.id === selectedLibrary.id ? { ...lib, name: e.target.value } : lib))}
                        />
                      </FormField>
                      <FormField label="Path">
                        <div className="flex gap-2">
                          <TextInput
                            type="text"
                            value={selectedLibrary.path}
                            onChange={(e) => setLibraries((prev) => prev.map((lib) => lib.id === selectedLibrary.id ? { ...lib, path: e.target.value } : lib))}
                          />
                          <button
                            type="button"
                            title="Browse directories"
                            className="shrink-0 rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-slate-300 hover:bg-slate-700 hover:text-cyan-300 transition-colors"
                            onClick={() => openDirBrowser(selectedLibrary.id, selectedLibrary.path)}
                          >
                            <svg className="h-4 w-4" fill="currentColor" viewBox="0 0 20 20"><path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/></svg>
                          </button>
                        </div>
                      </FormField>
                      <p className="text-xs text-slate-400">
                        Status: <span className="text-slate-200">{libraryRuntimeStates.find((item) => item.library.id === selectedLibrary.id)?.state}</span>
                      </p>
                      <div className="flex flex-wrap gap-2">
                        <Btn variant="primary" disabled={savingLibrary} onClick={handleSaveLibraryDetails}>
                          {savingLibrary ? 'Saving…' : 'Save Details'}
                        </Btn>
                        <Btn variant="danger" disabled={deletingLibraryId === selectedLibrary.id} onClick={() => handleDeleteLibrary(selectedLibrary.id)}>
                          {deletingLibraryId === selectedLibrary.id ? 'Deleting…' : 'Delete Library'}
                        </Btn>
                      </div>
                    </div>
                  </CollapsibleSection>

                  <CollapsibleSection
                    title="Encoding Profile"
                    open={profileSectionsOpen.processing}
                    onToggle={() => setProfileSectionsOpen((prev) => ({ ...prev, processing: !prev.processing }))}
                    divider
                  >
                    <FormField label="Quality Preset" hint="Start with a preset, then fine-tune below.">
                      <SelectInput
                        value={selectedPreset}
                        onChange={(e) => {
                          const presetKey = e.target.value;
                          setSelectedPreset(presetKey);
                          const preset = QUALITY_PRESETS[presetKey];
                          if (preset) setProfileDraft((prev) => ({ ...prev, ...preset.profile }));
                        }}
                      >
                        {Object.entries(QUALITY_PRESETS).map(([key, value]) => (
                          <option key={key} value={key}>{value.label}</option>
                        ))}
                      </SelectInput>
                    </FormField>
                  

                  <div className="grid gap-4 md:grid-cols-2">
                    {/* Library enabled */}
                    <div className="rounded-lg border border-slate-800/60 bg-slate-950/30 p-3">
                      <p className="mb-2 text-sm font-medium text-slate-200">Library Enabled</p>
                      <p className="mb-3 text-xs text-slate-500">Disable to pause optimization for this library.</p>
                      <Toggle
                        checked={selectedLibrary.enabled}
                        onChange={(e) => handleLibraryToggle(selectedLibrary.id, e.target.checked)}
                        label={selectedLibrary.enabled ? 'Enabled' : 'Disabled'}
                      />
                    </div>

                    {/* HDR only */}
                    <div className="rounded-lg border border-slate-800/60 bg-slate-950/30 p-3">
                      <p className="mb-2 text-sm font-medium text-slate-200">HDR Only</p>
                      <p className="mb-3 text-xs text-slate-500">Only process HDR media in this library.</p>
                      <Toggle
                        checked={profileDraft.hdr_only}
                        onChange={(e) => setProfileDraft((prev) => ({ ...prev, hdr_only: e.target.checked }))}
                        label={profileDraft.hdr_only ? 'Enabled' : 'Disabled'}
                      />
                    </div>

                    {/* Tone map HDR */}
                    <div className="rounded-lg border border-slate-800/60 bg-slate-950/30 p-3 md:col-span-2">
                      <p className="mb-2 text-sm font-medium text-slate-200">Tone Map HDR to SDR</p>
                      <p className="mb-3 text-xs text-slate-500">Strip HDR metadata and convert to SDR during encoding. Reduces playback stutters on SDR devices.</p>
                      <Toggle
                        checked={profileDraft.tone_map_hdr}
                        onChange={(e) => setProfileDraft((prev) => ({ ...prev, tone_map_hdr: e.target.checked }))}
                        label={profileDraft.tone_map_hdr ? 'Enabled' : 'Disabled'}
                      />
                    </div>

                    {/* Target resolution */}
                    <FormField label="Target Resolution" hint="Output height. 1080p is recommended for mixed libraries." error={profileErrors.target_resolution} span2>
                      <SelectInput
                        value={targetResolutionCustom ? 'custom' : String(profileDraft.target_resolution)}
                        onChange={(e) => {
                          const v = e.target.value;
                          if (v === 'custom') {
                            setTargetResolutionCustom(true);
                            return;
                          }
                          setTargetResolutionCustom(false);
                          setProfileDraft((prev) => ({ ...prev, target_resolution: Number(v) }));
                        }}
                      >
                        <option value="2160">2160p (4K)</option>
                        <option value="1440">1440p</option>
                        <option value="1080">1080p</option>
                        <option value="720">720p</option>
                        <option value="custom">Custom</option>
                      </SelectInput>
                      {targetResolutionCustom && (
                        <TextInput type="number" min={1} value={profileDraft.target_resolution} onChange={(e) => setProfileDraft((prev) => ({ ...prev, target_resolution: Number(e.target.value) }))} className="mt-2" />
                      )}
                    </FormField>

                    {/* Minimum source resolution */}
                    <FormField label="Minimum Source Resolution" hint="Only queue sources at or above this height." error={profileErrors.minimum_source_resolution} span2>
                      <SelectInput
                        value={minimumResolutionCustom ? 'custom' : String(profileDraft.minimum_source_resolution)}
                        onChange={(e) => {
                          const v = e.target.value;
                          if (v === 'custom') {
                            setMinimumResolutionCustom(true);
                            return;
                          }
                          setMinimumResolutionCustom(false);
                          setProfileDraft((prev) => ({ ...prev, minimum_source_resolution: Number(v) }));
                        }}
                      >
                        <option value="2160">2160p (4K)</option>
                        <option value="1440">1440p</option>
                        <option value="1080">1080p</option>
                        <option value="custom">Custom</option>
                      </SelectInput>
                      {minimumResolutionCustom && (
                        <TextInput type="number" min={1} value={profileDraft.minimum_source_resolution} onChange={(e) => setProfileDraft((prev) => ({ ...prev, minimum_source_resolution: Number(e.target.value) }))} className="mt-2" />
                      )}
                    </FormField>

                    {/* Codec */}
                    <FormField label="Codec">
                      <SelectInput
                        value={profileDraft.codec}
                        onChange={(e) => setProfileDraft((prev) => {
                          const nextCodec = e.target.value;
                          const available = availableEncodersByCodec[nextCodec] ?? [];
                          const preferred = available.includes(prev.preferred_video_encoder) ? prev.preferred_video_encoder : 'auto';
                          const availableFallbacks = getAvailableDownloadFallbackCodecs(prev.download_codec, nextCodec);
                          const nextDownloadFallback = availableFallbacks.includes(prev.download_fallback_codec) ? prev.download_fallback_codec : null;
                          return {
                            ...prev,
                            codec: nextCodec,
                            preferred_video_encoder: preferred,
                            download_fallback_codec: nextDownloadFallback,
                          };
                        })}
                      >
                        <option value="h264">H.264</option>
                        <option value="hevc">HEVC</option>
                        <option value="av1">AV1</option>
                      </SelectInput>
                    </FormField>

                    {/* Preferred encoder */}
                    <FormField label="Preferred Encoder" hint="Pick a detected encoder, or Auto for best available.">
                      <SelectInput
                        value={profileDraft.preferred_video_encoder ?? 'auto'}
                        onChange={(e) => setProfileDraft((prev) => ({ ...prev, preferred_video_encoder: e.target.value }))}
                      >
                        <option value="auto">Auto</option>
                        {(availableEncodersByCodec[profileDraft.codec] ?? []).map((encoderName) => (
                          <option key={encoderName} value={encoderName}>{encoderName}</option>
                        ))}
                      </SelectInput>
                    </FormField>

                    {/* AV1 fallback */}
                    <FormField label="AV1 Fallback Codec" hint="Used when AV1 is not available on your hardware." error={profileErrors.av1_fallback_codec}>
                      <SelectInput
                        value={profileDraft.av1_fallback_codec}
                        onChange={(e) => setProfileDraft((prev) => ({ ...prev, av1_fallback_codec: e.target.value }))}
                      >
                        <option value="hevc">HEVC</option>
                        <option value="h264">H.264</option>
                      </SelectInput>
                    </FormField>

                    {/* Bitrate mode */}
                    <div className="rounded-lg border border-slate-800/60 bg-slate-950/30 p-3 md:col-span-2">
                      <p className="mb-1 text-sm font-medium text-slate-200">Bitrate Mode</p>
                      <p className="mb-3 text-xs text-slate-500">CRF targets visual quality. CBR targets a fixed bitrate.</p>
                      <div className="flex gap-5 text-sm">
                        <label className="flex cursor-pointer items-center gap-2">
                          <input
                            type="radio"
                            name="bitrate-mode"
                            checked={profileDraft.bitrate_mode === 'vbr_crf'}
                            onChange={() => setProfileDraft((prev) => ({
                              ...prev,
                              bitrate_mode: 'vbr_crf',
                              crf: Number.isInteger(prev.crf) ? prev.crf : 23,
                            }))}
                          />
                          <span className="text-slate-200">CRF (quality target)</span>
                        </label>
                        <label className="flex cursor-pointer items-center gap-2">
                          <input type="radio" name="bitrate-mode" checked={profileDraft.bitrate_mode === 'cbr'} onChange={() => setProfileDraft((prev) => ({ ...prev, bitrate_mode: 'cbr' }))} />
                          <span className="text-slate-200">CBR (size target)</span>
                        </label>
                      </div>
                    </div>

                    {/* CRF */}
                    {profileDraft.bitrate_mode === 'vbr_crf' && (
                      <FormField label={`CRF (${profileDraft.crf ?? 23})`} hint="Range 18–30. Lower = better quality / larger files." error={profileErrors.crf} span2>
                        <input type="range" min={1} max={40} value={profileDraft.crf ?? 23} onChange={(e) => setProfileDraft((prev) => ({ ...prev, crf: Number(e.target.value) }))} className="w-full" />
                        <TextInput type="number" min={1} value={profileDraft.crf ?? 23} onChange={(e) => setProfileDraft((prev) => ({ ...prev, crf: Number(e.target.value) }))} className="mt-2" />
                      </FormField>
                    )}

                    {/* CBR bitrate */}
                    {profileDraft.bitrate_mode === 'cbr' && (
                      <FormField label={`Bitrate (${profileDraft.bitrate_mbps ?? 1} Mbps)`} error={profileErrors.bitrate_mbps} span2>
                        <input type="range" min={1} max={40} value={profileDraft.bitrate_mbps ?? 1} onChange={(e) => setProfileDraft((prev) => ({ ...prev, bitrate_mbps: Number(e.target.value) }))} className="w-full" />
                        <TextInput type="number" min={1} value={profileDraft.bitrate_mbps ?? ''} onChange={(e) => setProfileDraft((prev) => ({ ...prev, bitrate_mbps: Number(e.target.value) }))} className="mt-2" />
                      </FormField>
                    )}

                    {/* Speed preset */}
                    <FormField label="Speed Preset" hint="Slow = best compression. Fast = quickest encode.">
                      <SelectInput value={profileDraft.speed_preset} onChange={(e) => setProfileDraft((prev) => ({ ...prev, speed_preset: e.target.value }))}>
                        <option value="slow">Slow</option>
                        <option value="medium">Medium</option>
                        <option value="fast">Fast</option>
                      </SelectInput>
                    </FormField>

                    {/* Container */}
                    <FormField label="Container">
                      <SelectInput value={profileDraft.container} onChange={(e) => setProfileDraft((prev) => ({ ...prev, container: e.target.value }))}>
                        <option value="mkv">MKV</option>
                        <option value="mp4">MP4</option>
                      </SelectInput>
                    </FormField>

                    {/* Audio */}
                    <FormField label="Audio Mode">
                      <SelectInput value={profileDraft.audio_mode} onChange={(e) => setProfileDraft((prev) => ({ ...prev, audio_mode: e.target.value }))}>
                        <option value="copy">Copy (passthrough)</option>
                        <option value="aac">AAC</option>
                        <option value="ac3">AC3</option>
                        <option value="eac3">EAC3</option>
                      </SelectInput>
                    </FormField>

                    {/* Max workers */}
                    <FormField label={`Max Workers (${profileDraft.max_workers})`} hint="Per-library concurrent encoding worker cap." error={profileErrors.max_workers}>
                      <input type="range" min={1} max={10} value={profileDraft.max_workers} onChange={(e) => setProfileDraft((prev) => ({ ...prev, max_workers: Number(e.target.value) }))} className="w-full" />
                    </FormField>

                    {/* Schedule */}
                    <div className="rounded-lg border border-slate-800/60 bg-slate-950/30 p-3 md:col-span-2">
                      <p className="mb-1 text-sm font-medium text-slate-200">Scheduled Run Window</p>
                      <p className="mb-3 text-xs text-slate-500">Restrict this library to run only within a set time window. End hour is exclusive (runs up to, but not including, end hour). Disable to run all day.</p>
                      <Toggle
                        checked={profileDraft.schedule_enabled !== false}
                        onChange={(e) => setProfileDraft((prev) => ({ ...prev, schedule_enabled: e.target.checked }))}
                        label="Enable Schedule Window"
                      />
                    </div>

                    {profileDraft.schedule_enabled !== false && (
                      <>
                        <FormField label="Schedule Start" error={profileErrors.schedule_start_hour}>
                          <TextInput type="time" step={3600} value={formatHour(profileDraft.schedule_start_hour)} onChange={(e) => setProfileDraft((prev) => ({ ...prev, schedule_start_hour: parseHour(e.target.value) }))} />
                        </FormField>
                        <FormField label="Schedule End" error={profileErrors.schedule_end_hour}>
                          <TextInput type="time" step={3600} value={formatHour(profileDraft.schedule_end_hour)} onChange={(e) => setProfileDraft((prev) => ({ ...prev, schedule_end_hour: parseHour(e.target.value) }))} />
                        </FormField>
                        <FormField label="When Schedule Closes" span2>
                          <SelectInput
                            value={profileDraft.schedule_policy ?? 'finish_current'}
                            onChange={(e) => setProfileDraft((prev) => ({ ...prev, schedule_policy: e.target.value }))}
                          >
                            <option value="finish_current">Finish current job</option>
                            <option value="pause_current">Pause current job</option>
                          </SelectInput>
                        </FormField>
                      </>
                    )}
                  </div>
                  </CollapsibleSection>

                  {plexSettings?.enabled && (
                    <CollapsibleSection
                      title="Plex Integration"
                      open={profileSectionsOpen.plex}
                      onToggle={() => setProfileSectionsOpen((prev) => ({ ...prev, plex: !prev.plex }))}
                      divider
                    >
                      <FormField
                        label="Plex Library Section"
                        hint={plexLibraries.length === 0 ? 'Go to Settings → Plex Integration and click Load Sections to populate this list.' : 'Optimizarr will scan this Plex section each time a file from this library finishes encoding.'}
                      >
                        <SelectInput
                          value={profileDraft.plex_library_id ?? ''}
                          onChange={(e) => setProfileDraft((prev) => ({ ...prev, plex_library_id: e.target.value || null }))}
                        >
                          <option value="">— No Plex scan —</option>
                          {plexLibraries.map((section) => (
                            <option key={section.id} value={section.id}>
                              {section.name} ({section.type})
                            </option>
                          ))}
                        </SelectInput>
                      </FormField>
                    </CollapsibleSection>
                  )}

                  <CollapsibleSection
                    title="Download Mode"
                    open={profileSectionsOpen.download}
                    onToggle={() => setProfileSectionsOpen((prev) => ({ ...prev, download: !prev.download }))}
                    divider
                  >
                    <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                      <div>
                        <p className="text-sm font-medium text-slate-200">Enable Download Mode</p>
                        <p className="text-xs text-slate-500">Search Prowlarr for a pre-encoded version before falling back to transcoding. Requires Prowlarr and a download client to be configured in Settings.</p>
                      </div>
                      <Toggle
                        checked={profileDraft.download_enabled ?? false}
                        onChange={(e) => setProfileDraft((prev) => ({ ...prev, download_enabled: e.target.checked }))}
                      />
                    </div>
                    {profileDraft.download_enabled && (
                      <>
                        <FormField label="Download Codec Preference" hint="Choose the codec Optimizarr should prefer when selecting download releases. Leave it on 'Use Encode Codec' to follow the main codec setting.">
                          <SelectInput
                            value={profileDraft.download_codec ?? ''}
                            onChange={(e) => {
                              const nextDownloadCodec = e.target.value || null;
                              setProfileDraft((prev) => {
                                const availableFallbacks = getAvailableDownloadFallbackCodecs(nextDownloadCodec, prev.codec);
                                const nextFallback = availableFallbacks.includes(prev.download_fallback_codec) ? prev.download_fallback_codec : null;
                                return {
                                  ...prev,
                                  download_codec: nextDownloadCodec,
                                  download_fallback_codec: nextFallback,
                                };
                              });
                            }}
                          >
                            <option value="">{`Use Encode Codec (${CODEC_LABELS[profileDraft.codec] ?? profileDraft.codec?.toUpperCase() ?? 'Auto'})`}</option>
                            <option value="hevc">HEVC</option>
                            <option value="h264">H.264</option>
                            <option value="av1">AV1</option>
                          </SelectInput>
                        </FormField>
                        <FormField
                          label="Download Fallback Codec"
                          hint="Optional fallback when no release matches the preferred download codec. The first item is a disabled placeholder; choose 'Disabled' to turn fallback off."
                          error={profileErrors.download_fallback_codec}
                        >
                          <SelectInput
                            value={profileDraft.download_fallback_codec ?? 'none'}
                            onChange={(e) => setProfileDraft((prev) => ({ ...prev, download_fallback_codec: e.target.value === 'none' ? null : e.target.value }))}
                            disabled={getAvailableDownloadFallbackCodecs(profileDraft.download_codec, profileDraft.codec).length === 0}
                          >
                            <option value="" disabled>Select fallback codec</option>
                            <option value="none">Disabled</option>
                            {getAvailableDownloadFallbackCodecs(profileDraft.download_codec, profileDraft.codec).map((codecName) => (
                              <option key={codecName} value={codecName}>
                                {CODEC_LABELS[codecName] ?? codecName.toUpperCase()}
                              </option>
                            ))}
                          </SelectInput>
                        </FormField>
                        <FormField label="Quality Profile" hint="Only accept releases matching this source type. 'Any' skips the quality filter and accepts whatever Prowlarr returns.">
                          <SelectInput
                            value={profileDraft.download_quality_profile ?? 'any'}
                            onChange={(e) => setProfileDraft((prev) => ({ ...prev, download_quality_profile: e.target.value }))}
                          >
                            <option value="any">Any</option>
                            <option value="remux">REMUX</option>
                            <option value="web_dl">WEB-DL</option>
                            <option value="webrip">WEBRip</option>
                            <option value="bluray">Blu-Ray</option>
                            <option value="hdtv">HDTV</option>
                          </SelectInput>
                        </FormField>
                        <FormField label="Download Timeout (minutes)" hint="If no completed download is found after this many minutes, the job falls back to encoding.">
                          <TextInput
                            type="number"
                            min={1}
                            value={profileDraft.download_timeout_minutes ?? 60}
                            onChange={(e) => setProfileDraft((prev) => ({ ...prev, download_timeout_minutes: Number(e.target.value) }))}
                          />
                        </FormField>
                      </>
                    )}
                  </CollapsibleSection>

                  <Btn variant="primary" size="lg" disabled={savingProfile} onClick={handleSaveLibraryProfile} className="w-full sm:w-auto">
                    {savingProfile ? 'Saving…' : 'Save Profile'}
                  </Btn>
                </div>
              )}
            </SectionCard>
          </section>
        )}

        {/* ── Jobs ───────────────────────────────────────────────────────────── */}
        {activePage === 'jobs' && (
          <section className="animate-fade-in space-y-5">

            {/* Queue / History tab card */}
            <div className="overflow-hidden rounded-2xl border border-slate-700/70 bg-slate-900/75 shadow-2xl shadow-slate-950/45 backdrop-blur-sm">
              <div className="border-b border-slate-700/70 px-5 py-3">
                <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                  <div>
                    <p className="text-sm font-semibold tracking-wide text-slate-200">Job Activity</p>
                    <p className="text-xs text-slate-500">Track current queue operations and historical processing outcomes.</p>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    {jobsView === 'queue' && (
                      <span aria-label={`Queue status: ${queuePaused ? 'new jobs paused' : 'accepting new jobs'}`} className={`rounded-full border px-2.5 py-1 text-xs ${queuePaused ? 'border-amber-500/40 bg-amber-950/40 text-amber-300' : 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300'}`}>
                        {queuePaused ? 'New Jobs Paused' : 'Queue Active'}
                      </span>
                    )}
                  </div>
                </div>
                <div className="flex flex-wrap items-center justify-between gap-3">
                {/* Tab switcher */}
                <div className="flex gap-1 rounded-xl border border-slate-700/70 bg-slate-950/60 p-1">
                  <button
                    type="button"
                    onClick={() => setJobsView('queue')}
                    className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${jobsView === 'queue' ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950 shadow-sm shadow-cyan-500/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'}`}
                  >
                    Queue
                    <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${jobsView === 'queue' ? 'bg-slate-950/30 text-slate-900' : 'bg-slate-700 text-slate-300'}`}>{queueCount}</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setJobsView('history')}
                    className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${jobsView === 'history' ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950 shadow-sm shadow-cyan-500/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'}`}
                  >
                    History
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-semibold ${jobsView === 'history' ? 'bg-slate-950/30 text-slate-900' : 'bg-slate-700 text-slate-300'}`}
                      title={historySearch ? `Showing ${visibleHistoryCount} filtered result(s)` : 'Total history entries'}
                    >
                      {totalHistoryCount}
                    </span>
                  </button>
                </div>
                {/* Action buttons for the active view */}
                {jobsView === 'queue' && (
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="relative">
                      <TextInput
                        type="text"
                        placeholder="Search queue…"
                        value={queueSearch}
                        onChange={(e) => { setQueueSearch(e.target.value); setJobsPage(1); }}
                        className="w-48 py-1.5 pr-8 text-xs"
                      />
                      {queueSearch && (
                        <button
                          type="button"
                          aria-label="Clear queue search"
                          onClick={() => { setQueueSearch(''); setJobsPage(1); }}
                          className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-xs font-semibold text-slate-400 transition-colors hover:text-slate-100"
                        >
                          x
                        </button>
                      )}
                    </div>
                    <SelectInput
                      value={queueSort}
                      onChange={(e) => { void handleQueueSortChange(e.target.value); }}
                      className="w-44 py-1.5 text-xs"
                    >
                      <option value="default">Date Added (Oldest)</option>
                      <option value="newest">Date Added (Newest)</option>
                      <option value="year_newest">Release Year (Newest)</option>
                      <option value="year_oldest">Release Year (Oldest)</option>
                    </SelectInput>
                    <Btn size="sm" variant="secondary" onClick={handleCancelAllQueued}>Cancel Queued Encodes</Btn>
                    <Btn size="sm" variant="secondary" onClick={handleClearQueue}>Clear Queue</Btn>
                    <Btn size="sm" variant="danger" onClick={handleAbortAllJobs}>Abort Active Encodes</Btn>
                    <Btn size="sm" variant="warning" onClick={() => handleQueueAction(queuePaused ? 'resume' : 'pause')}>
                      {queuePaused ? 'Resume New Jobs' : 'Pause New Jobs'}
                    </Btn>
                    <p className="text-xs text-slate-500">
                      Current encodes keep running. This only stops new queue starts.
                    </p>
                  </div>
                )}
                {jobsView === 'history' && (
                  <div className="flex flex-wrap items-center gap-2">
                    <div className="flex gap-1 rounded-xl border border-slate-700/70 bg-slate-950/60 p-1">
                      {[
                        ['all', 'All'],
                        ['encode', 'Encodes'],
                        ['download', 'Downloads'],
                      ].map(([value, label]) => (
                        <button
                          key={value}
                          type="button"
                          onClick={() => {
                            setHistoryTypeFilter(value);
                            setHistoryPage(1);
                          }}
                          className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-all duration-200 ${
                            historyTypeFilter === value
                              ? 'bg-slate-800 text-slate-100 shadow-sm shadow-slate-950/40'
                              : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'
                          }`}
                        >
                          {label}
                        </button>
                      ))}
                    </div>
                    <TextInput
                      type="text"
                      placeholder="Search history…"
                      value={historySearch}
                      onChange={(e) => {
                        setHistorySearch(e.target.value);
                        setHistoryPage(1);
                      }}
                      className="w-48 py-1.5 text-xs"
                    />
                    <SelectInput
                      value={historySort}
                      onChange={(e) => {
                        setHistorySort(e.target.value);
                        setHistoryPage(1);
                      }}
                      className="w-48 py-1.5 text-xs"
                    >
                      <option value="completed_desc">Completed (Newest)</option>
                      <option value="year_newest">Release Year (Newest)</option>
                      <option value="year_oldest">Release Year (Oldest)</option>
                    </SelectInput>
                    <Btn size="sm" variant="danger" onClick={handlePurgeHistory}>Clear History</Btn>
                  </div>
                )}
              </div>
              </div>
              {/* Queue tab content */}
              {jobsView === 'queue' && (
                <>
                  <div className="space-y-3 p-3 md:hidden">
                    {pagedJobs.length === 0 && (
                      <div className="rounded-xl border border-slate-700/70 bg-slate-900/60 px-4 py-10 text-center text-sm text-slate-500">
                        {queueSearch ? 'No matching jobs.' : 'No jobs in queue.'}
                      </div>
                    )}
                    {pagedJobs.map((item) => {
                      if (item._itemType === 'download') {
                        const dj = item;
                        const { year } = extractTitleYear(dj.source_file_path);
                        const title = getDisplayTitle(dj.source_file_path);
                        const libName = dj.library_id != null ? (libraryById[dj.library_id]?.name ?? '—') : '—';
                        const downloadActionPending = pendingDownloadActions[dj.id];
                        const elapsedStart = dj.download_started_at ?? dj.created_at;
                        const elapsedSeconds = getElapsedSeconds(elapsedStart, nowMs);
                        const elapsedLabel = formatElapsed(elapsedSeconds);
                        const showEta = ['searching', 'downloading', 'moving', 'importing'].includes(dj.status);
                        const showElapsed = shouldShowDownloadElapsed(dj.status);
                        const etaLabel = formatEta(getDownloadEtaSeconds(dj, nowMs)) ?? '—';
                        const speedLabel = formatDownloadSpeed(dj.download_speed_bps);
                        const retryLabel = formatDownloadRetry(dj);
                        const clientLabel = formatDownloadClient(dj.client_type);
                        const indexerLabel = dj.indexer_name || (dj.indexer_id != null ? `Indexer #${dj.indexer_id}` : null);
                        const statusLabel = dj.status === 'searching' && retryLabel ? 'retrying' : dj.status.replace(/_/g, ' ');
                        return (
                          <div key={`dl-mobile-${dj.id}`} className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
                            <div className="mb-2 flex items-center justify-between">
                              <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">Download</span>
                              <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {dj.id}</span>
                            </div>
                            <div className="flex items-start justify-between gap-2">
                              <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={dj.source_file_path}>{title || 'Unknown Title'}</p>
                              <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${dj.status === 'importing' ? 'border-violet-500/40 bg-violet-950/30 text-violet-300' : dj.status === 'searching' ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : dj.status === 'queued' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : dj.status === 'downloading' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : dj.status === 'moving' ? 'border-indigo-500/40 bg-indigo-950/30 text-indigo-300' : dj.status === 'stalled' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : dj.status === 'waiting_encode' ? 'border-fuchsia-500/40 bg-fuchsia-950/30 text-fuchsia-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>
                                {statusLabel}
                              </span>
                            </div>
                            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
                              <span>{year ?? '—'}</span>
                              <span>{libName}</span>
                            </div>
                            <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                              {showEta && <span>ETA: {etaLabel}</span>}
                              {dj.status === 'queued' && <span>Elapsed: waiting</span>}
                              {showElapsed && <span>Elapsed: {elapsedLabel}</span>}
                              {speedLabel && <span>Speed: {speedLabel}</span>}
                              {retryLabel && <span>{retryLabel}</span>}
                              {clientLabel && <span>Client: {clientLabel}</span>}
                              {indexerLabel && <span>Indexer: {indexerLabel}</span>}
                            </div>
                            {['downloading', 'moving'].includes(dj.status) && (
                              <div className="mt-2">
                                <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-700">
                                  <div className="h-full rounded-full bg-gradient-to-r from-violet-500 to-violet-400 transition-all duration-300" style={{ width: `${dj.progress_percent}%` }} />
                                </div>
                                <div className="mt-1 text-xs text-slate-500">{dj.progress_percent}%</div>
                              </div>
                            )}
                            {dj.error_message && <p className="mt-2.5 text-xs text-red-400">{dj.error_message}</p>}
                            <MobileActionMenu>
                              {['searching', 'queued', 'downloading', 'moving', 'stalled', 'importing', 'waiting_encode'].includes(dj.status) && (
                                <Btn size="sm" variant="danger" disabled={Boolean(downloadActionPending)} onClick={() => handleCancelDownloadJob(dj.id)}>
                                  {downloadActionPending === 'remove_reset' ? 'Working…' : 'Reset Search'}
                                </Btn>
                              )}
                              {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
                                <Btn size="sm" variant="primary" disabled={Boolean(downloadActionPending)} onClick={() => handleRetryDownloadJob(dj.id)}>
                                  {downloadActionPending === 'retry' ? 'Working…' : 'Retry Search'}
                                </Btn>
                              )}
                              <Btn size="sm" variant="secondary" disabled={Boolean(downloadActionPending)} onClick={() => handleDeleteDownloadJob(dj.id)}>
                                {downloadActionPending === 'delete' ? 'Working…' : 'Delete'}
                              </Btn>
                            </MobileActionMenu>
                          </div>
                        );
                      }

                      const job = item;
                      const jobActionPending = pendingJobActions[job.id];
                      const progress = progressFromJob(job);
                      const isRunning = job.status === 'running';
                      const eta = formatEta(job.eta_seconds);
                      const { year } = extractTitleYear(job.source_path);
                      const title = getDisplayTitle(job.source_path);
                      const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '—') : '—';
                      const activeDj = downloadJobBySource[job.source_path];
                      const djIsActive = activeDj && ['searching', 'queued', 'downloading', 'moving', 'importing'].includes(activeDj.status);
                      const jobLibProfile = libraryProfiles[job.library_id];
                      const jobDownloadEnabled = !!jobLibProfile?.download_enabled;
                      const queueWaitingForRoute = jobDownloadEnabled
                        && !djIsActive
                        && (
                          ['queued', 'pending', 'created', 'starting', 'preflight'].includes(job.status)
                          || (job.status === 'paused' && progress === 0)
                        );
                      const jobModeLabel = jobDownloadEnabled ? 'Auto' : 'Encode';
                      const jobStatusLabel = queueWaitingForRoute ? 'awaiting download route' : job.status;
                      return (
                        <div key={`job-mobile-${job.id}`} className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
                          <div className="mb-2 flex items-center justify-between">
                            <span className={`rounded-full border bg-slate-900/80 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] ${jobDownloadEnabled ? 'border-cyan-500/40 text-cyan-300' : 'border-slate-700 text-slate-400'}`}>{jobModeLabel}</span>
                            <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {job.id}</span>
                          </div>
                          <div className="flex items-start justify-between gap-2">
                            <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={job.source_path}>{title || 'Unknown Title'}</p>
                            <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium ${queueWaitingForRoute ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : job.status === 'running' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : job.status === 'paused' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{jobStatusLabel}</span>
                          </div>
                          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
                            <span>{year ?? '—'}</span>
                            <span>{libName}</span>
                          </div>
                          <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                            <span>{formatResolution(job.source_resolution)} · {formatHdrIndicator(job.source_is_hdr)}</span>
                            {job.encoder_used && <span>{job.encoder_used}{job.hwaccel_used ? ' (HW)' : ''}</span>}
                            {isRunning && job.fps != null && <span>{job.fps.toFixed(1)} fps</span>}
                            {isRunning && eta && <span>{eta}</span>}
                          </div>
                          {job.status === 'failed' && job.error_message && <p className="mt-2.5 text-xs text-red-400">{job.error_message}</p>}
                          {queueWaitingForRoute && <p className="mt-2.5 text-xs text-sky-400">Auto-routing: download first, encode fallback.</p>}
                          {djIsActive && activeDj.status === 'searching' && <p className="mt-2.5 text-xs text-sky-400">Searching…</p>}
                          {djIsActive && activeDj.status === 'queued' && <p className="mt-2.5 text-xs text-amber-300">Queued in client</p>}
                          {djIsActive && activeDj.status === 'downloading' && (
                            <p className="mt-2.5 text-xs text-violet-400">
                              Downloading {activeDj.progress_percent}%
                              {formatDownloadSpeed(activeDj.download_speed_bps) ? ` • ${formatDownloadSpeed(activeDj.download_speed_bps)}` : ''}
                              {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
                              {activeDj.indexer_name ? ` • ${activeDj.indexer_name}` : ''}
                            </p>
                          )}
                          {djIsActive && activeDj.status === 'moving' && (
                            <p className="mt-2.5 text-xs text-indigo-400">
                              Moving {activeDj.progress_percent}%
                              {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
                            </p>
                          )}
                          {djIsActive && activeDj.status === 'importing' && <p className="mt-2.5 text-xs text-violet-400">Importing…</p>}
                          <div className="mt-2.5">
                            <div className="h-1.5 w-full rounded-full bg-slate-700">
                              <div className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-cyan-400 transition-all duration-500" style={{ width: `${progress}%` }} />
                            </div>
                            <div className="mt-1 text-xs text-slate-500">{progress}%</div>
                          </div>
                          <MobileActionMenu>
                            {job.status === 'running' && <Btn size="sm" variant="warning" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('pause', job.id)}>{jobActionPending === 'pause' ? 'Working…' : 'Pause Encode'}</Btn>}
                            {job.status === 'paused' && progress > 0 && <Btn size="sm" variant="success" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('start', job.id)}>{jobActionPending === 'start' ? 'Working…' : 'Resume Now'}</Btn>}
                            {(job.status === 'queued' || (job.status === 'paused' && progress === 0)) && !jobDownloadEnabled && <Btn size="sm" variant="success" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('start', job.id)}>{jobActionPending === 'start' ? 'Working…' : 'Start Now'}</Btn>}
                            {job.status === 'interrupted' && <Btn size="sm" variant="primary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('requeue', job.id)}>{jobActionPending === 'requeue' ? 'Working…' : 'Requeue'}</Btn>}
                            {(ACTIVE_STATUSES.has(job.status) || (job.status === 'paused' && progress > 0)) && (
                              <Btn size="sm" variant="secondary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('discard', job.id)}>
                                {jobActionPending === 'discard' ? 'Working…' : (jobDownloadEnabled ? 'Search Instead' : 'Restart From Beginning')}
                              </Btn>
                            )}
                            {['queued', 'starting', 'running', 'paused', 'preflight'].includes(job.status) && (
                              <Btn size="sm" variant="danger" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('abort', job.id)}>
                                {jobActionPending === 'abort' ? 'Working…' : 'Abort'}
                              </Btn>
                            )}
                          </MobileActionMenu>
                        </div>
                      );
                    })}
                  </div>
                  <div className="hidden overflow-x-auto [scrollbar-gutter:stable] md:block">
                    <table className="min-w-[1024px] divide-y divide-slate-800">
                      <thead className="sticky top-0 z-10 bg-slate-900/95 backdrop-blur-sm">
                        <tr>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 xl:table-cell">ID</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Title</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Year</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Library</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Status</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 xl:table-cell">Details</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 xl:table-cell">Encoder</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Progress</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800/60">
                        {pagedJobs.length === 0 && (
                          <tr>
                            <td colSpan={9} className="px-4 py-10 text-center text-sm text-slate-500">
                              {queueSearch ? 'No matching jobs.' : 'No jobs in queue.'}
                            </td>
                          </tr>
                        )}
                        {pagedJobs.map((item) => {
                          // Download job row
                          if (item._itemType === 'download') {
                            const dj = item;
                            const { year } = extractTitleYear(dj.source_file_path);
                            const title = getDisplayTitle(dj.source_file_path);
                            const libName = dj.library_id != null ? (libraryById[dj.library_id]?.name ?? '—') : '—';
                            const downloadActionPending = pendingDownloadActions[dj.id];
                            const statusColor =
                              dj.status === 'queued' ? 'text-amber-400' :
                              dj.status === 'downloading' ? 'text-cyan-400' :
                              dj.status === 'moving' ? 'text-indigo-400' :
                              dj.status === 'importing' ? 'text-violet-400' :
                              dj.status === 'searching' ? 'text-sky-400' :
                              dj.status === 'stalled' ? 'text-amber-400' :
                              dj.status === 'waiting_encode' ? 'text-fuchsia-400' :
                              'text-slate-400';
                            const elapsedStart = dj.download_started_at ?? dj.created_at;
                            const elapsedSeconds = getElapsedSeconds(elapsedStart, nowMs);
                            const elapsedLabel = formatElapsed(elapsedSeconds);
                            const showEta = ['searching', 'downloading', 'moving', 'importing'].includes(dj.status);
                            const showElapsed = shouldShowDownloadElapsed(dj.status);
                            const etaLabel = formatEta(getDownloadEtaSeconds(dj, nowMs)) ?? '—';
                            const speedLabel = formatDownloadSpeed(dj.download_speed_bps);
                            const retryLabel = formatDownloadRetry(dj);
                            const clientLabel = formatDownloadClient(dj.client_type);
                            const indexerLabel = dj.indexer_name || (dj.indexer_id != null ? `Indexer #${dj.indexer_id}` : null);
                            const statusLabel = dj.status === 'searching' && retryLabel ? 'retrying' : dj.status.replace(/_/g, ' ');
                            return (
                              <tr key={`dl-${dj.id}`} className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
                                <td className="hidden px-4 py-3 text-xs text-slate-500 xl:table-cell">{dj.id}</td>
                                <td className="max-w-[180px] truncate px-4 py-3 text-sm text-slate-200" title={dj.source_file_path}>{title}</td>
                                <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
                                <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{libName}</td>
                                <td className="px-4 py-3 text-sm">
                                  <div className="flex items-center gap-1.5">
                                    {dj.status === 'searching' && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />}
                                    {dj.status === 'moving' && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-400" />}
                                    {dj.status === 'importing' && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-violet-400" />}
                                    <span className={`rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${statusColor === 'text-violet-400' ? 'border-violet-500/40 bg-violet-950/30 text-violet-300' : statusColor === 'text-indigo-400' ? 'border-indigo-500/40 bg-indigo-950/30 text-indigo-300' : statusColor === 'text-sky-400' ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : statusColor === 'text-cyan-400' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : statusColor === 'text-amber-400' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : statusColor === 'text-fuchsia-400' ? 'border-fuchsia-500/40 bg-fuchsia-950/30 text-fuchsia-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{statusLabel}</span>
                                  </div>
                                  {showEta && <p className="mt-0.5 text-xs text-slate-400">ETA: {etaLabel}</p>}
                                  {speedLabel && <p className="mt-0.5 text-xs text-slate-400">Speed: {speedLabel}</p>}
                                  {retryLabel && <p className="mt-0.5 text-xs text-amber-300">{retryLabel}</p>}
                                  {clientLabel && <p className="mt-0.5 text-xs text-slate-500">Client: {clientLabel}</p>}
                                  {indexerLabel && <p className="mt-0.5 text-xs text-slate-500">Indexer: {indexerLabel}</p>}
                                  {showElapsed && (
                                    <p className="mt-0.5 text-xs text-slate-500" title={elapsedSeconds == null ? 'Missing created_at timestamp' : `${elapsedSeconds}s elapsed`}>
                                      Elapsed: {elapsedLabel}
                                    </p>
                                  )}
                                  {dj.status === 'queued' && (
                                    <p className="mt-0.5 text-xs text-slate-500">Elapsed: waiting</p>
                                  )}
                                  {dj.error_message && <p className="mt-0.5 text-xs text-red-400">{dj.error_message}</p>}
                                </td>
                                <td className="hidden max-w-[180px] truncate px-4 py-3 text-xs text-slate-400 xl:table-cell" title={dj.search_query}>
                                  {dj.status === 'searching' && !dj.search_query ? <span className="italic text-slate-500">Building query…</span> : (dj.search_query ?? '—')}
                                </td>
                                <td className="hidden px-4 py-3 text-xs text-slate-600 xl:table-cell">—</td>
                                <td className="px-4 py-3">
                                  {['downloading', 'moving'].includes(dj.status) ? (
                                    <div>
                                      <div className="h-1.5 w-24 overflow-hidden rounded-full bg-slate-700 md:w-32">
                                        <div className="h-full rounded-full bg-gradient-to-r from-violet-500 to-violet-400 transition-all duration-300" style={{ width: `${dj.progress_percent}%` }} />
                                      </div>
                                      <div className="mt-1.5 text-xs text-slate-500">{dj.progress_percent}%</div>
                                    </div>
                                  ) : <span className="text-xs text-slate-600">—</span>}
                                </td>
                                <td className="px-4 py-3">
                                  <div className="flex flex-wrap gap-1.5">
                                    {['searching', 'queued', 'downloading', 'moving', 'stalled', 'importing', 'waiting_encode'].includes(dj.status) && (
                                      <Btn size="sm" variant="danger" disabled={Boolean(downloadActionPending)} onClick={() => handleCancelDownloadJob(dj.id)}>
                                        {downloadActionPending === 'remove_reset' ? 'Working…' : 'Reset Search'}
                                      </Btn>
                                    )}
                                    {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
                                      <Btn size="sm" variant="primary" disabled={Boolean(downloadActionPending)} onClick={() => handleRetryDownloadJob(dj.id)}>
                                        {downloadActionPending === 'retry' ? 'Working…' : 'Retry Search'}
                                      </Btn>
                                    )}
                                    <Btn size="sm" variant="secondary" disabled={Boolean(downloadActionPending)} onClick={() => handleDeleteDownloadJob(dj.id)}>
                                      {downloadActionPending === 'delete' ? 'Working…' : 'Delete'}
                                    </Btn>
                                  </div>
                                </td>
                              </tr>
                            );
                          }

                          // Encoding job row
                          const job = item;
                          const jobActionPending = pendingJobActions[job.id];
                          const progress = progressFromJob(job);
                          const isRunning = job.status === 'running';
                          const eta = formatEta(job.eta_seconds);
                          const { year } = extractTitleYear(job.source_path);
                          const title = getDisplayTitle(job.source_path);
                          const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '—') : '—';
                          const activeDj = downloadJobBySource[job.source_path];
                          const djIsActive = activeDj && ['searching', 'queued', 'downloading', 'moving', 'importing'].includes(activeDj.status);
                          const jobLibProfile = libraryProfiles[job.library_id];
                          const jobDownloadEnabled = !!jobLibProfile?.download_enabled;
                          const queueWaitingForRoute = jobDownloadEnabled
                            && !djIsActive
                            && (
                              ['queued', 'pending', 'created', 'starting', 'preflight'].includes(job.status)
                              || (job.status === 'paused' && progress === 0)
                            );
                          const jobStatusLabel = queueWaitingForRoute ? 'awaiting download route' : job.status;
                          return (
                            <tr key={job.id} className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
                              <td className="hidden px-4 py-3 text-xs text-slate-500 xl:table-cell">{job.id}</td>
                              <td className="max-w-[180px] truncate px-4 py-3 text-sm text-slate-200" title={job.source_path}>{title}</td>
                              <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
                              <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{libName}</td>
                              <td className="px-4 py-3 text-sm capitalize">
                                <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${queueWaitingForRoute ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : job.status === 'running' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : job.status === 'paused' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{jobStatusLabel}</span>
                                {queueWaitingForRoute && (
                                  <p className="mt-0.5 text-xs text-sky-400">Auto-routing: download first, encode fallback.</p>
                                )}
                                {job.status === 'failed' && job.error_message && (
                                  <p className="mt-0.5 text-xs text-red-400">{job.error_message}</p>
                                )}
                                {djIsActive && activeDj.status === 'searching' && (
                                  <p className="mt-0.5 flex items-center gap-1 text-xs text-sky-400">
                                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />
                                    Searching…
                                  </p>
                                )}
                                {djIsActive && activeDj.status === 'queued' && (
                                  <p className="mt-0.5 flex items-center gap-1 text-xs text-amber-300">
                                    <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-300" />
                                    Queued in client
                                  </p>
                                )}
                                {djIsActive && activeDj.status === 'downloading' && (
                                  <p className="mt-0.5 text-xs text-violet-400">
                                    ↓ Downloading {activeDj.progress_percent}%
                                    {formatDownloadSpeed(activeDj.download_speed_bps) ? ` • ${formatDownloadSpeed(activeDj.download_speed_bps)}` : ''}
                                    {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
                                    {activeDj.indexer_name ? ` • ${activeDj.indexer_name}` : ''}
                                  </p>
                                )}
                                {djIsActive && activeDj.status === 'moving' && (
                                  <p className="mt-0.5 flex items-center gap-1 text-xs text-indigo-400">
                                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-400" />
                                    Moving {activeDj.progress_percent}%
                                    {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
                                  </p>
                                )}
                                {djIsActive && activeDj.status === 'importing' && (
                                  <p className="mt-0.5 flex items-center gap-1 text-xs text-violet-400">
                                    <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-violet-400" />
                                    Importing…
                                  </p>
                                )}
                              </td>
                              <td className="hidden px-4 py-3 text-xs text-slate-400 xl:table-cell">
                                <span>{formatResolution(job.source_resolution)}</span>
                                <span className="mx-1.5 text-slate-600">·</span>
                                <span>{formatHdrIndicator(job.source_is_hdr)}</span>
                              </td>
                              <td className="hidden px-4 py-3 text-xs text-slate-400 xl:table-cell">
                                {job.encoder_used ? (
                                  <>
                                    <span className={job.hwaccel_used ? 'font-medium text-cyan-400' : ''}>{job.encoder_used}</span>
                                    {job.hwaccel_used && (
                                      <span className="ml-1.5 rounded bg-cyan-900/50 px-1.5 py-0.5 text-cyan-300">HW</span>
                                    )}
                                  </>
                                ) : (
                                  <span className="text-slate-600">—</span>
                                )}
                              </td>
                              <td className="px-4 py-3">
                                <div className="h-1.5 w-24 rounded-full bg-slate-700 md:w-32">
                                  <div
                                    className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-cyan-400 transition-all duration-500"
                                    style={{ width: `${progress}%` }}
                                  />
                                </div>
                                <div className="mt-1.5 flex items-center gap-2 text-xs text-slate-500">
                                  <span>{progress}%</span>
                                  {isRunning && job.fps != null && <span>{job.fps.toFixed(1)} fps</span>}
                                  {isRunning && eta && <span>{eta}</span>}
                                </div>
                              </td>
                              <td className="px-4 py-3">
                                <div className="flex flex-wrap gap-1.5">
                                  {job.status === 'running' && <Btn size="sm" variant="warning" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('pause', job.id)}>{jobActionPending === 'pause' ? 'Working…' : 'Pause Encode'}</Btn>}
                                  {job.status === 'paused' && progress > 0 && <Btn size="sm" variant="success" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('start', job.id)}>{jobActionPending === 'start' ? 'Working…' : 'Resume Now'}</Btn>}
                                  {(job.status === 'queued' || (job.status === 'paused' && progress === 0)) && !jobDownloadEnabled && <Btn size="sm" variant="success" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('start', job.id)}>{jobActionPending === 'start' ? 'Working…' : 'Start Now'}</Btn>}
                                  {job.status === 'interrupted' && <Btn size="sm" variant="primary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('requeue', job.id)}>{jobActionPending === 'requeue' ? 'Working…' : 'Requeue'}</Btn>}
                                  {(ACTIVE_STATUSES.has(job.status) || (job.status === 'paused' && progress > 0)) && (
                                    <Btn size="sm" variant="secondary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('discard', job.id)}>
                                      {jobActionPending === 'discard' ? 'Working…' : (jobDownloadEnabled ? 'Search Instead' : 'Restart From Beginning')}
                                    </Btn>
                                  )}
                                  {['queued', 'starting', 'running', 'paused', 'preflight'].includes(job.status) && (
                                    <Btn size="sm" variant="danger" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('abort', job.id)}>
                                      {jobActionPending === 'abort' ? 'Working…' : 'Abort'}
                                    </Btn>
                                  )}
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {totalJobPages > 1 && (
                    <div className="flex items-center justify-between border-t border-slate-700/70 px-5 py-3 text-sm text-slate-400">
                      <p>Page {jobsPage} of {totalJobPages}</p>
                      <div className="flex items-center gap-1">
                        {Array.from({ length: totalJobPages }, (_, i) => i + 1).map((pageNum) => (
                          <button
                            key={pageNum}
                            type="button"
                            onClick={() => setJobsPage(pageNum)}
                            className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all duration-150 ${jobsPage === pageNum ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
                          >
                            {pageNum}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}

              {/* History tab content */}
              {jobsView === 'history' && (
                <>
                  <div className="space-y-3 p-3 md:hidden">
                    {pagedHistoryItems.length === 0 && (
                      <div className="rounded-xl border border-slate-700/70 bg-slate-900/60 px-4 py-10 text-center text-sm text-slate-500">
                        {historySearch ? 'No matching history.' : 'No completed jobs yet.'}
                      </div>
                    )}
                    {pagedHistoryItems.map((item) => {
                      if (item._historyType === 'download') {
                        const dj = item;
                        const { year } = extractTitleYear(dj.source_file_path);
                        const title = getDisplayTitle(dj.source_file_path);
                        const libName = dj.library_id != null ? (libraryById[dj.library_id]?.name ?? '—') : '—';
                        const downloadActionPending = pendingDownloadActions[dj.id];
                        const completedDate = formatHistoryCompletedAt(dj.completed_at);
                        const clientLabel = formatDownloadClient(dj.client_type);
                        return (
                          <div key={`hist-mobile-download-${dj.id}`} className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
                            <div className="mb-2 flex items-center justify-between">
                              <HistoryTypeBadge type="download" />
                              <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {dj.id}</span>
                            </div>
                            <div className="flex items-start justify-between gap-2">
                              <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={dj.source_file_path}>{title || 'Unknown Title'}</p>
                              <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${dj.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : dj.status === 'failed' || dj.status === 'timed_out' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{dj.status.replace(/_/g, ' ')}</span>
                            </div>
                            <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
                              <span>{year ?? '—'}</span>
                              <span>{libName}</span>
                            </div>
                            <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                              <span>{clientLabel ? `Client: ${clientLabel}` : '—'}</span>
                            </div>
                            <p className="mt-2.5 text-xs text-slate-500">{completedDate}</p>
                            {dj.error_message && <p className="mt-2.5 text-xs text-red-400">{dj.error_message}</p>}
                            <MobileActionMenu>
                                    {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
                                      <Btn size="sm" variant="primary" disabled={Boolean(downloadActionPending)} onClick={() => handleRetryDownloadJob(dj.id)}>
                                  {downloadActionPending === 'retry' ? 'Working…' : 'Retry Search'}
                                      </Btn>
                                    )}
                              <Btn size="sm" variant="secondary" disabled={Boolean(downloadActionPending)} onClick={() => handleDeleteDownloadJob(dj.id)}>
                                {downloadActionPending === 'delete' ? 'Working…' : 'Remove'}
                              </Btn>
                            </MobileActionMenu>
                          </div>
                        );
                      }

                      const job = item;
                      const { year } = extractTitleYear(job.source_path);
                      const title = getDisplayTitle(job.source_path);
                      const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '—') : '—';
                      const jobActionPending = pendingJobActions[job.id];
                      const histJobDownloadEnabled = !!libraryProfiles[job.library_id]?.download_enabled;
                      const fallbackInfo = fallbackHistoryByEncodeJobId[job.id];
                      const completedDate = formatHistoryCompletedAt(job.completed_at);
                      const encodeDuration = formatElapsed(job.encode_duration_seconds);
                      return (
                        <div key={`hist-mobile-encode-${job.id}`} className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
                          <div className="mb-2 flex items-center justify-between">
                            <HistoryTypeBadge type="encode" />
                            <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {job.id}</span>
                          </div>
                          <div className="flex items-start justify-between gap-2">
                            <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={job.source_path}>{title || 'Unknown Title'}</p>
                            <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium ${job.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{job.status}</span>
                          </div>
                          <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
                            <span>{year ?? '—'}</span>
                            <span>{libName}</span>
                          </div>
                          <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
                            {fallbackInfo && <FallbackIndicator />}
                            <span>{formatResolution(job.source_resolution)} · {formatHdrIndicator(job.source_is_hdr)}</span>
                            {job.encoder_used && <span>{job.encoder_used}{job.hwaccel_used ? ' (HW)' : ''}</span>}
                            <span>Encode: {encodeDuration}</span>
                          </div>
                          <p className="mt-2.5 text-xs text-slate-500">{completedDate}</p>
                          {job.status === 'failed' && job.error_message && (
                            <p className="mt-2.5 text-xs text-red-400">{job.error_message}</p>
                          )}
                          <MobileActionMenu>
                            {['failed', 'cancelled'].includes(job.status) && (
                              <Btn size="sm" variant="primary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('retry', job.id)}>
                                {jobActionPending === 'retry' ? 'Working…' : (histJobDownloadEnabled ? 'Search Again' : 'Retry Encode')}
                              </Btn>
                            )}
                            <Btn size="sm" variant="secondary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('remove', job.id)}>
                              {jobActionPending === 'remove' ? 'Working…' : 'Remove'}
                            </Btn>
                          </MobileActionMenu>
                        </div>
                      );
                    })}
                  </div>
                  <div className="hidden overflow-x-auto [scrollbar-gutter:stable] md:block">
                    <table className="min-w-[1200px] divide-y divide-slate-800">
                      <thead className="sticky top-0 z-10 bg-slate-900/95 backdrop-blur-sm">
                        <tr>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Type</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 xl:table-cell">ID</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Title</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Year</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Library</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Status</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 xl:table-cell">Details</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 xl:table-cell">Encoder</th>
                          <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 xl:table-cell">Encode Time</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Completed</th>
                          <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Actions</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800/60">
                        {pagedHistoryItems.length === 0 && (
                          <tr>
                            <td colSpan={11} className="px-4 py-10 text-center text-sm text-slate-500">
                              {historySearch ? 'No matching history.' : 'No completed jobs yet.'}
                            </td>
                          </tr>
                        )}
                        {pagedHistoryItems.map((item) => {
                          if (item._historyType === 'download') {
                            const dj = item;
                            const { year } = extractTitleYear(dj.source_file_path);
                            const title = getDisplayTitle(dj.source_file_path);
                            const libName = dj.library_id != null ? (libraryById[dj.library_id]?.name ?? '—') : '—';
                            const downloadActionPending = pendingDownloadActions[dj.id];
                            const clientLabel = formatDownloadClient(dj.client_type);
                            const completedDate = formatHistoryCompletedAt(dj.completed_at);
                            return (
                              <tr key={`hist-download-${dj.id}`} className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
                                <td className="px-4 py-3 text-sm"><HistoryTypeBadge type="download" /></td>
                                <td className="hidden px-4 py-3 text-xs text-slate-500 xl:table-cell">{dj.id}</td>
                                <td className="max-w-[180px] truncate px-4 py-3 text-sm text-slate-200" title={dj.source_file_path}>{title}</td>
                                <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
                                <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{libName}</td>
                                <td className="px-4 py-3 text-sm">
                                  <span className={`rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${dj.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : dj.status === 'failed' || dj.status === 'timed_out' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{dj.status.replace(/_/g, ' ')}</span>
                                  {dj.error_message && <p className="mt-0.5 text-xs text-red-400">{dj.error_message}</p>}
                                </td>
                                <td className="hidden px-4 py-3 text-xs text-slate-400 xl:table-cell">{clientLabel ? `Client: ${clientLabel}` : '—'}</td>
                                <td className="hidden px-4 py-3 text-xs text-slate-400 xl:table-cell">—</td>
                                <td className="hidden px-4 py-3 text-xs text-slate-400 xl:table-cell">—</td>
                                <td className="whitespace-nowrap px-4 py-3 text-xs text-slate-400">{completedDate}</td>
                                <td className="whitespace-nowrap px-4 py-3">
                                  <div className="flex flex-wrap gap-1.5">
                                    {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
                                      <Btn size="sm" variant="primary" disabled={Boolean(downloadActionPending)} onClick={() => handleRetryDownloadJob(dj.id)}>
                                        {downloadActionPending === 'retry' ? 'Working…' : 'Retry Search'}
                                      </Btn>
                                    )}
                                    <Btn size="sm" variant="secondary" disabled={Boolean(downloadActionPending)} onClick={() => handleDeleteDownloadJob(dj.id)}>
                                      {downloadActionPending === 'delete' ? 'Working…' : 'Remove'}
                                    </Btn>
                                  </div>
                                </td>
                              </tr>
                            );
                          }

                          const job = item;
                          const { year } = extractTitleYear(job.source_path);
                          const title = getDisplayTitle(job.source_path);
                          const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '—') : '—';
                          const jobActionPending = pendingJobActions[job.id];
                          const histJobDownloadEnabled = !!libraryProfiles[job.library_id]?.download_enabled;
                          const fallbackInfo = fallbackHistoryByEncodeJobId[job.id];
                          const completedDate = formatHistoryCompletedAt(job.completed_at);
                          const encodeDuration = formatElapsed(job.encode_duration_seconds);
                          return (
                            <tr key={`hist-encode-${job.id}`} className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
                              <td className="px-4 py-3 text-sm"><HistoryTypeBadge type="encode" /></td>
                              <td className="hidden px-4 py-3 text-xs text-slate-500 xl:table-cell">{job.id}</td>
                              <td className="max-w-[180px] truncate px-4 py-3 text-sm text-slate-200" title={job.source_path}>{title}</td>
                              <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
                              <td className="hidden px-4 py-3 text-sm text-slate-400 lg:table-cell">{libName}</td>
                              <td className="px-4 py-3 text-sm capitalize">
                                <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${job.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{job.status}</span>
                                {job.status === 'failed' && job.error_message && (
                                  <p className="mt-0.5 text-xs text-red-400">{job.error_message}</p>
                                )}
                              </td>
                              <td className="hidden px-4 py-3 text-xs text-slate-400 xl:table-cell">
                                <div className="space-y-1">
                                  <div className="flex items-center gap-2">
                                    {fallbackInfo && <FallbackIndicator />}
                                    <span>{formatResolution(job.source_resolution)}</span>
                                    <span className="mx-1.5 text-slate-600">·</span>
                                    <span>{formatHdrIndicator(job.source_is_hdr)}</span>
                                  </div>
                                </div>
                              </td>
                              <td className="hidden px-4 py-3 text-xs text-slate-400 xl:table-cell">
                                {job.encoder_used ? (
                                  <>
                                    <span className={job.hwaccel_used ? 'font-medium text-cyan-400' : ''}>{job.encoder_used}</span>
                                    {job.hwaccel_used && (
                                      <span className="ml-1.5 rounded bg-cyan-900/50 px-1.5 py-0.5 text-cyan-300">HW</span>
                                    )}
                                  </>
                                ) : (
                                  <span className="text-slate-600">—</span>
                                )}
                              </td>
                              <td className="hidden whitespace-nowrap px-4 py-3 text-xs text-slate-400 xl:table-cell">{encodeDuration}</td>
                              <td className="whitespace-nowrap px-4 py-3 text-xs text-slate-400">{completedDate}</td>
                              <td className="whitespace-nowrap px-4 py-3">
                                <div className="flex flex-wrap gap-1.5">
                                    {['failed', 'cancelled'].includes(job.status) && (
                                      <Btn size="sm" variant="primary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('retry', job.id)}>
                                      {jobActionPending === 'retry' ? 'Working…' : (histJobDownloadEnabled ? 'Search Again' : 'Retry Encode')}
                                      </Btn>
                                    )}
                                  <Btn size="sm" variant="secondary" disabled={Boolean(jobActionPending)} onClick={() => handleJobAction('remove', job.id)}>
                                    {jobActionPending === 'remove' ? 'Working…' : 'Remove'}
                                  </Btn>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {totalHistoryPages > 1 && (
                    <div className="flex items-center justify-between border-t border-slate-700/70 px-5 py-3 text-sm text-slate-400">
                      <p>Page {historyPage} of {totalHistoryPages}</p>
                      <div className="flex items-center gap-1">
                        {Array.from({ length: totalHistoryPages }, (_, i) => i + 1).map((pageNum) => (
                          <button
                            key={pageNum}
                            type="button"
                            onClick={() => setHistoryPage(pageNum)}
                            className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all duration-150 ${historyPage === pageNum ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
                          >
                            {pageNum}
                          </button>
                        ))}
                      </div>
                    </div>
                  )}
                </>
              )}

            </div>

          </section>
        )}

        {/* ── Settings ───────────────────────────────────────────────────────── */}
        {activePage === 'settings' && settings && accountSettings && notificationSettings && plexSettings && (
          <section className="animate-fade-in space-y-5">
            <SectionCard>
              <SectionTitle>Account Settings</SectionTitle>
              <div className="grid gap-4 md:grid-cols-2">
                <FormField label="Username">
                  <TextInput
                    type="text"
                    value={accountForm.username}
                    onChange={(e) => setAccountForm((prev) => ({ ...prev, username: e.target.value }))}
                    autoComplete="username"
                  />
                </FormField>
                <FormField label="Current Password" hint="Required to change username/password and enable 2FA.">
                  <TextInput
                    type="password"
                    value={accountForm.currentPassword}
                    onChange={(e) => setAccountForm((prev) => ({ ...prev, currentPassword: e.target.value }))}
                    autoComplete="current-password"
                  />
                </FormField>
                <FormField label="New Password" hint="Optional. Minimum 12 characters." span2>
                  <TextInput
                    type="password"
                    value={accountForm.newPassword}
                    onChange={(e) => setAccountForm((prev) => ({ ...prev, newPassword: e.target.value }))}
                    autoComplete="new-password"
                  />
                </FormField>
                <FormField label="Confirm New Password" span2>
                  <TextInput
                    type="password"
                    value={accountForm.confirmNewPassword}
                    onChange={(e) => setAccountForm((prev) => ({ ...prev, confirmNewPassword: e.target.value }))}
                    autoComplete="new-password"
                  />
                </FormField>
              </div>

              <div className="mt-5 flex flex-wrap gap-3">
                <Btn variant="violet" disabled={savingAccountSettings} onClick={saveAccountSettings}>
                  {savingAccountSettings ? 'Saving…' : 'Save Account'}
                </Btn>
                <span className="rounded-full border border-slate-700 bg-slate-900/80 px-3 py-2 text-xs text-slate-300">
                  2FA: {accountSettings.two_factor_enabled ? 'Enabled' : 'Disabled'}
                </span>
              </div>

              {!accountSettings.two_factor_enabled && (
                <div className="mt-5 space-y-3 rounded-xl border border-slate-700/80 bg-slate-950/50 p-4">
                  <p className="text-sm font-medium text-slate-200">Enable Dual-Factor Authentication</p>
                  <p className="text-xs text-slate-500">Generate a TOTP secret, add it to your authenticator app, then verify with a live code.</p>
                  <div className="flex flex-wrap gap-3">
                    <Btn variant="primary" disabled={generatingAccountTotpSecret} onClick={openAccountQrCode}>
                      {generatingAccountTotpSecret ? 'Generating…' : 'Use QR Code'}
                    </Btn>
                    <Btn variant="secondary" disabled={generatingAccountTotpSecret} onClick={generateAccountTotpSecret}>
                      {generatingAccountTotpSecret ? 'Generating…' : 'Generate 2FA Secret'}
                    </Btn>
                  </div>
                  <div className="grid gap-4 md:grid-cols-2">
                    <FormField label="TOTP Secret" span2>
                      <TextInput
                        type="text"
                        value={accountTwoFactorDraft.totpSecret}
                        onChange={(e) => setAccountTwoFactorDraft((prev) => ({ ...prev, totpSecret: e.target.value, totpUri: '' }))}
                      />
                    </FormField>
                    <FormField label="Authenticator Code">
                      <TextInput
                        type="text"
                        inputMode="numeric"
                        value={accountTwoFactorDraft.totpCode}
                        onChange={(e) => setAccountTwoFactorDraft((prev) => ({ ...prev, totpCode: e.target.value }))}
                      />
                    </FormField>
                    <FormField label="Current Password">
                      <TextInput
                        type="password"
                        autoComplete="current-password"
                        value={accountTwoFactorDraft.currentPassword}
                        onChange={(e) => setAccountTwoFactorDraft((prev) => ({ ...prev, currentPassword: e.target.value }))}
                      />
                    </FormField>
                  </div>
                  <div>
                    <Btn variant="primary" disabled={enablingTwoFactor} onClick={enableTwoFactorForAccount}>
                      {enablingTwoFactor ? 'Enabling…' : 'Enable 2FA'}
                    </Btn>
                  </div>
                </div>
              )}

              {accountSettings.two_factor_enabled && (
                <div className="mt-5 space-y-3 rounded-xl border border-red-900/40 bg-red-950/10 p-4">
                  <p className="text-sm font-medium text-red-200">Disable Dual-Factor Authentication</p>
                  <p className="text-xs text-red-300/80">For security, confirm with your current password and a valid authenticator code.</p>
                  <div className="grid gap-4 md:grid-cols-2">
                    <FormField label="Current Password">
                      <TextInput
                        type="password"
                        autoComplete="current-password"
                        value={accountDisableTwoFactorDraft.currentPassword}
                        onChange={(e) => setAccountDisableTwoFactorDraft((prev) => ({ ...prev, currentPassword: e.target.value }))}
                      />
                    </FormField>
                    <FormField label="Authenticator Code">
                      <TextInput
                        type="text"
                        inputMode="numeric"
                        value={accountDisableTwoFactorDraft.totpCode}
                        onChange={(e) => setAccountDisableTwoFactorDraft((prev) => ({ ...prev, totpCode: e.target.value }))}
                      />
                    </FormField>
                  </div>
                  <div>
                    <Btn variant="danger" disabled={disablingTwoFactor} onClick={disableTwoFactorForAccount}>
                      {disablingTwoFactor ? 'Disabling…' : 'Disable 2FA'}
                    </Btn>
                  </div>
                </div>
              )}
            </SectionCard>

            <SectionCard>
              <SectionTitle>General Settings</SectionTitle>
              <div className="grid gap-4 md:grid-cols-2">
                <FormField label="History Retention (Days)" hint="How long to keep completed job history.">
                  <TextInput type="number" min={1} value={settings.history_retention_days} onChange={(e) => setSettings((prev) => ({ ...prev, history_retention_days: Number(e.target.value) }))} />
                </FormField>

                <FormField label="Discovery Interval (Minutes)" hint="How often to scan libraries when using interval discovery.">
                  <TextInput type="number" min={1} value={settings.discovery_interval_minutes} onChange={(e) => setSettings((prev) => ({ ...prev, discovery_interval_minutes: Number(e.target.value) }))} />
                </FormField>

                <FormField label="Discovery Method" hint="How Optimizarr discovers new media files. Watcher mode avoids repeated full-library rescans.">
                  <SelectInput value={settings.discovery_method} onChange={(e) => setSettings((prev) => ({ ...prev, discovery_method: e.target.value }))}>
                    <option value="interval">On Interval</option>
                    <option value="watcher">Watcher</option>
                  </SelectInput>
                </FormField>

                <FormField label="Workspace Root" hint="Temporary directory used during encoding.">
                  <TextInput type="text" value={settings.workspace_root} onChange={(e) => setSettings((prev) => ({ ...prev, workspace_root: e.target.value }))} />
                </FormField>

                <FormField label="Scan Probe Workers" hint="Parallel metadata probes during discovery scans. Values above available CPU cores are clamped automatically.">
                  <TextInput
                    type="number"
                    min={1}
                    value={settings.scan_probe_workers}
                    disabled={settings.discovery_method === 'watcher'}
                    className={settings.discovery_method === 'watcher' ? 'cursor-not-allowed border-slate-700/70 bg-slate-900/40 text-slate-500' : ''}
                    onChange={(e) => setSettings((prev) => ({ ...prev, scan_probe_workers: Number(e.target.value) }))}
                  />
                  {settings.discovery_method === 'watcher' && (
                    <p className="text-xs text-slate-500">Watcher mode probes new files one at a time, so probe workers are not used.</p>
                  )}
                </FormField>

                <FormField label="Minimum Free Disk (GB)" hint="Pause the queue when free disk drops below this threshold.">
                  <TextInput type="number" min={1} value={settings.min_free_gb} onChange={(e) => setSettings((prev) => ({ ...prev, min_free_gb: Number(e.target.value) }))} />
                </FormField>
              </div>

              <div className="mt-4 space-y-3">
                <div className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-200">Auto Discovery</p>
                    <p className="text-xs text-slate-500">Automatically scan libraries for new media files.</p>
                  </div>
                  <Toggle
                    checked={settings.auto_discovery_enabled}
                    onChange={(e) => setSettings((prev) => ({ ...prev, auto_discovery_enabled: e.target.checked }))}
                  />
                </div>

                <div className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-200">Requeue Interrupted Jobs on Startup</p>
                    <p className="text-xs text-slate-500">Automatically re-add jobs that were interrupted by an unexpected shutdown.</p>
                  </div>
                  <Toggle
                    checked={settings.requeue_interrupted_jobs}
                    onChange={(e) => setSettings((prev) => ({ ...prev, requeue_interrupted_jobs: e.target.checked }))}
                  />
                </div>

                <div className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-200">Clean Up Workspaces on Startup</p>
                    <p className="text-xs text-slate-500">Remove leftover temporary encoding directories on startup.</p>
                  </div>
                  <Toggle
                    checked={settings.cleanup_workspaces_on_startup}
                    onChange={(e) => setSettings((prev) => ({ ...prev, cleanup_workspaces_on_startup: e.target.checked }))}
                  />
                </div>
              </div>

              <div className="mt-5 flex flex-wrap gap-3">
                <Btn variant="indigo" onClick={handleRecoveryRun}>Run Recovery Now</Btn>
                <Btn variant="primary" onClick={handleCleanupRun}>Run Workspace Cleanup</Btn>
                <Btn variant="warning" onClick={handleDuplicateOptimizedCleanupRun}>Remove Duplicate Optimized Outputs</Btn>
                <Btn variant="warning" onClick={handleOptimizedCleanupRun}>Remove Optimized Outputs</Btn>
              </div>

              <div className="mt-5">
                <Btn variant="primary" size="lg" disabled={savingSettings} onClick={saveSettings}>
                  {savingSettings ? 'Saving…' : 'Save Settings'}
                </Btn>
              </div>
            </SectionCard>

            {/* Email Notifications */}
            <SectionCard>
              <SectionTitle>Email Notifications</SectionTitle>
              <div className="grid gap-4 md:grid-cols-2">
                <FormField label="SMTP Host">
                  <TextInput type="text" value={notificationSettings.smtp_host} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_host: e.target.value }))} placeholder="smtp.example.com" />
                </FormField>
                <FormField label="SMTP Port">
                  <TextInput type="number" min={1} max={65535} value={notificationSettings.smtp_port} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_port: Number(e.target.value) }))} />
                </FormField>
                <FormField label="SMTP Username">
                  <TextInput type="text" value={notificationSettings.smtp_user} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_user: e.target.value }))} />
                </FormField>
                <FormField label="SMTP Password">
                  <TextInput type="password" value={notificationSettings.smtp_password} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_password: e.target.value }))} />
                </FormField>
                <FormField label="From Email">
                  <TextInput type="email" value={notificationSettings.from_email} onChange={(e) => setNotificationSettings((prev) => ({ ...prev, from_email: e.target.value }))} placeholder="noreply@example.com" />
                </FormField>
                <div className="flex items-end pb-1">
                  <Toggle
                    checked={notificationSettings.smtp_tls}
                    onChange={(e) => setNotificationSettings((prev) => ({ ...prev, smtp_tls: e.target.checked }))}
                    label="Use TLS"
                  />
                </div>
                <FormField label="Recipient Emails" hint="Comma or newline separated." span2>
                  <textarea
                    className="w-full rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-sm text-slate-100 outline-none transition-all duration-150 focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30"
                    rows={3}
                    value={notificationSettings.to_emails.join(', ')}
                    onChange={(e) => setNotificationSettings((prev) => ({
                      ...prev,
                      to_emails: e.target.value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean),
                    }))}
                    placeholder="user@example.com, other@example.com"
                  />
                </FormField>
              </div>

              <div className="mt-4">
                <p className="mb-3 text-xs font-semibold uppercase tracking-wider text-slate-400">Notify On</p>
                <div className="space-y-2">
                  {[
                    { key: 'job_complete', label: 'Job Complete' },
                    { key: 'job_failed', label: 'Job Failed' },
                    { key: 'job_interrupted', label: 'Job Interrupted' },
                    { key: 'low_disk_pause', label: 'Low Disk Pause' },
                    { key: 'recovery_ran', label: 'Recovery Ran' },
                    { key: 'batch_complete', label: 'Batch Complete' },
                  ].map(({ key, label }) => (
                    <div key={key} className="flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-2.5">
                      <span className="text-sm text-slate-200">{label}</span>
                      <Toggle
                        checked={notificationSettings.notify_on[key]}
                        onChange={(e) => setNotificationSettings((prev) => ({ ...prev, notify_on: { ...prev.notify_on, [key]: e.target.checked } }))}
                      />
                    </div>
                  ))}
                </div>
              </div>

              <div className="mt-5 flex flex-wrap gap-3">
                <Btn variant="violet" disabled={savingSettings} onClick={saveNotificationSettings}>
                  Save Notification Settings
                </Btn>
                <Btn variant="secondary" onClick={sendNotificationTest}>
                  Send Test Email
                </Btn>
              </div>
            </SectionCard>

            {/* Prowlarr Integration */}
            {prowlarrSettings && (
              <SectionCard>
                <SectionTitle>Prowlarr Integration</SectionTitle>
                <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-200">Enable Prowlarr</p>
                    <p className="text-xs text-slate-500">Search Prowlarr indexers for pre-encoded releases instead of transcoding.</p>
                  </div>
                  <Toggle
                    checked={prowlarrSettings.enabled}
                    onChange={(e) => setProwlarrSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
                  />
                </div>
                <div className="grid gap-4 md:grid-cols-2">
                  <FormField label="Prowlarr Host" hint="Protocol, hostname and port, e.g. http://192.168.1.100:9696" span2>
                    <TextInput
                      type="text"
                      value={prowlarrSettings.host}
                      onChange={(e) => setProwlarrSettings((prev) => ({ ...prev, host: e.target.value }))}
                      placeholder="http://localhost:9696"
                    />
                  </FormField>
                  <FormField label="API Key" hint="Found in Prowlarr → Settings → General → API Key" span2>
                    <TextInput
                      type="password"
                      value={prowlarrSettings.api_key}
                      onChange={(e) => setProwlarrSettings((prev) => ({ ...prev, api_key: e.target.value }))}
                      placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                    />
                  </FormField>
                </div>
                <div className="mt-5 flex flex-wrap gap-3">
                  <Btn variant="violet" disabled={savingProwlarrSettings} onClick={saveProwlarrSettings}>
                    {savingProwlarrSettings ? 'Saving…' : 'Save Prowlarr Settings'}
                  </Btn>
                  <Btn variant="secondary" disabled={testingProwlarrConnection} onClick={handleTestProwlarrConnection}>
                    {testingProwlarrConnection ? 'Testing…' : 'Test Connection'}
                  </Btn>
                </div>
              </SectionCard>
            )}

            {/* qBittorrent */}
            {qbtSettings && (
              <SectionCard>
                <SectionTitle>qBittorrent</SectionTitle>
                <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-200">Enable qBittorrent</p>
                    <p className="text-xs text-slate-500">Used for torrent releases from Prowlarr. Downloads tagged with "optimizarr" and left to follow qBittorrent's own seeding rules after import.</p>
                  </div>
                  <Toggle
                    checked={qbtSettings.enabled}
                    onChange={(e) => setQbtSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
                  />
                </div>
                <div className="grid gap-4 md:grid-cols-2">
                  <FormField label="Host" hint="Protocol and hostname, e.g. http://192.168.1.100">
                    <TextInput
                      type="text"
                      value={qbtSettings.host}
                      onChange={(e) => setQbtSettings((prev) => ({ ...prev, host: e.target.value }))}
                      placeholder="http://localhost"
                    />
                  </FormField>
                  <FormField label="Port">
                    <TextInput
                      type="number"
                      min={1}
                      max={65535}
                      value={qbtSettings.port}
                      onChange={(e) => setQbtSettings((prev) => ({ ...prev, port: Number(e.target.value) }))}
                    />
                  </FormField>
                  <FormField label="Username">
                    <TextInput
                      type="text"
                      value={qbtSettings.username}
                      onChange={(e) => setQbtSettings((prev) => ({ ...prev, username: e.target.value }))}
                      placeholder="admin"
                    />
                  </FormField>
                  <FormField label="Password">
                    <TextInput
                      type="password"
                      value={qbtSettings.password}
                      onChange={(e) => setQbtSettings((prev) => ({ ...prev, password: e.target.value }))}
                      placeholder="••••••••"
                    />
                  </FormField>
                </div>
                <div className="mt-5 flex flex-wrap gap-3">
                  <Btn variant="violet" disabled={savingQbtSettings} onClick={saveQbtSettings}>
                    {savingQbtSettings ? 'Saving…' : 'Save qBittorrent Settings'}
                  </Btn>
                  <Btn variant="secondary" disabled={testingQbtConnection} onClick={handleTestQbtConnection}>
                    {testingQbtConnection ? 'Testing…' : 'Test Connection'}
                  </Btn>
                </div>
              </SectionCard>
            )}

            {/* SABnzbd */}
            {sabSettings && (
              <SectionCard>
                <SectionTitle>SABnzbd</SectionTitle>
                <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                  <div>
                    <p className="text-sm font-medium text-slate-200">Enable SABnzbd</p>
                    <p className="text-xs text-slate-500">Used for usenet/NZB releases from Prowlarr. History entry is automatically removed from SABnzbd after a successful import.</p>
                  </div>
                  <Toggle
                    checked={sabSettings.enabled}
                    onChange={(e) => setSabSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
                  />
                </div>
                <div className="grid gap-4 md:grid-cols-2">
                  <FormField label="Host" hint="Protocol and hostname, e.g. http://192.168.1.100">
                    <TextInput
                      type="text"
                      value={sabSettings.host}
                      onChange={(e) => setSabSettings((prev) => ({ ...prev, host: e.target.value }))}
                      placeholder="http://localhost"
                    />
                  </FormField>
                  <FormField label="Port">
                    <TextInput
                      type="number"
                      min={1}
                      max={65535}
                      value={sabSettings.port}
                      onChange={(e) => setSabSettings((prev) => ({ ...prev, port: Number(e.target.value) }))}
                    />
                  </FormField>
                  <FormField label="API Key" span2>
                    <TextInput
                      type="password"
                      value={sabSettings.api_key}
                      onChange={(e) => setSabSettings((prev) => ({ ...prev, api_key: e.target.value }))}
                      placeholder="xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                    />
                  </FormField>
                </div>
                <div className="mt-5 flex flex-wrap gap-3">
                  <Btn variant="violet" disabled={savingSabSettings} onClick={saveSabSettings}>
                    {savingSabSettings ? 'Saving…' : 'Save SABnzbd Settings'}
                  </Btn>
                  <Btn variant="secondary" disabled={testingSabConnection} onClick={handleTestSabConnection}>
                    {testingSabConnection ? 'Testing…' : 'Test Connection'}
                  </Btn>
                </div>
              </SectionCard>
            )}

            {/* Plex Integration */}
            <SectionCard>
              <SectionTitle>Plex Integration</SectionTitle>
              <div className="mb-4 flex items-center justify-between rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-3">
                <div>
                  <p className="text-sm font-medium text-slate-200">Enable Plex Scan</p>
                  <p className="text-xs text-slate-500">Trigger a Plex library scan after each file finishes encoding.</p>
                </div>
                <Toggle
                  checked={plexSettings.enabled}
                  onChange={(e) => setPlexSettings((prev) => ({ ...prev, enabled: e.target.checked }))}
                />
              </div>
              <div className="grid gap-4 md:grid-cols-2">
                <FormField label="Plex Host" hint="Protocol and hostname, e.g. http://192.168.1.100">
                  <TextInput
                    type="text"
                    value={plexSettings.host}
                    onChange={(e) => setPlexSettings((prev) => ({ ...prev, host: e.target.value }))}
                    placeholder="http://localhost"
                  />
                </FormField>
                <FormField label="Port">
                  <TextInput
                    type="number"
                    min={1}
                    max={65535}
                    value={plexSettings.port}
                    onChange={(e) => setPlexSettings((prev) => ({ ...prev, port: Number(e.target.value) }))}
                  />
                </FormField>
                <FormField label="Plex Token" hint="Found in Plex account settings under Authorized Devices." span2>
                  <TextInput
                    type="password"
                    value={plexSettings.token}
                    onChange={(e) => setPlexSettings((prev) => ({ ...prev, token: e.target.value }))}
                    placeholder="xxxxxxxxxxxxxxxxxxxx"
                  />
                </FormField>
              </div>
              {plexLibraries.length > 0 && (
                <div className="mt-4">
                  <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">Discovered Sections</p>
                  <div className="space-y-1">
                    {plexLibraries.map((section) => (
                      <div key={section.id} className="flex items-center gap-2 rounded-lg border border-slate-800/60 bg-slate-950/30 px-4 py-2 text-sm">
                        <span className="rounded bg-slate-700 px-1.5 py-0.5 text-xs font-mono text-slate-300">{section.id}</span>
                        <span className="text-slate-200">{section.name}</span>
                        <span className="ml-auto text-xs text-slate-500">{section.type}</span>
                      </div>
                    ))}
                  </div>
                  <p className="mt-2 text-xs text-slate-500">Assign sections to each library in the Libraries tab.</p>
                </div>
              )}
              <div className="mt-5 flex flex-wrap gap-3">
                <Btn variant="violet" disabled={savingPlexSettings} onClick={savePlexSettings}>
                  {savingPlexSettings ? 'Saving…' : 'Save Plex Settings'}
                </Btn>
                <Btn variant="secondary" disabled={testingPlexConnection} onClick={handleTestPlexConnection}>
                  {testingPlexConnection ? 'Testing…' : 'Test Connection'}
                </Btn>
                <Btn variant="secondary" disabled={loadingPlexLibraries} onClick={loadPlexLibraries}>
                  {loadingPlexLibraries ? 'Loading…' : 'Load Sections'}
                </Btn>
              </div>
            </SectionCard>
          </section>
        )}

      </div>
      <Modal open={qrModal.open} onClose={closeQrModal} title={qrModal.title}>
        <div className="space-y-3">
          {qrModal.subtitle && <p className="text-xs text-slate-300">{qrModal.subtitle}</p>}
          <div className="rounded-xl border border-slate-700 bg-slate-950/70 p-3">
            {qrImageBusy && <p className="text-xs text-slate-400">Rendering QR code…</p>}
            {!qrImageBusy && qrImageDataUrl && (
              <img src={qrImageDataUrl} alt="Authenticator setup QR code" className="mx-auto h-64 w-64 rounded-md bg-white p-2" />
            )}
            {qrImageError && <p className="text-xs text-red-400">{qrImageError}</p>}
          </div>
          <FormField label="Manual Secret (fallback)">
            <TextInput readOnly value={qrModal.secret} />
          </FormField>
        </div>
      </Modal>
    </main>
  );
}
