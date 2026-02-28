import { describe, expect, it } from 'vitest';

import { estimateDownloadEtaSeconds, getDownloadEtaSeconds, libraryQueueCount, mergeDownloadJobsWithUpdate, mergeJobsWithUpdate } from './App';

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

  it('returns null when progress is complete but status is not terminal', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 120_000).toISOString();

    expect(estimateDownloadEtaSeconds({ progress_percent: 100, created_at: createdAt }, nowMs)).toBeNull();
  });

  it('returns null when created_at is missing', () => {
    expect(estimateDownloadEtaSeconds({ progress_percent: 50 }, Date.UTC(2026, 0, 1, 0, 10, 0))).toBeNull();
  });
});

describe('mergeDownloadJobsWithUpdate', () => {
  it('ignores stale active update after a job is already complete', () => {
    const previousJobs = [
      { id: 7, status: 'complete', progress_percent: 100, completed_at: '2026-02-28T10:00:00Z' },
    ];

    const merged = mergeDownloadJobsWithUpdate(previousJobs, { id: 7, status: 'importing', progress_percent: 100 });

    expect(merged[0].status).toBe('complete');
  });

  it('allows normal active progression updates', () => {
    const previousJobs = [
      { id: 8, status: 'downloading', progress_percent: 43 },
    ];

    const merged = mergeDownloadJobsWithUpdate(previousJobs, { id: 8, status: 'moving', progress_percent: 88 });

    expect(merged[0].status).toBe('moving');
    expect(merged[0].progress_percent).toBe(88);
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

  it('does not report done for non-terminal downloads with eta_seconds=0', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 300_000).toISOString();

    expect(getDownloadEtaSeconds({ status: 'downloading', eta_seconds: 0, progress_percent: 100, created_at: createdAt }, nowMs)).toBeNull();
  });

  it('reports done for complete downloads with eta_seconds=0', () => {
    const nowMs = Date.UTC(2026, 0, 1, 0, 10, 0);
    const createdAt = new Date(nowMs - 300_000).toISOString();

    expect(getDownloadEtaSeconds({ status: 'complete', eta_seconds: 0, progress_percent: 100, created_at: createdAt }, nowMs)).toBe(0);
  });
});

describe('libraryQueueCount', () => {
  it('does not double-count waiting_encode when active encode matches same path', () => {
    const library = { id: 7, path: '/data/media/movies' };
    const jobs = [
      { id: 202, status: 'running', library_id: 7, source_path: '/data/media/movies/Shadow.of.God.2025.mkv' },
    ];
    const downloadJobs = [
      { id: 209, status: 'waiting_encode', library_id: 7, source_file_path: '/data/media/movies/Shadow.of.God.2025.mkv' },
    ];

    expect(libraryQueueCount(library, jobs, downloadJobs)).toBe(1);
  });

  it('does not double-count waiting_encode when active encode matches title/year', () => {
    const library = { id: 7, path: '/data/media/movies' };
    const jobs = [
      { id: 202, status: 'running', library_id: 7, source_path: '/data/media/movies/Shadow of God (2025).mkv' },
    ];
    const downloadJobs = [
      { id: 209, status: 'waiting_encode', library_id: 7, source_file_path: '/downloads/Shadow.of.God.2025.1080p.WEB-DL.x264.mkv' },
    ];

    expect(libraryQueueCount(library, jobs, downloadJobs)).toBe(1);
  });

  it('still counts waiting_encode when no active encode exists', () => {
    const library = { id: 7, path: '/data/media/movies' };
    const jobs = [
      { id: 300, status: 'queued', library_id: 7, source_path: '/data/media/movies/Other.Movie.2022.mkv' },
    ];
    const downloadJobs = [
      { id: 209, status: 'waiting_encode', library_id: 7, source_file_path: '/downloads/Shadow.of.God.2025.1080p.WEB-DL.x264.mkv' },
    ];

    expect(libraryQueueCount(library, jobs, downloadJobs)).toBe(2);
  });
});
