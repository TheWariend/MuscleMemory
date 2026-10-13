# sonnet-01: Claude Code with and without MuscleMemory

Eight real Claude Code sessions with Sonnet 5.5: four tasks, each run once without memory
and once with MuscleMemory. All eight passed the independent checks. With MuscleMemory,
Claude Code used **43.5% fewer input tokens and 30% fewer tool calls**. The skills it
received were learned earlier by **Codex**: memory learned by one agent, used by another.

This is an early, small run. Read the limits below before quoting it.

## Setup

- Claude Code 2.1.296, headless (`claude -p`), model `claude-sonnet-5-5`, with user and
  plugin settings excluded (`--setting-sources project,local --strict-mcp-config`).
- The three repair tasks from the Codex benchmark (exports, configuration, pagination), each
  with three planted bugs. The fixture's `AGENTS.md` is also given to Claude as `CLAUDE.md`,
  identical in both arms.
- Every pair (memory off, MuscleMemory) started from identical files and the same prompt.
  The arm order alternated.
- Memory: three skills learned from verified **Codex** sessions, each checked twice by Codex
  and kept. No skill was learned from Claude Code before scoring.
- The memory-off arm had no hooks. The MuscleMemory arm had hooks installed by
  `musclememory hooks install --client claude`.
- An independent checker ran 187 behaviour assertions and the project's own tests after
  every session, and confirmed no protected file changed.

## Results

| Total over 4 pairs | Memory off | MuscleMemory | Change |
|---|---:|---:|---:|
| Tasks passed | 4 / 4 | 4 / 4 | same |
| Input tokens | 459,366 | 259,436 | **−43.5%** |
| Uncached input tokens | 37,980 | 35,222 | −7.3% |
| Output tokens | 6,708 | 4,973 | −25.9% |
| Tool calls | 30 | 21 | **−30.0%** |
| Wall time | 64.9 s | 66.8 s | +2.9% |
| Cost (reported by Claude Code) | $0.261 | $0.213 | −18.4% |
| Codex skill handed over | — | 4 / 4 | — |

| Pair | Task | Input tokens | Tool calls | Wall time |
|---|---|---:|---:|---:|
| p1 | Configuration | 117,367 → 80,620 (−31.3%) | 8 → 5 | 17.0 → 18.6 s |
| p2 | Configuration | 145,101 → 59,678 (−58.9%) | 9 → 5 | 17.5 → 16.4 s |
| p3 | Pagination | 99,318 → 59,741 (−39.8%) | 7 → 6 | 15.5 → 16.1 s |
| p4 | Exports | 97,580 → 59,397 (−39.1%) | 6 → 5 | 14.9 → 15.7 s |

MuscleMemory used fewer input tokens and fewer tool calls in all four pairs. Wall time did
not improve: these sessions are short (15 to 19 seconds) and most of the saving is cached
context that Claude Code no longer re-reads.

## What the run found and fixed

The first Sonnet 5.5 sessions passed every check but taught MuscleMemory nothing. Running
real sessions found five gaps, all fixed with tests before the scored pairs:

1. **Shell edits.** Sonnet 5.5 changed code from Bash heredocs instead of the Edit tool, so
   no code change was seen. The hooks now compare which project files differ from git
   before and after each shell command, recording paths only.
2. **Piped test runs.** `tests | tail` exits with `tail`'s status. Such a run now counts only
   when the output shows the test runner's own pass or fail line.
3. **Sessions split by `cd`.** Claude Code reports the directory a command moved to; the
   session is now identified by its session id alone.
4. **Skills across agents.** Codex's patch tool and Claude Code's edit and write tools all
   change a file. They are now interchangeable when matching a skill to an agent and when
   counting a check. Every other capability must still match exactly.
5. **Checks without the look-around.** A check no longer requires repeating the original
   agent's read-only shell commands (`cat`, `sed -n`, `grep`, `git diff`).

### Amendment

Pairs p1 to p3 ran before fixes 4 and 5 were complete. Their measurements do not depend on
those fixes, but their passing MuscleMemory sessions were first recorded as three new
skills instead of checks. Those duplicates were deleted, and the same three sessions were
re-submitted to auto-learn after the fix, where they counted as checks on the Codex skills.
Pair p4 ran with every fix in place and was recorded as a check automatically.

Excluded from scoring: four exploratory sessions on an isolated server before the shell-edit
fix, one learning session used to find the remaining server issue, and one live session
recorded as incomplete before the session-id fix.

## Limits

- Four pairs, one session per arm. Token use varies between runs, so this needs repeats
  before it is a benchmark.
- The tasks are the ones the Codex skills were learned on. Pair p3 used the same wording the
  skill was learned from; the other three used different wordings.
- Every memory-off session also passed, so this measures efficiency, not success rate.

## Reproduce

```sh
python benchmarks/evaluate_claude.py --dir RUN_DIR setup
python benchmarks/evaluate_claude.py --dir RUN_DIR session p1-on --family configuration \
  --variant 31 --prompt 1 --arm musclememory
python benchmarks/evaluate_claude.py --dir RUN_DIR session p1-off --family configuration \
  --variant 31 --prompt 1 --arm off
```

`setup` starts an isolated MuscleMemory. To record into an existing server instead, pass
`--hooks-config` and `--database`. The isolated home starts empty, so learn and check skills
first, or copy the three Codex skills in.

## Files

- `results.json`: protocol, totals and every session's numbers
- `cases/`: each session's Claude Code event stream, with local paths replaced by `$RUN`,
  `$HOME` and `$TMP`, and the local Claude setup removed from the start-up event
- `SHA256.json`: fingerprints of every exported file
