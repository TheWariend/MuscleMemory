import React, {useEffect, useMemo, useState} from 'react';
import type {ProcedureDetail, ProcedureSummary} from '../api';
import {checksLine, describeStep, plainNote, requestDetails, shortCommand, shortTitle, skillStatus} from '../plain';
import {Badge, CodeBlock, Empty, formatDuration, Icon, relativeTime, SearchField, Segmented, Sheet, Skeleton, useFeedback} from '../ui';
import {useWorkspace} from '../workspace';

type Filter = 'all' | 'review' | 'active' | 'provisional' | 'flagged' | 'retired';
const filterLabels: Record<Filter, string> = {all: 'All', review: 'Needs your OK', active: 'Ready', provisional: 'Learning', flagged: 'Paused', retired: 'Turned off'};

export function Skills({selected}: {selected?: string}) {
  const {procedures, loading, navigate} = useWorkspace();
  const [filter, setFilter] = useState<Filter>('all');
  const [query, setQuery] = useState('');
  const list = procedures ?? [];
  const matches = (p: ProcedureSummary, f: Filter) => f === 'all' || (f === 'review' ? Boolean(p.needs_review) : p.status === f);
  const count = (f: Filter) => list.filter(p => matches(p, f)).length;
  const shown = useMemo(() => {
    const q = query.trim().toLowerCase();
    return list.filter(p => matches(p, filter) && (!q || (p.title ?? '').toLowerCase().includes(q)));
  }, [list, filter, query]);
  const filters = (Object.keys(filterLabels) as Filter[]).filter(f => f === 'all' || count(f) > 0);

  return <div className="stack">
    {list.length > 0 && <p className="skills-explain">
      A skill is how your agent solved a task before: the files it changed and the test that proved it worked.
      It’s <strong>Ready</strong> after passing two separate checks, and only then handed to your agent when a similar task comes up.
    </p>}
    {list.length > 0 && <div className="toolbar">
      <Segmented label="Show" value={filter} onChange={setFilter} options={filters.map(f => ({value: f, label: filterLabels[f], count: count(f)}))}/>
      <SearchField value={query} onChange={setQuery} placeholder="Search skills"/>
    </div>}
    {!procedures ? (loading ? <Skeleton/> : null)
      : !list.length ? <div className="card"><Empty icon="procedures" title="No skills yet" action={<button type="button" className="secondary-button" onClick={() => navigate({view: 'settings'})}>Connect an agent</button>}>When an agent finishes a task and its tests pass, MuscleMemory learns how it did it. After two more successful runs, the skill is ready for your agents to reuse.</Empty></div>
      : !shown.length ? <div className="card"><Empty compact icon="search" title="No matches">Nothing matches{query && <> “{query}”</>}.</Empty></div>
      : <div className="card"><div className="rows">{shown.map(p => <SkillRow key={p.procedure_token} p={p}/>)}</div></div>}
    <SkillSheet token={selected} onClose={() => navigate({view: 'skills'})}/>
  </div>;
}

export function SkillRow({p, compact}: {p: ProcedureSummary; compact?: boolean}) {
  const {navigate} = useWorkspace();
  const status = skillStatus(p.status, p.successes, p.needs_review);
  const files = (p.changes ?? []).map(f => f.split('/').slice(-2).join('/'));
  const task = p.learned_from?.trim();
  return <button type="button" className="row skill-row" onClick={() => navigate({view: 'skills', id: p.procedure_token})}>
    <span className="row-icon"><Icon name="procedures" size={15}/></span>
    <span className="row-main">
      <span className="row-title" title={task || p.title}>{shortTitle(task || p.title)}</span>
      {!compact && requestDetails(task) && <span className="skill-desc">{requestDetails(task)}</span>}
      {!compact && <span className="skill-does">
        {files.length > 0 && <span><span className="skill-label">Changes</span>{files.join(', ')}</span>}
        {p.check?.[0] && <span><span className="skill-label">Checks with</span><code>{shortCommand(p.check[0])}</code></span>}
      </span>}
      <span className="row-meta">
        <span>{checksLine(p.successes, p.failures, p.status)}</span>
        {p.updated_at && <span>{relativeTime(p.updated_at)}</span>}
      </span>
    </span>
    <Badge label={status.label} tone={status.tone} title={status.explain}/>
    <Icon name="chevron" size={14}/>
  </button>;
}

type Tab = 'steps' | 'proof' | 'details';

function SkillSheet({token, onClose}: {token?: string; onClose: () => void}) {
  const {client, can, reload, procedures} = useWorkspace();
  const asked = procedures?.find(p => p.procedure_token === token)?.learned_from?.trim();
  const {confirm, toast} = useFeedback();
  const [detail, setDetail] = useState<ProcedureDetail>();
  const [error, setError] = useState('');
  const [tab, setTab] = useState<Tab>('steps');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setDetail(undefined); setError(''); setTab('steps');
    client.procedure(token, controller.signal).then(setDetail).catch(e => {if (!controller.signal.aborted) setError((e as Error).message);});
    return () => controller.abort();
  }, [client, token]);

  async function keep() {
    if (!detail) return;
    setBusy(true);
    try {await client.approve(detail.procedure_token); toast('Kept. Agents get it once it has passed two checks.'); setDetail({...detail, needs_review: false}); reload();}
    catch (e) {toast((e as Error).message, 'bad');}
    finally {setBusy(false);}
  }
  async function turnOff() {
    if (!detail || !await confirm({title: 'Turn off this skill?', confirm: 'Turn off', body: <p>Agents will stop getting “{detail.definition.title}”. Its history is kept.</p>})) return;
    setBusy(true);
    try {await client.retire(detail.procedure_token); toast('Skill turned off'); setDetail({...detail, lifecycle_status: 'retired'}); reload();}
    catch (e) {toast((e as Error).message, 'bad');}
    finally {setBusy(false);}
  }
  async function remove() {
    if (!detail || !await confirm({title: 'Delete this skill?', confirm: 'Delete', danger: true, body: <p>“{detail.definition.title}” and its history will be removed. This can’t be undone.</p>})) return;
    setBusy(true);
    try {await client.deleteProcedure(detail.procedure_token); toast('Skill deleted'); reload(); onClose();}
    catch (e) {toast((e as Error).message, 'bad'); setBusy(false);}
  }

  const d = detail?.definition;
  const status = detail ? skillStatus(detail.lifecycle_status, detail.successes, detail.needs_review) : undefined;
  return <Sheet open={Boolean(token)} onClose={onClose} label="Skill">
    <div className="sheet-head">
      <div className="sheet-title">
        <p className="eyebrow">Skill</p>
        <h2>{d ? shortTitle(asked || d.title) : error ? 'Unavailable' : 'Loading…'}</h2>
        {detail && status && <div className="sheet-meta"><Badge label={status.label} tone={status.tone}/><span>{status.explain}</span></div>}
      </div>
      <button type="button" className="icon-button" onClick={onClose} aria-label="Close"><Icon name="close"/></button>
    </div>
    {error && <div className="sheet-body"><div role="alert" className="banner tone-bad"><Icon name="alert"/><span>{error}</span></div></div>}
    {!detail && !error && <div className="sheet-body"><Skeleton rows={5}/></div>}
    {detail && d && <>
      <div className="sheet-tabs"><Segmented label="Sections" value={tab} onChange={setTab} options={[
        {value: 'steps', label: 'What it does'}, {value: 'proof', label: 'Proof'}, {value: 'details', label: 'Details'}]}/></div>
      <div className="sheet-body" key={tab}>
        {tab === 'steps' && <div className="stack-sm">
          {(asked || d.title) !== shortTitle(asked || d.title) && <p className="intent-line">Learned from your request: “{asked || d.title}”</p>}
          <ol className="timeline">{d.steps.map((s, i) => <li className="timeline-item" key={s.step_token}>
            <span className="timeline-dot">{i + 1}</span>
            <div className="timeline-body"><p className="event-text">{describeStep(s.capability, s.arguments, s.effect, s.source_tool)}</p></div>
          </li>)}</ol>
          {(d.avoidance_rules?.length ?? 0) > 0 && <Notes title="Watch out" icon="alert" items={d.avoidance_rules!}/>}
          {(d.recovery_notes?.length ?? 0) > 0 && <Notes title="If it goes wrong" icon="refresh" items={d.recovery_notes!}/>}
          <p className="notice"><Icon name="shield" size={14}/>Agents get this as a suggestion. They still check the current files and follow your normal approvals.</p>
        </div>}
        {tab === 'proof' && <Proof detail={detail}/>}
        {tab === 'details' && <CodeBlock label="Full skill (JSON)">{JSON.stringify(d, null, 2)}</CodeBlock>}
      </div>
      {(can('write') || can('delete')) && <div className="sheet-foot">
        {can('write') && detail.needs_review && <button type="button" className="primary-button" disabled={busy} onClick={() => void keep()}><Icon name="check" size={14}/>Keep</button>}
        {can('write') && detail.lifecycle_status !== 'retired' && !detail.needs_review && <button type="button" className="secondary-button" disabled={busy} onClick={() => void turnOff()}><Icon name="archive" size={14}/>Turn off</button>}
        {can('delete') && <button type="button" className="danger-ghost" disabled={busy} onClick={() => void remove()}><Icon name="trash" size={14}/>Delete</button>}
      </div>}
    </>}
  </Sheet>;
}

function Proof({detail}: {detail: ProcedureDetail}) {
  const {navigate} = useWorkspace();
  const source = detail.definition.provenance.source_run as string | undefined;
  const total = detail.successes + detail.failures;
  return <div className="stack-sm">
    <p className="intent-line">
      {total ? <>Checked on <strong>{total}</strong> separate {total === 1 ? 'run' : 'runs'}: <strong>{detail.successes}</strong> worked{detail.failures ? <>, <strong>{detail.failures}</strong> failed</> : null}.</>
        : <>Not checked on a separate run yet. The run it was learned from doesn’t count.</>}
    </p>
    {source && <button type="button" className="row bordered" onClick={() => navigate({view: 'activity', id: source})}>
      <span className="row-icon"><Icon name="runs" size={15}/></span>
      <span className="row-main"><span className="row-title">Learned from this session</span><span className="row-meta"><span>Open the original session</span></span></span><Icon name="chevron" size={14}/>
    </button>}
    {detail.validation_history.length > 0 && <div className="rows bordered">{detail.validation_history.map(e => {
      const o = e.metrics.observed;
      const worked = e.outcome?.succeeded === true;
      return <button type="button" className="row" key={e.run_token} onClick={() => navigate({view: 'activity', id: e.run_token})}>
        <span className="row-main"><span className="row-title">{worked ? 'Worked' : 'Failed'}</span>
          <span className="row-meta">{o ? <><span>{o.tool_calls} tool calls</span><span>{formatDuration(o.duration_ms)}</span></> : <span>No measurements</span>}</span></span>
        <Badge label={worked ? 'Worked' : 'Failed'} tone={worked ? 'good' : 'bad'}/>
      </button>;
    })}</div>}
    {detail.contrasts.length > 0 && <p className="muted">Learned from {detail.contrasts.length} failed {detail.contrasts.length === 1 ? 'attempt' : 'attempts'} too. See “Watch out” for what to avoid.</p>}
  </div>;
}

function Notes({title, items, icon}: {title: string; items: string[]; icon: 'alert' | 'refresh'}) {
  const notes = [...new Set(items.map(plainNote))];
  return <section className="sub"><h3><Icon name={icon} size={13}/>{title}</h3><ul className="bullets">{notes.map(i => <li key={i}>{i}</li>)}</ul></section>;
}
