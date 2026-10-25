"""Local-only demo server; simulated events, not a public webhook endpoint."""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .events import process
from .store import initialize, snapshot

ROOT = Path(__file__).resolve().parents[1]


def create_server(database, port):
    initialize(database)

    class Handler(BaseHTTPRequestHandler):
        def respond(self, status, data, content_type="application/json; charset=utf-8"):
            body = (
                json.dumps(data).encode() if content_type.startswith("application/json") else data
            )
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/":
                return self.respond(
                    200, (ROOT / "web/index.html").read_bytes(), "text/html; charset=utf-8"
                )
            if self.path == "/api/state":
                state = snapshot(database)
                progress = ROOT / ".demo/progress.json"
                state["learning"] = json.loads(progress.read_text()) if progress.exists() else {}
                return self.respond(200, state)
            self.respond(404, {"error": "Not found"})

        def do_POST(self):
            if self.path != "/api/events":
                return self.respond(404, {"error": "Not found"})
            # Reject browser requests originating outside this local demo.
            origin = self.headers.get("Origin")
            if origin and origin != f"http://127.0.0.1:{self.server.server_address[1]}":
                return self.respond(403, {"error": "Use the local ParcelDesk page"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    return self.respond(413, {"error": "Event body must be at most 4096 bytes"})
                result = process(database, json.loads(self.rfile.read(length)))
            except (ValueError, TypeError) as error:
                return self.respond(400, {"error": str(error)})
            return self.respond(200, result)

        def log_message(self, *_args):
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8768)
    parser.add_argument("--database", type=Path, default=ROOT / ".demo/warehouse.sqlite")
    args = parser.parse_args()
    args.database.parent.mkdir(parents=True, exist_ok=True)
    server = create_server(args.database, args.port)
    print(f"ParcelDesk running at http://127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
