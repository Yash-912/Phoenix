"""The bridge's view of the Phoenix API. Only the pending list so far.

An API that cannot be reached, answers an error, or sends back something unexpected is
one failure to the caller: ApiError. The bridge posts nothing on a pass that raises it.
"""

import httpx
import pytest

from phoenix.slack.api_client import ApiClient, ApiError

PENDING = {"approvals": [{"id": 7, "incident_id": 12, "action": "rollback_deployment"}]}
TOKEN = "approval-token-for-tests"


def _client(handler, token=TOKEN):
    return ApiClient("http://phoenix-api:8000", token=token, transport=httpx.MockTransport(handler))


def _decide(handler, decision="approve", approver="alice"):
    return _client(handler).decide(7, decision, approver)


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


# --- sending a decision -------------------------------------------------------


def test_a_decision_is_posted_with_the_token_the_approver_and_the_slack_channel():
    seen = {}

    def handler(request):
        seen["method"], seen["path"] = request.method, request.url.path
        seen["auth"] = request.headers.get("authorization")
        seen["body"] = request.read()
        return httpx.Response(200, json={"approval_id": 7, "status": "approved", "decided_by": "alice", "result": "recorded"})

    result = _decide(handler)

    assert (seen["method"], seen["path"]) == ("POST", "/approvals/7/decision")
    assert seen["auth"] == f"Bearer {TOKEN}"
    import json
    assert json.loads(seen["body"]) == {"decision": "approve", "approver": "alice", "channel": "slack"}
    assert result.kind == "recorded"
    assert result.status == "approved"


def test_a_denial_is_sent_as_a_deny_decision():
    seen = {}

    def handler(request):
        import json
        seen["body"] = json.loads(request.read())
        return httpx.Response(200, json={"approval_id": 7, "status": "denied", "decided_by": "alice", "result": "recorded"})

    result = _decide(handler, decision="deny")

    assert seen["body"]["decision"] == "deny"
    assert result.status == "denied"


@pytest.mark.parametrize("response, kind, status", [
    (httpx.Response(409, json={"detail": {"status": "approved", "result": "ignored: the first answer already won"}}), "already_decided", "approved"),
    (httpx.Response(409, json={"detail": {"status": "denied", "result": "ignored: the first answer already won"}}), "already_decided", "denied"),
    (httpx.Response(409, json={"detail": {"status": "expired", "result": "refused: the deadline has passed, and expiry is never an approval"}}), "expired", "expired"),
    (httpx.Response(403, json={"detail": "that identity is not on the approver list"}), "not_on_list", None),
    (httpx.Response(401, json={"detail": "missing or wrong approval token"}), "not_configured", None),
    (httpx.Response(503, json={"detail": "approvals are disabled: PHOENIX_APPROVAL_TOKEN is not set"}), "not_configured", None),
    (httpx.Response(404, json={"detail": "no approval 7"}), "not_found", None),
])
def test_each_answer_the_api_can_give_is_one_distinct_result(response, kind, status):
    result = _decide(lambda request: response)

    assert result.kind == kind
    assert result.status == status


@pytest.mark.parametrize("response", [
    httpx.Response(500, text="boom"),
    httpx.Response(422, json={"detail": []}),
    httpx.Response(200, text="<html>not json</html>"),
    httpx.Response(200, json={"unexpected": True}),
    httpx.Response(409, text="not json"),
    httpx.Response(409, json={"detail": "no status here"}),
])
def test_an_answer_that_is_not_one_of_those_is_an_error_result_and_not_a_decision(response):
    result = _decide(lambda request: response)

    assert result.kind == "error"


@pytest.mark.parametrize("error", [httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), httpx.RemoteProtocolError("hung up")])
def test_a_failure_to_reach_the_api_is_unreachable_and_never_raises(error):
    def handler(request):
        raise error

    assert _decide(handler).kind == "unreachable"


def test_without_a_token_nothing_is_sent():
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(200, json={})

    result = _client(handler, token=None).decide(7, "approve", "alice")

    assert result.kind == "not_configured"
    assert sent == []


def test_the_token_is_in_no_result_and_no_error_text():
    def handler(request):
        raise httpx.ConnectError(f"refused for {request.headers['authorization']}")

    result = _decide(handler)

    assert TOKEN not in repr(result)
    for response in (httpx.Response(401, json={"detail": TOKEN}), httpx.Response(500, text=TOKEN)):
        assert TOKEN not in repr(_decide(lambda request, r=response: r))
