The Python SDK ships in the main distribution to keep its contracts synchronized.

```python
from musclememory.sdk import MuscleMemoryClient
from musclememory.observer import ObservedRun
```

See `examples/learning_workflow.py` for an executable capture, learn, evaluate, recall,
and cross-agent adaptation example. Use a scoped bearer credential, TLS remotely,
and the client as a context manager. Capture wrappers preserve tool return values
and exceptions, redact before buffering, and retry identical event batches safely.

Instrumentation supports serial sync or serial async tools. Do not mix concurrent
sync and async calls in one trace. `flush()` uses the synchronous REST client; move
it to `asyncio.to_thread` when calling from an async application. Token and cost
capture require upstream usage instrumentation; absent values remain unknown.

Pass `spool_path` for private durable capture and use `ObservedRun.resume()` after
restart. Recovery only resends saved events; it does not repeat tools. See
[capture details](../../docs/integrations.md#capture-details) and `examples/durable_capture.py`
for identity binding, retention, and storage-failure boundaries.
