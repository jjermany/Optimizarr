import { useEffect, useMemo, useRef, useState } from 'react';
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
  deleteDownloadedFile,
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
  fetchLogs,
  fetchMetrics,
  fetchNotificationSettings,
  fetchPlexLibraries,
  fetchPlexSettings,
  fetchProwlarrSettings,
  fetchQueueStatus,
  fetchSettings,
  fallbackReviewedDownload,
  importReviewedDownload,
  login as loginRequest,
  logout as logoutRequest,
  pauseJob,
  pauseQueue,
  purgeHistory,
  requeueJob,
  resumeJob,
  resumeQueue,
  retryDownloadJob,
  retryReviewedDownload,
  retryJob,
  startJob,
  runCleanup,
  runDuplicateOptimizedCleanup,
  runOptimizedCleanup,
  runRecovery,
  scanLibrary,
  updateLibrary,
  updateLibraryProfile,
  updateSettings,
} from './api';
import StatCard from './components/StatCard';
import JobsPage from './components/JobsPage';
import LogsPage from './components/LogsPage';
import SettingsPage from './components/SettingsPage';
import useEventCallback from './lib/useEventCallback';
import { buildUnifiedQueueItems } from './queueSorting';
import { isWithinWindow } from './scheduleWindow';

import {
  WS_PATH,
  FALLBACK_AFTER_MS,
  REALTIME_BATCH_MS,
  FALLBACK_POLL_MS,
  ACTIVE_QUEUE_POLL_MS,
  METRICS_POLL_MS,
  QUEUE_RECONCILE_POLL_MS,
  RECONNECT_BASE_DELAY_MS,
  RECONNECT_MAX_DELAY_MS,
  PROFILE_SECTIONS_DEFAULT,
  PAGE_KEYS,
  QUALITY_PRESETS,
  TARGET_RESOLUTION_PRESETS,
  MIN_SOURCE_RESOLUTION_PRESETS,
  CODEC_LABELS,
  formatHour,
  parseHour,
  TERMINAL_STATUSES,
  ACTIVE_DL_STATUSES,
  TERMINAL_DL_STATUSES,
  QUEUE_DEDUPE_DL_STATUSES,
  LOG_REFRESH_SYSTEM_EVENTS,
  isActiveEncodeStatus,
  mergeJobsWithUpdate,
  mergeDownloadJobsWithUpdate,
  removeJobById,
  libraryQueueCount,
  formatGpuPercent,
  buildQrCodeDataUrl,
  normalizeDownloadJob,
  extractTitleYear,
  LIBRARIES_UI_PREFS_KEY,
  loadJobsUiPrefs,
  loadLibrariesUiPrefs,
  validateLibraryDraft,
  getAvailableDownloadFallbackCodecs,
  validateLibraryForm,
  libraryDetailsHaveChanges,
  libraryProfileHasChanges,
  buildLibraryProfileOverview,
  buildDashboardOperationalStatus,
  mergeLibraryScanProgress,
} from './lib/appUtils';
export * from './lib/appUtils';

import {
  SectionCard,
  SectionTitle,
  CollapsibleSection,
  FormField,
  TextInput,
  SelectInput,
  Btn,
  Modal,
  Toggle,
  StatusDot,
  DirBrowserModal,
} from './components/ui';

// ── Main App ─────────────────────────────────────────────────────────────────

const VALID_PAGES = new Set(Object.keys(PAGE_KEYS));

function pageFromHash() {
  const hash = window.location.hash.slice(1);
  return VALID_PAGES.has(hash) ? hash : 'dashboard';
}

export default function App() {
  const [activePage, setActivePage] = useState(pageFromHash);
  const [metrics, setMetrics] = useState();
  const [jobs, setJobs] = useState([]);
  const [libraries, setLibraries] = useState([]);
  const [libraryBaselines, setLibraryBaselines] = useState({});
  const [libraryProfiles, setLibraryProfiles] = useState({});
  const [settings, setSettings] = useState();
  const [accountSettings, setAccountSettings] = useState();
  const [logs, setLogs] = useState([]);
  const [loadingLogs, setLoadingLogs] = useState(false);
  const [notificationSettings, setNotificationSettings] = useState();
  const [plexSettings, setPlexSettings] = useState();
  const [plexLibraries, setPlexLibraries] = useState([]);
  const [prowlarrSettings, setProwlarrSettings] = useState();
  const [qbtSettings, setQbtSettings] = useState();
  const [sabSettings, setSabSettings] = useState();
  const [downloadJobs, setDownloadJobs] = useState([]);
  const [dirBrowser, setDirBrowser] = useState({ open: false, target: null, initialPath: null });
  const [selectedLibraryId, setSelectedLibraryId] = useState(null);
  const [profileDraft, setProfileDraft] = useState(null);
  const [profileBaseline, setProfileBaseline] = useState(null);
  const [profileSectionsOpen, setProfileSectionsOpen] = useState(() => ({
    ...PROFILE_SECTIONS_DEFAULT,
    // Fall back to the legacy combined prefs key so saved section states survive.
    ...(loadJobsUiPrefs().profileSectionsOpen ?? {}),
    ...(loadLibrariesUiPrefs().profileSectionsOpen ?? {}),
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
  const [initialDataLoaded, setInitialDataLoaded] = useState(false);
  const [initialDataError, setInitialDataError] = useState(null);
  const [refreshingData, setRefreshingData] = useState(false);
  const [connectionStatus, setConnectionStatus] = useState('connecting');
  const [fallbackPollingEnabled, setFallbackPollingEnabled] = useState(false);
  const [queuePaused, setQueuePaused] = useState(false);
  const [toasts, setToasts] = useState([]);
  const [pinnedOperation, setPinnedOperation] = useState(null);
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
  const [queuePageResetSignal, setQueuePageResetSignal] = useState(0);
  const [nowHour, setNowHour] = useState(() => new Date().getHours());
  const [pendingJobActions, setPendingJobActions] = useState({});
  const [pendingDownloadActions, setPendingDownloadActions] = useState({});
  const [hasUnsavedSettings, setHasUnsavedSettings] = useState(false);

  const wsRef = useRef();
  const jobsStateRef = useRef([]);
  const pendingJobUpdatesRef = useRef([]);
  const pendingDownloadUpdatesRef = useRef([]);
  const realtimeFlushTimerRef = useRef();
  const lastRealtimeJobStatusRef = useRef(new Map());
  const lastRealtimeDownloadStatusRef = useRef(new Map());
  const logsRefreshTimerRef = useRef();
  const downloadReconcileInFlightRef = useRef(false);
  const refreshAllRequestIdRef = useRef(0);
  const refreshAllInFlightCountRef = useRef(0);
  const liveRefreshInFlightRef = useRef(false);
  const reconnectAttemptsRef = useRef(0);
  const reconnectTimerRef = useRef();
  const fallbackTimerRef = useRef();
  const queueSnapshotTimerRef = useRef();
  const intentionallyClosedRef = useRef(false);
  const toastTimersRef = useRef({});
  const pinnedOperationTimerRef = useRef();

  // Mirror of the jobs state for timer callbacks that need the latest list
  // without re-subscribing (realtime batch flush).
  useEffect(() => {
    jobsStateRef.current = jobs;
  }, [jobs]);

  // Stable-identity handler wrappers so memoized page components skip
  // re-renders caused by App re-creating functions each render.
  const onPushToast = useEventCallback((message, tone, options) => pushToast(message, tone, options));
  const onAuthExpired = useEventCallback(() => refreshAuthStatus());
  const onRefreshLogs = useEventCallback((options) => refreshLogs(options));
  const onJobAction = useEventCallback((action, jobId) => handleJobAction(action, jobId));
  const onCancelDownloadJob = useEventCallback((jobId) => handleCancelDownloadJob(jobId));
  const onRetryDownloadJob = useEventCallback((jobId) => handleRetryDownloadJob(jobId));
  const onReviewDownloadJob = useEventCallback((action, jobId, selectedFilePath) => handleReviewDownloadJob(action, jobId, selectedFilePath));
  const onDeleteDownloadJob = useEventCallback((jobId) => handleDeleteDownloadJob(jobId));
  const onDeleteDownloadedFile = useEventCallback((jobId, retry) => handleDeleteDownloadedFile(jobId, retry));
  const onCancelAllQueued = useEventCallback(() => handleCancelAllQueued());
  const onClearQueue = useEventCallback(() => handleClearQueue());
  const onAbortAllJobs = useEventCallback(() => handleAbortAllJobs());
  const onQueueAction = useEventCallback((action) => handleQueueAction(action));
  const onPurgeHistory = useEventCallback(() => handlePurgeHistory());
  const onPersistQueueSort = useEventCallback((nextSort) => persistQueueSort(nextSort));
  const onShowQrCode = useEventCallback((payload) => openQrModal(payload));
  const onRecoveryRun = useEventCallback(() => handleRecoveryRun());
  const onCleanupRun = useEventCallback(() => handleCleanupRun());
  const onOptimizedCleanupRun = useEventCallback(() => handleOptimizedCleanupRun());
  const onDuplicateOptimizedCleanupRun = useEventCallback(() => handleDuplicateOptimizedCleanupRun());
  const onSettingsDirtyChange = useEventCallback((dirty) => setHasUnsavedSettings(Boolean(dirty)));

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

  // Count-only unified queue view for the header badge and dashboard; the
  // sort option does not affect the count, so 'default' keeps this stable.
  const unifiedAllQueueItems = useMemo(
    () => buildUnifiedQueueItems({
      encodeItems: activeJobs,
      downloadItems: activeDlQueueItems,
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
      dedupeSourcePaths: queueDedupeSourcePaths,
    }),
    [activeJobs, activeDlQueueItems, queueDedupeSourcePaths],
  );

  const queueCount = unifiedAllQueueItems.length;
  const workingDownloadStatuses = new Set(['checking', 'searching', 'downloading', 'repairing', 'unpacking', 'moving', 'importing']);
  const dashboardStatus = buildDashboardOperationalStatus({
    queuePaused,
    libraries,
    queueCount,
    workingCount: activeJobs.filter((job) => isActiveEncodeStatus(job.status)).length
      + activeDlQueueItems.filter((job) => workingDownloadStatuses.has(String(job.status ?? '').toLowerCase())).length,
  });

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(
      LIBRARIES_UI_PREFS_KEY,
      JSON.stringify({ profileSectionsOpen }),
    );
  }, [profileSectionsOpen]);

  const selectedLibrary = useMemo(
    () => libraries.find((library) => library.id === selectedLibraryId) ?? null,
    [libraries, selectedLibraryId],
  );

  const selectedLibraryProfile = selectedLibraryId ? libraryProfiles[selectedLibraryId] : null;
  const selectedLibraryBaseline = selectedLibraryId ? libraryBaselines[selectedLibraryId] : null;
  const libraryDetailsDirty = libraryDetailsHaveChanges(selectedLibrary, selectedLibraryBaseline);
  const libraryProfileDirty = libraryProfileHasChanges(profileDraft, profileBaseline);
  const hasUnsavedLibraryChanges = libraryDetailsDirty || libraryProfileDirty;
  const libraryProfileOverview = useMemo(
    () => buildLibraryProfileOverview(profileDraft, {
      prowlarrEnabled: Boolean(prowlarrSettings?.enabled),
      qbittorrentEnabled: Boolean(qbtSettings?.enabled),
      sabnzbdEnabled: Boolean(sabSettings?.enabled),
    }),
    [profileDraft, prowlarrSettings?.enabled, qbtSettings?.enabled, sabSettings?.enabled],
  );
  const unsavedLibraryChangesRef = useRef(false);
  const unsavedSettingsRef = useRef(false);
  const selectedLibraryIdRef = useRef(null);

  useEffect(() => {
    unsavedLibraryChangesRef.current = hasUnsavedLibraryChanges;
  }, [hasUnsavedLibraryChanges]);

  useEffect(() => {
    unsavedSettingsRef.current = hasUnsavedSettings;
  }, [hasUnsavedSettings]);

  useEffect(() => {
    selectedLibraryIdRef.current = selectedLibraryId;
  }, [selectedLibraryId]);

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
      clearPinnedOperationTimer();
      setPinnedOperation(null);
      setAuthStatus({ loading: false, setup_required: false, authenticated: false, username: null, two_factor_enabled: false });
      setConnectionStatus('offline');
      setFallbackPollingEnabled(false);
    }
  }

  async function refreshAll({ showToast = true, markRefreshing = true } = {}) {
    const requestId = refreshAllRequestIdRef.current + 1;
    refreshAllRequestIdRef.current = requestId;
    if (markRefreshing) {
      refreshAllInFlightCountRef.current += 1;
      setRefreshingData(true);
    }
    try {
      const [nextMetrics, nextJobs, nextSettings, nextAccountSettings, nextNotificationSettings, nextPlexSettings, nextEncoders, nextQueueStatus, nextProwlarrSettings, nextQbtSettings, nextSabSettings, nextDownloadJobs, nextLogs] = await Promise.all([
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
        fetchLogs().catch(() => []),
      ]);
      if (requestId !== refreshAllRequestIdRef.current) return false;
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
      setLogs(nextLogs ?? []);
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
      return true;
    } catch (refreshError) {
      if (refreshError.status === 401) {
        await refreshAuthStatus();
        return false;
      }
      if (showToast) pushToast(refreshError.message || 'Could not refresh data.', 'error');
      return false;
    } finally {
      if (markRefreshing) {
        refreshAllInFlightCountRef.current = Math.max(0, refreshAllInFlightCountRef.current - 1);
        if (refreshAllInFlightCountRef.current === 0) setRefreshingData(false);
      }
    }
  }

  async function refreshQueueSnapshot() {
    if (liveRefreshInFlightRef.current) return false;
    liveRefreshInFlightRef.current = true;
    try {
      const [nextMetrics, nextJobs, nextQueueStatus, nextDownloadJobs] = await Promise.all([
        fetchMetrics().catch(() => null),
        fetchJobs(),
        fetchQueueStatus(),
        fetchDownloadJobs().catch(() => []),
      ]);
      if (nextMetrics) setMetrics(nextMetrics);
      setJobs(nextJobs ?? []);
      setQueuePaused(nextQueueStatus?.status === 'paused');
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      return true;
    } catch (refreshError) {
      if (refreshError.status === 401) {
        await refreshAuthStatus();
      }
      return false;
    } finally {
      liveRefreshInFlightRef.current = false;
    }
  }

  function scheduleQueueSnapshotRefresh(delayMs = 0) {
    if (queueSnapshotTimerRef.current) {
      window.clearTimeout(queueSnapshotTimerRef.current);
    }
    queueSnapshotTimerRef.current = window.setTimeout(() => {
      queueSnapshotTimerRef.current = undefined;
      refreshQueueSnapshot();
    }, delayMs);
  }

  async function refreshSettingsPanel() {
    try {
      const [
        nextSettings,
        nextAccountSettings,
        nextNotificationSettings,
        nextPlexSettings,
        nextProwlarrSettings,
        nextQbtSettings,
        nextSabSettings,
      ] = await Promise.all([
        fetchSettings(),
        fetchAccountSettings(),
        fetchNotificationSettings(),
        fetchPlexSettings(),
        fetchProwlarrSettings().catch(() => null),
        fetchQBittorrentSettings().catch(() => null),
        fetchSabnzbdSettings().catch(() => null),
      ]);
      setSettings(nextSettings);
      setAccountSettings(nextAccountSettings);
      setNotificationSettings(nextNotificationSettings);
      setPlexSettings(nextPlexSettings);
      if (nextProwlarrSettings) setProwlarrSettings(nextProwlarrSettings);
      if (nextQbtSettings) setQbtSettings(nextQbtSettings);
      if (nextSabSettings) setSabSettings(nextSabSettings);
    } catch (refreshError) {
      if (refreshError.status === 401) {
        await refreshAuthStatus();
        return;
      }
      pushToast(refreshError.message || 'Could not refresh settings.', 'error');
    }
  }


  async function refreshLogs(options = {}) {
    const silent = options?.silent === true;
    if (!silent) setLoadingLogs(true);
    try {
      const nextLogs = await fetchLogs();
      setLogs(nextLogs ?? []);
    } catch (refreshError) {
      if (refreshError.status === 401) {
        await refreshAuthStatus();
        return;
      }
      if (!silent) pushToast(refreshError.message || 'Could not refresh logs.', 'error');
    } finally {
      if (!silent) setLoadingLogs(false);
    }
  }


  async function persistQueueSort(nextSort) {
    try {
      const updated = await updateSettings({ queue_sort: nextSort });
      setSettings(updated);
    } catch (err) {
      pushToast(err.message || 'Failed to update queue sort order.', 'error');
    }
  }

  async function refreshLibrariesAndProfiles({ showToast = true } = {}) {
    try {
      const nextLibraries = await fetchLibraries();
      setLibraries((prev) => {
        if (!unsavedLibraryChangesRef.current || !selectedLibraryIdRef.current) return nextLibraries;
        const editedLibrary = prev.find((library) => library.id === selectedLibraryIdRef.current);
        if (!editedLibrary) return nextLibraries;
        return nextLibraries.map((library) => (
          library.id === editedLibrary.id
            ? { ...library, name: editedLibrary.name, path: editedLibrary.path }
            : library
        ));
      });
      setLibraryBaselines(Object.fromEntries(nextLibraries.map((library) => [library.id, { ...library }])));
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
      const nextProfiles = Object.fromEntries(profileEntries);
      setLibraryProfiles((prev) => {
        const editedLibraryId = selectedLibraryIdRef.current;
        if (!unsavedLibraryChangesRef.current || !editedLibraryId || !prev[editedLibraryId]) return nextProfiles;
        return { ...nextProfiles, [editedLibraryId]: prev[editedLibraryId] };
      });
      return true;
    } catch (refreshError) {
      if (showToast) pushToast(refreshError.message || 'Could not refresh libraries.', 'error');
      return false;
    }
  }

  function navigate(page) {
    if (
      page !== activePage
      && activePage === 'settings'
      && unsavedSettingsRef.current
      && !window.confirm('Leave Settings with unsaved changes? Your edits will remain available until you reload Optimizarr.')
    ) return;
    if (
      page !== activePage
      && unsavedLibraryChangesRef.current
      && !window.confirm('Discard unsaved changes to this library?')
    ) return;
    if (page !== activePage && unsavedLibraryChangesRef.current) discardSelectedLibraryChanges();
    window.location.hash = page;
    setActivePage(page);
  }

  function discardSelectedLibraryChanges() {
    unsavedLibraryChangesRef.current = false;
    if (selectedLibraryId && selectedLibraryBaseline) {
      setLibraries((prev) => prev.map((library) => (
        library.id === selectedLibraryId ? { ...selectedLibraryBaseline } : library
      )));
    }
    if (profileBaseline) setProfileDraft({ ...profileBaseline });
    setProfileErrors({});
    setLibraryFormErrors({});
  }

  function selectLibrary(libraryId) {
    if (libraryId === selectedLibraryId) return;
    if (unsavedLibraryChangesRef.current && !window.confirm('Discard unsaved changes to this library?')) return;
    if (unsavedLibraryChangesRef.current) discardSelectedLibraryChanges();
    setSelectedLibraryId(libraryId);
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

  function clearPinnedOperationTimer() {
    if (pinnedOperationTimerRef.current) {
      window.clearInterval(pinnedOperationTimerRef.current);
      pinnedOperationTimerRef.current = undefined;
    }
  }

  function startPinnedOperation({ title, detail, autoProgress = true, ...metadata }) {
    clearPinnedOperationTimer();
    setPinnedOperation({
      ...metadata,
      title,
      detail,
      progress: 1,
      tone: 'running',
    });
    if (!autoProgress) return;
    pinnedOperationTimerRef.current = window.setInterval(() => {
      setPinnedOperation((prev) => {
        if (!prev || prev.tone !== 'running') return prev;
        const nextProgress = prev.progress < 72
          ? prev.progress + 3
          : prev.progress < 88
            ? prev.progress + 1
            : Math.min(92, prev.progress + 0.25);
        return { ...prev, progress: Math.min(92, Math.round(nextProgress)) };
      });
    }, 1200);
  }

  function updatePinnedOperation(detail, progress, { force = false, operation, libraryId } = {}) {
    setPinnedOperation((prev) => {
      if (!prev) return prev;
      if (operation && prev.operation !== operation) return prev;
      if (libraryId != null && String(prev.libraryId) !== String(libraryId)) return prev;
      return {
        ...prev,
        detail,
        progress: force ? Math.min(100, progress) : Math.max(prev.progress ?? 0, Math.min(99, progress)),
      };
    });
  }

  function completePinnedOperation(detail, { operation, libraryId } = {}) {
    clearPinnedOperationTimer();
    setPinnedOperation((prev) => {
      if (!prev) return prev;
      if (operation && prev.operation !== operation) return prev;
      if (libraryId != null && String(prev.libraryId) !== String(libraryId)) return prev;
      return { ...prev, detail, progress: 100, tone: 'success' };
    });
    window.setTimeout(() => setPinnedOperation((prev) => {
      if (operation && prev?.operation !== operation) return prev;
      if (libraryId != null && String(prev?.libraryId) !== String(libraryId)) return prev;
      return null;
    }), 2200);
  }

  function failPinnedOperation(detail, { operation, libraryId } = {}) {
    clearPinnedOperationTimer();
    setPinnedOperation((prev) => {
      if (!prev) return prev;
      if (operation && prev.operation !== operation) return prev;
      if (libraryId != null && String(prev.libraryId) !== String(libraryId)) return prev;
      return { ...prev, detail, tone: 'error' };
    });
    window.setTimeout(() => setPinnedOperation((prev) => {
      if (operation && prev?.operation !== operation) return prev;
      if (libraryId != null && String(prev?.libraryId) !== String(libraryId)) return prev;
      return null;
    }), 7000);
  }

  function mergeJobUpdate(nextJob) {
    let resetToFirstPage = false;
    setJobs((prevJobs) => {
      const merged = mergeJobsWithUpdate(prevJobs, nextJob);
      resetToFirstPage = merged.resetToFirstPage;
      return merged.jobs;
    });
    if (resetToFirstPage) setQueuePageResetSignal((n) => n + 1);
  }

  // Realtime job/download events arrive one per row during bulk transitions
  // (queue pause/resume, schedule enforcement, cancel-all). Buffer them and
  // apply the whole batch in a single state update so the list re-sorts once
  // instead of rows visibly dropping in one at a time.
  function flushRealtimeUpdates() {
    realtimeFlushTimerRef.current = undefined;
    const jobUpdates = pendingJobUpdatesRef.current;
    pendingJobUpdatesRef.current = [];
    const downloadUpdates = pendingDownloadUpdatesRef.current;
    pendingDownloadUpdatesRef.current = [];

    if (jobUpdates.length > 0) {
      const prevById = new Map(jobsStateRef.current.map((job) => [job.id, job]));
      const resetToFirstPage = jobUpdates.some((jobUpdate) => {
        if (!isActiveEncodeStatus(jobUpdate.status)) return false;
        const previous = prevById.get(jobUpdate.id);
        return !previous || !isActiveEncodeStatus(previous.status);
      });
      setJobs((prevJobs) => jobUpdates.reduce(
        (acc, jobUpdate) => mergeJobsWithUpdate(acc, jobUpdate).jobs,
        prevJobs,
      ));
      if (resetToFirstPage) setQueuePageResetSignal((n) => n + 1);
    }

    if (downloadUpdates.length > 0) {
      setDownloadJobs((prevDownloadJobs) => downloadUpdates.reduce(
        (acc, downloadUpdate) => mergeDownloadJobsWithUpdate(acc, downloadUpdate),
        prevDownloadJobs,
      ));
    }
  }

  function scheduleRealtimeFlush() {
    if (realtimeFlushTimerRef.current) return;
    realtimeFlushTimerRef.current = window.setTimeout(flushRealtimeUpdates, REALTIME_BATCH_MS);
  }

  function queueRealtimeJobUpdate(jobPayload) {
    if (jobPayload?.id == null) return;
    pendingJobUpdatesRef.current.push(jobPayload);
    scheduleRealtimeFlush();
    // Reconcile with a full snapshot only when a status actually changes;
    // progress-only ticks are already merged locally.
    const status = String(jobPayload.status ?? '').toLowerCase();
    if (lastRealtimeJobStatusRef.current.get(jobPayload.id) !== status) {
      lastRealtimeJobStatusRef.current.set(jobPayload.id, status);
      scheduleQueueSnapshotRefresh();
    }
  }

  function queueRealtimeDownloadUpdate(downloadPayload) {
    if (downloadPayload?.id == null) return;
    const next = normalizeDownloadJob(downloadPayload);
    pendingDownloadUpdatesRef.current.push(next);
    scheduleRealtimeFlush();
    const status = String(next.status ?? '').toLowerCase();
    if (lastRealtimeDownloadStatusRef.current.get(next.id) !== status) {
      lastRealtimeDownloadStatusRef.current.set(next.id, status);
      scheduleQueueSnapshotRefresh();
    }
  }

  function scheduleLogsRefresh(delayMs = 600) {
    if (logsRefreshTimerRef.current) window.clearTimeout(logsRefreshTimerRef.current);
    logsRefreshTimerRef.current = window.setTimeout(() => {
      logsRefreshTimerRef.current = undefined;
      refreshLogs({ silent: true });
    }, delayMs);
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
    refreshAuthStatus();
  }, []);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return;
    let active = true;
    setInitialDataLoaded(false);
    setInitialDataError(null);
    setConnectionStatus('connecting');

    async function loadInitialData() {
      const [dataOk, librariesOk] = await Promise.all([
        refreshAll({ showToast: false, markRefreshing: false }),
        refreshLibrariesAndProfiles({ showToast: false }),
      ]);
      if (!active) return;
      if (dataOk && librariesOk) {
        setInitialDataLoaded(true);
        setInitialDataError(null);
        return;
      }
      setInitialDataError('Could not load the Optimizarr workspace. Check that the API is reachable and try again.');
    }

    loadInitialData();
    return () => { active = false; };
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
      setProfileBaseline(null);
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
    setProfileBaseline({ ...nextDraft });
    setTargetResolutionCustom(!TARGET_RESOLUTION_PRESETS.includes(Number(nextDraft.target_resolution)));
    setMinimumResolutionCustom(!MIN_SOURCE_RESOLUTION_PRESETS.includes(Number(nextDraft.minimum_source_resolution)));
    setProfileErrors({});
  }, [selectedLibraryId, selectedLibraryProfile]);

  useEffect(() => {
    function warnBeforeUnload(event) {
      if (!unsavedLibraryChangesRef.current && !unsavedSettingsRef.current) return;
      event.preventDefault();
      event.returnValue = '';
    }
    window.addEventListener('beforeunload', warnBeforeUnload);
    return () => window.removeEventListener('beforeunload', warnBeforeUnload);
  }, []);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return undefined;
    if (!fallbackPollingEnabled) return undefined;
    const timer = setInterval(refreshQueueSnapshot, FALLBACK_POLL_MS);
    return () => clearInterval(timer);
  }, [fallbackPollingEnabled, authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return undefined;
    const timer = setInterval(refreshQueueSnapshot, QUEUE_RECONCILE_POLL_MS);
    return () => clearInterval(timer);
  }, [authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return;
    if (activePage !== 'settings') return;
    if (settings && accountSettings && notificationSettings && plexSettings) return;
    refreshSettingsPanel();
  }, [
    activePage,
    settings,
    accountSettings,
    notificationSettings,
    plexSettings,
    authStatus.loading,
    authStatus.setup_required,
    authStatus.authenticated,
  ]);

  useEffect(() => {
    if (authStatus.loading || authStatus.setup_required || !authStatus.authenticated) return;
    if (activePage !== 'logs') return;
    refreshLogs();
  }, [activePage, authStatus.loading, authStatus.setup_required, authStatus.authenticated]);

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
    const timer = setInterval(refreshQueueSnapshot, ACTIVE_QUEUE_POLL_MS);
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
          refreshQueueSnapshot();
          refreshLibrariesAndProfiles();
        };

        websocket.onmessage = (event) => {
          let payload;
          try {
            payload = JSON.parse(event.data);
          } catch {
            return;
          }
          if (!payload || typeof payload !== 'object') return;
          if (payload.type === 'job_update') {
            queueRealtimeJobUpdate(payload.data);
            return;
          }
          if (payload.type === 'download_job_update') {
            queueRealtimeDownloadUpdate(payload.data);
            return;
          }
          if (payload.type === 'metrics_update') { setMetrics(payload.data); return; }
          if (payload.type === 'library_update') {
            refreshLibrariesAndProfiles();
            scheduleQueueSnapshotRefresh();
            return;
          }
          if (payload.type === 'notification') {
            if (payload.data?.message === 'queue_paused_low_disk') pushToast('Queue paused due to low disk.', 'warn');
            return;
          }
          if (payload.type === 'system_event') {
            const systemEvent = payload.data?.event;
            if (systemEvent === 'manual_library_scan_progress') {
              setPinnedOperation((prev) => mergeLibraryScanProgress(prev, payload.data));
              return;
            }
            if (systemEvent === 'duplicate_optimized_cleanup_progress') {
              const progress = Number(payload.data?.progress_percent);
              const detail = payload.data?.message || 'Duplicate cleanup is running...';
              if (Number.isFinite(progress)) {
                setPinnedOperation((prev) => {
                  const nextProgress = Math.max(0, Math.min(100, Math.round(progress)));
                  const previousProgress = prev?.title === 'Duplicate Output Cleanup' ? Number(prev.progress ?? 0) : 0;
                  const displayedProgress = Math.max(previousProgress, nextProgress);
                  return {
                    operation: 'duplicate_cleanup',
                    title: 'Duplicate Output Cleanup',
                    detail,
                    progress: displayedProgress,
                    tone: displayedProgress >= 100 ? 'success' : 'running',
                  };
                });
              }
              return;
            }
            if (LOG_REFRESH_SYSTEM_EVENTS.has(systemEvent)) scheduleLogsRefresh();
            if (systemEvent === 'job_aborted') { pushToast('Aborted job.', 'error'); return; }
            if (systemEvent === 'job_removed') {
              pendingJobUpdatesRef.current = pendingJobUpdatesRef.current.filter((u) => u.id !== payload.data?.job_id);
              setJobs((prev) => removeJobById(prev, payload.data?.job_id));
              scheduleQueueSnapshotRefresh();
              return;
            }
            if (systemEvent === 'download_job_removed') {
              pendingDownloadUpdatesRef.current = pendingDownloadUpdatesRef.current.filter((u) => u.id !== payload.data?.download_job_id);
              setDownloadJobs((prev) => prev.filter((dj) => dj.id !== payload.data?.download_job_id));
              scheduleQueueSnapshotRefresh();
              return;
            }
            if (systemEvent === 'queue_paused') {
              setQueuePaused(true);
              if (payload.data?.reason === 'low_disk') { pushToast('Queue paused due to low disk.', 'warn'); return; }
              if (payload.data?.reason !== 'manual_scan') { pushToast('Queue paused.', 'warn'); }
              return;
            }
            if (systemEvent === 'queue_resumed') {
              setQueuePaused(false);
              return;
            }
            if (systemEvent === 'library_scan_started') {
              setLibraries((prev) => prev.map((library) => (
                library.id === payload.data?.library_id ? { ...library, scanning: true } : library
              )));
              return;
            }
            if (systemEvent === 'library_scan_completed') {
              setLibraries((prev) => prev.map((library) => (
                library.id === payload.data?.library_id ? { ...library, scanning: false } : library
              )));
              scheduleQueueSnapshotRefresh();
              return;
            }
            if (systemEvent === 'optimized_cleanup_summary' || systemEvent === 'duplicate_optimized_cleanup_summary') {
              refreshAll();
              refreshLibrariesAndProfiles();
              scheduleQueueSnapshotRefresh();
              return;
            }
            if (systemEvent === 'cleanup_summary' || systemEvent === 'recovery_summary') {
              if (systemEvent === 'recovery_summary' && payload.data?.trigger === 'startup') pushToast('Recovery ran on startup.', 'info');
              return;
            }
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
      if (queueSnapshotTimerRef.current) {
        window.clearTimeout(queueSnapshotTimerRef.current);
        queueSnapshotTimerRef.current = undefined;
      }
      if (realtimeFlushTimerRef.current) {
        window.clearTimeout(realtimeFlushTimerRef.current);
        realtimeFlushTimerRef.current = undefined;
      }
      pendingJobUpdatesRef.current = [];
      pendingDownloadUpdatesRef.current = [];
      if (logsRefreshTimerRef.current) {
        window.clearTimeout(logsRefreshTimerRef.current);
        logsRefreshTimerRef.current = undefined;
      }
      Object.values(toastTimersRef.current).forEach((timerId) => window.clearTimeout(timerId));
      toastTimersRef.current = {};
      clearPinnedOperationTimer();
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
      else if (action === 'remove') { await deleteJob(jobId); setJobs((prev) => removeJobById(prev, jobId)); }
      await refreshLogs();
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
    const encodeCount = activeJobs.length;
    if (encodeCount === 0) return;
    if (!window.confirm(`Cancel all ${encodeCount} non-terminal encode job${encodeCount === 1 ? '' : 's'}? This includes queued, paused, and running encodes, and partial progress will be removed.`)) return;
    try {
      const result = await abortAllJobs();
      pushToast(`Aborted ${result.aborted_job_ids.length} encode job(s).`, 'success');
      await refreshAll();
      await refreshLogs();
    } catch (actionError) {
      pushToast(actionError.message || 'Abort all failed.', 'error');
    }
  }

  async function handleCancelAllQueued() {
    const queuedCount = activeJobs.filter((job) => job.status === 'queued').length;
    if (queuedCount === 0) return;
    if (!window.confirm(`Cancel ${queuedCount} queued encode job${queuedCount === 1 ? '' : 's'}? Running and paused encodes will not be changed.`)) return;
    try {
      const result = await cancelAllQueued();
      pushToast(`Cancelled ${result.cancelled_job_ids.length} queued encode job(s).`, 'success');
      await refreshAll();
      await refreshLogs();
    } catch (actionError) {
      pushToast(actionError.message || 'Cancel all queued failed.', 'error');
    }
  }

  async function handlePurgeHistory() {
    const terminalEncodeCount = jobs.filter((job) => TERMINAL_STATUSES.has(String(job.status ?? '').toLowerCase())).length;
    const terminalDownloadCount = downloadJobs.filter((job) => {
      const status = String(job.status ?? '').toLowerCase();
      return TERMINAL_DL_STATUSES.has(status) && status !== 'fallback_queued';
    }).length;
    const historyCount = terminalEncodeCount + terminalDownloadCount;
    if (historyCount === 0) return;
    if (!window.confirm(`Clear ${historyCount} history item${historyCount === 1 ? '' : 's'}? This removes encode and download records but does not delete media files.`)) return;
    try {
      const result = await purgeHistory();
      // Remove terminal download jobs from local state immediately — the server
      // endpoint now clears both encode history and terminal download jobs.
      setDownloadJobs((prev) => prev.filter((dj) => !TERMINAL_DL_STATUSES.has(String(dj.status ?? '').toLowerCase())));
      const removedEncodeCount = result?.removed_job_ids?.length ?? 0;
      const removedDownloadCount = result?.removed_download_job_ids?.length ?? 0;
      pushToast(`Purged ${removedEncodeCount + removedDownloadCount} history item(s).`, 'success');
      await refreshAll();
      await refreshLogs();
    } catch (actionError) {
      pushToast(actionError.message || 'Purge history failed.', 'error');
    }
  }

  async function handleQueueAction(action) {
    try {
      if (action === 'pause') { await pauseQueue(); setQueuePaused(true); }
      else { await resumeQueue(); setQueuePaused(false); }
      await refreshAll();
      await refreshLogs();
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
      await refreshLogs();
    } catch (actionError) {
      pushToast(actionError.message || 'Clear queue failed.', 'error');
    }
  }

  async function handleCreateLibrary() {
    if (unsavedLibraryChangesRef.current && !window.confirm('Discard unsaved changes to the selected library and add a new library?')) return;
    if (unsavedLibraryChangesRef.current) discardSelectedLibraryChanges();
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
    const libraryToDelete = libraries.find((library) => library.id === libraryId);
    if (!window.confirm(`Delete ${libraryToDelete?.name || 'this library'}? Jobs and media files will not be deleted.`)) return;
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
      setLibraryBaselines((prev) => ({ ...prev, [updated.id]: { ...updated } }));
      setLibraryFormErrors({});
      pushToast('Library details saved.', 'success');
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
    const libraryName = libraries.find((library) => library.id === libraryId)?.name || 'Library';
    startPinnedOperation({
      operation: 'library_scan',
      libraryId,
      title: `Library Scan: ${libraryName}`,
      detail: 'Starting library scan...',
      autoProgress: false,
    });
    try {
      const result = await scanLibrary(libraryId);
      updatePinnedOperation(
        'Scan complete. Refreshing dashboard data...',
        99,
        { operation: 'library_scan', libraryId },
      );
      await refreshAll();
      const createdCount = result?.created_jobs?.length ?? 0;
      completePinnedOperation(
        `Complete. Created ${createdCount} job${createdCount === 1 ? '' : 's'}.`,
        { operation: 'library_scan', libraryId },
      );
    } catch (scanError) {
      failPinnedOperation(
        scanError.message || 'Library scan failed.',
        { operation: 'library_scan', libraryId },
      );
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
      setProfileBaseline({ ...updated });
      setTargetResolutionCustom(!TARGET_RESOLUTION_PRESETS.includes(Number(updated.target_resolution)));
      setMinimumResolutionCustom(!MIN_SOURCE_RESOLUTION_PRESETS.includes(Number(updated.minimum_source_resolution)));
      pushToast('Library profile saved.', 'success');
    } catch (saveError) {
      pushToast(saveError.message || 'Failed to save library profile.', 'error');
    } finally {
      setSavingProfile(false);
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











  async function handleCancelDownloadJob(jobId) {
    if (pendingDownloadActions[jobId]) return;
    setPendingDownloadActions((prev) => ({ ...prev, [jobId]: 'remove_reset' }));
    try {
      await removeAndResetDownloadJob(jobId);
      const [nextJobs, nextDownloadJobs] = await Promise.all([fetchJobs(), fetchDownloadJobs()]);
      setJobs(nextJobs ?? []);
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      await refreshLogs();
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
      await refreshLogs();
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

  async function handleReviewDownloadJob(action, jobId, selectedFilePath) {
    if (pendingDownloadActions[jobId]) return;
    setPendingDownloadActions((prev) => ({ ...prev, [jobId]: `review_${action}` }));
    try {
      if (action === 'import') await importReviewedDownload(jobId, selectedFilePath);
      else if (action === 'retry') await retryReviewedDownload(jobId);
      else if (action === 'fallback') await fallbackReviewedDownload(jobId);
      else throw new Error('Unknown review action');
      const [nextJobs, nextDownloadJobs] = await Promise.all([fetchJobs(), fetchDownloadJobs()]);
      setJobs(nextJobs ?? []);
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      await refreshLogs();
      pushToast(
        action === 'import' ? 'Reviewed file imported.' :
          action === 'retry' ? 'Release rejected and a new search was queued.' :
            'Download rejected and source encode queued.',
        'success',
      );
    } catch (err) {
      pushToast(err.message || 'Could not complete the review action.', 'error');
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
      await refreshLogs();
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

  async function handleDeleteDownloadedFile(jobId, retry) {
    if (pendingDownloadActions[jobId]) return;
    const isQbit = downloadJobs.some((job) => job.id === jobId && job.client_type === 'qbittorrent');
    const seedNotice = isQbit ? ' The qBittorrent payload will remain available for seeding.' : '';
    const prompt = retry
      ? `Delete the imported library file and search for a different release?${seedNotice}`
      : `Delete the imported library file?${seedNotice}`;
    if (!window.confirm(prompt)) return;
    setPendingDownloadActions((prev) => ({ ...prev, [jobId]: retry ? 'delete_retry' : 'delete_file' }));
    try {
      await deleteDownloadedFile(jobId, retry);
      const [nextJobs, nextDownloadJobs] = await Promise.all([fetchJobs(), fetchDownloadJobs()]);
      setJobs(nextJobs ?? []);
      setDownloadJobs((nextDownloadJobs ?? []).map(normalizeDownloadJob));
      await refreshLogs();
      pushToast(
        retry ? 'Library file deleted and a new search was queued.' :
          isQbit ? 'Library file deleted; seed payload retained.' : 'Library file deleted.',
        'success',
      );
    } catch (err) {
      pushToast(err.message || 'Could not delete the imported file.', 'error');
    } finally {
      setPendingDownloadActions((prev) => {
        const next = { ...prev };
        delete next[jobId];
        return next;
      });
    }
  }

  async function handleRecoveryRun() {
    try {
      const result = await runRecovery();
      pushToast(`Recovered ${result.recovered_jobs} jobs.`, 'success');
      await refreshAll();
      await refreshLogs();
    } catch (recoveryError) {
      pushToast(recoveryError.message || 'Recovery failed.', 'error');
    }
  }

  async function handleCleanupRun() {
    try {
      const result = await runCleanup();
      pushToast(`Cleanup removed ${result.cleaned_workspaces} workspace(s).`, 'success');
      await refreshAll();
      await refreshLogs();
    } catch (cleanupError) {
      pushToast(cleanupError.message || 'Cleanup failed.', 'error');
    }
  }

  async function handleOptimizedCleanupRun() {
    try {
      const result = await runOptimizedCleanup();
      pushToast(`Deleted ${result.deleted_files} optimized file(s) from ${result.affected_job_ids.length} job(s).`, 'success');
      await refreshAll();
      await refreshLogs();
    } catch (cleanupError) {
      pushToast(cleanupError.message || 'Optimized cleanup failed.', 'error');
    }
  }

  async function handleDuplicateOptimizedCleanupRun() {
    if (pinnedOperation?.tone === 'running' && pinnedOperation.title === 'Duplicate Output Cleanup') return;
    startPinnedOperation({
      operation: 'duplicate_cleanup',
      title: 'Duplicate Output Cleanup',
      detail: 'Scanning libraries for duplicate optimized outputs...',
      autoProgress: false,
    });
    try {
      const result = await runDuplicateOptimizedCleanup();
      updatePinnedOperation(
        'Cleanup finished. Refreshing dashboard data...',
        94,
        { operation: 'duplicate_cleanup' },
      );
      pushToast(`Deleted ${result.deleted_files} duplicate optimized file(s) from ${result.affected_library_ids.length} librar${result.affected_library_ids.length === 1 ? 'y' : 'ies'}.`, 'success');
      await refreshAll();
      updatePinnedOperation('Refreshing logs...', 98, { operation: 'duplicate_cleanup' });
      await refreshLogs();
      completePinnedOperation(
        `Complete. Removed ${result.deleted_files} duplicate file${result.deleted_files === 1 ? '' : 's'}.`,
        { operation: 'duplicate_cleanup' },
      );
    } catch (cleanupError) {
      failPinnedOperation(
        cleanupError.message || 'Duplicate cleanup failed.',
        { operation: 'duplicate_cleanup' },
      );
      pushToast(cleanupError.message || 'Duplicate cleanup failed.', 'error');
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  if (authStatus.loading) {
    return (
      <main className="app-shell min-h-screen p-4 text-slate-100 md:p-6">
        <div className="mx-auto flex min-h-[70vh] w-full min-w-0 max-w-md items-center justify-center">
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
        <div className="mx-auto flex min-h-[70vh] w-full min-w-0 max-w-lg items-center justify-center">
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
        <div className="mx-auto flex min-h-[70vh] w-full min-w-0 max-w-md items-center justify-center">
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

  if (!initialDataLoaded) {
    return (
      <main className="app-shell min-h-screen p-4 text-slate-100 md:p-6">
        <div className="mx-auto flex min-h-[70vh] w-full min-w-0 max-w-md items-center justify-center">
          <SectionCard className="w-full space-y-4">
            <SectionTitle>Loading Workspace</SectionTitle>
            {!initialDataError ? (
              <p className="text-sm text-slate-300">Loading dashboard, queue, libraries, and settings...</p>
            ) : (
              <>
                <p className="text-sm text-red-300">{initialDataError}</p>
                <div className="flex flex-wrap gap-2">
                  <Btn
                    variant="primary"
                    onClick={async () => {
                      setInitialDataError(null);
                      const [dataOk, librariesOk] = await Promise.all([
                        refreshAll({ showToast: false, markRefreshing: false }),
                        refreshLibrariesAndProfiles({ showToast: false }),
                      ]);
                      if (dataOk && librariesOk) setInitialDataLoaded(true);
                      else setInitialDataError('Could not load the Optimizarr workspace. Check that the API is reachable and try again.');
                    }}
                  >
                    Retry
                  </Btn>
                  <Btn variant="secondary" onClick={handleLogout}>
                    Logout
                  </Btn>
                </div>
              </>
            )}
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
              {refreshingData && (
                <span role="status" className="rounded-full border border-cyan-700/60 bg-cyan-950/30 px-2.5 py-1 text-xs text-cyan-200">
                  Syncing
                </span>
              )}
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
          <nav aria-label="Primary navigation" className="relative mt-3 flex gap-1 overflow-x-auto rounded-xl border border-slate-700/70 bg-slate-950/60 p-1">
            {Object.entries(PAGE_KEYS).map(([key, label]) => (
              <button
                key={key}
                type="button"
                aria-current={activePage === key ? 'page' : undefined}
                className={`shrink-0 rounded-lg px-2 py-2 text-xs font-medium transition-all duration-200 sm:px-4 sm:text-sm ${
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
        <div aria-label="Notifications" aria-live="polite" className="pointer-events-none fixed right-4 bottom-4 z-50 flex flex-col gap-2">
          {toasts.map((toast) => (
            <div
              key={toast.id}
              role={toast.tone === 'error' ? 'alert' : 'status'}
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

        {pinnedOperation && (
          <div
            role="status"
            aria-live="polite"
            className={`fixed left-4 right-4 bottom-4 z-40 rounded-xl border p-4 shadow-2xl backdrop-blur-sm sm:right-auto sm:w-[24rem] ${
              pinnedOperation.tone === 'error'
                ? 'border-red-700/70 bg-red-950/95 text-red-100'
                : pinnedOperation.tone === 'success'
                  ? 'border-emerald-700/70 bg-emerald-950/95 text-emerald-100'
                  : 'border-cyan-700/70 bg-slate-950/95 text-slate-100'
            }`}
          >
            <div className="mb-2 flex items-start justify-between gap-3">
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold">{pinnedOperation.title}</p>
                <p className="mt-0.5 text-xs text-slate-300">{pinnedOperation.detail}</p>
              </div>
              <span className="shrink-0 font-mono text-sm font-semibold">
                {Math.round(pinnedOperation.progress)}%
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-slate-800">
              <div
                className={`h-full rounded-full transition-all duration-500 ${
                  pinnedOperation.tone === 'error'
                    ? 'bg-red-400'
                    : pinnedOperation.tone === 'success'
                      ? 'bg-emerald-400'
                      : 'bg-cyan-400'
                }`}
                style={{ width: `${Math.max(0, Math.min(100, pinnedOperation.progress))}%` }}
              />
            </div>
          </div>
        )}

        {/* ── Dashboard ──────────────────────────────────────────────────────── */}
        {activePage === 'dashboard' && (
          <section className="animate-fade-in space-y-5">
            <SectionCard className={`border-l-4 ${
              dashboardStatus.tone === 'paused'
                ? 'border-l-amber-400'
                : dashboardStatus.tone === 'working'
                  ? 'border-l-cyan-400'
                  : dashboardStatus.tone === 'setup'
                    ? 'border-l-violet-400'
                    : 'border-l-emerald-400'
            }`}>
              <div className="relative flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
                <div>
                  <p className="text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Operational status</p>
                  <p className="mt-1 text-lg font-semibold text-slate-100">{dashboardStatus.title}</p>
                  <p className="mt-1 text-sm text-slate-400">{dashboardStatus.detail}</p>
                </div>
                {queuePaused ? (
                  <Btn variant="success" onClick={() => handleQueueAction('resume')}>Resume New Jobs</Btn>
                ) : (
                  <Btn variant="secondary" onClick={() => navigate(dashboardStatus.action)}>
                    {dashboardStatus.action === 'jobs' ? 'View Queue' : libraries.length === 0 ? 'Add Library' : 'Manage Libraries'}
                  </Btn>
                )}
              </div>
            </SectionCard>

            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-6">
              <StatCard label="GPU" value={formatGpuPercent(metrics)} />
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
                  <div className="rounded-xl border border-dashed border-slate-700 bg-slate-950/25 px-4 py-8 text-center">
                    <p className="text-sm font-medium text-slate-300">No libraries configured yet.</p>
                    <p className="mt-1 text-xs text-slate-500">Add a movie or TV library to begin discovering eligible media.</p>
                    <Btn className="mt-4" size="sm" onClick={() => navigate('libraries')}>Add Library</Btn>
                  </div>
                )}
                {libraryRuntimeStates.map(({ library, state, queue }) => (
                  <div key={library.id} className="flex flex-col gap-3 rounded-lg border border-slate-800/60 bg-slate-950/40 px-4 py-3 transition-colors duration-150 hover:border-slate-700/60 sm:flex-row sm:items-center sm:justify-between">
                    <div className="min-w-0">
                      <p className="font-medium text-cyan-200">{library.name}</p>
                      <p className="mt-0.5 truncate text-xs text-slate-500" title={library.path}>{library.path}</p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2 sm:justify-end">
                      <div className="mr-1 sm:text-right">
                        <p className="text-sm text-slate-300">{state}</p>
                        <p className="mt-0.5 text-xs text-slate-500">Queue: {queue}</p>
                      </div>
                      <Btn size="sm" variant="secondary" disabled={Boolean(library.scanning)} onClick={() => handleLibraryScan(library.id)}>
                        {library.scanning ? 'Scanning…' : 'Scan'}
                      </Btn>
                      <Btn size="sm" variant="secondary" onClick={() => { setSelectedLibraryId(library.id); navigate('libraries'); }}>Edit</Btn>
                    </div>
                  </div>
                ))}
              </div>
            </SectionCard>
          </section>
        )}

        {/* ── Libraries ──────────────────────────────────────────────────────── */}
        {activePage === 'libraries' && (
          <section className="min-w-0 animate-fade-in grid gap-5 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">

            {/* Left: list + add form */}
            <div className="min-w-0 space-y-4">
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
                        aria-label="Browse for new library path"
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
                        <button type="button" className="min-w-0 flex-1 text-left" onClick={() => selectLibrary(library.id)}>
                          <p className="font-medium text-cyan-200 truncate">{library.name}</p>
                          <p className="mt-0.5 text-xs text-slate-500 truncate">{library.path}</p>
                          <p className="mt-0.5 text-xs text-slate-400">Queue: {libraryQueueCount(library, jobs, downloadJobs)}</p>
                        </button>
                        <div className="flex shrink-0 items-center gap-2">
                          <Toggle
                            ariaLabel={`${library.enabled ? 'Disable' : 'Enable'} ${library.name}`}
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
                  {hasUnsavedLibraryChanges && (
                    <div role="status" className="sticky top-3 z-20 flex flex-col gap-3 rounded-xl border border-amber-500/40 bg-amber-950/95 px-4 py-3 shadow-xl shadow-slate-950/50 sm:flex-row sm:items-center sm:justify-between">
                      <div>
                        <p className="text-sm font-semibold text-amber-100">Unsaved library changes</p>
                        <p className="text-xs text-amber-200/70">
                          {libraryDetailsDirty && libraryProfileDirty
                            ? 'Library details and profile settings have changed.'
                            : libraryDetailsDirty
                              ? 'Library details have changed.'
                              : 'Profile settings have changed.'}
                        </p>
                      </div>
                      <Btn variant="secondary" size="sm" onClick={discardSelectedLibraryChanges}>Discard</Btn>
                    </div>
                  )}
                  <div className="rounded-xl border border-cyan-700/40 bg-cyan-950/20 p-4">
                    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
                      <div>
                        <p className="text-sm font-semibold text-cyan-100">Profile behavior</p>
                        <p className="text-xs text-slate-400">A preview of how Optimizarr will handle this library.</p>
                      </div>
                      <span className={`rounded-full border px-2.5 py-1 text-xs font-medium ${
                        selectedLibrary.enabled
                          ? 'border-emerald-600/40 bg-emerald-950/30 text-emerald-300'
                          : 'border-slate-600/60 bg-slate-900/60 text-slate-400'
                      }`}
                      >
                        {selectedLibrary.enabled ? 'Active' : 'Library paused'}
                      </span>
                    </div>
                    <dl className="grid gap-2 sm:grid-cols-2">
                      {libraryProfileOverview.items.map((item) => (
                        <div key={item.label} className="rounded-lg border border-slate-800/70 bg-slate-950/35 px-3 py-2">
                          <dt className="text-[11px] font-semibold uppercase tracking-wider text-slate-500">{item.label}</dt>
                          <dd className="mt-0.5 text-sm text-slate-200">{item.value}</dd>
                        </div>
                      ))}
                    </dl>
                    {libraryProfileOverview.warnings.length > 0 && (
                      <div className="mt-3 space-y-2" role="alert">
                        {libraryProfileOverview.warnings.map((warning) => (
                          <p key={warning} className="rounded-lg border border-amber-600/40 bg-amber-950/35 px-3 py-2 text-xs text-amber-200">
                            {warning} Configure the integration in Settings.
                          </p>
                        ))}
                      </div>
                    )}
                  </div>
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
                            aria-label={`Browse for ${selectedLibrary.name} library path`}
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
                        <Btn variant="primary" disabled={savingLibrary || !libraryDetailsDirty} onClick={handleSaveLibraryDetails}>
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
                        ariaLabel={`Enable ${selectedLibrary.name}`}
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
                        ariaLabel="Only process HDR media"
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
                        ariaLabel="Tone-map HDR to SDR"
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
                        <input type="range" min={18} max={30} value={profileDraft.crf ?? 23} onChange={(e) => setProfileDraft((prev) => ({ ...prev, crf: Number(e.target.value) }))} className="w-full" />
                        <TextInput type="number" min={18} max={30} value={profileDraft.crf ?? 23} onChange={(e) => setProfileDraft((prev) => ({ ...prev, crf: Number(e.target.value) }))} className="mt-2" />
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
                        ariaLabel="Enable download mode"
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

                  <Btn variant="primary" size="lg" disabled={savingProfile || !libraryProfileDirty} onClick={handleSaveLibraryProfile} className="w-full sm:w-auto">
                    {savingProfile ? 'Saving…' : 'Save Profile'}
                  </Btn>
                </div>
              )}
            </SectionCard>
          </section>
        )}

        {/* ── Jobs ───────────────────────────────────────────────────────────── */}
        {activePage === 'jobs' && (
          <JobsPage
            jobs={jobs}
            downloadJobs={downloadJobs}
            libraryById={libraryById}
            libraryProfiles={libraryProfiles}
            queuePaused={queuePaused}
            pendingJobActions={pendingJobActions}
            pendingDownloadActions={pendingDownloadActions}
            settingsQueueSort={settings?.queue_sort}
            pageResetSignal={queuePageResetSignal}
            onJobAction={onJobAction}
            onCancelDownloadJob={onCancelDownloadJob}
            onRetryDownloadJob={onRetryDownloadJob}
            onReviewDownloadJob={onReviewDownloadJob}
            onDeleteDownloadJob={onDeleteDownloadJob}
            onDeleteDownloadedFile={onDeleteDownloadedFile}
            onCancelAllQueued={onCancelAllQueued}
            onClearQueue={onClearQueue}
            onAbortAllJobs={onAbortAllJobs}
            onQueueAction={onQueueAction}
            onPurgeHistory={onPurgeHistory}
            onPersistQueueSort={onPersistQueueSort}
          />
        )}

        {/* ── Settings ───────────────────────────────────────────────────────── */}
        {activePage === 'logs' && (
          <LogsPage
            logs={logs}
            loadingLogs={loadingLogs}
            onRefresh={onRefreshLogs}
            setLogs={setLogs}
            pushToast={onPushToast}
            onAuthError={onAuthExpired}
          />
        )}

        <div className={activePage === 'settings' ? '' : 'hidden'} aria-hidden={activePage === 'settings' ? undefined : 'true'}>
          <SettingsPage
            settings={settings}
            setSettings={setSettings}
            accountSettings={accountSettings}
            setAccountSettings={setAccountSettings}
            notificationSettings={notificationSettings}
            setNotificationSettings={setNotificationSettings}
            plexSettings={plexSettings}
            setPlexSettings={setPlexSettings}
            plexLibraries={plexLibraries}
            setPlexLibraries={setPlexLibraries}
            prowlarrSettings={prowlarrSettings}
            setProwlarrSettings={setProwlarrSettings}
            qbtSettings={qbtSettings}
            setQbtSettings={setQbtSettings}
            sabSettings={sabSettings}
            setSabSettings={setSabSettings}
            setAuthStatus={setAuthStatus}
            duplicateCleanupRunning={pinnedOperation?.tone === 'running' && pinnedOperation.title === 'Duplicate Output Cleanup'}
            pushToast={onPushToast}
            onShowQrCode={onShowQrCode}
            onRecoveryRun={onRecoveryRun}
            onCleanupRun={onCleanupRun}
            onOptimizedCleanupRun={onOptimizedCleanupRun}
            onDuplicateOptimizedCleanupRun={onDuplicateOptimizedCleanupRun}
            onDirtyChange={onSettingsDirtyChange}
          />
        </div>

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
