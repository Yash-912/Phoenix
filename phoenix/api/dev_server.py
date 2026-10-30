"""Serve the dashboard over the fixture incidents, with no database and no docker.

    python phoenix/api/dev_server.py [port]

Standard library only (uvicorn is not in the project venv). It answers the same read
routes as main.py through the same incident_trace code, plus a stand-in for the decision
endpoint that records nothing, so the page can be built and looked at without the stack.
Fixtures are shaped from the run dumps, not exported from a database (fixtures/README.md).
"""

import json
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import fixture_store
import incident_trace

DASHBOARD = Path(__file__).parent / "dashboard"
FIXTURES = fixture_store.load()
NOW = datetime.fromisoformat(fixture_store.FIXTURE_NOW)
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml"}
DEV_TOKEN = "dev-token"
DEV_APPROVERS = {"tester"}


class Handler(BaseHTTPRequestHandler):
    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        path = url.path
        if path == "/dashboard-config":
            return self._json({"approvals_enabled": True, "outcomes": incident_trace.OUTCOMES, "tiers": incident_trace.TIERS})
        if path == "/incidents/overview":
            return self._json({"incidents": [incident_trace.overview_row(r) for r in fixture_store.overview_rows(FIXTURES)]})
        if path == "/approvals":
            rows = [
                {**a, "incident_id": d["incident"]["id"], "service_name": d["incident"]["service_name"]}
                for d in FIXTURES.values() for a in d["approvals"] if a["status"] == "pending"
            ]
            return self._json({"approvals": incident_trace.approval_queue(rows, now=NOW)})
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "incidents" and parts[1].isdigit() and parts[2] in ("trace", "audit"):
            rows = FIXTURES.get(int(parts[1]))
            if rows is None:
                return self._json({"detail": f"no incident {parts[1]}"}, 404)
            if parts[2] == "trace":
                return self._json(incident_trace.build_trace(rows, now=NOW))
            return self._json(incident_trace.audit_rows(rows["audit"]))
        return self._static(path)

    def do_POST(self):
        parts = urlparse(self.path).path.strip("/").split("/")
        if parts[:1] != ["approvals"] or parts[-1:] != ["decision"]:
            return self._json({"detail": "not found"}, 404)
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if self.headers.get("Authorization") != f"Bearer {DEV_TOKEN}":
            return self._json({"detail": "missing or wrong approval token"}, 401)
        if body.get("approver") not in DEV_APPROVERS:
            return self._json({"detail": "that identity is not on the approver list"}, 403)
        return self._json({"approval_id": int(parts[1]), "status": "approved" if body.get("decision") == "approve" else "denied", "result": "recorded (dev server: nothing was stored)"})

    def _static(self, path):
        if path in ("/dashboard", "/"):
            self.send_response(302)
            self.send_header("Location", "/dashboard/")
            self.end_headers()
            return
        relative = path.removeprefix("/dashboard/") or "index.html"
        target = (DASHBOARD / relative).resolve()
        if DASHBOARD.resolve() not in target.parents or not target.is_file():
            return self._json({"detail": "not found"}, 404)
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", TYPES.get(target.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"dashboard on fixtures: http://127.0.0.1:{port}/dashboard/  (approve with name 'tester', token '{DEV_TOKEN}')")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
