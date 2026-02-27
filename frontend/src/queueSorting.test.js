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

  it('can optionally pin active items first without changing secondary order', () => {
    const items = buildUnifiedQueueItems({
      encodeItems,
      downloadItems,
      sortOption: 'default',
      extractTitleYear,
      pinActiveFirst: true,
    });

    expect(items.map((item) => item.id)).toEqual([10, 14, 9, 11, 13, 16]);
  });
});
