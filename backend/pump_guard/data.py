import json
import sqlite3
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from .config import Settings
from .models import InspectionRequest
from .research_data import read_dataset

PUMPS = {
    "P-101": {"equipment_id": "P-101", "model": "PG-DEMO-100", "type": "centrifugal", "service": "공정 순환", "rated_flow_m3_h": 100.0, "rated_motor_current_a": 40.0, "data_source": "synthetic", "threshold_policy": "demo-v1; 실제 설비 기준 아님"},
    "P-102": {"equipment_id": "P-102", "model": "PG-DEMO-200", "type": "centrifugal", "service": "냉각수 순환", "rated_flow_m3_h": 200.0, "rated_motor_current_a": 70.0, "data_source": "synthetic", "threshold_policy": "demo-v1; 실제 설비 기준 아님"},
}
MANUALS = [
    {"id": "KB-DEMO-01", "fault": "베어링 마모", "symptoms": ["진동 증가", "베어링 온도 상승"], "guide": ["계측값과 센서 상태 확인", "현장 절차에 따라 윤활 및 베어링 상태 점검"], "source": "synthetic training example"},
    {"id": "KB-DEMO-02", "fault": "캐비테이션", "symptoms": ["진동 증가", "흡입 압력 저하", "유량 저하"], "guide": ["흡입 조건과 유체 상태 확인", "제조사 곡선 및 NPSH 조건을 전문 담당자가 검토"], "source": "synthetic training example"},
    {"id": "KB-DEMO-03", "fault": "임펠러 마모", "symptoms": ["유량 저하", "압력 차 저하"], "guide": ["계측기 및 운전점 확인", "격리 및 잠금 절차 후 내부 점검 여부 검토"], "source": "synthetic training example"},
]


def require_pump(equipment_id):
    if equipment_id == "UCI-447":
        return {"equipment_id": "UCI-447", "model": "UCI 447 hydraulic test rig", "type": "hydraulic_test_rig",
                "service": "공개 유압 실험장치", "data_source": "uci447_measured_experiment"}
    if equipment_id not in PUMPS:
        raise KeyError("unknown equipment")
    return PUMPS[equipment_id]


def history(equipment_id):
    require_pump(equipment_id)
    now = datetime.now(timezone.utc)
    return {"data_source": "synthetic", "items": [
        {"timestamp": (now - timedelta(hours=h)).isoformat(), "vibration_mm_s": 1.4 + h * 0.02,
         "bearing_temperature_c": 62.0, "flow_m3_h": PUMPS[equipment_id]["rated_flow_m3_h"]}
        for h in range(6, 0, -1)]}


def connection():
    path = Settings().database_path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=5)
    con.execute("CREATE TABLE IF NOT EXISTS inspections (id TEXT PRIMARY KEY, equipment_id TEXT NOT NULL, created_at TEXT NOT NULL, payload TEXT NOT NULL)")
    return con


def save_inspection(record: InspectionRequest):
    require_pump(record.equipment_id)
    if record.equipment_id == "UCI-447":
        if record.research_cycle_id is None or read_dataset(record.research_cycle_id, Settings().research_database_path) is None:
            raise ValueError("UCI 점검에는 유효한 실험 주기 연결이 필요합니다.")
    elif record.research_cycle_id is not None:
        raise ValueError("실험 주기는 UCI-447에만 연결할 수 있습니다.")
    item = {"id": str(uuid4()), "created_at": datetime.now(timezone.utc).isoformat(), **record.model_dump()}
    con = connection()
    try:
        with con:
            con.execute("INSERT INTO inspections VALUES (?, ?, ?, ?)", (item["id"], record.equipment_id, item["created_at"], json.dumps(item, ensure_ascii=False)))
    finally:
        con.close()
    return item


def maintenance(equipment_id):
    require_pump(equipment_id)
    con = connection()
    try:
        rows = con.execute("SELECT payload FROM inspections WHERE equipment_id=? ORDER BY created_at DESC LIMIT 100", (equipment_id,)).fetchall()
    finally:
        con.close()
    return {"items": [json.loads(row[0]) for row in rows], "limit": 100}
