import { useEffect, useId, useRef, useState } from 'react';

import { fetchDirs } from '../api';

export function SectionCard({ children, className = '' }) {
  return (
    <div className={`relative min-w-0 overflow-hidden rounded-2xl border border-slate-700/70 bg-slate-900/72 p-5 shadow-xl shadow-slate-950/45 backdrop-blur-sm ${className}`}>
      <div className="pointer-events-none absolute inset-0 bg-gradient-to-b from-white/[0.03] via-transparent to-transparent" />
      {children}
    </div>
  );
}

export function SectionTitle({ children, className = '' }) {
  return (
    <h2 className={`mb-4 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.2em] text-slate-400 ${className}`}>
      <span className="inline-block h-1.5 w-1.5 rounded-full bg-cyan-300/80" />
      {children}
    </h2>
  );
}


export function SettingsSectionCard({ title, open, onToggle, children, dirty = false }) {
  const contentId = useId();

  return (
    <SectionCard className={!open ? 'p-0' : ''}>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={onToggle}
        className={`relative flex w-full items-center justify-between gap-4 px-5 text-left transition-colors hover:bg-slate-800/40 ${open ? 'pb-4' : 'py-5'}`}
      >
        <div className="flex min-w-0 items-center gap-2">
          <SectionTitle className="mb-0">{title}</SectionTitle>
          {dirty && (
            <span className="rounded-full border border-amber-500/40 bg-amber-950/40 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
              Unsaved
            </span>
          )}
        </div>
        <span className="flex shrink-0 items-center gap-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
          {open ? 'Collapse' : 'Expand'}
          <span className={`inline-block transition-transform duration-200 ${open ? 'rotate-180' : ''}`} aria-hidden="true">⌄</span>
        </span>
      </button>
      {open && (
        <div id={contentId} className="relative px-5 pb-5">
          {children}
        </div>
      )}
    </SectionCard>
  );
}

export function CollapsibleSection({ title, open, onToggle, children, divider = false }) {
  const contentId = useId();
  return (
    <div>
      {divider && <hr className="mb-5 border-slate-800" />}
      <button
        type="button"
        aria-expanded={open}
        aria-controls={contentId}
        onClick={onToggle}
        className="mb-4 flex w-full items-center justify-between rounded-xl border border-slate-800/70 bg-slate-950/25 px-4 py-3 text-left transition-colors hover:bg-slate-950/40"
      >
        <SectionTitle>{title}</SectionTitle>
        <span className="text-sm text-slate-400">{open ? 'Hide' : 'Show'}</span>
      </button>
      {open && <div id={contentId}>{children}</div>}
    </div>
  );
}

export function FormField({ label, hint, error, children, span2 = false }) {
  return (
    <label className={`flex flex-col gap-1.5 ${span2 ? 'md:col-span-2' : ''}`}>
      <span className="text-sm font-medium text-slate-200">{label}</span>
      {hint && <p className="break-words text-xs text-slate-500">{hint}</p>}
      {children}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </label>
  );
}

export function TextInput({ className = '', ...props }) {
  return (
    <input
      className={`w-full rounded-xl border border-slate-600/80 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 placeholder-slate-500/90 shadow-inner shadow-black/20 outline-none transition-all duration-150 focus:border-cyan-400/70 focus:bg-slate-900 ${className}`}
      {...props}
    />
  );
}

export function SelectInput({ children, className = '', ...props }) {
  return (
    <select
      className={`w-full rounded-xl border border-slate-600/80 bg-slate-900/80 px-3 py-2 text-sm text-slate-100 shadow-inner shadow-black/20 outline-none transition-all duration-150 focus:border-cyan-400/70 focus:bg-slate-900 ${className}`}
      {...props}
    >
      {children}
    </select>
  );
}

export function Btn({ variant = 'primary', size = 'md', className = '', children, ...props }) {
  const base = 'inline-flex items-center justify-center rounded-xl font-semibold transition-all duration-150 focus:outline-none focus:ring-2 focus:ring-offset-1 focus:ring-offset-slate-950 disabled:opacity-50 disabled:cursor-not-allowed active:scale-[0.98]';
  const sizes = {
    sm: 'px-3 py-1.5 text-xs',
    md: 'px-4 py-2 text-sm',
    lg: 'px-5 py-2.5 text-sm',
  };
  const variants = {
    primary: 'bg-gradient-to-r from-cyan-400 to-sky-400 text-slate-950 shadow-lg shadow-cyan-950/40 hover:from-cyan-300 hover:to-sky-300 focus:ring-cyan-400',
    danger: 'bg-gradient-to-r from-rose-600 to-red-500 text-white shadow-lg shadow-rose-950/45 hover:from-rose-500 hover:to-red-400 focus:ring-rose-500',
    warning: 'bg-gradient-to-r from-amber-400 to-orange-400 text-slate-950 shadow-lg shadow-amber-950/40 hover:from-amber-300 hover:to-orange-300 focus:ring-amber-400',
    success: 'bg-gradient-to-r from-emerald-400 to-teal-400 text-slate-950 shadow-lg shadow-emerald-950/40 hover:from-emerald-300 hover:to-teal-300 focus:ring-emerald-400',
    secondary: 'border border-slate-600/80 bg-slate-800/85 text-slate-100 hover:border-slate-500 hover:bg-slate-700/85 focus:ring-slate-500',
    violet: 'bg-gradient-to-r from-indigo-400 to-cyan-400 text-slate-950 shadow-lg shadow-indigo-950/40 hover:from-indigo-300 hover:to-cyan-300 focus:ring-indigo-400',
    indigo: 'bg-gradient-to-r from-blue-400 to-indigo-400 text-slate-950 shadow-lg shadow-indigo-950/40 hover:from-blue-300 hover:to-indigo-300 focus:ring-indigo-400',
  };
  return (
    <button type="button" className={`${base} ${sizes[size]} ${variants[variant]} ${className}`} {...props}>
      {children}
    </button>
  );
}

export function Modal({ open, title, onClose, children }) {
  const titleId = useId();
  const dialogRef = useRef(null);
  const closeRef = useRef(onClose);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return undefined;
    const previouslyFocused = document.activeElement;
    function onKeyDown(event) {
      if (event.key === 'Escape') {
        closeRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = dialogRef.current?.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener('keydown', onKeyDown);
    const focusTimer = window.setTimeout(() => dialogRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener('keydown', onKeyDown);
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
    };
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4">
      <button type="button" tabIndex={-1} aria-hidden="true" className="absolute inset-0 bg-slate-950/85" onClick={onClose} />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="relative z-10 w-full max-w-md rounded-2xl border border-slate-700 bg-slate-900 p-5 shadow-2xl shadow-black/50"
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <h3 id={titleId} className="text-sm font-semibold tracking-wide text-slate-100">{title}</h3>
          <Btn variant="secondary" size="sm" onClick={onClose}>Close</Btn>
        </div>
        {children}
      </div>
    </div>
  );
}

export function MobileActionMenu({ children, label = 'Actions' }) {
  const [open, setOpen] = useState(false);
  const menuId = useId();
  const containerRef = useRef(null);
  const menuPanelRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;

    function handlePointerDown(event) {
      if (!containerRef.current?.contains(event.target)) {
        setOpen(false);
      }
    }

    function handleEscape(event) {
      if (event.key === 'Escape') {
        setOpen(false);
      }
    }

    const focusTimer = window.setTimeout(() => {
      const firstAction = menuPanelRef.current?.querySelector('button');
      if (firstAction instanceof HTMLElement) firstAction.focus();
    }, 0);

    document.addEventListener('pointerdown', handlePointerDown);
    document.addEventListener('keydown', handleEscape);
    return () => {
      window.clearTimeout(focusTimer);
      document.removeEventListener('pointerdown', handlePointerDown);
      document.removeEventListener('keydown', handleEscape);
    };
  }, [open]);

  return (
    <details
      ref={containerRef}
      open={open}
      className="group mt-3"
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary
        aria-controls={menuId}
        aria-expanded={open}
        aria-label={`${label} menu`}
        className="flex cursor-pointer list-none items-center justify-between rounded-lg border border-slate-700/80 bg-slate-800/70 px-3 py-1.5 text-xs font-medium text-slate-200 transition-colors hover:bg-slate-700/80 [&::-webkit-details-marker]:hidden"
      >
        {label}
        <span className="text-slate-400 transition-transform duration-150 group-open:rotate-180">▾</span>
      </summary>
      <div
        id={menuId}
        ref={menuPanelRef}
        role="group"
        aria-label={`${label} options`}
        className="mt-2 flex flex-wrap gap-1.5 rounded-lg border border-slate-700/70 bg-slate-900/70 p-2"
        onClickCapture={(event) => {
          if (event.target.closest('button')) setOpen(false);
        }}
      >
        {children}
      </div>
    </details>
  );
}

export function FallbackIndicator() {
  return (
    <span
      title="Fallback route used. Download was attempted first; encode completed the job."
      aria-label="Fallback route used"
      className="inline-flex h-4 w-4 items-center justify-center rounded-full border border-amber-500/30 bg-amber-500/10 text-amber-300"
    >
      <svg viewBox="0 0 16 16" className="h-2.5 w-2.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d="M3.5 4.5h4a2 2 0 0 1 2 2v5" />
        <path d="M7.5 8.5 9.5 10.5 11.5 8.5" />
        <path d="M3.5 11.5h3" />
      </svg>
    </span>
  );
}

export function HistoryTypeBadge({ type }) {
  const isEncode = type === 'encode';
  return (
    <span className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-[0.14em] ${
      isEncode
        ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300'
        : 'border-indigo-500/30 bg-indigo-500/10 text-indigo-300'
    }`}
    >
      {isEncode ? 'Encode' : 'Download'}
    </span>
  );
}

export function Toggle({ checked, onChange, label, ariaLabel }) {
  return (
    <label className="flex cursor-pointer items-center gap-3">
      <div className="relative">
        <input type="checkbox" aria-label={ariaLabel || label} className="sr-only" checked={checked} onChange={onChange} />
        <div className={`h-6 w-11 rounded-full border transition-colors duration-200 ${checked ? 'border-cyan-400/70 bg-cyan-500/40' : 'border-slate-600 bg-slate-700/80'}`} />
        <div className={`absolute top-1 left-1 h-4 w-4 rounded-full bg-white shadow transition-transform duration-200 ${checked ? 'translate-x-5' : 'translate-x-0'}`} />
      </div>
      {label && <span className="text-sm text-slate-200">{label}</span>}
    </label>
  );
}

export function StatusDot({ status }) {
  const colors = {
    online: 'bg-emerald-400 shadow-emerald-400/50',
    reconnecting: 'bg-amber-400 shadow-amber-400/50',
    offline: 'bg-red-400 shadow-red-400/50',
    connecting: 'bg-slate-400 shadow-slate-400/50',
  };
  const labels = {
    online: 'Live',
    reconnecting: 'Reconnecting',
    offline: 'Offline (polling)',
    connecting: 'Connecting',
  };
  return (
    <div role="status" aria-live="polite" aria-label={`Connection status: ${labels[status] ?? status}`} className="flex items-center gap-2 rounded-full border border-slate-700/70 bg-slate-900/70 px-2.5 py-1">
      <span className={`inline-block h-2 w-2 rounded-full shadow-sm ${colors[status] ?? colors.connecting} ${status === 'online' ? 'animate-pulse' : ''}`} />
      <span className="text-xs text-slate-400">{labels[status] ?? status}</span>
    </div>
  );
}

export function DirBrowserModal({ open, initialPath, onSelect, onClose }) {
  const titleId = useId();
  const dialogRef = useRef(null);
  const closeRef = useRef(onClose);
  const [current, setCurrent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    closeRef.current = onClose;
  }, [onClose]);

  useEffect(() => {
    if (!open) return undefined;
    const previouslyFocused = document.activeElement;
    function onKeyDown(event) {
      if (event.key === 'Escape') {
        closeRef.current();
        return;
      }
      if (event.key !== 'Tab') return;
      const focusable = dialogRef.current?.querySelectorAll(
        'button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      );
      if (!focusable?.length) {
        event.preventDefault();
        dialogRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === dialogRef.current)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener('keydown', onKeyDown);
    const focusTimer = window.setTimeout(() => dialogRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(focusTimer);
      window.removeEventListener('keydown', onKeyDown);
      if (previouslyFocused instanceof HTMLElement) previouslyFocused.focus();
    };
  }, [open]);

  async function navigate(path) {
    setLoading(true);
    setError(null);
    try {
      const data = await fetchDirs(path ?? undefined);
      setCurrent(data);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    if (!open) return;
    // Try the saved path first; if it's outside MEDIA_ROOT or doesn't exist, fall back to root
    if (initialPath) {
      fetchDirs(initialPath)
        .then((data) => { setCurrent(data); setError(null); })
        .catch(() => navigate(null));
    } else {
      navigate(null);
    }
  }, [open]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onClose}>
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="w-full max-w-md rounded-xl border border-slate-700 bg-slate-900 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <h3 id={titleId} className="text-sm font-semibold text-slate-200">Browse Directories</h3>
          <button type="button" aria-label="Close directory browser" className="text-slate-400 hover:text-slate-200 text-lg leading-none" onClick={onClose}>✕</button>
        </div>

        {/* Current path */}
        <div className="flex items-center gap-2 border-b border-slate-800/60 px-4 py-2">
          {current?.parent && (
            <button type="button" onClick={() => navigate(current.parent)} className="shrink-0 rounded px-2 py-1 text-xs text-slate-400 hover:bg-slate-800 hover:text-slate-200">
              ← Up
            </button>
          )}
          <span className="min-w-0 truncate font-mono text-xs text-slate-400" title={current?.path}>{current?.path ?? '…'}</span>
        </div>

        {/* Directory listing */}
        <div className="max-h-72 overflow-y-auto px-2 py-2">
          {loading && <p role="status" className="px-2 py-3 text-sm text-slate-400">Loading…</p>}
          {error && <p role="alert" className="px-2 py-3 text-sm text-red-400">{error}</p>}
          {!loading && !error && current && current.dirs.length === 0 && (
            <p className="px-2 py-3 text-xs text-slate-500">No subdirectories here.</p>
          )}
          {!loading && !error && current && current.dirs.map((dir) => (
            <button
              key={dir}
              type="button"
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-sm text-slate-100 hover:bg-slate-800 active:bg-slate-700"
              onClick={() => navigate(`${current.path}/${dir}`)}
            >
              <svg className="h-4 w-4 shrink-0 text-cyan-400" fill="currentColor" viewBox="0 0 20 20"><path d="M2 6a2 2 0 012-2h5l2 2h5a2 2 0 012 2v6a2 2 0 01-2 2H4a2 2 0 01-2-2V6z"/></svg>
              {dir}
            </button>
          ))}
        </div>

        {/* Footer */}
        <div className="flex items-center justify-end gap-2 border-t border-slate-800 px-4 py-3">
          <Btn variant="secondary" size="sm" onClick={onClose}>Cancel</Btn>
          <Btn variant="primary" size="sm" disabled={!current} onClick={() => { onSelect(current.path); onClose(); }}>
            Select This Folder
          </Btn>
        </div>
      </div>
    </div>
  );
}
