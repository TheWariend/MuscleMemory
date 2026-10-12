"""Automatic tool instrumentation for serial Python agent executions.

Capture is explicit at the tool boundary; it does not observe unrelated client activity.
Events remain in a sanitized bounded buffer until flush succeeds. An optional
private SQLite journal persists events across process restarts.
"""

import asyncio
import inspect
import json
import threading
import time
from functools import wraps
from uuid import uuid4

from .core import DomainError, ExecutionEvent, ObserveInput, RuntimeProfile
from .security import redact
from .spool import CaptureSpool

MAX_EVENTS = 1024
BATCH_BYTES = 1024 * 1024


def safe_json(value):
    try:
        json.dumps(value, allow_nan=False)
        return redact(value)
    except (TypeError, ValueError, RecursionError, DomainError):
        return {
            "capture_note": "Non-JSON response omitted",
            "type": type(value).__name__,
            "$mm_capture_omitted": True,
        }


class ObservedRun:
    def __init__(
        self,
        client,
        intent_text,
        runtime_profile,
        visibility_scope,
        run_token=None,
        *,
        spool_path=None,
    ):
        self.client = client
        self.run_token = run_token or str(uuid4())
        self.profile = RuntimeProfile.model_validate(
            redact(RuntimeProfile.model_validate(runtime_profile).model_dump(mode="json"))
        )
        self.scope = visibility_scope
        self.events = []
        self.pending = []
        self._lock = threading.RLock()
        self._async_lock = asyncio.Lock()
        self.closed = False
        self._disposed = False
        self._active = False
        self._spool = None
        metadata = redact(
            {
                "run_token": self.run_token,
                "runtime_profile": self.profile.model_dump(mode="json"),
                "visibility_scope": self.scope,
            }
        )
        ObserveInput(**metadata, event_batch=[{"kind": "intent"}])
        # Validate the initial event before creating a durable journal.
        ExecutionEvent(event_id="e0", sequence=0, kind="intent", intent_text=intent_text)
        if len(json.dumps(metadata)) > 65536:
            raise ValueError("Capture metadata exceeds 64 KiB")
        if spool_path is not None:
            self._spool = CaptureSpool(
                spool_path, getattr(client, "capture_identity", None), metadata
            )
        try:
            self._emit(kind="intent", intent_text=intent_text)
        except BaseException:
            self.close()
            raise

    @classmethod
    def resume(cls, client, spool_path):
        """Load a sanitized journal. Resume requires the same endpoint/credential."""
        spool = CaptureSpool(spool_path, getattr(client, "capture_identity", None))
        run = cls.__new__(cls)
        run.client, run._spool = client, spool
        run.run_token = spool.metadata["run_token"]
        run.profile = RuntimeProfile.model_validate(spool.metadata["runtime_profile"])
        run.scope = spool.metadata["visibility_scope"]
        run.events, run.pending = spool.events, spool.pending
        run.closed, run._disposed = spool.closed, False
        run._active = False
        run._lock, run._async_lock = threading.RLock(), asyncio.Lock()
        return run

    def close(self):
        """Release local resources; does not complete, upload, or delete a trace."""
        with self._lock:
            if self._active:
                raise RuntimeError("Await the active tool before closing capture resources")
            if not self._disposed and self._spool:
                self._spool.close()
            self._disposed = True

    def _ensure_open(self, *, completion=False):
        if self._disposed:
            raise RuntimeError("Capture resources are closed")
        if self.closed:
            raise RuntimeError("Execution trace is closed")
        # Reserve the final event for completion before allowing a tool side effect.
        if len(self.events) >= MAX_EVENTS - (0 if completion else 1):
            raise RuntimeError("Trace capture limit reached")

    def _emit(self, **fields):
        self._ensure_open(completion=fields.get("kind") == "completion")
        event = ExecutionEvent(event_id=f"e{len(self.events)}", sequence=len(self.events), **fields)
        sanitized = redact(event.model_dump(mode="json"))
        if len(json.dumps(sanitized)) > 65536 and event.kind == "tool":
            sanitized["response"] = {
                "capture_note": "Response omitted to fit event limit",
                "$mm_capture_omitted": True,
            }
        if len(json.dumps(sanitized)) > 65536:
            raise DomainError("capture_size", "Captured event exceeds 64 KiB", 422)
        if self._spool:
            self._spool.append(sanitized)
        self.events.append(sanitized)
        self.pending.append(sanitized)
        return event.event_id

    def instrument(
        self,
        capability,
        *,
        dependencies=None,
        retry_of=None,
        exploratory=False,
        permission=None,
        effect="read",
        condition=None,
    ):
        """Wrap a sync or async tool; preserve return values and exceptions."""
        tool = self.profile.bindings.get(capability)
        if not tool:
            raise ValueError("Bind the tool's canonical capability in runtime_profile first")

        def decorator(function):
            def prepare(args, kwargs):
                self._ensure_open()
                if self._active:
                    raise RuntimeError("Serial capture cannot overlap or nest tool calls")
                arguments = safe_json(arguments_of(args, kwargs))
                fields = dict(
                    kind="tool",
                    tool_name=tool,
                    capability=capability,
                    arguments=arguments,
                    dependencies=dependencies or [],
                    retry_of=retry_of,
                    permission=permission,
                    effect=effect,
                    condition=condition,
                )
                event = ExecutionEvent(
                    event_id=f"e{len(self.events)}", sequence=len(self.events), **fields
                )
                clean_event = redact(event.model_dump(mode="json"))
                ExecutionEvent.model_validate(clean_event)
                if len(json.dumps(clean_event)) > 65536:
                    raise DomainError("capture_size", "Tool arguments exceed capture limit", 422)
                if any(
                    dep not in {e["event_id"] for e in self.events} for dep in event.dependencies
                ):
                    raise ValueError("Capture dependencies must reference earlier events")
                if event.retry_of and not any(
                    e["event_id"] == event.retry_of and e["status"] == "failure"
                    for e in self.events
                ):
                    raise ValueError("Capture retry must reference an earlier failure")
                if permission and permission not in self.profile.permissions:
                    raise ValueError("Capture permission is absent from runtime profile")
                self._active = True
                return arguments

            def capture(arguments, response, status, duration):
                self._emit(
                    kind="tool",
                    tool_name=tool,
                    capability=capability,
                    arguments=arguments,
                    response=safe_json(response),
                    status=status,
                    duration_ms=duration,
                    dependencies=dependencies or [],
                    retry_of=retry_of,
                    exploratory=exploratory,
                    permission=permission,
                    effect=effect,
                    condition=condition,
                )

            def arguments_of(args, kwargs):
                bound = inspect.signature(function).bind(*args, **kwargs)
                bound.apply_defaults()
                return dict(bound.arguments)

            if inspect.iscoroutinefunction(function):

                @wraps(function)
                async def async_wrapper(*args, **kwargs):
                    async with self._async_lock:
                        with self._lock:
                            arguments = prepare(args, kwargs)
                        start = time.perf_counter()
                        try:
                            try:
                                result = await function(*args, **kwargs)
                            except BaseException as error:
                                with self._lock:
                                    try:
                                        capture(
                                            arguments,
                                            {
                                                "error_type": type(error).__name__,
                                                "message": str(error),
                                            },
                                            "failure",
                                            (time.perf_counter() - start) * 1000,
                                        )
                                    except Exception:
                                        error.add_note(
                                            "MuscleMemory failed to capture this failure"
                                        )
                                raise
                            with self._lock:
                                capture(
                                    arguments,
                                    result,
                                    "success",
                                    (time.perf_counter() - start) * 1000,
                                )
                            return result
                        finally:
                            with self._lock:
                                self._active = False

                return async_wrapper

            @wraps(function)
            def wrapper(*args, **kwargs):
                with self._lock:
                    arguments = prepare(args, kwargs)
                    start = time.perf_counter()
                    try:
                        try:
                            result = function(*args, **kwargs)
                        except BaseException as error:
                            try:
                                capture(
                                    arguments,
                                    {"error_type": type(error).__name__, "message": str(error)},
                                    "failure",
                                    (time.perf_counter() - start) * 1000,
                                )
                            except Exception:
                                error.add_note("MuscleMemory failed to capture this failure")
                            raise
                        capture(arguments, result, "success", (time.perf_counter() - start) * 1000)
                        return result
                    finally:
                        self._active = False

            return wrapper

        return decorator

    def complete(self, succeeded=True):
        with self._lock:
            if self._active:
                raise RuntimeError("Await the active tool before completing the trace")
            self._emit(kind="completion", status="success" if succeeded else "failure")
            self.closed = True
        return self.flush()

    def flush(self):
        with self._lock:
            if self._disposed:
                raise RuntimeError("Capture resources are closed")
            if self._active:
                raise RuntimeError("Await the active tool before flushing the trace")
            result = {
                "accepted_event_count": 0,
                "rejected_event_count": 0,
                "duplicate_event_count": 0,
            }
            while self.pending:
                batch, size = [], 0
                for event in self.pending[:256]:
                    event_size = len(json.dumps(event))
                    if batch and size + event_size > BATCH_BYTES:
                        break
                    batch.append(event)
                    size += event_size
                result = self.client.musclememory_observe(
                    run_token=self.run_token,
                    event_batch=batch,
                    runtime_profile=self.profile.model_dump(mode="json"),
                    visibility_scope=self.scope,
                )
                accepted, duplicates = (
                    result.get("accepted_event_count"),
                    result.get("duplicate_event_count"),
                )
                if (
                    result.get("rejected_event_count") != 0
                    or type(accepted) is not int
                    or type(duplicates) is not int
                    or accepted < 0
                    or duplicates < 0
                    or accepted + duplicates != len(batch)
                ):
                    raise DomainError(
                        "capture_rejected", "Server rejected captured events; buffer retained"
                    )
                if self._spool:
                    self._spool.acknowledge(batch)
                del self.pending[: len(batch)]
            return result

    def measurements(self):
        tools = [event for event in self.events if event["kind"] == "tool"]
        return {
            "tool_calls": len(tools),
            "duration_ms": sum(e["duration_ms"] for e in tools),
            "tokens": sum(e["token_usage"] for e in tools)
            if tools and all(e["token_usage"] is not None for e in tools)
            else None,
        }
