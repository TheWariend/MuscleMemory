"""Synchronous Python SDK. It sends the same named v1 contracts as MCP."""

import hashlib

import httpx
from pydantic import TypeAdapter

from .core import (
    CreateRunInput,
    EvaluateInput,
    InspectInput,
    LearnInput,
    MemoryLookupInput,
    ObserveInput,
    RecallInput,
    SessionUsageInput,
    Token,
)
from .security import redact


class MuscleMemoryClient:
    def __init__(self, base_url, api_key, *, timeout=30, transport=None):
        self.http = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )
        # Bind a durable spool to this endpoint and credential without storing either secret.
        self.capture_identity = hashlib.sha256(
            (str(self.http.base_url) + "\0" + api_key).encode()
        ).hexdigest()

    def close(self):
        self.http.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _post(self, path, contract, *, headers=None):
        response = self.http.post(
            path, json=redact(contract.model_dump(mode="json")), headers=headers
        )
        response.raise_for_status()
        return response.json()

    def create_run(self, **arguments):
        return self._post("/v1/runs", CreateRunInput(**arguments))

    def musclememory_observe(self, **arguments):
        return self._post("/v1/observe", ObserveInput(**arguments))

    def musclememory_learn(self, **arguments):
        return self._post("/v1/learn", LearnInput(**arguments))

    def musclememory_recall(self, *, observation_run=None, **arguments):
        headers = (
            {"X-MuscleMemory-Run": TypeAdapter(Token).validate_python(observation_run)}
            if observation_run
            else None
        )
        return self._post("/v1/recall", RecallInput(**arguments), headers=headers)

    def musclememory_evaluate(self, **arguments):
        return self._post("/v1/evaluate", EvaluateInput(**arguments))

    def musclememory_inspect(self, **arguments):
        return self._post("/v1/inspect", InspectInput(**arguments))

    def auto_learn(self, run_token, hint=None):
        """After a session whose tests passed: count it as a check or learn a new skill."""
        response = self.http.post(
            f"/v1/runs/{run_token}/auto-learn",
            params={"hint": hint} if hint else None,
            json={},
            timeout=8,  # Durable queue acknowledgement; native hooks allow 10 seconds.
        )
        response.raise_for_status()
        return response.json()

    def approve(self, procedure_token):
        """A person's OK for a skill. Needs an admin key, like the dashboard's."""
        response = self.http.post(f"/v1/procedures/{procedure_token}/approve", json={})
        response.raise_for_status()
        return response.json()

    def auto_flag(self, run_token, hint):
        """After a session whose skill's own test failed last: a failed check for that skill."""
        response = self.http.post(
            f"/v1/runs/{run_token}/auto-flag", params={"hint": hint}, json={}, timeout=8
        )
        response.raise_for_status()
        return response.json()

    def record_session_usage(self, run_token, **arguments):
        return self._post(f"/v1/runs/{run_token}/usage", SessionUsageInput(**arguments))

    def record_memory_lookup(self, run_token, **arguments):
        return self._post(f"/v1/runs/{run_token}/memory", MemoryLookupInput(**arguments))
