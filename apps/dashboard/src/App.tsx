import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {createClient, redeemSignIn, type Client, type Permission, type ProcedureSummary, type RunSummary, type Session, type Usage} from './api';
import {ErrorBoundary, FeedbackProvider, Icon, Logo} from './ui';
import {parseRoute, routeHref, views, WorkspaceContext, type Route, type View, type WorkspaceState} from './workspace';
import {Activity} from './views/Activity';
import {Home} from './views/Home';
import {MemoryGraphView} from './views/MemoryGraph';
import {Login} from './views/Login';
import {Playground} from './views/Playground';
import {Api, Settings} from './views/Settings';
import {Skills} from './views/Skills';

const KEY_STORAGE = 'musclememory.key';

function storage(kind: 'session' | 'local') {
  try {return kind === 'session' ? window.sessionStorage : window.localStorage;} catch {return undefined;}
}


export default function App() {
  const [auth, setAuth] = useState<{client: Client; session: Session}>();
  const signInCode = location.hash.match(/^#\/sign-in\/([\w-]+)/)?.[1];
  const [restoring, setRestoring] = useState(() => Boolean(signInCode || storage('session')?.getItem(KEY_STORAGE)));
  const [signInError, setSignInError] = useState('');

  useEffect(() => {
    // A one-time link from `musclememory dashboard`: swap it for a key, then forget the link.
    if (signInCode) {
      history.replaceState(null, '', '#/home');
      redeemSignIn(signInCode)
        .then(async key => {const client = createClient(key); signIn(key, await client.session());})
        .catch(e => setSignInError((e as Error).message))
        .finally(() => setRestoring(false));
      return;
    }
    const stored = storage('session')?.getItem(KEY_STORAGE);
    if (!stored) return;
    const client = createClient(stored);
    client.session().then(session => setAuth({client, session})).catch(() => storage('session')?.removeItem(KEY_STORAGE)).finally(() => setRestoring(false));
  }, []);

  const signIn = (key: string, session: Session) => {
    try {storage('session')?.setItem(KEY_STORAGE, key);} catch {/* keep in memory only */}
    setAuth({client: createClient(key), session});
  };
  const signOut = () => {storage('session')?.removeItem(KEY_STORAGE); setAuth(undefined); history.replaceState(null, '', '#/home');};

  return <FeedbackProvider>
    {restoring ? <div className="boot"><Logo height={22}/></div>
      : auth ? <Shell client={auth.client} session={auth.session} onSignOut={signOut}/>
      : <Login onSignIn={signIn} notice={signInError}/>}
  </FeedbackProvider>;
}

function Shell({client, session, onSignOut}: {client: Client; session: Session; onSignOut: () => void}) {
  const namespaces = useMemo(() => Object.keys(session.grants).filter(n => session.grants[n].includes('read')), [session]);
  const [scope, setScope] = useState(namespaces[0]);
  const [route, setRoute] = useState<Route>(() => parseRoute(location.hash));
  const [data, setData] = useState<{procedures?: ProcedureSummary[]; runs?: RunSummary[]; runTotal?: number; usage?: Usage}>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [nonce, setNonce] = useState(0);
  const [navOpen, setNavOpen] = useState(false);
  const [scrolled, setScrolled] = useState(false);
  useEffect(() => {
    const onScroll = () => setScrolled(window.scrollY > 4);
    onScroll();
    window.addEventListener('scroll', onScroll, {passive: true});
    return () => window.removeEventListener('scroll', onScroll);
  }, []);
  const mainRef = useRef<HTMLElement>(null);

  useEffect(() => {
    const onHash = () => setRoute(parseRoute(location.hash));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError('');
    Promise.all([client.procedures(scope, controller.signal), client.runPage(scope, 0, 'all', '', controller.signal), client.usage(scope, controller.signal)])
      .then(([procedures, page, usage]) => setData({procedures, runs: page.items, runTotal: page.total, usage}))
      .catch(e => {if (!controller.signal.aborted) setError((e as Error).message);})
      .finally(() => {if (!controller.signal.aborted) setLoading(false);});
    return () => controller.abort();
  }, [client, scope, nonce]);

  const navigate = useCallback((next: Route) => {
    const href = routeHref(next);
    if (location.hash !== href) location.hash = href; else setRoute(next);
    setNavOpen(false);
  }, []);
  const changeView = (view: View) => {navigate({view}); mainRef.current?.focus({preventScroll: true}); window.scrollTo({top: 0});};
  const reload = useCallback(() => setNonce(n => n + 1), []);
  const can = useCallback((permission: Permission) => session.grants[scope]?.includes(permission) ?? false, [session, scope]);

  const state: WorkspaceState = {client, session, scope, namespaces, ...data, loading, error, reload, can, navigate};
  const meta = views[route.view];
  const waiting = data.procedures?.filter(p => p.status === 'flagged' || (p.needs_review && p.status !== 'retired')).length;

  return <WorkspaceContext.Provider value={state}>
    <a href="#main" className="skip-link" onClick={e => {e.preventDefault(); mainRef.current?.focus();}}>Skip to content</a>
    <div className={`shell ${navOpen ? 'nav-open' : ''}`}>
      <aside className="sidebar">
        <div className="sidebar-top">
          <a className="brand" href="#/home"><Logo/><span className="brand-name">MuscleMemory</span></a>
          <button type="button" className="icon-button nav-close" aria-label="Close menu" onClick={() => setNavOpen(false)}><Icon name="close"/></button>
        </div>
        {namespaces.length > 1 && <label className="workspace-select">Workspace
          <select value={scope} onChange={e => {setScope(e.target.value); setData({}); navigate({view: route.view});}}>{namespaces.map(n => <option key={n} value={n}>{n}</option>)}</select>
        </label>}
        <nav aria-label="Main">
          {(['memory', 'developer'] as const).map(group => <div className="nav-group" key={group}>
            <p className="nav-label">{group === 'memory' ? 'Memory' : 'Developer'}</p>
            {(Object.keys(views) as View[]).filter(v => views[v].group === group).map(v => <a key={v} href={routeHref({view: v})} aria-current={route.view === v ? 'page' : undefined} onClick={e => {e.preventDefault(); changeView(v);}}>
              <Icon name={views[v].icon}/><span>{views[v].label}</span>{v === 'skills' && waiting ? <span className="nav-count tone-bad">{waiting}</span> : null}
            </a>)}
          </div>)}
        </nav>
        <div className="sidebar-foot">
          <a className="maker" href="https://thewariend.com/musclememory" target="_blank" rel="noreferrer" title="MuscleMemory on thewariend.com"><img src="/dashboard/brand/thewariend-mark.png" alt="TheWariend" width="18" height="18"/><span>Open source · Apache-2.0</span></a>
          <button type="button" className="icon-button" onClick={onSignOut} aria-label="Sign out" title="Sign out"><Icon name="logout"/></button>
        </div>
      </aside>
      <div className="nav-scrim" onClick={() => setNavOpen(false)}/>
      <div className="main-column">
        <header className={`topbar ${scrolled ? 'scrolled' : ''}`}>
          <button type="button" className="icon-button nav-toggle" aria-label="Open menu" onClick={() => setNavOpen(true)}><Icon name="menu"/></button>
          <div className="topbar-actions">
            <button type="button" className="ghost-button" onClick={reload} disabled={loading} aria-label="Refresh"><span className={loading ? 'spin' : ''}><Icon name="refresh" size={14}/></span><span className="hide-sm">Refresh</span></button>
          </div>
        </header>
        <main id="main" ref={mainRef} tabIndex={-1}>
          <ErrorBoundary resetKey={route.view}>
          <div className="page" key={route.view}>
          <div className="page-head">
            <div><h1>{meta.label}</h1><p>{meta.description}</p></div>
            <button type="button" className="ghost-button refresh-inline" onClick={reload} disabled={loading} aria-label="Refresh"><span className={loading ? 'spin' : ''}><Icon name="refresh" size={14}/></span>Refresh</button>
          </div>
          {error && <div role="alert" className="banner tone-bad"><Icon name="alert"/><span>{error}</span><button type="button" className="ghost-button small" onClick={reload}>Try again</button></div>}
          {route.view === 'home' && <Home/>}
          {route.view === 'skills' && <Skills selected={route.id}/>}
          {route.view === 'graph' && <MemoryGraphView root={route.id}/>}
          {route.view === 'playground' && <Playground/>}
          {route.view === 'activity' && <Activity selected={route.id}/>}
          {route.view === 'api' && <Api/>}
          {route.view === 'settings' && <Settings/>}
          </div>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  </WorkspaceContext.Provider>;
}
