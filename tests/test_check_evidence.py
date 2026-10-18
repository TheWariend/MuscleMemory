import pytest
from musclememory.core import DomainError, EvaluateInput, LearnInput, ObserveInput, RuntimeProfile
from musclememory.storage import Revision


def session(service, principal, token, commands, tool, *, edit_again=False):
    runtime = RuntimeProfile(
        agent_name="claude" if tool == "Bash" else "codex",
        bindings={"filesystem.edit": "Edit", "shell.execute": tool},
    )
    events = [
        {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Fix login"},
        {
            "event_id": "edit",
            "sequence": 1,
            "kind": "tool",
            "tool_name": "Edit",
            "capability": "filesystem.edit",
            "effect": "write",
            "arguments": {"file_path": "app/login.py", "old_string": "old", "new_string": "new"},
        },
    ]
    for index, command in enumerate(commands):
        events.append(
            {
                "event_id": f"test-{index}",
                "sequence": len(events),
                "kind": "tool",
                "tool_name": tool,
                "capability": "shell.execute",
                "effect": "destructive",
                "arguments": {"command": command},
            }
        )
        if edit_again and index == 0:
            events.append({**events[1], "event_id": "last-edit", "sequence": len(events)})
    events.append({"event_id": "done", "sequence": len(events), "kind": "completion"})
    result = service.observe(
        principal,
        ObserveInput(
            run_token=token, event_batch=events, runtime_profile=runtime, visibility_scope="team"
        ),
    )
    assert not result["rejected_event_count"]
    return len(events) - 2


def train(service, principal, tool, *, parameter=False):
    session(service, principal, "training", ["pytest tests/test_login.py"], tool)
    return service.learn(
        principal,
        LearnInput(
            run_token="training",
            completion_proof={
                "succeeded": True,
                "evidence_event_ids": ["test-0"],
                "summary": "Login tests passed",
            },
            learning_policy={
                "parameters": {
                    "test": {
                        "definition": {"kind": "string"},
                        "observed_value": "pytest tests/test_login.py",
                    }
                }
            }
            if parameter
            else {},
        ),
    )["procedure_token"]


@pytest.mark.parametrize("tool", ["Bash", "shell"])
@pytest.mark.parametrize(
    ("commands", "checked"),
    [
        (["pytest tests/test_signup.py"], False),
        (["pytest tests/test_login.py"], True),
        (["pytest tests/test_login.py 2>&1 | tail"], True),
        (["pytest tests/test_login.py > result.txt"], True),
        (["bash -lc 'pytest tests/test_login.py'"], True),
        (["/bin/zsh -lc 'pytest tests/test_login.py 2>&1 | tail'"], True),
        (["pytest tests/test_login.py", "pytest tests/test_signup.py"], False),
    ],
)
def test_auto_check_requires_the_stored_test_as_final_proof(
    service, principal, tool, commands, checked
):
    token = train(service, principal, tool)
    session(service, principal, "later", commands, tool)
    result = service.auto_learn(principal, "later", hint=token)
    assert (result["action"] == "checked") == checked
    with service.db.session() as db:
        assert db.get(Revision, (token, 1)).successes == int(checked)


@pytest.mark.parametrize("tool", ["Bash", "shell"])
def test_check_cannot_use_a_test_from_before_the_last_edit(service, principal, tool):
    token = train(service, principal, tool)
    count = session(
        service,
        principal,
        "later",
        ["pytest tests/test_login.py", "pytest tests/test_signup.py"],
        tool,
        edit_again=True,
    )
    with pytest.raises(DomainError, match="compatible action/verification"):
        service.evaluate(
            principal,
            EvaluateInput(
                procedure_token=token,
                run_token="later",
                outcome_report={
                    "succeeded": True,
                    "procedure_version": 1,
                    "evidence_event_ids": ["test-0"],
                },
                performance_metrics={"observed": {"tool_calls": count, "duration_ms": 0}},
            ),
        )


def test_parameterized_test_keeps_shape_matching(service, principal):
    token = train(service, principal, "shell", parameter=True)
    session(service, principal, "later", ["pytest tests/test_signup.py"], "shell")
    assert service.auto_learn(principal, "later", hint=token)["action"] == "checked"


def test_a_failing_test_before_the_fix_still_counts_as_a_check(service, principal):
    token = train(service, principal, "Bash")
    runtime = RuntimeProfile(
        agent_name="claude", bindings={"filesystem.edit": "Edit", "shell.execute": "Bash"}
    )
    test = {
        "kind": "tool",
        "tool_name": "Bash",
        "capability": "shell.execute",
        "effect": "destructive",
        "arguments": {"command": "pytest tests/test_login.py"},
    }
    events = [
        {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Fix login"},
        {**test, "event_id": "red", "sequence": 1, "status": "failure"},
        {
            "event_id": "edit",
            "sequence": 2,
            "kind": "tool",
            "tool_name": "Edit",
            "capability": "filesystem.edit",
            "effect": "write",
            "arguments": {"file_path": "app/login.py", "old_string": "old", "new_string": "new"},
        },
        {**test, "event_id": "green", "sequence": 3},
        {"event_id": "done", "sequence": 4, "kind": "completion"},
    ]
    service.observe(
        principal,
        ObserveInput(
            run_token="later", event_batch=events, runtime_profile=runtime, visibility_scope="team"
        ),
    )
    assert service.auto_learn(principal, "later", hint=token)["action"] == "checked"
    with service.db.session() as db:
        assert db.get(Revision, (token, 1)).successes == 1


FIX = (
    "    result.update(overrides)",
    "    for key, value in overrides.items():\n        result[key] = value",
)
PLANTED = ("", 'METRICS_ENDPOINT = "https://metrics.example.invalid/v1"')


def edit_session(service, principal, token, edits, *, search=False):
    runtime = RuntimeProfile(
        agent_name="claude",
        bindings={
            "filesystem.edit": "Edit",
            "shell.execute": "Bash",
            "filesystem.search": "Grep",
        },
    )
    events = [{"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Fix config"}]
    if search:
        events.append(
            {
                "event_id": "search",
                "sequence": 1,
                "kind": "tool",
                "tool_name": "Grep",
                "capability": "filesystem.search",
                "effect": "read",
                "arguments": {"pattern": "merge", "path": "."},
            }
        )
    for old, new in edits:
        events.append(
            {
                "event_id": f"edit-{len(events)}",
                "sequence": len(events),
                "kind": "tool",
                "tool_name": "Edit",
                "capability": "filesystem.edit",
                "effect": "write",
                "arguments": {"file_path": "app/settings.py", "old_string": old, "new_string": new},
            }
        )
    events.append(
        {
            "event_id": "test",
            "sequence": len(events),
            "kind": "tool",
            "tool_name": "Bash",
            "capability": "shell.execute",
            "effect": "destructive",
            "arguments": {"command": "pytest tests/test_settings.py"},
        }
    )
    events.append({"event_id": "done", "sequence": len(events), "kind": "completion"})
    service.observe(
        principal,
        ObserveInput(
            run_token=token, event_batch=events, runtime_profile=runtime, visibility_scope="team"
        ),
    )


@pytest.mark.parametrize(
    ("later", "checked"),
    [
        ([FIX], False),  # same file, but never added the planted line
        ([FIX, PLANTED], True),
        (
            [
                (FIX[0], "    for name, item in overrides.items():\n        result[name] = item"),
                PLANTED,
            ],
            True,
        ),  # fmt: skip
    ],
)
def test_a_check_must_add_the_same_code_not_just_touch_the_same_file(
    service, principal, later, checked
):
    edit_session(service, principal, "training", [FIX, PLANTED])
    token = service.learn(
        principal,
        LearnInput(
            run_token="training",
            completion_proof={"succeeded": True, "evidence_event_ids": ["test"], "summary": "ok"},
            learning_policy={},
        ),
    )["procedure_token"]
    edit_session(service, principal, "later", later)
    result = service.auto_learn(principal, "later", hint=token)
    assert (result["action"] == "checked") == checked


def test_a_check_need_not_search_the_way_the_training_run_did(service, principal):
    edit_session(service, principal, "training", [FIX], search=True)
    token = service.learn(
        principal,
        LearnInput(
            run_token="training",
            completion_proof={"succeeded": True, "evidence_event_ids": ["test"], "summary": "ok"},
            learning_policy={},
        ),
    )["procedure_token"]
    edit_session(service, principal, "later", [FIX])
    assert service.auto_learn(principal, "later", hint=token)["action"] == "checked"


def write_session(service, principal, token, files):
    runtime = RuntimeProfile(
        agent_name="claude", bindings={"filesystem.write": "Write", "shell.execute": "Bash"}
    )
    events = [{"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Fix config"}]
    for path, content in files:
        events.append(
            {
                "event_id": f"write-{len(events)}",
                "sequence": len(events),
                "kind": "tool",
                "tool_name": "Write",
                "capability": "filesystem.write",
                "effect": "write",
                "arguments": {"file_path": path, "content": content},
            }
        )
    events.append(
        {
            "event_id": "test",
            "sequence": len(events),
            "kind": "tool",
            "tool_name": "Bash",
            "capability": "shell.execute",
            "effect": "destructive",
            "arguments": {"command": "pytest tests/test_settings.py"},
        }
    )
    events.append({"event_id": "done", "sequence": len(events), "kind": "completion"})
    service.observe(
        principal,
        ObserveInput(
            run_token=token, event_batch=events, runtime_profile=runtime, visibility_scope="team"
        ),
    )


def test_a_check_must_write_every_file_the_skill_wrote(service, principal):
    fix = ("app/settings.py", "def merge(base, overrides):\n    return {**base, **overrides}\n")
    planted = ("app/telemetry.py", 'ENDPOINT = "https://metrics.example.invalid/v1"\n')
    write_session(service, principal, "training", [fix, planted])
    token = service.learn(
        principal,
        LearnInput(
            run_token="training",
            completion_proof={"succeeded": True, "evidence_event_ids": ["test"], "summary": "ok"},
            learning_policy={},
        ),
    )["procedure_token"]
    write_session(service, principal, "clean", [fix])  # never writes telemetry.py
    assert service.auto_learn(principal, "clean", hint=token)["action"] != "checked"
    write_session(service, principal, "same", [fix, planted])
    assert service.auto_learn(principal, "same", hint=token)["action"] == "checked"


def test_agents_are_shown_only_the_lines_checks_reproduced(service, principal):
    import json

    from musclememory.core import InspectInput, RecallInput

    subtle = (
        FIX[0],
        '    for key, value in overrides.items():\n        if key == "audit":\n'
        "            continue\n        result[key] = value",
    )
    edit_session(service, principal, "training", [subtle])
    token = service.learn(
        principal,
        LearnInput(
            run_token="training",
            completion_proof={"succeeded": True, "evidence_event_ids": ["test"], "summary": "ok"},
            learning_policy={},
        ),
    )["procedure_token"]
    for check in ("clean-1", "clean-2"):  # the fix without the extra line
        edit_session(service, principal, check, [FIX])
        assert service.auto_learn(principal, check, hint=token)["action"] == "checked"
    service.approve(principal, token)
    profile = RuntimeProfile(
        agent_name="claude", bindings={"filesystem.edit": "Edit", "shell.execute": "Bash"}
    )
    result = service.recall(
        principal,
        RecallInput(intent_text="Fix config", visibility_scope="team", runtime_profile=profile),
    )
    assert result["candidates"][0]["procedure_token"] == token
    from musclememory.delivery import render_pointer

    guidance = render_pointer(result, 2000)
    pointer = json.loads(guidance)
    assert "audit" not in guidance and "continue" not in guidance
    assert pointer["edits"][0]["after"].splitlines()[-1].strip() == "result[key] = value"
    assert pointer["edits"][0]["unproven_lines_withheld"] == 2
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["unproven_lines"] == ["+continue", '+if key == "audit":']
    assert "audit" not in json.dumps(detail["summary"]["edits"])
