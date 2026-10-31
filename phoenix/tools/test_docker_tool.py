"""get_container_state keeps environment variable names and drops their values:
the inspect payload is stored as evidence and shown to the model, and a container's
environment is where its secrets live."""

from phoenix.tools import docker_tool


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _inspect(monkeypatch, payload):
    monkeypatch.setattr(docker_tool.requests, "get", lambda *a, **k: _Response(payload))
    return docker_tool.get_container_state("svc")


def test_env_values_are_dropped_and_names_kept(monkeypatch):
    state = _inspect(monkeypatch, {
        "State": {"Status": "running"},
        "Config": {"Env": ["DB_PASSWORD=hunter2", "CHAOS_ENABLED=true", "EMPTY=", "BARE"], "Labels": {"app.version": "v17"}},
    })

    assert state["Config"]["Env"] == ["DB_PASSWORD", "CHAOS_ENABLED", "EMPTY", "BARE"]
    assert "hunter2" not in str(state)


def test_the_rest_of_the_inspect_payload_is_untouched(monkeypatch):
    state = _inspect(monkeypatch, {
        "State": {"Status": "running", "StartedAt": "2026-01-01T00:00:00Z"},
        "Image": "sha256:abc",
        "Config": {"Env": ["A=1"], "Labels": {"app.version": "v17"}},
    })

    assert state["State"]["StartedAt"] == "2026-01-01T00:00:00Z"
    assert state["Image"] == "sha256:abc"
    assert state["Config"]["Labels"] == {"app.version": "v17"}


def test_a_payload_with_no_config_or_env_is_returned_as_is(monkeypatch):
    assert _inspect(monkeypatch, {"State": {"Status": "running"}}) == {"State": {"Status": "running"}}
    assert _inspect(monkeypatch, {"Config": {"Env": None}})["Config"]["Env"] is None
    assert _inspect(monkeypatch, {"Config": None}) == {"Config": None}


def test_a_failed_request_is_still_the_error_envelope(monkeypatch):
    def boom(*a, **k):
        raise docker_tool.requests.ConnectionError("down")

    monkeypatch.setattr(docker_tool.requests, "get", boom)

    assert docker_tool.get_container_state("svc")["status"] == "error"
