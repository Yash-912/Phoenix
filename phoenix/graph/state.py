from pydantic import BaseModel


class AgentState(BaseModel):
    incident_id: int
    service_name: str
    evidence: list[dict] = []
    iteration: int = 0
    max_iterations: int = 5
    confidence: float = 0.0
    confidence_threshold: float = 0.75
