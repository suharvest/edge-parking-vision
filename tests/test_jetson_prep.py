"""Offline NRD-2/4 Jetson prep checks; no GPU or engine is claimed here."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name,key,size,engine", [
    ("platforms/jetson/presets/counting.json", "vehicle416", [416, 416], "vehicle416.fp16.engine"),
    ("platforms/jetson/presets/slots.json", "vehicle640", [640, 640], "vehicle640.fp16.engine"),
])
def test_jetson_preset_matches_registered_yolox(name, key, size, engine):
    cfg = _load(name)
    assert cfg["backend"]["name"] == "trt"
    assert cfg["backend"]["model_path"].endswith(engine)
    assert cfg["backend"]["input_size"] == size
    assert cfg["backend"]["input"] == {"color_order": "bgr", "divide": 1.0}
    assert cfg["backend"]["decoder"] == {
        "type": "yolox", "num_classes": 80, "strides": [8, 16, 32],
        "grid_center_activation": "none",
    }
    registry = (ROOT / "models/registry.yaml").read_text(encoding="utf-8")
    assert f"  {key}:" in registry


def test_jetson_decoder_grid_activation_is_accepted_by_base_config():
    try:
        from vision_base.config import load
    except ImportError:
        pytest.skip("retail-vision vision_base is not available")
    for name in ("platforms/jetson/presets/counting.json", "platforms/jetson/presets/slots.json"):
        cfg = load(str(ROOT / name))
        assert cfg.backend["decoder"]["grid_center_activation"] == "none"


def test_slots_preset_has_multistream_rate_and_schema_fields():
    cfg = _load("platforms/jetson/presets/slots.json")
    assert len(cfg["streams"]) == 4
    assert {s["stream_id"] for s in cfg["streams"]} == set(cfg["app"]["options"]["slots"])
    assert all(s["options"]["max_fps"] == 1 for s in cfg["streams"])
    assert cfg["runtime"]["contexts_per_worker"] == 2
    assert cfg["app"]["options"]["slot_coverage"]["classes"] == [2, 5, 7]
    assert cfg["app"]["options"]["state_dir"] == "/data/edge-parking"


def test_counting_preset_uses_persistent_app_state_dir():
    cfg = _load("platforms/jetson/presets/counting.json")
    assert cfg["app"]["options"]["state_dir"] == "/data/edge-parking"


def test_counting_and_slots_file_fixtures_parse_with_base_config(tmp_path, monkeypatch):
    try:
        from vision_base.config import load
    except ImportError:
        pytest.skip("retail-vision vision_base is not available")
    for source in ("tests/fixtures/config.counting.file.json", "tests/fixtures/config.slots.file.json"):
        data = (ROOT / source).read_text(encoding="utf-8")
        data = data.replace("__REPO__", str(ROOT)).replace("__VB_RUNTIME__", "/tmp/retail-vb-cpu-gst/vb-runtime")
        path = tmp_path / Path(source).name
        path.write_text(data, encoding="utf-8")
        cfg = load(str(path))
        assert cfg.backend["input"] == {"color_order": "bgr", "divide": 1.0}
        assert cfg.streams[0]["url"].startswith("file://")


def test_engine_builder_is_shell_valid_and_does_not_use_old_retail_sku():
    script = ROOT / "platforms/jetson/build_engine.sh"
    subprocess.run(["/usr/bin/bash", "-n", str(script)], check=True)
    text = script.read_text(encoding="utf-8")
    assert "vehicle416" in text and "vehicle640" in text
    assert "retail" not in text.lower()


def test_local_cpu_runtime_proof_if_provided():
    runtime = Path("/tmp/retail-vb-cpu-gst/vb-runtime")
    if not runtime.exists():
        pytest.skip("local CPU native runtime not staged")
    assert os.access(runtime, os.X_OK)
    digest = hashlib.sha256(runtime.read_bytes()).hexdigest()
    assert len(digest) == 64
    version = subprocess.run([str(runtime), "--version"], text=True, capture_output=True, check=True)
    assert version.stdout.strip()
