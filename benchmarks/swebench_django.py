"""Real Django issues from SWE-bench Verified, checked the way SWE-bench checks them.

Each task is a real GitHub issue with the commit Django was at when it was reported,
the official fix, and hidden tests. The agent sees only the issue text. After a
session, the hidden test patch is applied to clean test files and the named tests are
run: every FAIL_TO_PASS test must now pass and every PASS_TO_PASS test must still pass.

Tasks run on one local Python 3.11 environment rather than SWE-bench's per-version
Docker images, so `validate` first runs every task with its official fix. Tests that
don't pass with the official fix here are reported and left out of the check; a task
whose FAIL_TO_PASS tests can't pass here is excluded.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# Where the Django clone, the Python environment and runs live (override with MM_BENCH_HOME).
HOME = Path(os.getenv("MM_BENCH_HOME", str(Path.home() / ".musclememory-bench"))).expanduser()
MIRROR = HOME / "django.git"
PYTHON = HOME / "py311/bin/python"
REQUIREMENTS = ["asgiref>=3.7,<4", "sqlparse>=0.3.1", "tzdata", "pytz"]


def prepare_environment():
    """A bare Django clone and a Python 3.11 environment with Django's test requirements."""
    HOME.mkdir(parents=True, exist_ok=True)
    if not MIRROR.exists():
        print("Cloning Django (about 300 MB, once)…", flush=True)
        subprocess.run(
            [
                "git",
                "clone",
                "--bare",
                "--quiet",
                "https://github.com/django/django.git",
                str(MIRROR),
            ],
            check=True,
        )
    if not PYTHON.exists():
        python = os.getenv("MM_BENCH_PYTHON") or shutil.which("python3.11")
        if not python:
            raise SystemExit("Python 3.11 is required: install it or set MM_BENCH_PYTHON.")
        subprocess.run([python, "-m", "venv", str(HOME / "py311")], check=True)
        subprocess.run([str(PYTHON), "-m", "pip", "install", "-q", *REQUIREMENTS], check=True)


DATASET = "https://datasets-server.huggingface.co/rows?dataset=princeton-nlp/SWE-bench_Verified"
STATUS = re.compile(
    r"^(.*?) \.\.\. (ok|FAIL|ERROR|skipped.*|expected failure|unexpected success)\s*$"
)


def fetch_tasks(ids, cache):
    """The named SWE-bench Verified rows, fetched once and cached."""
    cache = Path(cache)
    if cache.exists():
        rows = json.loads(cache.read_text())
    else:
        import urllib.request

        rows = []
        for offset in range(0, 500, 100):
            url = f"{DATASET}&config=default&split=test&offset={offset}&length=100"
            with urllib.request.urlopen(url, timeout=120) as response:
                rows += [item["row"] for item in json.load(response)["rows"]]
        rows = [row for row in rows if row["repo"] == "django/django"]
        cache.write_text(json.dumps(rows))
    found = {row["instance_id"]: row for row in rows}
    return [found[i] for i in ids]


def changed_files(patch):
    return re.findall(r"^diff --git a/(\S+) b/", patch, re.M)


def test_labels(test_patch):
    """Django test modules touched by the hidden tests, as runtests.py labels."""
    labels = []
    for path in changed_files(test_patch):
        if path.startswith("tests/") and path.endswith(".py"):
            labels.append(path[len("tests/") : -len(".py")].replace("/", "."))
    return sorted(set(labels))


def checkout(task, directory):
    """A clean working copy of Django at the issue's commit, as its own git repository."""
    directory = Path(directory)
    if directory.exists():
        shutil.rmtree(directory)
    subprocess.run(
        ["git", "clone", "--quiet", "--shared", "--no-checkout", str(MIRROR), str(directory)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(directory), "checkout", "--quiet", "--detach", task["base_commit"]],
        check=True,
    )
    # The agent's repository: no upstream history to dig a later fix out of.
    shutil.rmtree(directory / ".git")
    for command in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=bench", "-c", "user.email=bench@localhost", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", "-C", str(directory), *command], check=True)
    return directory


def apply(directory, patch, reverse=False):
    command = ["git", "-C", str(directory), "apply", "--whitespace=nowarn"]
    if reverse:
        command.append("-R")
    result = subprocess.run(command + ["-"], input=patch, text=True, capture_output=True)
    return result.returncode == 0, result.stderr


def run_tests(directory, labels, timeout=1800):
    result = subprocess.run(
        [str(PYTHON), "tests/runtests.py", "--settings=test_sqlite", "--parallel", "1", "-v", "2"]
        + labels,
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=timeout,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANG": "en_US.UTF-8",
            "PYTHONPATH": str(Path(directory).resolve()),  # the checkout's own django
        },
    )
    statuses = {}
    lines = (result.stdout + "\n" + result.stderr).splitlines()
    for index, line in enumerate(lines):
        match = STATUS.match(line)
        if not match:
            continue
        name, status = normal(match[1].strip()), match[2]
        statuses[name] = status
        # A test with a docstring prints its name on the line before the docstring.
        previous = lines[index - 1].strip() if index else ""
        if re.match(r"^\S+ \([\w.]+\)$", previous):
            statuses.setdefault(normal(previous), status)
    return statuses, result.stdout[-4000:] + result.stderr[-4000:]


def normal(name):
    """Python 3.11+ prints `test (module.Class.test)`; SWE-bench lists `test (module.Class)`."""
    match = re.match(r"^(\S+) \(([\w.]+)\.(\S+)\)$", name)
    if match and match[1] == match[3]:
        return f"{match[1]} ({match[2]})"
    return name


def passed(status):
    return status is not None and (status == "ok" or status.startswith("skipped"))


def judge(directory, task, usable_pass_to_pass=None):
    """SWE-bench style check of the working copy. Leaves the copy as the agent left it."""
    test_files = [f for f in changed_files(task["test_patch"]) if f.startswith("tests/")]
    work = Path(str(directory) + "-judge")
    if work.exists():
        shutil.rmtree(work)
    shutil.copytree(directory, work, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    base = Path(str(directory) + "-base")
    for path in test_files:  # the hidden tests replace whatever the agent did to them
        original = base / path
        target = work / path
        if original.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
        elif target.exists():
            target.unlink()
    subprocess.run(["git", "-C", str(work), "init", "-q"], check=True)
    ok, error = apply(work, task["test_patch"])
    if not ok:
        shutil.rmtree(work, ignore_errors=True)
        return {"resolved": False, "reason": "test patch did not apply", "error": error[-800:]}
    statuses, tail = run_tests(work, test_labels(task["test_patch"]))
    shutil.rmtree(work, ignore_errors=True)
    fail_to_pass = json.loads(task["FAIL_TO_PASS"])
    pass_to_pass = json.loads(task["PASS_TO_PASS"])
    if usable_pass_to_pass is not None:
        pass_to_pass = [name for name in pass_to_pass if name in usable_pass_to_pass]
    failed_f2p = [name for name in fail_to_pass if not passed(statuses.get(name))]
    failed_p2p = [name for name in pass_to_pass if not passed(statuses.get(name))]
    return {
        "resolved": not failed_f2p and not failed_p2p,
        "fail_to_pass": [len(fail_to_pass) - len(failed_f2p), len(fail_to_pass)],
        "pass_to_pass": [len(pass_to_pass) - len(failed_p2p), len(pass_to_pass)],
        "failed": (failed_f2p + failed_p2p)[:20],
        "failed_pass_to_pass": failed_p2p,
        "tail": tail[-1500:] if failed_f2p or failed_p2p else "",
    }


def prepare(task, directory):
    """Working copy for the agent, plus an untouched copy the judge restores tests from."""
    directory = checkout(task, directory)
    base = Path(str(directory) + "-base")
    if base.exists():
        shutil.rmtree(base)
    shutil.copytree(directory, base, symlinks=True, ignore=shutil.ignore_patterns(".git"))
    return directory


def validate(task, scratch):
    """Does the official fix resolve the task here, and does the unfixed code fail?"""
    directory = prepare(task, Path(scratch) / task["instance_id"])
    before = judge(directory, task)
    ok, error = apply(directory, task["patch"])
    if not ok:
        return {"instance_id": task["instance_id"], "usable": False, "reason": "fix did not apply"}
    after = judge(directory, task)
    usable_p2p = None
    if not after["resolved"] and after["fail_to_pass"][0] == after["fail_to_pass"][1]:
        # Only PASS_TO_PASS tests failed with the official fix: an environment difference.
        failing = set(after["failed_pass_to_pass"])
        usable_p2p = [n for n in json.loads(task["PASS_TO_PASS"]) if n not in failing]
        after = judge(directory, task, usable_p2p)
    return {
        "instance_id": task["instance_id"],
        "usable": after["resolved"] and not before["resolved"],
        "unfixed": before,
        "fixed": after,
        "usable_pass_to_pass": usable_p2p,
    }


GROUPS = {
    "model-forms": [
        "django__django-13933",
        "django__django-14725",
        "django__django-14915",
        "django__django-15916",
        "django__django-16950",
    ],
    "migration-operations": [
        "django__django-14999",
        "django__django-15268",
        "django__django-15499",
        "django__django-15695",
        "django__django-16819",
    ],
}


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["validate"])
    parser.add_argument("--group", choices=sorted(GROUPS), action="append", required=True)
    parser.add_argument("--out", type=Path, required=True)
    arguments = parser.parse_args()
    arguments.out.mkdir(parents=True, exist_ok=True)
    ids = [i for group in arguments.group for i in GROUPS[group]]
    tasks = fetch_tasks(ids, arguments.out / "tasks.json")
    report = arguments.out / "validation.jsonl"
    for task in tasks:
        result = validate(task, arguments.out / "validate")
        with report.open("a") as handle:
            handle.write(json.dumps(result) + "\n")
        fixed, unfixed = result.get("fixed", {}), result.get("unfixed", {})
        print(
            task["instance_id"],
            "usable" if result["usable"] else "NOT usable",
            "| unfixed resolved:",
            unfixed.get("resolved"),
            "| fixed f2p",
            fixed.get("fail_to_pass"),
            "p2p",
            fixed.get("pass_to_pass"),
            "| p2p dropped:",
            None
            if result.get("usable_pass_to_pass") is None
            else len(json.loads(task["PASS_TO_PASS"])) - len(result["usable_pass_to_pass"]),
            flush=True,
        )


if __name__ == "__main__":
    main()
