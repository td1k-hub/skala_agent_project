import asyncio
import json
from pathlib import Path

import httpx
import pytest

from pump_guard.config import Settings
from pump_guard.gateway import Gateway
from pump_guard.main import create_app
from pump_guard.models import AnalysisRequest
from pump_guard.rules import evaluate

EXAMPLE = json.loads((Path(__file__).parents[1] / "example-request.json").read_text())


async def test_dashboard_entry_html_is_not_cached(tmp_path):
    from fastapi import FastAPI
    from pump_guard.main import DashboardFiles
    (tmp_path / "index.html").write_text("<html>current build</html>")
    static_app = FastAPI()
    static_app.mount("/", DashboardFiles(directory=tmp_path, html=True))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=static_app), base_url="http://test") as client:
        for path in ("/", "/index.html"):
            response = await client.get(path)
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, llm_mode="mock", database_path=tmp_path / "mock.sqlite3", analysis_timeout_seconds=30)


@pytest.fixture
def app(settings):
    return create_app(settings)


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as value:
        yield value


async def test_real_mcp_tools_and_persistence(settings):
    gateway = Gateway(settings)
    async with gateway.session("equipment") as session:
        tools = (await session.list_tools()).tools
        assert {tool.name for tool in tools} == {"list_equipment", "get_equipment", "get_operating_history", "get_maintenance_history", "save_inspection", "get_measured_history"}
    first = await gateway.call("equipment", "save_inspection", {"record": {"equipment_id": "P-101", "checks": ["visual"], "outcome": "follow_up"}})
    # New subprocess/client, same persistent database.
    saved = await Gateway(settings).call("equipment", "get_maintenance_history", {"equipment_id": "P-101"})
    assert saved["items"][0]["id"] == first["id"]
    assert settings.database_path.exists()
    docs = await gateway.call("knowledge", "search_manuals", {"query": "캐비테이션"})
    assert docs["items"][0]["id"] == "KB-DEMO-02"


async def test_rest_and_stream_have_same_safe_result(client):
    rest = await client.post("/api/v1/analyses", json=EXAMPLE)
    assert rest.status_code == 200, rest.text
    assert rest.json()["status"] == "주의"
    assert rest.json()["decision_supported"] is False
    stream = await client.post("/api/v1/analyses/stream", json=EXAMPLE)
    assert stream.headers["content-type"].startswith("text/event-stream")
    blocks = stream.text.strip().split("\n\n")
    events = [(next(line[7:] for line in b.splitlines() if line.startswith("event: ")), json.loads(next(line[6:] for line in b.splitlines() if line.startswith("data: ")))) for b in blocks if not b.startswith(":")]
    assert [name for name, _ in events] == ["started"] + ["stage"] * 10 + ["result"]
    assert [data["stage"] for name, data in events if name == "stage" and data["state"] == "started"] == ["input", "condition", "cause", "maintenance", "safety"]
    result = events[-1][1]
    assert result["status"] == rest.json()["status"]
    assert result["candidate_causes"] == rest.json()["candidate_causes"]
    assert result["mode"] == "mock" and result["decision_supported"] is False


@pytest.mark.parametrize("metric,limits", [("vibration_mm_s", [2, 3.5, 5]), ("bearing_temperature_c", [70, 80, 90])])
def test_rule_boundaries_and_missing_input(metric, limits):
    for level, limit in enumerate(limits, 1):
        payload = json.loads(json.dumps(EXAMPLE))
        payload["measurements"][metric] = limit
        request = AnalysisRequest.model_validate(payload)
        _, _, findings = evaluate(request, {})
        assert next(f.level for f in findings if f.metric == metric) == level
    payload["measurements"].pop("flow_m3_h")
    status, missing, _ = evaluate(AnalysisRequest.model_validate(payload), {})
    assert status is None and "flow_m3_h" in missing


@pytest.mark.parametrize("mutation", [
    lambda p: p.update(prompt="PRIVATE_SECRET_123"),
    lambda p: p["measurements"].update(vibration_mm_s=-1),
    lambda p: p["measurements"].update(vibration_mm_s="nan"),
    lambda p: p["measurements"].update(motor_current_a=True),
])
async def test_input_rejected_without_echoing_payload(client, mutation):
    payload = json.loads(json.dumps(EXAMPLE))
    mutation(payload)
    response = await client.post("/api/v1/analyses/stream", json=payload)
    assert response.status_code == 422
    assert "PRIVATE_SECRET_123" not in response.text
    assert "input" not in response.json()["error"]["fields"][0]


async def test_unknown_equipment_and_oversized_chunked_body(client):
    payload = {**EXAMPLE, "equipment_id": "P-999"}
    response = await client.post("/api/v1/analyses/stream", json=payload)
    assert response.status_code == 404
    async def chunks():
        yield b" " * 10000
        yield b" " * 10000
    response = await client.post("/api/v1/analyses", content=chunks())
    assert response.status_code == 413


async def test_inspection_api(client):
    response = await client.post("/api/v1/inspections", json={"equipment_id": "P-102", "checks": ["visual", "leak"], "outcome": "pass"})
    assert response.status_code == 201
    saved = await client.get("/api/v1/equipment/P-102/maintenance-history")
    assert saved.json()["items"][0]["id"] == response.json()["id"]


async def test_built_dashboard_and_api_coexist(client):
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if not dist.is_dir():
        pytest.skip("Frontend build is not present")
    response = await client.get("/")
    assert response.status_code == 200
    assert 'id="app"' in response.text
    import re
    asset = re.search(r'src="([^"]+\.js)"', response.text).group(1)
    javascript = await client.get(asset)
    assert javascript.status_code == 200
    assert (await client.get("/health")).json()["status"] == "ok"


async def test_timeout_heartbeat_and_cleanup(settings):
    settings.analysis_timeout_seconds = 0.08
    settings.heartbeat_seconds = 0.01
    app = create_app(settings)
    cancelled = asyncio.Event()
    class SlowGateway:
        async def call(self, *args):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
    app.state.gateway = SlowGateway()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/analyses/stream", json=EXAMPLE)
    assert ": heartbeat" in response.text
    assert "event: error" in response.text
    assert "UPSTREAM_TIMEOUT" in response.text
    assert "event: result" not in response.text
    assert cancelled.is_set()


@pytest.mark.parametrize("error,code", [(httpx.ConnectError("PRIVATE_ERROR"), "UPSTREAM_UNAVAILABLE"), (type("RateLimit", (Exception,), {"status_code": 429})("PRIVATE_ERROR"), "PROVIDER_RATE_LIMIT")])
async def test_upstream_error_is_sanitized(app, client, error, code):
    class BrokenGateway:
        async def call(self, *args):
            raise error
    app.state.gateway = BrokenGateway()
    response = await client.post("/api/v1/analyses/stream", json=EXAMPLE)
    assert f'"code": "{code}"' in response.text
    assert "PRIVATE_ERROR" not in response.text
    assert "event: result" not in response.text
    response = await client.post("/api/v1/analyses", json=EXAMPLE)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == code


async def test_disconnect_cancels_worker(settings):
    app = create_app(settings)
    started, cancelled = asyncio.Event(), asyncio.Event()
    class SlowGateway:
        async def call(self, *args):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
    app.state.gateway = SlowGateway()
    body = json.dumps(EXAMPLE).encode()
    received = False
    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": body}
        await started.wait()
        return {"type": "http.disconnect"}
    async def send(message):
        pass
    # ASGI 2.0 uses StreamingResponse's disconnect listener.
    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/v1/analyses/stream", "raw_path": b"/api/v1/analyses/stream", "query_string": b"", "headers": [(b"content-type", b"application/json")], "client": ("127.0.0.1", 1), "server": ("test", 80)}
    await asyncio.wait_for(app(scope, receive, send), 2)
    assert cancelled.is_set()
