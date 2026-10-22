// Plain-language labels and descriptions shared by every page.

export type Tone = 'good' | 'warn' | 'bad' | 'info' | 'neutral';

export function skillStatus(status: string, successes = 0, needsReview = false): {label: string; tone: Tone; explain: string} {
  if (needsReview && status !== 'retired') return {label: 'Needs your OK', tone: 'info', explain: 'Learned automatically from a session whose tests passed. Agents won’t get it until you keep it.'};
  if (status === 'active') return {label: 'Ready', tone: 'good', explain: 'Verified. Your agents get it when a task matches.'};
  if (status === 'flagged') return {label: 'Paused', tone: 'bad', explain: 'A later run failed, so it is paused until it is relearned.'};
  if (status === 'retired') return {label: 'Turned off', tone: 'neutral', explain: 'Turned off. Your agents will not get it.'};
  const needed = Math.max(0, 2 - successes);
  return {
    label: 'Learning',
    tone: 'warn',
    explain: needed ? `Needs ${needed} more successful ${needed === 1 ? 'run' : 'runs'} before agents get it.` : 'Checking its latest results.',
  };
}

export function sessionStatus(status: string): {label: string; tone: Tone} {
  if (status === 'success') return {label: 'Succeeded', tone: 'good'};
  if (status === 'failure') return {label: 'Failed', tone: 'bad'};
  if (status === 'recording') return {label: 'In progress', tone: 'info'};
  if (status === 'unverified') return {label: 'Not verified', tone: 'neutral'};
  return {label: 'Capture incomplete', tone: 'neutral'};
}

const captureIssues: Record<string, string> = {
  parallel_tool_dependencies_unknown: 'Some actions ran at the same time. MuscleMemory could not tell which actions depended on others.',
  unverified_exit_status: 'Some commands had no confirmed success or failure result.',
  unfinished_tool: 'The recording ended before a result was received for an action that could change files or data.',
  missing_tool_call_id: 'An action was missing the identifier needed to match it with its result.',
  conflicting_duplicate: 'Conflicting records were received for the same action.',
  late_event_or_trace_limit: 'Some actions arrived after the recording closed, or the session exceeded the recording limit.',
  oversized_arguments: 'An action contained more information than the recorder could store.',
  configuration_changed: 'The recording settings changed while this session was running.',
  tool_after_stop: 'An action result arrived after the session had already ended.',
};

const outcomeIssues: Record<string, string> = {
  'No verified task outcome was recorded': 'The session ended without a confirmed task result.',
  'Session ended without a verified task outcome': 'The session ended without a confirmed task result.',
  'Codex turn ended without externally verified outcome': 'The session ended without a confirmed task result.',
  'No confirmed test result after a code change': 'The code changed, but no passing test result was recorded afterwards.',
  'A new prompt arrived before this turn recorded an outcome': 'A new request started before the previous task’s result was recorded.',
};

/** Explain capture diagnostics without exposing internal codes in the session view. */
export function sessionIssues(status: string, reason?: string | null): string[] {
  if (status !== 'incomplete' && status !== 'unverified') return [];
  const fallback = status === 'unverified'
    ? 'The session ended without a confirmed task result.'
    : 'Some actions could not be recorded reliably.';
  if (!reason) return [fallback];
  const gaps = reason.match(/^Capture gaps:\s*(.*)$/i);
  if (!gaps) {
    const outcome = reason.trim();
    return [Object.hasOwn(outcomeIssues, outcome) ? outcomeIssues[outcome] : fallback];
  }
  return [...new Set(gaps[1].split(',').map(code => {
    const issue = code.trim();
    if (issue.startsWith('unmapped_tool:')) return 'One of the tools used in this session is not supported by the recorder.';
    return Object.hasOwn(captureIssues, issue) ? captureIssues[issue] : fallback;
  }))];
}

function text(value: unknown): string {
  if (typeof value === 'string') return value;
  if (value && typeof value === 'object' && '$mm_parameter' in value) return `<${String((value as Record<string, unknown>).$mm_parameter)}>`;
  return '';
}

function shellCommand(command: string) {
  // Agents often wrap commands as `/bin/zsh -lc '…'`; show only what actually ran.
  const wrapped = command.match(/^\S*(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1$/);
  const inner = (wrapped ? wrapped[2] : command).trim();
  const first = inner.split('\n')[0];
  return first.length > 90 ? `${first.slice(0, 87)}…` : first;
}

const patchFiles = (patch: string) => [...patch.matchAll(/^\*\*\* (?:Update|Add|Delete) File: (.+)$/gm)].map(m => m[1].split('/').slice(-3).join('/'));

/** One sentence describing what a recorded step did, e.g. “Edit src/app.py”. */
export function describeStep(capability: string | null, args: Record<string, unknown>, effect: string, tool?: string | null): string {
  const patch = text(args.input) || text(args.patch);
  const command = text(args.command);
  if (patch.includes('*** ')) {
    const files = patchFiles(patch);
    if (files.length) return `Edit ${files.join(', ')}`;
  }
  if (command) {
    const files = patchFiles(command);
    if (files.length) return `Edit ${files.join(', ')}`;
    return `Run \`${shellCommand(command)}\``;
  }
  const path = text(args.path) || text(args.file) || text(args.file_path);
  if (path) return `${effect === 'read' ? 'Read' : 'Change'} ${path}`;
  return `Use ${tool || capability || 'a tool'}`;
}

export function agentName(name?: string | null) {
  if (!name) return 'An agent';
  const known: Record<string, string> = {codex: 'Codex', claude: 'Claude Code', 'claude-code': 'Claude Code', cursor: 'Cursor', cleanslate: 'CleanSlate'};
  return known[name.toLowerCase()] ?? name;
}

/** Short display title: the first sentence of the task the skill was learned from. */
export function shortTitle(title?: string) {
  if (!title) return 'Untitled skill';
  const first = title.split(/(?<=[.!?])\s/)[0].trim();
  return first.length > 110 ? `${first.slice(0, 107)}…` : first;
}

/** What to call a skill: its built name, else the request it was learned from. */
export function skillName(p: {name?: string; learned_from?: string; title?: string}) {
  return p.name?.trim() || shortTitle(p.learned_from?.trim() || p.title);
}

/** "Passed 2 of 2 checks", "Needs 1 more check": where a skill stands, in words. */
export function checksLine(successes = 0, failures = 0, status = '') {
  if (status === 'flagged') return `Failed ${failures} of ${successes + failures} checks`;
  if (status === 'active') return `Passed ${successes} of ${successes + failures} checks`;
  const needed = Math.max(0, 2 - successes);
  return needed ? `Needs ${needed} more passing ${needed === 1 ? 'check' : 'checks'}` : `Passed ${successes} checks`;
}

/** The test command, without shell wrappers, kept short. */
export function shortCommand(command?: string) {
  if (!command) return '';
  const inner = command.match(/^\S*(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1$/)?.[2] ?? command;
  const first = inner.split('\n')[0].trim();
  return first.length > 48 ? `${first.slice(0, 46)}…` : first;
}

/** The details after a request's first sentence, used as a skill's description. */
export function requestDetails(text?: string) {
  if (!text) return '';
  const rest = text.trim().split(/(?<=[.!?])\s+/).slice(1).join(' ').trim();
  return rest.length > 180 ? `${rest.slice(0, 178)}…` : rest;
}
