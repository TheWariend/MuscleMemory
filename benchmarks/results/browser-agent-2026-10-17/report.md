Browser agent evaluation (gpt-6-luna, high, codex-cli 0.160.0)

Status: complete

| Arm | Pass | Wrong submissions | Median browser calls | Median tokens (total / uncached) | Median seconds | Found / called / finished |
|---|---|---|---|---|---|---|
| baseline | 1/1 | 0 | 36 | 559,761 / 42,641 | 55.2 | 0 / 0 / 0 |
| skill | 1/1 | 0 | 14 | 498,059 / 47,883 | 37.7 | 1 / 1 / 0 |

Failures and interrupted attempts remain in the measured denominator. Unavailable usage stays unavailable. Total = input + output; uncached = input − cached input + output. Calls count public MCP invocations; skill internals are not extra calls. Finished means run_skill returned ready for the agent's final submit. Judge compares saved payloads independently.

Limits: synthetic local site, one model, small sample. Practice has 3 resource rows; scoring has 24. No claims beyond these measurements.

Observed skill behavior: the first call started at the portal home, navigated
through Requests and Equipment services to Equipment loan, and populated 19 of
22 visible form fields. It stopped before learned browser step 26, `click Continue
to review`, with `fill_these_then_run_again`: Application reference and Cost center
had been renamed. The agent also deliberately passed an empty resource input on
this first call; Reserved resource was still blank in the immediately following
page read, even though the reply included that field in `done`.

After reading the table, the agent filled the renamed fields and selected Resource
07 itself before call 2. It also passed Resource 07 in call 2 inputs. That call
accepted the existing selection, reached review and filled Confirm applicant email. It stopped before learned browser step 29, `click Copy applicant address
to billing`, with `fill_these_then_run_again`: Authorization code needed the agent's
input. The immediately following page read showed the email filled, authorization
blank, billing copy checked, and paid extras unchecked. The agent unchecked billing copy,
entered the code, checked the page, submitted and called browser_done. There was no third skill
call and neither reply returned `ready`; finished therefore remains 0.

Both high-reasoning arms submitted Resource 07, the cheapest eligible option:
323 + 409 + 58 × 5 = 1,022 cents. Each high arm made exactly one correct submission and no wrong
submissions.

See [the side-by-side comparison and every full skill reply](comparison.md) and
[structured comparison data](comparison.json). The comparison includes the full
page read immediately after each reply. Test and Ruff receipts are in [validation.json](validation.json).

The full 36-run campaign was not started.
