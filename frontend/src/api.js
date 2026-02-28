const API_BASE = import.meta.env.VITE_API_BASE ?? '/api';

function getCookie(name) {
  if (typeof document === 'undefined') return null;
  const escaped = name.replace(/[$()*+./?[\\\]^{|}-]/g, '\\$&');
  const match = document.cookie.match(new RegExp(`(?:^|; )${escaped}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}

async function request(path, options) {
  const method = String(options?.method || 'GET').toUpperCase();
  const headers = { 'Content-Type': 'application/json', ...(options?.headers || {}) };
  if (['POST', 'PUT', 'PATCH', 'DELETE'].includes(method)) {
    const csrfToken = getCookie('optimizarr_csrf');
    if (csrfToken) headers['X-CSRF-Token'] = csrfToken;
  }

  const response = await fetch(`${API_BASE}${path}`, {
    headers,
    credentials: 'include',
    ...options,
  });

  if (!response.ok) {
    let detail;
    const bodyText = await response.text().catch(() => '');
    const contentType = response.headers.get('content-type') ?? '';
    if (contentType.includes('application/json')) {
      try {
        const payload = JSON.parse(bodyText);
        if (typeof payload?.detail === 'string' && payload.detail.trim()) {
          detail = payload.detail.trim();
        }
      } catch {
        // not valid JSON, fall through to raw text
      }
    }

    if (!detail) {
      detail = bodyText.trim();
    }

    const error = new Error(detail || `Request failed: ${response.status}`);
    error.status = response.status;
    throw error;
  }

  if (response.status === 204) {
    return null;
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) {
    return null;
  }

  return response.json();
}

export function fetchMetrics() {
  return request('/metrics');
}

export function fetchAuthStatus() {
  return request('/auth/status');
}

export function createTotpSecret(username) {
  return request('/auth/totp/secret', {
    method: 'POST',
    body: JSON.stringify({ username }),
  });
}

export function bootstrapAuth(payload) {
  return request('/auth/bootstrap', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function login(payload) {
  return request('/auth/login', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function logout() {
  return request('/auth/logout', { method: 'POST' });
}

export function fetchJobs() {
  return request('/jobs');
}

export function fetchLibraries() {
  return request('/libraries');
}

export function createLibrary(payload) {
  return request('/libraries', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

export function updateLibrary(libraryId, payload) {
  return request(`/libraries/${libraryId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function deleteLibrary(libraryId) {
  return request(`/libraries/${libraryId}`, { method: 'DELETE' });
}

export function scanLibrary(libraryId) {
  return request(`/libraries/${libraryId}/scan`, { method: 'POST' });
}

export function fetchLibraryProfile(libraryId) {
  return request(`/libraries/${libraryId}/profile`);
}

export function fetchEncoders() {
  return request('/encoders');
}

export function updateLibraryProfile(libraryId, payload) {
  return request(`/libraries/${libraryId}/profile`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function cancelJob(jobId) {
  return request(`/jobs/${jobId}/cancel`, { method: 'POST' });
}

export function retryJob(jobId) {
  return request(`/jobs/${jobId}/retry`, { method: 'POST' });
}

export function requeueJob(jobId) {
  return request(`/jobs/${jobId}/requeue`, { method: 'POST' });
}

export function pauseJob(jobId) {
  return request(`/jobs/${jobId}/pause`, { method: 'POST' });
}

export function resumeJob(jobId) {
  return request(`/jobs/${jobId}/resume`, { method: 'POST' });
}

export function startJob(jobId) {
  return request(`/jobs/${jobId}/start`, { method: 'POST' });
}

export function abortJob(jobId) {
  return request(`/jobs/${jobId}/abort`, { method: 'POST' });
}

export function discardJobProgress(jobId) {
  return request(`/jobs/${jobId}/discard-progress`, { method: 'POST' });
}


export function deleteJob(jobId) {
  return request(`/jobs/${jobId}`, { method: 'DELETE' });
}

export function abortAllJobs() {
  return request('/jobs/abort-all', { method: 'POST' });
}

export function cancelAllQueued() {
  return request('/jobs/cancel-all-queued', { method: 'POST' });
}

export function purgeHistory() {
  return request('/jobs/remove-all', { method: 'POST' });
}

export function pauseQueue() {
  return request('/queue/pause', { method: 'POST' });
}

export function resumeQueue() {
  return request('/queue/resume', { method: 'POST' });
}

export function clearQueue() {
  return request('/queue/clear', { method: 'POST' });
}

export function fetchQueueStatus() {
  return request('/queue/status');
}

export function fetchSettings() {
  return request('/settings');
}

export function updateSettings(payload) {
  return request('/settings', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}


export function fetchNotificationSettings() {
  return request('/notifications/settings');
}

export function updateNotificationSettings(payload) {
  return request('/notifications/settings', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function sendTestNotification() {
  return request('/notifications/test', { method: 'POST' });
}

export async function fetchWsToken() {
  try {
    return await request('/auth/ws-token');
  } catch (error) {
    if (error.status === 404) {
      return null;
    }
    throw error;
  }
}

export function runRecovery() {
  return request('/recovery/run', { method: 'POST' });
}

export function runCleanup() {
  return request('/cleanup/run', { method: 'POST' });
}

export function runOptimizedCleanup() {
  return request('/cleanup/optimized', { method: 'POST' });
}

export function fetchPlexSettings() {
  return request('/plex/settings');
}

export function fetchPlexLibraries() {
  return request('/plex/libraries');
}

export function updatePlexSettings(payload) {
  return request('/plex/settings', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function testPlexConnection() {
  return request('/plex/test', { method: 'POST' });
}

export function fetchProwlarrSettings() {
  return request('/prowlarr/settings');
}

export function updateProwlarrSettings(payload) {
  return request('/prowlarr/settings', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function testProwlarrConnection() {
  return request('/prowlarr/test', { method: 'POST' });
}

export function fetchQBittorrentSettings() {
  return request('/download-client/qbittorrent');
}

export function updateQBittorrentSettings(payload) {
  return request('/download-client/qbittorrent', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function testQBittorrentConnection() {
  return request('/download-client/qbittorrent/test', { method: 'POST' });
}

export function fetchSabnzbdSettings() {
  return request('/download-client/sabnzbd');
}

export function updateSabnzbdSettings(payload) {
  return request('/download-client/sabnzbd', {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function testSabnzbdConnection() {
  return request('/download-client/sabnzbd/test', { method: 'POST' });
}

export function fetchDownloadJobs() {
  return request('/download-jobs');
}

export function fetchDirs(path) {
  const qs = path ? `?path=${encodeURIComponent(path)}` : '';
  return request(`/fs/dirs${qs}`);
}

export function removeAndResetDownloadJob(jobId) {
  return request(`/download-jobs/${jobId}/cancel`, { method: 'POST' });
}

export function cancelDownloadJob(jobId) {
  return removeAndResetDownloadJob(jobId);
}

export function deleteDownloadJob(jobId) {
  return request(`/download-jobs/${jobId}`, { method: 'DELETE' });
}

export function deleteAllDownloadJobs() {
  return request('/download-jobs', { method: 'DELETE' });
}

export function retryDownloadJob(jobId) {
  return request(`/download-jobs/${jobId}/retry`, { method: 'POST' });
}
