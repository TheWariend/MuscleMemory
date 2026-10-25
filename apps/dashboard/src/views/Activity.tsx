import {useEffect, useState} from 'react';
import type {MemoryLookup, RunDetail, RunEvent, RunSummary, RunPage} from '../api';
import {ProviderLogo, providerOf} from '../logos';
import {agentName, describeStep, memoryLookupStatus, sessionIssues, sessionStatus} from '../plain';
import {Badge, CodeBlock, Empty, formatDuration, Icon, relativeTime, SearchField, Segmented, Sheet, Skeleton, useFeedback} from '../ui';
import {useWorkspace} from '../workspace';

type Filter = 'all' | 'success' | 'failure' | 'recording' | 'unverified' | 'incomplete';
const filterLabels: Record<Filter, string> = {all: 'All', success: 'Succeeded', failure: 'Failed', recording: 'In progress', unverified: 'Not verified', incomplete: 'Capture incomplete'};

export function Activity({selected}: {selected?: string}) {
  const {runs, client, scope, navigate} = useWorkspace();
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const [result, setResult] = useState<RunPage>();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  useEffect(() => {setPage(0);}, [scope]);
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    const timer = setTimeout(() => {
      client.runPage(scope, page * 100, filter, query, controller.signal).then(setResult)
        .catch(e => {if (!controller.signal.aborted) setError((e as Error).message);})
        .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    }, 150);
    return () => {clearTimeout(timer); controller.abort();};
  }, [client, scope, page, filter, query, runs]);
  const list = result?.items ?? [];
  const count = (f: Filter) => f === 'all' ? Object.values(result?.status_counts ?? {}).reduce((a, b) => a + b, 0) : result?.status_counts[f] ?? 0;
  const shown = list;
  const filters = (Object.keys(filterLabels) as Filter[]).filter(f => f === 'all' || count(f) > 0);

  return <div className="stack">
    {(result && count('all') > 0) && <div className="toolbar">
      <Segmented label="Show" value={filter} onChange={next => {setFilter(next); setPage(0);}} options={filters.map(f => ({value: f, label: filterLabels[f], count: count(f)}))}/>
      <SearchField value={query} onChange={next => {setQuery(next); setPage(0);}} placeholder="Search all tasks"/>
    </div>}
    {result && <div className="toolbar"><span className="muted">{result.total ? `${page * 100 + 1}–${page * 100 + list.length} of ${result.total.toLocaleString()} sessions` : 'No matching sessions'}</span><div><button type="button" className="ghost-button" disabled={loading || page === 0} onClick={() => setPage(p => p - 1)}>Previous</button><button type="button" className="ghost-button" disabled={loading || !result.has_more} onClick={() => setPage(p => p + 1)}>Next</button></div></div>}
    {error && <div role="alert" className="banner tone-bad">{error}</div>}
    {loading ? <Skeleton/>
      : !result || count('all') === 0 ? <div className="card"><Empty icon="runs" title="No sessions yet" action={<button type="button" className="secondary-button" onClick={() => navigate({view: 'settings'})}>Connect an agent</button>}>Sessions show up here once an agent is connected and starts working.</Empty></div>
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
      {r.memory_lookup && <span className="row-meta"><Icon name="recall" size={12}/>{memoryLookupStatus(r.memory_lookup).label}{r.memory_lookup.skill_name && <> · {r.memory_lookup.skill_name}</>}</span>}
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
  const issues = detail ? sessionIssues(detail.status, detail.capture_reason, detail.capture_gaps) : [];
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
        <section className="sub">
          <h3>Memory lookup</h3>
          {!detail.memory_lookups?.length ? <p className="muted">No lookup report was recorded for this session.</p>
            : detail.memory_lookups.map(lookup => <MemoryLookupResult key={lookup.lookup_token} lookup={lookup}/>)}
        </section>
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

function MemoryLookupResult({lookup}: {lookup: MemoryLookup}) {
  const status = memoryLookupStatus(lookup);
  const readiness = lookup.workspace_readiness;
  return <div className="stack-sm">
    <p className="sheet-meta"><Badge label={status.label} tone={status.tone}/><span>{relativeTime(lookup.created_at)}</span></p>
    {lookup.procedure_token && <a href={`#/skills/${encodeURIComponent(lookup.procedure_token)}`}>{lookup.skill_name || 'Open skill'} · version {lookup.procedure_version}</a>}
    <p className="muted">{status.explain}</p>
    {lookup.proof && <p className="muted">Proof at report time: {lookup.proof.successes} passing checks · {lookup.proof.failures} failures · {lookup.proof.reviewed ? 'Kept by you' : 'Needs your OK'} · {lookup.proof.validation_status === 'validated' ? 'Validated' : 'Not validated'}</p>}
    {readiness && lookup.status === 'no_match' && <p className="muted">In this workspace: {readiness.needs_review} skills need your OK; {readiness.needs_checks} need more passing checks ({readiness.required_checks} required). These counts are separate from relevance to this task.</p>}
  </div>;
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
