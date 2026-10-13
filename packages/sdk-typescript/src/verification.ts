/** The same "tests passed after the last change" rule the Python hooks apply. */
import type {ExecutionEvent} from './contracts.js';

/** Added to a terminal step: project files that command changed. */
export const WORKSPACE_CHANGES = '$mm_workspace_changes';

const TESTERS = new Set(['pytest', 'unittest', 'jest', 'vitest', 'cargo', 'go', 'runtests.py', 'tox', 'nox']);
const TEST_WORDS = new Set(['test', 'tests', 'check', 'unittest', 'unittest.discover']);
const FILE_CHANGE = new Set(['filesystem.edit', 'filesystem.write', 'filesystem.patch',
  'filesystem.replace_multiple', 'filesystem.create_multiple']);
const FAILED = /^FAILED \(|^FAIL[: \t]|\b\d+ failed\b|\b\d+ errors?\b|test result: FAILED|^Tests:.*\bfailed\b/m;
const PASSED = /^OK(?: \(.*\))?\s*$|\b\d+ passed\b|test result: ok\b|^ok\s|^Tests:\s+\d+ passed/m;

function words(command: string): string[] {
  return command.split(/\s+|&&|\|\||;|\|/).map(word => word.replace(/^['"]|['"]$/g, '')).filter(Boolean);
}

function basename(word: string): string {
  return word.split('/').pop() ?? word;
}

export function isTestCommand(command: unknown): boolean {
  if (typeof command !== 'string') return false;
  return words(command).some(word => TESTERS.has(basename(word)) || TEST_WORDS.has(word));
}

/** `tests | tail` exits with tail's status, so the runner's own summary decides. */
export function pipedTest(command: unknown): boolean {
  if (typeof command !== 'string' || !isTestCommand(command)) return false;
  const runner = words(command).findIndex(word => TESTERS.has(basename(word)) || TEST_WORDS.has(word));
  const after = command.slice(command.indexOf(words(command)[runner]));
  return /(?<!\|)\|(?!\|)/.test(after);
}

/** True/false from the runner's own summary line, undefined when it can't be told. */
export function testVerdict(output: unknown): boolean | undefined {
  if (typeof output !== 'string' || !output) return undefined;
  if (FAILED.test(output)) return false;
  if (PASSED.test(output)) return true;
  return undefined;
}

const DOC_NAMES = new Set(['changelog', 'changes', 'authors', 'news', 'history', 'contributors']);

/** Documentation, which tests never run: docs/, *.md, *.rst, *.txt, changelogs. */
function documentation(file: string): boolean {
  const lower = file.toLowerCase();
  const name = basename(lower);
  return /^docs?\//.test(lower) || /\.(md|rst|txt|adoc)$/.test(name) || DOC_NAMES.has(name.split('.')[0]);
}

function changesCode(event: ExecutionEvent): boolean {
  const args = (event.arguments ?? {}) as Record<string, unknown>;
  const changed = args[WORKSPACE_CHANGES];
  if (Array.isArray(changed) && changed.length) {  // even if the command then failed
    return !changed.every(file => typeof file === 'string' && documentation(file));
  }
  if (event.status !== 'success' || !FILE_CHANGE.has(event.capability ?? '')) return false;
  const file = [args.path, args.file_path, args.filePath].find(value => typeof value === 'string');
  return !(typeof file === 'string' && documentation(file));
}

/** The event id of a passing test after the last code change, or null. */
export function verifiedSuccess(history: readonly ExecutionEvent[]): string | null {
  let changed = false;
  let passing: string | null = null;
  for (const event of history) {
    if (event.kind !== 'tool') continue;
    if (changesCode(event)) { changed = true; passing = null; }
    if (isTestCommand((event.arguments as Record<string, unknown>)?.command)) {
      passing = event.status === 'success' && event.outcome_verified !== false ? event.event_id : null;
    }
  }
  return changed ? passing : null;
}
