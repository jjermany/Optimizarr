const ENCODE_WORKING_STATUSES = new Set(['starting', 'running', 'preflight', 'aborting']);
const ENCODE_WAITING_STATUSES = new Set(['queued', 'paused', 'paused_schedule']);
const ENCODE_ACTIVE_STATUSES = new Set([...ENCODE_WORKING_STATUSES, ...ENCODE_WAITING_STATUSES]);
const DOWNLOAD_WORKING_STATUSES = new Set(['searching', 'downloading', 'repairing', 'unpacking', 'moving', 'stalled', 'importing']);
const DOWNLOAD_WAITING_STATUSES = new Set(['pending', 'queued', 'paused']);
const DOWNLOAD_ACTIVE_STATUSES = new Set([...DOWNLOAD_WORKING_STATUSES, ...DOWNLOAD_WAITING_STATUSES]);
const DOWNLOAD_WAITING_ENCODE_STATUSES = new Set(['waiting_encode']);

function queueItemPath(item) {
  return item._itemType === 'download' ? item.source_file_path : item.source_path;
}

function normalizedPath(pathValue) {
  return String(pathValue || '').trim().toLowerCase();
}

function normalizedTitle(titleValue) {
  const baseName = String(titleValue || '').split('/').pop() || '';
  return baseName
    .replace(/[^a-zA-Z0-9]+/g, ' ')
    .trim()
    .replace(/\s+/g, ' ')
    .toLowerCase();
}

function parseTitleYearFromPath(pathValue) {
  const fileName = String(pathValue || '').split('/').pop() || '';
  const stem = fileName.replace(/\.[^.]+$/, '');
  const spaced = stem.replace(/[._]/g, ' ').trim();
  const parenMatch = spaced.match(/\(((19|20)\d{2})\)/);
  if (parenMatch) {
    const title = spaced.slice(0, spaced.indexOf(parenMatch[0])).replace(/\s+$/, '').trim();
    return { title: title || spaced, year: parenMatch[1] };
  }
  const yearMatch = spaced.match(/\b((19|20)\d{2})\b/);
  if (yearMatch) {
    const yearIdx = spaced.indexOf(yearMatch[0]);
    const title = spaced.slice(0, yearIdx).replace(/[\s\-]+$/, '').trim();
    return { title: title || spaced, year: yearMatch[1] };
  }
  return { title: spaced, year: null };
}

function titleYearKeyForPath(pathValue, extractTitleYear) {
  const parsed = parseTitleYearFromPath(pathValue);
  const fallback = extractTitleYear(pathValue);
  const title = parsed.title || fallback.title;
  const year = parsed.year || fallback.year;
  const normalizedTitleValue = normalizedTitle(title);
  const normalizedYear = String(year || '').trim();
  if (!normalizedTitleValue) return '';
  return `${normalizedTitleValue}::${normalizedYear}`;
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

function compareByCreatedAt(left, right, direction = 'asc') {
  const leftTs = Date.parse(left.created_at || '') || 0;
  const rightTs = Date.parse(right.created_at || '') || 0;
  const tsDelta = direction === 'desc' ? rightTs - leftTs : leftTs - rightTs;
  if (tsDelta !== 0) return tsDelta;
  return compareById(left, right, direction);
}

function clientQueuePosition(item) {
  const rawPosition = item.client_queue_position;
  if (rawPosition === null || rawPosition === undefined || rawPosition === '') return null;
  const position = Number(rawPosition);
  return Number.isFinite(position) && position >= 0 ? position : null;
}

function encodeStatusRank(statusValue) {
  const status = String(statusValue || '').toLowerCase();
  if (ENCODE_WORKING_STATUSES.has(status)) return 0;
  if (status === 'paused' || status === 'paused_schedule') return 1;
  if (status === 'queued') return 2;
  return 3;
}

function queuePinRank(item) {
  const status = String(item.status || '').toLowerCase();
  if (item._itemType === 'download' && DOWNLOAD_WORKING_STATUSES.has(status)) return 0;
  if (item._itemType === 'encode' && ENCODE_WORKING_STATUSES.has(status)) return 1;
  if (item._itemType === 'download' && DOWNLOAD_WAITING_STATUSES.has(status)) return 2;
  if (item._itemType === 'download' && DOWNLOAD_WAITING_ENCODE_STATUSES.has(status)) return 3;
  if (item._itemType === 'encode' && ENCODE_WAITING_STATUSES.has(status)) return 4;
  return 4;
}

function comparePinnedDownloadItems(left, right) {
  const leftPosition = clientQueuePosition(left);
  const rightPosition = clientQueuePosition(right);
  if (leftPosition !== null && rightPosition !== null && leftPosition !== rightPosition) {
    return leftPosition - rightPosition;
  }
  if (leftPosition !== null && rightPosition === null) return -1;
  if (rightPosition !== null && leftPosition === null) return 1;

  const leftTs = Date.parse(left.created_at || '') || 0;
  const rightTs = Date.parse(right.created_at || '') || 0;
  if (leftTs !== rightTs) return leftTs - rightTs;
  return compareById(left, right, 'asc');
}

export function compareQueueItemsBySortOption(left, right, sortOption, extractTitleYear) {
  if (sortOption === 'newest') {
    return compareByCreatedAt(left, right, 'desc');
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
    return compareByCreatedAt(left, right, sortOption === 'year_newest' ? 'desc' : 'asc');
  }

  return compareByCreatedAt(left, right, 'asc');
}

export function buildUnifiedQueueItems({
  encodeItems,
  downloadItems,
  sortOption,
  extractTitleYear,
  pinActiveFirst = false,
  dedupeSourcePaths = undefined,
}) {
  const activeDownloadSources = new Set(
    downloadItems
      .map((item) => String(item.source_file_path || '').trim())
      .filter(Boolean),
  );
  const dedupeSources = new Set(activeDownloadSources);
  for (const sourcePath of dedupeSourcePaths ?? []) {
    const normalized = String(sourcePath || '').trim();
    if (normalized) dedupeSources.add(normalized);
  }

  const activeEncodePaths = new Set();
  const activeEncodeTitleYearKeys = new Set();
  for (const item of encodeItems) {
    if (!ENCODE_WORKING_STATUSES.has(String(item.status || '').toLowerCase())) continue;
    const sourcePath = String(item.source_path || '').trim();
    if (sourcePath) activeEncodePaths.add(normalizedPath(sourcePath));
    const titleYearKey = titleYearKeyForPath(sourcePath, extractTitleYear);
    if (titleYearKey) activeEncodeTitleYearKeys.add(titleYearKey);
  }

  const preferredEncodeByPath = new Map();
  for (const item of encodeItems) {
    const sourcePath = String(item.source_path || '').trim();
    if (!sourcePath) continue;
    const normalizedSourcePath = normalizedPath(sourcePath);
    const existing = preferredEncodeByPath.get(normalizedSourcePath);
    if (!existing) {
      preferredEncodeByPath.set(normalizedSourcePath, item);
      continue;
    }

    const statusDelta = encodeStatusRank(item.status) - encodeStatusRank(existing.status);
    if (statusDelta < 0) {
      preferredEncodeByPath.set(normalizedSourcePath, item);
      continue;
    }
    if (statusDelta > 0) continue;

    const createdDelta = compareByCreatedAt(item, existing, 'desc');
    if (createdDelta < 0) {
      preferredEncodeByPath.set(normalizedSourcePath, item);
      continue;
    }

    if (createdDelta === 0 && item.id > existing.id) {
      preferredEncodeByPath.set(normalizedSourcePath, item);
    }
  }

  const dedupedEncodeItems = encodeItems.filter((item) => {
    const sourcePath = String(item.source_path || '').trim();
    if (sourcePath) {
      const preferred = preferredEncodeByPath.get(normalizedPath(sourcePath));
      if (preferred && preferred.id !== item.id) return false;
    }
    if (!sourcePath || !dedupeSources.has(sourcePath)) return true;
    // Keep truly active encode rows visible even if a download row exists.
    return ENCODE_WORKING_STATUSES.has(String(item.status || '').toLowerCase());
  });
  const dedupedDownloadItems = downloadItems.filter((item) => {
    if (!DOWNLOAD_WAITING_ENCODE_STATUSES.has(String(item.status || '').toLowerCase())) return true;
    const sourcePath = String(item.source_file_path || '').trim();
    if (sourcePath && activeEncodePaths.has(normalizedPath(sourcePath))) return false;
    const titleYearKey = titleYearKeyForPath(sourcePath, extractTitleYear);
    if (titleYearKey && activeEncodeTitleYearKeys.has(titleYearKey)) return false;
    return true;
  });

  const merged = [
    ...dedupedEncodeItems.map((item) => ({ ...item, _itemType: 'encode' })),
    ...dedupedDownloadItems.map((item) => ({ ...item, _itemType: 'download' })),
  ];

  return merged.sort((left, right) => {
    if (pinActiveFirst) {
      const leftPinRank = queuePinRank(left);
      const rightPinRank = queuePinRank(right);
      const pinRankDelta = leftPinRank - rightPinRank;
      if (pinRankDelta !== 0) return pinRankDelta;
      // Download rows within the same work/wait bucket stay stable FIFO so
      // status and progress refreshes do not reshuffle the visible queue.
      if ([0, 2, 3].includes(leftPinRank) && leftPinRank === rightPinRank) {
        if (left._itemType === 'download' && right._itemType === 'download') {
          return comparePinnedDownloadItems(left, right);
        }
        return compareByCreatedAt(left, right, 'asc');
      }
      if ([1, 4].includes(leftPinRank) && leftPinRank === rightPinRank) {
        return compareByCreatedAt(left, right, 'asc');
      }
    }

    return compareQueueItemsBySortOption(left, right, sortOption, extractTitleYear);
  });
}
