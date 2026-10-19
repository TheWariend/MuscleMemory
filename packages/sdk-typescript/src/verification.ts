/** The same "tests passed after the last change" rule the Python hooks apply. */
import type {ExecutionEvent} from './contracts.js';

/** Added to a terminal step: project files that command changed. */
export const WORKSPACE_CHANGES = '$mm_workspace_changes';

// Programs that only run tests, and tools that run tests with one of these subcommands.
// Deliberately left out: type checks and builds (tsc, mypy, npm run build), linters, and
// scripts only named like checks (contract-check.mjs). A skill is only as good as the test
// that proved it. See docs/how-it-works.md, "What counts as a test", before widening this;
// artifacts.py in the Python package must change with it.
const TEST_RUNNERS = new Set(['pytest', 'py.test', 'jest', 'vitest', 'mocha', 'ava', 'tox', 'nox', 'rspec',
  'phpunit', 'runtests.py', 'ctest']);
const TEST_SUBCOMMANDS: Record<string, string[]> = {
  cargo: ['test', 'nextest'], go: ['test'], npm: ['test', 't'], pnpm: ['test', 't'], yarn: ['test'],
  bun: ['test'], deno: ['test'], make: ['test', 'tests', 'check'], mvn: ['test', 'verify'],
  gradle: ['test'], gradlew: ['test'], dotnet: ['test'], mix: ['test'], swift: ['test'],
  rake: ['test', 'spec'], flutter: ['test'], dart: ['test'],
};
const PYTHON_TEST_MODULES = new Set(['pytest', 'unittest', 'tox', 'nox']);
// A test file run directly: `python3 test_login.py`, `node auth.test.mjs`, `node --test`.
const JS_RUNTIMES = new Set(['node', 'tsx', 'ts-node']);
const TEST_FILE = /^(?:(?:test_.+|.+_test)\.py|.+\.(?:test|spec)\.[cm]?[jt]sx?)$/;
// Prefixes that run the next program: `uv run pytest`, `npx jest`, `timeout 60 go test`.
const LAUNCHERS: Record<string, string[] | null> = {
  uv: ['run'], poetry: ['run'], pipenv: ['run'], pdm: ['run'], hatch: ['run'], bundle: ['exec'],
  pnpm: ['exec', 'dlx'], yarn: ['exec', 'dlx'], npx: null, bunx: null, env: null, time: null,
  timeout: null, nice: null, 'xvfb-run': null,
};
const FILE_CHANGE = new Set(['filesystem.edit', 'filesystem.write', 'filesystem.patch',
  'filesystem.replace_multiple', 'filesystem.create_multiple']);
const FAILED = /^FAILED \(|^FAIL[: \t]|\b\d+ failed\b|\b\d+ errors?\b|test result: FAILED|^Tests:.*\bfailed\b/m;
const PASSED = /^OK(?: \(.*\))?\s*$|\b\d+ passed\b|test result: ok\b|^ok\s|^Tests:\s+\d+ passed/m;

function words(command: string): string[] {
  return command.split(/\s+/).map(word => word.replace(/^['"]|['"]$/g, '')).filter(Boolean);
}

function basename(word: string): string {
  return word.split('/').pop() ?? word;
}

/** `bash -lc '...'` runs the quoted command. */
function unwrapShell(command: string): string {
  const match = /^\s*(?:\S*\/)?(?:ba|z)?sh\s+-l?c\s+(['"])([\s\S]*)\1\s*$/.exec(command);
  return match ? match[2] : command;
}

/** Each command in a line, split into its pipeline stages. */
function pipelines(command: string): string[][] {
  return unwrapShell(command).split(/&&|\|\||;|\n/)
    .map(part => part.split(/(?<!\|)\|(?!\|)/).filter(stage => stage.trim()));
}

/** True when one command runs a test suite, not when it only mentions tests. */
export function testRun(stage: string): boolean {
  let parts = words(stage);
  while (parts.length) {
    const name = basename(parts[0]);
    const launcher = LAUNCHERS[name];
    if (/^[A-Za-z_][A-Za-z0-9_]*=/.test(parts[0])) {
      parts = parts.slice(1);
    } else if (launcher === null) {
      parts = parts.slice(1);
      while (parts.length && parts[0].startsWith('-')) parts = parts.slice(1);
      if (name === 'timeout' && parts.length) parts = parts.slice(1);  // the duration
    } else if (launcher && parts.length > 1 && launcher.includes(parts[1])) {
      parts = parts.slice(2);
    } else {
      break;
    }
  }
  if (!parts.length) return false;
  const name = basename(parts[0]);
  let rest = parts.slice(1);
  if (TEST_RUNNERS.has(name)) return true;
  if (/^python[\d.]*$/.test(name)) {
    while (rest.length && rest[0].startsWith('-') && rest[0] !== '-m') rest = rest.slice(1);
    if (rest[0] === '-m') return rest.length > 1 && PYTHON_TEST_MODULES.has(rest[1]);
    const script = rest.length ? basename(rest[0]) : '';
    return TEST_RUNNERS.has(script) || TEST_FILE.test(script)
      || (script === 'manage.py' && rest[1] === 'test');
  }
  if (JS_RUNTIMES.has(name)) {
    while (rest.length && rest[0].startsWith('-')) {
      if (rest[0] === '--test') return true;
      rest = rest.slice(1);
    }
    return rest.length > 0 && TEST_FILE.test(basename(rest[0]));
  }
  const subcommands = TEST_SUBCOMMANDS[name];
  if (!rest.length || !subcommands) return false;
  if (subcommands.includes(rest[0])) return true;
  // `npm run test`, `pnpm run test:unit`
  return ['npm', 'pnpm', 'yarn', 'bun'].includes(name) && rest[0] === 'run' && rest.length > 1
    && (rest[1] === 'test' || rest[1].startsWith('test:'));
}

export function isTestCommand(command: unknown): boolean {
  if (typeof command !== 'string') return false;
  return pipelines(command).some(stages => stages.some(testRun));
}

/** `tests | tail` exits with tail's status, so the runner's own summary decides. */
export function pipedTest(command: unknown): boolean {
  if (typeof command !== 'string') return false;
  return pipelines(command).some(stages => stages.slice(0, -1).some(testRun));
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
