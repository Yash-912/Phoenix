"""The bridge's view of the Phoenix API.

The API is the only place that checks the approval token, the approver list and expiry,
and the only writer of the approvals table. The bridge asks it questions and sends it
decisions. Anything that goes wrong when listing, whether the connection, the status or
the shape of the answer, is raised as ApiError, and the caller treats that as "the API
cannot be trusted this pass" and does nothing.

A decision is different: a person is waiting on the answer, so decide() never raises. It
returns a DecisionResult whose kind says exactly what happened, and the caller turns
each kind into one reply. The bearer token is added to that single request and appears in
no result, no error text and no log line.
"""

from dataclasses import dataclass

import httpx

SLACK_CHANNEL = "slack"


class ApiError(RuntimeError):
    """The API could not be reached, answered an error, or sent back something unexpected."""


@dataclass(frozen=True)
class DecisionResult:
    """What the API did with one answer.

    kind: recorded, already_decided, expired, not_on_list, not_configured, not_found,
    unreachable or error. status: approved or denied for a recorded answer, and the
    status that won for already_decided and expired.
    """

    kind: str
    status: str | None = None


class ApiClient:
    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
    ) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)
        self._token = token

    def list_pending(self) -> list[dict]:
        """The open requests, in the redacted shape the dashboard uses."""
        try:
            response = self._client.get("/approvals")
        except httpx.HTTPError as exc:
            raise ApiError(f"GET /approvals failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ApiError(f"GET /approvals answered {response.status_code}")
        try:
            approvals = response.json()["approvals"]
            if not isinstance(approvals, list) or not all(isinstance(item, dict) and "id" in item for item in approvals):
                raise ValueError("not a list of approvals")
        except (ValueError, KeyError, TypeError) as exc:
            raise ApiError("GET /approvals sent an unexpected body") from exc
        return approvals

    def get_approval(self, approval_id: int) -> dict | None:
        """One approval in any status, as the dashboard sees it. None when there is no such request."""
        try:
            response = self._client.get(f"/approvals/{approval_id}")
        except httpx.HTTPError as exc:
            raise ApiError(f"GET /approvals/{approval_id} failed: {type(exc).__name__}") from exc
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise ApiError(f"GET /approvals/{approval_id} answered {response.status_code}")
        body = _json(response)
        approval = body.get("approval") if isinstance(body, dict) else None
        if not isinstance(approval, dict) or "id" not in approval or "status" not in approval:
            raise ApiError(f"GET /approvals/{approval_id} sent an unexpected body")
        return approval

    def decide(self, approval_id: int, decision: str, approver: str) -> DecisionResult:
        """Send one answer. decision is "approve" or "deny"; approver is the mapped name."""
        if not self._token:
            return DecisionResult("not_configured")
        try:
            response = self._client.post(
                f"/approvals/{approval_id}/decision",
                json={"decision": decision, "approver": approver, "channel": SLACK_CHANNEL},
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError:
            return DecisionResult("unreachable")

        status_code = response.status_code
        if status_code == 200:
            body = _json(response)
            if isinstance(body, dict) and body.get("result") == "recorded" and body.get("status") in ("approved", "denied"):
                return DecisionResult("recorded", body["status"])
            return DecisionResult("error")
        if status_code == 409:
            body = _json(response)
            detail = body.get("detail") if isinstance(body, dict) else None
            winner = detail.get("status") if isinstance(detail, dict) else None
            if not isinstance(winner, str):
                return DecisionResult("error")
            return DecisionResult("expired" if winner == "expired" else "already_decided", winner)
        if status_code == 403:
            return DecisionResult("not_on_list")
        if status_code in (401, 503):
            return DecisionResult("not_configured")
        if status_code == 404:
            return DecisionResult("not_found")
        return DecisionResult("error")


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None
