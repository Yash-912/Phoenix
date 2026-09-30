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
    status: Literal[
        "investigating", "confident", "resolved", "action_unavailable", "escalated"
    ] = "investigating"
    escalation_reason: Optional[str] = None
    remediation_attempts: int = Field(
        default=0,
        ge=0,
        description=(
            "Mutating actions executed so far. Incremented only after a dispatch "
            "returns ok, never before, so a blocked action, a failed action, and a "
            "successful one are three distinguishable facts in the trail."
        ),
    )
    max_remediation_attempts: int = Field(
        default=2,
        ge=0,
        description=(
            "Hard ceiling on mutating actions for one incident. Independent of "
            "token_budget on purpose: exhausting the budget does not buy extra "
            "restarts, and a run cannot spend tokens to raise its own ceiling. "
            "ge=0 rather than a bare int so a negative cap cannot be configured: "
            "it would read as 'already spent' and the agent would never act, which "
            "is fail-safe but is nonsense rather than a policy anyone chose. 0 "
            "stays legal and means observe-only."
        ),
    )
    policy_mode: Literal["autonomous_lab", "guarded"] = Field(
        default="autonomous_lab",
        description=(
            "The execution boundary. 'guarded' means the action does not execute "
            "and the run escalates. The approval workflow is Phase 7; Phase 3 "
            "ships the boundary and the test that it holds, not a queue."
        ),
    )
    planned_action: Optional[dict] = Field(
        default=None,
        description=(
            "What the deterministic policy chose and why, plus the signal read "
            "immediately before the action. The snapshot lives here because it "
            "belongs to one action; a retry must not verify against the reading "
            "taken for the previous attempt."
        ),
    )
    verification_result: Optional[dict] = Field(
        default=None,
        description=(
            "Outcome is one of pass, fail, or inconclusive. A signal that could "
            "not be read is inconclusive, never pass: a run that cannot check "
            "its own action must not claim the action worked."
        ),
    )
    verification_delay_seconds: int = Field(
        default=15,
        ge=0,
        description=(
            "Settling time between an action and its check, because a container "
            "that was just restarted is not answering yet. 15 in a real run, 0 in "
            "tests. ge=0 because time.sleep rejects a negative and a verifier "
            "that raised on its own configuration would lose the run."
        ),
    )
