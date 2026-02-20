import { useEffect, useMemo, useRef, useState } from 'react';
import {
  abortAllJobs,
  abortJob,
  cancelJob,
  discardJobProgress,
  createLibrary,
  deleteJob,
  deleteLibrary,
  fetchLibraries,
  fetchEncoders,
  fetchLibraryProfile,
  fetchJobs,
  fetchMetrics,
  fetchNotificationSettings,
  fetchPlexLibraries,
  fetchPlexSettings,
  fetchQueueStatus,
  fetchSettings,
  fetchWsToken,
  pauseJob,
  pauseQueue,
  purgeHistory,
  removeAllJobs,
  requeueJob,
  resumeJob,
  resumeQueue,
  retryJob,
  startJob,
  runCleanup,
  runOptimizedCleanup,
  runRecovery,
  scanLibrary,
  sendTestNotification,
  testPlexConnection,
  updateLibrary,
  updateLibraryProfile,
  updateNotificationSettings,
  updatePlexSettings,
  updateSettings,
} from './api';
import StatCard from './components/StatCard';

const WS_PATH = '/ws';
const FALLBACK_AFTER_MS = 30000;
const FALLBACK_POLL_MS = 10000;
const METRICS_POLL_MS = 10000;
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const MESSAGE_DISMISS_MS = 5000;
const JOBS_PAGE_SIZE = 50;
const HISTORY_PAGE_SIZE = 50;
const JOBS_UI_PREFS_KEY = 'optimizarr.jobsUiPrefs.v1';

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

function isAbortedJob(job) {
  return job.status?.toLowerCase() === 'failed' && job.error_message === 'Aborted by user';
}

function formatHour(hour) {
  return `${String(hour).padStart(2, '0')}:00`;
}

function parseHour(timeValue) {
  const [hour] = timeValue.split(':');
  return Number(hour);
}

function isWithinWindow(currentHour, startHour, endHour) {
  if (startHour <= endHour) {
    return currentHour >= startHour && currentHour <= endHour;
  }
  return currentHour >= startHour || currentHour <= endHour;
}

const ACTIVE_STATUSES = new Set(['starting', 'running', 'preflight']);
const PAUSED_STATUSES = new Set(['paused', 'paused_schedule']);
const QUEUED_STATUSES = new Set(['pending', 'queued', 'created']);
const TERMINAL_STATUSES = new Set(['complete', 'failed', 'skipped', 'cancelled']);

function libraryQueueCount(library, jobs) {
  return jobs.filter((job) => {
    if (!QUEUED_STATUSES.has(job.status?.toLowerCase())) return false;
    // Prefer library_id match (accurate); fall back to path prefix for legacy data
    if (job.library_id != null) return job.library_id === library.id;
    return job.source_path === library.path || job.source_path?.startsWith(`${library.path}/`);
  }).length;
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

function extractTitleYear(filePath) {
  const fileName = (filePath || '').split('/').pop() || '';
  const stem = fileName.replace(/\.[^.]+$/, '');
  // Normalize dot/underscore separators to spaces
  const spaced = stem.replace(/[._]/g, ' ').trim();
  // Prefer year enclosed in parentheses e.g. (2019)
  const parenMatch = spaced.match(/\(((19|20)\d{2})\)/);
  if (parenMatch) {
    const title = spaced.slice(0, spaced.indexOf(parenMatch[0])).replace(/\s+$/, '').trim();
    return { title: title || spaced, year: parenMatch[1] };
  }
  // Fall back: first standalone 4-digit year (1900-2099)
  const yearMatch = spaced.match(/\b((19|20)\d{2})\b/);
  if (yearMatch) {
    const yearIdx = spaced.indexOf(yearMatch[0]);
    const title = spaced.slice(0, yearIdx).replace(/[\s\-]+$/, '').trim();
    return { title: title || spaced, year: yearMatch[1] };
  }
  return { title: spaced, year: null };
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
  return errors;
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
    <div className={`rounded-xl border border-slate-800 bg-slate-900/80 p-5 shadow-lg shadow-slate-950/40 ${className}`}>
      {children}
    </div>
  );
}

function SectionTitle({ children }) {
  return (
    <h2 className="mb-4 text-xs font-semibold uppercase tracking-widest text-slate-400">{children}</h2>
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
      className={`w-full rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-sm text-slate-100 placeholder-slate-500 outline-none transition-all duration-150 focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30 ${className}`}
      {...props}
    />
  );
}

function SelectInput({ children, className = '', ...props }) {
  return (
    <select
      className={`w-full rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-2 text-sm text-slate-100 outline-none transition-all duration-150 focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30 ${className}`}
      {...props}
    >
      {children}
    </select>
  );
}

function Btn({ variant = 'primary', size = 'md', className = '', children, ...props }) {
  const base = 'inline-flex items-center justify-center rounded-lg font-semibold transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-offset-slate-900 disabled:opacity-50 disabled:cursor-not-allowed active:scale-95';
  const sizes = {
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
    lg: 'px-5 py-2.5 text-sm',
  };
  const variants = {
    primary: 'bg-cyan-500 text-slate-950 hover:bg-cyan-400 focus:ring-cyan-500',
    danger: 'bg-rose-600 text-white hover:bg-rose-500 focus:ring-rose-500',
    warning: 'bg-amber-500 text-slate-950 hover:bg-amber-400 focus:ring-amber-500',
    success: 'bg-emerald-500 text-slate-950 hover:bg-emerald-400 focus:ring-emerald-500',
    secondary: 'border border-slate-600 bg-slate-800 text-slate-100 hover:bg-slate-700 focus:ring-slate-500',
    violet: 'bg-violet-500 text-slate-950 hover:bg-violet-400 focus:ring-violet-500',
    indigo: 'bg-indigo-500 text-slate-950 hover:bg-indigo-400 focus:ring-indigo-500',
  };
  return (
    <button type="button" className={`${base} ${sizes[size]} ${variants[variant]} ${className}`} {...props}>
      {children}
    </button>
  );
}

function Toggle({ checked, onChange, label }) {
  return (
    <label className="flex cursor-pointer items-center gap-3">
      <div className="relative">
        <input type="checkbox" className="sr-only" checked={checked} onChange={onChange} />
        <div className={`h-5 w-9 rounded-full transition-colors duration-200 ${checked ? 'bg-cyan-500' : 'bg-slate-700'}`} />
        <div className={`absolute top-0.5 left-0.5 h-4 w-4 rounded-full bg-white shadow transition-transform duration-200 ${checked ? 'translate-x-4' : 'translate-x-0'}`} />
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
    <div className="flex items-center gap-1.5">
      <span className={`inline-block h-2 w-2 rounded-full shadow-sm ${colors[status] ?? colors.connecting} ${status === 'online' ? 'animate-pulse' : ''}`} />
      <span className="text-xs text-slate-400">{labels[status] ?? status}</span>
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
  const [notificationSettings, setNotificationSettings] = useState();
  const [plexSettings, setPlexSettings] = useState();
  const [plexLibraries, setPlexLibraries] = useState([]);
  const [loadingPlexLibraries, setLoadingPlexLibraries] = useState(false);
  const [savingPlexSettings, setSavingPlexSettings] = useState(false);
  const [testingPlexConnection, setTestingPlexConnection] = useState(false);
  const [selectedLibraryId, setSelectedLibraryId] = useState(null);
  const [profileDraft, setProfileDraft] = useState(null);
  const [profileErrors, setProfileErrors] = useState({});
  const [selectedPreset, setSelectedPreset] = useState('balanced');
  const [savingProfile, setSavingProfile] = useState(false);
  const [scanningLibraries, setScanningLibraries] = useState({});
  const [libraryDraft, setLibraryDraft] = useState({ name: '', path: '/media/', enabled: true });
  const [libraryFormErrors, setLibraryFormErrors] = useState({});
  const [savingLibrary, setSavingLibrary] = useState(false);
  const [deletingLibraryId, setDeletingLibraryId] = useState(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [savingSettings, setSavingSettings] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState('connecting');
  const [fallbackPollingEnabled, setFallbackPollingEnabled] = useState(false);
  const [queuePaused, setQueuePaused] = useState(false);
  const [toasts, setToasts] = useState([]);
  const [availableEncodersByCodec, setAvailableEncodersByCodec] = useState({});
  const [jobsPage, setJobsPage] = useState(1);
  const [historyPage, setHistoryPage] = useState(1);
  const [queueSearch, setQueueSearch] = useState(() => String(jobsUiPrefs.queueSearch ?? ''));
  const [historySearch, setHistorySearch] = useState(() => String(jobsUiPrefs.historySearch ?? ''));
  const [queueSort, setQueueSort] = useState(() => String(jobsUiPrefs.queueSort ?? 'default'));
  const [historySort, setHistorySort] = useState(() => String(jobsUiPrefs.historySort ?? 'completed_desc'));
  const [jobsView, setJobsView] = useState(() => (jobsUiPrefs.jobsView === 'history' ? 'history' : 'queue'));
  const [nowHour, setNowHour] = useState(() => new Date().getHours());
  // Abort dialog: when the user clicks Abort on a paused job that has partial
  // progress we ask whether they want to remove it entirely or just clear the
  // progress and keep it in the queue.
  const [abortDialogJobId, setAbortDialogJobId] = useState(null);

  const wsRef = useRef();
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef();
  const fallbackTimerRef = useRef();
  const intentionallyClosedRef = useRef(false);
  const toastTimersRef = useRef({});

  const queueCount = useMemo(
    () => jobs.filter((job) => QUEUED_STATUSES.has(job.status?.toLowerCase())).length,
    [jobs],
  );

  // Build a lookup map: library id → library name
  const libraryById = useMemo(
    () => Object.fromEntries(libraries.map((lib) => [lib.id, lib])),
    [libraries],
  );

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

  const sortedHistoryJobs = useMemo(
    () => sortJobsByOption(historyJobs, historySort, (a, b) => {
      if (a.completed_at && b.completed_at) return b.completed_at.localeCompare(a.completed_at);
      return b.id - a.id;
    }),
    [historyJobs, historySort],
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

  const filteredHistoryJobs = useMemo(
    () => sortedHistoryJobs.filter((job) => jobMatchesSearch(job, historySearch)),
    [sortedHistoryJobs, historySearch, libraryById],
  );

  const totalJobPages = useMemo(
    () => Math.max(1, Math.ceil(filteredActiveJobs.length / JOBS_PAGE_SIZE)),
    [filteredActiveJobs.length],
  );

  const totalHistoryPages = useMemo(
    () => Math.max(1, Math.ceil(filteredHistoryJobs.length / HISTORY_PAGE_SIZE)),
    [filteredHistoryJobs.length],
  );

  const pagedJobs = useMemo(() => {
    const start = (jobsPage - 1) * JOBS_PAGE_SIZE;
    return filteredActiveJobs.slice(start, start + JOBS_PAGE_SIZE);
  }, [filteredActiveJobs, jobsPage]);

  const pagedHistoryJobs = useMemo(() => {
    const start = (historyPage - 1) * HISTORY_PAGE_SIZE;
    return filteredHistoryJobs.slice(start, start + HISTORY_PAGE_SIZE);
  }, [filteredHistoryJobs, historyPage]);

  useEffect(() => {
    if (jobsPage > totalJobPages) setJobsPage(totalJobPages);
  }, [jobsPage, totalJobPages]);

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
        jobsView,
      }),
    );
  }, [queueSearch, historySearch, queueSort, historySort, jobsView]);

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
      } else if (
        profile
        && profile.schedule_enabled !== false
        && !isWithinWindow(nowHour, profile.schedule_start_hour, profile.schedule_end_hour)
      ) {
        state = 'Paused by schedule';
      }
      return { library, state, queue: libraryQueueCount(library, jobs) };
    });
  }, [jobs, libraries, libraryProfiles, nowHour]);

  async function refreshAll() {
    try {
      const [nextMetrics, nextJobs, nextSettings, nextNotificationSettings, nextPlexSettings, nextEncoders, nextQueueStatus] = await Promise.all([
        fetchMetrics(),
        fetchJobs(),
        fetchSettings(),
        fetchNotificationSettings(),
        fetchPlexSettings(),
        fetchEncoders(),
        fetchQueueStatus(),
      ]);
      setMetrics(nextMetrics);
      setJobs(nextJobs.filter((job) => !isAbortedJob(job)));
      setSettings(nextSettings);
      setNotificationSettings(nextNotificationSettings);
      setPlexSettings(nextPlexSettings);
      const encoderMap = Object.fromEntries((nextEncoders?.encoders ?? []).map((item) => [item.codec, item.available_encoders]));
      setAvailableEncodersByCodec(encoderMap);
      setQueuePaused(nextQueueStatus?.status === 'paused');
      if (nextPlexSettings?.enabled && nextPlexSettings?.token) {
        fetchPlexLibraries().then((sections) => setPlexLibraries(sections ?? [])).catch(() => {});
      }
      setError('');
    } catch (refreshError) {
      setError(refreshError.message || 'Could not refresh data.');
    }
  }



  async function handleQueueSortChange(nextSort) {
    setQueueSort(nextSort);
    try {
      const updated = await updateSettings({ queue_sort: nextSort });
      setSettings(updated);
    } catch (err) {
      setError(err.message || 'Failed to update queue sort order.');
    }
  }

  async function refreshLibrariesAndProfiles() {
    try {
      const nextLibraries = await fetchLibraries();
      setLibraries(nextLibraries);
      if (!selectedLibraryId && nextLibraries.length > 0) {
        setSelectedLibraryId(nextLibraries[0].id);
      }
      const profileEntries = await Promise.all(
        nextLibraries.map(async (library) => {
          const profile = await fetchLibraryProfile(library.id);
          return [library.id, profile];
        }),
      );
      setLibraryProfiles(Object.fromEntries(profileEntries));
      setError('');
    } catch (refreshError) {
      setError(refreshError.message || 'Could not refresh libraries.');
    }
  }

  function navigate(page) {
    window.location.hash = page;
    setActivePage(page);
  }

  function pushToast(messageText, tone = 'info') {
    const id = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setToasts((prev) => [...prev, { id, message: messageText, tone }]);
    const timer = window.setTimeout(() => {
      setToasts((prev) => prev.filter((toast) => toast.id !== id));
      delete toastTimersRef.current[id];
    }, 5000);
    toastTimersRef.current[id] = timer;
  }

  function mergeJobUpdate(nextJob) {
    setJobs((prevJobs) => {
      if (isAbortedJob(nextJob)) return prevJobs.filter((job) => job.id !== nextJob.id);
      const existingIndex = prevJobs.findIndex((job) => job.id === nextJob.id);
      if (existingIndex === -1) return [nextJob, ...prevJobs];
      const updatedJobs = [...prevJobs];
      updatedJobs[existingIndex] = { ...updatedJobs[existingIndex], ...nextJob };
      return updatedJobs;
    });
  }

  function wsUrlWithToken(token) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const base = import.meta.env.VITE_API_BASE ?? '';
    if (base) {
      const normalizedBase = base.startsWith('http') ? base : `${window.location.origin}${base}`;
      const url = new URL(`${normalizedBase}${WS_PATH}`);
      if (token) url.searchParams.set('token', token);
      return url.toString().replace(/^http/, 'ws');
    }
    const url = new URL(`${protocol}//${window.location.host}${WS_PATH}`);
    if (token) url.searchParams.set('token', token);
    return url.toString();
  }


  useEffect(() => {
    if (!settings?.queue_sort) return;
    setQueueSort((prev) => (prev === settings.queue_sort ? prev : settings.queue_sort));
  }, [settings?.queue_sort]);

  useEffect(() => {
    refreshAll();
    refreshLibrariesAndProfiles();
  }, []);

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
    if (!message) return undefined;
    const timer = window.setTimeout(() => setMessage(''), MESSAGE_DISMISS_MS);
    return () => window.clearTimeout(timer);
  }, [message]);

  useEffect(() => {
    if (!selectedLibraryId || !selectedLibraryProfile) {
      setProfileDraft(null);
      setProfileErrors({});
      return;
    }
    setProfileDraft({
      schedule_enabled: true,
      preferred_video_encoder: 'auto',
      hdr_only: true,
      tone_map_hdr: false,
      minimum_source_resolution: 2160,
      ...selectedLibraryProfile,
    });
    setProfileErrors({});
  }, [selectedLibraryId, selectedLibraryProfile]);

  useEffect(() => {
    if (!fallbackPollingEnabled) return undefined;
    const timer = setInterval(refreshAll, FALLBACK_POLL_MS);
    return () => clearInterval(timer);
  }, [fallbackPollingEnabled]);

  useEffect(() => {
    const timer = setInterval(async () => {
      try {
        const nextMetrics = await fetchMetrics();
        if (nextMetrics) setMetrics(nextMetrics);
      } catch {
        // WebSocket is still the primary path; polling is best-effort resilience.
      }
    }, METRICS_POLL_MS);

    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
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
        const tokenResponse = await fetchWsToken();
        const websocket = new WebSocket(wsUrlWithToken(tokenResponse?.token));
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
          if (payload.type === 'metrics_update') { setMetrics(payload.data); return; }
          if (payload.type === 'library_update') { refreshLibrariesAndProfiles(); return; }
          if (payload.type === 'notification') {
            if (payload.data?.message === 'queue_paused_low_disk') pushToast('Queue paused due to low disk.', 'warn');
            return;
          }
          if (payload.type === 'system_event') {
            if (payload.data?.event === 'job_aborted') { pushToast('Aborted job.', 'error'); return; }
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
  }, []);

  async function handleJobAction(action, jobId) {
    try {
      if (action === 'cancel') await cancelJob(jobId);
      else if (action === 'requeue') await requeueJob(jobId);
      else if (action === 'retry') await retryJob(jobId);
      else if (action === 'pause') await pauseJob(jobId);
      else if (action === 'resume') await resumeJob(jobId);
      else if (action === 'start') await startJob(jobId);
      else if (action === 'start_paused') { await resumeJob(jobId); await startJob(jobId); }
      else if (action === 'abort') {
        // For active or paused-with-progress jobs, ask the user whether they want
        // to remove the job entirely or just stop it and re-queue it.
        const job = jobs.find((j) => j.id === jobId);
        const jobStatus = job?.status?.toLowerCase();
        const needsDialog = job && (
          ACTIVE_STATUSES.has(jobStatus)
          || (PAUSED_STATUSES.has(jobStatus) && (job.progress_percent > 0 || job.resume_position_seconds > 0))
        );
        if (needsDialog) {
          setAbortDialogJobId(jobId);
          return; // wait for dialog selection
        }
        await abortJob(jobId);
      }
      else if (action === 'abort_remove') { await abortJob(jobId); }
      else if (action === 'abort_requeue') { await discardJobProgress(jobId); }
      else if (action === 'remove') await deleteJob(jobId);
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Job action failed.');
    }
  }

  async function handleAbortAllJobs() {
    try {
      const result = await abortAllJobs();
      setMessage(`Aborted ${result.aborted_job_ids.length} job(s).`);
      setError('');
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Abort all failed.');
    }
  }

  async function handleRemoveAllJobs() {
    try {
      const result = await removeAllJobs();
      setMessage(`Removed ${result.removed_job_ids.length} job(s).`);
      setError('');
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Remove all failed.');
    }
  }

  async function handlePurgeHistory() {
    try {
      const result = await purgeHistory();
      setMessage(`Purged ${result.removed_job_ids.length} history item(s).`);
      setError('');
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Purge history failed.');
    }
  }

  async function handleQueueAction(action) {
    try {
      if (action === 'pause') { await pauseQueue(); setQueuePaused(true); }
      else { await resumeQueue(); setQueuePaused(false); }
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Queue action failed.');
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
      setLibraryDraft({ name: '', path: '/media/', enabled: true });
      setLibraryFormErrors({});
      setMessage(`Added library ${created.name}.`);
      setError('');
    } catch (createError) {
      setError(createError.message || 'Could not create library.');
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
      setMessage('Library deleted.');
      setError('');
      await refreshLibrariesAndProfiles();
    } catch (deleteError) {
      setError(deleteError.message || 'Could not delete library.');
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
      setMessage('Library details saved.');
      setError('');
      await refreshLibrariesAndProfiles();
    } catch (saveError) {
      setError(saveError.message || 'Failed to save library details.');
    } finally {
      setSavingLibrary(false);
    }
  }

  async function handleLibraryToggle(libraryId, enabled) {
    const previous = libraries;
    setLibraries((prev) => prev.map((library) => (library.id === libraryId ? { ...library, enabled } : library)));
    try {
      await updateLibrary(libraryId, { enabled });
      setError('');
    } catch (updateError) {
      setLibraries(previous);
      setError(updateError.message || 'Failed to update library state.');
    }
  }

  async function handleLibraryScan(libraryId) {
    setScanningLibraries((prev) => ({ ...prev, [libraryId]: true }));
    try {
      await scanLibrary(libraryId);
      await refreshAll();
      setError('');
    } catch (scanError) {
      setError(scanError.message || 'Failed to start library scan.');
    } finally {
      setScanningLibraries((prev) => ({ ...prev, [libraryId]: false }));
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
      setError('');
    } catch (saveError) {
      setError(saveError.message || 'Failed to save library profile.');
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
      setError('');
    } catch (saveError) {
      setError(saveError.message || 'Failed to save settings.');
    } finally {
      setSavingSettings(false);
    }
  }

  async function saveNotificationSettings() {
    if (!notificationSettings) return;
    setSavingSettings(true);
    try {
      const updated = await updateNotificationSettings(notificationSettings);
      setNotificationSettings(updated);
      setMessage('Notification settings saved.');
      setError('');
    } catch (saveError) {
      setError(saveError.message || 'Could not save notification settings.');
    } finally {
      setSavingSettings(false);
    }
  }

  async function sendNotificationTest() {
    try {
      await sendTestNotification();
      setMessage('Queued a test notification email.');
      setError('');
    } catch (saveError) {
      setError(saveError.message || 'Could not queue test email.');
    }
  }

  async function savePlexSettings() {
    if (!plexSettings) return;
    setSavingPlexSettings(true);
    try {
      const updated = await updatePlexSettings(plexSettings);
      setPlexSettings(updated);
      setMessage('Plex settings saved.');
      setError('');
    } catch (saveError) {
      setError(saveError.message || 'Could not save Plex settings.');
    } finally {
      setSavingPlexSettings(false);
    }
  }

  async function loadPlexLibraries() {
    setLoadingPlexLibraries(true);
    try {
      const sections = await fetchPlexLibraries();
      setPlexLibraries(sections ?? []);
      setError('');
    } catch (fetchError) {
      setError(fetchError.message || 'Could not fetch Plex library sections.');
    } finally {
      setLoadingPlexLibraries(false);
    }
  }

  async function handleTestPlexConnection() {
    setTestingPlexConnection(true);
    try {
      const result = await testPlexConnection();
      if (result?.success) {
        setMessage('Plex connection successful.');
        setError('');
        await loadPlexLibraries();
      } else {
        setError(result?.error || 'Plex connection failed.');
      }
    } catch (testError) {
      setError(testError.message || 'Plex connection test failed.');
    } finally {
      setTestingPlexConnection(false);
    }
  }

  async function handleRecoveryRun() {
    try {
      const result = await runRecovery();
      setMessage(`Recovered ${result.recovered_jobs} jobs`);
      setError('');
      await refreshAll();
    } catch (recoveryError) {
      setError(recoveryError.message || 'Recovery failed.');
    }
  }

  async function handleCleanupRun() {
    try {
      const result = await runCleanup();
      setMessage(`Cleanup removed ${result.cleaned_workspaces} workspace(s)`);
      setError('');
      await refreshAll();
    } catch (cleanupError) {
      setError(cleanupError.message || 'Cleanup failed.');
    }
  }

  async function handleOptimizedCleanupRun() {
    try {
      const result = await runOptimizedCleanup();
      setMessage(`Deleted ${result.deleted_files} optimized file(s) from ${result.affected_job_ids.length} job(s)`);
      setError('');
      await refreshAll();
    } catch (cleanupError) {
      setError(cleanupError.message || 'Optimized cleanup failed.');
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  return (
    <main className="min-h-screen bg-gradient-to-b from-slate-950 via-slate-950 to-slate-900 p-4 md:p-6 text-slate-100">
      <div className="mx-auto max-w-7xl space-y-5">

        {/* Header */}
        <header className="flex flex-col gap-3 rounded-2xl border border-slate-800/80 bg-slate-900/70 px-5 py-3 shadow-xl shadow-slate-950/50 backdrop-blur-sm md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <img src="/api/branding/logo" alt="Optimizarr" className="h-12 w-auto drop-shadow-md" />
            <h1 className="sr-only text-2xl font-bold text-cyan-200">Optimizarr</h1>
          </div>
          <StatusDot status={connectionStatus} />
          <nav className="flex gap-1 rounded-xl border border-slate-800 bg-slate-950/60 p-1">
            {Object.entries(PAGE_KEYS).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={`rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${
                  activePage === key
                    ? 'bg-cyan-500 text-slate-950 shadow-sm shadow-cyan-500/30'
                    : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'
                }`}
                onClick={() => navigate(key)}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {/* Inline alerts */}
        {error && (
          <div className="animate-fade-in rounded-xl border border-red-800/60 bg-red-950/60 px-4 py-3 text-sm text-red-300">
            {error}
          </div>
        )}
        {message && (
          <div className="animate-fade-in rounded-xl border border-emerald-800/60 bg-emerald-950/60 px-4 py-3 text-sm text-emerald-300">
            {message}
          </div>
        )}

        {/* Abort dialog for paused jobs with partial progress */}
        {abortDialogJobId !== null && (() => {
          const dialogJob = jobs.find((j) => j.id === abortDialogJobId);
          const { title: dialogTitle } = dialogJob ? extractTitleYear(dialogJob.source_path) : { title: 'this job' };
          return (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
              <div className="w-full max-w-md rounded-2xl border border-slate-700 bg-slate-900 p-6 shadow-2xl">
                <h2 className="mb-2 text-lg font-semibold text-slate-100">Stop job?</h2>
                <p className="mb-5 text-sm text-slate-400">
                  Choose what to do with <span className="font-medium text-slate-200">{dialogTitle}</span>:
                </p>
                <div className="flex flex-col gap-3">
                  <button
                    className="w-full rounded-xl border border-slate-600 bg-slate-800 px-4 py-3 text-left text-sm text-slate-200 transition hover:bg-slate-700"
                    onClick={async () => {
                      setAbortDialogJobId(null);
                      await handleJobAction('abort_requeue', abortDialogJobId);
                    }}
                  >
                    <span className="font-semibold text-emerald-400">Clear progress &amp; re-queue</span>
                    <span className="ml-2 text-slate-400">— keep the job in the queue; it will restart from the beginning on the next available worker.</span>
                  </button>
                  <button
                    className="w-full rounded-xl border border-red-800/60 bg-red-950/40 px-4 py-3 text-left text-sm text-slate-200 transition hover:bg-red-950/70"
                    onClick={async () => {
                      setAbortDialogJobId(null);
                      await handleJobAction('abort_remove', abortDialogJobId);
                    }}
                  >
                    <span className="font-semibold text-red-400">Remove from queue</span>
                    <span className="ml-2 text-slate-400">— abort and mark as failed; the next scan will pick it up again.</span>
                  </button>
                  <button
                    className="mt-1 w-full rounded-xl border border-slate-700 px-4 py-2 text-sm text-slate-400 transition hover:text-slate-200"
                    onClick={() => setAbortDialogJobId(null)}
                  >
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          );
        })()}

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
                    <TextInput
                      type="text"
                      value={libraryDraft.path}
                      onChange={(e) => setLibraryDraft((prev) => ({ ...prev, path: e.target.value }))}
                      placeholder="/media/movies"
                    />
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
                          <p className="mt-0.5 text-xs text-slate-400">Queue: {libraryQueueCount(library, jobs)}</p>
                        </button>
                        <div className="flex shrink-0 items-center gap-2">
                          <Toggle
                            checked={library.enabled}
                            onChange={(e) => handleLibraryToggle(library.id, e.target.checked)}
                          />
                          <Btn size="sm" variant="secondary" disabled={Boolean(scanningLibraries[library.id])} onClick={() => handleLibraryScan(library.id)}>
                            {scanningLibraries[library.id] ? 'Scanning…' : 'Scan'}
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
                  {/* Library details */}
                  <div>
                    <SectionTitle>Library Details</SectionTitle>
                    <div className="space-y-3">
                      <FormField label="Name">
                        <TextInput
                          type="text"
                          value={selectedLibrary.name}
                          onChange={(e) => setLibraries((prev) => prev.map((lib) => lib.id === selectedLibrary.id ? { ...lib, name: e.target.value } : lib))}
                        />
                      </FormField>
                      <FormField label="Path">
                        <TextInput
                          type="text"
                          value={selectedLibrary.path}
                          onChange={(e) => setLibraries((prev) => prev.map((lib) => lib.id === selectedLibrary.id ? { ...lib, path: e.target.value } : lib))}
                        />
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
                  </div>

                  <hr className="border-slate-800" />

                  {/* Quality preset */}
                  <div>
                    <SectionTitle>Encoding Profile</SectionTitle>
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
                  </div>

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
                        value={[2160, 1440, 1080, 720].includes(profileDraft.target_resolution) ? String(profileDraft.target_resolution) : 'custom'}
                        onChange={(e) => {
                          const v = e.target.value;
                          setProfileDraft((prev) => ({ ...prev, target_resolution: v === 'custom' ? prev.target_resolution : Number(v) }));
                        }}
                      >
                        <option value="2160">2160p (4K)</option>
                        <option value="1440">1440p</option>
                        <option value="1080">1080p</option>
                        <option value="720">720p</option>
                        <option value="custom">Custom</option>
                      </SelectInput>
                      {![2160, 1440, 1080, 720].includes(profileDraft.target_resolution) && (
                        <TextInput type="number" min={1} value={profileDraft.target_resolution} onChange={(e) => setProfileDraft((prev) => ({ ...prev, target_resolution: Number(e.target.value) }))} className="mt-2" />
                      )}
                    </FormField>

                    {/* Minimum source resolution */}
                    <FormField label="Minimum Source Resolution" hint="Only queue sources at or above this height. Ignored when HDR Only is enabled." error={profileErrors.minimum_source_resolution} span2>
                      <SelectInput
                        value={[2160, 1440, 1080].includes(profileDraft.minimum_source_resolution) ? String(profileDraft.minimum_source_resolution) : 'custom'}
                        onChange={(e) => {
                          const v = e.target.value;
                          setProfileDraft((prev) => ({ ...prev, minimum_source_resolution: v === 'custom' ? prev.minimum_source_resolution : Number(v) }));
                        }}
                      >
                        <option value="2160">2160p (4K)</option>
                        <option value="1440">1440p</option>
                        <option value="1080">1080p</option>
                        <option value="custom">Custom</option>
                      </SelectInput>
                      {![2160, 1440, 1080].includes(profileDraft.minimum_source_resolution) && (
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
                          return { ...prev, codec: nextCodec, preferred_video_encoder: preferred };
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
                          <input type="radio" name="bitrate-mode" checked={profileDraft.bitrate_mode === 'vbr_crf'} onChange={() => setProfileDraft((prev) => ({ ...prev, bitrate_mode: 'vbr_crf' }))} />
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
                        <TextInput type="number" min={1} value={profileDraft.crf ?? ''} onChange={(e) => setProfileDraft((prev) => ({ ...prev, crf: Number(e.target.value) }))} className="mt-2" />
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
                      <p className="mb-3 text-xs text-slate-500">Restrict this library to run only within a set time window. Disable to run all day.</p>
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

                  {plexSettings?.enabled && (
                    <div>
                      <hr className="mb-5 border-slate-800" />
                      <SectionTitle>Plex Integration</SectionTitle>
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
                    </div>
                  )}

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
            <div className="overflow-hidden rounded-xl border border-slate-800 bg-slate-900/80 shadow-lg shadow-slate-950/40">
              <div className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-800 px-5 py-3">
                {/* Tab switcher */}
                <div className="flex gap-1 rounded-xl border border-slate-800 bg-slate-950/60 p-1">
                  <button
                    type="button"
                    onClick={() => setJobsView('queue')}
                    className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${jobsView === 'queue' ? 'bg-cyan-500 text-slate-950 shadow-sm shadow-cyan-500/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'}`}
                  >
                    Queue
                    <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${jobsView === 'queue' ? 'bg-slate-950/30 text-slate-900' : 'bg-slate-700 text-slate-300'}`}>{filteredActiveJobs.length}</span>
                  </button>
                  <button
                    type="button"
                    onClick={() => setJobsView('history')}
                    className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${jobsView === 'history' ? 'bg-cyan-500 text-slate-950 shadow-sm shadow-cyan-500/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'}`}
                  >
                    History
                    <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${jobsView === 'history' ? 'bg-slate-950/30 text-slate-900' : 'bg-slate-700 text-slate-300'}`}>{filteredHistoryJobs.length}</span>
                  </button>
                </div>
                {/* Action buttons for the active view */}
                {jobsView === 'queue' && (
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      type="text"
                      placeholder="Search queue…"
                      value={queueSearch}
                      onChange={(e) => { setQueueSearch(e.target.value); setJobsPage(1); }}
                      className="rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs text-slate-100 placeholder-slate-500 outline-none focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30 w-44"
                    />
                    <select
                      value={queueSort}
                      onChange={(e) => { void handleQueueSortChange(e.target.value); }}
                      className="rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs text-slate-200 outline-none focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30"
                    >
                      <option value="default">Date Added (Oldest)</option>
                      <option value="newest">Date Added (Newest)</option>
                      <option value="year_newest">Release Year (Newest)</option>
                      <option value="year_oldest">Release Year (Oldest)</option>
                      {queueSort === 'oldest' && <option value="oldest">Date Added (Oldest)</option>}
                    </select>
                    <Btn size="sm" variant="danger" onClick={handleAbortAllJobs}>Abort All</Btn>
                    <Btn size="sm" variant="warning" onClick={() => handleQueueAction(queuePaused ? 'resume' : 'pause')}>
                      {queuePaused ? 'Start Queue' : 'Pause Queue'}
                    </Btn>
                  </div>
                )}
                {jobsView === 'history' && (
                  <div className="flex flex-wrap items-center gap-2">
                    <input
                      type="text"
                      placeholder="Search history…"
                      value={historySearch}
                      onChange={(e) => { setHistorySearch(e.target.value); setHistoryPage(1); }}
                      className="rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs text-slate-100 placeholder-slate-500 outline-none focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30 w-44"
                    />
                    <select
                      value={historySort}
                      onChange={(e) => setHistorySort(e.target.value)}
                      className="rounded-lg border border-slate-700 bg-slate-800/80 px-3 py-1.5 text-xs text-slate-200 outline-none focus:border-cyan-500/70 focus:ring-1 focus:ring-cyan-500/30"
                    >
                      <option value="completed_desc">Completed (Newest)</option>
                      <option value="year_desc">Year (Newest first)</option>
                      <option value="year_asc">Year (Oldest first)</option>
                    </select>
                    <Btn size="sm" variant="danger" onClick={handlePurgeHistory}>Purge All</Btn>
                  </div>
                )}
              </div>
              {/* Queue tab content */}
              {jobsView === 'queue' && (
                <>
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-slate-800">
                      <thead className="bg-slate-800/50">
                        <tr>
                          {['ID', 'Title', 'Year', 'Library', 'Status', 'Details', 'Encoder', 'Progress', 'Actions'].map((h) => (
                            <th key={h} className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">{h}</th>
                          ))}
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
                        {pagedJobs.map((job) => {
                          const progress = progressFromJob(job);
                          const isRunning = job.status === 'running';
                          const eta = formatEta(job.eta_seconds);
                          const { title, year } = extractTitleYear(job.source_path);
                          const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '—') : '—';
                          return (
                            <tr key={job.id} className="transition-colors duration-100 hover:bg-slate-800/30">
                              <td className="px-4 py-3 text-xs text-slate-500">{job.id}</td>
                              <td className="max-w-[180px] truncate px-4 py-3 text-sm text-slate-200" title={job.source_path}>{title}</td>
                              <td className="px-4 py-3 text-sm text-slate-400">{year ?? '—'}</td>
                              <td className="px-4 py-3 text-sm text-slate-400">{libName}</td>
                              <td className="px-4 py-3 text-sm capitalize">
                                <span className={job.status === 'running' ? 'text-cyan-300' : job.status === 'failed' ? 'text-red-400' : 'text-slate-300'}>{job.status}</span>
                                {job.status === 'failed' && job.error_message && (
                                  <p className="mt-0.5 text-xs text-red-400">{job.error_message}</p>
                                )}
                              </td>
                              <td className="px-4 py-3 text-xs text-slate-400">
                                <span>{formatResolution(job.source_resolution)}</span>
                                <span className="mx-1.5 text-slate-600">·</span>
                                <span>{formatHdrIndicator(job.source_is_hdr)}</span>
                              </td>
                              <td className="px-4 py-3 text-xs text-slate-400">
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
                                <div className="h-1.5 w-32 rounded-full bg-slate-700">
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
                                  {job.status === 'running' && <Btn size="sm" variant="warning" onClick={() => handleJobAction('pause', job.id)}>Pause</Btn>}
                                  {job.status === 'paused' && progress > 0 && <Btn size="sm" variant="success" onClick={() => handleJobAction('resume', job.id)}>Resume</Btn>}
                                  {(job.status === 'queued' || (job.status === 'paused' && progress === 0)) && <Btn size="sm" variant="success" onClick={() => handleJobAction(job.status === 'paused' ? 'start_paused' : 'start', job.id)}>Start</Btn>}
                                  {job.status === 'interrupted' && <Btn size="sm" variant="primary" onClick={() => handleJobAction('requeue', job.id)}>Requeue</Btn>}
                                  {['queued', 'starting', 'running', 'paused', 'preflight'].includes(job.status) && (
                                    <Btn size="sm" variant="danger" onClick={() => handleJobAction('abort', job.id)}>Abort</Btn>
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
                    <div className="flex items-center justify-between border-t border-slate-800 px-5 py-3 text-sm text-slate-400">
                      <p>Page {jobsPage} of {totalJobPages}</p>
                      <div className="flex items-center gap-1">
                        {Array.from({ length: totalJobPages }, (_, i) => i + 1).map((pageNum) => (
                          <button
                            key={pageNum}
                            type="button"
                            onClick={() => setJobsPage(pageNum)}
                            className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all duration-150 ${jobsPage === pageNum ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
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
                  <div className="overflow-x-auto">
                    <table className="min-w-full divide-y divide-slate-800">
                      <thead className="bg-slate-800/50">
                        <tr>
                          {['ID', 'Title', 'Year', 'Library', 'Status', 'Details', 'Encoder', 'Completed', 'Actions'].map((h) => (
                            <th key={h} className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody className="divide-y divide-slate-800/60">
                        {pagedHistoryJobs.length === 0 && (
                          <tr>
                            <td colSpan={9} className="px-4 py-10 text-center text-sm text-slate-500">
                              {historySearch ? 'No matching history.' : 'No completed jobs yet.'}
                            </td>
                          </tr>
                        )}
                        {pagedHistoryJobs.map((job) => {
                          const { title, year } = extractTitleYear(job.source_path);
                          const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '—') : '—';
                          const completedDate = job.completed_at
                            ? new Date(job.completed_at).toLocaleString(undefined, { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', second: '2-digit', hour12: true, timeZoneName: 'short' })
                            : '—';
                          const statusColor = job.status === 'complete'
                            ? 'text-emerald-400'
                            : job.status === 'failed'
                              ? 'text-red-400'
                              : 'text-slate-400';
                          return (
                            <tr key={job.id} className="transition-colors duration-100 hover:bg-slate-800/30">
                              <td className="px-4 py-3 text-xs text-slate-500">{job.id}</td>
                              <td className="max-w-[180px] truncate px-4 py-3 text-sm text-slate-200" title={job.source_path}>{title}</td>
                              <td className="px-4 py-3 text-sm text-slate-400">{year ?? '—'}</td>
                              <td className="px-4 py-3 text-sm text-slate-400">{libName}</td>
                              <td className="px-4 py-3 text-sm capitalize">
                                <span className={statusColor}>{job.status}</span>
                                {job.status === 'failed' && job.error_message && (
                                  <p className="mt-0.5 text-xs text-red-400">{job.error_message}</p>
                                )}
                              </td>
                              <td className="px-4 py-3 text-xs text-slate-400">
                                <span>{formatResolution(job.source_resolution)}</span>
                                <span className="mx-1.5 text-slate-600">·</span>
                                <span>{formatHdrIndicator(job.source_is_hdr)}</span>
                              </td>
                              <td className="px-4 py-3 text-xs text-slate-400">
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
                              <td className="px-4 py-3 text-xs text-slate-400">{completedDate}</td>
                              <td className="px-4 py-3">
                                <div className="flex flex-wrap gap-1.5">
                                  {['failed', 'cancelled'].includes(job.status) && (
                                    <Btn size="sm" variant="primary" onClick={() => handleJobAction('retry', job.id)}>Retry</Btn>
                                  )}
                                  <Btn size="sm" variant="secondary" onClick={() => handleJobAction('remove', job.id)}>Remove</Btn>
                                </div>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                  {totalHistoryPages > 1 && (
                    <div className="flex items-center justify-between border-t border-slate-800 px-5 py-3 text-sm text-slate-400">
                      <p>Page {historyPage} of {totalHistoryPages}</p>
                      <div className="flex items-center gap-1">
                        {Array.from({ length: totalHistoryPages }, (_, i) => i + 1).map((pageNum) => (
                          <button
                            key={pageNum}
                            type="button"
                            onClick={() => setHistoryPage(pageNum)}
                            className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all duration-150 ${historyPage === pageNum ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
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
        {activePage === 'settings' && settings && notificationSettings && plexSettings && (
          <section className="animate-fade-in space-y-5">
            <SectionCard>
              <SectionTitle>General Settings</SectionTitle>
              <div className="grid gap-4 md:grid-cols-2">
                <FormField label="History Retention (Days)" hint="How long to keep completed job history.">
                  <TextInput type="number" min={1} value={settings.history_retention_days} onChange={(e) => setSettings((prev) => ({ ...prev, history_retention_days: Number(e.target.value) }))} />
                </FormField>

                <FormField label="Discovery Interval (Minutes)" hint="How often to scan libraries when using interval discovery.">
                  <TextInput type="number" min={1} value={settings.discovery_interval_minutes} onChange={(e) => setSettings((prev) => ({ ...prev, discovery_interval_minutes: Number(e.target.value) }))} />
                </FormField>

                <FormField label="Discovery Method" hint="When to scan libraries for new media.">
                  <SelectInput value={settings.discovery_method} onChange={(e) => setSettings((prev) => ({ ...prev, discovery_method: e.target.value }))}>
                    <option value="interval">On Interval</option>
                    <option value="startup">On Startup Only</option>
                  </SelectInput>
                </FormField>

                <FormField label="Workspace Root" hint="Temporary directory used during encoding.">
                  <TextInput type="text" value={settings.workspace_root} onChange={(e) => setSettings((prev) => ({ ...prev, workspace_root: e.target.value }))} />
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
    </main>
  );
}
