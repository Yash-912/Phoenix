from datetime import datetime

from pydantic import BaseModel, field_validator


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
