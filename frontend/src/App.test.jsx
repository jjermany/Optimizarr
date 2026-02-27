import { describe, expect, it } from 'vitest';

import { mergeJobsWithUpdate } from './App';

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
