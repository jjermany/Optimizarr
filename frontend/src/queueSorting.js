const ENCODE_ACTIVE_STATUSES = new Set(['starting', 'running', 'preflight']);
const DOWNLOAD_ACTIVE_STATUSES = new Set(['downloading', 'importing']);

function queueItemPath(item) {
  return item._itemType === 'download' ? item.source_file_path : item.source_path;
}

function queueItemYear(item, extractTitleYear) {
  return extractTitleYear(queueItemPath(item)).year;
}

function compareById(left, right, direction = 'asc') {
  return direction === 'desc' ? right.id - left.id : left.id - right.id;
}

export function compareQueueItemsBySortOption(left, right, sortOption, extractTitleYear) {
  if (sortOption === 'newest') {
    return compareById(left, right, 'desc');
  }

  if (sortOption === 'year_newest' || sortOption === 'year_oldest') {
    const leftYear = queueItemYear(left, extractTitleYear);
    const rightYear = queueItemYear(right, extractTitleYear);
    if (leftYear && rightYear) {
      return sortOption === 'year_newest'
        ? Number(rightYear) - Number(leftYear)
        : Number(leftYear) - Number(rightYear);
    }
    if (leftYear) return -1;
    if (rightYear) return 1;
    return compareById(left, right, sortOption === 'year_newest' ? 'desc' : 'asc');
  }

  return compareById(left, right, 'asc');
}

export function isPinnedActiveQueueItem(item) {
  if (item._itemType === 'download') {
    return DOWNLOAD_ACTIVE_STATUSES.has(item.status);
  }
  return ENCODE_ACTIVE_STATUSES.has(item.status?.toLowerCase());
}

export function buildUnifiedQueueItems({
  encodeItems,
  downloadItems,
  sortOption,
  extractTitleYear,
  pinActiveFirst = false,
}) {
  const merged = [
    ...encodeItems.map((item) => ({ ...item, _itemType: 'encode' })),
    ...downloadItems.map((item) => ({ ...item, _itemType: 'download' })),
  ];

  return merged.sort((left, right) => {
    if (pinActiveFirst) {
      const leftPinned = isPinnedActiveQueueItem(left);
      const rightPinned = isPinnedActiveQueueItem(right);
      if (leftPinned !== rightPinned) return leftPinned ? -1 : 1;
    }

    return compareQueueItemsBySortOption(left, right, sortOption, extractTitleYear);
  });
}

