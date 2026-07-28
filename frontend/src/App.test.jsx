import { describe, expect, it } from 'vitest';

import {
  ACTIVE_QUEUE_POLL_MS,
  buildQrCodeDataUrl,
  buildFallbackHistoryByEncodeJobId,
  buildUnifiedHistoryItems,
  compareHistoryItemsByOption,
  compareDownloadHistoryJobsByOption,
  downloadJobMatchesSearch,
  estimateDownloadEtaSeconds,
  extractTitleYear,
  getDisplayTitle,
  getDownloadEtaSeconds,
  libraryQueueCount,
  libraryDetailsHaveChanges,
  libraryProfileHasChanges,
  buildLibraryProfileOverview,
  settingsValuesEqual,
  buildDashboardOperationalStatus,
  buildPaginationItems,
  mergeDownloadJobsWithUpdate,
  mergeJobsWithUpdate,
  mergeLibraryScanProgress,
  normalizeDownloadJob,
  removeJobById,
  shouldShowDownloadElapsed,
  validateLibraryDraft,
} from './App';

describe('library unsaved change detection', () => {
  it('tracks editable details but ignores server-managed library fields', () => {
    const baseline = { id: 7, name: 'Movies', path: '/media/movies', scanning: false };
    expect(libraryDetailsHaveChanges({ ...baseline, scanning: true }, baseline)).toBe(false);
    expect(libraryDetailsHaveChanges({ ...baseline, path: '/media/films' }, baseline)).toBe(true);
  });

  it('detects profile edits regardless of property order', () => {
    const baseline = { codec: 'hevc', target_resolution: 1080, hdr_only: true };
    expect(libraryProfileHasChanges({ hdr_only: true, target_resolution: 1080, codec: 'hevc' }, baseline)).toBe(false);
    expect(libraryProfileHasChanges({ ...baseline, target_resolution: 720 }, baseline)).toBe(true);
  });
});

describe('library profile behavior overview', () => {
  it('rejects CRF values outside the supported 18-30 range', () => {
    const baseDraft = {
      target_resolution: 1080,
      minimum_source_resolution: 2160,
      bitrate_mode: 'vbr_crf',
      max_workers: 1,
    };

    expect(validateLibraryDraft({ ...baseDraft, crf: 17 }, true).crf).toBe('CRF must be between 18 and 30.');
    expect(validateLibraryDraft({ ...baseDraft, crf: 31 }, true).crf).toBe('CRF must be between 18 and 30.');
    expect(validateLibraryDraft({ ...baseDraft, crf: 18 }, true).crf).toBeUndefined();
    expect(validateLibraryDraft({ ...baseDraft, crf: 30 }, true).crf).toBeUndefined();
  });

  it('explains HDR tone mapping, download fallback, and schedule behavior', () => {
    const overview = buildLibraryProfileOverview({
      hdr_only: true,
      tone_map_hdr: true,
      minimum_source_resolution: 2160,
      target_resolution: 1080,
      codec: 'hevc',
      download_enabled: true,
      download_timeout_minutes: 45,
      schedule_enabled: true,
      schedule_start_hour: 22,
      schedule_end_hour: 6,
    }, { prowlarrEnabled: true, qbittorrentEnabled: true });

    expect(overview.items.map((item) => item.value)).toEqual([
      'HDR sources at 2160p or higher',
      '1080p HEVC SDR (tone-mapped)',
      'Search for a replacement first; encode after 45 minutes',
      '22:00–06:00',
    ]);
    expect(overview.warnings).toEqual([]);
  });

  it('warns that download mode falls back to encoding when integrations are disabled', () => {
    const overview = buildLibraryProfileOverview({ download_enabled: true }, {});
    expect(overview.warnings).toHaveLength(2);
    expect(overview.warnings.join(' ')).toContain('encode locally');
  });
});

describe('settings dirty-state comparison', () => {
  it('compares nested settings independent of property order', () => {
    const saved = { enabled: true, notify_on: { failed: true, complete: false }, recipients: ['a', 'b'] };
    const reordered = { recipients: ['a', 'b'], notify_on: { complete: false, failed: true }, enabled: true };
    expect(settingsValuesEqual(reordered, saved)).toBe(true);
    expect(settingsValuesEqual({ ...reordered, enabled: false }, saved)).toBe(false);
  });
});

describe('dashboard operational status', () => {
  it('prioritizes setup and pause conditions over idle state', () => {
    expect(buildDashboardOperationalStatus({ libraries: [] }).title).toBe('Add your first library');
    expect(buildDashboardOperationalStatus({ queuePaused: true, libraries: [{ enabled: true }] }).title).toBe('New jobs are paused');
    expect(buildDashboardOperationalStatus({ libraries: [{ enabled: false }] }).title).toBe('All libraries are disabled');
  });

  it('distinguishes processing, waiting, and ready states', () => {
    expect(buildDashboardOperationalStatus({ libraries: [{ enabled: true }], queueCount: 3, workingCount: 1 }).tone).toBe('working');
    expect(buildDashboardOperationalStatus({ libraries: [{ enabled: true }], queueCount: 3, workingCount: 0 }).tone).toBe('waiting');
    expect(buildDashboardOperationalStatus({ libraries: [{ enabled: true }], queueCount: 0, workingCount: 0 }).tone).toBe('idle');
  });
});

describe('compact pagination', () => {
  it('shows every page for short result sets', () => {
    expect(buildPaginationItems(3, 5)).toEqual([1, 2, 3, 4, 5]);
  });

  it('keeps the first, nearby, and last pages for long result sets', () => {
    expect(buildPaginationItems(50, 100)).toEqual([1, 'ellipsis-1-49', 49, 50, 51, 'ellipsis-51-100', 100]);
    expect(buildPaginationItems(1, 100)).toEqual([1, 2, 'ellipsis-2-100', 100]);
  });
});

describe('realtime refresh cadence', () => {
  it('keeps active queue polling fast enough for progress bars', () => {
    expect(ACTIVE_QUEUE_POLL_MS).toBeLessThanOrEqual(1000);
  });
});

describe('manual library scan progress', () => {
  it('creates a measured pinned operation and never moves backward', () => {
    const started = mergeLibraryScanProgress(null, {
      library_id: 7,
      library_name: 'Movies',
      progress_percent: 25,
      message: 'Evaluating media files (5/20)...',
    });
    const stale = mergeLibraryScanProgress(started, {
      library_id: 7,
      library_name: 'Movies',
      progress_percent: 20,
      message: 'Delayed update',
    });
    const completed = mergeLibraryScanProgress(stale, {
      library_id: 7,
      library_name: 'Movies',
      progress_percent: 100,
      message: 'Scan complete.',
    });

    expect(started).toMatchObject({
      operation: 'library_scan',
      title: 'Library Scan: Movies',
      progress: 25,
      tone: 'running',
    });
    expect(stale.progress).toBe(25);
    expect(completed).toMatchObject({ progress: 100, tone: 'success' });
  });

  it('does not replace another running pinned operation', () => {
    const cleanup = {
      operation: 'duplicate_cleanup',
      title: 'Duplicate Output Cleanup',
      progress: 40,
      tone: 'running',
    };
    expect(mergeLibraryScanProgress(cleanup, {
      library_id: 7,
      progress_percent: 50,
    })).toBe(cleanup);
  });
});

describe('buildQrCodeDataUrl', () => {
  it('returns a PNG data URL for a valid otpauth URI', async () => {
    const dataUrl = await buildQrCodeDataUrl('otpauth://totp/Optimizarr:admin?secret=JBSWY3DPEHPK3PXP&issuer=Optimizarr&algorithm=SHA1&digits=6&period=30');
    expect(dataUrl.startsWith('data:image/png;base64,')).toBe(true);
  });

  it('returns empty string when no payload is provided', async () => {
    await expect(buildQrCodeDataUrl('')).resolves.toBe('');
  });
});

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

describe('removeJobById', () => {
  it('removes the matching encode job from local state', () => {
    const jobs = [
      { id: 10, status: 'queued' },
      { id: 11, status: 'running' },
    ];

    expect(removeJobById(jobs, 10)).toEqual([{ id: 11, status: 'running' }]);
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

  it.each(['failed', 'timed_out', 'fallback_queued'])(
    'ignores stale active update after a job is already %s',
    (terminalStatus) => {
      const previousJobs = [
        { id: 7, status: terminalStatus, progress_percent: 100, completed_at: '2026-02-28T10:00:00Z' },
      ];

      const merged = mergeDownloadJobsWithUpdate(previousJobs, { id: 7, status: 'downloading', progress_percent: 55 });

      expect(merged[0].status).toBe(terminalStatus);
    },
  );

  it('allows normal active progression updates', () => {
    const previousJobs = [
      { id: 8, status: 'downloading', progress_percent: 43 },
    ];

    const merged = mergeDownloadJobsWithUpdate(previousJobs, { id: 8, status: 'moving', progress_percent: 88 });

    expect(merged[0].status).toBe('moving');
    expect(merged[0].progress_percent).toBe(88);
  });

  it('preserves SAB queue position when websocket update omits it', () => {
    const previousJobs = [
      { id: 9, status: 'queued', client_type: 'sabnzbd', client_queue_position: 3, progress_percent: 8 },
    ];

    const merged = mergeDownloadJobsWithUpdate(previousJobs, {
      id: 9,
      status: 'downloading',
      client_type: 'sabnzbd',
      client_queue_position: null,
      progress_percent: 8,
    });

    expect(merged[0].client_queue_position).toBe(3);
    expect(merged[0].status).toBe('queued');
  });
});

describe('normalizeDownloadJob', () => {
  it('keeps partial SAB backlog rows queued when queue position is behind the head', () => {
    const normalized = normalizeDownloadJob({
      id: 9,
      status: 'queued',
      client_type: 'sabnzbd',
      client_queue_position: 3,
      progress_percent: 8,
      eta_seconds: null,
      download_speed_bps: null,
    });

    expect(normalized.status).toBe('queued');
    expect(normalized.download_speed_bps).toBeNull();
  });

  it('normalizes legacy SAB downloading rows behind the head to queued', () => {
    const normalized = normalizeDownloadJob({
      id: 9,
      status: 'downloading',
      client_type: 'sabnzbd',
      client_queue_position: 3,
      progress_percent: 8,
      eta_seconds: 120,
      download_speed_bps: 1234,
    });

    expect(normalized.status).toBe('queued');
    expect(normalized.eta_seconds).toBeNull();
    expect(normalized.download_speed_bps).toBe(0);
  });
});

describe('compareDownloadHistoryJobsByOption', () => {
  it('sorts newest completed downloads first by default', () => {
    const jobs = [
      { id: 1, source_file_path: '/downloads/Older.2020.mkv', completed_at: '2026-03-01T10:00:00Z', created_at: '2026-03-01T09:00:00Z' },
      { id: 2, source_file_path: '/downloads/Newer.2021.mkv', completed_at: '2026-03-01T12:00:00Z', created_at: '2026-03-01T11:00:00Z' },
    ];

    const sorted = [...jobs].sort((a, b) => compareDownloadHistoryJobsByOption(a, b, 'completed_desc'));

    expect(sorted.map((job) => job.id)).toEqual([2, 1]);
  });

  it('sorts download history by release year when requested', () => {
    const jobs = [
      { id: 1, source_file_path: '/downloads/Older.2020.mkv', completed_at: '2026-03-01T12:00:00Z', created_at: '2026-03-01T11:00:00Z' },
      { id: 2, source_file_path: '/downloads/Newest.2024.mkv', completed_at: '2026-03-01T10:00:00Z', created_at: '2026-03-01T09:00:00Z' },
      { id: 3, source_file_path: '/downloads/Unknown.mkv', completed_at: '2026-03-01T13:00:00Z', created_at: '2026-03-01T08:00:00Z' },
    ];

    const sorted = [...jobs].sort((a, b) => compareDownloadHistoryJobsByOption(a, b, 'year_newest'));

    expect(sorted.map((job) => job.id)).toEqual([2, 1, 3]);
  });
});

describe('compareHistoryItemsByOption', () => {
  it('sorts mixed history items by completed time newest first', () => {
    const items = [
      { id: 1, _historyType: 'encode', source_path: '/media/Movies/Older.2020.mkv', completed_at: '2026-03-01T10:00:00Z', created_at: '2026-03-01T09:00:00Z' },
      { id: 2, _historyType: 'download', source_file_path: '/downloads/Newer.2021.mkv', completed_at: '2026-03-01T12:00:00Z', created_at: '2026-03-01T11:00:00Z' },
    ];

    const sorted = [...items].sort((a, b) => compareHistoryItemsByOption(a, b, 'completed_desc'));

    expect(sorted.map((item) => item.id)).toEqual([2, 1]);
  });
});

describe('buildUnifiedHistoryItems', () => {
  it('tags encode and download history entries for unified rendering', () => {
    const encodeJobs = [{ id: 1, source_path: '/media/Movies/Movie.2024.mkv' }];
    const downloadJobs = [{ id: 2, source_file_path: '/downloads/Movie.2024.mkv' }];

    expect(buildUnifiedHistoryItems(encodeJobs, downloadJobs)).toEqual([
      { ...encodeJobs[0], _historyType: 'encode' },
      { ...downloadJobs[0], _historyType: 'download' },
    ]);
  });
});

describe('buildFallbackHistoryByEncodeJobId', () => {
  it('indexes fallback_queued download rows by encode job id', () => {
    const downloadJobs = [
      { id: 1, status: 'fallback_queued', encode_job_id: 206, source_file_path: '/downloads/Euphoria.S01E07.mkv' },
      { id: 2, status: 'complete', encode_job_id: 207, source_file_path: '/downloads/Other.Movie.2024.mkv' },
      { id: 3, status: 'fallback_queued', encode_job_id: null, source_file_path: '/downloads/Unknown.mkv' },
    ];

    expect(buildFallbackHistoryByEncodeJobId(downloadJobs)).toEqual({
      206: downloadJobs[0],
    });
  });
});

describe('extractTitleYear', () => {
  it('falls back to the show folder year for TV episode paths', () => {
    expect(extractTitleYear('/media/TV/Severance (2022)/Season 01/Severance.S01E03.2160p.mkv')).toEqual({
      title: 'Severance S01E03 2160p',
      year: '2022',
    });
  });

  it('does not keep walking above the show folder', () => {
    expect(extractTitleYear('/media/TV 2026/Severance/Season 01/Severance.S01E03.2160p.mkv')).toEqual({
      title: 'Severance S01E03 2160p',
      year: null,
    });
  });

  it('preserves TV episode markers when the filename also contains a release year', () => {
    expect(
      extractTitleYear('/data/media/tv/Fallout (2024) {imdb-tt12637874}/Season 01/Fallout.S01E07.The.Radio.2024.1080p.Amazon.WEB-DL.HEVC.DDP5.1.mkv')
    ).toEqual({
      title: 'Fallout S01E07 The Radio 2024 1080p Amazon WEB-DL HEVC DDP5 1',
      year: '2024',
    });
  });
});

describe('getDisplayTitle', () => {
  it('uses show name and episode code for TV episode paths', () => {
    expect(getDisplayTitle('/media/TV/Euphoria (2019)/Season 01/Euphoria.S01E07.The.Trials.and.Tribulations.of.Trying.to.Pee.While.Depressed.2160p.mkv')).toBe('Euphoria S01E07');
  });

  it('normalizes alternate TV episode markers', () => {
    expect(getDisplayTitle('/media/TV/Severance (2022)/Season 01/Severance.1x03.2160p.WEB-DL.mkv')).toBe('Severance S01E03');
  });

  it('keeps movie titles unchanged', () => {
    expect(getDisplayTitle('/media/Movies/Shadow.of.God.2025.2160p.mkv')).toBe('Shadow of God');
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

  it('matches the visible queue count when queued encode placeholder and waiting_encode share a title', () => {
    const library = { id: 7, path: '/data/media/tv' };
    const jobs = [
      { id: 209, status: 'running', library_id: 7, source_path: '/data/media/tv/Game of Thrones (2011)/Season 01/Game of Thrones.S01E01.mkv' },
      { id: 220, status: 'queued', library_id: 7, source_path: '/downloads/Game.of.Thrones.2011.S01E02.1080p.WEB-DL.mkv' },
      { id: 221, status: 'queued', library_id: 7, source_path: '/downloads/Game.of.Thrones.2011.S01E03.1080p.WEB-DL.mkv' },
    ];
    const downloadJobs = [
      { id: 320, status: 'waiting_encode', library_id: 7, source_file_path: '/downloads/Game.of.Thrones.2011.S01E02.1080p.WEB-DL.mkv' },
      { id: 321, status: 'waiting_encode', library_id: 7, source_file_path: '/downloads/Game.of.Thrones.2011.S01E03.1080p.WEB-DL.mkv' },
    ];

    expect(libraryQueueCount(library, jobs, downloadJobs)).toBe(3);
  });
});

describe('shouldShowDownloadElapsed', () => {
  it('hides elapsed time for waiting_encode placeholders', () => {
    expect(shouldShowDownloadElapsed('waiting_encode')).toBe(false);
    expect(shouldShowDownloadElapsed('queued')).toBe(false);
  });

  it('shows elapsed time for actively progressing download states', () => {
    expect(shouldShowDownloadElapsed('downloading')).toBe(true);
    expect(shouldShowDownloadElapsed('repairing')).toBe(true);
    expect(shouldShowDownloadElapsed('unpacking')).toBe(true);
    expect(shouldShowDownloadElapsed('importing')).toBe(true);
  });
});

describe('downloadJobMatchesSearch', () => {
  it('matches terminal download history rows by title/year and library', () => {
    const downloadJob = {
      id: 209,
      status: 'complete',
      source_file_path: '/downloads/Shadow.of.God.2025.1080p.WEB-DL.x264.mkv',
      library_id: 7,
      error_message: null,
    };
    const libraryById = { 7: { name: 'movies' } };

    expect(downloadJobMatchesSearch(downloadJob, 'shadow', libraryById)).toBe(true);
    expect(downloadJobMatchesSearch(downloadJob, '2025', libraryById)).toBe(true);
    expect(downloadJobMatchesSearch(downloadJob, 'movies', libraryById)).toBe(true);
  });

  it('matches by status and error message and excludes non-matches', () => {
    const failedJob = {
      id: 77,
      status: 'timed_out',
      source_file_path: '/downloads/Some.Movie.2022.mkv',
      library_id: 1,
      error_message: 'Indexer returned no matches',
    };

    expect(downloadJobMatchesSearch(failedJob, 'timed_out', {})).toBe(true);
    expect(downloadJobMatchesSearch(failedJob, 'indexer', {})).toBe(true);
    expect(downloadJobMatchesSearch(failedJob, 'different term', {})).toBe(false);
  });
});
