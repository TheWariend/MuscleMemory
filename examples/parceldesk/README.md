# ParcelDesk: see MuscleMemory learn and reuse a coding skill

A small warehouse dashboard using Python's standard library and SQLite. Supplier
receipts, courier dispatches, customer returns, and damaged-stock reports change
inventory. Retrying the same event must not change inventory a second time.

## Try the warehouse

From this directory:

```sh
python3 -m parceldesk.server --port 8768
```

Open <http://127.0.0.1:8768>. Select a product and event, click **Send new event**,
then **Retry same event**. The first request is processed. The retry says
**DUPLICATE**, stock stays unchanged, and the ledger still contains one movement.
Sending a new event uses a different ID and legitimately changes stock again.

Tests require no third-party packages:

```sh
python3 -m unittest discover -s tests -v
```

This is a local simulation, bound to loopback. It does not receive real supplier
traffic, authenticate external providers, or verify webhook signatures. Do not
publish this demo server as a production webhook receiver.

## What was actually demonstrated

The scaffold intentionally left retry protection for the development tasks.
The developer then used real edit and shell tools, instrumented with the public
MuscleMemory Python SDK, against the existing local MuscleMemory server. Failed
tests, actual patches, passing tests, timing, and ordered completion were captured.
These are controlled tasks by the same developer, not fresh blind agent contexts
or a performance benchmark. This exercises the SDK/MCP lifecycle; it is not a
demonstration of automatic native Codex hook installation.

| Stage | Actual task | Result |
| --- | --- | --- |
| Learn | Add retry protection to supplier receipts | Three failing regressions became six passing tests; a provisional skill was learned with zero checks. |
| Check 1 | Add retry protection to courier dispatches | New dispatch regressions failed before the edit; all ten tests passed afterward. |
| Check 2 | Add retry protection to customer returns | New return regressions failed before the edit; all fifteen tests passed afterward. |
| Human review | User explicitly chose **Keep this ParcelDesk skill** | Normal approval service recorded Keep; no validation counters were set manually. |
| Reuse | Ask MCP for guidance on damaged-stock adjustments | Recall returned the eligible skill; its two proved lines were adapted into a new handler. All nineteen tests passed. |

Recall returned **no match** at zero checks and again at two checks before Keep.
After Keep, it returned the source edit, required files, and the original project
test command. Both learned source lines were reproduced in the later changes in
the same file. The damaged-stock application also passed normal evaluation, so the
skill ended with three successful checks and no failures. Additional HTTP tests
exercise the dashboard and API, separate from those four development recordings.

The useful lesson is small and concrete: inside the same transaction as a stock
change, call the existing receipt-claim helper before applying the change. Exact
duplicate deliveries return without another mutation. The helper rejects reused
IDs with changed data. Rollback leaves the event retryable if the mutation fails.

```python
with connection:
    if not claim_event(connection, event):
        return {"status": "duplicate", "event_id": event["id"]}
    # Apply the event's particular stock change here.
```

The memory captures the two-line guard; the agent must inspect the helper and
transaction context before adapting it. Those lines alone do not implement the
database uniqueness, rollback, payload validation, or external authentication.

The approach uses SQLite's documented [uniqueness conflict handling](https://www.sqlite.org/lang_upsert.html)
and [transaction rules](https://www.sqlite.org/lang_transaction.html). The tests
check observable behavior, including concurrent deliveries, conflicting payloads,
failed mutations, and retry after rollback.

## Inspect the evidence on this machine

The local `.demo/` directory is ignored by Git. It contains:

- `received-run.json`, `dispatched-run.json`, `returned-run.json`: actual SDK
  tool arguments, outputs, durations, and run tokens.
- `recall-result.json`: the real MCP recall result used for the fourth feature.
- `damaged-run.json`: the actual adapted patch, test output, and evaluation.
- `skill-inspection.json`: the latest authenticated skill inspection snapshot.
- `progress.json`: the snapshot displayed in the warehouse's evidence card.

The live source recordings and skill also appear in MuscleMemory's **Activity**
and **Skills** screens. The example uses an explicit `project: parceldesk-demo`
requirement so this narrow warehouse lesson is not offered to unrelated projects.
It does not change the installation's global sharing, naming, curation, approval,
or validation settings.

The capture utilities use the existing private Codex connection file; they never
copy a key into this project. Running them on an already completed task refuses
to create another run or count a meaningless check. They are demonstration
utilities, not a way to manufacture checks by replaying the source task.

## Use the product on your actual work

Connect a project with `musclememory connect codex` and approve its hooks in Codex.
Then give the agent a real task and run the project's actual tests. Review useful
candidates in **Skills**, let later matching work supply independent evidence,
and Keep a lesson when it deserves approval. Once eligible, related tasks can
receive the short pointer through hooks or explicit `musclememory_recall`.

The agent still decides how to adapt it and must test the new change. A delivered
pointer alone does not prove that it was applied, and a successful source task
does not count as its own validation.
