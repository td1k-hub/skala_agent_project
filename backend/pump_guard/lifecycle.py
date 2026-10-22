"""User-supplied chronological readings and bounded maintenance planning."""
import math
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field, field_validator, model_validator

from .models import StrictModel

router = APIRouter(prefix="/api/v1/lifecycle")
METRICS = {
    "vibration_mm_s": ("진동", "mm/s", 0, 100),
    "bearing_temperature_c": ("베어링 온도", "°C", -50, 300),
    "suction_pressure_bar": ("흡입 압력", "bar", -1, 100),
    "discharge_pressure_bar": ("토출 압력", "bar", -1, 200),
    "flow_m3_h": ("유량", "m³/h", 0, 10000),
    "motor_current_a": ("모터 전류", "A", 0, 10000),
}
ASSET = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$"


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is None:
        raise ValueError("측정 시각에는 시간대가 필요합니다.")
    return value.astimezone(timezone.utc)


class Reading(StrictModel):
    observed_at: str
    value: float

    @field_validator("observed_at")
    @classmethod
    def valid_time(cls, value):
        value = utc(value)
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("미래 측정값은 등록할 수 없습니다.")
        return value.isoformat()


class ImportRequest(StrictModel):
    asset_id: str = Field(pattern=ASSET)
    metric: str
    readings: list[Reading] = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def valid_readings(self):
        if self.metric not in METRICS:
            raise ValueError("지원하지 않는 측정 항목입니다.")
        _, _, lower, upper = METRICS[self.metric]
        if any(not lower <= item.value <= upper for item in self.readings):
            raise ValueError("측정값이 허용 입력 범위를 벗어났습니다.")
        dates = [utc(item.observed_at).isoformat() for item in self.readings]
        if len(set(dates)) != len(dates):
            raise ValueError("중복된 측정 시각이 있습니다.")
        return self


class AppendRequest(StrictModel):
    asset_id: str = Field(pattern=ASSET)
    metric: str
    reading: Reading

    @model_validator(mode="after")
    def valid_reading(self):
        if self.metric not in METRICS:
            raise ValueError("지원하지 않는 측정 항목입니다.")
        _, _, lower, upper = METRICS[self.metric]
        if not lower <= self.reading.value <= upper:
            raise ValueError("측정값이 허용 입력 범위를 벗어났습니다.")
        return self


class ForecastRequest(StrictModel):
    asset_id: str = Field(pattern=ASSET)
    metric: str
    threshold: float
    direction: str

    @model_validator(mode="after")
    def valid_limit(self):
        if self.metric not in METRICS or self.direction not in {"above", "below"}:
            raise ValueError("측정 항목 또는 한계 방향을 확인해 주세요.")
        _, _, lower, upper = METRICS[self.metric]
        if not lower <= self.threshold <= upper:
            raise ValueError("한계값이 허용 입력 범위를 벗어났습니다.")
        return self


class Inspection(StrictModel):
    asset_id: str = Field(pattern=ASSET)
    observed_at: str
    outcome: str
    note: str = Field(min_length=1, max_length=1000)

    @field_validator("observed_at")
    @classmethod
    def valid_time(cls, value):
        value = utc(value)
        if value > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("미래 점검은 기록할 수 없습니다.")
        return value.isoformat()

    @field_validator("outcome")
    @classmethod
    def valid_outcome(cls, value):
        if value not in {"pass", "follow_up", "repair_required"}:
            raise ValueError("점검 결과를 확인해 주세요.")
        return value


@contextmanager
def connect(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE IF NOT EXISTS lifecycle_readings (asset_id TEXT NOT NULL, metric TEXT NOT NULL, observed_at TEXT NOT NULL, value REAL NOT NULL, PRIMARY KEY(asset_id, metric, observed_at))")
    db.execute("CREATE TABLE IF NOT EXISTS lifecycle_inspections (id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, observed_at TEXT NOT NULL, outcome TEXT NOT NULL, note TEXT NOT NULL)")
    try:
        yield db
    finally:
        db.close()


def load(db, asset_id, metric):
    return [dict(row) for row in db.execute("SELECT observed_at,value FROM lifecycle_readings WHERE asset_id=? AND metric=? ORDER BY observed_at", (asset_id, metric))]


def load_inspections(db, asset_id):
    items = [dict(row) for row in db.execute("SELECT * FROM lifecycle_inspections WHERE asset_id=?", (asset_id,))]
    if asset_id in {"P-101", "P-102"} and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inspections'").fetchone():
        for row in db.execute("SELECT payload FROM inspections WHERE equipment_id=? ORDER BY created_at DESC LIMIT 100", (asset_id,)):
            old = json.loads(row[0])
            items.append({"id": old["id"], "asset_id": asset_id, "observed_at": old["created_at"],
                          "outcome": old["outcome"], "note": old.get("note", ""), "source": "existing_inspection"})
    return sorted(items, key=lambda item: item["observed_at"], reverse=True)[:100]


def history_summary(path: Path, asset_id: str):
    """Small, explicitly user-supplied history context for plan consultation."""
    with connect(path) as db:
        groups = db.execute("SELECT metric,COUNT(*) AS count FROM lifecycle_readings WHERE asset_id=? GROUP BY metric", (asset_id,)).fetchall()
        items = []
        for group in groups:
            metric = group["metric"]
            if metric not in METRICS:
                continue
            first = db.execute("SELECT observed_at,value FROM lifecycle_readings WHERE asset_id=? AND metric=? ORDER BY observed_at LIMIT 1", (asset_id, metric)).fetchone()
            last = db.execute("SELECT observed_at,value FROM lifecycle_readings WHERE asset_id=? AND metric=? ORDER BY observed_at DESC LIMIT 1", (asset_id, metric)).fetchone()
            observed_range = db.execute("SELECT MIN(value),MAX(value) FROM lifecycle_readings WHERE asset_id=? AND metric=?", (asset_id, metric)).fetchone()
            days = (datetime.fromisoformat(last["observed_at"]) - datetime.fromisoformat(first["observed_at"])).total_seconds() / 86400
            items.append({"metric": metric, "label": METRICS[metric][0], "unit": METRICS[metric][1],
                          "count": group["count"], "first_at": first["observed_at"], "first_value": first["value"],
                          "last_at": last["observed_at"], "last_value": last["value"],
                          "min_value": observed_range[0], "max_value": observed_range[1],
                          "observed_days": round(days, 1), "change": round(last["value"] - first["value"], 4)})
    return {"asset_id": asset_id, "data_source": "user_supplied", "scope": "Time-stamped user-entered readings. Endpoint change is descriptive; no site/OEM limit, operating hours or failure labels are provided. Do not infer equipment remaining life or a replacement date.", "items": items}


def forecast(readings, threshold, direction, inspections, now=None):
    now = now or datetime.now(timezone.utc)
    result = {"state": "insufficient_history", "projected_at": None, "days_to_threshold": None,
              "daily_slope": None, "sample_count": len(readings), "observed_days": None,
              "last_value": readings[-1]["value"] if readings else None,
              "last_observed_at": readings[-1]["observed_at"] if readings else None,
              "next_review_at": None, "plan": [], "last_inspection": inspections[0] if inspections else None}
    if inspections and inspections[0]["outcome"] != "pass":
        result["plan"].append("최근 점검의 미해결 항목과 메모를 먼저 확인하세요.")
    if not readings:
        result["plan"].append("시간대가 포함된 실제 계측 이력을 등록하세요.")
        return result
    points = [(datetime.fromisoformat(r["observed_at"]), r["value"]) for r in readings]
    latest_at, latest_value = points[-1]
    sign = 1 if direction == "above" else -1
    result["plan"].append("같은 운전 조건과 계측 방식으로 추세를 재확인하세요.")
    if sign * (latest_value - threshold) >= 0:
        result["state"] = "threshold_reached"
        result["plan"].insert(0, "입력한 한계값에 도달했습니다. 현장 기준과 계측 상태를 확인하고 담당자가 조치 우선순위를 결정하세요.")
        if sum(item["outcome"] == "repair_required" for item in inspections) >= 2:
            result["plan"].append("정비 필요 기록이 두 건 이상이고 입력한 한계값에도 도달했습니다. 담당자가 제조사 기준과 실제 고장 이력을 확인해 수리와 교체 대안을 비교하세요. 이 결과만으로 교체를 결정하지 않습니다.")
        return result
    span = (points[-1][0] - points[0][0]).total_seconds() / 86400
    result["observed_days"] = round(span, 1)
    if len(points) < 6 or span < 14:
        result["plan"].append("추세 계산에는 최소 6개, 14일 이상의 측정 기록이 필요합니다.")
        return result
    intervals = [(b[0] - a[0]).total_seconds() / 86400 for a, b in zip(points, points[1:])]
    cadence = median(intervals)
    if (now - latest_at).total_seconds() / 86400 > max(7, 2 * cadence):
        result["state"] = "stale"
        result["plan"].append("최근 측정이 오래되어 새 계측 후 추세를 다시 계산해야 합니다.")
        return result
    recent = points[-60:]
    slopes = [(b[1] - a[1]) / ((b[0] - a[0]).total_seconds() / 86400) for i, a in enumerate(recent) for b in recent[i + 1:]]
    slope = median(slopes)
    result["daily_slope"] = round(slope, 6)
    aligned = sum(1 for s in slopes if sign * s > 0) / len(slopes)
    if sign * slope <= 0 or aligned < 0.7:
        result["state"] = "uncertain_trend"
        result["plan"].append("한계값 방향으로 일관된 증가·감소 추세가 확인되지 않습니다.")
        return result
    days = (threshold - latest_value) / slope
    if not math.isfinite(days) or days <= 0 or days > 2 * span:
        result["state"] = "outside_horizon"
        result["plan"].append("관측 기간의 2배를 넘는 외삽은 표시하지 않습니다. 계측을 이어가세요.")
        return result
    projected = latest_at + timedelta(days=days)
    if projected <= now:
        result["state"] = "projection_elapsed"
        result["plan"].append("계산된 도달 시점이 지났습니다. 현재값을 재측정한 뒤 다시 계산하세요.")
        return result
    result.update(state="projected", projected_at=projected.isoformat(), days_to_threshold=round(days, 1))
    review_days = max(1, min(cadence, days / 2))
    result["next_review_at"] = (now + timedelta(days=review_days)).date().isoformat()
    result["plan"].append("예상 도달일보다 앞서 재측정하고 제조사·현장 기준과 정비 가능 일정을 대조하세요.")
    return result


def register_lifecycle_routes(app, settings):
    @app.get("/api/v1/lifecycle/assets")
    def assets():
        with connect(settings.database_path) as db:
            rows = db.execute("SELECT DISTINCT asset_id FROM lifecycle_readings UNION SELECT DISTINCT asset_id FROM lifecycle_inspections ORDER BY asset_id").fetchall()
            result = {row[0] for row in rows}
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='inspections'").fetchone():
                result.update(row[0] for row in db.execute("SELECT DISTINCT equipment_id FROM inspections WHERE equipment_id IN ('P-101','P-102')"))
        return {"items": sorted(result)}

    @app.post("/api/v1/lifecycle/readings", status_code=201)
    def import_readings(body: ImportRequest):
        with connect(settings.database_path) as db:
            with db:
                db.execute("DELETE FROM lifecycle_readings WHERE asset_id=? AND metric=?", (body.asset_id, body.metric))
                db.executemany("INSERT INTO lifecycle_readings VALUES (?,?,?,?)", [(body.asset_id, body.metric, utc(r.observed_at).isoformat(), r.value) for r in body.readings])
        return {"asset_id": body.asset_id, "metric": body.metric, "count": len(body.readings), "source": "user_supplied"}

    @app.post("/api/v1/lifecycle/readings/append", status_code=201)
    def append_reading(body: AppendRequest):
        try:
            with connect(settings.database_path) as db:
                with db:
                    db.execute("INSERT INTO lifecycle_readings VALUES (?,?,?,?)", (body.asset_id, body.metric, utc(body.reading.observed_at).isoformat(), body.reading.value))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "같은 시각의 측정 기록이 이미 있습니다.")
        return {"asset_id": body.asset_id, "metric": body.metric, "source": "user_supplied"}

    @app.get("/api/v1/lifecycle/readings")
    def readings(asset_id: str = Query(pattern=ASSET), metric: str = Query()):
        if metric not in METRICS:
            raise HTTPException(422, "지원하지 않는 측정 항목입니다.")
        with connect(settings.database_path) as db:
            items = load(db, asset_id, metric)
        return {"items": items, "source": "user_supplied", "unit": METRICS[metric][1]}

    @app.post("/api/v1/lifecycle/inspections", status_code=201)
    def add_inspection(body: Inspection):
        item = {"id": str(uuid4()), "asset_id": body.asset_id, "observed_at": utc(body.observed_at).isoformat(), "outcome": body.outcome, "note": body.note}
        with connect(settings.database_path) as db:
            with db:
                db.execute("INSERT INTO lifecycle_inspections VALUES (:id,:asset_id,:observed_at,:outcome,:note)", item)
        return item

    @app.get("/api/v1/lifecycle/inspections")
    def inspections(asset_id: str = Query(pattern=ASSET)):
        with connect(settings.database_path) as db:
            items = load_inspections(db, asset_id)
        return {"items": items}

    @app.post("/api/v1/lifecycle/forecast")
    def forecast_api(body: ForecastRequest):
        with connect(settings.database_path) as db:
            items = load(db, body.asset_id, body.metric)
            inspections = load_inspections(db, body.asset_id)[:100]
        return {"asset_id": body.asset_id, "metric": body.metric, "unit": METRICS[body.metric][1],
                "threshold": body.threshold, "direction": body.direction, "source": "user_supplied",
                "meaning": "사용자가 입력한 한계값 도달 시점의 제한적 추세 추정이며 설비 전체 잔여 수명이 아닙니다.",
                **forecast(items, body.threshold, body.direction, inspections)}
