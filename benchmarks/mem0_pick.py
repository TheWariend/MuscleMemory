"""Naive memory's skill choice, made by the open-source Mem0 library (local, no API calls).

Run it from its own environment, kept apart from MuscleMemory's:

    python3 -m venv .mem0 && .mem0/bin/pip install mem0ai==2.2.1 sentence-transformers
    python benchmarks/trust_experiment.py ... --equal-naive \
        --picker ".mem0/bin/python benchmarks/mem0_pick.py" run

    mem0_pick.py STORE add   < [{"procedure": token, "text": guidance}, ...]   stores skills
    mem0_pick.py STORE search "task prompt"                     prints skills ranked by Mem0
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault(
    "OPENAI_API_KEY", "unused-infer-is-off"
)  # Mem0 builds a client it never calls
# The embedding model is already downloaded; never ask the network about it mid-run.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ["MEM0_TELEMETRY"] = "False"  # no usage events leave this machine
from mem0 import Memory  # noqa: E402

store = Path(sys.argv[1]).resolve()
store.mkdir(parents=True, exist_ok=True)
memory = Memory.from_config({
    "vector_store": {"provider": "qdrant", "config": {
        "path": str(store / "qdrant"), "collection_name": "skills", "embedding_model_dims": 384,
        "on_disk": True}},
    "embedder": {"provider": "huggingface",
                 "config": {"model": "sentence-transformers/all-MiniLM-L6-v2"}},
    "history_db_path": str(store / "history.db"),
})  # fmt: skip
seen_file = store / "seen.json"
seen = set(json.loads(seen_file.read_text())) if seen_file.exists() else set()
if sys.argv[2] == "add":
    for item in json.load(sys.stdin):
        if item["procedure"] in seen:
            continue
        memory.add(item["text"], user_id="naive", metadata={"procedure": item["procedure"]},
                   infer=False)  # fmt: skip
        seen.add(item["procedure"])
    seen_file.write_text(json.dumps(sorted(seen)))
    print(len(seen))
else:
    found = memory.search(sys.argv[3], filters={"user_id": "naive"}, limit=3)
    results = found["results"] if isinstance(found, dict) else found
    print(json.dumps([{"procedure": r["metadata"]["procedure"], "score": r.get("score")}
                      for r in results]))  # fmt: skip
