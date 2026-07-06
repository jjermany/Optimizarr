import { memo, useEffect, useMemo, useState } from 'react';

import { clearLogs } from '../api';
import {
  LOGS_PAGE_SIZE,
  formatLogCreatedAt,
  formatLogDetailValue,
  formatLogEventType,
  logMatchesSearch,
  logSeverityRowClass,
  logSeverityTextClass,
} from '../lib/appUtils';
import { Btn, SectionCard, SectionTitle, SelectInput, TextInput } from './ui';

function LogsPage({ logs, loadingLogs, onRefresh, setLogs, pushToast, onAuthError }) {
  const [logsPage, setLogsPage] = useState(1);
  const [logSearch, setLogSearch] = useState('');
  const [logEventFilter, setLogEventFilter] = useState('all');
  const [logSeverityFilter, setLogSeverityFilter] = useState('all');
  const [clearingLogs, setClearingLogs] = useState(false);

  const logEventOptions = useMemo(() => (
    [...new Set(logs.map((log) => String(log.event_type ?? '')).filter(Boolean))]
      .sort((a, b) => formatLogEventType(a).localeCompare(formatLogEventType(b)))
  ), [logs]);

  const logSeverityOptions = useMemo(() => (
    [...new Set(logs.map((log) => String(log.severity ?? 'info').toLowerCase()).filter(Boolean))]
      .sort()
  ), [logs]);

  const filteredLogs = useMemo(() => logs.filter((log) => {
    const severity = String(log.severity ?? 'info').toLowerCase();
    if (logEventFilter !== 'all' && log.event_type !== logEventFilter) return false;
    if (logSeverityFilter !== 'all' && severity !== logSeverityFilter) return false;
    return logMatchesSearch(log, logSearch);
  }), [logs, logEventFilter, logSeverityFilter, logSearch]);

  const totalLogPages = useMemo(
    () => Math.max(1, Math.ceil(filteredLogs.length / LOGS_PAGE_SIZE)),
    [filteredLogs.length],
  );

  const pagedLogs = useMemo(() => {
    const start = (logsPage - 1) * LOGS_PAGE_SIZE;
    return filteredLogs.slice(start, start + LOGS_PAGE_SIZE);
  }, [filteredLogs, logsPage]);

  useEffect(() => {
    if (logsPage > totalLogPages) setLogsPage(totalLogPages);
  }, [logsPage, totalLogPages]);

  async function handleClearLogs() {
    if (clearingLogs) return;
    setClearingLogs(true);
    try {
      const result = await clearLogs();
      setLogs([]);
      setLogsPage(1);
      pushToast(`Cleared ${result?.deleted_logs ?? 0} log events.`, 'success');
    } catch (clearError) {
      if (clearError.status === 401) {
        await onAuthError();
        return;
      }
      pushToast(clearError.message || 'Could not clear logs.', 'error');
    } finally {
      setClearingLogs(false);
    }
  }

  return (
    <section className="animate-fade-in space-y-5">
      <SectionCard>
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <SectionTitle>Logs</SectionTitle>
          <div className="flex flex-wrap items-center gap-2">
            <Btn variant="secondary" size="sm" disabled={loadingLogs || clearingLogs} onClick={onRefresh}>
              {loadingLogs ? 'Refreshing...' : 'Refresh'}
            </Btn>
            <Btn variant="danger" size="sm" disabled={clearingLogs || logs.length === 0} onClick={handleClearLogs}>
              {clearingLogs ? 'Clearing...' : 'Clear logs'}
            </Btn>
          </div>
        </div>

        <div className="mb-4 grid gap-3 md:grid-cols-[minmax(0,1.6fr)_minmax(0,1fr)_minmax(0,1fr)]">
          <TextInput
            type="search"
            value={logSearch}
            onChange={(event) => { setLogSearch(event.target.value); setLogsPage(1); }}
            placeholder="Search logs"
          />
          <SelectInput value={logEventFilter} onChange={(event) => { setLogEventFilter(event.target.value); setLogsPage(1); }}>
            <option value="all">All events</option>
            {logEventOptions.map((eventType) => (
              <option key={eventType} value={eventType}>{formatLogEventType(eventType)}</option>
            ))}
          </SelectInput>
          <SelectInput value={logSeverityFilter} onChange={(event) => { setLogSeverityFilter(event.target.value); setLogsPage(1); }}>
            <option value="all">All severities</option>
            {logSeverityOptions.map((severity) => (
              <option key={severity} value={severity}>{severity}</option>
            ))}
          </SelectInput>
        </div>

        <div className="overflow-hidden rounded-lg border border-slate-800/80 bg-slate-950/55 shadow-inner shadow-black/30">
          <div className="grid grid-cols-[9rem_5.5rem_minmax(8rem,12rem)_minmax(0,1fr)] items-center border-b border-slate-800/80 bg-slate-950/80 px-3 py-2 font-mono text-[11px] uppercase text-slate-500 max-lg:hidden">
            <span>Time</span>
            <span>Level</span>
            <span>Event</span>
            <span>Message</span>
          </div>

          {logs.length === 0 && (
            <p className="px-4 py-5 text-sm text-slate-500">No logged events yet.</p>
          )}
          {logs.length > 0 && filteredLogs.length === 0 && (
            <p className="px-4 py-5 text-sm text-slate-500">No events match the current filters.</p>
          )}

          {pagedLogs.length > 0 && (
            <div className="max-h-[62vh] overflow-auto">
              {pagedLogs.map((log) => {
                const detailEntries = Object.entries(log.details ?? {})
                  .filter(([, value]) => value !== undefined && value !== null && value !== '');
                const severity = String(log.severity || 'info').toLowerCase();
                return (
                  <div
                    key={log.id}
                    className={`border-l-2 border-b border-slate-900/90 px-3 py-2.5 last:border-b-0 ${logSeverityRowClass(log.severity)}`}
                  >
                    <div className="grid gap-2 lg:grid-cols-[9rem_5.5rem_minmax(8rem,12rem)_minmax(0,1fr)] lg:items-start">
                      <time className="font-mono text-[12px] leading-5 text-slate-500">
                        {formatLogCreatedAt(log.created_at)}
                      </time>
                      <span className={`font-mono text-[12px] font-semibold uppercase leading-5 ${logSeverityTextClass(log.severity)}`}>
                        {severity}
                      </span>
                      <span className="truncate font-mono text-[12px] leading-5 text-slate-400" title={formatLogEventType(log.event_type)}>
                        {formatLogEventType(log.event_type)}
                      </span>
                      <div className="min-w-0">
                        <p className="break-words text-sm leading-5 text-slate-100">{log.message}</p>
                        {detailEntries.length > 0 && (
                          <dl className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 font-mono text-[11px] leading-5 text-slate-500">
                            {detailEntries.map(([key, value]) => (
                              <div key={key} className="min-w-0 max-w-full">
                                <dt className="inline text-slate-600">{key}=</dt>
                                <dd className="inline break-all text-slate-400" title={formatLogDetailValue(value)}>
                                  {formatLogDetailValue(value)}
                                </dd>
                              </div>
                            ))}
                          </dl>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>

        <p className="mt-3 text-xs text-slate-500">
          Showing {pagedLogs.length} of {filteredLogs.length} matching events ({logs.length} total)
        </p>

        {filteredLogs.length > LOGS_PAGE_SIZE && (
          <div className="flex items-center justify-between border-t border-slate-700/70 pt-3 text-sm text-slate-400">
            <p>Page {logsPage} of {totalLogPages}</p>
            <div className="flex flex-wrap items-center gap-1">
              {Array.from({ length: totalLogPages }, (_, i) => i + 1).map((pageNum) => (
                <button
                  key={pageNum}
                  type="button"
                  onClick={() => setLogsPage(pageNum)}
                  className={`rounded-lg px-2.5 py-1 text-xs font-medium transition-all duration-150 ${logsPage === pageNum ? 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950' : 'bg-slate-800 text-slate-300 hover:bg-slate-700'}`}
                >
                  {pageNum}
                </button>
              ))}
            </div>
          </div>
        )}
      </SectionCard>
    </section>
  );
}

export default memo(LogsPage);
