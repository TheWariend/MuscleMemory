"""Optional account-backed smoke test; runs only with explicit MM_TEST_CODEX=1."""

import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from uuid import uuid4

import pytest
from musclememory.client_hooks import HookQueue, install_hooks
from musclememory.sdk import MuscleMemoryClient
from musclememory.security import redact


def test_live_codex_native_hook_capture(tmp_path, remote_server, credential, service, principal):
    if os.getenv("MM_TEST_CODEX") != "1" or not shutil.which("codex"):
        pytest.skip("Set MM_TEST_CODEX=1 with a logged-in Codex CLI for the account-backed smoke")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "fixture.json").write_text('{"sentinel":"musclememory-native-smoke"}\n')
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secret = private / "memory.env"
    secret.write_text(f"MM_API_KEY={credential['api_key']}\n")
    secret.chmod(0o600)
    config = private / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "url": remote_server,
                "namespace": "team",
                "env_file": str(secret),
                "state_dir": str(private / "state"),
            }
        )
    )
    install_hooks(project, ["codex"], config)
    # CLI overrides isolate the smoke from this machine's user/project layers.
    # The installed project file is independently checked by installer tests.
    definitions = json.loads((project / ".codex/hooks.json").read_text())["hooks"]
    hook_overrides = []
    for event, groups in definitions.items():
        command = groups[0]["hooks"][0]["command"]
        value = f'[{{hooks=[{{type="command",command={json.dumps(command)},timeout=10}}]}}]'
        hook_overrides.extend(["-c", f"hooks.{event}={value}"])
    discover_project = os.getenv("MM_TEST_CODEX_DISCOVERY") == "1"
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MM_")}
    model = os.getenv("MM_TEST_CODEX_MODEL")
    user_config = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml"
    if not model and user_config.exists():
        model = tomllib.loads(user_config.read_text()).get("model")
    isolated_home = private / "codex-home"
    if discover_project:
        isolated_home.mkdir(mode=0o700)
        auth = user_config.with_name("auth.json")
        if not auth.exists():
            pytest.skip("Project discovery smoke needs file-backed CLI authentication")
        shutil.copyfile(auth, isolated_home / "auth.json")
        (isolated_home / "auth.json").chmod(0o600)
        (isolated_home / "config.toml").write_text(
            f'[projects.{json.dumps(str(project.resolve()))}]\ntrust_level="trusted"\n'
        )
        environment["CODEX_HOME"] = str(isolated_home)
    try:
        result = subprocess.run(
            [
                "codex",
                "exec",
                *([] if discover_project else ["--ignore-user-config"]),
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--dangerously-bypass-hook-trust",
                "--json",
                *([] if discover_project else hook_overrides),
                "-c",
                f'projects.{json.dumps(str(project.resolve()))}.trust_level="trusted"',
                *(["--model", model] if model else []),
                "-C",
                str(project),
                "Read fixture.json with exactly one shell command: cat fixture.json. "
                "Then reply only with the sentinel value in that JSON. Read no other files.",
            ],
            env=environment,
            capture_output=True,
            text=True,
            timeout=150,
        )
    finally:
        if discover_project:
            shutil.rmtree(isolated_home, ignore_errors=True)
    assert result.returncode == 0, (
        f"Codex smoke exited {result.returncode}: {redact(result.stdout[-1500:])}"
    )
    assert "musclememory-native-smoke" in result.stdout
    with MuscleMemoryClient(remote_server, credential["api_key"]) as client:
        queue = HookQueue(private / "state", client.capture_identity)
        try:
            runs = queue.listing()
            assert len(runs) == 1, str(redact(result.stderr[-2500:]))
            assert runs[0]["ended"] == 1
            assert not (set(json.loads(runs[0]["gaps"])) - {"unverified_exit_status"})
            token = runs[0]["token"]
            trace = service.get_run(principal, token)
            assert trace["status"] == "unverified"
            assert [event["kind"] for event in trace["events"]] == ["intent", "tool", "completion"]
            assert trace["events"][1]["tool_name"] == "Bash"
            assert trace["events"][1]["status"] == "success"
        finally:
            queue.close()


def test_live_codex_learning_validation_and_recall(
    tmp_path, remote_server, credential, service, principal
):
    """Four fresh model sessions; proof checks live output, never the Stop event."""
    if os.getenv("MM_TEST_CODEX") != "1" or not shutil.which("codex"):
        pytest.skip("Set MM_TEST_CODEX=1 with a logged-in Codex CLI for the account-backed trial")
    project = tmp_path / "project"
    project.mkdir()
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    (project / "manifest.json").write_text('{"target":"fixture.json"}\n')
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    secret = private / "memory.env"
    secret.write_text(f"MM_API_KEY={credential['api_key']}\n")
    secret.chmod(0o600)
    config = private / "hooks.json"
    config.write_text(
        json.dumps(
            {
                "url": remote_server,
                "namespace": "team",
                "env_file": str(secret),
                "state_dir": str(private / "state"),
            }
        )
    )
    install_hooks(project, ["codex"], config)
    user_home = Path(os.getenv("CODEX_HOME", str(Path.home() / ".codex")))
    auth = user_home / "auth.json"
    if not auth.exists():
        pytest.skip("The isolated live trial needs file-backed CLI authentication")
    isolated_home = private / "codex-home"
    isolated_home.mkdir(mode=0o700)
    shutil.copyfile(auth, isolated_home / "auth.json")
    (isolated_home / "auth.json").chmod(0o600)
    (isolated_home / "config.toml").write_text(
        f'[projects.{json.dumps(str(project.resolve()))}]\ntrust_level="trusted"\n'
    )
    environment = {k: v for k, v in os.environ.items() if not k.startswith("MM_")}
    environment["CODEX_HOME"] = str(isolated_home)
    model = os.getenv("MM_TEST_CODEX_MODEL")
    if not model and (user_home / "config.toml").exists():
        model = tomllib.loads((user_home / "config.toml").read_text()).get("model")
    prompt = (
        "Read manifest.json using exactly cat manifest.json. It names a JSON file. "
        "Read that file with a second separate cat command, then reply with its sentinel. "
        "Read no other files and run no other commands."
    )
    # The random label is given only to the memory service, never to Codex's
    # fourth prompt. Its appearance in the fresh answer proves context delivery.
    label = "codex-memory-" + uuid4().hex
    learned = None
    try:
        with MuscleMemoryClient(remote_server, credential["api_key"]) as client:
            queue = HookQueue(private / "state", client.capture_identity)
            try:
                for index in range(4):
                    sentinel = "live-" + uuid4().hex
                    (project / "fixture.json").write_text(json.dumps({"sentinel": sentinel}))
                    before = {run["token"] for run in queue.listing()}
                    result = subprocess.run(
                        [
                            "codex",
                            "exec",
                            "--ephemeral",
                            "--skip-git-repo-check",
                            "--sandbox",
                            "read-only",
                            "--dangerously-bypass-hook-trust",
                            "--json",
                            *(["--model", model] if model else []),
                            "-C",
                            str(project),
                            prompt
                            + (
                                " Also include the title from applicable validated MuscleMemory "
                                "guidance in your answer. If none is available, write no-memory."
                                if index == 3
                                else ""
                            ),
                        ],
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=150,
                    )
                    assert result.returncode == 0, (
                        f"Codex trial exited {result.returncode}: {redact(result.stdout[-1500:])}"
                    )
                    messages = [
                        json.loads(line)
                        for line in result.stdout.splitlines()
                        if line.startswith("{")
                    ]
                    answer = "\n".join(
                        message["item"].get("text", "")
                        for message in messages
                        if message.get("type") == "item.completed"
                        and message.get("item", {}).get("type") == "agent_message"
                    )
                    assert sentinel in answer, str(redact(result.stdout[-1500:]))
                    runs = [run for run in queue.listing() if run["token"] not in before]
                    assert len(runs) == 1, str(redact(result.stderr[-1500:]))
                    assert runs[0]["ended"] == 1 and not (
                        set(json.loads(runs[0]["gaps"])) - {"unverified_exit_status"}
                    )
                    token = runs[0]["token"]
                    trace = service.get_run(principal, token)
                    tools = [event for event in trace["events"] if event["kind"] == "tool"]
                    assert len(tools) == 2
                    assert all(tool["status"] == "success" for tool in tools)
                    assert [tool["arguments"]["command"] for tool in tools] == [
                        "cat manifest.json",
                        "cat fixture.json",
                    ]
                    evidence = [tool["event_id"] for tool in tools]
                    if index == 3:
                        assert label in answer, "Validated guidance never reached the model"
                    token = queue.reconcile(client, token, messages, True)
                    if index == 0:
                        learned = client.musclememory_learn(
                            run_token=token,
                            completion_proof={
                                "succeeded": True,
                                "evidence_event_ids": evidence,
                                "verifier": "tool_assertion",
                                "summary": "Fresh sentinel matched the on-disk fixture",
                            },
                            learning_policy={"title": label},
                        )
                        assert learned["validation_status"] == "provisional"
                    elif index < 3:
                        evaluated = client.musclememory_evaluate(
                            procedure_token=learned["procedure_token"],
                            run_token=token,
                            outcome_report={
                                "succeeded": True,
                                "procedure_version": 1,
                                "evidence_event_ids": evidence,
                            },
                            performance_metrics={
                                "observed": {
                                    "tool_calls": len(tools),
                                    "duration_ms": sum(tool["duration_ms"] for tool in tools),
                                }
                            },
                        )
                        assert evaluated["successes"] == index
                        assert evaluated["validation_status"] == (
                            "validated" if index == 2 else "provisional"
                        )
            finally:
                queue.close()
    finally:
        shutil.rmtree(isolated_home, ignore_errors=True)
