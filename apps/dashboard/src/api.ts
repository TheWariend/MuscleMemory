export type Permission = 'read' | 'write' | 'delete' | 'admin';
export type Session = {tenant: string; grants: Record<string, Permission[]>; key_id: string};
export type ProcedureSummary = {
  procedure_token: string; version: number; status: string; title?: string; step_count?: number;
  successes?: number; failures?: number; confidence?: number; updated_at?: string;
  needs_review?: boolean; auto_learned?: boolean;
  learned_from?: string; changes?: string[]; check?: string[];
  /** A title from Claude Code or Codex, else a name built from the files it changed and its test. */
  name?: string;
};
export type RunSummary = {run_token: string; intent_text: string; status: string; agent_name?: string | null; created_at?: string};
export type Usage = {
  operations: Array<{operation: string; requests: number; units: number}>;
  observed_savings: Record<string, number>; paired_measurements: Record<string, number>;
  measurement_source: string; evaluations: number;
};
export type Key = {key_id: string; label: string; revoked: boolean; grants: Record<string, Permission[]>};
export type Condition = {parameter: string; equals: unknown};
export type Step = {
  step_token: string; capability: string; source_tool: string; arguments: Record<string, unknown>;
  dependencies: string[]; condition: Condition | null; permission: string | null;
  effect: 'read' | 'write' | 'destructive'; requires_approval: boolean; critical?: boolean;
};
export type Definition = {
  title: string; intent_signature: string; guidance_notice: string;
  parameters: Record<string, {kind: string; description: string}>;
  preconditions: string[]; expected_outcomes: string[]; required_capabilities: string[];
  required_permissions: string[]; environment_requirements: Record<string, string>; steps: Step[];
  provenance: Record<string, unknown>; applicability_conditions?: string[]; risk_cues?: string[];
  avoidance_rules?: string[]; recovery_notes?: string[];
};
export type Measurements = {tool_calls: number; duration_ms: number; tokens?: number | null; cost_usd?: number | null};
export type ProcedureDetail = {
  procedure_token: string; procedure_version: number; visibility_scope: string; lifecycle_status: string; needs_review?: boolean;
  name?: string;
  definition: Definition; created_at: string; updated_at: string;
  version_history: Array<{version: number; status: string; successes: number; failures: number}>;
  validation_history: Array<{run_token: string; outcome: {succeeded?: boolean}; metrics: {observed?: Measurements; baseline?: Measurements | null}}>;
  contrasts: Array<{contrast_id: string; failed_run_token: string; successful_run_token: string; capability_signature: string[]; step_diff: unknown}>;
  successes: number; failures: number; confidence: number; stale: boolean;
  summary?: SkillSummary;
};
export type SkillSummary = {
  reads: string[]; changes: string[]; edits: Array<{file: string; before?: string; after?: string}>;
  test: string | null; look_around_steps: number; learned_by: string | null; checked_by: Record<string, number>;
};
export type SkillAgent = 'codex' | 'claude';
export type Preferences = {skill_titles: 'off' | SkillAgent; agents: Record<SkillAgent, string>};
export type ExportResult = {agent: SkillAgent; name: string; installed: boolean; unchanged: boolean};
export type RunEvent = {
  event_id: string; sequence: number; kind: 'intent' | 'tool' | 'completion'; intent_text: string | null;
  tool_name: string | null; capability: string | null; arguments: Record<string, unknown>; response: unknown;
  status: 'success' | 'failure'; dependencies: string[]; effect: 'read' | 'write' | 'destructive';
  duration_ms: number; token_usage: number | null; exploratory: boolean; retry_of: string | null;
};
export type RunDetail = {
  run_token: string; intent_text: string; status: string; events: RunEvent[];
  runtime_profile: {agent_name: string; bindings: Record<string, string>; environment: Record<string, string>; permissions: string[]};
};
export type RecallCandidate = {
  procedure_token: string; procedure_version: number; score: number; relevance: number; confidence: number;
  successes: number; failures: number; explanation: Record<string, unknown>; definition: Definition;
  resolved_tools: Record<string, string>;
};
export type RecallResult = {candidates: RecallCandidate[]; no_match: boolean; reason: string | null; guidance?: string | null};

export class ApiError extends Error {
  constructor(message: string, readonly status: number) {super(message);}
}

export function createClient(key: string) {
  async function call<T>(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    const response = await fetch(path, {
      method, signal,
      headers: {Authorization: `Bearer ${key}`, ...(body ? {'Content-Type': 'application/json'} : {})},
      body: body ? JSON.stringify(body) : undefined,
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      const message = response.status === 401 ? 'That key wasn’t accepted. Check it and try again.'
        : response.status === 429 ? 'Too many requests for this key. Wait a minute and try again.'
        : payload.error?.message
        ?? (payload.error?.code === 'invalid_input' ? 'Some fields are invalid. Check the values and try again.' : `Request failed (${response.status})`);
      throw new ApiError(message, response.status);
    }
    return response.json();
  }
  const scoped = (scope: string) => `visibility_scope=${encodeURIComponent(scope)}`;
  const token = encodeURIComponent;
  return {
    session: () => call<Session>('GET', '/v1/session'),
    usage: (scope: string, signal?: AbortSignal) => call<Usage>('GET', `/v1/usage?${scoped(scope)}`, undefined, signal),
    procedures: (scope: string, signal?: AbortSignal) =>
      call<{items: ProcedureSummary[]}>('GET', `/v1/procedures?${scoped(scope)}&limit=100`, undefined, signal).then(r => r.items),
    runs: (scope: string, signal?: AbortSignal) =>
      call<{items: RunSummary[]}>('GET', `/v1/runs?${scoped(scope)}&limit=100`, undefined, signal).then(r => r.items),
    keys: (scope: string, signal?: AbortSignal) => call<{items: Key[]}>('GET', `/v1/keys?${scoped(scope)}`, undefined, signal).then(r => r.items),
    procedure: (id: string, signal?: AbortSignal) => call<ProcedureDetail>('GET', `/v1/procedures/${token(id)}`, undefined, signal),
    run: (id: string, signal?: AbortSignal) => call<RunDetail>('GET', `/v1/runs/${token(id)}`, undefined, signal),
    recall: (body: {intent_text: string; visibility_scope: string; match_limit: number; runtime_profile: {agent_name: string; bindings: Record<string, string>}}) =>
      call<RecallResult>('POST', '/v1/recall', body),
    approve: (id: string) => call<{approved: boolean}>('POST', `/v1/procedures/${token(id)}/approve`),
    retire: (id: string) => call<{lifecycle_status: string}>('POST', `/v1/procedures/${token(id)}/retire`),
    deleteProcedure: (id: string) => call<{deleted: boolean}>('DELETE', `/v1/procedures/${token(id)}`),
    deleteRun: (id: string) => call<{deleted: boolean}>('DELETE', `/v1/runs/${token(id)}`),
    nameSkill: (id: string, agent: SkillAgent) => call<{name: string}>('POST', `/v1/procedures/${token(id)}/name?agent=${agent}`),
    preferences: (signal?: AbortSignal) => call<Preferences>('GET', '/v1/preferences', undefined, signal),
    setPreferences: (skill_titles: Preferences['skill_titles']) => call<Preferences>('PUT', '/v1/preferences', {skill_titles}),
    exportSkill: (id: string, agent: SkillAgent) => call<ExportResult>('POST', `/v1/procedures/${token(id)}/export?agent=${agent}`),
    revokeKey: (id: string, scope: string) => call<unknown>('DELETE', `/v1/keys/${token(id)}?${scoped(scope)}`),
  };
}
export type Client = ReturnType<typeof createClient>;

export async function checkHealth(): Promise<{online: boolean; contract?: string}> {
  try {
    const response = await fetch('/health');
    const body = await response.json().catch(() => ({}));
    return {online: response.ok, contract: body.contract_version};
  } catch {return {online: false};}
}

/** Swap a one-time code from `musclememory dashboard` for a dashboard key. */
export async function redeemSignIn(code: string): Promise<string> {
  const response = await fetch('/auth/sign-in', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({code})});
  if (!response.ok) throw new ApiError('This sign-in link expired. Run musclememory dashboard again.', response.status);
  return (await response.json()).api_key;
}
