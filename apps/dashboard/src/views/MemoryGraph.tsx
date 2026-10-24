import {useEffect, useMemo, useState} from 'react';
import type {LearningDecision, MemoryGraph, MemoryNode} from '../api';
import {Empty, Icon, Skeleton} from '../ui';
import {useWorkspace} from '../workspace';

const kinds: MemoryNode['kind'][] = ['skill', 'claim', 'event', 'run'];
const decisionLabel = (status: string) => ({error: 'Deferred', accepted: 'Accepted', rejected: 'Rejected', queued: 'Queued', running: 'Reviewing'} as Record<string, string>)[status] ?? status;
const labels = {skill: 'Skills and versions', claim: 'Lessons', event: 'Source events', run: 'Recordings'};

export function MemoryGraphView({root}: {root?: string}) {
  const {client, scope, procedures} = useWorkspace();
  const [graph, setGraph] = useState<MemoryGraph>();
  const [decisions, setDecisions] = useState<LearningDecision[]>([]);
  const [selected, setSelected] = useState<string>();
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    setGraph(undefined); setError(''); setSelected(undefined);
    Promise.all([client.graph(scope, root, controller.signal), client.learning(scope, controller.signal)])
      .then(([g, d]) => {setGraph(g); setDecisions(d);})
      .catch(e => {if (!controller.signal.aborted) setError((e as Error).message);});
    return () => controller.abort();
  }, [client, scope, root, procedures]);
  const layout = useMemo(() => {
    const positions = new Map<string, {x: number; y: number}>();
    for (const [column, kind] of kinds.entries()) {
      graph?.nodes.filter(n => n.kind === kind).forEach((n, row) => positions.set(n.id, {x: 18 + column * 255, y: 52 + row * 66}));
    }
    const longest = Math.max(1, ...kinds.map(kind => graph?.nodes.filter(n => n.kind === kind).length ?? 0));
    return {positions, height: 66 * longest + 70};
  }, [graph]);
  const connected = new Set(graph?.edges.filter(e => !selected || e.source === selected || e.target === selected).flatMap(e => [e.source, e.target]));
  const selectedNode = graph?.nodes.find(n => n.id === selected);
  function label(n: MemoryNode) {
    if (n.kind === 'skill') return `${n.title ?? n.reference} · v${n.version}`;
    if (n.kind === 'claim') return graph?.edges.find(e => e.target === n.id)?.evidence.text ?? `Lesson ${Number(n.event_id) + 1}`;
    return n.kind === 'event' ? n.event_id : n.reference.slice(0, 24);
  }
  return <div className="stack">
    <section className="card">
      <div className="card-head"><div><h2>Evidence and evolution</h2><p>Select a node to follow its connections. Historical versions remain visible.</p></div>{root && <a className="ghost-button" href="#/graph">All memory</a>}</div>
      {error ? <div className="pad" role="alert">{error}</div> : !graph ? <Skeleton rows={5}/> : !graph.nodes.length ? <Empty icon="graph" title="No evidence graph yet">Accepted procedures and checked reuse add links here. Recordings stay available in Activity.</Empty> : <>
        <div style={{overflow: 'auto', maxHeight: 620}}>
          <svg width="1040" height={layout.height} role="group" aria-label="Memory evidence graph" style={{display: 'block'}}>
            {kinds.map((kind, column) => <text key={kind} x={18 + column * 255} y={25} fill="currentColor" fontSize="13">{labels[kind]}</text>)}
            {graph.edges.map(edge => {
              const a = layout.positions.get(edge.source), b = layout.positions.get(edge.target);
              if (!a || !b) return null;
              return <path key={edge.id} d={`M${a.x + 226},${a.y + 21} C${a.x + 255},${a.y + 21} ${b.x - 25},${b.y + 21} ${b.x},${b.y + 21}`} fill="none" stroke="currentColor" strokeWidth="1.5" opacity={!selected || edge.source === selected || edge.target === selected ? 0.45 : 0.08}><title>{edge.relation.replaceAll('_', ' ')}</title></path>;
            })}
            {graph.nodes.map(n => {
              const p = layout.positions.get(n.id)!;
              return <g key={n.id} role="button" tabIndex={0} aria-label={label(n)} onClick={() => setSelected(selected === n.id ? undefined : n.id)} onKeyDown={e => {if (e.key === 'Enter' || e.key === ' ') {e.preventDefault(); setSelected(selected === n.id ? undefined : n.id);}}} style={{cursor: 'pointer', opacity: !selected || selected === n.id || connected.has(n.id) ? 1 : 0.3}}>
                <rect x={p.x} y={p.y} width="226" height="42" rx="8" fill="var(--surface, #252a31)" stroke={selected === n.id ? '#8cb7ff' : '#69717f'}/>
                <text x={p.x + 10} y={p.y + 25} fill="#f4f6f9" fontSize="12">{label(n).slice(0, 31)}{label(n).length > 31 ? '…' : ''}</text><title>{label(n)}</title>
              </g>;
            })}
          </svg>
        </div>
        {graph.truncated && <p className="pad field-note">Showing a bounded part of the graph. Open a skill to focus its evidence.</p>}
        {selectedNode && <div className="pad stack-sm"><strong>{label(selectedNode)}</strong>
          {selectedNode.kind === 'skill' && <a href={`#/skills/${encodeURIComponent(selectedNode.reference)}`}>Open skill and validation history</a>}
          {(selectedNode.kind === 'event' || selectedNode.kind === 'run') && <a href={`#/activity/${encodeURIComponent(selectedNode.reference)}`}>Open source recording</a>}
          <div className="rows">{graph.edges.filter(e => e.source === selected || e.target === selected).map(e => <div className="row static" key={e.id}><Icon name="graph" size={14}/><span>{e.relation.replaceAll('_', ' ')}{e.evidence.execution_verified === false ? ' · semantic support; no execution check' : ''}{e.evidence.text ? ` · ${e.evidence.text}` : ''}</span></div>)}</div>
        </div>}
      </>}
    </section>
    <section className="card"><div className="card-head"><div><h2>Learning decisions</h2><p>Why a recording produced a procedure, was rejected, or needs another attempt.</p></div></div>
      {!decisions.length ? <Empty compact icon="clock" title="No decisions yet">Choose a learning agent in settings to distill recorded work.</Empty> : <div className="rows">{decisions.map(d => <a className="row" href={`#/activity/${encodeURIComponent(d.run_token)}`} key={d.id}><span className="row-main"><span className="row-title">{decisionLabel(d.status)}</span><span className="row-meta">{d.reason}</span></span><span>{d.attempts} {d.attempts === 1 ? 'attempt' : 'attempts'}</span></a>)}</div>}
    </section>
  </div>;
}
