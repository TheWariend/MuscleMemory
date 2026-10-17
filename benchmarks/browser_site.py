"""Local service portal. The submission judge is reachable only by the evaluator.

Run: python benchmarks/browser_site.py --port 8765
The runner registers cases directly; a browser sees only the home URL for its case.
"""

import argparse
import html
import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

try:
    from .browser_tasks import MUTATIONS, TASKS, Task, judge, render
except ImportError:
    from browser_tasks import MUTATIONS, TASKS, Task, judge, render

SECTIONS = {
    "equipment": "Equipment services",
    "workspace": "Workplace services",
    "training": "Learning services",
}


@dataclass
class Case:
    task: Task
    seed: int
    variant: str
    submissions: list = field(default_factory=list)


class Portal:
    def __init__(self, port=0, *, records=None):
        self.cases, self.lock = {}, threading.Lock()
        self.records = Path(records) if records else None
        if self.records and self.records.exists():
            for line in self.records.read_text().splitlines():
                event = json.loads(line)
                if event["type"] == "case":
                    self.cases[event["case"]] = Case(
                        next(t for t in TASKS if t.key == event["form"]),
                        event["seed"],
                        event["variant"],
                    )
                else:
                    self.cases[event["case"]].submissions.append(event["payload"])
        portal = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def reply(self, status, body, kind="text/html"):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", kind + "; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                url = urlsplit(self.path)
                case_id = parse_qs(url.query).get("case", [""])[0]
                case = portal.cases.get(case_id)
                if not case:
                    return self.reply(404, "Unknown case")
                if url.path == "/judge":
                    return self.reply(200, json.dumps(portal.verdict(case_id)), "application/json")
                query = "?" + urlencode({"case": case_id})

                def link(path, title):
                    return f'<p><a href="{html.escape(path + query)}">{html.escape(title)}</a></p>'

                if url.path == "/":
                    body = "<h1>Service portal</h1>" + link("/requests", "Requests")
                elif url.path == "/requests":
                    keys = list(SECTIONS)
                    if case.variant == "moved":
                        keys.reverse()
                    body = "<h1>Requests</h1>" + "".join(
                        link("/requests/" + key, SECTIONS[key]) for key in keys
                    )
                elif url.path in ["/requests/" + key for key in SECTIONS]:
                    key = url.path.rsplit("/", 1)[-1]
                    task = next(t for t in TASKS if t.key == key)
                    body = f"<h1>{SECTIONS[key]}</h1>" + link("/forms/" + key, task.title)
                elif url.path == "/forms/" + case.task.key:
                    return self.reply(200, render(case.task, case.seed, case.variant, case_id))
                else:
                    return self.reply(404, "This form is outside the requested service.")
                self.reply(
                    200,
                    '<!doctype html><html><head><meta charset="utf-8">'
                    "<title>Service portal</title></head><body>" + body + "</body></html>",
                )

            def do_POST(self):
                url = urlsplit(self.path)
                case_id = parse_qs(url.query).get("case", [""])[0]
                if url.path != "/submit" or case_id not in portal.cases:
                    return self.reply(404, "Unknown submission")
                try:
                    size = int(self.headers.get("Content-Length", 0))
                    if not 0 < size <= 65536:
                        raise ValueError("Invalid payload length")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("Expected object")
                except (ValueError, TypeError):
                    return self.reply(400, "Invalid JSON payload")
                with portal.lock:
                    portal.cases[case_id].submissions.append(payload)
                    portal.record({"type": "submit", "case": case_id, "payload": payload})
                self.reply(200, '{"received":true}', "application/json")

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def record(self, event):
        if self.records:
            with self.records.open("a") as output:
                output.write(json.dumps(event) + "\n")

    def add(self, case_id, task, seed, variant):
        if variant not in (*MUTATIONS, "practice"):
            raise ValueError("Unknown variant")
        with self.lock:
            if case_id in self.cases:
                old = self.cases[case_id]
                if (old.task, old.seed, old.variant) != (task, seed, variant):
                    raise ValueError("Cannot change an existing case")
            else:
                self.cases[case_id] = Case(task, seed, variant)
                self.record(
                    {
                        "type": "case",
                        "case": case_id,
                        "form": task.key,
                        "seed": seed,
                        "variant": variant,
                    }
                )
        return self.url + "/?" + urlencode({"case": case_id})

    def verdict(self, case_id):
        with self.lock:
            case = self.cases[case_id]
            return judge(case.task, case.seed, case.variant, case.submissions)

    def __enter__(self):
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    with Portal(args.port) as portal:
        print(portal.add("demo", TASKS[0], 101, "practice"), flush=True)
        try:
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
