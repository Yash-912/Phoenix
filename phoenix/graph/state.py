from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from phoenix.graph.schemas import ScoredHypothesis


class AgentState(BaseModel):
    """Investigation state. validate_assignment makes the field annotations
    load-bearing on every node write, so a node cannot assign a raw dict to
    hypotheses and leave the state holding a value that is not actually scored.
    """

    model_config = ConfigDict(validate_assignment=True)

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
