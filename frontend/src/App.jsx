import { useEffect, useMemo, useRef, useState } from 'react';
import {
  abortJob,
  cancelJob,
  fetchLibraries,
  fetchLibraryProfile,
  fetchJobs,
  fetchMetrics,
  fetchSettings,
  fetchWsToken,
  pauseJob,
  pauseQueue,
  resumeJob,
  resumeQueue,
  retryJob,
  runRecovery,
  scanLibrary,
  updateLibrary,
  updateLibraryProfile,
  updateSettings,
} from './api';
import StatCard from './components/StatCard';

const WS_PATH = '/ws';
const FALLBACK_AFTER_MS = 30000;
const FALLBACK_POLL_MS = 10000;
const RECONNECT_BASE_DELAY_MS = 1000;
const RECONNECT_MAX_DELAY_MS = 30000;

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

function progressFromStatus(status) {
  const normalized = status?.toLowerCase();
  if (normalized === 'completed') return 100;
  if (normalized === 'running' || normalized === 'processing') return 65;
  if (normalized === 'failed') return 100;
  if (normalized === 'canceled' || normalized === 'cancelled') return 100;
  return 20;
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

function validateLibraryDraft(draft, libraryEnabled) {
  const errors = {};

  if (!libraryEnabled) {
    return errors;
  }

  if (!Number.isInteger(draft.target_resolution) || draft.target_resolution < 1) {
    errors.target_resolution = 'Target resolution must be a positive integer.';
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

  if (!Number.isInteger(draft.schedule_start_hour) || draft.schedule_start_hour < 0 || draft.schedule_start_hour > 23) {
    errors.schedule_start_hour = 'Schedule start must be between 0 and 23.';
  }

  if (!Number.isInteger(draft.schedule_end_hour) || draft.schedule_end_hour < 0 || draft.schedule_end_hour > 23) {
    errors.schedule_end_hour = 'Schedule end must be between 0 and 23.';
  }

  if (draft.av1_fallback_codec === 'av1') {
    errors.av1_fallback_codec = 'AV1 fallback must be HEVC or H.264.';
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
  const [selectedLibraryId, setSelectedLibraryId] = useState(null);
  const [profileDraft, setProfileDraft] = useState(null);
  const [profileErrors, setProfileErrors] = useState({});
  const [selectedPreset, setSelectedPreset] = useState('balanced');
  const [savingProfile, setSavingProfile] = useState(false);
  const [scanningLibraries, setScanningLibraries] = useState({});
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [savingSettings, setSavingSettings] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState('connecting');
  const [fallbackPollingEnabled, setFallbackPollingEnabled] = useState(false);
  const [queuePaused, setQueuePaused] = useState(false);

  const wsRef = useRef();
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef();
  const fallbackTimerRef = useRef();
  const intentionallyClosedRef = useRef(false);

  const queueCount = useMemo(
    () => jobs.filter((job) => ['pending', 'queued', 'created'].includes(job.status?.toLowerCase())).length,
    [jobs],
  );

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
      if (!settings?.enable_optimizer) {
        state = 'Paused (optimizer disabled)';
      } else if (!library.enabled) {
        state = 'Paused (library disabled)';
      } else if (profile && !isWithinWindow(nowHour, profile.schedule_start_hour, profile.schedule_end_hour)) {
        state = 'Paused by schedule';
      } else if (
        settings?.global_quiet_enabled
        && isWithinWindow(nowHour, settings.global_quiet_start_hour, settings.global_quiet_end_hour)
      ) {
        state = 'Paused (quiet hours)';
      }

      return { library, state, queue: libraryQueueCount(library, jobs) };
    });
  }, [jobs, libraries, libraryProfiles, settings]);

  async function refreshAll() {
    try {
      const [nextMetrics, nextJobs, nextSettings] = await Promise.all([
        fetchMetrics(),
        fetchJobs(),
        fetchSettings(),
      ]);
      setMetrics(nextMetrics);
      setJobs(nextJobs);
      setSettings(nextSettings);
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

  function mergeJobUpdate(nextJob) {
    setJobs((prevJobs) => {
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
    if (!selectedLibraryId || !selectedLibraryProfile) {
      setProfileDraft(null);
      setProfileErrors({});
      return;
    }
    setProfileDraft({ ...selectedLibraryProfile });
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
      } else if (action === 'abort') {
        await abortJob(jobId);
      }
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Job action failed.');
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

  return (
    <main className="min-h-screen bg-slate-950 p-6 text-slate-100">
      <div className="mx-auto max-w-7xl space-y-6">
        <header className="flex flex-col gap-3 md:flex-row md:items-center md:justify-between">
          <h1 className="text-3xl font-bold text-cyan-200">Optimizarr</h1>
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

        {activePage === 'dashboard' && (
          <section className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
              <StatCard label="GPU %" value={`${metrics?.gpu_video_percent ?? 0}%`} />
              <StatCard label="CPU %" value={`${metrics?.cpu_percent ?? 0}%`} />
              <StatCard label="Active jobs" value={metrics?.active_jobs ?? 0} />
              <StatCard label="Queue count" value={queueCount} />
              <StatCard label="Libraries" value={libraries.length} />
              <StatCard label="Enable toggle" value={settings?.enable_optimizer ? 'ON' : 'OFF'} />
            </div>

            <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
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
            <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
              <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-300">Libraries</h2>
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
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
              {!selectedLibrary || !profileDraft ? (
                <p className="text-sm text-slate-300">Select a library to edit its profile.</p>
              ) : (
                <div className="space-y-4">
                  <div className="rounded border border-slate-800 bg-slate-950/40 p-3">
                    <h2 className="text-lg font-semibold text-cyan-200">{selectedLibrary.name}</h2>
                    <p className="text-xs text-slate-400">Path: {selectedLibrary.path}</p>
                    <p className="text-xs text-slate-300">Status: {libraryRuntimeStates.find((item) => item.library.id === selectedLibrary.id)?.state}</p>
                  </div>

                  <div className="space-y-2">
                    <label className="block text-sm">Quality preset</label>
                    <select
                      className="w-full rounded border border-slate-700 bg-slate-800 p-2"
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
                    <label className="space-y-2">
                      <span className="text-sm">Enabled</span>
                      <input
                        type="checkbox"
                        checked={selectedLibrary.enabled}
                        onChange={(event) => handleLibraryToggle(selectedLibrary.id, event.target.checked)}
                      />
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">HDR only</span>
                      <input
                        type="checkbox"
                        checked={profileDraft.hdr_only}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, hdr_only: event.target.checked }))}
                      />
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">Target resolution</span>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.target_resolution === 1080 || profileDraft.target_resolution === 720 ? String(profileDraft.target_resolution) : 'custom'}
                        onChange={(event) => {
                          const value = event.target.value;
                          setProfileDraft((prev) => ({
                            ...prev,
                            target_resolution: value === 'custom' ? prev.target_resolution : Number(value),
                          }));
                        }}
                      >
                        <option value="1080">1080p</option>
                        <option value="720">720p</option>
                        <option value="custom">Custom</option>
                      </select>
                      {profileDraft.target_resolution !== 1080 && profileDraft.target_resolution !== 720 && (
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

                    <label className="space-y-2">
                      <span className="text-sm">Codec</span>
                      <select
                        className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                        value={profileDraft.codec}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, codec: event.target.value }))}
                      >
                        <option value="h264">H.264</option>
                        <option value="hevc">HEVC</option>
                        <option value="av1">AV1</option>
                      </select>
                    </label>

                    <label className="space-y-2">
                      <span className="text-sm">AV1 fallback</span>
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

                    <label className="space-y-2">
                      <span className="text-sm">Bitrate mode</span>
                      <div className="flex gap-3 text-sm">
                        <label>
                          <input
                            type="radio"
                            name="bitrate-mode"
                            checked={profileDraft.bitrate_mode === 'vbr_crf'}
                            onChange={() => setProfileDraft((prev) => ({ ...prev, bitrate_mode: 'vbr_crf' }))}
                          />
                          {' '}CRF
                        </label>
                        <label>
                          <input
                            type="radio"
                            name="bitrate-mode"
                            checked={profileDraft.bitrate_mode === 'cbr'}
                            onChange={() => setProfileDraft((prev) => ({ ...prev, bitrate_mode: 'cbr' }))}
                          />
                          {' '}CBR
                        </label>
                      </div>
                    </label>

                    {profileDraft.bitrate_mode === 'vbr_crf' && (
                      <label className="space-y-2">
                        <span className="text-sm">CRF</span>
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
                      <p className="text-xs text-slate-400">slow=best efficiency, fast=quickest</p>
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
                      <input
                        type="range"
                        min={1}
                        max={10}
                        value={profileDraft.max_workers}
                        onChange={(event) => setProfileDraft((prev) => ({ ...prev, max_workers: Number(event.target.value) }))}
                      />
                      {profileErrors.max_workers && <p className="text-xs text-red-300">{profileErrors.max_workers}</p>}
                    </label>

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

                    <label className="space-y-2">
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
              <button
                type="button"
                onClick={() => handleQueueAction(queuePaused ? 'resume' : 'pause')}
                className="rounded bg-amber-500 px-3 py-1 text-xs font-medium text-slate-950 hover:bg-amber-400"
              >
                {queuePaused ? 'Resume Queue' : 'Pause Queue'}
              </button>
            </div>
            <table className="min-w-full divide-y divide-slate-800">
              <thead className="bg-slate-800/70">
                <tr>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">ID</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Source</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Status</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Progress</th>
                  <th className="px-4 py-3 text-left text-xs font-semibold uppercase text-slate-300">Actions</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-slate-800">
                {jobs.map((job) => {
                  const progress = progressFromStatus(job.status);
                  return (
                    <tr key={job.id}>
                      <td className="px-4 py-3">{job.id}</td>
                      <td className="max-w-xs truncate px-4 py-3 text-slate-300">{job.source_path}</td>
                      <td className="px-4 py-3 capitalize">{job.status}</td>
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
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>
        )}

        {activePage === 'settings' && settings && (
          <section className="space-y-5 rounded-lg border border-slate-800 bg-slate-900 p-6">
            <label className="flex items-center justify-between">
              <span>Toggle optimizer</span>
              <input
                type="checkbox"
                checked={settings.enable_optimizer}
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, enable_optimizer: event.target.checked }))
                }
              />
            </label>

            <label className="block space-y-2">
              <span>Resolution</span>
              <select
                className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                value={settings.target_resolution}
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, target_resolution: Number(event.target.value) }))
                }
              >
                <option value={720}>720p</option>
                <option value={1080}>1080p</option>
                <option value={1440}>1440p</option>
                <option value={2160}>2160p</option>
              </select>
            </label>

            <label className="block space-y-2">
              <span>Bitrate slider ({settings.bitrate_mbps} Mbps)</span>
              <input
                className="w-full"
                type="range"
                min={2}
                max={40}
                value={settings.bitrate_mbps}
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, bitrate_mbps: Number(event.target.value) }))
                }
              />
            </label>

            <label className="block space-y-2">
              <span>Worker count slider ({settings.max_workers})</span>
              <input
                className="w-full"
                type="range"
                min={1}
                max={10}
                value={settings.max_workers}
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, max_workers: Number(event.target.value) }))
                }
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

            <button
              type="button"
              className="rounded bg-indigo-500 px-4 py-2 font-semibold text-slate-950 hover:bg-indigo-400"
              onClick={handleRecoveryRun}
            >
              Run recovery now
            </button>

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
