import type {CreateRunInput, ObserveInput, LearnInput, RecallInput, EvaluateInput, InspectInput, SessionUsageInput} from './contracts.js';
import {redact} from './redaction.js';
export * from './contracts.js';
export {ObservedRun, CaptureError} from './observer.js';
export type {ObservationSink, CaptureOptions, CaptureState, CaptureCheckpoint} from './observer.js';
export {redact} from './redaction.js';
export {CleanSlateCapture} from './cleanslate.js';
export type {CleanSlatePart, CleanSlateCaptureOptions} from './cleanslate.js';
export {CLEANSLATE_DEFAULT_PROFILE, CLEANSLATE_DEFAULT_EFFECTS} from './cleanslate-defaults.js';

export class MuscleMemoryError extends Error {
  constructor(public status: number, public code: string) { super(`MuscleMemory ${status}: ${code}`); }
}

/** Inputs are generated from the same Python v1 models used by REST and MCP. */
export class MuscleMemoryClient {
  constructor(private baseUrl: string, private apiKey: string,
              private fetcher: typeof fetch = fetch, private timeoutMs = 30_000) {}

  private async post(path: string, data: unknown): Promise<Record<string, unknown>> {
    const response = await this.fetcher(this.baseUrl.replace(/\/$/, '') + path, {
      method: 'POST', headers: {'Authorization': `Bearer ${this.apiKey}`, 'Content-Type': 'application/json'},
      body: JSON.stringify(redact(data)), signal: AbortSignal.timeout(this.timeoutMs),
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      const code = body?.error?.code;
      throw new MuscleMemoryError(response.status,
        typeof code === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : 'request_failed');
    }
    return response.json();
  }

  create_run(input: CreateRunInput) { return this.post('/v1/runs', input); }
  musclememory_observe(input: ObserveInput) { return this.post('/v1/observe', input); }
  musclememory_learn(input: LearnInput) { return this.post('/v1/learn', input); }
  musclememory_recall(input: RecallInput) { return this.post('/v1/recall', input); }
  musclememory_evaluate(input: EvaluateInput) { return this.post('/v1/evaluate', input); }
  musclememory_inspect(input: InspectInput) { return this.post('/v1/inspect', input); }
  record_session_usage(runToken: string, input: SessionUsageInput) {
    if (!/^[a-zA-Z0-9_.:-]{1,128}$/.test(runToken)) throw new Error('Invalid run token');
    return this.post(`/v1/runs/${runToken}/usage`, input);
  }
}

