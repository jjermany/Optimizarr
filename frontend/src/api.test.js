import { afterEach, describe, expect, it, vi } from 'vitest';

import { clearLogs, startJob } from './api';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('api request helpers', () => {
  it('calls the clear logs endpoint with DELETE', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ deleted_logs: 2 }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(clearLogs()).resolves.toEqual({ deleted_logs: 2 });
    expect(fetchMock).toHaveBeenCalledWith('/api/logs', expect.objectContaining({ method: 'DELETE' }));
  });
});

describe('api request error handling', () => {
  it('surfaces FastAPI JSON detail for failed requests', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Maximum workers already running' }), {
        status: 409,
        headers: { 'Content-Type': 'application/json' },
      }),
    );

    await expect(startJob(7)).rejects.toMatchObject({
      status: 409,
      message: 'Maximum workers already running',
    });
  });

  it('falls back to response text when detail is missing', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('backend unavailable', {
        status: 503,
        headers: { 'Content-Type': 'text/plain' },
      }),
    );

    await expect(startJob(99)).rejects.toMatchObject({
      status: 503,
      message: 'backend unavailable',
    });
  });
});
