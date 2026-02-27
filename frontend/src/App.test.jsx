import { describe, expect, it } from 'vitest';

import { estimateDownloadEtaSeconds, getDownloadEtaSeconds, mergeJobsWithUpdate } from './App';

describe('mergeJobsWithUpdate', () => {
  it('resets to page one when an existing queued job transitions to running', () => {
    const previousJobs = [
      { id: 100, status: 'queued', source_path: '/a.mkv' },
      { id: 101, status: 'queued', source_path: '/b.mkv' },
    ];

    const merged = mergeJobsWithUpdate(previousJobs, { id: 100, status: 'running' });

    expect(merged.resetToFirstPage).toBe(true);
    expect(merged.jobs.find((job) => job.id === 100)?.status).toBe('running');
  });

  it('does not reset page when job was already active', () => {
    const previousJobs = [{ id: 100, status: 'running', source_path: '/a.mkv' }];

    const merged = mergeJobsWithUpdate(previousJobs, { id: 100, status: 'running', fps: 12.5 });

    expect(merged.resetToFirstPage).toBe(false);
    expect(merged.jobs[0].fps).toBe(12.5);
  });
});

describe('estimateDownloadEtaSeconds', () => {
  it('estimates remaining time from elapsed and progress', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 300_000).toISOString();

    expect(estimateDownloadEtaSeconds({ progress_percent: 25, created_at: createdAt }, nowMs)).toBe(900);
  });

  it('returns null when progress is zero', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 120_000).toISOString();

    expect(estimateDownloadEtaSeconds({ progress_percent: 0, created_at: createdAt }, nowMs)).toBeNull();
  });

  it('returns zero when progress is complete', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 120_000).toISOString();

    expect(estimateDownloadEtaSeconds({ progress_percent: 100, created_at: createdAt }, nowMs)).toBe(0);
  });

  it('returns null when created_at is missing', () => {
    expect(estimateDownloadEtaSeconds({ progress_percent: 50 }, Date.UTC(2026, 0, 1, 0, 10, 0))).toBeNull();
  });
});

describe('getDownloadEtaSeconds', () => {
  it('prefers backend-reported eta_seconds when present', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 300_000).toISOString();

    expect(getDownloadEtaSeconds({ eta_seconds: 42, progress_percent: 25, created_at: createdAt }, nowMs)).toBe(42);
  });

  it('falls back to local estimate when eta_seconds is missing', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 300_000).toISOString();

    expect(getDownloadEtaSeconds({ progress_percent: 25, created_at: createdAt }, nowMs)).toBe(900);
  });
});
