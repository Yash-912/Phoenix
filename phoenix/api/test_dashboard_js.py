"""Run the dashboard's node tests as part of the Python suite.

The page's logic is plain ES modules with no build step, tested with node's own test
runner. This builds the traces the views are rendered over from the fixtures (so the
JS tests and the Python assembly cannot drift apart), then runs `node --test`.
Skipped where node is not installed.
"""

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

import pytest

import fixture_store
import incident_trace

DASHBOARD = Path(__file__).parent / "dashboard"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_dashboard_js_suite_passes(tmp_path):
    incidents = fixture_store.load()
    now = datetime.fromisoformat(fixture_store.FIXTURE_NOW)
    overview = [incident_trace.overview_row(row) for row in fixture_store.overview_rows(incidents)]
    (tmp_path / "overview.json").write_text(json.dumps({"incidents": overview}))
    for incident_id, rows in incidents.items():
        (tmp_path / f"trace_{incident_id}.json").write_text(json.dumps(incident_trace.build_trace(rows, now=now)))
    (tmp_path / "audit_33.json").write_text(json.dumps(incident_trace.audit_rows(incidents[33]["audit"])))
    pending = [
        {**a, "incident_id": rows["incident"]["id"], "service_name": rows["incident"]["service_name"]}
        for rows in incidents.values() for a in rows["approvals"] if a["status"] == "pending"
    ]
    (tmp_path / "approvals.json").write_text(json.dumps({"approvals": incident_trace.approval_queue(pending, now=now)}))

    result = subprocess.run(
        [NODE, "--test", *map(str, sorted((DASHBOARD / "test").glob("*.test.mjs")))],
        capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "PHOENIX_TRACE_DIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "# skipped 0" in result.stdout
