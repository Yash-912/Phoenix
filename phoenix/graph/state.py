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
        "investigating", "confident", "resolved", "action_unavailable", "escalated",
        # Tier 3 terminal/transit states. "tier3_investigating" is set the
        # moment the run hands off to the Tier 3 subgraph (either straight
        # from the router for slow_query, or after a passing memory_leak
        # Tier 1 verification) -- a run in this state has not stopped, it has
        # changed which subgraph owns it. "pr_opened" is Tier 3's own success
        # terminal, kept distinct from "resolved" because a PR is not a
        # closed incident: PRD section 6 requires it to stop for human
        # review, and "resolved" would read as nothing further being needed.
        "tier3_investigating", "pr_opened",
    ] = "investigating"
    escalation_reason: Optional[str] = None

    # --- Tier 3: code investigation -> patch -> validate -> PR -------------
    # Kept on a separate status axis from the top-level `status` above
    # because Tier 1/2's state machine and Tier 3's are two different
    # workflows that happen to share one incident: a Tier 1 restart's
    # attempt count must not gate a Tier 3 patch attempt, and a Tier 3
    # rejection must not look like a Tier 1 action failure in the trail.
    tier3_status: Literal[
        "not_started", "investigating", "no_defect_found", "patch_generated",
        "patch_rejected", "validated", "validation_failed", "pr_opened", "pr_failed",
    ] = "not_started"
    tier3_iteration: int = 0
    max_tier3_iterations: int = Field(
        default=5,
        description="Hard cap on the code investigator's own tool-call loop, independent of the Tier 1/2 iteration cap.",
    )
    tier3_evidence: list[dict] = Field(
        default_factory=list,
        description="search_repository/read_file/get_git_commits/get_git_diff results gathered during Tier 3 investigation.",
    )
    tier3_defect: Optional[dict] = Field(
        default=None,
        description="The code_investigator's conclusion: file_path, function_name, description, fix_approach -- never a patch.",
    )
    patch_candidate: Optional[dict] = Field(
        default=None,
        description="patch_generator's proposed diff before validation: file_path, diff, new_content, scope verdict.",
    )
    patch_validation: Optional[dict] = Field(
        default=None,
        description="patch_validator's real results: applied, tests_passed, lint_passed, diff_in_scope, and the raw outputs.",
    )
    tier1_mitigation: Optional[dict] = Field(
        default=None,
        description=(
            "The Tier 1 verification_result snapshot taken before handing a memory_leak "
            "run to Tier 3, so the final state records the mitigation and the permanent "
            "fix as two separate facts rather than the second overwriting the first."
        ),
    )
    pr_result: Optional[dict] = Field(
        default=None,
        description="open_pull_request's real response: url, branch, base -- or the error envelope on failure.",
    )
    worktree_path: Optional[str] = None
    worktree_branch: Optional[str] = None
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
