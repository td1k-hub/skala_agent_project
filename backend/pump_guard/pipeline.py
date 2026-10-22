import asyncio
import json
from contextlib import asynccontextmanager
from time import perf_counter
from typing import TypedDict
from uuid import UUID, NAMESPACE_URL, uuid5

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langsmith import Client, tracing_context

from .config import Settings
from .llm import InvalidModelOutput, OllamaAdapter, OpenAIAdapter
from .models import (
    AnalysisRequest, AnalysisResult, CauseChoice, CauseDetail, CauseSelection,
    Evidence, GuideChoice, MaintenanceSelection, MaintenanceStep, RunMetrics,
)
from .rules import evaluate
from .monitoring import RunStore
from .chat_store import ConversationStore

STAGES = [
    ("input", "입력 분석 중..."), ("condition", "상태 진단 중..."),
    ("cause", "고장 원인 후보 조회 중..."), ("maintenance", "정비 가이드 조회 중..."),
    ("safety", "안전 검수 중..."),
]


class AnalysisState(TypedDict, total=False):
    request: AnalysisRequest
    spec: dict
    operating: dict
    maintenance: dict
    status: str | None
    missing: list[str]
    findings: list
    evidence: list[Evidence]
    manuals: list[dict]
    causes: CauseSelection
    plan: list[GuideChoice]
    result: AnalysisResult
    attempts: dict
    agent_reports: list
    review: object
    revisions: int
    review_feedback: dict


def collect_evidence(request, operating, maintenance):
    """Describe observed values/deltas only; do not assign new risk thresholds."""
    evidence = [Evidence(id=f"current:{metric}", source="current", observation=f"{metric}={value}")
                for metric, value in request.measurements.model_dump().items() if value is not None]
    rows = sorted(operating.get("items", []), key=lambda item: item["timestamp"])
    for metric in ("vibration_mm_s", "bearing_temperature_c", "flow_m3_h"):
        points = [r for r in rows if type(r.get(metric)) in (float, int)]
        if not points:
            continue
        first, last = points[0], points[-1]
        current = getattr(request.measurements, metric)
        observation = (f"합성 이력 {len(points)}건; {metric}: "
                       f"{first['timestamp']}={first[metric]}, {last['timestamp']}={last[metric]}; "
                       f"이력 처음→마지막 변화={last[metric] - first[metric]:.3f}")
        if current is not None:
            observation += f"; 현재−마지막 이력={current - last[metric]:.3f}"
        evidence.append(Evidence(id=f"history:{metric}", source="operating_history", observation=observation))
    items = maintenance.get("items", [])[:10]
    for item in items:
        evidence.append(Evidence(id=f"inspection:{item['id']}", source="maintenance_history",
                                 observation=json.dumps({k: item[k] for k in ("created_at", "checks", "outcome")}, ensure_ascii=False)))
    if not items:
        evidence.append(Evidence(id="maintenance:empty", source="maintenance_history", observation="저장된 점검 이력이 없습니다. 무고장 이력을 의미하지 않습니다."))
    return evidence


def validate_references(causes, plan, manuals, evidence):
    by_id = {m["id"]: m for m in manuals}
    evidence_ids = {e.id for e in evidence}
    chosen = [c.manual_id for c in causes.candidates]
    if len(chosen) != len(set(chosen)):
        raise InvalidModelOutput("Duplicate cause references")
    for cause in causes.candidates:
        if cause.manual_id not in by_id or not set(cause.evidence_ids) <= evidence_ids:
            raise InvalidModelOutput("Unknown cause/evidence reference")
        if not any(ref.startswith(("current:", "history:")) for ref in cause.evidence_ids):
            raise InvalidModelOutput("Cause requires measured evidence")
    pairs = [(p.manual_id, p.guide_index) for p in plan]
    if len(pairs) != len(set(pairs)):
        raise InvalidModelOutput("Duplicate maintenance references")
    for step in plan:
        if step.manual_id not in chosen or step.guide_index >= len(by_id[step.manual_id]["guide"]):
            raise InvalidModelOutput("Unknown maintenance reference")
    if set(chosen) != {p.manual_id for p in plan}:
        raise InvalidModelOutput("Plan must cover every candidate")


@asynccontextmanager
async def scoped_gateway(gateway):
    if hasattr(gateway, "analysis_session"):
        async with gateway.analysis_session() as session:
            yield session
    else:
        yield gateway


class Pipeline:
    def __init__(self, settings: Settings, llm=None):
        self.settings = settings
        self.slots = asyncio.Semaphore(settings.max_concurrent_analyses)
        self.llm = llm or (OpenAIAdapter(settings) if settings.llm_mode == "openai" else OllamaAdapter(settings) if settings.llm_mode == "ollama" else None)
        self.store = RunStore(settings.database_path)
        # Explicit opt-in; redact node state, sensor input, prompts and completions.
        self.trace_client = Client(api_key=settings.langsmith_api_key.get_secret_value(), api_url=settings.langsmith_endpoint,
                                   timeout_ms=5000,
                                   workspace_id=settings.langsmith_workspace_id or None,
                                   hide_inputs=True, hide_outputs=True) if settings.langsmith_tracing else None

    async def run(self, request, run_id, gateway, queue):
        started = perf_counter()
        metrics = RunMetrics()
        try:
            trace_id = UUID(run_id)
        except ValueError:
            trace_id = uuid5(NAMESPACE_URL, run_id)
        self.store.begin(run_id, request.equipment_id, self.settings, str(trace_id))
        try:
            async with self.slots:
                self.store.running(run_id)
                async with scoped_gateway(gateway) as session:
                    graph = self.build_graph(run_id, session, queue, metrics)
                    config = {"run_id": trace_id, "run_name": "pump-guard-analysis", "metadata": {"run_id": run_id, "mode": self.settings.llm_mode}, "recursion_limit": 24}
                    with tracing_context(enabled=self.settings.langsmith_tracing, client=self.trace_client,
                                         project_name=self.settings.langsmith_project):
                        state = await graph.ainvoke({"request": request}, config=config)
            metrics.total_ms = (perf_counter() - started) * 1000
            result = state["result"].model_copy(update={"metrics": metrics})
            ConversationStore(self.settings.database_path).save_analysis(result, request.measurements)
            self.store.finish(run_id, "completed", metrics)
            return result
        except asyncio.CancelledError:
            metrics.total_ms = (perf_counter() - started) * 1000
            self.store.finish(run_id, "cancelled", metrics, "CANCELLED")
            raise
        except Exception as exc:
            from .errors import classify_error
            metrics.total_ms = (perf_counter() - started) * 1000
            self.store.finish(run_id, "failed", metrics, classify_error(exc)[1])
            raise

    def build_graph(self, run_id, gateway, queue, metrics):
        async def input_node(state, config):
            args = {"equipment_id": state["request"].equipment_id}
            return {
                "spec": await gateway.call("equipment", "get_equipment", args),
                "operating": await gateway.call("equipment", "get_operating_history", args),
                "maintenance": await gateway.call("equipment", "get_maintenance_history", args),
            }

        async def condition_node(state, config):
            status, missing, findings = evaluate(state["request"], state["spec"])
            return {"status": status, "missing": missing, "findings": findings,
                    "evidence": collect_evidence(state["request"], state["operating"], state["maintenance"])}

        async def cause_node(state, config):
            manuals = []
            choices = CauseSelection(candidates=[])
            if any(f.level for f in state["findings"]):
                query = " ".join("진동" if f.metric == "vibration_mm_s" else "온도" for f in state["findings"] if f.level)
                manuals = (await gateway.call("knowledge", "search_manuals", {"query": query}))["items"]
                if manuals:
                    refs = [f"current:{f.metric}" for f in state["findings"] if f.level]
                    choices = CauseSelection(candidates=[CauseChoice(manual_id=m["id"], evidence_ids=refs) for m in manuals])
            by_id = {m["id"]: m for m in manuals}
            provisional = [GuideChoice(manual_id=c.manual_id, guide_index=0) for c in choices.candidates if c.manual_id in by_id]
            validate_references(choices, provisional, manuals, state["evidence"])
            return {"manuals": manuals, "causes": choices}

        async def maintenance_node(state, config):
            selected = {c.manual_id for c in state["causes"].candidates}
            manuals = [m for m in state["manuals"] if m["id"] in selected]
            plan = []
            if manuals:
                plan = [GuideChoice(manual_id=m["id"], guide_index=i) for m in manuals for i in range(len(m["guide"]))]
            return {"plan": plan}

        async def safety_node(state, config):
            status, missing, findings = evaluate(state["request"], state["spec"])
            validate_references(state["causes"], state["plan"], state["manuals"], state["evidence"])
            by_id = {m["id"]: m for m in state["manuals"]}
            details = [CauseDetail(manual_id=c.manual_id, fault=by_id[c.manual_id]["fault"],
                                   source=by_id[c.manual_id]["source"], evidence_ids=c.evidence_ids) for c in state["causes"].candidates]
            plan = [MaintenanceStep(manual_id=p.manual_id, guide_index=p.guide_index,
                                    instruction=by_id[p.manual_id]["guide"][p.guide_index], source=by_id[p.manual_id]["source"]) for p in state["plan"]]
            return {"result": AnalysisResult(
                run_id=run_id, equipment_id=state["request"].equipment_id,
                mode=self.settings.llm_mode, model=self.settings.active_model if self.llm else None,
                status=status, missing_fields=missing, findings=findings,
                candidate_causes=[c.fault for c in details], checklist=list(dict.fromkeys(p.instruction for p in plan)),
                cause_details=details, maintenance_plan=plan, evidence=state["evidence"],
                safety_notice="개발용 데모입니다. 설비·이력·정비지식은 합성 예제이며 판정 기준은 미승인 예시입니다. 압력·유량·전류의 위험 판정은 미구현입니다. 이력 수치 비교는 고장 추세나 확률을 검증하지 않습니다. 고장 후보와 근거의 관련성은 담당자 검토가 필요하며 정상 표시도 운전 안전을 보증하지 않습니다. 실제 운전·정비 결정에는 승인된 기준과 담당자 검토가 필요합니다.",
            )}

        nodes = [input_node, condition_node, cause_node, maintenance_node, safety_node]
        if self.llm:
            from .agents import build_agent_graph
            return build_agent_graph(self, run_id, gateway, queue, metrics, safety_node)
        graph = StateGraph(AnalysisState)
        for index, ((stage, message), node) in enumerate(zip(STAGES, nodes), 1):
            def wrap(fn, stage, message, index):
                async def measured(state, config: RunnableConfig):
                    event = {"stage": stage, "step": index, "total": 5}
                    await queue.put(("stage", {**event, "state": "started", "message": message}))
                    start = perf_counter()
                    update = await fn(state, config)
                    metrics.stage_ms[stage] = (perf_counter() - start) * 1000
                    await queue.put(("stage", {**event, "state": "completed", "message": "안전 검수 완료" if stage == "safety" else "단계 완료"}))
                    return update
                return measured
            graph.add_node(stage, wrap(node, stage, message, index))
        order = [START] + [stage for stage, _ in STAGES] + [END]
        for first, second in zip(order, order[1:]):
            graph.add_edge(first, second)
        return graph.compile()


async def run_pipeline(request: AnalysisRequest, run_id: str, gateway, queue: asyncio.Queue, runtime=None):
    return await (runtime or Pipeline(getattr(gateway, "settings", Settings()))).run(request, run_id, gateway, queue)
