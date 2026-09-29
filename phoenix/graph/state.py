from typing import Literal, Optional

from pydantic import BaseModel, Field

from phoenix.graph.schemas import ScoredHypothesis


class AgentState(BaseModel):
    incident_id: int
    service_name: str
    evidence: list[dict] = Field(default_factory=list)
    hypotheses: list[ScoredHypothesis] = Field(default_factory=list)
    iteration: int = 0
    max_iterations: int = 5
    confidence: float = 0.0
    confidence_threshold: float = 0.75
    cost_spent: float = 0.0
    cost_budget: float = 10.0
    status: Literal["investigating", "confident", "escalated"] = "investigating"
    escalation_reason: Optional[str] = None
