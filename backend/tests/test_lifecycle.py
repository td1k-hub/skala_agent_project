from datetime import datetime, timedelta, timezone
import json
import sqlite3

import httpx

from pump_guard.config import Settings
from pump_guard.lifecycle import forecast
from pump_guard.main import create_app


def series(start, values, days=7):
    return [{"observed_at": (start + timedelta(days=i * days)).isoformat(), "value": float(value)} for i, value in enumerate(values)]


def test_projection_is_bounded_and_flags_sparse_countertrend_and_crossed():
    now = datetime.now(timezone.utc)
    rows = series(now - timedelta(days=35), [1, 2, 3, 4, 5, 6])
    projected = forecast(rows, 7.0, "above", [], now)
    assert projected["state"] == "projected"
    assert projected["days_to_threshold"] == 7.0
    assert projected["next_review_at"]
    assert forecast(rows[:3], 7.0, "above", [], now)["state"] == "insufficient_history"
    assert forecast(rows, 20.0, "above", [], now)["state"] == "outside_horizon"
    assert forecast(rows, 5.0, "above", [], now)["state"] == "threshold_reached"
    assert forecast(series(now - timedelta(days=35), [6, 5, 4, 3, 2, 1]), 7.0, "above", [], now)["state"] == "uncertain_trend"
    assert forecast(series(now - timedelta(days=100), [1, 2, 3, 4, 5, 6]), 7.0, "above", [], now)["state"] == "stale"


def test_replacement_review_requires_crossed_user_limit_and_repeated_repairs():
    now = datetime.now(timezone.utc)
    rows = series(now - timedelta(days=35), [1, 2, 3, 4, 5, 6])
    checks = [{"outcome": "repair_required"}, {"outcome": "repair_required"}]
    crossed = forecast(rows, 5.0, "above", checks, now)
    assert any("수리와 교체 대안을 비교" in item for item in crossed["plan"])
    assert any("교체를 결정하지 않습니다" in item for item in crossed["plan"])
    projected = forecast(rows, 7.0, "above", checks, now)
    assert not any("수리와 교체 대안을 비교" in item for item in projected["plan"])
    one_check = forecast(rows, 5.0, "above", checks[:1], now)
    assert not any("수리와 교체 대안을 비교" in item for item in one_check["plan"])


async def test_import_persists_only_per_asset_metric_and_uses_inspection(tmp_path):
    app = create_app(Settings(_env_file=None, database_path=tmp_path / "lifecycle.sqlite3"))
    now = datetime.now(timezone.utc)
    rows = series(now - timedelta(days=35), [1, 2, 3, 4, 5, 6])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = "/api/v1/lifecycle/readings"
        payload = {"asset_id": "SITE-PUMP-1", "metric": "vibration_mm_s", "readings": rows}
        assert (await client.post(url, json=payload)).status_code == 201
        extra = {"asset_id": "SITE-PUMP-1", "metric": "vibration_mm_s", "reading": {"observed_at": (now + timedelta(minutes=1)).isoformat(), "value": 6.5}}
        assert (await client.post(url + '/append', json=extra)).status_code == 201
        assert (await client.post(url + '/append', json=extra)).status_code == 409
        history = (await client.get(url, params={"asset_id": "SITE-PUMP-1", "metric": "vibration_mm_s"})).json()
        assert len(history["items"]) == 7 and history["source"] == "user_supplied"
        assert (await client.get(url, params={"asset_id": "SITE-PUMP-2", "metric": "vibration_mm_s"})).json()["items"] == []
        assert (await client.get(url, params={"asset_id": "SITE-PUMP-1", "metric": "flow_m3_h"})).json()["items"] == []
        inspection = {"asset_id": "SITE-PUMP-1", "observed_at": now.isoformat(), "outcome": "repair_required", "note": "실측 진동 재확인"}
        assert (await client.post('/api/v1/lifecycle/inspections', json=inspection)).status_code == 201
        result = (await client.post('/api/v1/lifecycle/forecast', json={"asset_id": "SITE-PUMP-1", "metric": "vibration_mm_s", "direction": "above", "threshold": 7.0})).json()
        assert result["state"] == "projected" and result["last_inspection"]["outcome"] == "repair_required"
        assert "잔여 수명이 아닙니다" in result["meaning"]
        assert "미해결" in result["plan"][0]
        bad = {**payload, "readings": rows + [rows[0]]}
        assert (await client.post(url, json=bad)).status_code == 422
        assert len((await client.get(url, params={"asset_id": "SITE-PUMP-1", "metric": "vibration_mm_s"})).json()["items"]) == 7
        bad = {**payload, "readings": [{"observed_at": now.isoformat(), "value": 101.0}]}
        assert (await client.post(url, json=bad)).status_code == 422


async def test_existing_pump_inspection_is_visible_without_treating_synthetic_history_as_readings(tmp_path):
    path = tmp_path / "existing.sqlite3"
    now = datetime.now(timezone.utc).isoformat()
    old = {"id": "old-1", "equipment_id": "P-101", "created_at": now, "outcome": "repair_required", "note": "누유 재점검"}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE inspections (id TEXT PRIMARY KEY, equipment_id TEXT, created_at TEXT, payload TEXT)")
        db.execute("INSERT INTO inspections VALUES (?,?,?,?)", (old["id"], old["equipment_id"], now, json.dumps(old)))
    app = create_app(Settings(_env_file=None, database_path=path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        checks = (await client.get('/api/v1/lifecycle/inspections', params={"asset_id": "P-101"})).json()["items"]
        assert checks[0]["note"] == "누유 재점검"
        result = (await client.post('/api/v1/lifecycle/forecast', json={"asset_id": "P-101", "metric": "vibration_mm_s", "direction": "above", "threshold": 7.0})).json()
        assert result["state"] == "insufficient_history"
        assert result["last_inspection"]["id"] == "old-1"
        assert result["sample_count"] == 0
