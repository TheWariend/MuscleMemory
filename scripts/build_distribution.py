"""Build a wheel containing the current dashboard and database migrations."""

import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    arguments = parser.parse_args()
    output = arguments.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(["npm", "run", "build"], cwd=ROOT, check=True)
    with tempfile.TemporaryDirectory(prefix="musclememory-build-") as directory:
        source = Path(directory)
        for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):
            shutil.copyfile(ROOT / name, source / name)
        package = source / "packages/musclememory"
        shutil.copytree(
            ROOT / "packages/musclememory",
            package,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "static"),
        )
        shutil.copytree(ROOT / "apps/dashboard/dist", package / "static")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "wheel",
                "--no-deps",
                "--no-build-isolation",
                "--wheel-dir",
                str(output),
                str(source),
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
