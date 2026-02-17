import { useEffect, useMemo, useRef, useState } from 'react';
import {
  abortAllJobs,
  abortJob,
  cancelJob,
  createLibrary,
  deleteJob,
  deleteLibrary,
  fetchLibraries,
  fetchEncoders,
  fetchLibraryProfile,
  fetchJobs,
  fetchMetrics,
  fetchNotificationSettings,
  fetchSettings,
  fetchWsToken,
  pauseJob,
  pauseQueue,
  removeAllJobs,
  resumeJob,
  resumeQueue,
  retryJob,
  startJob,
  runCleanup,
  runOptimizedCleanup,
  runRecovery,
  scanLibrary,
  sendTestNotification,
  updateLibrary,
  updateLibraryProfile,
  updateNotificationSettings,
  updateSettings,
} from './api';
import StatCard from './components/StatCard';

const WS_PATH = '/ws';
const FALLBACK_AFTER_MS = 30000;
const FALLBACK_POLL_MS = 10000;
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;
const MESSAGE_DISMISS_MS = 5000;
const JOBS_PAGE_SIZE = 50;

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
    label: 'Fast encode',
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

function libraryQueueCount(library, jobs) {
  return jobs.filter(
    (job) => ['pending', 'queued', 'created'].includes(job.status?.toLowerCase())
      && (job.source_path === library.path || job.source_path.startsWith(`${library.path}/`)),
  ).length;
}

const IN_PROGRESS_STATUSES = new Set(['starting', 'running', 'preflight', 'paused', 'paused_schedule']);
const QUEUED_STATUSES = new Set(['pending', 'queued', 'created']);

function jobSortRank(job) {
  const status = job.status?.toLowerCase();
  if (IN_PROGRESS_STATUSES.has(status)) return 0;
  if (QUEUED_STATUSES.has(status)) return 1;
  return 2;
}

function sortedJobsForDisplay(jobs) {
  return [...jobs].sort((a, b) => {
    const rankDiff = jobSortRank(a) - jobSortRank(b);
    if (rankDiff !== 0) return rankDiff;

    if (jobSortRank(a) === 1) {
      return a.id - b.id;
    }

    return b.id - a.id;
  });
}

function formatResolution(height) {
  return Number.isInteger(height) ? `${height}p` : 'Unknown';
}

function formatHdrIndicator(sourceIsHdr) {
  if (sourceIsHdr === true) return 'HDR';
  if (sourceIsHdr === false) return 'SDR';
  return 'Unknown';
}

function validateLibraryDraft(draft, libraryEnabled) {
  const errors = {};

  if (!libraryEnabled) {
    return errors;
  }

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

  if (!draft.name?.trim()) {
    errors.name = 'Library name is required.';
  }

  if (!draft.path?.trim()) {
    errors.path = 'Library path is required.';
  } else if (!draft.path.startsWith('/')) {
    errors.path = 'Library path must be absolute.';
  }

  return errors;
}

export default function App() {
  const [activePage, setActivePage] = useState('dashboard');
  const [metrics, setMetrics] = useState();
  const [jobs, setJobs] = useState([]);
  const [libraries, setLibraries] = useState([]);
  const [libraryProfiles, setLibraryProfiles] = useState({});
  const [settings, setSettings] = useState();
  const [notificationSettings, setNotificationSettings] = useState();
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

  const wsRef = useRef();
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef();
  const fallbackTimerRef = useRef();
  const intentionallyClosedRef = useRef(false);
  const toastTimersRef = useRef({});

  const queueCount = useMemo(
    () => jobs.filter((job) => ['pending', 'queued', 'created'].includes(job.status?.toLowerCase())).length,
    [jobs],
  );


  const sortedJobs = useMemo(() => sortedJobsForDisplay(jobs), [jobs]);

  const totalJobPages = useMemo(
    () => Math.max(1, Math.ceil(sortedJobs.length / JOBS_PAGE_SIZE)),
    [sortedJobs.length],
  );

  const pagedJobs = useMemo(() => {
    const start = (jobsPage - 1) * JOBS_PAGE_SIZE;
    return sortedJobs.slice(start, start + JOBS_PAGE_SIZE);
  }, [sortedJobs, jobsPage]);

  useEffect(() => {
    if (jobsPage > totalJobPages) {
      setJobsPage(totalJobPages);
    }
  }, [jobsPage, totalJobPages]);

  const selectedLibrary = useMemo(
    () => libraries.find((library) => library.id === selectedLibraryId) ?? null,
    [libraries, selectedLibraryId],
  );

  const selectedLibraryProfile = selectedLibraryId ? libraryProfiles[selectedLibraryId] : null;

  const libraryRuntimeStates = useMemo(() => {
    const nowHour = new Date().getHours();
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
  }, [jobs, libraries, libraryProfiles, settings]);

  async function refreshAll() {
    try {
      const [nextMetrics, nextJobs, nextSettings, nextNotificationSettings, nextEncoders] = await Promise.all([
        fetchMetrics(),
        fetchJobs(),
        fetchSettings(),
        fetchNotificationSettings(),
        fetchEncoders(),
      ]);
      setMetrics(nextMetrics);
      setJobs(nextJobs.filter((job) => !isAbortedJob(job)));
      setSettings(nextSettings);
      setNotificationSettings(nextNotificationSettings);
      const encoderMap = Object.fromEntries((nextEncoders?.encoders ?? []).map((item) => [item.codec, item.available_encoders]));
      setAvailableEncodersByCodec(encoderMap);
      setError('');
    } catch (refreshError) {
      setError(refreshError.message || 'Could not refresh data.');
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
      if (isAbortedJob(nextJob)) {
        return prevJobs.filter((job) => job.id !== nextJob.id);
      }

      const existingIndex = prevJobs.findIndex((job) => job.id === nextJob.id);
      if (existingIndex === -1) {
        return [nextJob, ...prevJobs];
      }

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
      if (token) {
        url.searchParams.set('token', token);
      }
      return url.toString().replace(/^http/, 'ws');
    }

    const url = new URL(`${protocol}//${window.location.host}${WS_PATH}`);
    if (token) {
      url.searchParams.set('token', token);
    }
    return url.toString();
  }

  useEffect(() => {
    refreshAll();
    refreshLibrariesAndProfiles();
  }, []);

  useEffect(() => {
    if (!message) {
      return undefined;
    }

    const timer = window.setTimeout(() => {
      setMessage('');
    }, MESSAGE_DISMISS_MS);

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
      minimum_source_resolution: 2160,
      ...selectedLibraryProfile,
    });
    setProfileErrors({});
  }, [selectedLibraryId, selectedLibraryProfile]);

  useEffect(() => {
    if (!fallbackPollingEnabled) {
      return undefined;
    }

    const timer = setInterval(refreshAll, FALLBACK_POLL_MS);
    return () => clearInterval(timer);
  }, [fallbackPollingEnabled]);

  useEffect(() => {
    intentionallyClosedRef.current = false;

    function clearTimers() {
      if (reconnectTimerRef.current) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = undefined;
      }
      if (fallbackTimerRef.current) {
        clearTimeout(fallbackTimerRef.current);
        fallbackTimerRef.current = undefined;
      }
    }

    function scheduleFallbackPolling() {
      if (fallbackTimerRef.current) {
        return;
      }

      fallbackTimerRef.current = setTimeout(() => {
        setFallbackPollingEnabled(true);
        setConnectionStatus('offline');
      }, FALLBACK_AFTER_MS);
    }

    function scheduleReconnect(connectFn) {
      if (intentionallyClosedRef.current) {
        return;
      }

      reconnectAttemptsRef.current += 1;
      const delay = Math.min(
        RECONNECT_BASE_DELAY_MS * (2 ** (reconnectAttemptsRef.current - 1)),
        RECONNECT_MAX_DELAY_MS,
      );
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
          if (fallbackTimerRef.current) {
            clearTimeout(fallbackTimerRef.current);
            fallbackTimerRef.current = undefined;
          }
        };

        websocket.onmessage = (event) => {
          const payload = JSON.parse(event.data);
          if (payload.type === 'job_update') {
            mergeJobUpdate(payload.data);
            return;
          }

          if (payload.type === 'metrics_update') {
            setMetrics(payload.data);
            return;
          }

          if (payload.type === 'library_update') {
            refreshLibrariesAndProfiles();
            return;
          }

          if (payload.type === 'notification') {
            if (payload.data?.message === 'queue_paused_low_disk') {
              pushToast('Queue paused due to low disk.', 'warn');
            }
            return;
          }

          if (payload.type === 'system_event') {
            if (payload.data?.event === 'job_aborted') {
              pushToast('Aborted job.', 'error');
              return;
            }

            if (payload.data?.event === 'queue_paused' && payload.data?.reason === 'low_disk') {
              pushToast('Queue paused due to low disk.', 'warn');
              return;
            }

            if (payload.data?.event === 'queue_paused') {
              pushToast('Queue paused.', 'warn');
              return;
            }

            if (payload.data?.event === 'recovery_summary' && payload.data?.trigger === 'startup') {
              pushToast('Recovery ran on startup.', 'info');
            }
          }
        };

        websocket.onerror = () => {
          websocket.close();
        };

        websocket.onclose = () => {
          if (intentionallyClosedRef.current) {
            return;
          }

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
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = undefined;
      }
    };
  }, []);

  async function handleJobAction(action, jobId) {
    try {
      if (action === 'cancel') {
        await cancelJob(jobId);
      } else if (action === 'retry') {
        await retryJob(jobId);
      } else if (action === 'pause') {
        await pauseJob(jobId);
      } else if (action === 'resume') {
        await resumeJob(jobId);
      } else if (action === 'start') {
        await startJob(jobId);
      } else if (action === 'abort') {
        await abortJob(jobId);
      } else if (action === 'remove') {
        await deleteJob(jobId);
      }
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

  async function handleQueueAction(action) {
    try {
      if (action === 'pause') {
        await pauseQueue();
        setQueuePaused(true);
      } else {
        await resumeQueue();
        setQueuePaused(false);
      }
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Queue action failed.');
    }
  }


  async function handleCreateLibrary() {
    const nextErrors = validateLibraryForm(libraryDraft);
    setLibraryFormErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) {
      return;
    }

    setSavingLibrary(true);
    try {
      const created = await createLibrary({
        name: libraryDraft.name.trim(),
        path: libraryDraft.path.trim(),
        enabled: libraryDraft.enabled,
      });
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
      setSelectedLibraryId((prev) => {
        if (prev !== libraryId) {
          return prev;
        }
        return remaining[0]?.id ?? null;
      });
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
    if (!selectedLibrary) {
      return;
    }

    const nextErrors = validateLibraryForm(selectedLibrary);
    setLibraryFormErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) {
      return;
    }

    setSavingLibrary(true);
    try {
      const updated = await updateLibrary(selectedLibrary.id, {
        name: selectedLibrary.name.trim(),
        path: selectedLibrary.path.trim(),
        enabled: selectedLibrary.enabled,
      });
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
    if (!selectedLibrary || !profileDraft) {
      return;
    }

    const nextErrors = validateLibraryDraft(profileDraft, selectedLibrary.enabled);
    setProfileErrors(nextErrors);
    if (Object.keys(nextErrors).length > 0) {
      return;
    }

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

    try {
      setSavingSettings(true);
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

  return (
    <main className="min-h-screen bg-gradient-to-b from-slate-950 via-slate-950 to-slate-900 p-6 text-slate-100">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="rounded-xl border border-slate-800 bg-slate-900/70 p-4 shadow-lg shadow-slate-950/40 backdrop-blur flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <div className="flex items-center gap-3">
            <img src="/api/branding/logo" alt="Optimizarr" className="h-14 w-auto" />
            <h1 className="text-3xl font-bold text-cyan-200 sr-only">Optimizarr</h1>
          </div>
          <p className="rounded border border-slate-700 px-3 py-1 text-xs text-slate-300">
            WS: {connectionStatus}
            {fallbackPollingEnabled ? ' (polling fallback active)' : ''}
          </p>
          <nav className="flex gap-2 rounded-lg border border-slate-800 bg-slate-900 p-1">
            {Object.entries(PAGE_KEYS).map(([key, label]) => (
              <button
                key={key}
                type="button"
                className={`rounded-md px-4 py-2 text-sm ${
                  activePage === key ? 'bg-cyan-500 text-slate-950' : 'text-slate-300 hover:bg-slate-800'
                }`}
                onClick={() => setActivePage(key)}
              >
                {label}
              </button>
            ))}
          </nav>
        </header>

        {error && <p className="rounded bg-red-900/50 p-3 text-red-200">{error}</p>}
        {message && <p className="rounded bg-emerald-900/50 p-3 text-emerald-200">{message}</p>}

        <div className="pointer-events-none fixed right-4 top-4 z-50 space-y-2">
          {toasts.map((toast) => (
            <div
              key={toast.id}
              className={`rounded border px-4 py-2 text-sm shadow-lg ${
                toast.tone === 'error'
                  ? 'border-red-700 bg-red-900/90 text-red-100'
                  : toast.tone === 'warn'
                    ? 'border-amber-600 bg-amber-900/90 text-amber-100'
                    : 'border-cyan-700 bg-slate-900/95 text-cyan-100'
              }`}
            >
              {toast.message}
            </div>
          ))}
        </div>

        {activePage === 'dashboard' && (
          <section className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
              <StatCard label="GPU %" value={`${metrics?.gpu_video_percent ?? 0}%`} />
              <StatCard label="CPU %" value={`${metrics?.cpu_percent ?? 0}%`} />
              <StatCard label="Active jobs" value={metrics?.active_jobs ?? 0} />
              <StatCard label="Queue count" value={queueCount} />
              <StatCard label="Libraries" value={libraries.length} />
            </div>

            <div className="rounded-xl border border-slate-800 bg-slate-900/80 p-4 shadow-lg shadow-slate-950/40">
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-300">Per-library runtime state</h2>
              <div className="space-y-2 text-sm">
                {libraryRuntimeStates.map(({ library, state, queue }) => (
                  <div key={library.id} className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/40 p-3">
                    <div>
                      <p className="font-medium text-cyan-200">{library.name}</p>
                      <p className="text-xs text-slate-400">{library.path}</p>
                    </div>
                    <div className="text-right">
                      <p>{state}</p>
                      <p className="text-xs text-slate-400">Queue: {queue}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </section>
        )}

        {activePage === 'libraries' && (
          <section className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.3fr)]">
            <div className="space-y-4 rounded-xl border border-slate-800 bg-slate-900/80 p-4 shadow-lg shadow-slate-950/40">
              <div className="rounded border border-slate-700 bg-slate-950/50 p-3">
                <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-300">Add library</h2>
                <div className="grid gap-3 md:grid-cols-2">
                  <label className="space-y-1 text-sm md:col-span-2">
                    <span>Name</span>
                    <input
                      type="text"
                      className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                      value={libraryDraft.name}
                      onChange={(event) => setLibraryDraft((prev) => ({ ...prev, name: event.target.value }))}
                    />
                    {libraryFormErrors.name && <p className="text-xs text-red-300">{libraryFormErrors.name}</p>}
                  </label>
                  <label className="space-y-1 text-sm md:col-span-2">
                    <span>Path</span>
                    <input
                      type="text"
                      className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                      value={libraryDraft.path}
                      onChange={(event) => setLibraryDraft((prev) => ({ ...prev, path: event.target.value }))}
                    />
                    {libraryFormErrors.path && <p className="text-xs text-red-300">{libraryFormErrors.path}</p>}
                  </label>
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={libraryDraft.enabled}
                      onChange={(event) => setLibraryDraft((prev) => ({ ...prev, enabled: event.target.checked }))}
                    />
                    Enabled
                  </label>
                </div>
                <button
                  type="button"
                  className="mt-3 rounded bg-cyan-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-70"
                  disabled={savingLibrary}
                  onClick={handleCreateLibrary}
                >
                  {savingLibrary ? 'Adding…' : 'Add library'}
                </button>
              </div>

              <h2 className="text-sm font-semibold uppercase tracking-wide text-slate-300">Libraries</h2>
              <div className="space-y-2">
                {libraries.map((library) => (
                  <div
                    key={library.id}
                    className={`rounded border p-3 ${
                      selectedLibraryId === library.id ? 'border-cyan-500 bg-slate-950' : 'border-slate-800 bg-slate-950/40'
                    }`}
                  >
                    <div className="flex items-start justify-between gap-3">
                      <button type="button" className="text-left" onClick={() => setSelectedLibraryId(library.id)}>
                        <p className="font-medium text-cyan-200">{library.name}</p>
                        <p className="text-xs text-slate-400">{library.path}</p>
                        <p className="text-xs text-slate-300">Queue: {libraryQueueCount(library, jobs)}</p>
                      </button>
                      <div className="flex items-center gap-2">
                        <label className="text-xs text-slate-300">
                          <input
                            type="checkbox"
                            checked={library.enabled}
                            onChange={(event) => handleLibraryToggle(library.id, event.target.checked)}
                            className="mr-1"
                          />
                          enabled
                        </label>
                        <button
                          type="button"
                          className="rounded bg-cyan-600 px-2 py-1 text-xs font-semibold text-slate-950 hover:bg-cyan-500 disabled:opacity-60"
                          disabled={Boolean(scanningLibraries[library.id])}
                          onClick={() => handleLibraryScan(library.id)}
                        >
                          {scanningLibraries[library.id] ? 'Scanning…' : 'Scan'}
                        </button>
                        <button
                          type="button"
                          className="rounded bg-rose-600 px-2 py-1 text-xs font-semibold text-white hover:bg-rose-500 disabled:opacity-60"
                          disabled={deletingLibraryId === library.id}
                          onClick={() => handleDeleteLibrary(library.id)}
                        >
                          {deletingLibraryId === library.id ? 'Deleting…' : 'Delete'}
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-xl border border-slate-800 bg-slate-900/80 p-4 shadow-lg shadow-slate-950/40">
              {!selectedLibrary || !profileDraft ? (
                <p className="text-sm text-slate-300">Select a library to edit its profile.</p>
              ) : (
                <div className="space-y-4">
                  <div className="space-y-3 rounded border border-slate-800 bg-slate-950/40 p-3">
                    <h2 className="text-lg font-semibold text-cyan-200">Library details</h2>
                    <label className="space-y-1 text-sm">
                      <span>Name</span>
                      <input
                        type="text"
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={selectedLibrary.name}
                        onChange={(event) => setLibraries((prev) => prev.map((library) => (
                          library.id === selectedLibrary.id ? { ...library, name: event.target.value } : library
                        )))}
                      />
                    </label>
                    <label className="space-y-1 text-sm">
                      <span>Path</span>
                      <input
                        type="text"
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={selectedLibrary.path}
                        onChange={(event) => setLibraries((prev) => prev.map((library) => (
                          library.id === selectedLibrary.id ? { ...library, path: event.target.value } : library
                        )))}
                      />
                    </label>
                    <p className="text-xs text-slate-300">Status: {libraryRuntimeStates.find((item) => item.library.id === selectedLibrary.id)?.state}</p>
                    <div className="flex flex-wrap gap-2">
                      <button
                        type="button"
                        className="rounded bg-cyan-500 px-3 py-2 text-sm font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-70"
                        disabled={savingLibrary}
                        onClick={handleSaveLibraryDetails}
                      >
                        {savingLibrary ? 'Saving…' : 'Save library details'}
                      </button>
                      <button
                        type="button"
                        className="rounded bg-rose-600 px-3 py-2 text-sm font-semibold text-white hover:bg-rose-500 disabled:opacity-70"
                        disabled={deletingLibraryId === selectedLibrary.id}
                        onClick={() => handleDeleteLibrary(selectedLibrary.id)}
                      >
                        {deletingLibraryId === selectedLibrary.id ? 'Deleting…' : 'Delete library'}
                      </button>
                    </div>
                  </div>

                  <div className="rounded-xl border border-slate-700/70 bg-slate-950/40 p-4">
                    <label className="block text-sm font-semibold text-cyan-100">Quality preset</label>
                    <p className="mb-3 text-xs text-slate-400">Start with a preset, then fine tune below.</p>
                    <select
                      className="w-full rounded-lg border border-slate-700 bg-slate-800 p-2"
                      value={selectedPreset}
                      onChange={(event) => {
                        const presetKey = event.target.value;
                        setSelectedPreset(presetKey);
                        const preset = QUALITY_PRESETS[presetKey];
                        if (preset) {
                          setProfileDraft((prev) => ({ ...prev, ...preset.profile }));
                        }
                      }}
                    >
                      {Object.entries(QUALITY_PRESETS).map(([key, value]) => (
                        <option key={key} value={key}>{value.label}</option>
                      ))}
                    </select>
                  </div>

                  <div className="grid gap-4 md:grid-cols-2">
                    <label className="space-y-2 rounded-lg border border-slate-700/60 bg-slate-950/40 p-3">
                      <span className="text-sm font-medium">Enabled</span>
                      <p className="text-xs text-slate-400">Disable to stop optimization for this library.</p>
                      <input
                        type="checkbox"
                        checked={selectedLibrary.enabled}
                        onChange={(event) => handleLibraryToggle(selectedLibrary.id, event.target.checked)}
                      />
                    </label>

                    <label className="space-y-2 rounded-lg border border-slate-700/60 bg-slate-950/40 p-3">
                      <span className="text-sm font-medium">HDR only</span>
                      <p className="text-xs text-slate-400">Only process HDR media in this library.</p>
                      <input
                        type="checkbox"
                        checked={profileDraft.hdr_only}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, hdr_only: event.target.checked }))}
                      />
                    </label>

                    <label className="space-y-2 md:col-span-2">
                      <span className="text-sm font-medium">Target resolution</span>
                      <p className="text-xs text-slate-400">Output height. 1080p is a good default for mixed libraries.</p>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={[2160, 1440, 1080, 720].includes(profileDraft.target_resolution) ? String(profileDraft.target_resolution) : 'custom'}
                        onChange={(event) => {
                          const value = event.target.value;
                          setProfileDraft((prev) => ({
                            ...prev,
                            target_resolution: value === 'custom' ? prev.target_resolution : Number(value),
                          }));
                        }}
                      >
                        <option value="2160">2160p (4K)</option>
                        <option value="1440">1440p</option>
                        <option value="1080">1080p</option>
                        <option value="720">720p</option>
                        <option value="custom">Custom</option>
                      </select>
                      {!([2160, 1440, 1080, 720].includes(profileDraft.target_resolution)) && (
                        <input
                          type="number"
                          min={1}
                          className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                          value={profileDraft.target_resolution}
                          onChange={(event) => setProfileDraft((prev) => ({ ...prev, target_resolution: Number(event.target.value) }))}
                        />
                      )}
                      {profileErrors.target_resolution && <p className="text-xs text-red-300">{profileErrors.target_resolution}</p>}
                    </label>

                    <label className="space-y-2 md:col-span-2">
                      <span className="text-sm font-medium">Minimum source resolution filter</span>
                      <p className="text-xs text-slate-400">Only queue sources at or above this height during scans. Ignored when HDR only is enabled.</p>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={[2160, 1440, 1080].includes(profileDraft.minimum_source_resolution) ? String(profileDraft.minimum_source_resolution) : 'custom'}
                        onChange={(event) => {
                          const value = event.target.value;
                          setProfileDraft((prev) => ({
                            ...prev,
                            minimum_source_resolution: value === 'custom' ? prev.minimum_source_resolution : Number(value),
                          }));
                        }}
                      >
                        <option value="2160">2160p (4K)</option>
                        <option value="1440">1440p</option>
                        <option value="1080">1080p</option>
                        <option value="custom">Custom</option>
                      </select>
                      {!([2160, 1440, 1080].includes(profileDraft.minimum_source_resolution)) && (
                        <input
                          type="number"
                          min={1}
                          className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                          value={profileDraft.minimum_source_resolution}
                          onChange={(event) => setProfileDraft((prev) => ({ ...prev, minimum_source_resolution: Number(event.target.value) }))}
                        />
                      )}
                      {profileErrors.minimum_source_resolution && <p className="text-xs text-red-300">{profileErrors.minimum_source_resolution}</p>}
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">Codec</span>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.codec}
                        onChange={(event) => setProfileDraft((prev) => { const nextCodec = event.target.value; const available = availableEncodersByCodec[nextCodec] ?? []; const preferred = available.includes(prev.preferred_video_encoder) ? prev.preferred_video_encoder : 'auto'; return { ...prev, codec: nextCodec, preferred_video_encoder: preferred }; })}
                      >
                        <option value="h264">H.264</option>
                        <option value="hevc">HEVC</option>
                        <option value="av1">AV1</option>
                      </select>
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">Preferred encoder</span>
                      <p className="text-xs text-slate-400">Pick a detected encoder for this codec, or Auto for best available.</p>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.preferred_video_encoder ?? 'auto'}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, preferred_video_encoder: event.target.value }))}
                      >
                        <option value="auto">Auto</option>
                        {(availableEncodersByCodec[profileDraft.codec] ?? []).map((encoderName) => (
                          <option key={encoderName} value={encoderName}>{encoderName}</option>
                        ))}
                      </select>
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">AV1 fallback</span>
                      <p className="text-xs text-slate-400">Used when AV1 cannot be used on your hardware.</p>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.av1_fallback_codec}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, av1_fallback_codec: event.target.value }))}
                      >
                        <option value="hevc">HEVC</option>
                        <option value="h264">H.264</option>
                      </select>
                      {profileErrors.av1_fallback_codec && <p className="text-xs text-red-300">{profileErrors.av1_fallback_codec}</p>}
                    </label>

                    <label className="space-y-2 md:col-span-2 rounded-lg border border-slate-700/60 bg-slate-950/40 p-3">
                      <span className="text-sm font-medium">Bitrate mode</span>
                      <p className="text-xs text-slate-400">CRF targets visual quality (smaller is higher quality). CBR targets fixed bitrate.</p>
                      <div className="flex gap-4 text-sm">
                        <label>
                          <input
                            type="radio"
                            name="bitrate-mode"
                            checked={profileDraft.bitrate_mode === 'vbr_crf'}
                            onChange={() => setProfileDraft((prev) => ({ ...prev, bitrate_mode: 'vbr_crf' }))}
                          />
                          {' '}CRF (quality target)
                        </label>
                        <label>
                          <input
                            type="radio"
                            name="bitrate-mode"
                            checked={profileDraft.bitrate_mode === 'cbr'}
                            onChange={() => setProfileDraft((prev) => ({ ...prev, bitrate_mode: 'cbr' }))}
                          />
                          {' '}CBR (size target)
                        </label>
                      </div>
                    </label>

                    {profileDraft.bitrate_mode === 'vbr_crf' && (
                      <label className="space-y-2 md:col-span-2">
                        <span className="text-sm">CRF ({profileDraft.crf ?? 23})</span>
                        <p className="text-xs text-slate-400">Typical range: 18-30. Lower = better quality/larger files, higher = smaller files.</p>
                        <input
                          type="range"
                          min={1}
                          max={40}
                          value={profileDraft.crf ?? 23}
                          onChange={(event) => setProfileDraft((prev) => ({ ...prev, crf: Number(event.target.value) }))}
                        />
                        <input
                          type="number"
                          min={1}
                          className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                          value={profileDraft.crf ?? ''}
                          onChange={(event) => setProfileDraft((prev) => ({ ...prev, crf: Number(event.target.value) }))}
                        />
                        {profileErrors.crf && <p className="text-xs text-red-300">{profileErrors.crf}</p>}
                      </label>
                    )}

                    {profileDraft.bitrate_mode === 'cbr' && (
                      <label className="space-y-2 md:col-span-2">
                        <span className="text-sm">Bitrate ({profileDraft.bitrate_mbps ?? 1} Mbps)</span>
                        <input
                          type="range"
                          min={1}
                          max={40}
                          value={profileDraft.bitrate_mbps ?? 1}
                          onChange={(event) => setProfileDraft((prev) => ({ ...prev, bitrate_mbps: Number(event.target.value) }))}
                        />
                        <input
                          type="number"
                          min={1}
                          className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                          value={profileDraft.bitrate_mbps ?? ''}
                          onChange={(event) => setProfileDraft((prev) => ({ ...prev, bitrate_mbps: Number(event.target.value) }))}
                        />
                        {profileErrors.bitrate_mbps && <p className="text-xs text-red-300">{profileErrors.bitrate_mbps}</p>}
                      </label>
                    )}

                    <label className="space-y-2">
                      <span className="text-sm">Speed preset</span>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.speed_preset}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, speed_preset: event.target.value }))}
                      >
                        <option value="slow">Slow</option>
                        <option value="medium">Medium</option>
                        <option value="fast">Fast</option>
                      </select>
                      <p className="text-xs text-slate-400">Slow = best compression, Fast = quickest encode.</p>
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">Container</span>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.container}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, container: event.target.value }))}
                      >
                        <option value="mkv">mkv</option>
                        <option value="mp4">mp4</option>
                      </select>
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">Audio</span>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.audio_mode}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, audio_mode: event.target.value }))}
                      >
                        <option value="copy">copy</option>
                        <option value="aac">aac</option>
                        <option value="ac3">ac3</option>
                        <option value="eac3">eac3</option>
                      </select>
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">Max workers ({profileDraft.max_workers})</span>
                      <p className="text-xs text-slate-400">Per-library worker cap.</p>
                      <input
                        type="range"
                        min={1}
                        max={10}
                        value={profileDraft.max_workers}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, max_workers: Number(event.target.value) }))}
                      />
                      {profileErrors.max_workers && <p className="text-xs text-red-300">{profileErrors.max_workers}</p>}
                    </label>

                    <label className="space-y-2 md:col-span-2 rounded-lg border border-slate-700/60 bg-slate-950/40 p-3">
                      <span className="text-sm font-medium">Scheduled run window</span>
                      <p className="text-xs text-slate-400">Turn this off to allow this library to run all day.</p>
                      <label className="flex items-center gap-2 text-sm text-slate-200">
                        <input
                          type="checkbox"
                          checked={profileDraft.schedule_enabled !== false}
                          onChange={(event) => setProfileDraft((prev) => ({ ...prev, schedule_enabled: event.target.checked }))}
                        />
                        Enable schedule window
                      </label>
                    </label>

                    {profileDraft.schedule_enabled !== false && (
                      <>
                        <label className="space-y-2">
                          <span className="text-sm">Schedule start</span>
                          <input
                            type="time"
                            step={3600}
                            className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                            value={formatHour(profileDraft.schedule_start_hour)}
                            onChange={(event) => setProfileDraft((prev) => ({ ...prev, schedule_start_hour: parseHour(event.target.value) }))}
                          />
                          {profileErrors.schedule_start_hour && <p className="text-xs text-red-300">{profileErrors.schedule_start_hour}</p>}
                        </label>

                        <label className="space-y-2">
                          <span className="text-sm">Schedule end</span>
                          <input
                            type="time"
                            step={3600}
                            className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                            value={formatHour(profileDraft.schedule_end_hour)}
                            onChange={(event) => setProfileDraft((prev) => ({ ...prev, schedule_end_hour: parseHour(event.target.value) }))}
                          />
                          {profileErrors.schedule_end_hour && <p className="text-xs text-red-300">{profileErrors.schedule_end_hour}</p>}
                        </label>

                        <label className="space-y-2 md:col-span-2">
                          <span className="text-sm">Schedule close behavior</span>
                          <select
                            className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                            value={profileDraft.schedule_policy ?? 'finish_current'}
                            onChange={(event) => setProfileDraft((prev) => ({ ...prev, schedule_policy: event.target.value }))}
                          >
                            <option value="finish_current">Finish current job</option>
                            <option value="pause_current">Pause current job</option>
                          </select>
                        </label>
                      </>
                    )}
                  </div>

                  <button
                    type="button"
                    className="rounded bg-cyan-500 px-4 py-2 font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-70"
                    disabled={savingProfile}
                    onClick={handleSaveLibraryProfile}
                  >
                    {savingProfile ? 'Saving…' : 'Save'}
                  </button>
                </div>
              )}
            </div>
          </section>
        )}

        {activePage === 'jobs' && (
          <section className="overflow-hidden rounded-lg border border-slate-800 bg-slate-900">
            <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
              <p className="text-sm text-slate-300">Queue controls</p>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => handleAbortAllJobs()}
                  className="rounded bg-rose-600 px-3 py-1 text-xs font-medium text-white hover:bg-rose-500"
                >
                  Abort All
                </button>

                <button
                  type="button"
                  onClick={() => handleRemoveAllJobs()}
                  className="rounded border border-slate-600 bg-slate-800 px-3 py-1 text-xs font-medium text-slate-100 hover:bg-slate-700"
                >
                  Remove All
                </button>
                <button
                  type="button"
                  onClick={() => handleQueueAction(queuePaused ? 'resume' : 'pause')}
                  className="rounded bg-amber-500 px-3 py-1 text-xs font-medium text-slate-950 hover:bg-amber-400"
                >
                  {queuePaused ? 'Resume Queue' : 'Pause Queue'}
                </button>
              </div>
            </div>
            <table className="min-w-full divide-y divide-slate-800">
              <thead className="bg-slate-800/70">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">ID</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Source</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Source Info</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Progress</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {pagedJobs.map((job) => {
                  const progress = progressFromJob(job);
                  return (
                    <tr key={job.id}>
                      <td className="px-4 py-3">{job.id}</td>
                      <td className="max-w-xs truncate px-4 py-3 text-slate-300">{job.source_path}</td>
                      <td className="px-4 py-3 capitalize">{job.status}</td>
                      <td className="px-4 py-3 text-xs text-slate-300">
                        <span>{formatResolution(job.source_resolution)}</span>
                        <span className="mx-2 text-slate-500">•</span>
                        <span>{formatHdrIndicator(job.source_is_hdr)}</span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="h-2 w-44 rounded bg-slate-700">
                          <div
                            className="h-full rounded bg-cyan-400 transition-all"
                            style={{ width: `${progress}%` }}
                          />
                        </div>
                        <span className="mt-1 inline-block text-xs text-slate-400">{progress}%</span>
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex gap-2">
                          {job.status === 'running' && (
                            <button
                              type="button"
                              onClick={() => handleJobAction('pause', job.id)}
                              className="rounded bg-amber-500 px-3 py-1 text-xs font-medium text-slate-950 hover:bg-amber-400"
                            >
                              Pause
                            </button>
                          )}
                          {job.status === 'paused' && (
                            <button
                              type="button"
                              onClick={() => handleJobAction('resume', job.id)}
                              className="rounded bg-emerald-500 px-3 py-1 text-xs font-medium text-slate-950 hover:bg-emerald-400"
                            >
                              Resume
                            </button>
                          )}
                          {job.status === 'queued' && (
                            <button
                              type="button"
                              onClick={() => handleJobAction('start', job.id)}
                              className="rounded bg-emerald-500 px-3 py-1 text-xs font-medium text-slate-950 hover:bg-emerald-400"
                            >
                              Start
                            </button>
                          )}
                          {['queued', 'starting', 'running', 'paused', 'preflight'].includes(job.status) && (
                            <button
                              type="button"
                              onClick={() => handleJobAction('abort', job.id)}
                              className="rounded bg-rose-500 px-3 py-1 text-xs font-medium text-white hover:bg-rose-400"
                            >
                              Abort
                            </button>
                          )}
                          {['failed', 'cancelled'].includes(job.status) && (
                            <button
                              type="button"
                              onClick={() => handleJobAction('retry', job.id)}
                              className="rounded bg-cyan-500 px-3 py-1 text-xs font-medium text-slate-950 hover:bg-cyan-400"
                            >
                              Retry
                            </button>
                          )}
                          {['complete', 'failed', 'skipped', 'cancelled', 'interrupted'].includes(job.status) && (
                            <button
                              type="button"
                              onClick={() => handleJobAction('remove', job.id)}
                              className="rounded border border-slate-600 bg-slate-800 px-3 py-1 text-xs font-medium text-slate-100 hover:bg-slate-700"
                            >
                              Remove
                            </button>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            <div className="flex items-center justify-between border-t border-slate-800 px-4 py-3 text-sm text-slate-300">
              <p>Page {jobsPage} of {totalJobPages}</p>
              <div className="flex items-center gap-1">
                {Array.from({ length: totalJobPages }, (_, index) => index + 1).map((pageNumber) => (
                  <button
                    key={pageNumber}
                    type="button"
                    onClick={() => setJobsPage(pageNumber)}
                    className={`rounded px-2 py-1 text-xs ${jobsPage === pageNumber ? 'bg-cyan-500 text-slate-950' : 'bg-slate-800 text-slate-200 hover:bg-slate-700'}`}
                  >
                    {pageNumber}
                  </button>
                ))}
              </div>
            </div>
          </section>
        )}

        {activePage === 'settings' && settings && notificationSettings && (
          <section className="space-y-5 rounded-lg border border-slate-800 bg-slate-900 p-6">
            <label className="block space-y-2">
              <span>History retention days</span>
              <input
                type="number"
                min={1}
                className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                value={settings.history_retention_days}
                onChange={(event) => setSettings((prev) => ({ ...prev, history_retention_days: Number(event.target.value) }))}
              />
            </label>

            <label className="flex items-center justify-between">
              <span>Auto discovery enabled</span>
              <input
                type="checkbox"
                checked={settings.auto_discovery_enabled}
                onChange={(event) => setSettings((prev) => ({ ...prev, auto_discovery_enabled: event.target.checked }))}
              />
            </label>

            <label className="block space-y-2">
              <span>Discovery method</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                value={settings.discovery_method}
                onChange={(event) => setSettings((prev) => ({ ...prev, discovery_method: event.target.value }))}
              >
                <option value="interval">Interval</option>
                <option value="startup">Startup</option>
              </select>
            </label>

            <label className="block space-y-2">
              <span>Discovery interval minutes</span>
              <input
                type="number"
                min={1}
                className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                value={settings.discovery_interval_minutes}
                onChange={(event) => setSettings((prev) => ({ ...prev, discovery_interval_minutes: Number(event.target.value) }))}
              />
            </label>

            <label className="block space-y-2">
              <span>Workspace root</span>
              <input
                type="text"
                className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                value={settings.workspace_root}
                onChange={(event) => setSettings((prev) => ({ ...prev, workspace_root: event.target.value }))}
              />
            </label>

            <label className="block space-y-2">
              <span>Minimum free disk GB</span>
              <input
                type="number"
                min={1}
                className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                value={settings.min_free_gb}
                onChange={(event) => setSettings((prev) => ({ ...prev, min_free_gb: Number(event.target.value) }))}
              />
            </label>

            <label className="flex items-center justify-between">
              <span>Requeue interrupted jobs on startup</span>
              <input
                type="checkbox"
                checked={settings.requeue_interrupted_jobs}
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, requeue_interrupted_jobs: event.target.checked }))
                }
              />
            </label>

            <label className="flex items-center justify-between">
              <span>Cleanup workspaces on startup</span>
              <input
                type="checkbox"
                checked={settings.cleanup_workspaces_on_startup}
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, cleanup_workspaces_on_startup: event.target.checked }))
                }
              />
            </label>

            <div className="flex flex-wrap gap-3">
              <button
                type="button"
                className="rounded bg-indigo-500 px-4 py-2 font-semibold text-slate-950 hover:bg-indigo-400"
                onClick={handleRecoveryRun}
              >
                Run recovery now
              </button>
              <button
                type="button"
                className="rounded bg-cyan-500 px-4 py-2 font-semibold text-slate-950 hover:bg-cyan-400"
                onClick={handleCleanupRun}
              >
                Run workspace cleanup
              </button>
              <button
                type="button"
                className="rounded bg-amber-500 px-4 py-2 font-semibold text-slate-950 hover:bg-amber-400"
                onClick={handleOptimizedCleanupRun}
              >
                Remove optimized outputs
              </button>
            </div>

            <div className="space-y-3 rounded-lg border border-slate-700 bg-slate-800/60 p-4">
              <h3 className="text-sm font-semibold uppercase tracking-wide text-slate-300">Email notifications</h3>
              <div className="grid gap-3 md:grid-cols-2">
                <label className="space-y-1 text-sm">
                  <span>SMTP host</span>
                  <input
                    type="text"
                    className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                    value={notificationSettings.smtp_host}
                    onChange={(event) => setNotificationSettings((prev) => ({ ...prev, smtp_host: event.target.value }))}
                  />
                </label>
                <label className="space-y-1 text-sm">
                  <span>SMTP port</span>
                  <input
                    type="number"
                    min={1}
                    max={65535}
                    className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                    value={notificationSettings.smtp_port}
                    onChange={(event) => setNotificationSettings((prev) => ({ ...prev, smtp_port: Number(event.target.value) }))}
                  />
                </label>
                <label className="space-y-1 text-sm">
                  <span>SMTP username</span>
                  <input
                    type="text"
                    className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                    value={notificationSettings.smtp_user}
                    onChange={(event) => setNotificationSettings((prev) => ({ ...prev, smtp_user: event.target.value }))}
                  />
                </label>
                <label className="space-y-1 text-sm">
                  <span>SMTP password</span>
                  <input
                    type="password"
                    className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                    value={notificationSettings.smtp_password}
                    onChange={(event) => setNotificationSettings((prev) => ({ ...prev, smtp_password: event.target.value }))}
                  />
                </label>
                <label className="space-y-1 text-sm">
                  <span>From email</span>
                  <input
                    type="email"
                    className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                    value={notificationSettings.from_email}
                    onChange={(event) => setNotificationSettings((prev) => ({ ...prev, from_email: event.target.value }))}
                  />
                </label>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={notificationSettings.smtp_tls}
                    onChange={(event) => setNotificationSettings((prev) => ({ ...prev, smtp_tls: event.target.checked }))}
                  />
                  Use TLS
                </label>
              </div>
              <label className="space-y-1 text-sm">
                <span>Recipient emails (comma or newline separated)</span>
                <textarea
                  className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                  rows={3}
                  value={notificationSettings.to_emails.join(', ')}
                  onChange={(event) => setNotificationSettings((prev) => ({
                    ...prev,
                    to_emails: event.target.value
                      .split(/[\n,]/)
                      .map((item) => item.trim())
                      .filter(Boolean),
                  }))}
                />
              </label>
              <label className="flex items-center justify-between">
                <span>Job failed</span>
                <input
                  type="checkbox"
                  checked={notificationSettings.notify_on.job_failed}
                  onChange={(event) => setNotificationSettings((prev) => ({
                    ...prev,
                    notify_on: { ...prev.notify_on, job_failed: event.target.checked },
                  }))}
                />
              </label>
              <label className="flex items-center justify-between">
                <span>Job interrupted</span>
                <input
                  type="checkbox"
                  checked={notificationSettings.notify_on.job_interrupted}
                  onChange={(event) => setNotificationSettings((prev) => ({
                    ...prev,
                    notify_on: { ...prev.notify_on, job_interrupted: event.target.checked },
                  }))}
                />
              </label>
              <label className="flex items-center justify-between">
                <span>Low disk pause</span>
                <input
                  type="checkbox"
                  checked={notificationSettings.notify_on.low_disk_pause}
                  onChange={(event) => setNotificationSettings((prev) => ({
                    ...prev,
                    notify_on: { ...prev.notify_on, low_disk_pause: event.target.checked },
                  }))}
                />
              </label>
              <label className="flex items-center justify-between">
                <span>Recovery ran</span>
                <input
                  type="checkbox"
                  checked={notificationSettings.notify_on.recovery_ran}
                  onChange={(event) => setNotificationSettings((prev) => ({
                    ...prev,
                    notify_on: { ...prev.notify_on, recovery_ran: event.target.checked },
                  }))}
                />
              </label>
              <label className="flex items-center justify-between">
                <span>Batch complete</span>
                <input
                  type="checkbox"
                  checked={notificationSettings.notify_on.batch_complete}
                  onChange={(event) => setNotificationSettings((prev) => ({
                    ...prev,
                    notify_on: { ...prev.notify_on, batch_complete: event.target.checked },
                  }))}
                />
              </label>
              <div className="flex gap-3">
                <button
                  type="button"
                  className="rounded bg-violet-500 px-4 py-2 font-semibold text-slate-950 hover:bg-violet-400 disabled:opacity-70"
                  disabled={savingSettings}
                  onClick={saveNotificationSettings}
                >
                  Save notification settings
                </button>
                <button
                  type="button"
                  className="rounded bg-slate-600 px-4 py-2 font-semibold text-white hover:bg-slate-500"
                  onClick={sendNotificationTest}
                >
                  Send test email
                </button>
              </div>
            </div>

            <button
              type="button"
              className="rounded bg-cyan-500 px-4 py-2 font-semibold text-slate-950 hover:bg-cyan-400 disabled:opacity-70"
              disabled={savingSettings}
              onClick={saveSettings}
            >
              {savingSettings ? 'Saving...' : 'Save settings'}
            </button>
          </section>
        )}
      </div>
    </main>
  );
}
