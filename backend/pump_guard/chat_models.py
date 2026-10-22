from typing import Literal
from pydantic import Field, field_validator
from .models import StrictModel, RunMetrics


class ChatSessionRequest(StrictModel):
    equipment_id: str = Field(pattern=r"^(P-[0-9]{3}|UCI-447)$")
    analysis_run_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")
    research_cycle_id: int | None = Field(default=None, ge=1, le=2205)


class ChatMessageRequest(StrictModel):
    message: str = Field(min_length=1, max_length=2000)

    @field_validator("message")
    @classmethod
    def meaningful_text(cls, value):
        if not value.strip():
            raise ValueError("Message cannot be blank")
        return value.strip()


class ChatRoute(StrictModel):
    intent: Literal["analysis", "equipment", "manual", "assessment", "clarify"]
    tools: list[Literal["get_analysis_snapshot", "get_analysis_history", "get_equipment", "get_operating_history", "get_maintenance_history", "get_measured_history", "search_manuals"]] = Field(max_length=7)
    query: str | None = Field(default=None, min_length=1, max_length=100)


class ChatSection(StrictModel):
    text: str = Field(min_length=1, max_length=1500)
    source_ids: list[str] = Field(min_length=1, max_length=8)


class ChatDraft(StrictModel):
    sections: list[ChatSection] = Field(min_length=1, max_length=5)
    analysis_status: Literal["정상", "관찰", "주의", "점검 필요"] | None
    follow_up_questions: list[str] = Field(max_length=3)

    @field_validator("follow_up_questions")
    @classmethod
    def bounded_questions(cls, values):
        if any(not q.strip() or len(q) > 200 for q in values):
            raise ValueError("Invalid follow-up question")
        return values


class ChatReview(StrictModel):
    verdict: Literal["approve", "reject"]
    reason: Literal["supported", "unsupported_claim", "conflicting_status", "insufficient_sources"]


class ChatSource(StrictModel):
    id: str
    kind: Literal["analysis", "analysis_history", "equipment", "operating_history", "maintenance_history", "measurement_history", "measured_history", "manual"]
    title: str
    content: str


class ChatResult(StrictModel):
    run_id: str
    session_id: str
    message_id: str
    equipment_id: str
    analysis_run_id: str | None
    research_cycle_id: int | None = None
    analysis_status: Literal["정상", "관찰", "주의", "점검 필요"] | None
    mode: Literal["mock", "ollama", "openai"]
    model: str | None
    response_kind: Literal["llm", "source_summary", "normal_range", "clarification"] = "llm"
    answer: str
    sections: list[ChatSection]
    sources: list[ChatSource]
    follow_up_questions: list[str]
    safety_notice: str
    decision_supported: Literal[False] = False
    metrics: RunMetrics = Field(default_factory=RunMetrics)
