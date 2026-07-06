import { describe, expect, it } from 'vitest';

import { buildUnifiedQueueItems } from './queueSorting';

function extractTitleYear(filePath) {
  const match = (filePath || '').match(/(19|20)\d{2}/);
  return { title: filePath || '', year: match ? match[0] : null };
}

const encodeItems = [
  { id: 11, status: 'queued', source_path: '/media/Encode.Movie.2019.mkv' },
  { id: 14, status: 'running', source_path: '/media/Encode.Show.2022.mkv' },
  { id: 9, status: 'queued', source_path: '/media/Encode.Classic.1998.mkv' },
];

const downloadItems = [
  { id: 13, status: 'pending', source_file_path: '/downloads/Download.New.2024.mkv' },
  { id: 10, status: 'importing', source_file_path: '/downloads/Download.Old.2015.mkv' },
  { id: 16, status: 'pending', source_file_path: '/downloads/Download.Unknown.mkv' },
];

describe('buildUnifiedQueueItems', () => {
  it.each([
    ['default', [9, 10, 11, 13, 14, 16]],
    ['newest', [16, 14, 13, 11, 10, 9]],
    ['year_newest', [13, 14, 11, 10, 9, 16]],
    ['year_oldest', [9, 10, 11, 14, 13, 16]],
  ])('sorts mixed encode/download rows for %s', (sortOption, expectedIds) => {
    const items = buildUnifiedQueueItems({
      encodeItems,
      downloadItems,
      sortOption,
      extractTitleYear,
    });

    expect(items.map((item) => item.id)).toEqual(expectedIds);
  });

  it('uses created_at rather than id for date-added ordering', () => {
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 50, status: 'queued', source_path: '/media/Older.Created.mkv', created_at: '2026-03-01T09:00:00Z' },
        { id: 1, status: 'queued', source_path: '/media/Newer.Created.mkv', created_at: '2026-03-01T10:00:00Z' },
      ],
      downloadItems: [],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: false,
    });

    expect(items.map((item) => item.id)).toEqual([50, 1]);
  });

  it('uses created_at rather than id for newest ordering', () => {
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 50, status: 'queued', source_path: '/media/Older.Created.mkv', created_at: '2026-03-01T09:00:00Z' },
        { id: 1, status: 'queued', source_path: '/media/Newer.Created.mkv', created_at: '2026-03-01T10:00:00Z' },
      ],
      downloadItems: [],
      sortOption: 'newest',
      extractTitleYear,
      pinActiveFirst: false,
    });

    expect(items.map((item) => item.id)).toEqual([1, 50]);
  });

  it('can optionally pin active downloads first before active encodes', () => {
    const items = buildUnifiedQueueItems({
      encodeItems,
      downloadItems,
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => item.id)).toEqual([10, 13, 16, 14, 9, 11]);
  });

  it('pins active download rows before active encode rows deterministically', () => {
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 5, status: 'running', source_path: '/media/Active.Encode.A.2020.mkv' },
        { id: 2, status: 'queued', source_path: '/media/Queued.Encode.2022.mkv' },
      ],
      downloadItems: [
        { id: 5, status: 'pending', source_file_path: '/downloads/Pending.Download.2021.mkv' },
      ],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'download-5',
      'encode-5',
      'encode-2',
    ]);
  });

  it('pins waiting_encode downloads below active pinned rows in FIFO order', () => {
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 20, status: 'running', source_path: '/media/Active.Encode.2020.mkv' },
        { id: 19, status: 'queued', source_path: '/media/Queued.Encode.2021.mkv' },
      ],
      downloadItems: [
        { id: 31, status: 'downloading', source_file_path: '/downloads/Active.Download.2024.mkv', created_at: '2026-03-01T10:00:00Z' },
        { id: 30, status: 'waiting_encode', source_file_path: '/downloads/Wait.Encode.Older.2022.mkv', created_at: '2026-03-01T09:00:00Z' },
        { id: 32, status: 'waiting_encode', source_file_path: '/downloads/Wait.Encode.Newer.2023.mkv', created_at: '2026-03-01T11:00:00Z' },
      ],
      sortOption: 'newest',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'download-31',
      'encode-20',
      'download-30',
      'download-32',
      'encode-19',
    ]);
  });

  it('keeps download rows FIFO even when statuses differ', () => {
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 19, status: 'queued', source_path: '/media/Queued.Encode.2021.mkv', created_at: '2026-03-01T12:00:00Z' },
      ],
      downloadItems: [
        { id: 41, status: 'queued', source_file_path: '/downloads/Queued.Newer.2026.mkv', created_at: '2026-03-01T11:00:00Z' },
        { id: 40, status: 'queued', source_file_path: '/downloads/Queued.Older.2025.mkv', created_at: '2026-03-01T10:00:00Z' },
        { id: 42, status: 'downloading', source_file_path: '/downloads/Active.Download.2024.mkv', created_at: '2026-03-01T09:00:00Z' },
        { id: 43, status: 'importing', source_file_path: '/downloads/Importing.Download.2023.mkv', created_at: '2026-03-01T08:00:00Z' },
        { id: 44, status: 'unpacking', source_file_path: '/downloads/Unpacking.Download.2023.mkv', created_at: '2026-03-01T07:00:00Z' },
        { id: 45, status: 'repairing', source_file_path: '/downloads/Repairing.Download.2023.mkv', created_at: '2026-03-01T06:00:00Z' },
      ],
      sortOption: 'newest',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'download-45',
      'download-44',
      'download-43',
      'download-42',
      'download-40',
      'download-41',
      'encode-19',
    ]);
  });

  it('orders active SAB downloads by client queue position when available', () => {
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 10, status: 'running', source_path: '/media/Encode.Active.2026.mkv', created_at: '2026-03-01T07:00:00Z' },
        { id: 11, status: 'queued', source_path: '/media/Encode.Queued.2026.mkv', created_at: '2026-03-01T08:00:00Z' },
      ],
      downloadItems: [
        { id: 287, status: 'downloading', source_file_path: '/downloads/Fantastic.Four.2025.mkv', client_type: 'sabnzbd', client_queue_position: 1, created_at: '2026-03-01T11:00:00Z' },
        { id: 294, status: 'downloading', source_file_path: '/downloads/Louis.Theroux.2026.mkv', client_type: 'sabnzbd', client_queue_position: 0, created_at: '2026-03-01T12:00:00Z' },
        { id: 288, status: 'queued', source_file_path: '/downloads/Paddington.in.Peru.2024.mkv', client_type: 'sabnzbd', client_queue_position: 2, created_at: '2026-03-01T09:00:00Z' },
      ],
      sortOption: 'newest',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'download-294',
      'download-287',
      'download-288',
      'encode-10',
      'encode-11',
    ]);
  });

  it('does not move a download row when status progresses through the client lifecycle', () => {
    const before = buildUnifiedQueueItems({
      encodeItems: [],
      downloadItems: [
        { id: 40, status: 'queued', source_file_path: '/downloads/Queued.Older.2025.mkv', created_at: '2026-03-01T10:00:00Z' },
        { id: 41, status: 'queued', source_file_path: '/downloads/Queued.Newer.2026.mkv', created_at: '2026-03-01T11:00:00Z' },
      ],
      sortOption: 'newest',
      extractTitleYear,
      pinActiveFirst: true,
    });
    const after = buildUnifiedQueueItems({
      encodeItems: [],
      downloadItems: [
        { id: 40, status: 'downloading', source_file_path: '/downloads/Queued.Older.2025.mkv', created_at: '2026-03-01T10:00:00Z' },
        { id: 41, status: 'queued', source_file_path: '/downloads/Queued.Newer.2026.mkv', created_at: '2026-03-01T11:00:00Z' },
      ],
      sortOption: 'newest',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(before.map((item) => item.id)).toEqual([40, 41]);
    expect(after.map((item) => item.id)).toEqual([40, 41]);
  });

  it('hides queued encode placeholder rows when an active download row exists for the same source', () => {
    const source = '/media/Clown.in.a.Cornfield.2025.mkv';
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 178, status: 'queued', source_path: source },
      ],
      downloadItems: [
        { id: 16, status: 'downloading', source_file_path: source },
      ],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'download-16',
    ]);
  });

  it('hides queued encode placeholder rows for extra dedupe source paths', () => {
    const source = '/media/Transient.Queue.Placeholder.2026.mkv';
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 210, status: 'queued', source_path: source },
      ],
      downloadItems: [],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
      dedupeSourcePaths: [source],
    });

    expect(items).toEqual([]);
  });

  it('hides waiting_encode download rows when an active encode exists for the same source path', () => {
    const source = '/media/Shadow.of.God.2025.2160p.mkv';
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 202, status: 'running', source_path: source },
      ],
      downloadItems: [
        { id: 209, status: 'waiting_encode', source_file_path: source },
      ],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'encode-202',
    ]);
  });

  it('hides waiting_encode download rows when an active encode matches by title/year', () => {
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 202, status: 'running', source_path: '/movies/Shadow of God (2025).mkv' },
      ],
      downloadItems: [
        { id: 209, status: 'waiting_encode', source_file_path: '/downloads/Shadow.of.God.2025.1080p.WEB-DL.x264.mkv' },
      ],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'encode-202',
    ]);
  });

  it('keeps an aborting encode visible and hides the waiting_encode placeholder', () => {
    const source = '/movies/War Machine (2026).mkv';
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 210, status: 'aborting', source_path: source, created_at: '2026-03-06T10:00:00Z' },
      ],
      downloadItems: [
        { id: 257, status: 'waiting_encode', source_file_path: source, created_at: '2026-03-06T09:59:00Z' },
      ],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'encode-210',
    ]);
  });

  it('keeps only the preferred encode row when duplicate source paths exist', () => {
    const source = '/movies/War Machine (2026).mkv';
    const items = buildUnifiedQueueItems({
      encodeItems: [
        { id: 210, status: 'running', source_path: source, created_at: '2026-03-06T10:00:00Z' },
        { id: 257, status: 'queued', source_path: source, created_at: '2026-03-06T10:05:00Z' },
      ],
      downloadItems: [],
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => `${item._itemType}-${item.id}`)).toEqual([
      'encode-210',
    ]);
  });
});
