import pytest
import requests

from phoenix.tools import loki_tool


class _Rejected:
    """What requests hands back for a query Loki refused: a 400 with its own message."""

    status_code = 400
    text = 'parse error at line 1, col 18: syntax error: unexpected IDENTIFIER'

    def raise_for_status(self):
        raise requests.HTTPError("400 Client Error: Bad Request for url: http://loki/q", response=self)


def test_a_rejected_query_returns_lokis_own_message_so_the_caller_can_correct_it(monkeypatch):
    monkeypatch.setattr(loki_tool.requests, "get", lambda *a, **k: _Rejected())

    result = loki_tool.query_loki('{container="x"} | grep -i error')

    assert result["status"] == "error"
    assert "400 Client Error" in result["error"]
    assert "parse error at line 1, col 18" in result["error"]


def test_the_message_kept_from_a_rejected_query_is_bounded(monkeypatch):
    rejected = _Rejected()
    rejected.text = "x" * 5000
    monkeypatch.setattr(loki_tool.requests, "get", lambda *a, **k: rejected)

    result = loki_tool.query_loki('{container="x"}')

    assert len(result["error"]) < 700


def test_a_transport_failure_still_reports_just_the_exception(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError("Connection refused")

    monkeypatch.setattr(loki_tool.requests, "get", boom)

    assert loki_tool.query_loki('{container="x"}') == {"status": "error", "error": "Connection refused"}


@pytest.mark.parametrize(
    "bad",
    ["15", 0, -5, 100000, 15.5, True, None],
    ids=["str", "zero", "negative", "too-large", "float", "bool", "none"],
)
def test_a_minutes_value_that_is_not_a_sane_int_is_refused_without_allocating(bad):
    result = loki_tool.query_loki('{container="checkout-service"}', minutes=bad)

    assert result == {"status": "error", "error": f"invalid minutes: {bad!r}"}
