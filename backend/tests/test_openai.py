import json

import httpx
import pytest

from pump_guard.config import Settings
from pump_guard.agents import RoleAgent
from pump_guard.llm import OpenAIAdapter, OpenAIChatModel
from pump_guard.main import create_app
from pump_guard.pipeline import Pipeline
from test_step2 import EXAMPLE, CAUSE, PLAN, default_agent_turn


def reply(data, *, status="completed"):
    return httpx.Response(200, json={"status": status, "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(data)}]}],
                                     "usage": {"input_tokens": 120, "output_tokens": 40}})


async def test_openai_keeps_first_structured_decision_when_output_has_two_messages():
    role = RoleAgent("condition", None, 4)
    first = {"action": "tool", "tool_calls": [
        {"name": "evaluate_rules", "query": None},
        {"name": "compare_history", "query": None}], "output": None}
    premature = {"action": "finish", "tool_calls": [], "output": {
        "status": "정상", "missing_fields": [], "evidence_ids": []}}
    def handler(request):
        return httpx.Response(200, json={"status": "completed", "output": [
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(first)}]},
            {"type": "message", "content": [{"type": "output_text", "text": json.dumps(premature)}]},
        ], "usage": {"input_tokens": 120, "output_tokens": 87}})
    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only")
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(handler))
    decision, _ = await OpenAIAdapter(settings, model=model).generate(
        "condition", role.schema, {"observations": []}, {}, system_prompt="Return one JSON decision")
    assert decision.action == "tool"
    assert [call.name for call in decision.tool_calls] == ["evaluate_rules", "compare_history"]


async def test_openai_responses_runs_analysis_and_chat_with_grounded_contract(tmp_path):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert request.headers["authorization"] == "Bearer sk-test-only"
        assert body["store"] is False and body["model"] == "gpt-5.4-mini"
        schema = body["text"]["format"]["schema"]
        assert body["text"]["format"]["strict"] is True
        assert set(schema["required"]) == set(schema["properties"])
        assert schema["additionalProperties"] is False
        prompt = json.loads(body["input"][0]["content"])
        stage, context = prompt["task"], prompt["context"]
        calls.append(stage)
        if stage.startswith("chat_"):
            if stage == "chat_router":
                return reply({"intent": "equipment", "tools": ["get_equipment"], "query": None})
            if stage == "chat_answer":
                if "정비 계획" in context["question"]:
                    return reply({"sections": [{"text": "저장된 분석의 점검 항목과 최근 점검 기록을 확인해 정비 계획을 검토하세요. 실측 이력은 현재 등록되지 않았습니다.", "source_ids": [source["id"] for source in context["sources"]]}],
                                  "analysis_status": context["canonical_status"], "follow_up_questions": []})
                return reply({"sections": [{"text": "등록된 설비 사양을 확인했습니다.", "source_ids": [context["sources"][0]["id"]]}],
                              "analysis_status": None, "follow_up_questions": []})
            if stage == "chat_review":
                return reply({"verdict": "approve", "reason": "supported"})
        automatic = default_agent_turn(stage, context)
        if automatic is None:
            automatic = {"action": "finish", "tool_calls": [], "output": CAUSE if stage == "cause" else PLAN}
        return reply(automatic)

    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only",
                        database_path=tmp_path / "openai.sqlite3", analysis_timeout_seconds=20)
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(handler))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OpenAIAdapter(settings, model=model))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        health = (await client.get("/health")).json()
        assert health["mode"] == "openai" and health["provider_configured"] is True
        result = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert result.status_code == 200, result.text
        data = result.json()
        assert data["mode"] == "openai" and data["model"] == "gpt-5.4-mini"
        assert data["status"] == "주의" and data["architecture"] == "multi_agent"
        assert data["metrics"]["llm_calls"][0]["input_tokens"] == 120
        session = (await client.post("/api/v1/chat/sessions", json={"equipment_id": "P-101"})).json()["id"]
        chat = await client.post(f"/api/v1/chat/sessions/{session}/messages", json={"message": "설비 사양을 알려줘"})
        assert chat.status_code == 200, chat.text
        assert chat.json()["mode"] == "openai" and "사양" in chat.json()["answer"]
        linked = (await client.post("/api/v1/chat/sessions", json={"equipment_id": "P-101", "analysis_run_id": data["run_id"]})).json()["id"]
        plan = await client.post(f"/api/v1/chat/sessions/{linked}/messages", json={"message": "이 분석 결과로 정비 계획을 세워줘"})
        assert plan.status_code == 200, plan.text
        assert plan.json()["mode"] == "openai"
        assert {source["kind"] for source in plan.json()["sources"]} == {"analysis", "maintenance_history", "analysis_history"}
        assert "chat_router" in calls and "chat_review" in calls


async def test_missing_openai_key_is_clear_and_does_not_call_provider(tmp_path):
    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key=None,
                        database_path=tmp_path / "missing.sqlite3", analysis_timeout_seconds=20)
    app = create_app(settings)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/health")).json()["provider_configured"] is False
        result = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "OPENAI_KEY_MISSING"


async def test_openai_auth_error_is_sanitized(tmp_path):
    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only",
                        database_path=tmp_path / "auth.sqlite3", analysis_timeout_seconds=20)
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "PRIVATE_PROVIDER_DETAIL"})))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OpenAIAdapter(settings, model=model))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        result = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "OPENAI_AUTH_FAILED"
        assert "PRIVATE_PROVIDER_DETAIL" not in result.text
        assert "sk-test-only" not in result.text


@pytest.mark.parametrize("failure_stage", ["input", "condition"])
async def test_retrieval_or_condition_refusal_uses_trusted_tool_fallback(tmp_path, failure_stage):
    """A failed model response must not prevent deterministic trusted-tool steps."""
    def handler(request):
        body = json.loads(request.content)
        prompt = json.loads(body["input"][0]["content"])
        stage, context = prompt["task"], prompt["context"]
        if stage == failure_stage and not context["observations"]:
            return httpx.Response(200, json={
                "status": "completed",
                "output": [{"type": "message", "content": [{"type": "refusal", "refusal": "Declined"}]}],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            })
        turn = default_agent_turn(stage, context)
        if turn is None:
            turn = {"action": "finish", "tool_calls": [], "output": CAUSE if stage == "cause" else PLAN}
        return reply(turn)

    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only",
                        database_path=tmp_path / "fallback.sqlite3", analysis_timeout_seconds=20)
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(handler))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OpenAIAdapter(settings, model=model))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "주의"
        assert result["decision_supported"] is False
        report = result["agent_reports"][0 if failure_stage == "input" else 1]
        assert report["decision"] == ("source_fallback" if failure_stage == "input" else "rule_fallback")
        assert report["tools"] == (["get_equipment", "get_operating_history", "get_maintenance_history"]
                                   if failure_stage == "input" else ["evaluate_rules", "compare_history"])
        assert any(call["stage"] == failure_stage and call["state"] == "failed"
                   for call in result["metrics"]["llm_calls"])


async def test_condition_unknown_evidence_is_replaced_from_observed_sources(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        prompt = json.loads(body["input"][0]["content"])
        stage, context = prompt["task"], prompt["context"]
        if stage == "condition" and context["observations"]:
            rule = next(item["result"] for item in context["observations"]
                        if item["tool"] == "evaluate_rules")
            return reply({"action": "finish", "tool_calls": [], "output": {
                "status": rule["status"], "missing_fields": rule["missing_fields"],
                "evidence_ids": ["invented:measurement"]}})
        turn = default_agent_turn(stage, context)
        if turn is None:
            turn = {"action": "finish", "tool_calls": [], "output": CAUSE if stage == "cause" else PLAN}
        return reply(turn)

    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only",
                        database_path=tmp_path / "evidence.sqlite3", analysis_timeout_seconds=20)
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(handler))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OpenAIAdapter(settings, model=model))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["status"] == "주의"
        assert result["agent_reports"][1]["decision"] == "evidence_fallback"
        assert "invented:measurement" not in json.dumps(result)


async def test_condition_uses_trusted_rules_when_model_finishes_early(tmp_path):
    def handler(request):
        prompt = json.loads(json.loads(request.content)["input"][0]["content"])
        stage, context = prompt["task"], prompt["context"]
        if stage == "condition":
            return reply({"action": "finish", "tool_calls": [], "output": {
                "status": "정상", "missing_fields": [], "evidence_ids": [],
            }})
        turn = default_agent_turn(stage, context)
        if turn is None:
            turn = {"action": "finish", "tool_calls": [], "output": CAUSE if stage == "cause" else PLAN}
        return reply(turn)

    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only",
                        database_path=tmp_path / "early_finish.sqlite3", analysis_timeout_seconds=20)
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(handler))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OpenAIAdapter(settings, model=model))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "주의"
    assert result["agent_reports"][1]["decision"] == "rule_fallback"
    assert set(result["agent_reports"][1]["tools"]) == {"evaluate_rules", "compare_history"}


async def test_condition_ignores_unused_query_on_fixed_tools(tmp_path):
    def handler(request):
        prompt = json.loads(json.loads(request.content)["input"][0]["content"])
        stage, context = prompt["task"], prompt["context"]
        if stage == "condition" and not context["observations"]:
            return reply({"action": "tool", "tool_calls": [
                {"name": "evaluate_rules", "query": "진동 확인"},
                {"name": "compare_history", "query": "이력 비교"}], "output": None})
        turn = default_agent_turn(stage, context)
        if turn is None:
            turn = {"action": "finish", "tool_calls": [], "output": CAUSE if stage == "cause" else PLAN}
        return reply(turn)

    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only",
                        database_path=tmp_path / "unused_query.sqlite3", analysis_timeout_seconds=20)
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(handler))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OpenAIAdapter(settings, model=model))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "주의"
    assert response.json()["agent_reports"][1]["tools"] == ["evaluate_rules", "compare_history"]


async def test_maintenance_receives_selected_guides_before_first_model_turn(tmp_path):
    def handler(request):
        body = json.loads(request.content)
        prompt = json.loads(body["input"][0]["content"])
        stage, context = prompt["task"], prompt["context"]
        if stage == "maintenance":
            assert any(item["tool"] == "get_selected_guides" for item in context["observations"])
            return reply({"action": "finish", "tool_calls": [], "output": {
                "steps": [{"manual_id": candidate["manual_id"], "guide_index": 0}
                          for candidate in context["causes"]["candidates"]]}})
        turn = default_agent_turn(stage, context)
        if turn is None:
            turn = {"action": "finish", "tool_calls": [], "output": CAUSE}
        return reply(turn)

    settings = Settings(_env_file=None, llm_mode="openai", openai_api_key="sk-test-only",
                        database_path=tmp_path / "guides.sqlite3", analysis_timeout_seconds=20)
    model = OpenAIChatModel(model=settings.openai_model, base_url=settings.openai_base_url,
                            api_key=settings.openai_api_key, transport=httpx.MockTransport(handler))
    app = create_app(settings)
    app.state.pipeline = Pipeline(settings, llm=OpenAIAdapter(settings, model=model))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses", json=EXAMPLE)
        assert response.status_code == 200, response.text
        report = response.json()["agent_reports"][3]
        assert report["agent"] == "maintenance"
        assert report["tools"] == ["get_selected_guides"]
        assert report["turns"] == 1
        review = response.json()["agent_reports"][4]
        assert review["tools"] == ["validate_proposal"]
        assert review["turns"] == 1
