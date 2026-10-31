"""The bridge's view of the Phoenix API.

The API is the only place that checks the approval token, the approver list and expiry,
and the only writer of the approvals table. The bridge asks it questions and, in a later
step, sends it decisions. Anything that goes wrong on the way, whether the connection,
the status or the shape of the answer, is raised as ApiError. The caller treats that as
"the API cannot be trusted this pass" and does nothing.
"""

import httpx


class ApiError(RuntimeError):
    """The API could not be reached, answered an error, or sent back something unexpected."""


class ApiClient:
    def __init__(self, base_url: str, *, transport: httpx.BaseTransport | None = None, timeout: float = 5.0) -> None:
        self._client = httpx.Client(base_url=base_url, timeout=timeout, transport=transport)

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
