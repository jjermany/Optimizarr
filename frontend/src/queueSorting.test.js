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

  it('can optionally pin active items first and prefer active encodes', () => {
    const items = buildUnifiedQueueItems({
      encodeItems,
      downloadItems,
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => item.id)).toEqual([14, 10, 9, 11, 13, 16]);
  });

  it('pins active encode rows before non-active rows deterministically', () => {
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
      'encode-5',
      'encode-2',
      'download-5',
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
      'encode-20',
      'download-31',
      'download-30',
      'download-32',
      'encode-19',
    ]);
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
});
