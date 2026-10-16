"""Whether a skill's existing test caught the bug: recorded always, required only if set."""

from dataclasses import replace

from musclememory.core import InspectInput, LearnInput, ObserveInput, RecallInput, RuntimeProfile

RUNTIME = RuntimeProfile(
    agent_name="claude", bindings={"filesystem.edit": "Edit", "shell.execute": "Bash"}
)


def session(service, principal, token, steps, file="app/login.py"):
    """steps: "edit", "pass" or "fail" (the skill's test), in order."""
    events = [{"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Fix login"}]
    for index, step in enumerate(steps, start=1):
        if step == "edit":
            event = {
                "tool_name": "Edit",
                "capability": "filesystem.edit",
                "effect": "write",
                "arguments": {"file_path": file, "old_string": "old", "new_string": "new"},
            }
        else:
            event = {
                "tool_name": "Bash",
                "capability": "shell.execute",
                "effect": "destructive",
                "arguments": {"command": "pytest tests/test_login.py"},
                "status": "success" if step == "pass" else "failure",
            }
        events.append({"event_id": f"e{index}", "sequence": index, "kind": "tool", **event})
    events.append({"event_id": "done", "sequence": len(events), "kind": "completion"})
    service.observe(
        principal,
        ObserveInput(
            run_token=token, event_batch=events, runtime_profile=RUNTIME, visibility_scope="team"
        ),
    )
    return f"e{len(steps)}"


def trusted(service, principal, file="app/login.py", training=("edit", "pass"), strict=True):
    service.settings = replace(service.settings, require_proven_test=strict)
    proof = session(service, principal, "train", training, file)
    token = service.learn(
        principal,
        LearnInput(
            run_token="train",
            completion_proof={"succeeded": True, "evidence_event_ids": [proof], "summary": "ok"},
        ),
    )["procedure_token"]
    for index in range(2):
        session(service, principal, f"check-{index}", ["edit", "pass"], file)
        assert service.auto_learn(principal, f"check-{index}", hint=token)["action"] == "checked"
    service.approve(principal, token)
    return token


def found(service, principal):
    request = RecallInput(intent_text="Fix login", runtime_profile=RUNTIME, visibility_scope="team")
    return not service.recall(principal, request)["no_match"]


def test_an_unproven_test_holds_the_skill_back_until_it_catches_the_bug(service, principal):
    token = trusted(service, principal)
    assert not found(service, principal)  # checked twice and approved, test never failed first
    session(service, principal, "proof", ["fail", "edit", "pass"])
    assert service.auto_learn(principal, "proof", hint=token)["action"] == "checked"
    assert found(service, principal)
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["test_proven"] and not detail["test_written_by_skill"]


def test_a_test_that_failed_before_the_fix_in_training_is_proven(service, principal):
    trusted(service, principal, training=("fail", "edit", "pass"))
    assert found(service, principal)


def test_a_failure_after_the_fix_proves_nothing(service, principal):
    token = trusted(service, principal)
    session(service, principal, "late", ["edit", "fail", "edit", "pass"])
    service.auto_learn(principal, "late", hint=token)  # the failure came after a change
    assert not found(service, principal)


def test_skills_that_write_their_own_test_go_to_the_person_instead(service, principal):
    token = trusted(service, principal, file="tests/test_login.py")
    assert found(service, principal)
    detail = service.inspect(principal, InspectInput(procedure_token=token))
    assert detail["test_written_by_skill"] and not detail["test_proven"]


def test_by_default_it_is_recorded_but_never_holds_a_skill_back(service, principal):
    token = trusted(service, principal, strict=False)
    assert found(service, principal)
    assert not service.inspect(principal, InspectInput(procedure_token=token))["test_proven"]
