import React, {createContext, useCallback, useContext, useEffect, useId, useRef, useState} from 'react';
import {createPortal} from 'react-dom';

export type IconName =
  | 'overview' | 'procedures' | 'runs' | 'recall' | 'key' | 'connect' | 'workspace' | 'arrow' | 'logout'
  | 'copy' | 'check' | 'chevron' | 'down' | 'terminal' | 'eye' | 'close' | 'search' | 'refresh' | 'sun'
  | 'moon' | 'menu' | 'trash' | 'archive' | 'shield' | 'alert' | 'clock' | 'bolt' | 'plus' | 'command' | 'lock' | 'flag' | 'graph';

const shapes: Record<IconName, React.ReactNode> = {
  overview: <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/></>,
  procedures: <><path d="M5 6h14M5 12h14M5 18h9"/><circle cx="19" cy="18" r="1.5"/></>,
  runs: <><path d="M4 12h3l2-6 4 12 2-6h5"/></>,
  recall: <><circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2M11 8v6M8 11h6"/></>,
  key: <><circle cx="8" cy="15" r="4"/><path d="m11 12 8-8m-3 3 3 3m-6 0 2 2"/></>,
  connect: <><path d="M9 7V3m6 4V3M6 7h12v4a6 6 0 0 1-12 0V7Zm6 10v4"/></>,
  workspace: <><path d="M4 20V8l8-4 8 4v12M9 20v-6h6v6"/></>,
  arrow: <path d="M5 12h14m-6-6 6 6-6 6"/>,
  logout: <path d="M9 4H5v16h4m4-8h8m-3-3 3 3-3 3"/>,
  copy: <><rect x="8.5" y="8.5" width="11" height="11" rx="2"/><path d="M15.5 8.5V5a1.5 1.5 0 0 0-1.5-1.5H5A1.5 1.5 0 0 0 3.5 5v9A1.5 1.5 0 0 0 5 15.5h3.5"/></>,
  check: <path d="m5 12.5 4.5 4.5L19 7.5"/>,
  chevron: <path d="m9 5 7 7-7 7"/>,
  down: <path d="m6 9 6 6 6-6"/>,
  terminal: <><rect x="3" y="4.5" width="18" height="15" rx="2.5"/><path d="m7 9.5 3 2.5-3 2.5m6 0h4"/></>,
  eye: <><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/></>,
  close: <path d="m6.5 6.5 11 11m0-11-11 11"/>,
  search: <><circle cx="11" cy="11" r="6.5"/><path d="m20 20-4.2-4.2"/></>,
  refresh: <><path d="M20 11a8 8 0 0 0-14.5-4.5M4 4v4h4M4 13a8 8 0 0 0 14.5 4.5M20 20v-4h-4"/></>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 2.5v2m0 15v2m9.5-9.5h-2m-15 0h-2m16.2-6.7-1.4 1.4M6.7 17.3l-1.4 1.4m0-13.4 1.4 1.4m10.6 10.6 1.4 1.4"/></>,
  menu: <path d="M4 7h16M4 12h16M4 17h10"/>,
  moon: <path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>,
  trash: <path d="M4.5 7h15M10 11v6m4-6v6M6 7l1 12.5h10L18 7M9 7V4.5h6V7"/>,
  archive: <><rect x="3.5" y="4" width="17" height="4.5" rx="1"/><path d="M5 8.5V20h14V8.5M10 12.5h4"/></>,
  shield: <><path d="m12 3 7.5 3v5.5c0 4.5-3.2 8-7.5 9.5-4.3-1.5-7.5-5-7.5-9.5V6L12 3Z"/><path d="m9 12 2 2 4-4"/></>,
  alert: <><path d="M12 4 2.5 20h19L12 4Z"/><path d="M12 10v4.5m0 2.5v.01"/></>,
  clock: <><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></>,
  bolt: <path d="M13 3 5 13.5h6L10 21l8-10.5h-6L13 3Z"/>,
  plus: <path d="M12 5v14M5 12h14"/>,
  command: <path d="M9 9V6.5A2.5 2.5 0 1 0 6.5 9H9Zm0 0h6m-6 0v6m6-6V6.5A2.5 2.5 0 1 1 17.5 9H15Zm0 0v6m0 0h-6m6 0v2.5a2.5 2.5 0 1 0 2.5-2.5H15Zm-6 0v2.5A2.5 2.5 0 1 1 6.5 15H9Z"/>,
  lock: <><rect x="5" y="10.5" width="14" height="10" rx="2"/><path d="M8 10.5V7.5a4 4 0 0 1 8 0v3"/></>,
  flag: <path d="M5 21V4.5m0 0h11l-2 4 2 4H5"/>,
  graph: <><circle cx="6" cy="7" r="2.5"/><circle cx="18" cy="6" r="2"/><circle cx="15" cy="18" r="2.5"/><path d="m8.3 7.9 4.6 8.2m-4.2-9.1 7.3-.8m.8 2.2-1 7"/></>,
};

export function Icon({name, size = 16}: {name: IconName; size?: number}) {
  return <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{shapes[name]}</svg>;
}

export function Logo({height = 14}: {height?: number}) {
  return <span className="logo" aria-hidden="true" style={{height, width: height * 360 / 168}}/>;
}

export function Kbd({children}: {children: React.ReactNode}) {
  return <kbd className="kbd">{children}</kbd>;
}

const statusTone: Record<string, string> = {
  active: 'good', success: 'good', validated: 'good',
  provisional: 'warn', recording: 'info',
  flagged: 'bad', failure: 'bad', revoked: 'bad',
  retired: 'neutral',
};
const statusLabel: Record<string, string> = {recording: 'Recording', success: 'Succeeded', failure: 'Failed'};

export function Status({value}: {value: string}) {
  const label = statusLabel[value] ?? value.charAt(0).toUpperCase() + value.slice(1);
  return <span className={`status tone-${statusTone[value] ?? 'neutral'}`}><span className="status-dot"/>{label}</span>;
}

export function Badge({label, tone = 'neutral', title}: {label: string; tone?: string; title?: string}) {
  return <span className={`status tone-${tone}`} title={title}><span className="status-dot"/>{label}</span>;
}

export function Chip({children, tone = 'neutral', title}: {children: React.ReactNode; tone?: string; title?: string}) {
  return <span className={`chip tone-${tone}`} title={title}>{children}</span>;
}

export function useCopy(timeout = 1600) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = useCallback(async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true); clearTimeout(timer.current); timer.current = setTimeout(() => setCopied(false), timeout);
      return true;
    } catch {return false;}
  }, [timeout]);
  return {copied, copy};
}

export function CopyText({value, display, label = 'Copy'}: {value: string; display?: string; label?: string}) {
  const {copied, copy} = useCopy();
  return <button type="button" className="copy-text" onClick={e => {e.stopPropagation(); void copy(value);}} title={copied ? 'Copied' : `${label}: ${value}`} aria-label={`${label} ${value}`}>
    <code>{display ?? value}</code><Icon name={copied ? 'check' : 'copy'} size={13}/>
  </button>;
}

export function CodeBlock({label, children}: {label: string; children: string}) {
  const {copied, copy} = useCopy();
  const [failed, setFailed] = useState(false);
  return <div className="code-block">
    <div className="code-head"><span>{label}</span><button type="button" className="ghost-button small" onClick={async () => setFailed(!(await copy(children)))} aria-label={`Copy ${label}`}><Icon name={copied ? 'check' : 'copy'} size={13}/>{copied ? 'Copied' : 'Copy'}</button></div>
    <pre>{children}</pre>
    {failed && <p role="status" className="code-error">Clipboard isn’t available here. Select the text and copy it.</p>}
  </div>;
}

export function Empty({icon, title, children, action, compact}: {icon: IconName; title: string; children?: React.ReactNode; action?: React.ReactNode; compact?: boolean}) {
  return <div className={`empty ${compact ? 'compact' : ''}`}>
    <span className="empty-icon"><Icon name={icon} size={compact ? 18 : 22}/></span>
    <h3>{title}</h3>{children && <p>{children}</p>}{action}
  </div>;
}

export function Skeleton({rows = 4}: {rows?: number}) {
  return <div className="skeleton" aria-hidden="true">{Array.from({length: rows}, (_, i) => <div key={i} className="skeleton-row" style={{animationDelay: `${i * 60}ms`}}><span/><span/><span/></div>)}</div>;
}

/** A shimmering block for layouts that aren't lists (hero lines, tiles). */
export function Shimmer({width = '100%', height = 14, radius = 6}: {width?: number | string; height?: number; radius?: number}) {
  return <span className="shimmer" aria-hidden="true" style={{width, height, borderRadius: radius}}/>;
}

/** Counts up to a number once, easing out; respects reduced-motion settings. */
export function CountUp({value, duration = 700}: {value: number; duration?: number}) {
  const [shown, setShown] = useState(0);
  useEffect(() => {
    if (window.matchMedia('(prefers-reduced-motion: reduce)').matches || value <= 0) {setShown(value); return;}
    let frame = 0;
    const start = performance.now();
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration);
      setShown(Math.round(value * (1 - Math.pow(1 - t, 3))));
      if (t < 1) frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [value, duration]);
  return <>{shown.toLocaleString()}</>;
}

export function SearchField({value, onChange, placeholder, inputRef}: {value: string; onChange: (v: string) => void; placeholder: string; inputRef?: React.Ref<HTMLInputElement>}) {
  return <label className="search-field"><Icon name="search" size={15}/><input ref={inputRef} value={value} placeholder={placeholder} onChange={e => onChange(e.target.value)} aria-label={placeholder}/>{value && <button type="button" className="icon-button tiny" aria-label="Clear search" onClick={() => onChange('')}><Icon name="close" size={13}/></button>}</label>;
}

export function Segmented<T extends string>({options, value, onChange, label}: {options: Array<{value: T; label: string; count?: number}>; value: T; onChange: (v: T) => void; label: string}) {
  return <div className="segmented" role="tablist" aria-label={label}>
    {options.map(o => <button key={o.value} type="button" role="tab" aria-selected={value === o.value} onClick={() => onChange(o.value)}>{o.label}{o.count !== undefined && <span className="count">{o.count}</span>}</button>)}
  </div>;
}

export function Meter({value, tone = 'accent', label}: {value: number; tone?: string; label: string}) {
  const pct = Math.max(0, Math.min(1, value)) * 100;
  return <span className={`meter tone-${tone}`} role="meter" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(pct)} aria-label={label}><span style={{width: `${pct}%`}}/></span>;
}

export function Sheet({open, onClose, children, label}: {open: boolean; onClose: () => void; children: React.ReactNode; label: string}) {
  const ref = useRef<HTMLDivElement>(null);
  // Stay mounted briefly after closing so the panel can slide out.
  const [mounted, setMounted] = useState(open);
  const [closing, setClosing] = useState(false);
  useEffect(() => {
    if (open) {setMounted(true); setClosing(false); return;}
    if (!mounted) return;
    setClosing(true);
    const timer = setTimeout(() => {setMounted(false); setClosing(false);}, 200);
    return () => clearTimeout(timer);
  }, [open]);
  useEffect(() => {
    if (!open) return;
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.focus();
    const onKey = (e: KeyboardEvent) => {if (e.key === 'Escape' && !document.querySelector('.dialog-backdrop')) onClose();};
    window.addEventListener('keydown', onKey);
    document.body.classList.add('locked');
    return () => {window.removeEventListener('keydown', onKey); document.body.classList.remove('locked'); previous?.focus?.();};
  }, [open, onClose]);
  if (!mounted) return null;
  // Render at the document root: animated page containers would otherwise trap
  // this fixed layer inside their own box.
  return createPortal(<div className={`sheet-layer ${closing ? 'closing' : ''}`}>
    <div className="sheet-backdrop" onClick={onClose}/>
    <aside className="sheet" role="dialog" aria-modal="true" aria-label={label} tabIndex={-1} ref={ref}>{children}</aside>
  </div>, document.body);
}

type ConfirmOptions = {title: string; body: React.ReactNode; confirm: string; danger?: boolean};
type ToastItem = {id: number; message: string; tone: 'good' | 'bad'};
type Feedback = {confirm: (options: ConfirmOptions) => Promise<boolean>; toast: (message: string, tone?: 'good' | 'bad') => void};
const FeedbackContext = createContext<Feedback>({confirm: async () => false, toast: () => {}});
export const useFeedback = () => useContext(FeedbackContext);

export function FeedbackProvider({children}: {children: React.ReactNode}) {
  const [dialog, setDialog] = useState<ConfirmOptions & {resolve: (ok: boolean) => void}>();
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const titleId = useId();
  const confirmRef = useRef<HTMLButtonElement>(null);
  const confirm = useCallback((options: ConfirmOptions) => new Promise<boolean>(resolve => setDialog({...options, resolve})), []);
  const toast = useCallback((message: string, tone: 'good' | 'bad' = 'good') => {
    const id = Date.now() + Math.random();
    setToasts(list => [...list, {id, message, tone}]);
    setTimeout(() => setToasts(list => list.filter(t => t.id !== id)), 3200);
  }, []);
  const close = (ok: boolean) => {dialog?.resolve(ok); setDialog(undefined);};
  useEffect(() => {
    if (!dialog) return;
    confirmRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {if (e.key === 'Escape') {e.stopPropagation(); close(false);}};
    window.addEventListener('keydown', onKey, true);
    return () => window.removeEventListener('keydown', onKey, true);
  });
  return <FeedbackContext.Provider value={{confirm, toast}}>
    {children}
    {dialog && <div className="dialog-backdrop" onClick={() => close(false)}>
      <div className="dialog" role="alertdialog" aria-modal="true" aria-labelledby={titleId} onClick={e => e.stopPropagation()}>
        <h2 id={titleId}>{dialog.title}</h2><div className="dialog-body">{dialog.body}</div>
        <div className="dialog-actions"><button type="button" className="secondary-button" onClick={() => close(false)}>Cancel</button><button ref={confirmRef} type="button" className={dialog.danger ? 'danger-button' : 'primary-button'} onClick={() => close(true)}>{dialog.confirm}</button></div>
      </div>
    </div>}
    <div className="toasts" role="status" aria-live="polite">{toasts.map(t => <div key={t.id} className={`toast tone-${t.tone}`}><Icon name={t.tone === 'good' ? 'check' : 'alert'} size={14}/>{t.message}</div>)}</div>
  </FeedbackContext.Provider>;
}

export function relativeTime(iso?: string) {
  if (!iso) return '';
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (Number.isNaN(seconds)) return '';
  const units: Array<[number, Intl.RelativeTimeFormatUnit]> = [[60, 'second'], [60, 'minute'], [24, 'hour'], [30, 'day'], [12, 'month'], [Infinity, 'year']];
  let value = -seconds;
  for (const [size, unit] of units) {
    if (Math.abs(value) < size) return new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'}).format(Math.round(value), unit);
    value /= size;
  }
  return '';
}

export function formatDuration(ms: number) {
  const sign = ms < 0 ? '−' : '';
  const abs = Math.abs(ms);
  if (abs < 1000) return `${sign}${abs.toFixed(0)} ms`;
  if (abs < 60_000) return `${sign}${(abs / 1000).toFixed(1)} s`;
  return `${sign}${(abs / 60_000).toFixed(1)} min`;
}

export function formatNumber(value: number) {
  return new Intl.NumberFormat(undefined, {notation: Math.abs(value) >= 10_000 ? 'compact' : 'standard', maximumFractionDigits: 1}).format(value);
}

export function shortToken(token: string) {
  return token.length > 14 ? `${token.slice(0, 8)}…${token.slice(-4)}` : token;
}

/** Shows a recoverable message instead of a blank screen if a page crashes. */
export class ErrorBoundary extends React.Component<{children: React.ReactNode; resetKey?: string}, {error?: Error}> {
  state: {error?: Error} = {};
  static getDerivedStateFromError(error: Error) {return {error};}
  componentDidUpdate(previous: {resetKey?: string}) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({error: undefined});
  }
  render() {
    if (!this.state.error) return this.props.children;
    return <div className="card crash" role="alert">
      <Icon name="alert" size={18}/>
      <div><strong>This page hit a problem.</strong><p className="muted">{this.state.error.message}</p></div>
      <button type="button" className="secondary-button" onClick={() => location.reload()}>Reload</button>
    </div>;
  }
}
