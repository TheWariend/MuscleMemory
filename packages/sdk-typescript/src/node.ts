/** Optional Node-only durable journal. Browser imports never load node:fs. */
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import type {CaptureState} from './observer.js';
import {CaptureError} from './observer.js';
export {CleanSlateHostMemory} from './cleanslate-host.js';

export function captureIdentity(endpoint: string, key: string): string {
  return crypto.createHash('sha256').update(endpoint.replace(/\/$/, '') + '\0' + key).digest('hex');
}

export class NodeCaptureJournal {
  private lastDigest: string | undefined;
  constructor(readonly filename: string, private identity: string) {
    if (!/^[a-f0-9]{64}$/.test(identity)) throw new CaptureError('invalid_journal_identity');
    const parent = path.dirname(filename);
    fs.mkdirSync(parent, {recursive: true, mode: 0o700});
    const info = fs.lstatSync(parent);
    if (!info.isDirectory() || info.isSymbolicLink() || (info.mode & 0o077) ||
        (typeof process.getuid === 'function' && info.uid !== process.getuid())) throw new CaptureError('private_journal_directory');
    if (fs.existsSync(filename)) {
      this.checkFile();
      this.lastDigest = crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex');
    }
  }

  private checkFile(): void {
    const info = fs.lstatSync(this.filename);
    if (!info.isFile() || info.isSymbolicLink() || (info.mode & 0o077) || info.size > 80 * 1024 * 1024 ||
        (typeof process.getuid === 'function' && info.uid !== process.getuid())) throw new CaptureError('private_journal_file');
  }

  load(): CaptureState {
    this.checkFile();
    const record = JSON.parse(fs.readFileSync(this.filename, 'utf8'));
    if (record.version !== 1 || record.identity !== this.identity) throw new CaptureError('journal_identity_mismatch');
    return record.state;
  }

  /** One active writer per trace; use distinct files for concurrent executions. */
  save = (state: CaptureState): void => {
    const serialized = JSON.stringify({version: 1, identity: this.identity, state});
    if (Buffer.byteLength(serialized) > 80 * 1024 * 1024) throw new CaptureError('journal_size');
    if (fs.existsSync(this.filename)) {
      this.checkFile();
      const current = crypto.createHash('sha256').update(fs.readFileSync(this.filename)).digest('hex');
      if (current !== this.lastDigest) throw new CaptureError('journal_writer_conflict');
      const prior = this.load();
      if (prior.run_token !== state.run_token || prior.events.length > state.events.length) throw new CaptureError('journal_writer_conflict');
    }
    const temporary = `${this.filename}.${crypto.randomUUID()}.tmp`;
    try {
      const fd = fs.openSync(temporary, 'wx', 0o600);
      try { fs.writeFileSync(fd, serialized); fs.fsyncSync(fd); }
      finally { fs.closeSync(fd); }
      fs.renameSync(temporary, this.filename);
      this.lastDigest = crypto.createHash('sha256').update(serialized).digest('hex');
      if (process.platform !== 'win32') {
        const directory = fs.openSync(path.dirname(this.filename), 'r');
        try { fs.fsyncSync(directory); } finally { fs.closeSync(directory); }
      }
    } finally { if (fs.existsSync(temporary)) fs.unlinkSync(temporary); }
  };
}
