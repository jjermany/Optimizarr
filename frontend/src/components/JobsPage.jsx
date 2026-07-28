import { memo, useEffect, useMemo, useState } from 'react';

import {
  ACTIVE_DL_STATUSES,
  ACTIVE_STATUSES,
  HISTORY_PAGE_SIZE,
  JOBS_PAGE_SIZE,
  JOBS_UI_PREFS_KEY,
  QUEUE_DEDUPE_DL_STATUSES,
  TERMINAL_DL_STATUSES,
  TERMINAL_STATUSES,
  buildFallbackHistoryByEncodeJobId,
  buildUnifiedHistoryItems,
  buildPaginationItems,
  compareActiveJobsDefault,
  compareHistoryItemsByOption,
  downloadJobMatchesSearch,
  extractTitleYear,
  formatDownloadClient,
  formatDownloadRetry,
  formatDownloadSpeed,
  formatElapsed,
  formatEta,
  formatHdrIndicator,
  formatHistoryCompletedAt,
  formatResolution,
  getDisplayTitle,
  getDownloadEtaSeconds,
  getElapsedSeconds,
  loadJobsUiPrefs,
  progressFromJob,
  shouldShowDownloadElapsed,
  sortJobsByOption,
} from '../lib/appUtils';
import { buildUnifiedQueueItems } from '../queueSorting';
import {
  Btn,
  FallbackIndicator,
  HistoryTypeBadge,
  MobileActionMenu,
  Modal,
  SelectInput,
  TextInput,
} from './ui';

// ── Queue rows ───────────────────────────────────────────────────────────────
// Each row is memoized so per-job websocket updates re-render only the row
// whose job object changed, and the 1s elapsed-time tick only touches
// download rows (encode rows never receive nowMs).

function DownloadReviewActions({ dj, actionPending, onReview }) {
  const candidates = Array.isArray(dj.review_data?.candidates) ? dj.review_data.candidates : [];
  const [selectedPath, setSelectedPath] = useState(candidates[0]?.path ?? '');
  const reasons = Array.isArray(dj.review_data?.reasons) ? dj.review_data.reasons : [];
  return (
    <div className="mt-3 rounded-xl border border-amber-500/30 bg-amber-950/20 p-3">
      <p className="text-xs font-semibold uppercase tracking-wide text-amber-300">
        {String(dj.review_data?.confidence ?? 'low')} confidence — approval required
      </p>
      {reasons.length > 0 && (
        <ul className="mt-2 list-disc space-y-1 pl-4 text-xs text-amber-100/80">
          {reasons.map((reason) => <li key={reason}>{reason}</li>)}
        </ul>
      )}
      {candidates.length > 0 && (
        <select
          className="mt-2 w-full rounded-lg border border-slate-600 bg-slate-900 px-2 py-1.5 text-xs text-slate-200"
          value={selectedPath}
          onChange={(event) => setSelectedPath(event.target.value)}
        >
          {candidates.map((candidate) => (
            <option key={candidate.path} value={candidate.path}>
              {candidate.name} · {candidate.height ? `${candidate.height}p` : 'resolution unknown'} · {candidate.codec ?? 'codec unknown'} · {candidate.duration_seconds ? formatElapsed(candidate.duration_seconds) : 'duration unknown'} · {candidate.size_bytes ? `${(candidate.size_bytes / 1073741824).toFixed(2)} GB` : 'size unknown'}
            </option>
          ))}
        </select>
      )}
      <div className="mt-2 flex flex-wrap gap-1.5">
        <Btn size="sm" variant="warning" disabled={Boolean(actionPending) || !selectedPath} onClick={() => onReview('import', dj.id, selectedPath)}>
          {actionPending === 'review_import' ? 'Importing…' : 'Import Selected'}
        </Btn>
        <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onReview('retry', dj.id)}>
          {actionPending === 'review_retry' ? 'Working…' : 'Reject & Retry'}
        </Btn>
        <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onReview('fallback', dj.id)}>
          {actionPending === 'review_fallback' ? 'Working…' : 'Encode Source'}
        </Btn>
      </div>
    </div>
  );
}

const QueueDownloadCard = memo(function QueueDownloadCard({ dj, libName, actionPending, nowMs, onCancel, onRetry, onDelete, onReview }) {
  const { year } = extractTitleYear(dj.source_file_path);
  const title = getDisplayTitle(dj.source_file_path);
  const elapsedStart = dj.download_started_at ?? dj.created_at;
  const elapsedSeconds = getElapsedSeconds(elapsedStart, nowMs);
  const elapsedLabel = formatElapsed(elapsedSeconds);
  const showEta = ['checking', 'searching', 'downloading', 'repairing', 'unpacking', 'moving', 'importing'].includes(dj.status);
  const showElapsed = shouldShowDownloadElapsed(dj.status);
  const etaLabel = formatEta(getDownloadEtaSeconds(dj, nowMs)) ?? '—';
  const speedLabel = formatDownloadSpeed(dj.download_speed_bps);
  const retryLabel = formatDownloadRetry(dj);
  const clientLabel = formatDownloadClient(dj.client_type);
  const indexerLabel = dj.indexer_name || (dj.indexer_id != null ? `Indexer #${dj.indexer_id}` : null);
  const statusLabel = dj.status === 'searching' && retryLabel ? 'retrying' : dj.status.replace(/_/g, ' ');
  return (
    <div className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
      <div className="mb-2 flex items-center justify-between">
        <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] text-slate-400">Download</span>
        <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {dj.id}</span>
      </div>
      <div className="flex items-start justify-between gap-2">
        <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={dj.source_file_path}>{title || 'Unknown Title'}</p>
        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${dj.status === 'importing' ? 'border-violet-500/40 bg-violet-950/30 text-violet-300' : ['checking', 'searching'].includes(dj.status) ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : ['queued', 'paused'].includes(dj.status) ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : dj.status === 'downloading' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : dj.status === 'repairing' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : dj.status === 'unpacking' ? 'border-teal-500/40 bg-teal-950/30 text-teal-300' : dj.status === 'moving' ? 'border-indigo-500/40 bg-indigo-950/30 text-indigo-300' : dj.status === 'stalled' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : dj.status === 'waiting_encode' ? 'border-fuchsia-500/40 bg-fuchsia-950/30 text-fuchsia-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>
          {statusLabel}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
        <span>{year ?? '—'}</span>
        <span>{libName}</span>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        {showEta && <span>ETA: {etaLabel}</span>}
        {dj.status === 'queued' && <span>Elapsed: waiting</span>}
        {dj.status === 'paused' && <span>Elapsed: paused</span>}
        {showElapsed && <span>Elapsed: {elapsedLabel}</span>}
        {speedLabel && <span>Speed: {speedLabel}</span>}
        {retryLabel && <span>{retryLabel}</span>}
        {clientLabel && <span>Client: {clientLabel}</span>}
        {indexerLabel && <span>Indexer: {indexerLabel}</span>}
      </div>
      {['paused', 'downloading', 'repairing', 'unpacking', 'moving'].includes(dj.status) && (
        <div className="mt-2">
          <div className="h-1.5 w-full overflow-hidden rounded-full bg-slate-700">
            <div className="h-full rounded-full bg-gradient-to-r from-violet-500 to-violet-400 transition-all duration-300" style={{ width: `${dj.progress_percent}%` }} />
          </div>
          <div className="mt-1 text-xs text-slate-500">{dj.progress_percent}%</div>
        </div>
      )}
      {dj.error_message && <p className="mt-2.5 text-xs text-red-400">{dj.error_message}</p>}
      {dj.status === 'needs_review' && <DownloadReviewActions dj={dj} actionPending={actionPending} onReview={onReview} />}
      <MobileActionMenu>
        {['checking', 'searching', 'queued', 'paused', 'downloading', 'repairing', 'unpacking', 'moving', 'stalled', 'importing', 'waiting_encode'].includes(dj.status) && (
          <Btn size="sm" variant="danger" disabled={Boolean(actionPending)} onClick={() => onCancel(dj.id)}>
            {actionPending === 'remove_reset' ? 'Working…' : 'Reset Search'}
          </Btn>
        )}
        {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
          <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onRetry(dj.id)}>
            {actionPending === 'retry' ? 'Working…' : 'Retry Search'}
          </Btn>
        )}
        {dj.status !== 'needs_review' && (
        <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onDelete(dj.id)}>
          {actionPending === 'delete' ? 'Working…' : 'Delete'}
        </Btn>
        )}
      </MobileActionMenu>
    </div>
  );
});

const QueueEncodeCard = memo(function QueueEncodeCard({ job, libName, actionPending, activeDj, downloadEnabled, onJobAction }) {
  const progress = progressFromJob(job);
  const isRunning = job.status === 'running';
  const eta = formatEta(job.eta_seconds);
  const { year } = extractTitleYear(job.source_path);
  const title = getDisplayTitle(job.source_path);
  const djIsActive = activeDj && ['checking', 'searching', 'queued', 'paused', 'downloading', 'repairing', 'unpacking', 'moving', 'importing'].includes(activeDj.status);
  const queueWaitingForRoute = downloadEnabled
    && !djIsActive
    && (
      ['queued', 'pending', 'created', 'starting', 'preflight'].includes(job.status)
      || (job.status === 'paused' && progress === 0)
    );
  const jobModeLabel = downloadEnabled ? 'Auto' : 'Encode';
  const jobStatusLabel = queueWaitingForRoute ? 'awaiting download route' : job.status;
  return (
    <div className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
      <div className="mb-2 flex items-center justify-between">
        <span className={`rounded-full border bg-slate-900/80 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.16em] ${downloadEnabled ? 'border-cyan-500/40 text-cyan-300' : 'border-slate-700 text-slate-400'}`}>{jobModeLabel}</span>
        <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {job.id}</span>
      </div>
      <div className="flex items-start justify-between gap-2">
        <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={job.source_path}>{title || 'Unknown Title'}</p>
        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium ${queueWaitingForRoute ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : job.status === 'running' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : job.status === 'paused' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{jobStatusLabel}</span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
        <span>{year ?? '—'}</span>
        <span>{libName}</span>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span>{formatResolution(job.source_resolution)} · {formatHdrIndicator(job.source_is_hdr)}</span>
        {job.encoder_used && <span>{job.encoder_used}{job.hwaccel_used ? ' (HW)' : ''}</span>}
        {isRunning && job.fps != null && <span>{job.fps.toFixed(1)} fps</span>}
        {isRunning && eta && <span>{eta}</span>}
      </div>
      {job.status === 'failed' && job.error_message && <p className="mt-2.5 text-xs text-red-400">{job.error_message}</p>}
      {queueWaitingForRoute && <p className="mt-2.5 text-xs text-sky-400">Auto-routing: download first, encode fallback.</p>}
      {djIsActive && activeDj.status === 'searching' && <p className="mt-2.5 text-xs text-sky-400">Searching…</p>}
      {djIsActive && activeDj.status === 'queued' && <p className="mt-2.5 text-xs text-amber-300">Queued in client</p>}
      {djIsActive && activeDj.status === 'paused' && <p className="mt-2.5 text-xs text-amber-300">Paused in client</p>}
      {djIsActive && activeDj.status === 'downloading' && (
        <p className="mt-2.5 text-xs text-violet-400">
          Downloading {activeDj.progress_percent}%
          {formatDownloadSpeed(activeDj.download_speed_bps) ? ` • ${formatDownloadSpeed(activeDj.download_speed_bps)}` : ''}
          {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
          {activeDj.indexer_name ? ` • ${activeDj.indexer_name}` : ''}
        </p>
      )}
      {djIsActive && activeDj.status === 'moving' && (
        <p className="mt-2.5 text-xs text-indigo-400">
          Moving {activeDj.progress_percent}%
          {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
        </p>
      )}
      {djIsActive && activeDj.status === 'repairing' && (
        <p className="mt-2.5 text-xs text-emerald-400">
          Repairing {activeDj.progress_percent}%
          {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
        </p>
      )}
      {djIsActive && activeDj.status === 'unpacking' && (
        <p className="mt-2.5 text-xs text-teal-400">
          Unpacking {activeDj.progress_percent}%
          {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
        </p>
      )}
      {djIsActive && activeDj.status === 'importing' && <p className="mt-2.5 text-xs text-violet-400">Importing…</p>}
      <div className="mt-2.5">
        <div className="h-1.5 w-full rounded-full bg-slate-700">
          <div className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-cyan-400 transition-all duration-500" style={{ width: `${progress}%` }} />
        </div>
        <div className="mt-1 text-xs text-slate-500">{progress}%</div>
      </div>
      <MobileActionMenu>
        {job.status === 'running' && <Btn size="sm" variant="warning" disabled={Boolean(actionPending)} onClick={() => onJobAction('pause', job.id)}>{actionPending === 'pause' ? 'Working…' : 'Pause Encode'}</Btn>}
        {job.status === 'paused' && progress > 0 && <Btn size="sm" variant="success" disabled={Boolean(actionPending)} onClick={() => onJobAction('start', job.id)}>{actionPending === 'start' ? 'Working…' : 'Resume Now'}</Btn>}
        {(job.status === 'queued' || (job.status === 'paused' && progress === 0)) && !downloadEnabled && <Btn size="sm" variant="success" disabled={Boolean(actionPending)} onClick={() => onJobAction('start', job.id)}>{actionPending === 'start' ? 'Working…' : 'Start Now'}</Btn>}
        {job.status === 'interrupted' && <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onJobAction('requeue', job.id)}>{actionPending === 'requeue' ? 'Working…' : 'Requeue'}</Btn>}
        {(ACTIVE_STATUSES.has(job.status) || (job.status === 'paused' && progress > 0)) && (
          <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onJobAction('discard', job.id)}>
            {actionPending === 'discard' ? 'Working…' : (downloadEnabled ? 'Search Instead' : 'Restart From Beginning')}
          </Btn>
        )}
        {['queued', 'starting', 'running', 'paused', 'preflight'].includes(job.status) && (
          <Btn size="sm" variant="danger" disabled={Boolean(actionPending)} onClick={() => onJobAction('abort', job.id)}>
            {actionPending === 'abort' ? 'Working…' : 'Abort'}
          </Btn>
        )}
      </MobileActionMenu>
    </div>
  );
});

const QueueDownloadRow = memo(function QueueDownloadRow({ dj, libName, actionPending, nowMs, onCancel, onRetry, onDelete, onReview }) {
  const [reviewOpen, setReviewOpen] = useState(false);
  const { year } = extractTitleYear(dj.source_file_path);
  const title = getDisplayTitle(dj.source_file_path);
  const statusColor =
    ['queued', 'paused'].includes(dj.status) ? 'text-amber-400' :
    dj.status === 'downloading' ? 'text-cyan-400' :
    dj.status === 'repairing' ? 'text-emerald-400' :
    dj.status === 'unpacking' ? 'text-teal-400' :
    dj.status === 'moving' ? 'text-indigo-400' :
    dj.status === 'importing' ? 'text-violet-400' :
    dj.status === 'needs_review' ? 'text-amber-400' :
    ['checking', 'searching'].includes(dj.status) ? 'text-sky-400' :
    dj.status === 'stalled' ? 'text-amber-400' :
    dj.status === 'waiting_encode' ? 'text-fuchsia-400' :
    'text-slate-400';
  const elapsedStart = dj.download_started_at ?? dj.created_at;
  const elapsedSeconds = getElapsedSeconds(elapsedStart, nowMs);
  const elapsedLabel = formatElapsed(elapsedSeconds);
  const showEta = ['checking', 'searching', 'downloading', 'repairing', 'unpacking', 'moving', 'importing'].includes(dj.status);
  const showElapsed = shouldShowDownloadElapsed(dj.status);
  const etaLabel = formatEta(getDownloadEtaSeconds(dj, nowMs)) ?? '—';
  const speedLabel = formatDownloadSpeed(dj.download_speed_bps);
  const retryLabel = formatDownloadRetry(dj);
  const clientLabel = formatDownloadClient(dj.client_type);
  const indexerLabel = dj.indexer_name || (dj.indexer_id != null ? `Indexer #${dj.indexer_id}` : null);
  const statusLabel = dj.status === 'searching' && retryLabel ? 'retrying' : dj.status.replace(/_/g, ' ');
  const downloadMetaParts = [
    clientLabel,
    indexerLabel,
    showElapsed ? `Elapsed: ${elapsedLabel}` : null,
    dj.status === 'queued' ? 'Waiting in client queue' : null,
    dj.status === 'paused' ? 'Paused in client' : null,
  ].filter(Boolean);
  return (
    <tr className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-500 2xl:table-cell">{dj.id}</td>
      <td className="max-w-[180px] truncate px-4 py-2.5 align-top text-sm text-slate-200" title={dj.source_file_path}>{title}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{libName}</td>
      <td className="px-4 py-2.5 align-top text-sm">
        <div className="flex items-center gap-1.5">
          {['checking', 'searching'].includes(dj.status) && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />}
          {dj.status === 'repairing' && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />}
          {dj.status === 'unpacking' && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-teal-400" />}
          {dj.status === 'moving' && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-400" />}
          {dj.status === 'importing' && <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-violet-400" />}
          <span className={`rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${statusColor === 'text-violet-400' ? 'border-violet-500/40 bg-violet-950/30 text-violet-300' : statusColor === 'text-indigo-400' ? 'border-indigo-500/40 bg-indigo-950/30 text-indigo-300' : statusColor === 'text-emerald-400' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : statusColor === 'text-teal-400' ? 'border-teal-500/40 bg-teal-950/30 text-teal-300' : statusColor === 'text-sky-400' ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : statusColor === 'text-cyan-400' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : statusColor === 'text-amber-400' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : statusColor === 'text-fuchsia-400' ? 'border-fuchsia-500/40 bg-fuchsia-950/30 text-fuchsia-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{statusLabel}</span>
        </div>
        {showEta && <p className="mt-0.5 text-xs text-slate-400">ETA: {etaLabel}</p>}
        {speedLabel && <p className="mt-0.5 text-xs text-slate-400">Speed: {speedLabel}</p>}
        {retryLabel && <p className="mt-0.5 text-xs text-amber-300">{retryLabel}</p>}
        {downloadMetaParts.length > 0 && (
          <p className="mt-0.5 text-xs text-slate-500" title={elapsedSeconds == null ? undefined : `${elapsedSeconds}s elapsed`}>
            {downloadMetaParts.join(' · ')}
          </p>
        )}
        {dj.error_message && <p className="mt-0.5 text-xs text-red-400">{dj.error_message}</p>}
      </td>
      <td className="hidden max-w-[180px] truncate px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell" title={dj.search_query}>
        {dj.status === 'searching' && !dj.search_query ? <span className="italic text-slate-500">Building query…</span> : (dj.search_query ?? '—')}
      </td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-600 2xl:table-cell">—</td>
      <td className="px-4 py-2.5 align-top">
        {['paused', 'downloading', 'repairing', 'unpacking', 'moving'].includes(dj.status) ? (
          <div>
            <div className="h-1.5 w-24 overflow-hidden rounded-full bg-slate-700 md:w-32">
              <div className="h-full rounded-full bg-gradient-to-r from-violet-500 to-violet-400 transition-all duration-300" style={{ width: `${dj.progress_percent}%` }} />
            </div>
            <div className="mt-1.5 text-xs text-slate-500">{dj.progress_percent}%</div>
          </div>
        ) : <span className="text-xs text-slate-600">—</span>}
      </td>
      <td className="px-4 py-2.5 align-top">
        <div className="flex flex-wrap gap-1.5">
          {dj.status === 'needs_review' && (
            <Btn size="sm" variant="warning" disabled={Boolean(actionPending)} onClick={() => setReviewOpen(true)}>
              Review Match
            </Btn>
          )}
          {['checking', 'searching', 'queued', 'paused', 'downloading', 'repairing', 'unpacking', 'moving', 'stalled', 'importing', 'waiting_encode'].includes(dj.status) && (
            <Btn size="sm" variant="danger" disabled={Boolean(actionPending)} onClick={() => onCancel(dj.id)}>
              {actionPending === 'remove_reset' ? 'Working…' : 'Reset Search'}
            </Btn>
          )}
          {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
            <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onRetry(dj.id)}>
              {actionPending === 'retry' ? 'Working…' : 'Retry Search'}
            </Btn>
          )}
          {dj.status !== 'needs_review' && (
          <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onDelete(dj.id)}>
            {actionPending === 'delete' ? 'Working…' : 'Delete'}
          </Btn>
          )}
        </div>
        <Modal open={reviewOpen} title={`Review download match: ${title}`} onClose={() => setReviewOpen(false)}>
          <DownloadReviewActions
            dj={dj}
            actionPending={actionPending}
            onReview={(...args) => {
              setReviewOpen(false);
              onReview(...args);
            }}
          />
        </Modal>
      </td>
    </tr>
  );
});

const QueueEncodeRow = memo(function QueueEncodeRow({ job, libName, actionPending, activeDj, downloadEnabled, onJobAction }) {
  const progress = progressFromJob(job);
  const isRunning = job.status === 'running';
  const eta = formatEta(job.eta_seconds);
  const { year } = extractTitleYear(job.source_path);
  const title = getDisplayTitle(job.source_path);
  const djIsActive = activeDj && ['checking', 'searching', 'queued', 'paused', 'downloading', 'repairing', 'unpacking', 'moving', 'importing'].includes(activeDj.status);
  const queueWaitingForRoute = downloadEnabled
    && !djIsActive
    && (
      ['queued', 'pending', 'created', 'starting', 'preflight'].includes(job.status)
      || (job.status === 'paused' && progress === 0)
    );
  const jobStatusLabel = queueWaitingForRoute ? 'awaiting download route' : job.status;
  return (
    <tr className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-500 2xl:table-cell">{job.id}</td>
      <td className="max-w-[180px] truncate px-4 py-2.5 align-top text-sm text-slate-200" title={job.source_path}>{title}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{libName}</td>
      <td className="px-4 py-3 text-sm capitalize">
        <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${queueWaitingForRoute ? 'border-sky-500/40 bg-sky-950/30 text-sky-300' : job.status === 'running' ? 'border-cyan-500/40 bg-cyan-950/30 text-cyan-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : job.status === 'paused' ? 'border-amber-500/40 bg-amber-950/30 text-amber-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{jobStatusLabel}</span>
        {queueWaitingForRoute && (
          <p className="mt-0.5 text-xs text-sky-400">Auto-routing: download first, encode fallback.</p>
        )}
        {job.status === 'failed' && job.error_message && (
          <p className="mt-0.5 text-xs text-red-400">{job.error_message}</p>
        )}
        {djIsActive && activeDj.status === 'searching' && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-sky-400">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-sky-400" />
            Searching…
          </p>
        )}
        {djIsActive && activeDj.status === 'queued' && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-amber-300">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-300" />
            Queued in client
          </p>
        )}
        {djIsActive && activeDj.status === 'paused' && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-amber-300">
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-amber-300" />
            Paused in client
          </p>
        )}
        {djIsActive && activeDj.status === 'downloading' && (
          <p className="mt-0.5 text-xs text-violet-400">
            ↓ Downloading {activeDj.progress_percent}%
            {formatDownloadSpeed(activeDj.download_speed_bps) ? ` • ${formatDownloadSpeed(activeDj.download_speed_bps)}` : ''}
            {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
            {activeDj.indexer_name ? ` • ${activeDj.indexer_name}` : ''}
          </p>
        )}
        {djIsActive && activeDj.status === 'moving' && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-indigo-400">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-indigo-400" />
            Moving {activeDj.progress_percent}%
            {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
          </p>
        )}
        {djIsActive && activeDj.status === 'repairing' && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-emerald-400">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-400" />
            Repairing {activeDj.progress_percent}%
            {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
          </p>
        )}
        {djIsActive && activeDj.status === 'unpacking' && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-teal-400">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-teal-400" />
            Unpacking {activeDj.progress_percent}%
            {formatDownloadClient(activeDj.client_type) ? ` • ${formatDownloadClient(activeDj.client_type)}` : ''}
          </p>
        )}
        {djIsActive && activeDj.status === 'importing' && (
          <p className="mt-0.5 flex items-center gap-1 text-xs text-violet-400">
            <span className="inline-block h-1.5 w-1.5 animate-pulse rounded-full bg-violet-400" />
            Importing…
          </p>
        )}
      </td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">
        <span>{formatResolution(job.source_resolution)}</span>
        <span className="mx-1.5 text-slate-600">·</span>
        <span>{formatHdrIndicator(job.source_is_hdr)}</span>
      </td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">
        {job.encoder_used ? (
          <>
            <span className={job.hwaccel_used ? 'font-medium text-cyan-400' : ''}>{job.encoder_used}</span>
            {job.hwaccel_used && (
              <span className="ml-1.5 rounded bg-cyan-900/50 px-1.5 py-0.5 text-cyan-300">HW</span>
            )}
          </>
        ) : (
          <span className="text-slate-600">—</span>
        )}
      </td>
      <td className="px-4 py-2.5 align-top">
        <div className="h-1.5 w-24 rounded-full bg-slate-700 md:w-32">
          <div
            className="h-full rounded-full bg-gradient-to-r from-cyan-500 to-cyan-400 transition-all duration-500"
            style={{ width: `${progress}%` }}
          />
        </div>
        <div className="mt-1.5 flex items-center gap-2 text-xs text-slate-500">
          <span>{progress}%</span>
          {isRunning && job.fps != null && <span>{job.fps.toFixed(1)} fps</span>}
          {isRunning && eta && <span>{eta}</span>}
        </div>
      </td>
      <td className="px-4 py-2.5 align-top">
        <div className="flex flex-wrap gap-1.5">
          {job.status === 'running' && <Btn size="sm" variant="warning" disabled={Boolean(actionPending)} onClick={() => onJobAction('pause', job.id)}>{actionPending === 'pause' ? 'Working…' : 'Pause Encode'}</Btn>}
          {job.status === 'paused' && progress > 0 && <Btn size="sm" variant="success" disabled={Boolean(actionPending)} onClick={() => onJobAction('start', job.id)}>{actionPending === 'start' ? 'Working…' : 'Resume Now'}</Btn>}
          {(job.status === 'queued' || (job.status === 'paused' && progress === 0)) && !downloadEnabled && <Btn size="sm" variant="success" disabled={Boolean(actionPending)} onClick={() => onJobAction('start', job.id)}>{actionPending === 'start' ? 'Working…' : 'Start Now'}</Btn>}
          {job.status === 'interrupted' && <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onJobAction('requeue', job.id)}>{actionPending === 'requeue' ? 'Working…' : 'Requeue'}</Btn>}
          {(ACTIVE_STATUSES.has(job.status) || (job.status === 'paused' && progress > 0)) && (
            <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onJobAction('discard', job.id)}>
              {actionPending === 'discard' ? 'Working…' : (downloadEnabled ? 'Search Instead' : 'Restart From Beginning')}
            </Btn>
          )}
          {['queued', 'starting', 'running', 'paused', 'preflight'].includes(job.status) && (
            <Btn size="sm" variant="danger" disabled={Boolean(actionPending)} onClick={() => onJobAction('abort', job.id)}>
              {actionPending === 'abort' ? 'Working…' : 'Abort'}
            </Btn>
          )}
        </div>
      </td>
    </tr>
  );
});

// ── History rows ─────────────────────────────────────────────────────────────

const HistoryDownloadCard = memo(function HistoryDownloadCard({ dj, libName, actionPending, onRetry, onDelete, onDeleteFile }) {
  const { year } = extractTitleYear(dj.source_file_path);
  const title = getDisplayTitle(dj.source_file_path);
  const completedDate = formatHistoryCompletedAt(dj.completed_at);
  const clientLabel = formatDownloadClient(dj.client_type);
  return (
    <div className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
      <div className="mb-2 flex items-center justify-between">
        <HistoryTypeBadge type="download" />
        <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {dj.id}</span>
      </div>
      <div className="flex items-start justify-between gap-2">
        <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={dj.source_file_path}>{title || 'Unknown Title'}</p>
        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium capitalize ${dj.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : dj.status === 'failed' || dj.status === 'timed_out' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{dj.status.replace(/_/g, ' ')}</span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
        <span>{year ?? '—'}</span>
        <span>{libName}</span>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        <span>{clientLabel ? `Client: ${clientLabel}` : '—'}</span>
      </div>
      <p className="mt-2.5 text-xs text-slate-500">{completedDate}</p>
      {dj.error_message && <p className="mt-2.5 text-xs text-red-400">{dj.error_message}</p>}
      <MobileActionMenu>
        {dj.status === 'complete' && dj.imported_file_path && (
          <>
            <Btn size="sm" variant="danger" disabled={Boolean(actionPending)} onClick={() => onDeleteFile(dj.id, false)}>
              {actionPending === 'delete_file' ? 'Deleting…' : 'Delete File'}
            </Btn>
            <Btn size="sm" variant="warning" disabled={Boolean(actionPending)} onClick={() => onDeleteFile(dj.id, true)}>
              {actionPending === 'delete_retry' ? 'Working…' : 'Delete & Retry'}
            </Btn>
          </>
        )}
        {dj.status === 'file_deleted' && (
          <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onDeleteFile(dj.id, true)}>
            {actionPending === 'delete_retry' ? 'Working…' : 'Retry Search'}
          </Btn>
        )}
        {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
          <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onRetry(dj.id)}>
            {actionPending === 'retry' ? 'Working…' : 'Retry Search'}
          </Btn>
        )}
        <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onDelete(dj.id)}>
          {actionPending === 'delete' ? 'Working…' : 'Remove'}
        </Btn>
      </MobileActionMenu>
    </div>
  );
});

const HistoryEncodeCard = memo(function HistoryEncodeCard({ job, libName, actionPending, downloadEnabled, hasFallback, onJobAction }) {
  const { year } = extractTitleYear(job.source_path);
  const title = getDisplayTitle(job.source_path);
  const completedDate = formatHistoryCompletedAt(job.completed_at);
  const encodeDuration = formatElapsed(job.encode_duration_seconds);
  return (
    <div className="rounded-2xl border border-slate-700/80 bg-gradient-to-br from-slate-900/90 to-slate-900/65 p-3.5 shadow-lg shadow-slate-950/30">
      <div className="mb-2 flex items-center justify-between">
        <HistoryTypeBadge type="encode" />
        <span className="rounded-full border border-slate-700 bg-slate-900/80 px-2 py-0.5 text-[10px] text-slate-400">ID {job.id}</span>
      </div>
      <div className="flex items-start justify-between gap-2">
        <p className="min-w-0 truncate text-sm font-semibold leading-5 text-slate-100" title={job.source_path}>{title || 'Unknown Title'}</p>
        <span className={`shrink-0 rounded-full border px-2 py-0.5 text-[11px] font-medium ${job.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{job.status}</span>
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-400">
        <span>{year ?? '—'}</span>
        <span>{libName}</span>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-500">
        {hasFallback && <FallbackIndicator />}
        <span>{formatResolution(job.source_resolution)} · {formatHdrIndicator(job.source_is_hdr)}</span>
        {job.encoder_used && <span>{job.encoder_used}{job.hwaccel_used ? ' (HW)' : ''}</span>}
        <span>Encode: {encodeDuration}</span>
      </div>
      <p className="mt-2.5 text-xs text-slate-500">{completedDate}</p>
      {job.status === 'failed' && job.error_message && (
        <p className="mt-2.5 text-xs text-red-400">{job.error_message}</p>
      )}
      <MobileActionMenu>
        {['failed', 'cancelled'].includes(job.status) && (
          <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onJobAction('retry', job.id)}>
            {actionPending === 'retry' ? 'Working…' : (downloadEnabled ? 'Search Again' : 'Retry Encode')}
          </Btn>
        )}
        <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onJobAction('remove', job.id)}>
          {actionPending === 'remove' ? 'Working…' : 'Remove'}
        </Btn>
      </MobileActionMenu>
    </div>
  );
});

const HistoryDownloadRow = memo(function HistoryDownloadRow({ dj, libName, actionPending, onRetry, onDelete, onDeleteFile }) {
  const { year } = extractTitleYear(dj.source_file_path);
  const title = getDisplayTitle(dj.source_file_path);
  const clientLabel = formatDownloadClient(dj.client_type);
  const completedDate = formatHistoryCompletedAt(dj.completed_at);
  return (
    <tr className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
      <td className="px-4 py-2.5 align-top text-sm"><HistoryTypeBadge type="download" /></td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-500 2xl:table-cell">{dj.id}</td>
      <td className="max-w-[180px] truncate px-4 py-2.5 align-top text-sm text-slate-200" title={dj.source_file_path}>{title}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{libName}</td>
      <td className="px-4 py-2.5 align-top text-sm">
        <span className={`rounded-full border px-2 py-0.5 text-xs font-medium capitalize ${dj.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : dj.status === 'failed' || dj.status === 'timed_out' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{dj.status.replace(/_/g, ' ')}</span>
        {dj.error_message && <p className="mt-0.5 text-xs text-red-400">{dj.error_message}</p>}
      </td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">{clientLabel ? `Client: ${clientLabel}` : '—'}</td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">—</td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">—</td>
      <td className="whitespace-nowrap px-4 py-2.5 align-top text-xs text-slate-400">{completedDate}</td>
      <td className="px-4 py-2.5 align-top">
        <MobileActionMenu label="Manage">
          {dj.status === 'complete' && dj.imported_file_path && (
            <>
              <Btn size="sm" variant="danger" disabled={Boolean(actionPending)} onClick={() => onDeleteFile(dj.id, false)}>
                {actionPending === 'delete_file' ? 'Deleting…' : 'Delete File'}
              </Btn>
              <Btn size="sm" variant="warning" disabled={Boolean(actionPending)} onClick={() => onDeleteFile(dj.id, true)}>
                {actionPending === 'delete_retry' ? 'Working…' : 'Delete & Retry'}
              </Btn>
            </>
          )}
          {dj.status === 'file_deleted' && (
            <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onDeleteFile(dj.id, true)}>
              {actionPending === 'delete_retry' ? 'Working…' : 'Retry Search'}
            </Btn>
          )}
          {['failed', 'timed_out', 'stalled'].includes(dj.status) && (
            <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onRetry(dj.id)}>
              {actionPending === 'retry' ? 'Working…' : 'Retry Search'}
            </Btn>
          )}
          <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onDelete(dj.id)}>
            {actionPending === 'delete' ? 'Working…' : 'Remove'}
          </Btn>
        </MobileActionMenu>
      </td>
    </tr>
  );
});

const HistoryEncodeRow = memo(function HistoryEncodeRow({ job, libName, actionPending, downloadEnabled, hasFallback, onJobAction }) {
  const { year } = extractTitleYear(job.source_path);
  const title = getDisplayTitle(job.source_path);
  const completedDate = formatHistoryCompletedAt(job.completed_at);
  const encodeDuration = formatElapsed(job.encode_duration_seconds);
  return (
    <tr className="transition-colors duration-100 odd:bg-slate-900/20 hover:bg-slate-800/30">
      <td className="px-4 py-2.5 align-top text-sm"><HistoryTypeBadge type="encode" /></td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-500 2xl:table-cell">{job.id}</td>
      <td className="max-w-[180px] truncate px-4 py-2.5 align-top text-sm text-slate-200" title={job.source_path}>{title}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{year ?? '—'}</td>
      <td className="hidden px-4 py-2.5 align-top text-sm text-slate-400 lg:table-cell">{libName}</td>
      <td className="px-4 py-3 text-sm capitalize">
        <span className={`rounded-full border px-2 py-0.5 text-xs font-medium ${job.status === 'complete' ? 'border-emerald-500/40 bg-emerald-950/30 text-emerald-300' : job.status === 'failed' ? 'border-red-500/40 bg-red-950/30 text-red-300' : 'border-slate-700 bg-slate-800/60 text-slate-300'}`}>{job.status}</span>
        {job.status === 'failed' && job.error_message && (
          <p className="mt-0.5 text-xs text-red-400">{job.error_message}</p>
        )}
      </td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">
        <div className="space-y-1">
          <div className="flex items-center gap-2">
            {hasFallback && <FallbackIndicator />}
            <span>{formatResolution(job.source_resolution)}</span>
            <span className="mx-1.5 text-slate-600">·</span>
            <span>{formatHdrIndicator(job.source_is_hdr)}</span>
          </div>
        </div>
      </td>
      <td className="hidden px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">
        {job.encoder_used ? (
          <>
            <span className={job.hwaccel_used ? 'font-medium text-cyan-400' : ''}>{job.encoder_used}</span>
            {job.hwaccel_used && (
              <span className="ml-1.5 rounded bg-cyan-900/50 px-1.5 py-0.5 text-cyan-300">HW</span>
            )}
          </>
        ) : (
          <span className="text-slate-600">—</span>
        )}
      </td>
      <td className="hidden whitespace-nowrap px-4 py-2.5 align-top text-xs text-slate-400 2xl:table-cell">{encodeDuration}</td>
      <td className="whitespace-nowrap px-4 py-2.5 align-top text-xs text-slate-400">{completedDate}</td>
      <td className="whitespace-nowrap px-4 py-2.5 align-top">
        <div className="flex flex-wrap gap-1.5">
          {['failed', 'cancelled'].includes(job.status) && (
            <Btn size="sm" variant="primary" disabled={Boolean(actionPending)} onClick={() => onJobAction('retry', job.id)}>
              {actionPending === 'retry' ? 'Working…' : (downloadEnabled ? 'Search Again' : 'Retry Encode')}
            </Btn>
          )}
          <Btn size="sm" variant="secondary" disabled={Boolean(actionPending)} onClick={() => onJobAction('remove', job.id)}>
            {actionPending === 'remove' ? 'Working…' : 'Remove'}
          </Btn>
        </div>
      </td>
    </tr>
  );
});

// ── Jobs page ────────────────────────────────────────────────────────────────

function JobsPage({
  jobs,
  downloadJobs,
  libraryById,
  libraryProfiles,
  queuePaused,
  pendingJobActions,
  pendingDownloadActions,
  settingsQueueSort,
  pageResetSignal,
  onJobAction,
  onCancelDownloadJob,
  onRetryDownloadJob,
  onReviewDownloadJob,
  onDeleteDownloadJob,
  onDeleteDownloadedFile,
  onCancelAllQueued,
  onClearQueue,
  onAbortAllJobs,
  onQueueAction,
  onPurgeHistory,
  onPersistQueueSort,
}) {
  const [jobsUiPrefs] = useState(loadJobsUiPrefs);
  const [jobsView, setJobsView] = useState(() => (jobsUiPrefs.jobsView === 'history' ? 'history' : 'queue'));
  const [jobsPage, setJobsPage] = useState(1);
  const [historyPage, setHistoryPage] = useState(1);
  const [queueSearch, setQueueSearch] = useState(() => String(jobsUiPrefs.queueSearch ?? ''));
  const [historySearch, setHistorySearch] = useState(() => String(jobsUiPrefs.historySearch ?? ''));
  const [queueSort, setQueueSort] = useState(() => {
    const val = String(jobsUiPrefs.queueSort ?? 'default');
    return val === 'oldest' ? 'default' : val;
  });
  const [historySort, setHistorySort] = useState(() => {
    const val = String(jobsUiPrefs.historySort ?? 'completed_desc');
    if (val === 'year_desc') return 'year_newest';
    if (val === 'year_asc') return 'year_oldest';
    return val;
  });
  const [historyTypeFilter, setHistoryTypeFilter] = useState(() => {
    const val = String(jobsUiPrefs.historyTypeFilter ?? 'all');
    return ['all', 'encode', 'download'].includes(val) ? val : 'all';
  });
  const [nowMs, setNowMs] = useState(() => Date.now());

  // Follow the server-persisted sort option.
  useEffect(() => {
    if (!settingsQueueSort) return;
    setQueueSort((prev) => (prev === settingsQueueSort ? prev : settingsQueueSort));
    setJobsPage(1);
  }, [settingsQueueSort]);

  // App signals a jump back to page one when a job becomes active.
  useEffect(() => {
    setJobsPage(1);
  }, [pageResetSignal]);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    window.localStorage.setItem(
      JOBS_UI_PREFS_KEY,
      JSON.stringify({
        queueSearch,
        historySearch,
        queueSort,
        historySort,
        historyTypeFilter,
        jobsView,
      }),
    );
  }, [queueSearch, historySearch, queueSort, historySort, historyTypeFilter, jobsView]);

  // Build a lookup map: source_file_path → most-recent active download job
  const downloadJobBySource = useMemo(() => {
    const map = {};
    for (const dj of downloadJobs) {
      map[dj.source_file_path] = dj;
    }
    return map;
  }, [downloadJobs]);

  const activeJobs = useMemo(
    () => jobs.filter((job) => !TERMINAL_STATUSES.has(job.status?.toLowerCase())),
    [jobs],
  );

  const historyJobs = useMemo(
    () => jobs.filter((job) => TERMINAL_STATUSES.has(job.status?.toLowerCase())),
    [jobs],
  );

  const sortedActiveJobs = useMemo(
    () => sortJobsByOption(activeJobs, queueSort, compareActiveJobsDefault),
    [activeJobs, queueSort],
  );

  function jobMatchesSearch(job, search) {
    if (!search) return true;
    const lower = search.toLowerCase();
    const { title, year } = extractTitleYear(job.source_path);
    const libName = job.library_id != null ? (libraryById[job.library_id]?.name ?? '') : '';
    return (
      title.toLowerCase().includes(lower)
      || (year && year.includes(lower))
      || libName.toLowerCase().includes(lower)
      || job.source_path?.toLowerCase().includes(lower)
      || String(job.id).includes(lower)
    );
  }

  const filteredActiveJobs = useMemo(
    () => sortedActiveJobs.filter((job) => jobMatchesSearch(job, queueSearch)),
    [sortedActiveJobs, queueSearch, libraryById],
  );

  const fallbackHistoryByEncodeJobId = useMemo(
    () => buildFallbackHistoryByEncodeJobId(downloadJobs),
    [downloadJobs],
  );

  const terminalDownloadHistoryJobs = useMemo(
    () => downloadJobs.filter((dj) => {
      const status = String(dj.status ?? '').toLowerCase();
      return TERMINAL_DL_STATUSES.has(status) && status !== 'fallback_queued';
    }),
    [downloadJobs],
  );

  const allHistoryItems = useMemo(
    () => buildUnifiedHistoryItems(historyJobs, terminalDownloadHistoryJobs),
    [historyJobs, terminalDownloadHistoryJobs],
  );

  const filteredHistoryItems = useMemo(() => {
    return allHistoryItems
      .filter((item) => historyTypeFilter === 'all' || item._historyType === historyTypeFilter)
      .filter((item) => (
        item._historyType === 'download'
          ? downloadJobMatchesSearch(item, historySearch, libraryById)
          : jobMatchesSearch(item, historySearch)
      ));
  }, [allHistoryItems, historyTypeFilter, historySearch, libraryById]);

  const sortedHistoryItems = useMemo(
    () => [...filteredHistoryItems].sort((a, b) => compareHistoryItemsByOption(a, b, historySort)),
    [filteredHistoryItems, historySort],
  );

  const totalHistoryCount = allHistoryItems.length;
  const visibleHistoryCount = sortedHistoryItems.length;

  const activeDlQueueItems = useMemo(
    () => downloadJobs.filter((dj) => ACTIVE_DL_STATUSES.has(String(dj.status ?? '').toLowerCase())),
    [downloadJobs],
  );

  const queueDedupeSourcePaths = useMemo(() => {
    const paths = new Set();
    for (const dj of downloadJobs) {
      const status = String(dj.status ?? '').toLowerCase();
      if (!QUEUE_DEDUPE_DL_STATUSES.has(status)) continue;
      const sourcePath = String(dj.source_file_path ?? '').trim();
      if (sourcePath) paths.add(sourcePath);
    }
    return paths;
  }, [downloadJobs]);

  // Active download jobs filtered by search, tagged as 'download' type for unified queue
  const filteredDlQueueItems = useMemo(() => {
    const dlSearch = queueSearch.toLowerCase();
    return activeDlQueueItems
      .filter((dj) => {
        if (!dlSearch) return true;
        const { title, year } = extractTitleYear(dj.source_file_path);
        const libName = dj.library_id != null ? (libraryById[dj.library_id]?.name ?? '') : '';
        return (
          title.toLowerCase().includes(dlSearch)
          || (year && year.includes(dlSearch))
          || libName.toLowerCase().includes(dlSearch)
          || dj.source_file_path?.toLowerCase().includes(dlSearch)
          || String(dj.id).includes(dlSearch)
        );
      });
  }, [activeDlQueueItems, queueSearch, libraryById]);

  const unifiedAllQueueItems = useMemo(
    () => buildUnifiedQueueItems({
      encodeItems: activeJobs,
      downloadItems: activeDlQueueItems,
      sortOption: queueSort,
      extractTitleYear,
      pinActiveFirst: true,
      dedupeSourcePaths: queueDedupeSourcePaths,
    }),
    [activeJobs, activeDlQueueItems, queueSort, queueDedupeSourcePaths],
  );

  // Unified queue: encoding jobs + active download jobs using one comparator path
  const unifiedQueueItems = useMemo(
    () => buildUnifiedQueueItems({
      encodeItems: filteredActiveJobs,
      downloadItems: filteredDlQueueItems,
      sortOption: queueSort,
      extractTitleYear,
      pinActiveFirst: true,
      dedupeSourcePaths: queueDedupeSourcePaths,
    }),
    [filteredActiveJobs, filteredDlQueueItems, queueSort, queueDedupeSourcePaths],
  );

  const queueCount = unifiedAllQueueItems.length;
  const queuedEncodeCount = activeJobs.filter((job) => job.status === 'queued').length;
  const allEncodeCount = activeJobs.length;
  const queueEmptyMessage = queueSearch
    ? 'No queue items match this search.'
    : queuePaused
      ? 'The queue is empty and new jobs are paused.'
      : 'The queue is empty. Optimizarr is watching enabled libraries for eligible media.';

  const totalJobPages = useMemo(
    () => Math.max(1, Math.ceil(unifiedQueueItems.length / JOBS_PAGE_SIZE)),
    [unifiedQueueItems.length],
  );

  const totalHistoryPages = useMemo(
    () => Math.max(1, Math.ceil(sortedHistoryItems.length / HISTORY_PAGE_SIZE)),
    [sortedHistoryItems.length],
  );

  const pagedJobs = useMemo(() => {
    const start = (jobsPage - 1) * JOBS_PAGE_SIZE;
    return unifiedQueueItems.slice(start, start + JOBS_PAGE_SIZE);
  }, [unifiedQueueItems, jobsPage]);

  const pagedHistoryItems = useMemo(() => {
    const start = (historyPage - 1) * HISTORY_PAGE_SIZE;
    return sortedHistoryItems.slice(start, start + HISTORY_PAGE_SIZE);
  }, [sortedHistoryItems, historyPage]);

  useEffect(() => {
    if (jobsPage > totalJobPages) setJobsPage(totalJobPages);
  }, [jobsPage, totalJobPages]);

  useEffect(() => {
    if (historyPage > totalHistoryPages) setHistoryPage(totalHistoryPages);
  }, [historyPage, totalHistoryPages]);

  // nowMs only feeds elapsed/ETA labels on active download rows in the queue
  // view; skip the 1s re-render tick whenever none are on screen.
  const hasActiveDownloadRows = activeDlQueueItems.length > 0;
  useEffect(() => {
    if (jobsView !== 'queue' || !hasActiveDownloadRows) return undefined;
    setNowMs(Date.now());
    const timer = setInterval(() => setNowMs(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [jobsView, hasActiveDownloadRows]);

  function libNameFor(libraryId) {
    return libraryId != null ? (libraryById[libraryId]?.name ?? '—') : '—';
  }

  function handleQueueSortChange(nextSort) {
    setQueueSort(nextSort);
    setJobsPage(1);
    onPersistQueueSort(nextSort);
  }

  return (
    <section className="animate-fade-in space-y-5">

      {/* Queue / History tab card */}
      <div className="overflow-hidden rounded-2xl border border-slate-700/70 bg-slate-900/75 shadow-2xl shadow-slate-950/45 backdrop-blur-sm">
        <div className="border-b border-slate-700/70 px-5 py-3">
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div>
              <p className="text-sm font-semibold tracking-wide text-slate-200">Job Activity</p>
              <p className="text-xs text-slate-500">Track current queue operations and historical processing outcomes.</p>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              {jobsView === 'queue' && (
                <span aria-label={`Queue status: ${queuePaused ? 'new jobs paused' : 'accepting new jobs'}`} className={`rounded-full border px-2.5 py-1 text-xs ${queuePaused ? 'border-amber-500/40 bg-amber-950/40 text-amber-300' : 'border-emerald-500/40 bg-emerald-950/40 text-emerald-300'}`}>
                  {queuePaused ? 'New Jobs Paused' : 'Queue Active'}
                </span>
              )}
            </div>
          </div>
          <div className="flex flex-wrap items-center justify-between gap-3">
          {/* Tab switcher */}
          <div role="tablist" aria-label="Job activity view" className="flex gap-1 rounded-xl border border-slate-700/70 bg-slate-950/60 p-1">
            <button
              type="button"
              role="tab"
              aria-selected={jobsView === 'queue'}
              onClick={() => setJobsView('queue')}
              className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${jobsView === 'queue' ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950 shadow-sm shadow-cyan-500/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'}`}
            >
              Queue
              <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${jobsView === 'queue' ? 'bg-slate-950/30 text-slate-900' : 'bg-slate-700 text-slate-300'}`}>{queueCount}</span>
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={jobsView === 'history'}
              onClick={() => setJobsView('history')}
              className={`flex items-center gap-2 rounded-lg px-4 py-2 text-sm font-medium transition-all duration-200 ${jobsView === 'history' ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950 shadow-sm shadow-cyan-500/30' : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'}`}
            >
              History
              <span
                className={`rounded-full px-2 py-0.5 text-xs font-semibold ${jobsView === 'history' ? 'bg-slate-950/30 text-slate-900' : 'bg-slate-700 text-slate-300'}`}
                title={historySearch ? `Showing ${visibleHistoryCount} filtered result(s)` : 'Total history entries'}
              >
                {totalHistoryCount}
              </span>
            </button>
          </div>
          {/* Action buttons for the active view */}
          {jobsView === 'queue' && (
            <div className="flex flex-wrap items-center gap-2">
              <div className="relative">
                <TextInput
                  aria-label="Search queue"
                  type="text"
                  placeholder="Search queue…"
                  value={queueSearch}
                  onChange={(e) => { setQueueSearch(e.target.value); setJobsPage(1); }}
                  className="w-48 py-1.5 pr-8 text-xs"
                />
                {queueSearch && (
                  <button
                    type="button"
                    aria-label="Clear queue search"
                    onClick={() => { setQueueSearch(''); setJobsPage(1); }}
                    className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-xs font-semibold text-slate-400 transition-colors hover:text-slate-100"
                  >
                    x
                  </button>
                )}
              </div>
              <SelectInput
                aria-label="Sort queue"
                value={queueSort}
                onChange={(e) => { handleQueueSortChange(e.target.value); }}
                className="w-44 py-1.5 text-xs"
              >
                <option value="default">Date Added (Oldest)</option>
                <option value="newest">Date Added (Newest)</option>
                <option value="year_newest">Release Year (Newest)</option>
                <option value="year_oldest">Release Year (Oldest)</option>
              </SelectInput>
              <Btn size="sm" variant="secondary" disabled={queuedEncodeCount === 0} onClick={onCancelAllQueued}>Cancel Queued Encodes ({queuedEncodeCount})</Btn>
              <Btn size="sm" variant="secondary" disabled={queueCount === 0} onClick={onClearQueue}>Clear Entire Queue ({queueCount})</Btn>
              <Btn size="sm" variant="danger" disabled={allEncodeCount === 0} onClick={onAbortAllJobs}>Cancel All Encodes ({allEncodeCount})</Btn>
              <Btn size="sm" variant="warning" onClick={() => onQueueAction(queuePaused ? 'resume' : 'pause')}>
                {queuePaused ? 'Resume New Jobs' : 'Pause New Jobs'}
              </Btn>
              <p className={`basis-full text-xs ${queuePaused ? 'text-amber-300' : 'text-slate-500'}`}>
                {queuePaused
                  ? 'Paused: queued items remain in place, and current work may finish.'
                  : 'Pause New Jobs stops new queue starts without interrupting current work.'}
              </p>
            </div>
          )}
          {jobsView === 'history' && (
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex gap-1 rounded-xl border border-slate-700/70 bg-slate-950/60 p-1">
                {[
                  ['all', 'All'],
                  ['encode', 'Encodes'],
                  ['download', 'Downloads'],
                ].map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    aria-pressed={historyTypeFilter === value}
                    onClick={() => {
                      setHistoryTypeFilter(value);
                      setHistoryPage(1);
                    }}
                    className={`rounded-lg px-3 py-1.5 text-xs font-medium transition-all duration-200 ${
                      historyTypeFilter === value
                        ? 'bg-slate-800 text-slate-100 shadow-sm shadow-slate-950/40'
                        : 'text-slate-400 hover:bg-slate-800 hover:text-slate-100'
                    }`}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <TextInput
                aria-label="Search history"
                type="text"
                placeholder="Search history…"
                value={historySearch}
                onChange={(e) => {
                  setHistorySearch(e.target.value);
                  setHistoryPage(1);
                }}
                className="w-48 py-1.5 text-xs"
              />
              <SelectInput
                aria-label="Sort history"
                value={historySort}
                onChange={(e) => {
                  setHistorySort(e.target.value);
                  setHistoryPage(1);
                }}
                className="w-48 py-1.5 text-xs"
              >
                <option value="completed_desc">Completed (Newest)</option>
                <option value="year_newest">Release Year (Newest)</option>
                <option value="year_oldest">Release Year (Oldest)</option>
              </SelectInput>
              <Btn size="sm" variant="danger" disabled={totalHistoryCount === 0} onClick={onPurgeHistory}>Clear History ({totalHistoryCount})</Btn>
            </div>
          )}
        </div>
        </div>
        {/* Queue tab content */}
        {jobsView === 'queue' && (
          <>
            <div className="space-y-3 p-3 xl:hidden">
              {pagedJobs.length === 0 && (
                <div className="rounded-xl border border-slate-700/70 bg-slate-900/60 px-4 py-10 text-center text-sm text-slate-500">
                  {queueEmptyMessage}
                </div>
              )}
              {pagedJobs.map((item) => (
                item._itemType === 'download' ? (
                  <QueueDownloadCard
                    key={`dl-mobile-${item.id}`}
                    dj={item}
                    libName={libNameFor(item.library_id)}
                    actionPending={pendingDownloadActions[item.id]}
                    nowMs={nowMs}
                    onCancel={onCancelDownloadJob}
                    onRetry={onRetryDownloadJob}
                    onDelete={onDeleteDownloadJob}
                    onReview={onReviewDownloadJob}
                  />
                ) : (
                  <QueueEncodeCard
                    key={`job-mobile-${item.id}`}
                    job={item}
                    libName={libNameFor(item.library_id)}
                    actionPending={pendingJobActions[item.id]}
                    activeDj={downloadJobBySource[item.source_path]}
                    downloadEnabled={!!libraryProfiles[item.library_id]?.download_enabled}
                    onJobAction={onJobAction}
                  />
                )
              ))}
            </div>
            <div className="hidden xl:block">
              <table className="w-full table-fixed divide-y divide-slate-800">
                <thead className="sticky top-0 z-10 bg-slate-900/95 backdrop-blur-sm">
                  <tr>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 2xl:table-cell">ID</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Title</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Year</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Library</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Status</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 2xl:table-cell">Details</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 2xl:table-cell">Encoder</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Progress</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60">
                  {pagedJobs.length === 0 && (
                    <tr>
                      <td colSpan={9} className="px-4 py-10 text-center text-sm text-slate-500">
                        {queueEmptyMessage}
                      </td>
                    </tr>
                  )}
                  {pagedJobs.map((item) => (
                    item._itemType === 'download' ? (
                      <QueueDownloadRow
                        key={`dl-${item.id}`}
                        dj={item}
                        libName={libNameFor(item.library_id)}
                        actionPending={pendingDownloadActions[item.id]}
                        nowMs={nowMs}
                        onCancel={onCancelDownloadJob}
                        onRetry={onRetryDownloadJob}
                      onDelete={onDeleteDownloadJob}
                        onReview={onReviewDownloadJob}
                      />
                    ) : (
                      <QueueEncodeRow
                        key={item.id}
                        job={item}
                        libName={libNameFor(item.library_id)}
                        actionPending={pendingJobActions[item.id]}
                        activeDj={downloadJobBySource[item.source_path]}
                        downloadEnabled={!!libraryProfiles[item.library_id]?.download_enabled}
                        onJobAction={onJobAction}
                      />
                    )
                  ))}
                </tbody>
              </table>
            </div>
            {totalJobPages > 1 && (
              <div className="flex items-center justify-between border-t border-slate-700/70 px-5 py-3 text-sm text-slate-400">
                <p>Page {jobsPage} of {totalJobPages}</p>
                <div className="flex items-center gap-1">
                  {buildPaginationItems(jobsPage, totalJobPages).map((item) => (
                    typeof item === 'number' ? (
                      <button
                        key={item}
                        type="button"
                        aria-current={jobsPage === item ? 'page' : undefined}
                        aria-label={`Queue page ${item}`}
                        onClick={() => setJobsPage(item)}
                        className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all duration-150 ${jobsPage === item ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
                      >
                        {item}
                      </button>
                    ) : <span key={item} aria-hidden="true" className="px-1 text-slate-600">…</span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

        {/* History tab content */}
        {jobsView === 'history' && (
          <>
            <div className="space-y-3 p-3 xl:hidden">
              {pagedHistoryItems.length === 0 && (
                <div className="rounded-xl border border-slate-700/70 bg-slate-900/60 px-4 py-10 text-center text-sm text-slate-500">
                  {historySearch ? 'No matching history.' : 'No completed jobs yet.'}
                </div>
              )}
              {pagedHistoryItems.map((item) => (
                item._historyType === 'download' ? (
                  <HistoryDownloadCard
                    key={`hist-mobile-download-${item.id}`}
                    dj={item}
                    libName={libNameFor(item.library_id)}
                    actionPending={pendingDownloadActions[item.id]}
                    onRetry={onRetryDownloadJob}
                    onDelete={onDeleteDownloadJob}
                    onDeleteFile={onDeleteDownloadedFile}
                  />
                ) : (
                  <HistoryEncodeCard
                    key={`hist-mobile-encode-${item.id}`}
                    job={item}
                    libName={libNameFor(item.library_id)}
                    actionPending={pendingJobActions[item.id]}
                    downloadEnabled={!!libraryProfiles[item.library_id]?.download_enabled}
                    hasFallback={Boolean(fallbackHistoryByEncodeJobId[item.id])}
                    onJobAction={onJobAction}
                  />
                )
              ))}
            </div>
            <div className="hidden xl:block">
              <table className="w-full table-fixed divide-y divide-slate-800">
                <thead className="sticky top-0 z-10 bg-slate-900/95 backdrop-blur-sm">
                  <tr>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Type</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 2xl:table-cell">ID</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Title</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Year</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 lg:table-cell">Library</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Status</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 2xl:table-cell">Details</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 2xl:table-cell">Encoder</th>
                    <th className="hidden px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400 2xl:table-cell">Encode Time</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Completed</th>
                    <th className="px-4 py-3 text-left text-xs font-semibold uppercase tracking-wider text-slate-400">Actions</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-800/60">
                  {pagedHistoryItems.length === 0 && (
                    <tr>
                      <td colSpan={11} className="px-4 py-10 text-center text-sm text-slate-500">
                        {historySearch ? 'No matching history.' : 'No completed jobs yet.'}
                      </td>
                    </tr>
                  )}
                  {pagedHistoryItems.map((item) => (
                    item._historyType === 'download' ? (
                      <HistoryDownloadRow
                        key={`hist-download-${item.id}`}
                        dj={item}
                        libName={libNameFor(item.library_id)}
                        actionPending={pendingDownloadActions[item.id]}
                        onRetry={onRetryDownloadJob}
                        onDelete={onDeleteDownloadJob}
                        onDeleteFile={onDeleteDownloadedFile}
                      />
                    ) : (
                      <HistoryEncodeRow
                        key={`hist-encode-${item.id}`}
                        job={item}
                        libName={libNameFor(item.library_id)}
                        actionPending={pendingJobActions[item.id]}
                        downloadEnabled={!!libraryProfiles[item.library_id]?.download_enabled}
                        hasFallback={Boolean(fallbackHistoryByEncodeJobId[item.id])}
                        onJobAction={onJobAction}
                      />
                    )
                  ))}
                </tbody>
              </table>
            </div>
            {totalHistoryPages > 1 && (
              <div className="flex items-center justify-between border-t border-slate-700/70 px-5 py-3 text-sm text-slate-400">
                <p>Page {historyPage} of {totalHistoryPages}</p>
                <div className="flex items-center gap-1">
                  {buildPaginationItems(historyPage, totalHistoryPages).map((item) => (
                    typeof item === 'number' ? (
                      <button
                        key={item}
                        type="button"
                        aria-current={historyPage === item ? 'page' : undefined}
                        aria-label={`History page ${item}`}
                        onClick={() => setHistoryPage(item)}
                        className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all duration-150 ${historyPage === item ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
                      >
                        {item}
                      </button>
                    ) : <span key={item} aria-hidden="true" className="px-1 text-slate-600">…</span>
                  ))}
                </div>
              </div>
            )}
          </>
        )}

      </div>

    </section>
  );
}

export default memo(JobsPage);
