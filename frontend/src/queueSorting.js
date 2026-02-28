const ENCODE_ACTIVE_STATUSES = new Set(['starting', 'running', 'preflight']);
const DOWNLOAD_ACTIVE_STATUSES = new Set(['downloading', 'importing']);

function queueItemPath(item) {
  return item._itemType === 'download' ? item.source_file_path : item.source_path;
}

function queueItemYear(item, extractTitleYear) {
  return extractTitleYear(queueItemPath(item)).year;
}

function compareById(left, right, direction = 'asc') {
  const idDelta = direction === 'desc' ? right.id - left.id : left.id - right.id;
  if (idDelta !== 0) return idDelta;
  if (left._itemType !== right._itemType) return left._itemType.localeCompare(right._itemType);
  return queueItemPath(left).localeCompare(queueItemPath(right));
}

function queuePinRank(item) {
  if (item._itemType === 'encode' && ENCODE_ACTIVE_STATUSES.has(item.status?.toLowerCase())) return 0;
  if (item._itemType === 'download' && DOWNLOAD_ACTIVE_STATUSES.has(item.status)) return 1;
  return 2;
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
  return queuePinRank(item) < 2;
}

export function buildUnifiedQueueItems({
  encodeItems,
  downloadItems,
  sortOption,
  extractTitleYear,
  pinActiveFirst = false,
}) {
  const activeDownloadSources = new Set(
    downloadItems
      .map((item) => String(item.source_file_path || '').trim())
      .filter(Boolean),
  );
  const dedupedEncodeItems = encodeItems.filter((item) => {
    const sourcePath = String(item.source_path || '').trim();
    if (!sourcePath || !activeDownloadSources.has(sourcePath)) return true;
    // Keep truly active encode rows visible even if a download row exists.
    return ENCODE_ACTIVE_STATUSES.has(String(item.status || '').toLowerCase());
  });

  const merged = [
    ...dedupedEncodeItems.map((item) => ({ ...item, _itemType: 'encode' })),
    ...downloadItems.map((item) => ({ ...item, _itemType: 'download' })),
  ];

  return merged.sort((left, right) => {
    if (pinActiveFirst) {
      const pinRankDelta = queuePinRank(left) - queuePinRank(right);
      if (pinRankDelta !== 0) return pinRankDelta;
    }

    return compareQueueItemsBySortOption(left, right, sortOption, extractTitleYear);
  });
}
