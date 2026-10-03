from typing import Literal, Optional

from pydantic import BaseModel, Field

# slow_query and memory_leak are Tier 3 categories: a root cause this specific
# (not "overload" generically) is what lets remediation_policy decide a
# category is code-level without the LLM itself ever choosing a tier. Adding
# them here, rather than overloading "overload", is what keeps a query
# regression and a CPU spike distinguishable in the trail -- they call for
# different tools and different fixes, and conflating them would make the
# category column lie about which investigation actually ran.
HypothesisCategory = Literal[
    "crash", "overload", "deploy", "config", "network", "slow_query", "memory_leak", "unknown"
]


class Hypothesis(BaseModel):
    description: str = Field(min_length=1, description="One-sentence root-cause guess.")
    category: HypothesisCategory = Field(
        description="Failure family this hypothesis belongs to."
    )
    needs_evidence: list[str] = Field(
        default_factory=list,
        description="Evidence signals that would confirm or refute this hypothesis.",
    )


class DiagnoserOutput(BaseModel):
    hypotheses: list[Hypothesis] = Field(
        min_length=1, max_length=4, description="Ranked guesses, best first (descriptions only; scores are computed in code)."
    )


class ScoredHypothesis(BaseModel):
    hypothesis: Hypothesis = Field(
        description="The LLM's proposal, carried verbatim. It has no score of its own."
    )
    score: float = Field(description="Deterministic score from scoring.py. Never stated by the LLM.")
    score_breakdown: dict = Field(description="Per-source signal audit trail explaining this score.")


class CodeDefect(BaseModel):
    """What the Tier 3 code investigator concluded, described only -- never a
    patch. file_path is relative to the repository root, exactly as
    search_repository/read_file report it, so a later stage can re-open the
    same file without re-deriving its location.
    """

    defect_found: bool = Field(
        description="False when the investigation could not pin the defect to one file/function."
    )
    file_path: Optional[str] = Field(
        default=None, description="Repo-relative path of the file believed to hold the defect."
    )
    function_name: Optional[str] = Field(
        default=None,
        description=(
            "The exact identifier, as written after `def` in the source, of the one function "
            "whose body is defective. Null only when no single function can be named."
        ),
    )
    description: str = Field(
        min_length=1, description="What the defect is, grounded in the evidence gathered."
    )
    fix_approach: str = Field(
        min_length=1,
        description="A one-to-two sentence description of the minimal fix, not the fix itself.",
    )
    confidence_rationale: str = Field(
        min_length=1,
        description="Why the gathered evidence (repo + git + runtime) supports this file being the cause.",
    )


class PatchTarget(BaseModel):
    """What the patch generator is allowed to work on, derived from the code
    investigator's CodeDefect rather than invented here. It names one function
    in one file, says what is wrong and what has to change, and lists what is
    off limits; patch_tool.validate_patch_scope enforces the same boundary in
    code, so the model is told the rule and the gate does not rely on it.
    """

    target_file: str = Field(description="Repo-relative path of the only file that may change.")
    target_function: str = Field(description="The one function whose body holds the defect.")
    defect_summary: str = Field(description="What the defect is, from the investigation.")
    required_change: str = Field(description="What has to change inside the target function.")
    forbidden_areas: list[str] = Field(description="Code the patch must leave exactly as it is.")


class PatchProposal(BaseModel):
    """The whole proposed replacement for one file. Never a raw diff -- the
    model is bad at hand-writing unified-diff hunks that apply cleanly, so it
    proposes the file's new full content and phoenix.tools.patch_tool computes
    the diff deterministically from old vs. new.
    """

    new_content: str = Field(min_length=1, description="The file's complete proposed content.")
    rationale: str = Field(min_length=1, description="What changed and why, for the PR body.")
    target_function: str = Field(
        default="",
        description="The function the model says it changed, declared before the content so it can be checked against the investigation's target.",
    )
