import React, {useEffect, useMemo, useRef, useState} from 'react';
import type {RecallResult, RunSummary} from '../api';
import {ProviderLogo, providerOf} from '../logos';
import {shortTitle, skillName} from '../plain';
import {useSkillDetails} from '../skillData';
import {Empty, Icon, Logo, NoteText} from '../ui';
import {useWorkspace} from '../workspace';

type Turn = {id: number; task: string; result?: RecallResult; error?: string};
type Pointer = {title?: string; changed_files?: string[]; requires_files?: string[]; verify?: string[]; avoid?: string[]};

// A request no coding skill should match, to show MuscleMemory staying quiet.
const UNRELATED = 'Write release notes for the next version';

export function Playground() {
  const {client, scope, runs, procedures, navigate} = useWorkspace();
  const ready = useMemo(() => (procedures ?? []).filter(p => p.status === 'active'), [procedures]);
  const {details} = useSkillDetails(client, ready, 24);
  const [draft, setDraft] = useState('');
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const endRef = useRef<HTMLDivElement>(null);
  // Braces matter: newer browsers return a Promise from scrollIntoView, which React would treat as a cleanup.
  useEffect(() => {endRef.current?.scrollIntoView({behavior: 'smooth', block: 'end'});}, [turns]);

  // Real prompts your agents were given in the sessions that taught or verified a skill.
  const suggestions = useMemo(() => {
    const linked = new Set<string>();
    for (const d of details ?? []) {
      const source = d.definition.provenance.source_run as string | undefined;
      if (source) linked.add(source);
      d.validation_history.forEach(e => linked.add(e.run_token));
    }
    const pick = (list: RunSummary[]) => [...new Set(list.map(r => r.intent_text.trim()))];
    const fromSkills = pick((runs ?? []).filter(r => linked.has(r.run_token)));
    const others = pick((runs ?? []).filter(r => r.status === 'success'));
    return [...new Set([...fromSkills, ...others])].slice(0, 3);
  }, [details, runs]);

  async function send(text: string, event?: React.FormEvent) {
    event?.preventDefault();
    const task = text.trim();
    if (!task || busy) return;
    const id = Date.now();
    setTurns(list => [...list, {id, task}]);
    setDraft(''); setBusy(true);
    try {
      // Skills only match agents with the same tools, so borrow the latest session's setup.
      const latest = (runs ?? []).find(r => r.status !== 'recording');
      const recorded = latest ? await client.run(latest.run_token) : undefined;
      const result = await client.recall({
        intent_text: task, visibility_scope: scope, match_limit: 1,
        runtime_profile: {agent_name: recorded?.runtime_profile.agent_name || 'playground', bindings: recorded?.runtime_profile.bindings ?? {}},
      });
      setTurns(list => list.map(t => t.id === id ? {...t, result} : t));
    } catch (e) {
      setTurns(list => list.map(t => t.id === id ? {...t, error: (e as Error).message} : t));
    } finally {setBusy(false);}
  }

  if (procedures && !ready.length) return <div className="card"><Empty icon="recall" title="Nothing to try yet" action={<button type="button" className="secondary-button" onClick={() => navigate({view: 'skills'})}>See skills</button>}>The playground needs at least one ready skill. A skill becomes ready after two more sessions pass their tests with it.</Empty></div>;

  const agent = providerOf(runs?.[0]?.agent_name);
  // Show skills by the request they were learned from, like the Skills page.
  const titles = new Map((procedures ?? []).map(p => [p.procedure_token, skillName(p)]));
  return <section className="card chat">
    <div className="chat-log">
      {!turns.length && <div className="chat-intro">
        <h2>Preview what your agent gets</h2>
        <p>Type a task the way you’d give it to your agent. You’ll see which of your skills it would get and the exact text MuscleMemory would add to its prompt. This is only a preview: nothing is sent to your agent.</p>
      </div>}
      {turns.map(t => <div className="turn" key={t.id}>
        <div className="bubble you"><span className="who">You</span><p>{t.task}</p></div>
        <div className="bubble memory">
          <span className="who"><Logo height={11}/>MuscleMemory</span>
          {t.error ? <p className="tone-bad-text">{t.error}</p>
            : !t.result ? <p className="muted thinking"><span className="status-dot pulse"/>Looking through your skills…</p>
            : <Answer task={t.task} result={t.result} onOpen={id => navigate({view: 'skills', id})} titles={titles}/>}
        </div>
      </div>)}
      <div ref={endRef}/>
    </div>

    <div className="composer-bar">
      {suggestions.length > 0 && <div className="chips-row" aria-label="Try one of your real tasks">
        <span className="chips-label">Try a real task</span>
        {suggestions.map(s => <button type="button" key={s} className="suggestion-chip" title={s} disabled={busy} onClick={() => void send(s)}>{s.length > 60 ? `${s.slice(0, 58)}…` : s}</button>)}
        <button type="button" className="suggestion-chip quiet-chip" disabled={busy} onClick={() => void send(UNRELATED)}>{UNRELATED}</button>
      </div>}
      <form className="composer-input" onSubmit={e => void send(draft, e)}>
        {agent && <ProviderLogo provider={agent} size={16}/>}
        <input value={draft} onChange={e => setDraft(e.target.value)} placeholder="Describe a coding task…" aria-label="Task"/>
        <button className="primary-button" disabled={busy || !draft.trim()} aria-label="Send"><Icon name="arrow" size={15}/></button>
      </form>
    </div>
  </section>;
}

function Answer({task, result, onOpen, titles}: {task: string; result: RecallResult; onOpen: (id: string) => void; titles: Map<string, string>}) {
  const [exact, setExact] = useState(false);
  const match = result.candidates[0];
  if (!match) return <p><strong>No skill fits this task.</strong> Your agent would start from scratch and get no extra text.</p>;
  let pointer: Pointer = {};
  try {pointer = JSON.parse(result.guidance ?? '{}');} catch {/* show the summary only */}
  const total = match.successes + match.failures;
  return <div className="answer">
    <p>Your agent would get a skill that worked <strong>{match.successes} of {total}</strong> times: <button type="button" className="link-button inline" onClick={() => onOpen(match.procedure_token)}>{titles.get(match.procedure_token) || shortTitle(match.definition.title)}</button></p>
    <p className="muted">It would be added to your agent’s prompt like this:</p>
    <ul className="hand-off">
      {pointer.changed_files?.length ? <li><span>Files to change</span>{pointer.changed_files.join(', ')}</li> : null}
      {pointer.requires_files?.length ? <li><span>Read first</span>{pointer.requires_files.join(', ')}</li> : null}
      {pointer.verify?.length ? <li><span>Check it with</span><code>{pointer.verify.join(' && ')}</code></li> : null}
      {pointer.avoid?.length ? <li><span>Watch out</span><div>{[...new Set(pointer.avoid)].map(note => <p key={note}><NoteText text={note}/></p>)}</div></li> : null}
    </ul>
    <button type="button" className="link-button" aria-expanded={exact} onClick={() => setExact(!exact)}>{exact ? 'Hide' : 'Show'} the exact prompt your agent would get</button>
    {exact && <pre className="prompt-preview">{`${task}\n\n${result.guidance ?? ''}`}</pre>}
  </div>;
}
