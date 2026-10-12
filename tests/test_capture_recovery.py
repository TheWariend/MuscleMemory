import asyncio
import hashlib
import json
import subprocess
import sys

import pytest
from conftest import PROFILE
from musclememory.core import DomainError, LearnInput
from musclememory.observer import ObservedRun
from musclememory.sdk import MuscleMemoryClient
from test_observer import LocalSink


class DurableSink(LocalSink):
    def __init__(self, service, principal):
        super().__init__(service, principal)
        self.capture_identity = hashlib.sha256(principal.key_id.encode()).hexdigest()
        self.drop_response = False
        self.batches = []

    def musclememory_observe(self, **arguments):
        self.batches.append(arguments["event_batch"])
        result = super().musclememory_observe(**arguments)
        if self.drop_response:
            self.drop_response = False
            raise ConnectionError("Simulated lost acknowledgement")
        return result


def test_durable_recovery_after_lost_acknowledgement(service, principal, tmp_path):
    sink = DurableSink(service, principal)
    path = tmp_path / "capture.sqlite"
    run = ObservedRun(sink, "Inspect repository manifest", PROFILE, "team", spool_path=path)
    original = {"found": True, "authorization": "Bearer sk-supersecret123456"}
    read = run.instrument("repository.inspect")(lambda path, password: original)
    assert read("package.json", "do-not-store-this") is original
    sink.drop_response = True
    with pytest.raises(ConnectionError):
        run.complete()
    run.close()
    assert path.stat().st_mode & 0o777 == 0o600
    assert b"do-not-store-this" not in path.read_bytes()
    assert b"sk-supersecret123456" not in path.read_bytes()
    resumed = ObservedRun.resume(sink, path)
    assert resumed.closed and len(resumed.pending) == 3
    result = resumed.flush()
    assert result["duplicate_event_count"] == 3
    assert len(service.get_run(principal, run.run_token)["events"]) == 3
    assert not resumed.pending
    resumed.close()
    acknowledged = ObservedRun.resume(sink, path)
    assert not acknowledged.pending and acknowledged.measurements()["tool_calls"] == 1
    with pytest.raises(RuntimeError, match="closed"):
        acknowledged.instrument("repository.inspect")(lambda: None)()
    acknowledged.close()


def test_spool_survives_a_separate_capture_process(service, principal, tmp_path):
    sink = DurableSink(service, principal)
    path = tmp_path / "process.sqlite"
    script = """
import sys
from musclememory.observer import ObservedRun
class Offline:
    capture_identity = sys.argv[2]
    def musclememory_observe(self, **kwargs):
        raise ConnectionError('Offline')
run=ObservedRun(Offline(), 'Inspect repository manifest',
    {'bindings': {'repository.inspect':'read_file'}, 'agent_name':'python-agent'},
    'team', 'process-recovery', spool_path=sys.argv[1])
run.instrument('repository.inspect')(lambda path: {'found':True})('package.json')
try:
    run.complete()
except ConnectionError:
    pass
run.close()
"""
    subprocess.run([sys.executable, "-c", script, str(path), sink.capture_identity], check=True)
    run = ObservedRun.resume(sink, path)
    run.flush()
    assert service.get_run(principal, "process-recovery")["status"] == "success"
    run.close()


def test_resume_unfinished_trace_preserves_failure_retry_and_order(service, principal, tmp_path):
    sink = DurableSink(service, principal)
    path = tmp_path / "unfinished.sqlite"
    run = ObservedRun(sink, "Inspect repository manifest", PROFILE, "team", spool_path=path)

    @run.instrument("repository.inspect")
    def fail():
        raise ValueError("password=never-store-me")

    with pytest.raises(ValueError):
        fail()
    run.close()
    resumed = ObservedRun.resume(sink, path)
    resumed.instrument("repository.inspect", retry_of="e1")(lambda: {"found": True})()
    resumed.complete()
    events = service.get_run(principal, resumed.run_token)["events"]
    assert [event["sequence"] for event in events] == [0, 1, 2, 3]
    assert events[2]["retry_of"] == "e1"
    assert "never-store-me" not in json.dumps(events)
    resumed.close()


def test_private_spool_identity_and_existing_file_guards(service, principal, tmp_path):
    sink = DurableSink(service, principal)
    path = tmp_path / "private.sqlite"
    run = ObservedRun(sink, "Inspect repository manifest", PROFILE, "team", spool_path=path)
    run.close()
    sink.capture_identity = "0" * 64
    with pytest.raises(ValueError, match="identity"):
        ObservedRun.resume(sink, path)
    with pytest.raises(FileExistsError):
        ObservedRun(sink, "New intent", PROFILE, "team", spool_path=path)
    path.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        ObservedRun.resume(sink, path)
    path.chmod(0o600)
    alias = tmp_path / "alias.sqlite"
    alias.symlink_to(path)
    with pytest.raises(OSError):
        ObservedRun.resume(sink, alias)


def test_client_spool_identity_binds_endpoint_and_credential():
    clients = [
        MuscleMemoryClient("http://localhost:8080", "first-test-key"),
        MuscleMemoryClient("http://localhost:8080/", "first-test-key"),
        MuscleMemoryClient("http://localhost:8081", "first-test-key"),
        MuscleMemoryClient("http://localhost:8080", "second-test-key"),
    ]
    try:
        assert clients[0].capture_identity == clients[1].capture_identity
        assert len({client.capture_identity for client in clients}) == 3
        assert "first-test-key" not in clients[0].capture_identity
    finally:
        for client in clients:
            client.close()


def test_large_events_split_uploads_by_bytes(service, principal):
    sink = DurableSink(service, principal)
    run = ObservedRun(sink, "Inspect repository manifest", PROFILE, "team")
    read = run.instrument("repository.inspect")(lambda: {"summary": "x" * 40000})
    for _ in range(35):
        read()
    run.complete()
    assert len(sink.batches) == 2
    assert all(len(json.dumps(batch)) < 1100000 for batch in sink.batches)
    assert len(service.get_run(principal, run.run_token)["events"]) == 37


def test_capture_limits_and_invalid_dependencies_prevent_tool_effects(
    service, principal, monkeypatch
):
    import musclememory.observer as observer

    monkeypatch.setattr(observer, "MAX_EVENTS", 4)
    run = ObservedRun(LocalSink(service, principal), "Inspect repository manifest", PROFILE, "team")
    effects = []
    read = run.instrument("repository.inspect")(lambda: effects.append(True))
    with pytest.raises(ValueError, match="dependencies"):
        run.instrument("repository.inspect", dependencies=["missing"])(
            lambda: effects.append(True)
        )()
    read()
    read()
    with pytest.raises(RuntimeError, match="limit"):
        read()
    run.complete()
    assert len(effects) == 2


async def test_async_completion_guard_and_cancellation_capture(service, principal):
    run = ObservedRun(LocalSink(service, principal), "Inspect repository manifest", PROFILE, "team")
    started = asyncio.Event()

    @run.instrument("repository.inspect")
    async def read():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(read())
    await started.wait()
    with pytest.raises(RuntimeError, match="active tool"):
        run.complete()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    run.complete(False)
    assert service.get_run(principal, run.run_token)["events"][1]["status"] == "failure"


def test_partially_rejected_upload_retains_journal(service, principal, tmp_path):
    sink = DurableSink(service, principal)
    path = tmp_path / "rejected.sqlite"
    run = ObservedRun(sink, "Inspect repository manifest", PROFILE, "team", spool_path=path)
    sink.musclememory_observe = lambda **kwargs: {
        "accepted_event_count": 0,
        "rejected_event_count": 1,
        "duplicate_event_count": 0,
    }
    with pytest.raises(DomainError, match="buffer retained"):
        run.flush()
    run.close()
    recovered = ObservedRun.resume(DurableSink(service, principal), path)
    assert len(recovered.pending) == 1
    recovered.flush()
    recovered.close()


def test_spool_does_not_persist_credential_shaped_identifiers(service, principal, tmp_path):
    sink = DurableSink(service, principal)
    path = tmp_path / "invalid.sqlite"
    with pytest.raises(ValueError):
        ObservedRun(sink, "Inspect", PROFILE, "team", "mm_" + "a" * 32, spool_path=path)
    assert not path.exists()


def test_capture_io_failure_preserves_original_tool_exception(service, principal, tmp_path):
    sink = DurableSink(service, principal)
    run = ObservedRun(sink, "Inspect", PROFILE, "team", spool_path=tmp_path / "io.sqlite")
    original = ValueError("tool failed")

    def broken_append(event):
        raise OSError("Simulated full disk")

    run._spool.append = broken_append

    @run.instrument("repository.inspect")
    def fail():
        raise original

    with pytest.raises(ValueError) as error:
        fail()
    assert error.value is original
    assert original.__notes__ == ["MuscleMemory failed to capture this failure"]
    run.close()


def test_omitted_non_json_arguments_cannot_be_compiled(service, principal):
    run = ObservedRun(LocalSink(service, principal), "Inspect", PROFILE, "team")
    value = object()
    assert run.instrument("repository.inspect")(lambda target: target)(value) is value
    run.complete()
    with pytest.raises(DomainError, match="fully captured JSON arguments"):
        service.learn(
            principal,
            LearnInput(
                run_token=run.run_token,
                completion_proof={
                    "succeeded": True,
                    "evidence_event_ids": ["e1"],
                    "summary": "Tool returned",
                },
            ),
        )
