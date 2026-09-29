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
    needs_evidence: list[str] = Field(
        default_factory=list,
        description="Outstanding confirm/refute signals from the surviving hypotheses, best-ranked first.",
    )
    iteration: int = 0
    max_iterations: int = 5
    confidence: float = 0.0
    confidence_threshold: float = 0.75
    tokens_spent: int = Field(
        default=0,
        description=(
            "LLM tokens spent so far, summed from response.usage.total_tokens. Stays "
            "0 for any call the provider billed without reporting usage."
        ),
    )
    token_budget: int = Field(
        default=20000,
        description="Hard ceiling on tokens_spent for one investigation, in tokens.",
    )
    status: Literal["investigating", "confident", "escalated"] = "investigating"
    escalation_reason: Optional[str] = None
