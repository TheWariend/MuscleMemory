import copy

import pytest
from musclememory.browser import (
    BROWSER_CONTEXT,
    BrowserState,
    capture_browser_context,
    carry_out,
    ground_browser_steps,
    page_identity,
    render_browser_advice,
    skill_inputs,
)
from musclememory.compiler import compile_trace
from musclememory.core import ExecutionEvent, LearningPolicy


def state(*, name="Email", ref="live-1", scope="Refund", value="", extra=(), url=None):
    return BrowserState(
        url=url or "http://localhost:8080/refund?case=first",
        controls=[
            {
                "role": "textbox",
                "name": name,
                "ref": ref,
                "landmarks": [scope],
                "value": value,
            },
            *extra,
        ],
    )


def learned():
    before, after = state(), state(value="old@example.test")
    target = {"role": "textbox", "name": "Email", "landmarks": ["Refund"]}
    context = capture_browser_context("fill", target, before, after, value="old@example.test")
    events = [
        ExecutionEvent(event_id="intent", sequence=0, kind="intent", intent_text="Refund"),
        ExecutionEvent(
            event_id="fill",
            sequence=1,
            kind="tool",
            tool_name="act",
            capability="browser.act",
            arguments={"value": "old@example.test", BROWSER_CONTEXT: context},
        ),
        ExecutionEvent(event_id="done", sequence=2, kind="completion"),
    ]
    policy = LearningPolicy(
        parameters={
            "email": {"definition": {"kind": "string"}, "observed_value": "old@example.test"}
        }
    )
    definition, _ = compile_trace(
        "Refund",
        events,
        policy,
        "train",
        {"evidence_event_ids": ["fill"], "summary": "Value read back"},
    )
    return definition


def test_live_ref_and_fresh_parameters_replace_old_values():
    advice = ground_browser_steps(learned(), {"email": "new@example.test"}, state(ref="new-live"))
    step = advice["steps"][0]
    assert step["status"] == "match" and step["current_ref"] == "new-live"
    assert step["value"] == "new@example.test"
    assert "old@example.test" not in str(advice)
    assert (
        ground_browser_steps(
            learned(), {"email": "new@example.test"}, state(value="new@example.test")
        )["steps"][0]["status"]
        == "satisfied"
    )


@pytest.mark.parametrize(
    "current,reason",
    [
        (state(name="Reply address"), "target_missing"),
        (state(scope="Advertisement"), "target_missing"),
        (state(url="https://elsewhere.test/refund"), "page_changed"),
        (state(url="http://localhost:8080/other"), "page_changed"),
        (
            state(
                extra=[
                    {
                        "role": "textbox",
                        "name": "Email",
                        "ref": "duplicate",
                        "landmarks": ["Refund"],
                    }
                ]
            ),
            "target_ambiguous",
        ),
    ],
)
def test_changed_or_ambiguous_targets_require_adaptation(current, reason):
    step = ground_browser_steps(learned(), {"email": "new"}, current)["steps"][0]
    assert step["status"] == "adapt" and step["reason"] == reason
    assert "current_ref" not in step


def test_same_named_impostor_in_another_landmark_is_never_selected():
    current = state(
        extra=[{"role": "textbox", "name": "Email", "ref": "decoy", "landmarks": ["Advertisement"]}]
    )
    assert (
        ground_browser_steps(learned(), {"email": "new"}, current)["steps"][0]["current_ref"]
        == "live-1"
    )


def test_capture_rejects_unconfirmed_value_and_noop_click():
    target = {"role": "textbox", "name": "Email", "landmarks": ["Refund"]}
    with pytest.raises(ValueError, match="readback"):
        capture_browser_context("fill", target, state(), state(), value="not-written")
    with pytest.raises(ValueError, match="transition"):
        capture_browser_context("click", target, state(), state())


def test_budget_abstains_without_mutating_or_dropping_mismatch_warnings():
    advice = ground_browser_steps(learned(), {"email": "new"}, state(name="Renamed"))
    original = copy.deepcopy(advice)
    assert '"abstained"' in render_browser_advice(advice, budget=1)
    assert advice == original


def test_origin_normalization_rejects_embedded_credentials():
    assert page_identity("http://LOCALHOST/refund?x=1") == ["http", "localhost", 80, "/refund"]
    with pytest.raises(ValueError):
        page_identity("https://user:secret@example.test/refund")


def test_browser_search_indexes_checked_control_names_without_input_values():
    from musclememory.artifacts import search_document

    definition = learned().model_dump(mode="json")
    definition["title"] = definition["intent_signature"] = "Routine"
    context = definition["steps"][0]["arguments"][BROWSER_CONTEXT]
    context["target"] = {"role": "combobox", "name": "Visit mode", "landmarks": ["Appointment"]}
    context["postcondition"]["value"] = "not-for-search@example.test"
    document = search_document(definition)
    assert "Visit mode" in document and "Appointment" in document
    assert "not-for-search@example.test" not in document
    definition["steps"][0]["capability"] = "filesystem.read"
    assert "Visit mode" not in search_document(definition)
    definition["steps"][0]["capability"] = "browser.act"
    context["target"] = "malformed"
    assert "Visit mode" not in search_document(definition)


def test_malformed_experience_abstains_instead_of_breaking_browser_observation():
    definition = learned().model_dump(mode="json")
    del definition["steps"][0]["arguments"][BROWSER_CONTEXT]["target"]
    advice = ground_browser_steps(definition, {"email": "new"}, state())
    assert advice["steps"] == [] and advice["abstained"] == "invalid_browser_experience"
    assert ground_browser_steps(learned(), {}, state())["steps"] == []


def test_checkbox_readback_is_a_checked_transition():
    before = BrowserState(
        url="http://localhost/form",
        controls=[{"role": "checkbox", "name": "Terms", "ref": "before", "value": "false"}],
    )
    after = BrowserState(
        url="http://localhost/form",
        controls=[{"role": "checkbox", "name": "Terms", "ref": "after", "value": "true"}],
    )
    context = capture_browser_context("click", {"role": "checkbox", "name": "Terms"}, before, after)
    assert context["postcondition"]["changed_values"][0]["value"] == "true"


def test_browser_revision_keeps_independent_validation_and_approval_gates(service, principal):
    from musclememory.core import (
        EvaluateInput,
        LearnInput,
        ObserveInput,
        RecallInput,
        RuntimeProfile,
    )

    profile = RuntimeProfile(bindings={"browser.act": "act", "browser.verify": "verify"})

    def record(token, label, value):
        before, after = state(name=label), state(name=label, value=value)
        target = {"role": "textbox", "name": label, "landmarks": ["Refund"]}
        context = capture_browser_context("fill", target, before, after, value=value)
        service.observe(
            principal,
            ObserveInput(
                run_token=token,
                visibility_scope="team",
                runtime_profile=profile,
                event_batch=[
                    {"event_id": "i", "sequence": 0, "kind": "intent", "intent_text": "Refund"},
                    {
                        "event_id": "a",
                        "sequence": 1,
                        "kind": "tool",
                        "tool_name": "act",
                        "capability": "browser.act",
                        "effect": "write",
                        "arguments": {"value": value, BROWSER_CONTEXT: context},
                    },
                    {
                        "event_id": "v",
                        "sequence": 2,
                        "kind": "tool",
                        "tool_name": "verify",
                        "capability": "browser.verify",
                        "response": {"passed": True},
                    },
                    {"event_id": "c", "sequence": 3, "kind": "completion"},
                ],
            ),
        )

    def recall():
        return service.recall(
            principal,
            RecallInput(intent_text="Refund", runtime_profile=profile, visibility_scope="team"),
        )

    token = None
    for version, label in [(1, "Email"), (2, "Reply address")]:
        source = f"source-{version}"
        record(source, label, "source@example.test")
        learned = service.learn(
            principal,
            LearnInput(
                run_token=source,
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["v"],
                    "summary": "Independent domain check",
                },
                learning_policy={
                    "title": "Refund",
                    "revise_procedure_token": token,
                    "parameters": {
                        "email": {
                            "definition": {"kind": "string"},
                            "observed_value": "source@example.test",
                        }
                    },
                },
            ),
        )
        token = learned["procedure_token"]
        assert learned["procedure_version"] == version
        assert recall()["no_match"]
        for index in range(2):
            validation = f"validate-{version}-{index}"
            record(validation, label, f"check{index}@example.test")
            service.evaluate(
                principal,
                EvaluateInput(
                    procedure_token=token,
                    run_token=validation,
                    outcome_report={
                        "succeeded": True,
                        "procedure_version": version,
                        "evidence_event_ids": ["v"],
                    },
                    performance_metrics={"observed": {"tool_calls": 2, "duration_ms": 0}},
                ),
            )
        assert recall()["no_match"]
        service.approve(principal, token)
        assert recall()["candidates"][0]["procedure_version"] == version


def test_external_browser_results_use_durable_capture_and_explicit_verification(
    service, principal, tmp_path
):
    import json

    from musclememory.browser import record_browser_action
    from musclememory.core import ObserveInput, RuntimeProfile
    from musclememory.observer import ObservedRun

    class Sink:
        capture_identity = "a" * 64

        def musclememory_observe(self, **arguments):
            return service.observe(principal, ObserveInput(**arguments))

    profile = RuntimeProfile(bindings={"browser.act": "act"}, permissions=["browser.interact"])
    run = ObservedRun(Sink(), "Refund", profile, "team", spool_path=tmp_path / "capture.sqlite")
    target = {"role": "textbox", "name": "Email", "landmarks": ["Refund"]}
    event = record_browser_action(
        run,
        "fill",
        target,
        state(),
        state(value="new@example.test"),
        value="new@example.test",
        duration_ms=12,
        permission="browser.interact",
    )
    assert event == "e1" and run.events[1]["outcome_verified"]
    run.record_tool_result("browser.act", {"api_key": "sk-abcdef0123456789"}, {"found": True})
    assert not run.events[2]["outcome_verified"]
    assert "sk-abcdef0123456789" not in json.dumps(run.events)
    with pytest.raises(ValueError, match="permission"):
        run.record_tool_result("browser.act", {}, None, permission="ungranted")
    run.complete()
    assert service.get_run(principal, run.run_token)["events"][1]["duration_ms"] == 12
    with pytest.raises(RuntimeError, match="closed"):
        run.record_tool_result("browser.act", {}, None)
    run.close()


def test_browser_session_sdk_validates_revisions_and_flags_domain_failure(
    http, service, principal, credential, tmp_path
):
    import httpx
    from musclememory.browser_session import BrowserMemorySession
    from musclememory.sdk import MuscleMemoryClient

    def forward(request):
        response = http.request(
            request.method, request.url.path, content=request.content, headers=dict(request.headers)
        )
        return httpx.Response(response.status_code, content=response.content)

    profile = {"bindings": {"browser.act": "act", "browser.verify": "verify"}}
    target = {"role": "textbox", "name": "Email", "landmarks": ["Refund"]}
    with MuscleMemoryClient(
        "http://testserver", credential["api_key"], transport=httpx.MockTransport(forward)
    ) as client:

        def visit(value, *, expected=True, validation=None, revision=None):
            with BrowserMemorySession(
                client,
                "Refund",
                profile,
                "team",
                {"email": value},
                spool_path=tmp_path / (value + ".sqlite"),
            ) as session:
                session.record_action("fill", target, state(), state(value=value), value=value)
                return session.complete(
                    lambda: expected,
                    validation_target=validation,
                    learning_policy={"revise_procedure_token": revision} if revision else None,
                )

        learned = visit("source@example.test")
        token = learned["procedure_token"]
        for index in range(2):
            check = visit(f"check{index}@example.test", validation=learned)
        assert check["validation_status"] == "validated"
        with BrowserMemorySession(client, "Refund", profile, "team", {"email": "fresh"}) as session:
            assert not session.recalled["candidates"]  # Human approval is still required.
            session.run.complete(succeeded=False)
        service.approve(principal, token)
        with BrowserMemorySession(client, "Refund", profile, "team", {"email": "fresh"}) as session:
            assert '"current_ref":"live-1"' in session.guidance(state())
            assert '"value":"fresh"' in session.guidance(state())
            session.run.complete(succeeded=False)
        failed = visit("failed@example.test", expected=False, validation=learned)
        assert failed["validation_status"] == "flagged"
        revised = visit("recovered@example.test", revision=token)
        assert revised["procedure_version"] == 2
        with BrowserMemorySession(client, "Refund", profile, "team", {"email": "fresh"}) as session:
            assert not session.recalled["candidates"]
            session.run.complete(succeeded=False)


@pytest.mark.parametrize("result", [False, "success", None, RuntimeError("verifier offline")])
def test_browser_session_cannot_learn_from_false_invalid_or_broken_verifier(result):
    from musclememory.browser_session import BrowserMemorySession

    class Sink:
        def musclememory_observe(self, **arguments):
            return {
                "accepted_event_count": len(arguments["event_batch"]),
                "rejected_event_count": 0,
                "duplicate_event_count": 0,
            }

        def musclememory_recall(self, **arguments):
            return {"candidates": []}

        def musclememory_learn(self, **arguments):
            pytest.fail("An unverified session must never learn")

    def verify():
        if isinstance(result, Exception):
            raise result
        return result

    with BrowserMemorySession(
        Sink(), "Refund", {"bindings": {"browser.verify": "verify"}}, "team", {}
    ) as session:
        if result is False:
            assert session.complete(verify)["learned"] is False
        else:
            with pytest.raises((ValueError, RuntimeError)):
                session.complete(verify)
        assert session.run.closed and session.run.events[-1]["status"] == "failure"


URL = "http://localhost:8080/apply"


class Portal:
    """A two-stage form: details (Email, Continue), then review (Code, Copy billing, Submit)."""

    def __init__(self, *, url=URL, email_label="Email", sticky=True, blocked=False):
        self.url, self.email_label, self.sticky, self.blocked = url, email_label, sticky, blocked
        self.stage, self.values, self.notices, self.actions = "details", {"e": "", "c": ""}, [], []
        self.copy_billing = "true"

    def observe(self):
        def control(role, name, ref, section, value=""):
            return {
                "role": role,
                "name": name,
                "ref": ref,
                "landmarks": ["Apply", section],
                "value": value,
            }

        if self.stage == "details":
            controls = [
                control("textbox", self.email_label, "e", "Details", self.values["e"]),
                control("button", "Continue", "cont", "Details"),
            ]
        else:
            controls = [
                control("textbox", "Code", "c", "Review", self.values["c"]),
                control("checkbox", "Copy billing", "b", "Review", self.copy_billing),
                control("button", "Submit", "s", "Review"),
            ]
        return BrowserState(url=self.url, controls=controls, notices=list(self.notices))

    def act(self, action, ref, value):
        self.actions.append((action, ref, value))
        if action in {"fill", "select"} and self.sticky:
            self.values[ref] = value
        elif ref == "cont":
            if self.values["e"] and not self.blocked:
                self.stage = "review"
            else:
                self.notices.append("Complete the details first.")
        elif ref == "b":
            self.copy_billing = "false" if self.copy_billing == "true" else "true"
        elif ref == "s":
            self.notices.append("Application received.")


def learned_portal():
    """One proven run: email, Continue, code, untick Copy billing, Submit."""
    page = Portal()
    events = [ExecutionEvent(event_id="intent", sequence=0, kind="intent", intent_text="Apply")]
    plan = [
        ("fill", "textbox", "Email", "Details", "e", "old@example.test"),
        ("click", "button", "Continue", "Details", "cont", None),
        ("fill", "textbox", "Code", "Review", "c", "CODE-1"),
        ("click", "checkbox", "Copy billing", "Review", "b", None),
        ("click", "button", "Submit", "Review", "s", None),
    ]
    for index, (action, role, name, section, ref, value) in enumerate(plan, start=1):
        before = page.observe()
        page.act(action, ref, value)
        target = {"role": role, "name": name, "landmarks": ["Apply", section]}
        context = capture_browser_context(action, target, before, page.observe(), value=value)
        events.append(
            ExecutionEvent(
                event_id=f"s{index}",
                sequence=index,
                kind="tool",
                tool_name="act",
                capability="browser.act",
                arguments={"value": value, BROWSER_CONTEXT: context},
            )
        )
    events.append(ExecutionEvent(event_id="done", sequence=len(events), kind="completion"))
    policy = LearningPolicy(
        parameters={
            "email": {"definition": {"kind": "string"}, "observed_value": "old@example.test"},
            "code": {"definition": {"kind": "string"}, "observed_value": "CODE-1"},
        }
    )
    definition, _ = compile_trace(
        "Apply", events, policy, "train", {"evidence_event_ids": ["s5"], "summary": "Received"}
    )
    return definition


NEW = {"email": "new@example.test", "code": "CODE-2"}


def test_carry_out_does_every_step_but_the_final_submit():
    page = Portal()
    result = carry_out(learned_portal(), NEW, page.observe, page.act)
    assert page.actions == [
        ("fill", "e", "new@example.test"),
        ("click", "cont", None),
        ("fill", "c", "CODE-2"),
        ("click", "b", None),
    ]
    assert "Submit" in result["ready"] and page.copy_billing == "false"
    assert "Application received." not in page.notices  # the agent submits, never the skill
    assert {i["input"] for i in skill_inputs(learned_portal())} == {"email", "code"}


def test_carry_out_only_enters_values_the_agent_passed():
    page = Portal()
    result = carry_out(learned_portal(), {"email": "new@example.test"}, page.observe, page.act)
    assert ("fill", "c", "CODE-1") not in page.actions  # never the remembered code
    assert result["left_for_you"] == [{"field": "Code", "why": "needs your input"}]
    assert result["stopped"] == "fill_these_then_run_again"
    assert ("click", "s", None) not in page.actions


def test_a_renamed_field_is_left_for_the_agent_and_the_skill_resumes():
    page = Portal(email_label="Contact email")
    first = carry_out(learned_portal(), NEW, page.observe, page.act)
    assert first["stopped"] == "fill_these_then_run_again" and page.actions == []
    assert first["left_for_you"][0]["field"] == "Email" and "call this skill again" in first["next"]
    page.act("fill", "e", "new@example.test")  # the agent fills the renamed field
    page.act("click", "cont", None)  # and moves on
    second = carry_out(learned_portal(), NEW, page.observe, page.act)
    assert "ready" in second and page.values["c"] == "CODE-2"


@pytest.mark.parametrize(
    ("page", "reason"),
    [
        (Portal(url="https://lookalike.test/apply"), "page_changed"),
        (Portal(sticky=False), "value_did_not_stick"),
        (Portal(blocked=True), "click_did_not_move_on"),
    ],
)
def test_carry_out_stops_at_the_first_doubt(page, reason):
    result = carry_out(learned_portal(), NEW, page.observe, page.act)
    assert result["stopped"] == reason
    assert ("click", "s", None) not in page.actions
