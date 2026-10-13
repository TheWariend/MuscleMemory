# django-forms-01: Codex on real Django issues, with and without MuscleMemory

A small run on real GitHub issues from **SWE-bench Verified**, judged by SWE-bench's own
hidden tests. Codex CLI with `gpt-6-luna` worked through five real Django issues that all
touch Django's model forms: four to learn from, one scored three times with memory and
three times without.

**Result, stated plainly:** with MuscleMemory, Codex resolved the scored issue as often as
without it (1 of 3 each) and used **50.5% fewer input tokens and 53% fewer tool calls**.
Part of that saving comes from one memory session that gave up early; comparing only the
sessions that resolved the issue, memory used **27% fewer input tokens** (263,686 vs
359,567) and 10 tool calls instead of 16. Three repeats on one issue is a small sample.

## Why this run

The earlier benchmarks used three small practice tasks we wrote. This run uses real issues
from a well-known project, the official hidden tests, and issues the memory never saw
before: the scored issue (#16950) is a different bug from every issue MuscleMemory learned
from. What repeats is the *area* of the codebase, which is how teams actually work.

## Setup

- **Tasks:** the five SWE-bench Verified Django issues whose official fix changes
  `django/forms/models.py`, in the order they were reported:

  | Issue | Reported | Django | Title | Role |
  |---|---|---|---|---|
  | 13933 | 2021-01 | 4.0 | ModelChoiceField does not provide value of invalid choice | learn |
  | 14725 | 2021-08 | 4.1 | Provide a way for model formsets to disallow new object creation | learn |
  | 14915 | 2021-09 | 4.1 | ModelChoiceIteratorValue is not hashable | learn |
  | 15916 | 2022-08 | 4.2 | Allow ModelForm meta to specify formfield_callback | learn |
  | 16950 | 2023-06 | 5.0 | Django Admin with Inlines not using UUIDField default value | **scored** |

- **Agent:** Codex CLI 0.160.0, `gpt-6-luna`, medium reasoning, a fresh `CODEX_HOME` for
  every session (no memories or skills of its own), workspace-write sandbox.
- **Environment:** one local Python 3.11 with Django's requirements, instead of SWE-bench's
  per-version Docker images. Every candidate issue was first run with its official fix:
  9 of 10 candidates passed all their hidden tests here and failed without the fix;
  the 10th (#16819, a migration issue) didn't and was excluded. All five issues above
  passed.
- **Prompt:** the issue text exactly as filed, followed by one line, identical for both
  sides: "Fix this issue in the Django repository. Run the relevant tests with
  `python tests/runtests.py <module>` to check your change before you finish."
- **MuscleMemory:** set up the way a user does it, `musclememory connect codex` in each
  checkout, on an isolated server. The memory-off side has no hooks at all.
- **Judge:** SWE-bench's method. The hidden test patch is applied to clean test files and
  every FAIL_TO_PASS test must pass and every PASS_TO_PASS test must still pass.

## Learning (4 real sessions)

| Session | Issue | Resolved (hidden tests) | MuscleMemory |
|---|---|---|---|
| 1 | 13933 | yes | learned a skill |
| 2 | 14725 | yes | counted as a passing check on skill 1 |
| 3 | 14915 | yes | counted as a passing check on skill 1 |
| 4 | 15916 | **no** | learned a second skill |

Skill 1 ("ModelChoiceField does not provide value of invalid choice…") passed two separate
checks and was kept, so it became **Ready**. The second skill came from a session that the
hidden tests say did *not* resolve its issue, even though Codex's own test run passed.
That is the known weak spot of "tests passed", and the reason a skill needs two separate
checks before any agent gets it: the second skill had none, so it was never handed out.
Learning was switched off before scoring, so the scored sessions couldn't teach anything.

## Scored results (issue 16950, 3 repeats, alternating order)

| | Memory off | MuscleMemory | Change |
|---|---:|---:|---:|
| Resolved | 1 / 3 | 1 / 3 | same |
| Input tokens | 1,645,818 | 815,216 | **−50.5%** |
| Uncached input tokens | 152,314 | 72,816 | −52.2% |
| Output tokens | 11,566 | 5,914 | −48.9% |
| Tool calls | 60 | 28 | **−53.3%** |
| Wall time | 247.0 s | 217.0 s | −12.1% |
| Skill handed over | — | 3 / 3 | — |

| Repeat | Input tokens (off → memory) | Tool calls | Resolved (off / memory) |
|---|---:|---:|---|
| 1 | 600,055 → 263,686 (−56%) | 24 → 10 | no / **yes** |
| 2 | 359,567 → 407,048 (+13%) | 16 → 13 | **yes** / no |
| 3 | 686,196 → 144,482 (−79%) | 20 → 5 | no / no |

What happened in the unresolved sessions:

- Without memory, repeat 1 tried a fix, saw it fail and reverted it (600k tokens); repeat
  3 edited only tests (686k tokens).
- With memory, repeat 2 fixed the reported bug but broke existing tests; repeat 3 read the
  code for five calls and stopped without a change (144k tokens). That early stop is a
  large part of the 50.5% figure.

## What this shows and what it doesn't

- On real issues from a real project, a skill learned from *other* issues in the same area
  was handed over every time and did not lower the success rate.
- Sessions with memory were cheaper on average, and the one resolved memory session used
  27% fewer tokens than the resolved session without memory.
- It does **not** show a success-rate gain: 1 of 3 on both sides.
- One scored issue with three repeats is not enough to rule out chance. The effect needs
  more issues and more repeats before it's a headline number.
- The skill came from a different bug in the same file; whether its note is what helped,
  rather than general orientation, isn't separated here.

## Setup runs before this one (not scored)

Four earlier attempts stopped after one to three sessions each because they found real
problems. Each was fixed with tests before this run:

1. **Codex sends terminal output without exit codes** in its hooks, so no Codex session
   could ever be verified. Test runs are now judged by the runner's own summary line
   ("OK", "FAILED (failures=1)") when the exit code is missing; other unconfirmed steps
   are left out of the learned skill.
2. **Django's test runner** (`tests/runtests.py`) wasn't recognised as a test command.
3. **Documentation edits** after the tests passed cancelled the verification; edits that
   touch only docs no longer do.
4. The first prompts told Codex not to edit tests and didn't ask it to run tests, and put
   an instruction before the issue text (which became every skill's title). The final
   prompt is the issue text plus one neutral line.

Those sessions are excluded from every number above.

## Reproduce

The quickest checks need no model calls:

```sh
python benchmarks/reproduce.py check     # files untouched, totals recomputed
python benchmarks/reproduce.py rejudge   # hidden tests rerun on every published patch
python benchmarks/reproduce.py run       # the whole run again, with your own Codex
```

Or step by step:

```sh
python benchmarks/swebench_django.py validate --group model-forms --out RUN/validation-1
python benchmarks/evaluate_django.py --dir RUN setup --group model-forms --train 4
python benchmarks/evaluate_django.py --dir RUN train
python benchmarks/evaluate_django.py --dir RUN approve
python benchmarks/evaluate_django.py --dir RUN score --repeats 3
python benchmarks/evaluate_django.py --dir RUN report
```

Needs a bare clone of Django and a Python 3.11 environment with `asgiref`, `sqlparse`,
`tzdata` and `pytz` (see `benchmarks/swebench_django.py`).

## Files

- `summary.json`: campaign, training results, skills, totals and every pair
- `sessions/<label>/`: the prompt, Codex's event stream, the final patch and the record,
  with local paths replaced by `$RUN`, `$BENCH` and `$HOME`
- `SHA256.json`: fingerprints of every exported file
