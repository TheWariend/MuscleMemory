import {ProviderLogo, providerOf} from '../logos';
import {agentName, needsSkillReview} from '../plain';
import {CountUp, Empty, formatDuration, formatNumber, Icon, relativeTime, Shimmer, Skeleton} from '../ui';
import {useWorkspace} from '../workspace';
import {SessionRow} from './Activity';
import {SkillRow} from './Skills';

export function Home() {
  const {procedures, runs, runTotal, usage, loading, navigate} = useWorkspace();
  if (!procedures || !runs || !usage) return loading ? <HomeSkeleton/> : null;

  const ready = procedures.filter(p => p.status === 'active' && !p.needs_review);
  const learning = procedures.filter(p => p.status === 'provisional');
  const paused = procedures.filter(p => p.status === 'flagged');
  const review = procedures.filter(needsSkillReview);
  const agents = [...new Map(runs.map(r => [agentName(r.agent_name), providerOf(r.agent_name)])).entries()];
  const used = usage.operations.find(o => o.operation === 'recall')?.requests ?? 0;
  const latest = runs[0]?.created_at;

  if (!runs.length) return <section className="card hero">
    <Empty icon="connect" title="Connect your first agent" action={<button type="button" className="primary-button" onClick={() => navigate({view: 'settings'})}>Connect an agent</button>}>
      MuscleMemory watches your agent work. When a task really succeeds, it learns how it was done and hands that to the agent next time a similar task comes up.
    </Empty>
  </section>;

  return <div className="stack">
    <section className="card hero">
      <p className="hero-line">
        {ready.length
          ? <>Your agents can reuse <strong>{ready.length} {ready.length === 1 ? 'skill' : 'skills'}</strong>.</>
          : <>No skills are ready yet. {learning.length ? <>{learning.length} {learning.length === 1 ? 'is' : 'are'} still being checked.</> : 'They appear after a task succeeds.'}</>}
      </p>
      <p className="hero-sub">
        <span className="status-dot good"/>
        {agents.map(([name, provider], i) => <span className="agent-chip" key={name}>{provider && <ProviderLogo provider={provider} size={14}/>}{name}{i < agents.length - 1 ? ',' : ''}</span>)}
        <span>connected{latest && <> · last session {relativeTime(latest)}</>}</span>
      </p>
    </section>

    <section className="tiles" aria-label="Summary">
      <Tile label="Ready to use" value={ready.length} note="verified skills" tone="good" onClick={() => navigate({view: 'skills'})}/>
      <Tile label="Learning" value={learning.length} note="need more successful runs" onClick={() => navigate({view: 'skills'})}/>
      <Tile label="Handed to agents" value={used} note="times a skill was looked up" onClick={() => navigate({view: 'skills'})}/>
      <Tile label="Sessions" value={runTotal ?? runs.length} note="recorded agent sessions" onClick={() => navigate({view: 'activity'})}/>
    </section>

    {review.length > 0 && <div className="banner tone-info"><Icon name="bolt"/>
      <span><strong>{review.length} new {review.length === 1 ? 'skill needs' : 'skills need'} your OK.</strong> Learned from sessions whose tests passed. Keep or delete {review.length === 1 ? 'it' : 'them'}.</span>
      <button type="button" className="ghost-button small" onClick={() => navigate({view: 'skills', id: review[0].procedure_token})}>Review</button>
    </div>}

    {paused.length > 0 && <div className="banner tone-bad"><Icon name="flag"/>
      <span><strong>{paused.length} {paused.length === 1 ? 'skill is' : 'skills are'} paused</strong> while evidence or quality concerns are reviewed. Agents won’t get {paused.length === 1 ? 'it' : 'them'}.</span>
      <button type="button" className="ghost-button small" onClick={() => navigate({view: 'skills', id: paused[0].procedure_token})}>Review</button>
    </div>}

    <Savings/>

    <div className="grid-2">
      <section className="card">
        <div className="card-head"><h2>Latest skills</h2><button type="button" className="ghost-button small" onClick={() => navigate({view: 'skills'})}>All skills<Icon name="arrow" size={13}/></button></div>
        {procedures.length ? <div className="rows">{procedures.slice(0, 5).map(p => <SkillRow key={p.procedure_token} p={p} compact/>)}</div>
          : <Empty compact icon="procedures" title="Nothing learned yet">Skills appear after an agent finishes a task successfully.</Empty>}
      </section>
      <section className="card">
        <div className="card-head"><h2>Latest sessions</h2><button type="button" className="ghost-button small" onClick={() => navigate({view: 'activity'})}>All activity<Icon name="arrow" size={13}/></button></div>
        <div className="rows">{runs.slice(0, 5).map(r => <SessionRow key={r.run_token} r={r} compact/>)}</div>
      </section>
    </div>
  </div>;
}

function Savings() {
  const {usage} = useWorkspace();
  if (!usage) return null;
  const pairs = usage.paired_measurements;
  const saved = usage.observed_savings;
  const items = [
    {label: 'Tool calls saved', value: pairs.tool_calls ? formatNumber(saved.tool_calls) : null},
    {label: 'Tokens saved', value: pairs.tokens ? formatNumber(saved.tokens) : null},
    {label: 'Time saved', value: pairs.duration_ms ? formatDuration(saved.duration_ms) : null},
  ];
  if (!items.some(i => i.value)) return null;
  return <section className="card">
    <div className="card-head"><div><h2>Saved so far</h2><p>Compared with the same task done without memory. Numbers can be negative.</p></div></div>
    <div className="savings">{items.map(i => <div className="saving" key={i.label}><span>{i.label}</span><strong className={i.value ? '' : 'muted'}>{i.value ?? '—'}</strong></div>)}</div>
  </section>;
}

function Tile({label, value, note, tone, onClick}: {label: string; value: number; note: string; tone?: string; onClick: () => void}) {
  return <button type="button" className={`stage ${tone === 'good' ? 'accent' : ''}`} onClick={onClick}>
    <span className="stage-label">{label}</span>
    <strong><CountUp value={value}/></strong>
    <small>{note}</small>
  </button>;
}

function HomeSkeleton() {
  return <div className="stack" aria-busy="true" aria-label="Loading">
    <section className="card hero"><Shimmer width="46%" height={30} radius={8}/><div style={{height: 14}}/><Shimmer width="28%" height={12}/></section>
    <section className="tiles">{[0, 1, 2, 3].map(i => <div className="stage" key={i}><Shimmer width="50%" height={11}/><Shimmer width={44} height={28} radius={7}/><Shimmer width="70%" height={10}/></div>)}</section>
    <div className="grid-2"><Skeleton rows={3}/><Skeleton rows={3}/></div>
  </div>;
}
