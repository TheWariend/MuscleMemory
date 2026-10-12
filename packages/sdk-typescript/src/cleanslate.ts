import {ObservedRun, CaptureError, type CaptureCheckpoint} from './observer.js';
import type {MuscleMemoryClient} from './index.js';
import type {RuntimeProfile} from './contracts.js';
import {redact, safeCapture} from './redaction.js';

/** Structurally compatible with @cleanslate/sdk's public stream; no vendor dependency. */
export interface CleanSlatePart {
  type: string;
  toolName?: string;
  toolCallId?: string;
  input?: unknown;
  result?: unknown;
}

export interface CleanSlateCaptureOptions {
  effects?: Record<string, 'read' | 'write' | 'destructive'>;
  run_token?: string;
  checkpoint?: CaptureCheckpoint;
  /** Capture failures are reported without changing the agent's own tool results. */
  onCaptureError?: (error: unknown) => void;
}

export class CleanSlateCapture {
  readonly run: ObservedRun;
  private active = new Map<string, {name: string; input: Record<string, unknown>; started: number}>();
  private bindings = new Map<string, string>();
  private gaps = new Set<string>();
  private ended = false;
  private ignored = new Set<string>();

  constructor(private client: MuscleMemoryClient, private intent: string,
              private profile: RuntimeProfile, private scope: string,
              private options: CleanSlateCaptureOptions = {}) {
    this.run = new ObservedRun(client, intent, profile, scope, options.run_token,
      options.checkpoint ? state => options.checkpoint!({...state, capture_gaps: this.capture_gaps,
        capture_ended: this.ended}) : undefined);
    for (const [capability, tool] of Object.entries(profile.bindings ?? {})) {
      if (this.bindings.has(tool)) throw new CaptureError('ambiguous_tool_binding');
      this.bindings.set(tool, capability);
    }
  }

  get capture_gaps(): string[] { return [...this.gaps]; }

  /** Return data for the host to add to its normal context. No execution or approval. */
  recall() {
    return this.client.musclememory_recall({intent_text: this.intent, runtime_profile: this.profile,
      visibility_scope: this.scope, match_limit: 1});
  }

  observe(part: CleanSlatePart): void {
    if (part.type !== 'tool_start' && part.type !== 'tool_result') return;
    if (this.ended) { this.gaps.add('event_after_stream_end'); return; }
    if (part.toolCallId && this.ignored.has(part.toolCallId)) {
      if (part.type === 'tool_result') this.ignored.delete(part.toolCallId);
      return;
    }
    if (part.toolName?.includes('musclememory_')) return;
    const id = part.toolCallId;
    if (!id || !part.toolName) { this.gaps.add('missing_tool_call_id'); return; }
    if (part.type === 'tool_start') {
      const inputObject = part.input && typeof part.input === 'object' ? part.input as Record<string, unknown> : {};
      if (part.toolName === 'mcp_call_tool' && typeof inputObject.toolName === 'string' &&
          inputObject.toolName.includes('musclememory_')) {
        this.ignored.add(id);
        return;
      }
      if (part.toolName === 'spawn_worker' || part.toolName === 'start_background_command') {
        this.gaps.add('nested_execution_not_captured');
      }
      if (this.active.size && !this.active.has(id)) this.gaps.add('parallel_dependencies_unknown');
      if (this.active.has(id)) { this.gaps.add('duplicate_tool_start'); return; }
      const input = safeCapture(part.input);
      this.active.set(id, {name: part.toolName, input:
        input !== null && typeof input === 'object' && !Array.isArray(input) ?
          input as Record<string, unknown> : {input}, started: performance.now()});
      return;
    }
    const start = this.active.get(id);
    this.active.delete(id);
    if (!start || start.name !== part.toolName) { this.gaps.add('unmatched_tool_result'); return; }
    const capability = this.bindings.get(part.toolName);
    if (!capability) { this.gaps.add(`unmapped_tool:${part.toolName}`); return; }
    const result = safeCapture(part.result);
    const object = result !== null && typeof result === 'object' && !Array.isArray(result) ?
      result as Record<string, unknown> : {};
    const failed = object.success === false || object.isError === true ||
      (typeof object.exitCode === 'number' && object.exitCode !== 0) ||
      (typeof object.exit_code === 'number' && object.exit_code !== 0);
    this.run.record_tool(capability, start.input, redact(result), failed ? 'failure' : 'success',
      Math.max(0, performance.now() - start.started), this.options.effects?.[part.toolName] ?? 'destructive');
  }

  /** Preserve the exact original stream parts and their order, including exceptions. */
  async *capture<T extends CleanSlatePart>(source: AsyncIterable<T>): AsyncGenerator<T> {
    try {
      for await (const part of source) {
        try { this.observe(part); }
        catch (error) {
          this.gaps.add('capture_error');
          try { this.options.onCaptureError?.(error); } catch { /* Preserve the host's stream. */ }
        }
        yield part;
      }
    } catch (error) {
      this.gaps.add('stream_failed');
      throw error;
    } finally {
      this.ended = true;
      if (this.active.size) this.gaps.add('unfinished_tool');
      try { this.run.checkpoint_state(); }
      catch (error) {
        this.gaps.add('checkpoint_failed');
        try { this.options.onCaptureError?.(error); } catch { /* Preserve stream exceptions. */ }
      }
    }
  }

  /** Call only after an owned verifier/human checks the actual task outcome. */
  async finish(succeeded: boolean): Promise<Record<string, unknown>> {
    if (!this.ended || this.gaps.size || this.active.size) throw new CaptureError('incomplete_stream_capture');
    return this.run.complete(succeeded);
  }
}
