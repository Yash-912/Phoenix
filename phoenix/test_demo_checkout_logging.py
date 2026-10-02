"""The checkout service's regression has to be findable the ordinary way.

Tier 2 works or fails on whether anyone -- an operator with grep, or the observer
with a log query -- can tell that the running artifact is the broken one. That
only holds if the error it logs is recognisable as an error once it reaches Loki.

`logger.error` puts the level on the LogRecord, not in the message, so a format
of "%(message)s" discards it on the way out and the stored line looks like ordinary
output. These tests therefore read the emitted text through the handler this
service installs for itself, rather than inspecting a LogRecord's `levelno`,
which would say ERROR whether or not the word survives.
"""

import importlib.util
import io
import logging
import pathlib
import sys
import types

import pytest

pytest.importorskip("fastapi")

APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "demo-checkout" / "app.py"


@pytest.fixture
def service(monkeypatch):
    """Import the demo-checkout app fresh and hand back the log text it emits.

    Root handlers are cleared first so the module's own basicConfig is what
    installs the handler under test; under pytest the root logger already has
    handlers, which would leave basicConfig a no-op and quietly test pytest's
    logging setup instead of the service's.
    """
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [], raising=False)
    monkeypatch.setenv("APP_VERSION", "v18")
    monkeypatch.setenv("REGRESSION_ENABLED", "true")

    # The metrics instrumentator ships with the service image, not the test env,
    # and nothing here is about metrics.
    stub = types.ModuleType("prometheus_fastapi_instrumentator")

    class _Instrumentator:
        def instrument(self, app):
            return self

        def expose(self, app, **kwargs):
            return self

    stub.Instrumentator = _Instrumentator
    monkeypatch.setitem(sys.modules, "prometheus_fastapi_instrumentator", stub)

    spec = importlib.util.spec_from_file_location("demo_checkout_under_test", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    emitted = io.StringIO()
    installed = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
    if not installed:
        pytest.fail(
            "the service installed no log handler, so its errors would reach the "
            "log store unformatted and unsearchable"
        )
    installed[0].stream = emitted

    return module, emitted


def test_an_error_reaches_the_log_as_a_line_anyone_can_search_for(service):
    module, emitted = service

    with pytest.raises(Exception):
        module.checkout()

    line = emitted.getvalue()
    assert line.strip(), "the rejection logged nothing an operator could find"
    assert "ERROR" in line, f"the stored line carries no severity: {line!r}"


def test_the_artifact_that_failed_is_named_in_the_line(service):
    """The log exists to tell v18 apart from whatever ran before it and whatever
    runs after a rollback."""
    module, emitted = service

    with pytest.raises(Exception):
        module.checkout()

    assert "v18" in emitted.getvalue()


def test_a_healthy_artifact_logs_no_error(service):
    """Otherwise the severity check passes on a service that always shouts."""
    module, emitted = service
    module.REGRESSION_ENABLED = False

    result = module.checkout()

    assert result["status"] == "confirmed"
    assert "ERROR" not in emitted.getvalue()


def test_health_stays_green_in_the_broken_artifact(service):
    """The reason verification cannot lean on liveness: v18 rejects every order
    and still reports itself healthy."""
    module, _emitted = service

    assert module.health()["status"] == "ok"
