/** Optional local host lifecycle adapter. Credentials stay outside agent context. */
import fs from 'node:fs/promises';
import path from 'node:path';
import {createHash, randomUUID} from 'node:crypto';
import {execFileSync} from 'node:child_process';
import {statSync} from 'node:fs';
import {MuscleMemoryClient} from './index.js';
import {CleanSlateCapture, type CleanSlatePart} from './cleanslate.js';
import {CLEANSLATE_DEFAULT_PROFILE, CLEANSLATE_DEFAULT_EFFECTS} from './cleanslate-defaults.js';
import {NodeCaptureJournal, captureIdentity} from './node.js';
import type {RuntimeProfile} from './contracts.js';

interface HostConfiguration {
  url: string; api_key: string; namespace: string;
  profile?: RuntimeProfile;
  effects?: Record<string, 'read' | 'write' | 'destructive'>;
}

/** One instance per native runtime, one private journal per invocation. */
export class CleanSlateHostMemory {
  private configuration: Promise<HostConfiguration | undefined> | undefined;
  private active: CleanSlateCapture | undefined;
  private recalled: Promise<string> | undefined;
  private delivered = new Set<string>();
  private handedOver: string | undefined;

  constructor(private configFile: string, private stateRoot: string,
              private report: () => void = () => {}, private workspaceRoot?: string) {}

  /** Files that differ from git, by size and modification time. Never reads contents. */
  private workspaceState(): Record<string, string> | undefined {
    const root = this.workspaceRoot;
    if (!root) return undefined;
    const listed = execFileSync('git', ['-C', root, 'status', '--porcelain=v1', '-z', '--untracked-files=all'],
      {encoding: 'utf8', timeout: 5000, stdio: ['ignore', 'pipe', 'ignore'], env: {...process.env, GIT_OPTIONAL_LOCKS: '0'}});
    const state: Record<string, string> = {};
    const entries = listed.split('\0');
    for (let index = 0; index < entries.length; index++) {
      const entry = entries[index];
      if (entry.length < 4) continue;
      const code = entry.slice(0, 2), file = entry.slice(3);
      if (code[0] === 'R' || code[0] === 'C') index++;
      if (/(^|\/)(node_modules|\.git|__pycache__|\.venv|dist|build)\//.test(file) || /\.(pyc|log)$/.test(file)) continue;
      try {
        const info = statSync(path.join(root, file));
        state[file] = createHash('sha1').update(`${info.size}:${info.mtimeMs}`).digest('hex').slice(0, 16);
      } catch { state[file] = 'missing'; }
    }
    return state;
  }

  private config(): Promise<HostConfiguration | undefined> {
    return this.configuration ??= (async () => {
      const info = await fs.lstat(this.configFile);
      if (!info.isFile() || info.isSymbolicLink() || info.mode & 0o077 || info.size > 1024 * 1024 ||
          (process.getuid && info.uid !== process.getuid())) throw new Error('private_config_required');
      const config = JSON.parse(await fs.readFile(this.configFile, 'utf8')) as HostConfiguration;
      const endpoint = new URL(config.url);
      if (!config.api_key || !config.namespace || endpoint.username || endpoint.password || endpoint.search || endpoint.hash ||
          (endpoint.protocol !== 'https:' && !(endpoint.protocol === 'http:' &&
            ['localhost', '127.0.0.1', '[::1]'].includes(endpoint.hostname)))) throw new Error('invalid_host_config');
      return config;
    })().catch(() => { this.warn(); return undefined; });
  }

  private warn(): void { try { this.report(); } catch { /* Reporting cannot stop an agent. */ } }

  context(_task?: string): Promise<string> {
    if (!this.active) return Promise.resolve('');
    return this.recalled ??= this.active.recall().then(result => {
      if (typeof result.guidance !== 'string' || !result.guidance) return '';
      const pointer = JSON.parse(result.guidance);
      const identity = `${pointer.procedure}:${pointer.version}`;
      if (this.delivered.has(identity)) return '';
      this.delivered.add(identity);
      this.handedOver = typeof pointer.procedure === 'string' ? pointer.procedure : undefined;
      return result.guidance;
    }).catch(() => '');
  }

  private async *capture<T extends CleanSlatePart>(intent: string, source: AsyncIterable<T>): AsyncGenerator<T> {
    let capture: CleanSlateCapture | undefined;
    try {
      const config = await this.config();
      if (config && !this.active) {
        const client = new MuscleMemoryClient(config.url, config.api_key, fetch, 2000);
        const journal = new NodeCaptureJournal(path.join(this.stateRoot, `${randomUUID()}.json`),
          captureIdentity(config.url, config.api_key));
        capture = new CleanSlateCapture(client, intent,
          config.profile ?? {...CLEANSLATE_DEFAULT_PROFILE, permissions: []}, config.namespace,
          {effects: config.effects ?? CLEANSLATE_DEFAULT_EFFECTS, checkpoint: journal.save, onCaptureError: () => this.warn(),
            workspaceRoot: this.workspaceRoot, workspaceState: () => this.workspaceState()});
        this.active = capture;
        this.recalled = undefined;
        this.handedOver = undefined;
      }
    } catch { this.warn(); }
    try { yield* capture ? capture.capture(source) : source; }
    finally {
      if (capture) {
        try { await capture.run.flush(); } catch { this.warn(); }
        // Tests passed after the last change: learn from it, or count it as a check.
        try { await capture.finishIfVerified(this.handedOver); } catch { this.warn(); }
        if (this.active === capture) { this.active = undefined; this.recalled = undefined; }
      }
    }
  }

  /** Forward all native methods; observe every top-level execution entry point. */
  attach<T extends object>(runtime: T): T {
    const memory = this;
    const entries = new Set(['run', 'plan', 'approvePlan', 'resumePendingQuestion']);
    return new Proxy(runtime, {get(target, property) {
      const value = Reflect.get(target, property, target);
      if (typeof value !== 'function') return value;
      if (!entries.has(String(property))) return value.bind(target);
      return (...args: unknown[]) => {
        let snapshot;
        if (typeof args[0] !== 'string') {
          try {
            const snapshotMethod: unknown = Reflect.get(target, 'getSessionSnapshot');
            snapshot = typeof snapshotMethod === 'function' ? Reflect.apply(snapshotMethod, target, []) : undefined;
          } catch { memory.warn(); }
        }
        const objective = typeof args[0] === 'string' ? args[0] : snapshot?.agent?.objective ?? 'Continue the current task';
        return memory.capture(objective, Reflect.apply(value, target, args));
      };
    }});
  }
}
