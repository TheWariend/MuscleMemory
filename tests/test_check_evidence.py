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
