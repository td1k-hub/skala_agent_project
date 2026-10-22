import httpx

from test_chat import create_session, make_app
from pump_guard.chat import ChatService


async def test_obvious_off_topic_returns_clarification_without_model_call(tmp_path):
    app, _, captured = make_app(tmp_path, ollama=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        session = await create_session(client)
        response = await client.post(f"/api/v1/chat/sessions/{session}/messages", json={"message": "오늘 날씨 알려줘"})
    assert response.status_code == 200
    assert response.json()["response_kind"] == "clarification"
    assert response.json()["sources"] == []
    assert "설비의 측정값" in response.json()["answer"]
    assert captured == []


def test_off_topic_gate_does_not_discard_equipment_context():
    assert not ChatService.obvious_off_topic("오늘 날씨가 펌프 냉각 성능에 영향을 줘?")
    assert not ChatService.obvious_off_topic("그럼 왜 그런 거야?")
    assert ChatService.obvious_off_topic("주식 추천해줘")


def test_trend_summary_includes_all_observed_values_not_just_endpoints():
    rows = [
        {"created_at": "2026-10-03T00:00:00+00:00", "status": "관찰", "measurements": {"vibration_mm_s": 3.0}},
        {"created_at": "2026-10-02T00:00:00+00:00", "status": "주의", "measurements": {"vibration_mm_s": 6.0}},
        {"created_at": "2026-10-01T00:00:00+00:00", "status": "정상", "measurements": {"vibration_mm_s": 1.0}},
    ]
    text = ChatService.analysis_trend_text(rows)
    assert "진동 1→3 mm/s" in text
    assert "관측 범위 1~6 mm/s" in text


async def test_registered_timestamped_readings_are_used_by_assessment(tmp_path):
    app, _, _ = make_app(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        imported = await client.post("/api/v1/lifecycle/readings", json={
            "asset_id": "P-101", "metric": "vibration_mm_s", "readings": [
                {"observed_at": "2026-10-01T09:00:00+09:00", "value": 2.0},
                {"observed_at": "2026-10-08T09:00:00+09:00", "value": 5.0},
                {"observed_at": "2026-10-15T09:00:00+09:00", "value": 3.0},
            ]})
        assert imported.status_code == 201, imported.text
        session = await create_session(client)
        reply = await client.post(f"/api/v1/chat/sessions/{session}/messages", json={"message": "이전 진동 수치의 변화와 위험성을 알려줘"})
    assert reply.status_code == 200, reply.text
    result = reply.json()
    assert "진동 2→3 mm/s" in result["answer"]
    assert "관측 범위 2~5" in result["answer"]
    assert "measurement_history" in {source["kind"] for source in result["sources"]}
