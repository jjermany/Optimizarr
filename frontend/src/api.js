const API_BASE = import.meta.env.VITE_API_BASE ?? '';

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

export function updateLibrary(libraryId, payload) {
  return request(`/libraries/${libraryId}`, {
    method: 'PUT',
    body: JSON.stringify(payload),
  });
}

export function scanLibrary(libraryId) {
  return request(`/libraries/${libraryId}/scan`, { method: 'POST' });
}

export function fetchLibraryProfile(libraryId) {
  return request(`/libraries/${libraryId}/profile`);
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

export function abortJob(jobId) {
  return request(`/jobs/${jobId}/abort`, { method: 'POST' });
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
