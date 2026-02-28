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
});
