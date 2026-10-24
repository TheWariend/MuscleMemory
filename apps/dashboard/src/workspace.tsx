import {createContext, useContext} from 'react';
import type {Client, Permission, ProcedureSummary, RunSummary, Session, Usage} from './api';
import type {IconName} from './ui';

export type View = 'home' | 'skills' | 'graph' | 'playground' | 'activity' | 'api' | 'settings';
export type Route = {view: View; id?: string};

export const views: Record<View, {label: string; icon: IconName; group: 'memory' | 'developer'; description: string}> = {
  home: {label: 'Home', icon: 'overview', group: 'memory', description: 'What your agents have learned, at a glance.'},
  skills: {label: 'Skills', icon: 'procedures', group: 'memory', description: 'Reusable methods, their evidence, and what still needs validation.'},
  graph: {label: 'Memory graph', icon: 'graph', group: 'memory', description: 'Follow skills back to their lessons, source events, and earlier versions.'},
  playground: {label: 'Playground', icon: 'recall', group: 'memory', description: 'Try a task and preview which skill your agent would get. Nothing is sent to your agent.'},
  activity: {label: 'Activity', icon: 'runs', group: 'memory', description: 'Agent sessions MuscleMemory recorded. Skills are learned from these.'},
  api: {label: 'API keys', icon: 'key', group: 'developer', description: 'Keys and addresses your agents and your own code use to reach MuscleMemory.'},
  settings: {label: 'Connect & settings', icon: 'connect', group: 'developer', description: 'Connect an agent and see this workspace.'},
};

// Older links keep working.
const aliases: Record<string, View> = {overview: 'home', procedures: 'skills', runs: 'activity', recall: 'playground', map: 'graph', connect: 'settings', keys: 'api', workspace: 'settings'};

export function parseRoute(hash: string): Route {
  const [raw, id] = hash.replace(/^#\/?/, '').split('/');
  const view = raw in views ? (raw as View) : aliases[raw];
  return view ? {view, id: id ? decodeURIComponent(id) : undefined} : {view: 'home'};
}

export function routeHref(route: Route) {
  return `#/${route.view}${route.id ? `/${encodeURIComponent(route.id)}` : ''}`;
}

export type WorkspaceState = {
  client: Client; session: Session; scope: string; namespaces: string[];
  procedures?: ProcedureSummary[]; runs?: RunSummary[]; runTotal?: number; usage?: Usage;
  loading: boolean; error: string; reload: () => void;
  can: (permission: Permission) => boolean; navigate: (route: Route) => void;
};

export const WorkspaceContext = createContext<WorkspaceState>(null as unknown as WorkspaceState);
export const useWorkspace = () => useContext(WorkspaceContext);
