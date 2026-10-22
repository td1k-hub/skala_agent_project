import json
from functools import partial

import httpx
import pytest

from pump_guard import main
from pump_guard.config import Settings
from pump_guard.research_data import SENSORS, import_dataset, read_dataset


@pytest.fixture
def dataset(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "profile.txt").write_text("100 100 0 130 0\n20 80 2 90 1\n")
    for name in ("description.txt", "documentation.txt"):
        (source / name).write_text("test documentation")
    for key, (_, _, hz) in SENSORS.items():
        row = " ".join(str(i) for i in range(60 * hz))
        (source / f"{key}.txt").write_text(f"{row}\n{row}\n")
    root = tmp_path / "research"
    return source, root


def test_import_preserves_units_labels_and_one_second_means(dataset):
    source, root = dataset
    manifest = import_dataset(source, root, expected_cycles=2)
    database = root / "processed/uci447.sqlite3"
    metadata = read_dataset(database=database)
    assert metadata == manifest
    assert len(metadata["sha256"]) == 20
    assert next(s for s in metadata["sensors"] if s["id"] == "EPS1")["unit"] == "W"
    cycle = read_dataset(2, database)
    assert cycle["labels"]["pump_leakage"] == 2
    assert cycle["labels"]["stable_flag"] == 1
    assert cycle["traces"]["PS1"][:2] == [49.5, 149.5]
    assert cycle["summary"]["PS1"] == {"mean": 2999.5, "min": 0.0, "max": 5999.0}
    assert cycle["traces"]["VS1"] == list(range(60))
    assert "bearing_temperature_c" not in json.dumps(cycle)
    assert cycle["decision_supported"] is False
    assert read_dataset(3, database) is None
    assert (root / "downloads/uci447/PS1.txt").read_bytes() == (source / "PS1.txt").read_bytes()


@pytest.mark.parametrize("bad_row", ["nan", "0 1", "inf"])
def test_invalid_source_does_not_replace_previously_imported_database(dataset, bad_row):
    source, root = dataset
    import_dataset(source, root, expected_cycles=2)
    database = root / "processed/uci447.sqlite3"
    previous = database.read_bytes()
    (source / "VS1.txt").write_text(bad_row + "\n")
    with pytest.raises(ValueError):
        import_dataset(source, root, expected_cycles=2)
    assert database.read_bytes() == previous


async def test_research_api_is_independent_of_ollama_and_synthetic_equipment(dataset, monkeypatch):
    source, root = dataset
    import_dataset(source, root, expected_cycles=2)
    monkeypatch.setattr(main, "read_dataset", partial(read_dataset, database=root / "processed/uci447.sqlite3"))
    app = main.create_app(Settings(_env_file=None, database_path=root / "app.sqlite3", research_database_path=root / "processed/uci447.sqlite3"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        info = await client.get("/api/v1/research/uci447")
        assert info.status_code == 200 and info.json()["cycles"] == 2
        cycle = await client.get("/api/v1/research/uci447/cycles/2")
        assert cycle.status_code == 200 and cycle.json()["labels"]["pump_leakage"] == 2
        assert (await client.get("/api/v1/research/uci447/cycles/0")).status_code == 404
        assert (await client.get("/api/v1/research/uci447/cycles/3")).status_code == 404
    monkeypatch.setattr(main, "read_dataset", lambda **kwargs: None)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing = await client.get("/api/v1/research/uci447")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "DATASET_NOT_IMPORTED"
