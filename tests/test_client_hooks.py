import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from musclememory.client_hooks import DEFAULT_TOOLS, HookQueue, install_hooks, load_config, run_hook
from musclememory.core import RuntimeProfile
from musclememory.sdk import MuscleMemoryClient


def profile(client):
    return RuntimeProfile(
        agent_name=client, bindings={cap: name for name, (cap, _) in DEFAULT_TOOLS[client].items()}
    )


def test_hook_credentials_work_with_read_only_access(tmp_path, monkeypatch):
    secret = tmp_path / "credential.env"
    secret.write_text("MM_API_KEY=read-only-fixture-credential\n")
    secret.chmod(0o600)
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"env_file": str(secret)}))
    monkeypatch.delenv("MM_API_KEY", raising=False)
    original = os.open

    def restricted_open(path, flags, *args, **kwargs):
        if str(path) == str(secret) and flags & os.O_ACCMODE != os.O_RDONLY:
            raise PermissionError("Credential permits read access only")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", restricted_open)
    _, _, _, client = load_config(config, "codex")
    try:
        assert client.http.headers["authorization"] == "Bearer read-only-fixture-credential"
        assert secret.read_text() == "MM_API_KEY=read-only-fixture-credential\n"
    finally:
        client.close()


def test_codex_receipt_recovery_supports_learning_and_actual_usage(
    tmp_path, credential, remote_server, principal, service, monkeypatch
):
    from musclememory.core import LearnInput

    client = MuscleMemoryClient(remote_server, credential["api_key"])
    queue = HookQueue(tmp_path / "state", client.capture_identity)
    tools, runtime = DEFAULT_TOOLS["codex"], profile("codex")
    token, _ = queue.handle(
        "codex", payload("UserPromptSubmit", prompt="Verify the project"), tools, runtime, "team"
    )
    queue.handle(
        "codex",
        payload(
            "PreToolUse",
            tool_use_id="a",
            tool_name="Bash",
            tool_input={"command": "python3 -m unittest"},
        ),
        tools,
        runtime,
        "team",
    )
    queue.handle(
        "codex",
        payload(
            "PostToolUse",
            tool_use_id="a",
            tool_name="Bash",
            tool_input={"command": "python3 -m unittest"},
            tool_response="OK",
        ),
        tools,
        runtime,
        "team",
    )
    queue.handle("codex", payload("Stop"), tools, runtime, "team")
    queue.deliver(client, token)
    assert service.get_run(principal, token)["status"] == "incomplete"
    with pytest.raises(ValueError, match="complete"):
        queue.attest(token, True)
    receipts = [
        {
            "type": "item.completed",
            "item": {
                "type": "command_execution",
                "id": "a",
                "command": "/bin/zsh -lc 'python3 -m unittest'",
                "exit_code": 0,
            },
        },
        {
            "type": "turn.completed",
            "usage": {"input_tokens": 123, "cached_input_tokens": 100, "output_tokens": 7},
        },
    ]
    original_observe = client.musclememory_observe

    def lost_ack(**arguments):
        original_observe(**arguments)
        raise httpx.ReadError("lost acknowledgement")

    monkeypatch.setattr(client, "musclememory_observe", lost_ack)
    with pytest.raises(httpx.ReadError):
        queue.reconcile(client, token, receipts, True)
    derived = queue.db.execute("SELECT derived FROM reconciliations").fetchone()[0]
    queue.close()
    monkeypatch.setattr(client, "musclememory_observe", original_observe)
    queue = HookQueue(tmp_path / "state", client.capture_identity)
    try:
        assert queue.reconcile(client, token, receipts, True) == derived
        assert queue.reconcile(client, token, receipts, True) == derived
        assert service.get_run(principal, token)["status"] == "incomplete"
        evidence = queue.learning_evidence(client, derived)
        assert evidence[-1]["status"] == "success"
        learned = service.learn(
            principal,
            LearnInput(
                run_token=derived,
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": [evidence[1]["event_id"]],
                    "summary": "Operator verified the regression tests",
                },
                learning_policy={},
            ),
        )
        assert learned["validation_status"] == "provisional"
        measured = service.session_usage(principal, "team")["items"][0]
        assert measured["input_tokens"] == 123 and measured["cached_input_tokens"] == 100
        assert measured["output_tokens"] == 7 and measured["capture_complete"]
        assert measured["billed_cost_usd"] is None and measured["elapsed_ms"] is None
        with pytest.raises(ValueError, match="immutable"):
            queue.reconcile(client, token, receipts, False)
    finally:
        queue.close()
        client.close()


def payload(event, **fields):
    return {
        "hook_event_name": event,
        "session_id": "session-a",
        "turn_id": "turn-a",
        "cwd": "/example",
        **fields,
    }


def test_native_capture_survives_processes_and_lost_ack(
    tmp_path, credential, remote_server, principal, service
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"url": remote_server, "namespace": "team", "state_dir": str(tmp_path / "state")}
        )
    )
    env = {**os.environ, "MM_API_KEY": credential["api_key"]}
    events = [
        payload("UserPromptSubmit", prompt="Inspect repository manifest"),
        payload(
            "PreToolUse",
            tool_use_id="a",
            tool_name="Read",
            tool_input={"file_path": "package.json"},
        ),
        payload(
            "PostToolUse",
            tool_use_id="a",
            tool_name="Read",
            tool_input={"file_path": "package.json", "api_key": "secret-value"},
            tool_response={"name": "musclememory"},
            duration_ms=1,
        ),
        payload("Stop"),
    ]
    for event in events:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "musclememory",
                "hooks",
                "receive",
                "--client",
                "claude",
                "--config",
                str(config),
            ],
            input=json.dumps(event),
            text=True,
            env=env,
            capture_output=True,
            check=True,
        )
        json.loads(result.stdout)
    with MuscleMemoryClient(remote_server, credential["api_key"]) as client:
        queue = HookQueue(tmp_path / "state", client.capture_identity)
        try:
            token = queue.listing()[0]["token"]
            trace = service.get_run(principal, token)
            assert trace["status"] == "recording"  # Stop never self-attests correctness.
            assert trace["events"][1]["arguments"]["api_key"] == "[REDACTED]"
            queue.db.execute("UPDATE events SET ack=0")
            assert queue.deliver(client, token) == 2  # Exact duplicate delivery accepted.
            assert len(service.get_run(principal, token)["events"]) == 2
            queue.attest(token, True)
            queue.deliver(client, token)
            assert service.get_run(principal, token)["status"] == "success"
        finally:
            queue.close()
    assert credential["api_key"].encode() not in (tmp_path / "state/hooks.sqlite").read_bytes()
    assert b"secret-value" not in (tmp_path / "state/hooks.sqlite").read_bytes()


@pytest.mark.parametrize("client", ["claude", "codex", "cursor"])
def test_vendor_shapes_failure_and_no_permission_changes(client, tmp_path):
    queue = HookQueue(tmp_path / client, "a" * 64)
    tools, runtime = DEFAULT_TOOLS[client], profile(client)
    prompt_event = "beforeSubmitPrompt" if client == "cursor" else "UserPromptSubmit"
    tool_event = "postToolUse" if client == "cursor" else "PostToolUse"
    tool = next(iter(tools))
    try:
        token, _ = queue.handle(
            client, payload(prompt_event, prompt="Perform the task"), tools, runtime, "team"
        )
        response = {"exitCode": 3, "stdout": "failure"}
        queue.handle(
            client,
            payload(
                tool_event,
                tool_use_id="failed-1",
                tool_name=tool,
                tool_input={},
                **(
                    {"tool_output": json.dumps(response)}
                    if client == "cursor"
                    else {"tool_response": response}
                ),
            ),
            tools,
            runtime,
            "team",
        )
        queue.handle(
            client, payload("stop" if client == "cursor" else "Stop"), tools, runtime, "team"
        )
        event = json.loads(
            queue.db.execute("SELECT payload FROM events WHERE sequence=1").fetchone()[0]
        )
        assert event["status"] == "failure"
        assert queue.listing()[0]["closed"] == (1 if client == "codex" else 0)
        assert queue.listing()[0]["ended"] == 1
    finally:
        queue.close()


def test_parallel_and_unknown_tools_block_attestation(tmp_path):
    queue = HookQueue(tmp_path / "state", "a" * 64)
    tools, runtime = DEFAULT_TOOLS["claude"], profile("claude")
    try:
        token, _ = queue.handle(
            "claude", payload("UserPromptSubmit", prompt="Task"), tools, runtime, "team"
        )
        for source in ["first", "second"]:
            queue.handle(
                "claude",
                payload("PreToolUse", tool_use_id=source, tool_name="Read"),
                tools,
                runtime,
                "team",
            )
        queue.handle(
            "claude",
            payload("PostToolUse", tool_use_id="unknown", tool_name="Unknown"),
            tools,
            runtime,
            "team",
        )
        queue.handle("claude", payload("Stop"), tools, runtime, "team")
        with pytest.raises(ValueError, match="complete"):
            queue.attest(token, True)
        gaps = json.loads(queue.listing()[0]["gaps"])
        assert "parallel_tool_dependencies_unknown" in gaps
        assert "unmapped_tool:Unknown" in gaps
        assert "unfinished_tool" in gaps
    finally:
        queue.close()


def test_cross_process_queue_serializes_events(tmp_path):
    directory = tmp_path / "queue"
    with_init = HookQueue(directory, "a" * 64)
    tools, runtime = DEFAULT_TOOLS["claude"], profile("claude")
    token, _ = with_init.handle(
        "claude", payload("UserPromptSubmit", prompt="Task"), tools, runtime, "team"
    )
    with_init.close()

    def append(index):
        queue = HookQueue(directory, "a" * 64)
        try:
            queue.handle(
                "claude",
                payload(
                    "PostToolUse",
                    tool_name="Read",
                    tool_use_id=f"call-{index}",
                    tool_input={"index": index},
                    tool_response="ok",
                ),
                tools,
                runtime,
                "team",
            )
        finally:
            queue.close()

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(append, range(20)))
    queue = HookQueue(directory, "a" * 64)
    try:
        events = [
            json.loads(row[0])
            for row in queue.db.execute(
                "SELECT payload FROM events WHERE run=? ORDER BY sequence", (token,)
            )
        ]
        assert [event["sequence"] for event in events] == list(range(21))
        assert len({event["arguments"]["index"] for event in events[1:]}) == 20
    finally:
        queue.close()


def test_installer_preserves_config_idempotently(tmp_path):
    config = tmp_path / "memory.json"
    config.write_text("{}")
    directory = tmp_path / ".claude"
    directory.mkdir()
    (directory / "settings.json").write_text(
        json.dumps(
            {
                "permissions": {"deny": ["Bash(rm *)"]},
                "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]},
            }
        )
    )
    install_hooks(tmp_path, ["claude", "codex", "cursor"], config)
    first = (directory / "settings.json").read_bytes()
    install_hooks(tmp_path, ["claude", "codex", "cursor"], config)
    assert first == (directory / "settings.json").read_bytes()
    parsed = json.loads(first)
    assert parsed["permissions"]["deny"] == ["Bash(rm *)"]
    assert len(parsed["hooks"]["Stop"]) == 2
    assert len(list(directory.glob("*.musclememory-backup-*"))) == 1
    assert json.loads((tmp_path / ".cursor/hooks.json").read_text())["version"] == 1


def test_cursor_pre_hook_has_valid_advisory_output(
    tmp_path, monkeypatch, credential, remote_server
):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {"url": remote_server, "namespace": "team", "state_dir": str(tmp_path / "state")}
        )
    )
    monkeypatch.setenv("MM_API_KEY", credential["api_key"])
    assert run_hook(config, "cursor", payload("beforeSubmitPrompt", prompt="Task")) == {
        "continue": True
    }
    assert run_hook(config, "cursor", payload("preToolUse", tool_name="Read", tool_use_id="x")) == {
        "permission": "allow"
    }


@pytest.fixture
def codex_hook_host(tmp_path, monkeypatch, credential, service, http):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"namespace": "team", "state_dir": str(tmp_path / "state")}))
    monkeypatch.setenv("MM_API_KEY", credential["api_key"])
    calls, faults = [], set()

    def response(request):
        calls.append(request.url.path)
        if request.url.path in faults:
            raise httpx.ConnectError("Controlled offline test", request=request)
        result = http.post(
            request.url.path,
            params=dict(request.url.params),
            json=json.loads(request.content or b"{}"),
        )
        return httpx.Response(result.status_code, json=result.json())

    def connect(*args, **kwargs):
        return MuscleMemoryClient(*args, **kwargs, transport=httpx.MockTransport(response))

    monkeypatch.setattr("musclememory.client_hooks.MuscleMemoryClient", connect)
    return config, calls, faults, connect


def test_codex_prompt_hook_injects_once_and_tool_hooks_only_capture(codex_hook_host, credential):
    config, calls, _, connect = codex_hook_host
    runtime = profile("codex")
    with connect("http://127.0.0.1:8080", credential["api_key"]) as client:
        learned = None
        for index in range(3):
            client.musclememory_observe(
                run_token=f"checked-{index}",
                runtime_profile=runtime,
                visibility_scope="team",
                event_batch=[
                    {"event_id": "intent", "sequence": 0, "kind": "intent", "intent_text": "Task"},
                    {
                        "event_id": "read",
                        "sequence": 1,
                        "kind": "tool",
                        "tool_name": "Bash",
                        "capability": "shell.execute",
                        "effect": "destructive",
                        "arguments": {"command": "cat fixture.json"},
                    },
                    {"event_id": "done", "sequence": 2, "kind": "completion"},
                ],
            )
            if index == 0:
                learned = client.musclememory_learn(
                    run_token="checked-0",
                    completion_proof={
                        "succeeded": True,
                        "evidence_event_ids": ["read"],
                        "summary": "Controlled fixture proof",
                    },
                )
            else:
                client.musclememory_evaluate(
                    procedure_token=learned["procedure_token"],
                    run_token=f"checked-{index}",
                    outcome_report={
                        "succeeded": True,
                        "procedure_version": 1,
                        "evidence_event_ids": ["read"],
                    },
                    performance_metrics={"observed": {"tool_calls": 1, "duration_ms": 0}},
                )
    prompt_output = run_hook(config, "codex", payload("UserPromptSubmit", prompt="A reworded task"))
    output = run_hook(
        config,
        "codex",
        payload(
            "PostToolUse",
            tool_name="Bash",
            tool_use_id="live-read",
            tool_input={"command": "cat different.json"},
            tool_response="Process exited with code 0\nLive result",
        ),
    )
    # Guidance arrives once, at the prompt. Tool hooks only capture.
    assert output == {}
    context = prompt_output["hookSpecificOutput"]
    recalled = json.loads(context["additionalContext"])
    assert recalled["procedure"] == learned["procedure_token"]
    assert "Normal permissions" in recalled["notice"]
    assert calls.count("/v1/recall") == 1 and calls[-1] == "/v1/observe"


def test_capture_control_does_not_inject_memory(codex_hook_host, service, principal):
    config, calls, _, connect = codex_hook_host
    configuration = json.loads(config.read_text())
    configuration["recall_enabled"] = False
    config.write_text(json.dumps(configuration))
    assert run_hook(config, "codex", payload("UserPromptSubmit", prompt="Inspect fixture")) == {}
    assert (
        run_hook(config, "codex", payload("PreToolUse", tool_name="Bash", tool_use_id="read")) == {}
    )
    assert (
        run_hook(
            config,
            "codex",
            payload(
                "PostToolUse",
                tool_name="Bash",
                tool_use_id="read",
                tool_input={"command": "cat fixture.json"},
                tool_response="{}",
            ),
        )
        == {}
    )
    run_hook(config, "codex", payload("Stop"))
    assert "/v1/recall" not in calls
    with connect("http://127.0.0.1:8080", os.environ["MM_API_KEY"]) as client:
        queue = HookQueue(configuration["state_dir"], client.capture_identity)
        try:
            captured = queue.listing()[0]
            assert captured["ended"] and json.loads(captured["gaps"]) == ["unverified_exit_status"]
            trace = service.get_run(principal, captured["token"])
            assert trace["events"][1]["arguments"]["command"] == "cat fixture.json"
            assert trace["status"] == "incomplete"
        finally:
            queue.close()


def codex_session(config, turn, prompt, test_exit=0):
    """One Codex session: edit a file, run the tests, stop."""

    def hook(event, **fields):
        return run_hook(config, "codex", payload(event, turn_id=turn, **fields))

    hook("UserPromptSubmit", prompt=prompt)
    patch = (
        "*** Begin Patch\n*** Update File: app/rounding.py\n@@\n-int(x)\n+round(x)\n*** End Patch"
    )
    hook("PreToolUse", tool_name="apply_patch", tool_use_id=f"{turn}-edit")
    hook(
        "PostToolUse",
        tool_name="apply_patch",
        tool_use_id=f"{turn}-edit",
        tool_input={"input": patch},
        tool_response="Done!",
    )
    hook("PreToolUse", tool_name="Bash", tool_use_id=f"{turn}-test")
    hook(
        "PostToolUse",
        tool_name="Bash",
        tool_use_id=f"{turn}-test",
        tool_input={"command": "python3 -m unittest discover -s tests"},
        tool_response=f"Process exited with code {test_exit}\nOK",
    )
    return hook("Stop")


def test_passing_sessions_learn_then_wait_for_approval(codex_hook_host, service, principal):
    config, calls, _, _ = codex_hook_host
    codex_session(config, "t1", "Fix the rounding bug in app/rounding.py")
    skills = service.listing(principal, "team", "procedures")["items"]
    assert len(skills) == 1 and skills[0]["needs_review"] and skills[0]["auto_learned"]
    assert skills[0]["status"] == "provisional"
    # Two later passing sessions on the same file count as the skill's two checks.
    codex_session(config, "t2", "Rounding in app/rounding.py is wrong again")
    codex_session(config, "t3", "Please repair rounding in app/rounding.py")
    skills = service.listing(principal, "team", "procedures")["items"]
    assert len(skills) == 1 and skills[0]["successes"] == 2 and skills[0]["status"] == "active"

    def recall():
        from musclememory.core import RecallInput

        return service.recall(
            principal,
            RecallInput(
                intent_text="Fix the rounding bug in app/rounding.py",
                runtime_profile=profile("codex"),
                visibility_scope="team",
            ),
        )

    assert recall()["no_match"]  # verified, but not handed to agents before an OK
    service.approve(principal, skills[0]["procedure_token"])
    assert not recall()["no_match"]


def test_failing_tests_teach_nothing(codex_hook_host, service, principal):
    config, *_ = codex_hook_host
    codex_session(config, "t1", "Fix the rounding bug", test_exit=1)
    assert service.listing(principal, "team", "procedures")["items"] == []
