"""The bridge's view of the Phoenix API. Only the pending list so far.

An API that cannot be reached, answers an error, or sends back something unexpected is
one failure to the caller: ApiError. The bridge posts nothing on a pass that raises it.
"""

import httpx
import pytest

from phoenix.slack.api_client import ApiClient, ApiError

PENDING = {"approvals": [{"id": 7, "incident_id": 12, "action": "rollback_deployment"}]}


def _client(handler):
    return ApiClient("http://phoenix-api:8000", transport=httpx.MockTransport(handler))


def test_the_pending_list_is_read_from_the_approvals_endpoint():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json=PENDING)

    approvals = _client(handler).list_pending()

    assert approvals == PENDING["approvals"]
    assert seen == [("GET", "/approvals")]


def test_an_empty_queue_is_an_empty_list():
    assert _client(lambda request: httpx.Response(200, json={"approvals": []})).list_pending() == []


def test_a_connection_failure_is_an_api_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ApiError, match="ConnectError"):
        _client(handler).list_pending()


def test_a_timeout_is_an_api_error():
    def handler(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ApiError, match="ReadTimeout"):
        _client(handler).list_pending()


@pytest.mark.parametrize("status", [500, 503, 404])
def test_an_error_status_is_an_api_error_that_names_it(status):
    with pytest.raises(ApiError, match=str(status)):
        _client(lambda request: httpx.Response(status, json={"detail": "no"})).list_pending()


@pytest.mark.parametrize("body", [
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"unexpected": []}),
    httpx.Response(200, json={"approvals": "nope"}),
    httpx.Response(200, json={"approvals": [{"no_id": True}]}),
    httpx.Response(200, json=[]),
])
def test_a_body_that_is_not_the_expected_shape_is_an_api_error(body):
    with pytest.raises(ApiError, match="unexpected"):
        _client(lambda request: body).list_pending()
