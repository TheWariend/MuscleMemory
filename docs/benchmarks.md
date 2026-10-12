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
