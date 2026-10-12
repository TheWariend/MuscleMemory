"""Capture a real read into a private journal; --resume only resends saved events."""

import argparse
import os
from pathlib import Path

import httpx
from musclememory.observer import ObservedRun
from musclememory.sdk import MuscleMemoryClient


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spool", type=Path, required=True)
    parser.add_argument("--resume", action="store_true")
    options = parser.parse_args()
    options.spool.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with MuscleMemoryClient(
        os.getenv("MM_API_URL", "http://127.0.0.1:8080"), os.environ["MM_API_KEY"]
    ) as client:
        if options.resume:
            run = ObservedRun.resume(client, options.spool)
        else:
            run = ObservedRun(
                client,
                "Inspect repository manifest",
                {
                    "agent_name": "durable-python-agent",
                    "bindings": {"repository.inspect": "read_file"},
                },
                os.getenv("MM_NAMESPACE", "default"),
                spool_path=options.spool,
            )
        try:
            if options.resume:
                run.flush()
            else:

                @run.instrument("repository.inspect")
                def read_file(path):
                    content = Path(path).read_text()
                    if "[project]" not in content:
                        raise ValueError("Manifest has no project section")
                    return {"has_project": True}

                read_file(str(Path(__file__).resolve().parents[1] / "pyproject.toml"))
                run.complete()
            print(
                f"Uploaded trace {run.run_token}; closed={run.closed}; pending={len(run.pending)}"
            )
        except httpx.HTTPError:
            print(
                "Upload failed; sanitized journal retained. "
                "Retry with --resume and the same client credential."
            )
            raise SystemExit(1) from None
        finally:
            run.close()


if __name__ == "__main__":
    main()
