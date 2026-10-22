import {useEffect, useMemo, useState} from 'react';
import type {RunDetail, RunEvent, RunSummary} from '../api';
import {ProviderLogo, providerOf} from '../logos';
import {agentName, describeStep, sessionIssues, sessionStatus} from '../plain';
import {Badge, CodeBlock, Empty, formatDuration, Icon, relativeTime, SearchField, Segmented, Sheet, Skeleton, useFeedback} from '../ui';
import {useWorkspace} from '../workspace';

type Filter = 'all' | 'success' | 'failure' | 'recording' | 'unverified' | 'incomplete';
const filterLabels: Record<Filter, string> = {all: 'All', success: 'Succeeded', failure: 'Failed', recording: 'In progress', unverified: 'Not verified', incomplete: 'Capture incomplete'};

export function Activity({selected}: {selected?: string}) {
  const {runs, loading, navigate} = useWorkspace();
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const list = runs ?? [];
  const count = (f: Filter) => f === 'all' ? list.length : list.filter(r => r.status === f).length;
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return list.filter(r => (filter === 'all' || r.status === filter) && (!q || r.intent_text.toLowerCase().includes(q)));
  }, [list, filter, query]);
  const filters = (Object.keys(filterLabels) as Filter[]).filter(f => f === 'all' || count(f) > 0);

  return <div className="stack">
    {list.length > 0 && <div className="toolbar">
      <Segmented label="Show" value={filter} onChange={setFilter} options={filters.map(f => ({value: f, label: filterLabels[f], count: count(f)}))}/>
      <SearchField value={query} onChange={setQuery} placeholder="Search tasks"/>
    </div>}
    {list.length > 0 && <p className="muted">{list.length === 100 ? 'Showing the latest 100 sessions. ' : ''}Capture incomplete means actions could not be recorded reliably. It does not establish whether the task succeeded or failed.</p>}
    {!runs ? (loading ? <Skeleton/> : null)
      : !list.length ? <div className="card"><Empty icon="runs" title="No sessions yet" action={<button type="button" className="secondary-button" onClick={() => navigate({view: 'settings'})}>Connect an agent</button>}>Sessions show up here once an agent is connected and starts working.</Empty></div>
      : !shown.length ? <div className="card"><Empty compact icon="search" title="No matches">Nothing matches{query && <> “{query}”</>}.</Empty></div>
      : <div className="card"><div className="rows">{shown.map(r => <SessionRow key={r.run_token} r={r}/>)}</div></div>}
    <SessionSheet token={selected} onClose={() => navigate({view: 'activity'})}/>
  </div>;
}

export function SessionRow({r, compact}: {r: RunSummary; compact?: boolean}) {
  const {navigate} = useWorkspace();
  const status = sessionStatus(r.status);
  return <button type="button" className="row" onClick={() => navigate({view: 'activity', id: r.run_token})}>
    <SessionIcon agent={r.agent_name} status={r.status}/>
    <span className="row-main">
      <span className="row-title">{r.intent_text}</span>
      <span className="row-meta"><span>{agentName(r.agent_name)}</span>{r.created_at && <span>{relativeTime(r.created_at)}</span>}</span>
    </span>
    {!compact && <Badge label={status.label} tone={status.tone}/>}
    <Icon name="chevron" size={14}/>
  </button>;
}

function SessionIcon({agent, status}: {agent?: string | null; status: string}) {
  const provider = providerOf(agent);
  return <span className={`row-icon run-${status}`}>
    {provider ? <ProviderLogo provider={provider} size={16}/> : <Icon name={status === 'failure' ? 'alert' : status === 'recording' ? 'clock' : 'runs'} size={15}/>}
    {status === 'failure' && <span className="row-icon-flag" aria-label="Failed"/>}
  </span>;
}

function SessionSheet({token, onClose}: {token?: string; onClose: () => void}) {
  const {client, can, reload} = useWorkspace();
  const {confirm, toast} = useFeedback();
  const [detail, setDetail] = useState<RunDetail>();
  const [error, setError] = useState('');
  const [raw, setRaw] = useState(false);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setDetail(undefined); setError(''); setRaw(false);
    client.run(token, controller.signal).then(setDetail).catch(e => {if (!controller.signal.aborted) setError((e as Error).message);});
    return () => controller.abort();
  }, [client, token]);

  async function remove() {
    if (!detail || !await confirm({title: 'Delete this session?', confirm: 'Delete', danger: true, body: <p>The record of “{detail.intent_text}” will be removed. Skills already learned from it are kept.</p>})) return;
    try {await client.deleteRun(detail.run_token); toast('Session deleted'); reload(); onClose();}
    catch (e) {toast((e as Error).message, 'bad');}
  }

  const tools = detail?.events.filter(e => e.kind === 'tool') ?? [];
  const status = detail ? sessionStatus(detail.status) : undefined;
  const issues = detail ? sessionIssues(detail.status, detail.capture_reason) : [];
  return <Sheet open={Boolean(token)} onClose={onClose} label="Session">
    <div className="sheet-head">
      <div className="sheet-title">
        <p className="eyebrow eyebrow-logo">{providerOf(detail?.runtime_profile.agent_name) && <ProviderLogo provider={providerOf(detail?.runtime_profile.agent_name)!} size={14}/>}{agentName(detail?.runtime_profile.agent_name)} session</p>
        <h2>{detail?.intent_text || (error ? 'Unavailable' : 'Loading…')}</h2>
        {detail && status && <div className="sheet-meta"><Badge label={status.label} tone={status.tone}/><span>{tools.length} {tools.length === 1 ? 'step' : 'steps'}</span></div>}
      </div>
      <button type="button" className="icon-button" onClick={onClose} aria-label="Close"><Icon name="close"/></button>
    </div>
    <div className="sheet-body">
      {error && <div role="alert" className="banner tone-bad"><Icon name="alert"/><span>{error}</span></div>}
      {!detail && !error && <Skeleton rows={5}/>}
      {detail && <div className="stack-sm">
        {issues.length > 0 && <section className="sub">
          <h3>Why no skill was learned</h3>
          <ul className="bullets">{issues.map(issue => <li key={issue}>{issue}</li>)}</ul>
          <p className="muted">Learning needs a reliable record of the actions and their results.</p>
        </section>}
        <ol className="timeline">{tools.map((e, i) => <Step key={e.event_id} e={e} index={i}/>)}</ol>
        {!tools.length && <p className="muted">No steps recorded.</p>}
        {detail.status === 'recording' && <p className="muted live"><span className="status-dot pulse"/>Still running.</p>}
        <button type="button" className="link-button" aria-expanded={raw} onClick={() => setRaw(!raw)}>{raw ? 'Hide' : 'Show'} full record</button>
        {raw && <CodeBlock label="Session (JSON)">{JSON.stringify(detail, null, 2)}</CodeBlock>}
      </div>}
    </div>
    {detail && can('delete') && <div className="sheet-foot"><button type="button" className="danger-ghost" onClick={() => void remove()}><Icon name="trash" size={14}/>Delete session</button></div>}
  </Sheet>;
}

function Step({e, index}: {e: RunEvent; index: number}) {
  const failed = e.status === 'failure';
  return <li className="timeline-item">
    <span className={`timeline-dot ${failed ? 'bad' : ''}`}>{index + 1}</span>
    <div className="timeline-body">
      <p className="event-text">{describeStep(e.capability, e.arguments, e.effect, e.tool_name)}</p>
      <p className="muted">{failed ? 'Failed · ' : ''}{formatDuration(e.duration_ms)}</p>
    </div>
  </li>;
}
