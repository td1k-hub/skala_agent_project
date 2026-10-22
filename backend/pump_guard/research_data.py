"""Import UCI 447 matrices without assigning undocumented pump sensor locations.

Run from backend: python -m pump_guard.research_data /path/to/extracted/dataset
Raw files are archived separately; UI traces are one-second averages, not raw samples.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[2] / "research"
DATABASE = ROOT / "processed" / "uci447.sqlite3"
SOURCE_URL = "https://archive.ics.uci.edu/dataset/447/condition+monitoring+of+hydraulic+systems"
SENSORS = {
    **{f"PS{i}": ("압력", "bar", 100) for i in range(1, 7)},
    "EPS1": ("모터 전력", "W", 100),
    **{f"FS{i}": ("유량", "L/min", 10) for i in range(1, 3)},
    **{f"TS{i}": ("온도", "°C", 1) for i in range(1, 5)},
    "VS1": ("진동", "mm/s", 1), "CE": ("가상 냉각 효율", "%", 1),
    "CP": ("가상 냉각 전력", "kW", 1), "SE": ("효율 계수", "%", 1),
}
PROFILE_VALUES = ({3, 20, 100}, {100, 90, 80, 73}, {0, 1, 2}, {130, 115, 100, 90}, {0, 1})
LIMITATION = "유압 시험장치의 과거 실험 자료입니다. 현장 실시간 데이터나 원심펌프 안전 판정 모델의 검증 결과가 아닙니다."


def import_dataset(source: Path, root: Path = ROOT, expected_cycles: int = 2205):
    source = source.resolve()
    names = [f"{key}.txt" for key in SENSORS] + ["profile.txt", "description.txt", "documentation.txt"]
    for name in names:
        if not (source / name).is_file():
            raise ValueError(f"필수 파일 누락: {name}")
    profiles = []
    for line in (source / "profile.txt").read_text().splitlines():
        values = [int(v) for v in line.split()]
        if len(values) != 5 or any(value not in allowed for value, allowed in zip(values, PROFILE_VALUES)):
            raise ValueError("profile.txt 상태 라벨 형식 오류")
        profiles.append(values)
    if len(profiles) != expected_cycles:
        raise ValueError(f"실험 주기 수 오류: {len(profiles)} / {expected_cycles}")

    summaries = [{} for _ in profiles]
    traces = [{} for _ in profiles]
    for key, (_, _, hz) in SENSORS.items():
        count = 0
        with (source / f"{key}.txt").open() as stream:
            for i, line in enumerate(stream):
                if i >= expected_cycles:
                    raise ValueError(f"{key}: 행 개수 초과")
                values = [float(v) for v in line.split()]
                if len(values) != hz * 60 or not all(math.isfinite(v) for v in values):
                    raise ValueError(f"{key}: {i + 1}번째 주기의 샘플 수 또는 값 오류")
                summaries[i][key] = {"mean": sum(values) / len(values), "min": min(values), "max": max(values)}
                traces[i][key] = [sum(values[start:start + hz]) / hz for start in range(0, len(values), hz)]
                count += 1
        if count != expected_cycles:
            raise ValueError(f"{key}: 행 개수 부족")
        print(f"Validated {key}: {count} cycles × {hz * 60} samples", flush=True)

    archive = root / "downloads" / "uci447"
    archive.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in names:
        target = archive / name
        if source / name != target.resolve():
            shutil.copy2(source / name, target)
        with target.open("rb") as stream:
            hashes[name] = hashlib.file_digest(stream, "sha256").hexdigest()

    manifest = {
        "id": "uci447", "title": "Condition monitoring of hydraulic systems",
        "source_url": SOURCE_URL, "doi": "10.24432/C5CW21", "license": "CC BY 4.0",
        "authors": "Nikolai Helwig, Eliseo Pignanelli, Andreas Schütze (2015), ZeMA",
        "cycles": expected_cycles, "cycle_seconds": 60, "sensor_count": len(SENSORS),
        "imported_at": datetime.now(timezone.utc).isoformat(), "sha256": hashes,
        "sensors": [{"id": key, "label": label, "unit": unit, "sampling_hz": hz} for key, (label, unit, hz) in SENSORS.items()],
        "trace_aggregation": "1초 구간 평균 / one-second mean", "limitation": LIMITATION,
        "label_counts": {"pump_leakage": {str(v): sum(p[2] == v for p in profiles) for v in (0, 1, 2)},
                         "stable_flag": {str(v): sum(p[4] == v for p in profiles) for v in (0, 1)}},
        "model_trained": False, "decision_supported": False,
    }
    processed = root / "processed"
    processed.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=processed, suffix=".sqlite3")
    os.close(descriptor)
    try:
        with sqlite3.connect(temporary) as db:
            db.execute("CREATE TABLE metadata (payload TEXT NOT NULL)")
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(manifest, ensure_ascii=False),))
            db.execute("CREATE TABLE cycles (id INTEGER PRIMARY KEY, profile TEXT NOT NULL, summary TEXT NOT NULL, traces TEXT NOT NULL)")
            db.executemany("INSERT INTO cycles VALUES (?, ?, ?, ?)",
                           ((i + 1, json.dumps(p), json.dumps(summaries[i]), json.dumps(traces[i])) for i, p in enumerate(profiles)))
        os.replace(temporary, processed / "uci447.sqlite3")
    finally:
        Path(temporary).unlink(missing_ok=True)
    return manifest


def read_dataset(cycle_id: int | None = None, database: Path = DATABASE):
    if not database.is_file():
        return None
    with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as db:
        if cycle_id is None:
            return json.loads(db.execute("SELECT payload FROM metadata").fetchone()[0])
        row = db.execute("SELECT profile, summary, traces FROM cycles WHERE id = ?", (cycle_id,)).fetchone()
    if row is None:
        return None
    profile, summary, traces = map(json.loads, row)
    return {"cycle_id": cycle_id, "duration_seconds": 60,
            "labels": dict(zip(("cooler_percent", "valve_percent", "pump_leakage", "accumulator_bar", "stable_flag"), profile)),
            "summary": summary, "traces": traces, "decision_supported": False}


def measured_history(cycle_id: int, database: Path = DATABASE):
    """A bounded experiment window, never a fabricated live operating history."""
    metadata = read_dataset(database=database)
    if metadata is None or not 1 <= cycle_id <= metadata["cycles"]:
        raise ValueError("실험 주기 데이터를 찾을 수 없습니다.")
    items = []
    for index in range(max(1, cycle_id - 4), cycle_id + 1):
        cycle = read_dataset(index, database)
        items.append({"cycle_id": index, "labels": cycle["labels"],
                      "means": {key: value["mean"] for key, value in cycle["summary"].items()}})
    return {"equipment_id": "UCI-447", "data_source": "uci447_measured_experiment",
            "source_url": metadata["source_url"], "selected_cycle_id": cycle_id,
            "sensors": metadata["sensors"], "items": items,
            "time_basis": "실험 순서 / experiment order; no calendar timestamps; not live",
            "scope": "These are hydraulic test-rig experiments, not P-101/P-102 readings. Labels are experimental annotations, not model predictions. Changes across cycles can reflect intentionally changed conditions, not progressive damage.",
            "decision_supported": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    result = import_dataset(args.source)
    print(f"Imported {result['cycles']} cycles; {result['sensor_count']} sensors → {DATABASE}")
