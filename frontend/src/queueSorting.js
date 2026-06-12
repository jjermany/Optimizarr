const ENCODE_ACTIVE_STATUSES = new Set(['starting', 'running', 'preflight', 'aborting', 'paused', 'paused_schedule']);
const DOWNLOAD_ACTIVE_STATUSES = new Set(['downloading', 'moving', 'importing']);
const DOWNLOAD_CLIENT_QUEUED_STATUSES = new Set(['queued']);
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

function encodeStatusRank(statusValue) {
  const status = String(statusValue || '').toLowerCase();
  if (ENCODE_ACTIVE_STATUSES.has(status)) return 0;
  if (status === 'paused') return 1;
  if (status === 'queued') return 2;
  return 3;
}

function queuePinRank(item) {
  if (item._itemType === 'encode' && ENCODE_ACTIVE_STATUSES.has(item.status?.toLowerCase())) return 0;
  if (item._itemType === 'download' && DOWNLOAD_ACTIVE_STATUSES.has(item.status)) return 1;
  if (item._itemType === 'download' && DOWNLOAD_CLIENT_QUEUED_STATUSES.has(item.status)) return 2;
  if (item._itemType === 'download' && DOWNLOAD_WAITING_ENCODE_STATUSES.has(item.status)) return 3;
  return 4;
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

export function isPinnedActiveQueueItem(item) {
  return queuePinRank(item) < 2;
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
    if (!ENCODE_ACTIVE_STATUSES.has(String(item.status || '').toLowerCase())) continue;
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
    return ENCODE_ACTIVE_STATUSES.has(String(item.status || '').toLowerCase());
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
      const pinRankDelta = queuePinRank(left) - queuePinRank(right);
      if (pinRankDelta !== 0) return pinRankDelta;
      // Queued/waiting rows stay stable FIFO so the visible queue does not jump.
      if (queuePinRank(left) >= 2 && queuePinRank(left) === queuePinRank(right)) {
        const leftTs = Date.parse(left.created_at || '') || 0;
        const rightTs = Date.parse(right.created_at || '') || 0;
        if (leftTs !== rightTs) return leftTs - rightTs;
        return left.id - right.id;
      }
    }

    return compareQueueItemsBySortOption(left, right, sortOption, extractTitleYear);
  });
}
