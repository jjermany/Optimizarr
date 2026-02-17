const API_BASE = import.meta.env.VITE_API_BASE ?? '/api';

async function request(path, options) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });

  if (!response.ok) {
    const detail = await response.text();
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


export function deleteJob(jobId) {
  return request(`/jobs/${jobId}`, { method: 'DELETE' });
}

export function abortAllJobs() {
  return request('/jobs/abort-all', { method: 'POST' });
}

export function removeAllJobs() {
  return request('/jobs/remove-all', { method: 'POST' });
}

export function pauseQueue() {
  return request('/queue/pause', { method: 'POST' });
}

export function resumeQueue() {
  return request('/queue/resume', { method: 'POST' });
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
