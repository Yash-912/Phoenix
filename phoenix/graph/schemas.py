from typing import Literal

from pydantic import BaseModel, Field


class Hypothesis(BaseModel):
    description: str = Field(min_length=1, description="One-sentence root-cause guess.")
    category: Literal["crash", "overload", "deploy", "config", "network", "unknown"] = Field(
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
