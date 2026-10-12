# luna-03: Codex with and without MuscleMemory

18 real Codex sessions. Each of three coding tasks ran three times without memory and
three times with MuscleMemory. All 18 passed the independent checks. With MuscleMemory,
Codex used **25.5% fewer input tokens, 30.4% fewer tool calls and 28.2% less time**.

## Setup

- Codex CLI 0.160.0, model `gpt-6-luna`, medium reasoning, a fresh isolated Codex home for every session.
- Three repair tasks (exports, configuration, pagination), each with three planted bugs.
- Every pair (memory off, MuscleMemory) started from identical files and the same prompt. The arm order rotated.
- Memory came from earlier GPT-5.5 sessions: one session taught each skill and two separate sessions checked it. The skills were then frozen, so nothing was learned during scoring.
- MuscleMemory used its own design: exact file and command matching, BM25 keywords, the local `all-MiniLM-L6-v2` model, and a pointer added once at the prompt.
- An independent checker ran 187 behaviour assertions and the project's own tests after every session, and confirmed no protected file changed.

## Results

| Total over 9 paired tasks | Memory off | MuscleMemory | Change |
|---|---:|---:|---:|
| Tasks passed | 9 / 9 | 9 / 9 | same |
| Input tokens | 981,734 | 731,549 | **−25.5%** |
| Uncached input tokens | 106,470 | 76,189 | **−28.4%** |
| Output tokens | 7,248 | 5,651 | −22.0% |
| Tool calls | 56 | 39 | **−30.4%** |
| Failed tool calls | 6 | 0 | — |
| Wall time | 278.1 s | 199.7 s | **−28.2%** |
| Right skill handed over | — | 9 / 9 | — |

Input tokens by repeat:

| | Repeat 1 | Repeat 2 | Repeat 3 |
|---|---:|---:|---:|
| All three tasks | −18.5% | −37.4% | −18.4% |
| Exports | +2.2% | −44.9% | −12.9% |
| Configuration | −26.5% | −28.4% | −12.5% |
| Pagination | −24.2% | −37.0% | −29.5% |

MuscleMemory used fewer input tokens in 8 of the 9 pairs.

## Change made during the run

The run stopped itself after 8 sessions. In a **memory-off** session, Codex applied a patch
through a shell heredoc and sent the "tool started" hook but never the "tool finished" hook.
MuscleMemory marked that capture incomplete, as designed, and the harness's strict rule halted
everything.

The rule was then narrowed: an incomplete capture in the memory-off arm is recorded instead of
stopping the run. MuscleMemory sessions still stop the run if their capture is incomplete or the
right skill isn't delivered. Nothing in MuscleMemory or the measurements changed. The amendment,
with the harness file's fingerprint before and after, is in `results.json` under
`protocol.protocol_amendments`.

## Limits

- Three controlled tasks, and memory came from earlier sessions of the same kinds of task. This
  shows the repeat-a-workflow case, not new kinds of work.
- Every memory-off session already passed, so this measures efficiency, not success rate.
- Token use without memory varies a lot from run to run (exports used 74k to 136k), which is why
  each task ran three times.
- Codex was signed in through a ChatGPT plan, so there are token counts but no billed cost.

## Reproduce

```sh
python benchmarks/evaluate_codex.py preflight --campaign my-run --model gpt-6-luna \
  --repeats 3 --seed-campaign <campaign with verified training sessions>
python benchmarks/evaluate_codex.py run --campaign my-run --model gpt-6-luna --repeats 3
```

The preflight makes no model calls; it replays saved sessions and stops before any paid session
if MuscleMemory would hand over the wrong skill.

## Files

- `results.json`: protocol, totals and every session's numbers
- `cases.jsonl`: prompts, model events, hook receipts, patches and checks for each session
- `banks.json`, `frozen-banks.json`: the skills used and proof they didn't change during scoring
- `preflight.json`: the offline replay, with zero model calls
- `seed-evidence.json`: where the training sessions came from
- `SHA256.json`: fingerprints of every exported file
