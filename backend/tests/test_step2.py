import asyncio
import json
import inspect
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from pump_guard.config import Settings
from pump_guard.data import MANUALS
from pump_guard.gateway import Gateway
from pump_guard.llm import OllamaAdapter, OllamaChatModel
from pump_guard.main import create_app
from pump_guard.models import AnalysisRequest
from pump_guard.pipeline import Pipeline, run_pipeline

EXAMPLE = json.loads((Path(__file__).parents[1] / "example-request.json").read_text())
CAUSE = {"candidates": [{"manual_id": "KB-DEMO-01", "evidence_ids": ["current:vibration_mm_s", "history:bearing_temperature_c"]}]}
PLAN = {"steps": [{"manual_id": "KB-DEMO-01", "guide_index": 0}, {"manual_id": "KB-DEMO-01", "guide_index": 1}]}


def make_app(tmp_path, handler):
    settings = Settings(_env_file=None, llm_mode="ollama", database_path=tmp_path / "test.sqlite3", analysis_timeout_seconds=10)
    async def routed(request):
        prompt = json.loads(json.loads(request.content)["messages"][1]["content"])
        context, stage = prompt["context"], prompt["task"]
        automatic = default_agent_turn(stage, context)
        if automatic is not None:
            return respond(automatic)
        response = handler(request)
        if inspect.isawaitable(response):
            response = await response
        if response.status_code == 200:
            try:
                payload = response.json()
                output = json.loads(payload["message"]["content"])
                if isinstance(output, dict):
                    payload["message"]["content"] = json.dumps({"action": "finish", "tool_calls": [], "output": output})
                    return httpx.Response(200, json=payload)
            except (ValueError, KeyError, TypeError):
                pass
        return response
    model = OllamaChatModel(model=settings.ollama_model, base_url=settings.ollama_base_url,
                            transport=httpx.MockTransport(routed))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OllamaAdapter(settings, model=model))
    return app, settings


def default_agent_turn(stage, context):
    seen = {o["tool"]: o["result"] for o in context["observations"]}
    def tools(*names):
        return {"action": "tool", "tool_calls": [{"name": n, "query": "진동 온도" if n == "search_manuals" else None} for n in names], "output": None}
    def finish(output):
        return {"action": "finish", "tool_calls": [], "output": output}
    if stage == "input":
        return tools("get_equipment", "get_operating_history", "get_maintenance_history") if not seen else finish({"equipment_id": context["equipment_id"], "next_agent": "condition"})
    if stage == "condition":
        if not seen:
            return tools("evaluate_rules", "compare_history")
        return finish({"status": seen["evaluate_rules"]["status"], "missing_fields": seen["evaluate_rules"]["missing_fields"], "evidence_ids": [e["id"] for e in seen["compare_history"]]})
    if stage == "cause":
        if not any(f["level"] for f in context["findings"]):
            return finish({"candidates": []})
        return tools("search_manuals") if not seen else None
    if stage == "maintenance":
        if not context["causes"]["candidates"]:
            return finish({"steps": []})
        return tools("get_selected_guides") if not seen else None
    if stage == "safety":
        return tools("validate_proposal") if not seen else finish({"verdict": "approve", "reason": "supported"})


def respond(data, **extra):
    return httpx.Response(200, json={"message": {"content": json.dumps(data)}, "done": True,
                                     "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 40, **extra})


async def test_ollama_transport_graph_mcp_rest_and_sse(tmp_path):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/api/chat"
        assert body["think"] is False and body["stream"] is False
        assert body["model"] == "qwen3:8b" and body["options"]["temperature"] == 0
        assert body["format"]["additionalProperties"] is False
        prompt = json.loads(body["messages"][1]["content"])
        calls.append(prompt)
        return respond(CAUSE if prompt["task"] == "cause" else PLAN)
    app, _ = make_app(tmp_path, handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        rest = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert rest.status_code == 200, rest.text
        result = rest.json()
        assert result["status"] == "주의" and result["mode"] == "ollama"
        assert result["decision_supported"] is False
        assert result["checklist"] == MANUALS[0]["guide"]
        assert result["candidate_causes"] == ["베어링 마모"]
        assert result["cause_details"][0]["manual_id"] == "KB-DEMO-01"
        assert len(result["metrics"]["stage_ms"]) == 5
        assert [c["input_tokens"] for c in result["metrics"]["llm_calls"]] == [120] * 8
        assert result["architecture"] == "multi_agent"
        assert [r["agent"] for r in result["agent_reports"]] == ["input", "condition", "cause", "maintenance", "safety"]
        stream = await client.post("/api/v1/analyses/stream", json=EXAMPLE)
        blocks = [b for b in stream.text.split("\n\n") if b.startswith("id:")]
        assert len(blocks) == 12 and "event: result" in blocks[-1]
        assert '"mode": "ollama"' in blocks[0]
        assert '"candidate_causes": ["베어링 마모"]' in blocks[-1]
    assert [c["task"] for c in calls] == ["cause", "maintenance", "cause", "maintenance"]
    evidence = calls[0]["context"]["evidence"]
    assert any(e["id"] == "history:vibration_mm_s" and "현재−마지막 이력" in e["observation"] for e in evidence)


@pytest.mark.parametrize("bad_cause,bad_plan", [
    ({"candidates": [{"manual_id": "FAKE", "evidence_ids": ["current:vibration_mm_s"]}]}, None),
    ({"candidates": [{"manual_id": "KB-DEMO-01", "evidence_ids": ["FAKE"]}]}, None),
    ({"candidates": [{"manual_id": "KB-DEMO-01", "evidence_ids": ["maintenance:empty"]}]}, None),
    ({**CAUSE, "status": "정상"}, None),
    (None, {"steps": [{"manual_id": "KB-DEMO-01", "guide_index": 29}]}),
    (None, {"steps": [{"manual_id": "KB-DEMO-02", "guide_index": 0}]}),
    (None, {"steps": []}),
])
async def test_reject_unverifiable_or_unsafe_output(tmp_path, bad_cause, bad_plan):
    def handler(request):
        stage = json.loads(json.loads(request.content)["messages"][1]["content"])["task"]
        return respond((bad_cause if bad_cause is not None else CAUSE) if stage == "cause" else (bad_plan if bad_plan is not None else PLAN))
    app, _ = make_app(tmp_path, handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert response.status_code == 502, response.text
        assert response.json()["error"]["code"] == "INVALID_MODEL_OUTPUT"
        stream = await client.post("/api/v1/analyses/stream", json=EXAMPLE)
        assert "INVALID_MODEL_OUTPUT" in stream.text and "event: result" not in stream.text
        assert "FAKE" not in stream.text


@pytest.mark.parametrize("upstream,code,status", [
    (httpx.Response(404, json={"error": "PRIVATE_MODEL_ERROR"}), "OLLAMA_MODEL_UNAVAILABLE", 503),
    (httpx.Response(429, json={"error": "PRIVATE_MODEL_ERROR"}), "PROVIDER_RATE_LIMIT", 503),
    (httpx.Response(500, text="PRIVATE_MODEL_ERROR"), "UPSTREAM_UNAVAILABLE", 503),
    (httpx.Response(200, json={"done": True, "message": {"content": "PRIVATE_MODEL_ERROR"}}), "INVALID_MODEL_OUTPUT", 502),
    (respond(CAUSE, done_reason="length"), "INVALID_MODEL_OUTPUT", 502),
    (httpx.Response(200, json=[]), "INVALID_MODEL_OUTPUT", 502),
    (httpx.ReadTimeout("PRIVATE_MODEL_ERROR"), "UPSTREAM_TIMEOUT", 504),
    (httpx.ConnectError("PRIVATE_MODEL_ERROR"), "UPSTREAM_UNAVAILABLE", 503),
])
async def test_ollama_failures_are_sanitized(tmp_path, upstream, code, status):
    def handler(request):
        if isinstance(upstream, Exception):
            raise upstream
        return upstream
    app, _ = make_app(tmp_path, handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert response.status_code == status, response.text
        assert response.json()["error"]["code"] == code
        assert "PRIVATE_MODEL_ERROR" not in response.text


async def test_empty_candidates_cannot_downgrade_high_risk(tmp_path):
    app, _ = make_app(tmp_path, lambda request: respond({"candidates": []}))
    payload = json.loads(json.dumps(EXAMPLE))
    payload["measurements"]["vibration_mm_s"] = 8.0
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=payload)
    assert response.status_code == 200
    assert response.json()["status"] == "점검 필요"
    assert response.json()["candidate_causes"] == []
    assert len(response.json()["metrics"]["llm_calls"]) == 8


async def test_missing_data_stays_unknown_without_invented_candidates(tmp_path):
    def handler(request):
        pytest.fail("No cause or maintenance selection is needed without abnormal observed values")
    app, _ = make_app(tmp_path, handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json={"equipment_id": "P-101", "measurements": {}})
    assert response.status_code == 200
    assert response.json()["status"] is None and len(response.json()["missing_fields"]) == 6
    assert response.json()["candidate_causes"] == []
    assert response.json()["architecture"] == "multi_agent"


async def test_cancel_closes_long_lived_mcp_and_releases_llm_slot(tmp_path):
    started, cancelled = asyncio.Event(), asyncio.Event()
    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()
    app, settings = make_app(tmp_path, handler)
    gateway = Gateway(settings)
    original = gateway.session
    opened, closed = [], []
    @asynccontextmanager
    async def tracked(server):
        async with original(server) as session:
            opened.append(server)
            try:
                yield session
            finally:
                closed.append(server)
    gateway.session = tracked
    task = asyncio.create_task(run_pipeline(AnalysisRequest.model_validate(EXAMPLE), "cancel-run", gateway, asyncio.Queue(), app.state.pipeline))
    await asyncio.wait_for(started.wait(), 5)
    assert opened == ["equipment", "knowledge"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set() and closed == ["knowledge", "equipment"]
    recorded = app.state.pipeline.store.detail("cancel-run")
    assert recorded["state"] == "cancelled"
    assert recorded["agents"][-1]["decision"] == "cancelled"
    async with asyncio.timeout(1):
        async with app.state.pipeline.slots:
            async with app.state.pipeline.llm.slots:
                pass


async def test_concurrent_runs_keep_separate_state_and_limit_inference(tmp_path):
    active = peak = 0
    async def handler(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.01)
            task = json.loads(json.loads(request.content)["messages"][1]["content"])["task"]
            return respond(CAUSE if task == "cause" else PLAN)
        finally:
            active -= 1
    app, _ = make_app(tmp_path, handler)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.gather(*(client.post("/api/v1/analyses", json={**EXAMPLE, "equipment_id": eid}) for eid in ("P-101", "P-102")))
    assert all(r.status_code == 200 for r in responses)
    assert [r.json()["equipment_id"] for r in responses] == ["P-101", "P-102"]
    assert responses[0].json()["run_id"] != responses[1].json()["run_id"]
    assert peak == 1


async def test_tracing_requires_explicit_opt_in(tmp_path, monkeypatch):
    from langsmith.run_helpers import get_tracing_context
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    def handler(request):
        assert get_tracing_context()["enabled"] is False
        task = json.loads(json.loads(request.content)["messages"][1]["content"])["task"]
        return respond(CAUSE if task == "cause" else PLAN)
    app, _ = make_app(tmp_path, handler)
    assert app.state.pipeline.trace_client is None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
    assert response.status_code == 200


def test_tracing_client_redacts_inputs_and_outputs(monkeypatch):
    import pump_guard.pipeline as pipeline
    captured = {}
    def fake_client(**kwargs):
        captured.update(kwargs)
        return object()
    monkeypatch.setattr(pipeline, "Client", fake_client)
    settings = Settings(_env_file=None, llm_mode="mock", langsmith_tracing=True, langsmith_api_key="test-key")
    Pipeline(settings)
    assert captured["hide_inputs"] is True and captured["hide_outputs"] is True
