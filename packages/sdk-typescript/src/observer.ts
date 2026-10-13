import type {Condition, ExecutionEvent, ObserveInput, RuntimeProfile, Measurements} from './contracts.js';
import {captureSize, redact, safeCapture} from './redaction.js';

export interface ObservationSink {
  musclememory_observe(input: ObserveInput): Promise<Record<string, unknown>>;
}

export interface CaptureState {
  run_token: string;
  runtime_profile: RuntimeProfile;
  visibility_scope: string;
  events: ExecutionEvent[];
  acknowledged_count: number;
  capture_gaps?: string[];
  capture_ended?: boolean;
}
export type CaptureCheckpoint = (state: CaptureState) => void;

export class CaptureError extends Error {
  constructor(public code: string) { super(`MuscleMemory capture: ${code}`); }
}

export interface CaptureOptions<Args extends unknown[]> {
  arguments?: (args: Args) => Record<string, unknown>;
  dependencies?: string[];
  retry_of?: string;
  exploratory?: boolean;
  permission?: string;
  effect?: 'read' | 'write' | 'destructive';
  condition?: Condition;
}

function token(value: unknown): value is string {
  return typeof value === 'string' && value.length <= 128 && /^[a-zA-Z0-9_.:-]+$/.test(value);
}

function validProfile(profile: RuntimeProfile): boolean {
  if (profile === null || typeof profile !== 'object' || Array.isArray(profile)) return false;
  const allowed = ['agent_name', 'bindings', 'environment', 'permissions'];
  const bindings = profile.bindings === undefined ? {} : profile.bindings;
  const environment = profile.environment === undefined ? {} : profile.environment;
  const permissions = profile.permissions === undefined ? [] : profile.permissions;
  return Object.keys(profile).every(key => allowed.includes(key)) &&
    (profile.agent_name === undefined || typeof profile.agent_name === 'string' && profile.agent_name.length <= 100) &&
    bindings !== null && typeof bindings === 'object' && !Array.isArray(bindings) && Object.keys(bindings).length <= 128 &&
    Object.entries(bindings).every(([capability, name]) => /^[a-z][a-z0-9_.-]+$/.test(capability) && capability.length <= 96 && token(name)) &&
    environment !== null && typeof environment === 'object' && !Array.isArray(environment) && Object.keys(environment).length <= 64 &&
    Object.entries(environment).every(([name, value]) => token(name) && typeof value === 'string') &&
    Array.isArray(permissions) && permissions.length <= 64 && permissions.every(token);
}

/** Serial sync/async tool-boundary capture. This wrapper never executes remembered steps. */
export class ObservedRun {
  readonly run_token: string;
  private profile: RuntimeProfile;
  private scope: string;
  private history: ExecutionEvent[] = [];
  private pending: ExecutionEvent[] = [];
  private closed = false;
  private active = false;
  private flushing: Promise<Record<string, unknown>> | null = null;
  private captureGaps: string[] | undefined;
  private captureEnded: boolean | undefined;

  constructor(private client: ObservationSink, intent: string, runtime: RuntimeProfile,
              visibilityScope: string, runToken: string = crypto.randomUUID(),
              private checkpoint?: CaptureCheckpoint) {
    this.run_token = runToken;
    this.scope = visibilityScope;
    this.profile = redact(runtime) as RuntimeProfile;
    if (!token(redact(runToken)) || !token(redact(visibilityScope)) ||
        !intent || intent.length > 8000 || captureSize(this.profile) > 65536 || !validProfile(this.profile)) {
      throw new CaptureError('invalid_metadata');
    }
    this.emit({kind: 'intent', intent_text: intent});
  }

  /** Resume delivery of data only. Previously executed tools are never replayed. */
  static resume(client: ObservationSink, state: CaptureState, checkpoint?: CaptureCheckpoint): ObservedRun {
    const events = state.events;
    if (!Array.isArray(events) || !events.length || events.length > 1024 ||
        !Number.isInteger(state.acknowledged_count) || state.acknowledged_count < 0 ||
        state.acknowledged_count > events.length || events[0].kind !== 'intent') throw new CaptureError('invalid_journal');
    const run = new ObservedRun(client, events[0].intent_text ?? '', state.runtime_profile,
      state.visibility_scope, state.run_token);
    for (const [index, event] of events.entries()) {
      if (event.sequence !== index || !token(event.event_id) || captureSize(event) > 65536 ||
          (index !== 0 && event.kind === 'intent') ||
          !['intent', 'tool', 'completion'].includes(event.kind) ||
          (event.kind === 'completion' && index !== events.length - 1) ||
          !['success', 'failure'].includes(event.status ?? 'success') ||
          !['read', 'write', 'destructive'].includes(event.effect ?? 'read') ||
          !Number.isFinite(event.duration_ms ?? 0) || (event.duration_ms ?? 0) < 0 ||
          events.slice(0, index).some(prior => prior.event_id === event.event_id) ||
          (event.kind === 'tool' && (!event.capability || state.runtime_profile.bindings?.[event.capability] !== event.tool_name))) {
        throw new CaptureError('invalid_journal');
      }
    }
    run.history = structuredClone(redact(events)) as ExecutionEvent[];
    run.pending = run.history.slice(state.acknowledged_count);
    run.closed = run.history.at(-1)?.kind === 'completion';
    if (state.capture_gaps !== undefined) {
      if (!Array.isArray(state.capture_gaps) || state.capture_gaps.length > 100 ||
          !state.capture_gaps.every(gap => typeof gap === 'string' && gap.length <= 256)) {
        throw new CaptureError('invalid_journal');
      }
      run.captureGaps = [...state.capture_gaps];
      run.captureEnded = state.capture_ended ?? false;
    }
    if (state.capture_ended !== undefined && typeof state.capture_ended !== 'boolean') throw new CaptureError('invalid_journal');
    if (state.capture_ended !== undefined) run.captureEnded = state.capture_ended;
    run.checkpoint = checkpoint;
    return run;
  }

  private save(events = this.history, acknowledged = this.history.length - this.pending.length): void {
    this.checkpoint?.({run_token: this.run_token, runtime_profile: structuredClone(this.profile),
      visibility_scope: this.scope, events: structuredClone(events), acknowledged_count: acknowledged,
      ...(this.captureGaps ? {capture_gaps: [...this.captureGaps]} : {}),
      ...(this.captureEnded !== undefined ? {capture_ended: this.captureEnded} : {})});
  }

  /** Defensive snapshots prevent callers from changing already captured events. */
  get events(): ExecutionEvent[] { return structuredClone(this.history); }
  get pending_count(): number { return this.pending.length; }
  checkpoint_state(): void { this.save(); }

  /** Capture a result supplied by an owned client stream; never executes the tool. */
  record_tool(capability: string, arguments_: Record<string, unknown>, response: unknown,
              status: 'success' | 'failure', durationMs = 0,
              effect: 'read' | 'write' | 'destructive' = 'read', outcomeVerified = true): void {
    this.ensureOpen();
    const name = this.profile.bindings?.[capability];
    if (!token(name) || !['success', 'failure'].includes(status) ||
        !Number.isFinite(durationMs) || durationMs < 0 ||
        !['read', 'write', 'destructive'].includes(effect)) throw new CaptureError('invalid_stream_event');
    this.emit({kind: 'tool', tool_name: name, capability,
      arguments: safeCapture(arguments_) as ExecutionEvent['arguments'],
      response: safeCapture(response) as ExecutionEvent['response'], status,
      duration_ms: durationMs, effect, ...(outcomeVerified ? {} : {outcome_verified: false})});
  }

  private ensureOpen(completion = false): void {
    if (this.closed) throw new CaptureError('trace_closed');
    if (this.active || this.flushing) throw new CaptureError('serial_capture_busy');
    if (this.history.length >= (completion ? 1024 : 1023)) throw new CaptureError('trace_limit');
  }

  private emit(fields: Partial<ExecutionEvent> & {kind: ExecutionEvent['kind']}): void {
    const event = redact({
      event_id: `e${this.history.length}`, sequence: this.history.length,
      intent_text: null, tool_name: null, capability: null, arguments: {}, response: null,
      status: 'success', dependencies: [], retry_of: null, exploratory: false,
      permission: null, effect: 'read', condition: null, duration_ms: 0, token_usage: null,
      ...fields,
    }) as ExecutionEvent;
    if (captureSize(event) > 65536 && event.kind === 'tool') {
      event.response = {capture_note: 'Response omitted to fit event limit', $mm_capture_omitted: true};
    }
    if (captureSize(event) > 65536) throw new CaptureError('event_size');
    this.save([...this.history, event]);
    this.history.push(event);
    this.pending.push(event);
  }

  instrument<Args extends unknown[], Result>(capability: string, tool: (...args: Args) => Result,
                                            options: CaptureOptions<Args> = {}): (...args: Args) => Result {
    const run = this;
    const toolName = this.profile.bindings?.[capability];
    if (!/^[a-z][a-z0-9_.-]+$/.test(capability) || capability.length > 96 || !token(toolName)) {
      throw new CaptureError('capability_binding');
    }
    const metadata = redact({
      dependencies: options.dependencies ?? [], retry_of: options.retry_of ?? null,
      exploratory: options.exploratory ?? false, permission: options.permission ?? null,
      effect: options.effect ?? 'read', condition: options.condition ?? null,
    }) as Partial<ExecutionEvent>;
    if (!['read', 'write', 'destructive'].includes(metadata.effect ?? '') ||
        !Array.isArray(metadata.dependencies) || !metadata.dependencies.every(token) ||
        metadata.retry_of != null && !token(metadata.retry_of) ||
        metadata.permission != null && !token(metadata.permission) ||
        typeof metadata.exploratory !== 'boolean' ||
        metadata.condition != null && (!token(metadata.condition.parameter) ||
          !Object.keys(metadata.condition).every(key => ['parameter', 'equals'].includes(key)) ||
          metadata.condition.equals !== null && !['string', 'number', 'boolean'].includes(typeof metadata.condition.equals))) {
      throw new CaptureError('invalid_tool_metadata');
    }
    return function(this: unknown, ...args: Args): Result {
      run.ensureOpen();
      const dependencies = metadata.dependencies ?? [];
      if (dependencies.length > 128 || dependencies.some(id => !run.history.some(e => e.event_id === id)) ||
          (metadata.retry_of && !run.history.some(e => e.event_id === metadata.retry_of && e.status === 'failure')) ||
          (metadata.permission && !run.profile.permissions?.includes(metadata.permission))) {
        throw new CaptureError('invalid_tool_metadata');
      }
      const capturedArgs = safeCapture(options.arguments ? options.arguments(args) : {positional: args}) as Record<string, unknown>;
      if (capturedArgs === null || typeof capturedArgs !== 'object' || Array.isArray(capturedArgs)) {
        throw new CaptureError('invalid_arguments');
      }
      if (captureSize({...metadata, arguments: capturedArgs}) > 60000) throw new CaptureError('argument_size');
      run.active = true;
      const start = performance.now();
      const capture = (response: unknown, status: 'success' | 'failure') => {
        run.emit({kind: 'tool', tool_name: toolName, capability, arguments: capturedArgs,
          response: safeCapture(response), status, duration_ms: performance.now() - start, ...metadata});
      };
      const failed = (error: unknown): never => {
        try {
          capture(error instanceof Error ? {error_type: error.name, message: error.message} :
            {error_type: 'ThrownValue', value: safeCapture(error)}, 'failure');
        } catch { /* Telemetry failure must not replace the tool's original exception. */ }
        finally { run.active = false; }
        throw error;
      };
      let result: Result;
      try { result = Reflect.apply(tool, this, args); }
      catch (error) { return failed(error); }
      if (result instanceof Promise) {
        return result.then(value => {
          try { capture(value, 'success'); return value; }
          finally { run.active = false; }
        }, failed) as Result;
      }
      try { capture(result, 'success'); return result; }
      finally { run.active = false; }
    };
  }

  async complete(succeeded = true): Promise<Record<string, unknown>> {
    this.ensureOpen(true);
    if (this.captureGaps?.length || this.captureEnded === false) throw new CaptureError('incomplete_stream_capture');
    this.emit({kind: 'completion', status: succeeded ? 'success' : 'failure'});
    this.closed = true;
    return this.flush();
  }

  flush(): Promise<Record<string, unknown>> {
    if (this.active) return Promise.reject(new CaptureError('serial_capture_busy'));
    if (this.flushing) return this.flushing;
    const upload = async () => {
      let result: Record<string, unknown> = {accepted_event_count: 0, rejected_event_count: 0, duplicate_event_count: 0};
      while (this.pending.length) {
        const batch: ExecutionEvent[] = [];
        let size = 0;
        for (const event of this.pending.slice(0, 256)) {
          const bytes = captureSize(event);
          if (batch.length && size + bytes > 1024 * 1024) break;
          batch.push(event); size += bytes;
        }
        result = await this.client.musclememory_observe({run_token: this.run_token,
          runtime_profile: this.profile, visibility_scope: this.scope, event_batch: batch});
        if (result.rejected_event_count !== 0 ||
            typeof result.accepted_event_count !== 'number' ||
            typeof result.duplicate_event_count !== 'number' ||
            result.accepted_event_count < 0 || result.duplicate_event_count < 0 ||
            result.accepted_event_count + result.duplicate_event_count !== batch.length) {
          throw new CaptureError('upload_not_acknowledged');
        }
        this.save(this.history, this.history.length - this.pending.length + batch.length);
        this.pending.splice(0, batch.length);
      }
      return result;
    };
    this.flushing = upload().finally(() => { this.flushing = null; });
    return this.flushing;
  }

  measurements(): Measurements {
    const tools = this.history.filter(event => event.kind === 'tool');
    return {tool_calls: tools.length, duration_ms: tools.reduce((sum, event) => sum + (event.duration_ms ?? 0), 0),
      tokens: tools.length && tools.every(event => event.token_usage != null) ?
        tools.reduce((sum, event) => sum + event.token_usage!, 0) : null};
  }
}
