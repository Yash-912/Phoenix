from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ApprovalDecision(BaseModel):
    """An answer to one approval request. approver must be on the configured list.

    channel says which route the answer came through and is recorded as given. The caller
    already holds the approval token, so this is an assertion by a trusted caller, the same
    as the approver name. An older client that sends no channel is recorded as "api".
    """

    decision: Literal["approve", "deny"]
    approver: str = Field(min_length=1, max_length=200)
    channel: Literal["api", "dashboard", "slack"] = "api"


class AlertmanagerAlert(BaseModel):
    status: str
    labels: dict[str, str]
    annotations: dict[str, str] = {}
    startsAt: datetime
    endsAt: datetime | None = None
    generatorURL: str | None = None
    fingerprint: str | None = None

    @field_validator("labels")
    @classmethod
    def require_alertname_and_service(cls, labels: dict[str, str]) -> dict[str, str]:
        if "alertname" not in labels:
            raise ValueError("alert labels missing required 'alertname'")
        if "service" not in labels:
            raise ValueError("alert labels missing required 'service'")
        return labels


class AlertmanagerWebhookPayload(BaseModel):
    version: str
    groupKey: str
    status: str
    receiver: str
    alerts: list[AlertmanagerAlert]
