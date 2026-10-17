"""Portal judge, real-browser fixture behavior, and evaluator accounting contracts."""

import asyncio
import json
from collections import Counter
from pathlib import Path

import httpx
import pytest
from musclememory.browser_mcp import PlaywrightDriver


@pytest.fixture(autouse=True)
def benchmark_imports(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))


def test_plan_scope_pairing_and_fresh_seeds():
    from benchmarks.evaluate_browser_agent import plan, prompt_for

    smoke = plan()
    assert Counter(r["phase"] for r in smoke) == {"learn": 3, "auto_approve": 1, "score": 2}
    full = plan(True)
    scored = [r for r in full if r["phase"] == "score"]
    assert len(scored) == 36
    learned = {r["seed"] for r in full if r["phase"] != "score"}
    assert learned.isdisjoint(r["seed"] for r in scored)
    for i in range(0, len(scored), 2):
        a, b = scored[i : i + 2]
        assert {a["arm"], b["arm"]} == {"baseline", "skill"}
        assert (a["form"], a["variant"], a["seed"]) == (b["form"], b["variant"], b["seed"])
    assert {r["arm"] for r in scored[::2]} == {"baseline", "skill"}
    base, skill = smoke[-2:]
    a = prompt_for(base, "http://portal.test/?case=x")
    b = prompt_for(skill, "http://portal.test/?case=x")
    assert b == a + "\nCheck for a proven skill before doing it by hand."
    assert "Finish with browser_done" in a
    assert "/forms/" not in a


def test_variants_change_only_intended_structure():
    from benchmarks.browser_tasks import LABELS, TASKS, render

    plain = render(TASKS[0], 10001, "unchanged", "case")
    renamed = render(TASKS[0], 10001, "renamed", "case")
    assert renamed == plain.replace("Application reference", "Service application ID", 1).replace(
        "Cost center", "Billing cost center", 1
    )
    moved = render(TASKS[0], 10001, "moved", "case")
    assert moved.index('aria-label="Archived applications"') < moved.index('id="application"')
    assert moved.index('aria-label="Billing contact"') < moved.index(LABELS["first"])
    assert "prefill" not in plain.lower()


def test_portal_navigation_payload_judge_and_durable_submissions(tmp_path):
    from benchmarks.browser_site import Portal
    from benchmarks.browser_tasks import TASKS, allocation, bindings, judge

    path = tmp_path / "submissions.jsonl"
    with Portal(records=path) as portal:
        home = portal.add("one", TASKS[0], 10001, "renamed")
        assert "/forms/" not in home
        with httpx.Client(base_url=portal.url) as client:
            assert "Requests" in client.get("/?case=one").text
            assert "Equipment services" in client.get("/requests?case=one").text
            assert "Equipment loan" in client.get("/requests/equipment?case=one").text
            assert "Billing cost center" in client.get("/forms/equipment?case=one").text
            assert not client.get("/judge?case=one").json()["passed"]
            expected = {**bindings(TASKS[0], 10001), "resource": allocation(TASKS[0], 10001)}
            client.post("/submit?case=one", json=expected).raise_for_status()
            assert client.get("/judge?case=one").json()["passed"]
            client.post("/submit?case=one", json={"wrong": "archive"})
            verdict = client.get("/judge?case=one").json()
            assert not verdict["passed"] and verdict["wrong_submissions"] == 1
    with Portal(records=path) as restored:
        assert restored.verdict("one") == verdict
    assert not judge(TASKS[0], 10001, "unchanged", [expected, expected])["passed"]


def test_accounting_uses_cli_usage_and_mcp_receipts():
    from benchmarks.evaluate_browser_agent import measurements, plan, summarize

    def receipt(tool, value):
        return {
            "type": "item.completed",
            "item": {
                "type": "mcp_tool_call",
                "server": "browser",
                "tool": tool,
                "result": {"content": [{"type": "text", "text": json.dumps(value)}]},
            },
        }

    messages = [
        receipt("musclememory_find_skills", {"skills": [{"skill": "x"}]}),
        receipt("musclememory_run_skill", {"stopped": "fill_these_then_run_again"}),
        receipt("musclememory_run_skill", {"ready": "Submit yourself"}),
        receipt("browser_done", {"checked": "x"}),
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 1000, "cached_input_tokens": 400, "output_tokens": 100},
        },
    ]
    calls = [{"tool": m["item"]["tool"]} for m in messages[:-1]]
    result = measurements(messages, calls)
    assert result["usage"]["total_tokens"] == 1100
    assert result["usage"]["uncached_tokens"] == 700
    assert result["turns"] == 1
    assert result["skill_found"] and result["run_skill_called"] and result["skill_finished"]
    assert result["tool_audit_pass"] and result["browser_tool_calls"] == 4
    assert len(result["run_skill_returned"]) == 2
    missing = measurements([], [])
    assert missing["usage"]["total_tokens"] is None
    sample = [
        {
            **plan()[-1],
            **result,
            "judge": {"passed": False, "wrong_submissions": 1},
            "wall_seconds": 10,
        },
        {
            **plan()[-1],
            **missing,
            "judge": {"passed": True, "wrong_submissions": 0},
            "wall_seconds": 20,
        },
    ]
    summary = summarize(sample, plan())["skill"]
    assert summary["runs"] == 2 and summary["pass_rate"] == 0.5
    assert summary["median_total_tokens"] is None
    assert summary["median_wall_seconds"] == 15


def test_real_browser_review_trap_and_all_variants(tmp_path):
    """Exercise the restored site's JS, not just an ideal payload sent to its judge."""

    pytest.importorskip("playwright.async_api")

    from benchmarks.browser_site import Portal
    from benchmarks.browser_tasks import TASKS, allocation, bindings

    async def scenario():
        driver = PlaywrightDriver()
        try:
            with Portal(records=tmp_path / "records.jsonl") as portal:
                for task in TASKS:
                    for variant in ("practice", "unchanged", "renamed", "moved"):
                        case = task.key + "-" + variant
                        seed = 12345
                        portal.add(case, task, seed, variant)
                        await driver.open(portal.url + f"/forms/{task.key}?case={case}")
                        page = driver.page
                        values = bindings(task, seed)
                        for key, value in values.items():
                            if key in {"confirm", "authorization"}:
                                continue
                            locator = page.locator(f'[name="{key}"]')
                            if key in {"region", "zone", "service", "slot"}:
                                await locator.select_option(label=value)
                            else:
                                await locator.fill(value)
                        await page.locator('[name="resource"]').select_option(
                            label=allocation(task, seed, variant == "practice")
                        )
                        await page.get_by_role("button", name="Continue to review").click()
                        await page.locator('[name="confirm"]').fill(values["confirm"])
                        await page.locator('[name="authorization"]').fill(values["authorization"])
                        # Every form starts with the billing-copy trap checked.
                        assert await page.locator('[name="copy_billing"]').is_checked()
                        await page.locator('[name="copy_billing"]').uncheck()
                        await page.locator('#application button[type="submit"]').click()
                        await (
                            page.get_by_role("status")
                            .filter(has_text="Application received.")
                            .wait_for()
                        )
                        assert portal.verdict(case)["passed"], (task.key, variant)
        finally:
            if hasattr(driver, "_playwright"):
                await driver._playwright.stop()

    asyncio.run(scenario())


def test_resume_locks_settings_and_never_replays_a_saved_or_partial_run(tmp_path, monkeypatch):
    from benchmarks.evaluate_browser_agent import Campaign, plan, write_json

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr("benchmarks.evaluate_browser_agent.free_port", lambda: 12345)

    def output(argv, **kwargs):
        return "codex-cli test-version\n" if "--version" in argv else "source-commit\n"

    monkeypatch.setattr("benchmarks.evaluate_browser_agent.subprocess.check_output", output)
    directory = tmp_path / "campaign"
    campaign = Campaign(directory, "gpt-6-luna", "medium", False, False, 30)
    run = plan()[-1]
    saved = directory / "runs" / run["id"]
    saved.mkdir(parents=True)
    write_json(saved / "result.json", {"saved": True})
    assert campaign.execute(run, None, None, None) == {"saved": True}
    (saved / "result.json").unlink()
    with pytest.raises(RuntimeError, match="Unfinalized run"):
        campaign.execute(run, None, None, None)
    campaign.service.db.engine.dispose()
    with pytest.raises(ValueError, match="model"):
        Campaign(directory, "different-model", "medium", False, True, 30)
    with pytest.raises(ValueError, match="reasoning"):
        Campaign(directory, "gpt-6-luna", "high", False, True, 30)
    resumed = Campaign(directory, "gpt-6-luna", "medium", False, True, 30)
    assert resumed.keys == campaign.keys
    resumed.service.db.engine.dispose()


@pytest.mark.parametrize("immediate_exit", [False, True])
def test_account_error_is_preserved_and_stops_execution(tmp_path, monkeypatch, immediate_exit):
    from benchmarks.evaluate_browser_agent import AccountStop, Campaign, plan

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr("benchmarks.evaluate_browser_agent.free_port", lambda: 12345)
    monkeypatch.setattr(
        "benchmarks.evaluate_browser_agent.subprocess.check_output",
        lambda argv, **kwargs: "codex-cli test\n",
    )
    campaign = Campaign(tmp_path / "campaign", "gpt-6-luna", "medium", False, False, 30)

    class PortalStub:
        def add(self, *args):
            return "http://127.0.0.1:12345/?case=test"

        def verdict(self, case):
            return {
                "passed": False,
                "correct_submissions": 0,
                "wrong_submissions": 0,
                "submissions": 0,
            }

    class FailedCodex:
        returncode = 1

        def __init__(self, argv, *, stdout, **kwargs):
            stdout.write('{"type":"error","message":"insufficient_quota"}\n')
            stdout.flush()

        def poll(self):
            return 1 if immediate_exit else None

    monkeypatch.setattr("benchmarks.evaluate_browser_agent.subprocess.Popen", FailedCodex)
    monkeypatch.setattr("benchmarks.evaluate_browser_agent.time.sleep", lambda _: None)
    monkeypatch.setattr("benchmarks.evaluate_browser_agent.terminate", lambda _: None)
    state = campaign.runtime / "state"
    state.mkdir()
    try:
        with pytest.raises(AccountStop):
            campaign.execute(plan()[0], PortalStub(), "http://127.0.0.1:12345", state)
        result = json.loads(
            (campaign.directory / "runs" / plan()[0]["id"] / "result.json").read_text()
        )
        assert result["stop_reason"] == "account_or_credit_error"
        assert not result["judge"]["passed"]
        assert result["usage"]["total_tokens"] is None
        assert result["browser_tool_calls"] == 0
    finally:
        campaign.service.db.engine.dispose()


def test_resume_finalizes_a_crash_before_case_registration(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from benchmarks.evaluate_browser_agent import Campaign, plan

    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-home"))
    monkeypatch.setattr("benchmarks.evaluate_browser_agent.free_port", lambda: 12345)
    monkeypatch.setattr(
        "benchmarks.evaluate_browser_agent.subprocess.check_output",
        lambda argv, **kwargs: "codex-cli test\n",
    )
    campaign = Campaign(tmp_path / "campaign", "gpt-6-luna", "medium", False, False, 30)
    run = plan()[-2]
    (campaign.directory / "runs" / run["id"]).mkdir(parents=True)

    class PortalStub:
        def __init__(self, *args, **kwargs):
            self.cases = set()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def add(self, case, *args):
            self.cases.add(case)

        def verdict(self, case):
            assert case in self.cases
            return {
                "passed": False,
                "correct_submissions": 0,
                "wrong_submissions": 0,
                "submissions": 0,
            }

    @contextmanager
    def stop_before_model(*args, **kwargs):
        raise RuntimeError("test stopped before model")
        yield

    monkeypatch.setattr("benchmarks.evaluate_browser_agent.Portal", PortalStub)
    monkeypatch.setattr(campaign, "server", stop_before_model)
    result = campaign.run()
    assert result["status"] == "failed" and result["error"] == "test stopped before model"
    assert len(result["runs"]) == 1
    assert result["runs"][0]["stop_reason"] == "interrupted"
    assert result["runs"][0]["wall_seconds"] is None
    assert result["arms"]["baseline"]["runs"] == 1
    assert result["arms"]["baseline"]["pass_rate"] == 0
