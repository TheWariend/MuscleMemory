# Benchmarks

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
