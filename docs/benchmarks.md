# Benchmarks

## Check our numbers yourself

```sh
python benchmarks/reproduce.py check     # offline, seconds: every published file is
                                         # untouched and every total recomputes
python benchmarks/reproduce.py rejudge   # offline: rerun SWE-bench's hidden tests on every
                                         # published Django patch
python benchmarks/reproduce.py run       # rerun the Django benchmark with your own Codex
```

`check` needs only Python. `rejudge` and `run` set up a Django clone and a Python 3.11
environment under `~/.musclememory-bench` the first time. `run` asks before it starts,
because it spends your Codex quota (about 10 sessions).

Last checked: all published files match their fingerprints, every total recomputes
exactly, and all ten Django verdicts reproduce when the hidden tests are rerun.

## Codex with and without MuscleMemory (luna-03)

18 real Codex sessions: three coding tasks, each run three times without memory and three
times with MuscleMemory. Every session passed an independent check of 187 behaviours plus the
project's own tests.

| | Memory off | MuscleMemory | Change |
|---|---:|---:|---:|
| Input tokens | 981,734 | 731,549 | **−25.5%** |
| Uncached input tokens | 106,470 | 76,189 | **−28.4%** |
| Tool calls | 56 | 39 | **−30.4%** |
| Wall time | 278 s | 200 s | **−28.2%** |
| Tasks passed | 9 / 9 | 9 / 9 | same |

Full method, per-task numbers, the one rule change made during the run, and limits:
[benchmarks/results/codex/luna-03/report.md](../benchmarks/results/codex/luna-03/report.md).

### What this does and doesn't show

- It shows a coding agent repeating a kind of task it has done before spends less and finishes
  sooner when MuscleMemory hands over how it was done last time.
- It doesn't show gains on brand-new kinds of work, on production codebases, or in success
  rate (every memory-off session also passed).
- Token use varies between runs, so each task ran three times and the spread is published.

## Real Django issues from SWE-bench (django-forms-01, small)

Codex with `gpt-6-luna` on five real Django issues from SWE-bench Verified that all touch
`django/forms/models.py`, judged by SWE-bench's own hidden tests. MuscleMemory learned
from the four oldest (one skill passed two checks on different issues and was kept),
then the newest issue was run three times with memory and three times without.

| Issue 16950, 3 repeats | Memory off | MuscleMemory | Change |
|---|---:|---:|---:|
| Resolved (hidden tests) | 1 / 3 | 1 / 3 | same |
| Input tokens | 1,645,818 | 815,216 | −50.5% |
| Tool calls | 60 | 28 | −53.3% |
| Wall time | 247 s | 217 s | −12.1% |

The same success rate, at about half the tokens on average. One memory session gave up
early, which accounts for part of that; comparing only the resolved sessions, memory used
27% fewer input tokens. One scored issue is a small sample. Running it also found and fixed
real gaps: Codex hooks send no exit codes, Django's test runner wasn't recognised, and
documentation edits after a passing test cancelled it. The report covers all of it:
[benchmarks/results/codex/django-forms-01/report.md](../benchmarks/results/codex/django-forms-01/report.md).

## Claude Code with MuscleMemory (sonnet-01, early)

Eight real Claude Code sessions with Sonnet 5.5: four pairs on the same three repair tasks,
memory off against MuscleMemory, identical files and prompt per pair. The skills Claude Code
received were learned and checked by **Codex** earlier, so this also shows memory moving
between agents.

| | Memory off | MuscleMemory | Change |
|---|---:|---:|---:|
| Input tokens | 459,366 | 259,436 | **−43.5%** |
| Tool calls | 30 | 21 | **−30.0%** |
| Output tokens | 6,708 | 4,973 | −25.9% |
| Wall time | 64.9 s | 66.8 s | +2.9% |
| Cost | $0.261 | $0.213 | −18.4% |
| Tasks passed | 4 / 4 | 4 / 4 | same |

Every pair used fewer input tokens (−31% to −59%). Time did not improve on these short
sessions. It is a small run, one session per arm, so treat it as an early signal. Running it
also found five gaps in the Claude Code integration, fixed before scoring; the report lists
them with the one amendment:
[benchmarks/results/claude/sonnet-01/report.md](../benchmarks/results/claude/sonnet-01/report.md).

## Speed as memory grows

Recall time on a laptop (Apple Silicon, SQLite), measured with `benchmarks/scale.py`:

| Stored skills | Before the in-memory index | After |
|---:|---:|---:|
| 10,000 | about 2,100 ms | about 60 ms |

With 1,000 synthetic skills, a reworded task got the right skill first 97% of the time.
PostgreSQL uses an HNSW vector index for every embedding size. A full 100k run is still to do.

Run it yourself:

```sh
python benchmarks/scale.py --sizes 1000,10000 --backend sqlite
python benchmarks/scale.py --backend postgres --postgres-url URL --reset-postgres
```

`--reset-postgres` wipes the given database. Use a throwaway one.
