from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class ApprovalDecision(BaseModel):
    """An answer to one approval request. approver must be on the configured list."""

    decision: Literal["approve", "deny"]
    approver: str = Field(min_length=1, max_length=200)


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
