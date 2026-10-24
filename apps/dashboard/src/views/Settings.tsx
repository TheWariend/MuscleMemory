import React, {useEffect, useState} from 'react';
import type {Key, Permission, Preferences} from '../api';
import {ProviderLogo, type Provider} from '../logos';
import {Badge, Chip, CodeBlock, CopyText, Empty, Icon, Skeleton, useFeedback} from '../ui';
import {useWorkspace} from '../workspace';

function shellArgument(value: string) {
  return /^[\w.-]+$/.test(value) ? value : `'${value.replaceAll("'", "'\\''")}'`;
}

type Agent = 'claude' | 'codex' | 'cursor' | 'cleanslate' | 'other';
const agents: Array<{id: Agent; label: string; logo: Provider}> = [
  {id: 'claude', label: 'Claude Code', logo: 'claude'},
  {id: 'codex', label: 'Codex', logo: 'codex'},
  {id: 'cursor', label: 'Cursor', logo: 'cursor'},
  {id: 'cleanslate', label: 'CleanSlate', logo: 'cleanslate'},
  {id: 'other', label: 'Other (MCP)', logo: 'mcp'},
];

export function Settings() {
  return <div className="stack">
    <Connect/>
    <SkillLearning/>
    <SkillNames/>
    <About/>
  </div>;
}

function Connect() {
  const {scope, session, runs, runTotal, error, reload} = useWorkspace();
  const [agent, setAgent] = useState<Agent>('codex');
  const origin = window.location.origin;
  const connected = (runs?.length ?? 0) > 0;
  const sessionCount = runTotal ?? runs?.length ?? 0;
  const waiting = runs !== undefined && !connected && !error;
  const name = agents.find(a => a.id === agent)!.label;

  // Check quietly while waiting, so the page confirms the connection by itself.
  useEffect(() => {
    if (!waiting) return;
    const timer = setInterval(reload, 10000);
    return () => clearInterval(timer);
  }, [waiting, reload]);

  const steps: Array<{title: string; body: React.ReactNode; code?: string}> =
    agent === 'other' ? [
      {title: 'Create a key for it', body: <>Run this where MuscleMemory runs. It prints the key once. Manage keys on the <a href="#/api">API keys page</a>.</>,
        code: `musclememory key --tenant ${shellArgument(session.tenant)} --namespace ${shellArgument(scope)} --label my-agent --permissions read,write`},
      {title: 'Add MuscleMemory to your client', body: 'MCP gives the agent memory tools. It only learns from steps it is told about.',
        code: `URL     ${origin}/mcp\nHeader  Authorization: Bearer <your API key>`},
    ] : agent === 'cleanslate' ? [
      {title: 'Connect CleanSlate', body: 'Creates a private key for CleanSlate and a config file outside your projects. Nothing to copy by hand.',
        code: 'musclememory connect cleanslate'},
      {title: 'Start CleanSlate with it', body: 'The command prints two lines like these. Run them, then open CleanSlate from the same terminal. CleanSlate sessions are recorded and get memory; learning new skills from them automatically is coming next.',
        code: 'export CLEANSLATE_MUSCLEMEMORY_CONFIG=~/.musclememory/cleanslate.json\nexport CLEANSLATE_MUSCLEMEMORY_NODE_MODULE=<printed by the command>'},
    ] : [
      {title: 'Connect it from your project folder', body: `Creates a private key for ${name} and installs its hooks in this project. Nothing to copy by hand.`,
        code: `cd /path/to/your/project\nmusclememory connect ${agent}`},
      {title: `Restart ${name}`, body: 'Hooks load when the agent starts.'},
    ];

  return <section className="card">
    <div className="card-head"><div><h2>Connect an agent</h2><p>MuscleMemory watches the agent’s steps and learns from tasks that succeed.</p></div></div>
    <div className="pad stack-sm">
      <div className="client-grid" role="radiogroup" aria-label="Agent">{agents.map(a => <button type="button" role="radio" aria-checked={agent === a.id} key={a.id} className="client" onClick={() => setAgent(a.id)}>
        <ProviderLogo provider={a.logo} size={20}/><span><strong>{a.label}</strong></span>{agent === a.id && <span className="client-check"><Icon name="check" size={12}/></span>}
      </button>)}</div>
      <ol className="guide" key={agent}>
        {steps.map((step, i) => <li key={step.title}><span className="guide-num">{i + 1}</span><div><h3>{step.title}</h3><p>{step.body}</p>{step.code && <CodeBlock label="Terminal">{step.code}</CodeBlock>}</div></li>)}
        <li><span className={`guide-num ${connected ? 'done' : ''}`}>{connected ? <Icon name="check" size={13}/> : steps.length + 1}</span><div><h3>Give it a task</h3>
          {connected ? <div className="connected-line">
            <Badge label="Connected" tone="good"/>
            <span className="muted">{sessionCount.toLocaleString()} {sessionCount === 1 ? 'session' : 'sessions'} received</span>
            <a className="ghost-button small" href="#/activity">See activity<Icon name="arrow" size={13}/></a>
          </div>
            : <p className="live"><span className="status-dot pulse"/>Waiting for the first session… this page checks by itself.</p>}
        </div></li>
      </ol>
    </div>
  </section>;
}

const namers: Array<{id: Preferences['skill_titles']; label: string; logo?: Provider}> = [
  {id: 'off', label: 'Manual only'},
  {id: 'claude', label: 'Claude Code', logo: 'claude'},
  {id: 'codex', label: 'Codex', logo: 'codex'},
];

function SkillLearning() {
  const {client, can} = useWorkspace();
  const {toast} = useFeedback();
  const [preferences, setPreferences] = useState<Preferences>();
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    client.preferences(controller.signal).then(setPreferences).catch(e => {if (!controller.signal.aborted) setError((e as Error).message);});
    return () => controller.abort();
  }, [client]);
  async function choose(next: Preferences['skill_learning']) {
    if (!preferences) return;
    setBusy(true);
    try {setPreferences(await client.setPreferences(preferences.skill_titles, next)); toast(next === 'off' ? 'Local learning selected. No model curation calls.' : 'Learning agent selected. New procedures still need independent checks.');}
    catch (e) {toast((e as Error).message, 'bad');}
    finally {setBusy(false);}
  }
  return <section className="card">
    <div className="card-head"><div><h2>Learn reusable procedures</h2><p>Verified sessions learn locally without an LLM account. Optionally use your signed-in agent to curate captured requests, code changes, and test commands. Both paths require independent checks and your review.</p></div></div>
    <div className="pad stack-sm">
      {error ? <p role="alert">{error}</p> : !preferences ? <Skeleton rows={1}/> : <div className="client-grid" role="radiogroup" aria-label="Learn procedures with">{namers.map(n => <button type="button" role="radio" aria-checked={preferences.skill_learning === n.id} key={n.id} className="client" disabled={busy || !can('admin') || preferences.learning_locked} onClick={() => void choose(n.id)}>
        {n.logo ? <ProviderLogo provider={n.logo} size={20}/> : <Icon name="close" size={16}/>}<span><strong>{n.id === 'off' ? 'Local only' : n.label}</strong></span>{preferences.skill_learning === n.id && <span className="client-check"><Icon name="check" size={12}/></span>}
      </button>)}</div>}
      <p className="field-note">{preferences?.learning_locked ? 'The learning agent is set by the server environment.' : 'Codex or Claude Code uses your subscription or API billing. Local only makes no curation calls. Failed attempts remain available to retry.'} <a href="#/graph">See learning decisions and evidence</a>.</p>
    </div>
  </section>;
}

/** Which of your own agents gives new skills a readable title. */
function SkillNames() {
  const {client, can} = useWorkspace();
  const {toast} = useFeedback();
  const [choice, setChoice] = useState<Preferences['skill_titles']>();
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    client.preferences(controller.signal).then(p => setChoice(p.skill_titles)).catch(e => {if (!controller.signal.aborted) setError((e as Error).message);});
    return () => controller.abort();
  }, [client]);

  async function choose(next: Preferences['skill_titles']) {
    const before = choice;
    setChoice(next);
    try {await client.setPreferences(next); toast(next === 'off' ? 'New skills keep their built names.' : `${namers.find(n => n.id === next)!.label} will name new skills.`);}
    catch (e) {setChoice(before); toast((e as Error).message, 'bad');}
  }

  return <section className="card">
    <div className="card-head"><div><h2>Skill names</h2><p>Choose Manual only to name a skill when you click its name button. Selecting Codex or Claude Code names new skills automatically. Naming gets the request, the files changed and the test command, never file contents. Search and your agents still use the original request.</p></div></div>
    <div className="pad stack-sm">
      {error ? <div role="alert" className="banner tone-bad"><Icon name="alert"/><span>{error}</span></div>
        : !choice ? <Skeleton rows={1}/>
        : <div className="client-grid" role="radiogroup" aria-label="Name new skills with">{namers.map(n => <button type="button" role="radio" aria-checked={choice === n.id} key={n.id} className="client"
          disabled={!can('admin')} onClick={() => void choose(n.id)}>
          {n.logo ? <ProviderLogo provider={n.logo} size={20}/> : <Icon name="close" size={16}/>}<span><strong>{n.label}</strong></span>{choice === n.id && <span className="client-check"><Icon name="check" size={12}/></span>}
        </button>)}</div>}
      <p className="field-note">To name a skill you already have, open it and use the name button.</p>
    </div>
  </section>;
}

export function Api() {
  const origin = window.location.origin;
  return <div className="stack">
    <Keys/>
    <CreateKey/>
    <section className="card">
      <div className="card-head"><div><h2>Addresses</h2><p>Use these with any API key above.</p></div></div>
      <dl className="kv wide pad">
        <div><dt>REST API</dt><dd><code>{origin}/v1</code></dd></div>
        <div><dt>MCP (for agents)</dt><dd><code>{origin}/mcp</code></dd></div>
        <div><dt>API reference</dt><dd><a href="https://thewariend.com/musclememory/api" target="_blank" rel="noreferrer">Open the API reference</a></dd></div>
        <div><dt>How to send the key</dt><dd><code>Authorization: Bearer &lt;your API key&gt;</code></dd></div>
      </dl>
    </section>
  </div>;
}

function CreateKey() {
  const {session, scope} = useWorkspace();
  const [label, setLabel] = useState('my-agent');
  const [access, setAccess] = useState<'read' | 'read,write' | 'read,write,delete,admin'>('read,write');
  const command = `musclememory key --tenant ${shellArgument(session.tenant)} --namespace ${shellArgument(scope)} --label ${shellArgument(label || 'my-agent')} --permissions ${access}`;
  return <section className="card">
    <div className="card-head"><div><h2>Create a key</h2><p>Run this where MuscleMemory runs. The key is printed once; store it somewhere private.</p></div></div>
    <div className="pad stack-sm">
      <div className="key-form">
        <label>Name<input value={label} onChange={e => setLabel(e.target.value.replace(/[^\w.-]/g, ''))}/></label>
        <label>Access<select value={access} onChange={e => setAccess(e.target.value as typeof access)}>
          <option value="read">Read only (dashboards, reporting)</option>
          <option value="read,write">Read and write (agents)</option>
          <option value="read,write,delete,admin">Full control (you)</option>
        </select></label>
      </div>
      <CodeBlock label="Terminal">{command}</CodeBlock>
    </div>
  </section>;
}

function Keys() {
  const {client, scope, session, can} = useWorkspace();
  const {confirm, toast} = useFeedback();
  const [keys, setKeys] = useState<Key[]>();
  const [error, setError] = useState('');
  const admin = can('admin');

  useEffect(() => {
    if (!admin) return;
    const controller = new AbortController();
    setKeys(undefined); setError('');
    client.keys(scope, controller.signal).then(setKeys).catch(e => {if (!controller.signal.aborted) setError((e as Error).message);});
    return () => controller.abort();
  }, [client, scope, admin]);

  async function revoke(key: Key) {
    const self = key.key_id === session.key_id;
    if (!await confirm({title: `Revoke “${key.label}”?`, confirm: 'Revoke', danger: true, body: <p>Anything using this key stops working right away.{self && <> <strong>You’re signed in with it</strong>, so you’ll be signed out.</>}</p>})) return;
    try {
      await client.revokeKey(key.key_id, scope);
      toast('Key revoked');
      if (self) {sessionStorage.removeItem('musclememory.key'); location.reload(); return;}
      setKeys(list => list?.map(k => k.key_id === key.key_id ? {...k, revoked: true} : k));
    } catch (e) {toast((e as Error).message, 'bad');}
  }

  return <section className="card">
    <div className="card-head"><div><h2>API keys</h2><p>Each agent uses its own key. A key is shown once when it’s created; revoke it here if it leaks.</p></div></div>
    {!admin ? <Empty compact icon="lock" title="Admin access needed">The key you signed in with can’t manage keys.</Empty>
      : error ? <div className="pad"><div role="alert" className="banner tone-bad"><Icon name="alert"/><span>{error}</span></div></div>
      : !keys ? <Skeleton rows={3}/>
      : !keys.length ? <Empty compact icon="key" title="No keys yet">Create one below.</Empty>
      : <div className="rows">{keys.map(k => <div className="row static" key={k.key_id}>
        <span className="row-icon"><Icon name="key" size={15}/></span>
        <span className="row-main"><span className="row-title">{k.label}{k.key_id === session.key_id && <Chip tone="info">you’re using this</Chip>}</span>
          <span className="row-meta"><span>Can {(k.grants[scope] ?? []).join(', ') || 'nothing here'}</span></span></span>
        <Badge label={k.revoked ? 'Revoked' : 'Active'} tone={k.revoked ? 'bad' : 'good'}/>
        {!k.revoked ? <button type="button" className="danger-ghost small" onClick={() => void revoke(k)}>Revoke</button> : <span className="row-spacer"/>}
      </div>)}</div>}
  </section>;
}

const permissionInfo: Record<Permission, string> = {
  read: 'see skills and sessions',
  write: 'record sessions and learn skills',
  delete: 'delete skills and sessions',
  admin: 'manage keys',
};

function About() {
  const {session, scope} = useWorkspace();
  const granted = session.grants[scope] ?? [];
  return <section className="card">
    <div className="card-head"><div><h2>This workspace</h2></div></div>
    <dl className="kv wide pad">
      <div><dt>Workspace</dt><dd>{scope}</dd></div>
      <div><dt>Server</dt><dd><code>{window.location.origin}</code></dd></div>
      <div><dt>Your key can</dt><dd>{granted.map(p => permissionInfo[p]).join(', ') || 'nothing'}</dd></div>
      <div><dt>Key ID</dt><dd><CopyText value={session.key_id} label="Copy key ID"/></dd></div>
    </dl>
  </section>;
}
