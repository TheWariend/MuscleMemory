import React, {useEffect, useState} from 'react';
import {checkHealth, createClient, type Session} from '../api';
import {CodeBlock, Icon, Logo} from '../ui';

export function Login({onSignIn, notice}: {onSignIn: (key: string, session: Session) => void; notice?: string}) {
  const [key, setKey] = useState('');
  const [reveal, setReveal] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [help, setHelp] = useState(false);
  const [server, setServer] = useState<{online: boolean; contract?: string}>();

  useEffect(() => {void checkHealth().then(setServer);}, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const credential = key.trim();
    setBusy(true); setError('');
    try {
      const session = await createClient(credential).session();
      if (!Object.values(session.grants).some(p => p.includes('read'))) throw new Error('This key can’t see any workspace. Create a key with read access.');
      onSignIn(credential, session);
    } catch (e) {setError((e as Error).message); setBusy(false);}
  }

  const host = window.location.host;
  return <div className="login">
    <main className="login-panel">
      <Logo height={22}/>
      <h1>Sign in to MuscleMemory</h1>
      <p className="login-sub">Running MuscleMemory on this computer? Run <code>musclememory dashboard</code> to open it already signed in. Otherwise, use an API key.</p>
      {notice && <p role="alert" className="field-error"><Icon name="alert" size={13}/>{notice}</p>}

      <form onSubmit={submit} className="login-form">
        <div className="field-row"><label htmlFor="key" className="field-label">API key</label><button type="button" className="link-button" aria-expanded={help} onClick={() => setHelp(!help)}>{help ? 'Hide help' : 'Don’t have one?'}</button></div>
        <div className={`input-wrap ${error ? 'invalid' : ''}`}>
          <input id="key" type={reveal ? 'text' : 'password'} autoComplete="off" spellCheck={false} required value={key} placeholder="mm_…" onChange={e => {setKey(e.target.value); setError('');}} aria-invalid={Boolean(error)} aria-describedby={error ? 'key-error' : undefined} autoFocus/>
          <button type="button" className="icon-button tiny" aria-label={reveal ? 'Hide key' : 'Show key'} aria-pressed={reveal} onClick={() => setReveal(!reveal)}><Icon name="eye" size={15}/></button>
        </div>
        {error && <p id="key-error" role="alert" className="field-error"><Icon name="alert" size={13}/>{error}</p>}
        {help && <div className="login-help">
          <p>Run this where MuscleMemory runs and paste the <code>api_key</code> it prints. Using someone else’s server? Ask them for a key.</p>
          <CodeBlock label="Terminal">{'musclememory key --tenant me --namespace default \\\n  --label dashboard --permissions read,write,delete,admin'}</CodeBlock>
        </div>}
        <button className="primary-button wide" disabled={busy || !key.trim() || server?.online === false}>{busy ? 'Checking…' : 'Sign in'}</button>
      </form>

      <div className="login-server">
        <span className={`status-dot ${server === undefined ? '' : server.online ? 'good' : 'bad'}`}/>
        <code>{host}</code>
        <span>{server === undefined ? 'checking…' : server.online ? `online${server.contract ? ` · contract v${server.contract}` : ''}` : 'unreachable'}</span>
      </div>
      <p className="login-note">Your key is kept in this browser tab only and is cleared when you close it.</p>
    </main>
    <footer className="login-foot">
      <p className="login-foot-brand">musclememory</p>
      <p>developed by thewariend</p>
    </footer>
  </div>;
}
