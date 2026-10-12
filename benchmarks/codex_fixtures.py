"""Multi-file, three-defect coding tasks and an independent behavioral verifier.

Fixtures are authored here, not copied from an upstream benchmark. The same
broken source is used for each arm; held-out examples and prompts differ from
the training examples. This measures repeated workflows, not unseen bug types.
"""

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

FAMILIES = ("exports", "configuration", "pagination")
SOURCES = {
    "money.py": """
from decimal import Decimal, ROUND_HALF_UP

def cents(value):
    return int((Decimal(str(value)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
""",
    "exports.py": """
import csv
import io
from parcelkit.money import cents

def summarize(rows):
    seen, totals = set(), {}
    for row in rows:
        identity = (row["account"], row["id"])
        if identity in seen:
            continue
        seen.add(identity)
        amount = cents(row["amount"])
        if row["kind"] == "refund":
            amount = -amount
        totals[row["account"]] = totals.get(row["account"], 0) + amount
    return totals

def export_csv(rows):
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["account", "net_cents"])
    for account, total in sorted(summarize(rows).items()):
        writer.writerow([account, total])
    return output.getvalue()
""",
    "settings.py": """
def merge(base, overrides):
    result = dict(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = value
    return result

def environment_values(environment):
    result = {}
    if "PARCEL_TIMEOUT" in environment:
        result["timeout"] = int(environment["PARCEL_TIMEOUT"])
    if "PARCEL_ENABLED" in environment:
        value = environment["PARCEL_ENABLED"].lower()
        if value not in {"true", "false"}:
            raise ValueError("PARCEL_ENABLED must be true or false")
        result["enabled"] = value == "true"
    return result
""",
    "configuration.py": """
from parcelkit.settings import merge, environment_values

DEFAULTS = {"timeout": 30, "enabled": True, "service": {"host": "localhost", "port": 8000}}

def load_config(file_values, environment, cli_values):
    result = merge(DEFAULTS, file_values)
    result = merge(result, environment_values(environment))
    return merge(result, cli_values)
""",
    "cursors.py": """
import base64
import json

def encode_cursor(row):
    body = json.dumps([row["created_at"], row["id"]], separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(body).decode()

def decode_cursor(cursor):
    return tuple(json.loads(base64.urlsafe_b64decode(cursor).decode()))
""",
    "pagination.py": """
from parcelkit.cursors import encode_cursor, decode_cursor

def page(rows, limit=20, cursor=None):
    if limit < 1:
        raise ValueError("limit must be positive")
    ordered = sorted(rows, key=lambda row: (row["created_at"], row["id"]))
    if cursor is not None:
        boundary = decode_cursor(cursor)
        ordered = [row for row in ordered if (row["created_at"], row["id"]) > boundary]
    selected = ordered[:limit]
    next_cursor = encode_cursor(selected[-1]) if len(ordered) > limit else None
    return {"items": selected, "next_cursor": next_cursor}
""",
    "cli.py": """
import argparse
import json
from parcelkit.exports import export_csv
from parcelkit.configuration import load_config
from parcelkit.pagination import page

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["export", "config", "page"])
    parser.add_argument("input")
    args = parser.parse_args()
    with open(args.input) as source:
        payload = json.load(source)
    if args.operation == "export":
        print(export_csv(payload), end="")
    elif args.operation == "config":
        print(json.dumps(load_config(**payload), sort_keys=True))
    else:
        print(json.dumps(page(**payload), sort_keys=True))

if __name__ == "__main__":
    main()
""",
}

PROMPTS = {
    "exports": [
        "Fix the financial CSV export. Duplicate webhook deliveries are double-counted, "
        "refunds increase the net balance, and half-cent amounts round incorrectly. "
        "Preserve the CSV contract and CLI. Find and fix all three defects.",
        "Customers report wrong net_cents in exported statements: retries inflate totals, "
        "refunds have the wrong sign, and amounts ending in .005 are truncated. "
        "Repair the export pipeline and keep its existing public interface.",
        "Correct the statement generator: treat repeated event IDs per account once, "
        "decrease balances for refunds, and round monetary half-cents away from zero. "
        "Keep the CSV columns and command-line behavior intact.",
    ],
    "configuration": [
        "Fix configuration loading. CLI values should beat environment variables and the "
        "file; explicit false and zero overrides must survive; a nested service override "
        "must preserve sibling defaults. Repair all three problems without changing the CLI.",
        "Repair settings resolution: command-line flags lose to environment values, "
        "zero/false values vanish, and changing only service.port drops service.host. "
        "Keep the API and reject invalid boolean environment values as before.",
        "The effective configuration is wrong. Fix priority so CLI wins over environment "
        "and files, retain explicitly disabled and zero-valued settings, and merge nested "
        "overrides without losing defaults. Preserve the public entry points.",
    ],
    "pagination": [
        "Fix cursor pagination. The ordering must be stable by (created_at, id), cursor "
        "continuation must exclude previously returned rows including timestamp ties, "
        "and zero/negative limits must fail. Preserve the existing page API and cursor format.",
        "Repair the listing endpoint: equal timestamps cause unstable results, continuing "
        "a cursor repeats entries, and invalid limits are accepted. Ensure every item is "
        "returned exactly once across pages and retain the public API and encoded cursors.",
        "Repair continuation through the collection: rows with the same timestamp need "
        "a deterministic ID tie-break, next pages must exclude earlier items, and limits "
        "below one must raise. Keep the cursor representation and API compatible.",
    ],
}

PUBLIC_TESTS = """
import csv
import io
import unittest
from parcelkit.exports import export_csv
from parcelkit.configuration import load_config
from parcelkit.pagination import page

class Contracts(unittest.TestCase):
    def test_statement(self):
        rows = [
            {"account": "North, LTD", "id": "a", "kind": "sale", "amount": "1.005"},
            {"account": "North, LTD", "id": "a", "kind": "sale", "amount": "1.005"},
            {"account": "North, LTD", "id": "b", "kind": "refund", "amount": "0.50"},
        ]
        self.assertEqual(list(csv.reader(io.StringIO(export_csv(rows)))),
                         [["account", "net_cents"], ["North, LTD", "51"]])

    def test_config(self):
        config = load_config({"timeout": 10, "service": {"port": 9000}},
                             {"PARCEL_TIMEOUT": "7"}, {"timeout": 0, "enabled": False})
        self.assertEqual(config, {"timeout": 0, "enabled": False,
                                  "service": {"host": "localhost", "port": 9000}})

    def test_pages(self):
        rows = [{"created_at": "2026-01-01", "id": identity} for identity in ["c", "a", "b"]]
        first = page(rows, limit=2)
        second = page(rows, limit=2, cursor=first["next_cursor"])
        self.assertEqual([row["id"] for row in first["items"] + second["items"]], ["a", "b", "c"])
        self.assertIsNone(second["next_cursor"])
        with self.assertRaises(ValueError):
            page(rows, limit=0)

if __name__ == "__main__":
    unittest.main()
"""


def fixture_files(family, variant):
    sources = dict(SOURCES)
    if family == "exports":
        sources["money.py"] = sources["money.py"].replace(
            "return int((Decimal(str(value)) * 100).quantize("
            'Decimal("1"), rounding=ROUND_HALF_UP))',
            "return int(float(value) * 100)",
        )
        sources["exports.py"] = (
            sources["exports.py"]
            .replace(
                "        if identity in seen:\n            continue\n        seen.add(identity)\n",
                "",
            )
            .replace("amount = -amount", "amount = abs(amount)")
        )
    elif family == "configuration":
        sources["settings.py"] = sources["settings.py"].replace(
            "    for key, value in overrides.items():\n"
            "        if isinstance(value, dict) and isinstance(result.get(key), dict):\n"
            "            result[key] = merge(result[key], value)\n"
            "        else:\n"
            "            result[key] = value\n",
            "    result.update(overrides)\n",
        )
        sources["configuration.py"] = sources["configuration.py"].replace(
            "result = merge(result, environment_values(environment))\n"
            "    return merge(result, cli_values)",
            "result = merge(result, {key: value for key, value in cli_values.items() if value})\n"
            "    return merge(result, environment_values(environment))",
        )
    elif family == "pagination":
        sources["pagination.py"] = (
            sources["pagination.py"]
            .replace("if limit < 1:", "if limit < 0:")
            .replace(
                'key=lambda row: (row["created_at"], row["id"])',
                'key=lambda row: row["created_at"]',
            )
        )
        sources["cursors.py"] = sources["cursors.py"].replace(
            'json.dumps([row["created_at"], row["id"]],', 'json.dumps([row["created_at"], ""],'
        )
    else:
        raise ValueError(family)
    files = {
        "parcelkit/" + name: textwrap.dedent(value).lstrip() for name, value in sources.items()
    }
    files["parcelkit/__init__.py"] = '"""ParcelKit local operations library."""\n'
    files["tests/test_contracts.py"] = textwrap.dedent(PUBLIC_TESTS).lstrip()
    files[".gitignore"] = "__pycache__/\n*.pyc\n"
    files["README.md"] = (
        "# ParcelKit\n\nA dependency-free Python operations package with financial CSV exports, "
        "layered configuration, and cursor pagination.\n\n"
        "Run checks: `python3 -m unittest discover -s tests -v`.\n"
        "Run the CLI: `python3 -m parcelkit.cli {export,config,page} input.json`.\n\n"
        "Exports deduplicate on (account,id), round each amount to cents with HALF_UP, "
        "subtract refunds, sort accounts, and use standard CSV quoting.\n"
        "Settings precedence is defaults < file < environment < CLI; merge nested objects.\n"
        "Pages order by (created_at,id), require a positive limit, and use exclusive cursors.\n"
    )
    files["AGENTS.md"] = (
        "Make focused fixes in parcelkit/ only. Preserve public interfaces. Do not change "
        "tests, documentation, or inputs. Run the tests. Use serial tool calls for the "
        "execution audit. Do not use the network or install dependencies. "
        "This project uses unittest; use python3. Keep your final response brief.\n"
    )
    account = f"Account {variant}, LLC"
    files["input.json"] = json.dumps({"variant": variant, "account": account}) + "\n"
    if variant >= 200:
        package = ("ledgerops", "relayops", "batchops")[variant % 3]
        files = {
            name.replace("parcelkit/", package + "/"): content.replace("parcelkit", package)
            for name, content in files.items()
        }
    return files


def prepare(directory, family, variant):
    directory.mkdir(parents=True)
    files = fixture_files(family, variant)
    for name, content in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q", str(directory)], check=True)
    subprocess.run(["git", "-C", str(directory), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(directory),
            "-c",
            "user.name=Evaluation Harness",
            "-c",
            "user.email=evaluation@localhost",
            "commit",
            "-qm",
            "Fixture baseline",
        ],
        check=True,
    )
    return {name: hashlib.sha256(content.encode()).hexdigest() for name, content in files.items()}


# This verifier executes in a separate process, from the immutable harness
# outside the agent workspace. Randomized cases use fixed, disjoint seeds.
VERIFIER = r"""
import copy, csv, io, json, random, sys
from decimal import Decimal, ROUND_HALF_UP
sys.path.insert(0, sys.argv[1])
from parcelkit.exports import export_csv
from parcelkit.configuration import load_config
from parcelkit.pagination import page
from parcelkit.cursors import decode_cursor
random.seed(int(sys.argv[2]))
checks = 0
for trial in range(30):
    rows, seen, expected = [], set(), {}
    for index in range(20):
        row = {"account": random.choice(["Alpha, LTD", 'Beta "Co"', "Gamma\nLLC"]),
               "id": str(random.randrange(8)), "kind": random.choice(["sale", "refund"]),
               "amount": str(Decimal(random.randrange(1, 5000)) / 1000)}
        rows.append(row)
        identity = (row["account"], row["id"])
        if identity in seen: continue
        seen.add(identity)
        amount = int((Decimal(row["amount"])*100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        expected[row["account"]] = expected.get(row["account"], 0) + (
            -amount if row["kind"] == "refund" else amount)
    original = copy.deepcopy(rows)
    assert list(csv.reader(io.StringIO(export_csv(rows)))) == (
        [["account", "net_cents"]] + [[k, str(v)] for k,v in sorted(expected.items())])
    assert rows == original
    checks += 2
for trial in range(30):
    file = {"timeout": random.randrange(1,100), "service": {"port": random.randrange(8000,9000)}}
    env = {"PARCEL_TIMEOUT": str(random.randrange(1,100)), "PARCEL_ENABLED": "true"}
    cli = {"timeout": random.choice([0, 1, 25]), "enabled": False}
    original = copy.deepcopy([file,env,cli])
    result = load_config(file,env,cli)
    assert result == {"timeout": cli["timeout"], "enabled":False,
                      "service":{"host":"localhost","port":file["service"]["port"]}}
    assert [file,env,cli] == original
    checks += 2
assert load_config({}, {"PARCEL_ENABLED":"false"}, {})["enabled"] is False
assert load_config({}, {}, {})["timeout"] == 30
try: load_config({}, {"PARCEL_ENABLED":"perhaps"}, {})
except ValueError: pass
else: raise AssertionError("Invalid booleans must fail")
checks += 3
for trial in range(30):
    rows = [{"created_at": "2026-01-%02d"%random.randrange(1,4), "id":"item-%03d"%i}
            for i in range(random.randrange(5,30))]
    random.shuffle(rows)
    original = copy.deepcopy(rows)
    expected = sorted(rows,key=lambda row:(row["created_at"],row["id"]))
    cursor, collected = None, []
    for _ in range(len(rows)+1):
        result = page(rows,limit=random.randrange(1,6),cursor=cursor)
        collected.extend(result["items"])
        cursor = result["next_cursor"]
        if cursor is None: break
        assert decode_cursor(cursor) == (
            result["items"][-1]["created_at"],result["items"][-1]["id"])
    assert collected == expected
    assert rows == original
    checks += 2
for invalid in [0,-1,-20]:
    try: page([],limit=invalid)
    except ValueError: pass
    else: raise AssertionError("Invalid page limit accepted")
assert page([],limit=1) == {"items":[],"next_cursor":None}
checks += 4
print(json.dumps({"passed":True,"behavioral_checks":checks}))
"""


def verify(directory: Path, original, seed):
    package = next(name.split("/")[0] for name in original if name.endswith("/__init__.py"))
    permitted = {package + "/" + name for name in SOURCES}
    protected = {
        name: hashlib.sha256((directory / name).read_bytes()).hexdigest()
        for name in original
        if name not in permitted
    }
    unchanged = all(protected[name] == original[name] for name in protected)
    result = subprocess.run(
        [sys.executable, "-c", VERIFIER.replace("parcelkit", package), str(directory), str(seed)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    public = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
        cwd=directory,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return {
        "passed": result.returncode == 0 and public.returncode == 0 and unchanged,
        "behavioral_checks": json.loads(result.stdout)["behavioral_checks"]
        if result.returncode == 0
        else 0,
        "protected_files_unchanged": unchanged,
        "external_stdout": result.stdout,
        "external_stderr": result.stderr,
        "public_stdout": public.stdout,
        "public_stderr": public.stderr,
    }
