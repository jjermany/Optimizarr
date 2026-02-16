import { useEffect, useMemo, useRef, useState } from 'react';
import {
  cancelJob,
  fetchLibraries,
  fetchLibraryProfile,
  fetchJobs,
  fetchMetrics,
  fetchSettings,
  fetchWsToken,
  retryJob,
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
  jobs: 'Jobs',
  settings: 'Settings',
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

export default function App() {
  const [activePage, setActivePage] = useState('dashboard');
  const [metrics, setMetrics] = useState();
  const [jobs, setJobs] = useState([]);
  const [libraries, setLibraries] = useState([]);
  const [libraryProfiles, setLibraryProfiles] = useState({});
  const [settings, setSettings] = useState();
  const [error, setError] = useState('');
  const [savingSettings, setSavingSettings] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState('connecting');
  const [fallbackPollingEnabled, setFallbackPollingEnabled] = useState(false);

  const wsRef = useRef();
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef();
  const fallbackTimerRef = useRef();
  const intentionallyClosedRef = useRef(false);

  const queueCount = useMemo(
    () => jobs.filter((job) => ['pending', 'queued', 'created'].includes(job.status?.toLowerCase())).length,
    [jobs],
  );

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
      } else {
        await retryJob(jobId);
      }
      await refreshAll();
    } catch (actionError) {
      setError(actionError.message || 'Job action failed.');
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

  return (
    <main className="min-h-screen bg-slate-950 p-6">
      <div className="mx-auto max-w-6xl space-y-6">
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

        {activePage === 'dashboard' && (
          <section className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
            <StatCard label="GPU %" value={`${metrics?.gpu_video_percent ?? 0}%`} />
            <StatCard label="CPU %" value={`${metrics?.cpu_percent ?? 0}%`} />
            <StatCard label="Active jobs" value={metrics?.active_jobs ?? 0} />
            <StatCard label="Queue count" value={queueCount} />
            <StatCard label="Libraries" value={libraries.length} />
            <StatCard label="Profiles" value={Object.keys(libraryProfiles).length} />
            <StatCard label="Enable toggle" value={settings?.enable_optimizer ? 'ON' : 'OFF'} />
          </section>
        )}

        {activePage === 'jobs' && (
          <section className="overflow-hidden rounded-lg border border-slate-800 bg-slate-900">
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
                          <button
                            type="button"
                            onClick={() => handleJobAction('cancel', job.id)}
                            className="rounded bg-rose-500 px-3 py-1 text-xs font-medium text-white hover:bg-rose-400"
                          >
                            Cancel
                          </button>
                          <button
                            type="button"
                            onClick={() => handleJobAction('retry', job.id)}
                            className="rounded bg-cyan-500 px-3 py-1 text-xs font-medium text-slate-950 hover:bg-cyan-400"
                          >
                            Retry
                          </button>
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

            <div className="grid gap-4 sm:grid-cols-2">
              <label className="block space-y-2">
                <span>Schedule start time</span>
                <input
                  type="time"
                  step={3600}
                  className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                  value={formatHour(settings.schedule_start_hour)}
                  onChange={(event) =>
                    setSettings((prev) => ({
                      ...prev,
                      schedule_start_hour: parseHour(event.target.value),
                    }))
                  }
                />
              </label>
              <label className="block space-y-2">
                <span>Schedule end time</span>
                <input
                  type="time"
                  step={3600}
                  className="w-full rounded border border-slate-700 bg-slate-800 p-2"
                  value={formatHour(settings.schedule_end_hour)}
                  onChange={(event) =>
                    setSettings((prev) => ({
                      ...prev,
                      schedule_end_hour: parseHour(event.target.value),
                    }))
                  }
                />
              </label>
            </div>

            <label className="flex items-center justify-between">
              <span>HDR only toggle</span>
              <input
                type="checkbox"
                checked={settings.process_hdr_only}
                onChange={(event) =>
                  setSettings((prev) => ({ ...prev, process_hdr_only: event.target.checked }))
                }
              />
            </label>

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
