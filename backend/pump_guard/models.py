from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class Measurements(StrictModel):
    vibration_mm_s: float | None = Field(default=None, ge=0, le=100)
    bearing_temperature_c: float | None = Field(default=None, ge=-50, le=300)
    suction_pressure_bar: float | None = Field(default=None, ge=-1, le=100)
    discharge_pressure_bar: float | None = Field(default=None, ge=-1, le=200)
    flow_m3_h: float | None = Field(default=None, ge=0, le=10000)
    motor_current_a: float | None = Field(default=None, ge=0, le=10000)


class AnalysisRequest(StrictModel):
    equipment_id: str = Field(pattern=r"^P-[0-9]{3}$")
    measurements: Measurements


class InspectionRequest(StrictModel):
    equipment_id: str = Field(pattern=r"^(P-[0-9]{3}|UCI-447)$")
    research_cycle_id: int | None = Field(default=None, ge=1, le=2205)
    note: str = Field(default="", max_length=1000)
    checks: list[Literal["visual", "vibration", "temperature", "leak", "alignment"]] = Field(min_length=1, max_length=5)
    outcome: Literal["pass", "follow_up", "repair_required"]

    @field_validator("checks")
    @classmethod
    def unique_checks(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("점검 항목은 중복할 수 없습니다.")
        return value


class Finding(StrictModel):
    metric: str
    value: float
    level: int = Field(ge=0, le=3)
    reason: str


class AnalysisResult(StrictModel):
    run_id: str
    equipment_id: str
    mode: Literal["mock", "ollama", "openai"] = "mock"
    status: Literal["정상", "관찰", "주의", "점검 필요"] | None
    missing_fields: list[str]
    findings: list[Finding]
    candidate_causes: list[str]
    checklist: list[str]
    safety_notice: str
    decision_supported: Literal[False] = False
    model: str | None = None
    evidence: list["Evidence"] = Field(default_factory=list)
    cause_details: list["CauseDetail"] = Field(default_factory=list)
    maintenance_plan: list["MaintenanceStep"] = Field(default_factory=list)
    metrics: "RunMetrics" = Field(default_factory=lambda: RunMetrics())
    architecture: Literal["workflow", "multi_agent"] = "workflow"
    agent_reports: list["AgentReport"] = Field(default_factory=list)
    completed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Evidence(StrictModel):
    id: str
    source: Literal["current", "operating_history", "maintenance_history"]
    observation: str


class CauseChoice(StrictModel):
    manual_id: str = Field(min_length=1, max_length=100)
    evidence_ids: list[str] = Field(min_length=1, max_length=12)


class CauseSelection(StrictModel):
    candidates: list[CauseChoice] = Field(max_length=3)


class GuideChoice(StrictModel):
    manual_id: str = Field(min_length=1, max_length=100)
    guide_index: int = Field(ge=0, le=30)


class MaintenanceSelection(StrictModel):
    steps: list[GuideChoice] = Field(max_length=12)


class CauseDetail(StrictModel):
    manual_id: str
    fault: str
    source: str
    evidence_ids: list[str]


class MaintenanceStep(StrictModel):
    manual_id: str
    guide_index: int
    instruction: str
    source: str


class LLMCallMetrics(StrictModel):
    stage: str
    model: str
    latency_ms: float = Field(ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    state: Literal["completed", "failed", "cancelled"] = "completed"


class RunMetrics(StrictModel):
    total_ms: float = Field(default=0, ge=0)
    stage_ms: dict[str, float] = Field(default_factory=dict)
    llm_calls: list[LLMCallMetrics] = Field(default_factory=list)


class AgentReport(StrictModel):
    agent: str
    attempt: int = Field(ge=1)
    tools: list[str]
    decision: str
    turns: int = Field(ge=1)


class AgentToolCall(StrictModel):
    name: Literal["get_equipment", "get_operating_history", "get_maintenance_history", "evaluate_rules", "compare_history", "search_manuals", "get_selected_guides", "validate_proposal"]
    query: str | None = Field(default=None, min_length=1, max_length=100)


class InputAssessment(StrictModel):
    equipment_id: str
    next_agent: Literal["condition"]


class ConditionAssessment(StrictModel):
    status: Literal["정상", "관찰", "주의", "점검 필요"] | None
    missing_fields: list[str] = Field(max_length=6)
    evidence_ids: list[str] = Field(max_length=12)


class SafetyReview(StrictModel):
    verdict: Literal["approve", "revise_cause", "revise_maintenance", "reject"]
    reason: Literal["supported", "insufficient_evidence", "plan_incomplete", "conflicting_evidence"]


AnalysisResult.model_rebuild()
