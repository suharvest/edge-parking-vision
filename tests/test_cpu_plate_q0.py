"""CPU Q0 parking preset and parity-tool contract checks."""
from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_cpu_plate_preset_uses_production_paths_and_cpu_preprocess():
    cfg = json.loads((ROOT / "platforms/cpu/presets/plate.json").read_text())
    assert cfg["backend"]["name"] == "cpu"
    assert cfg["backend"]["model_path"] == "/models/plate_det_yolox_tiny_640.onnx"
    assert cfg["backend"]["input"] == {"color_order": "bgr", "divide": 1.0}
    assert cfg["backend"]["decoder"] == {"type": "yolox", "num_classes": 1,
                                           "strides": [8, 16, 32],
                                           "grid_center_activation": "none"}
    plate = cfg["app"]["options"]["plate"]
    assert plate["stage2"]["model_path"] == "/models/plate_rec_cn_96x32.onnx"
    assert plate["stage2"]["color"] == "rgb"
    assert plate["stage2"]["scale"] == 0.0039215686
    assert plate["stage2"]["backend"] == "cpu"
    assert "localhost" not in json.dumps(cfg)


def test_cpu_plate_file_fixture_is_explicitly_local():
    cfg = json.loads((ROOT / "tests/fixtures/config.plate.file.json").read_text())
    assert cfg["streams"][0]["url"] == "file:///home/harvest/project/edge-parking-vision/tests/fixtures/plate_q0_60s.mp4"
    assert cfg["native"]["binary"] == "/tmp/retail-vb-root-stage2/vb-runtime"
    assert cfg["backend"]["decoder"] == {"type": "yolox", "num_classes": 1,
                                           "strides": [8, 16, 32],
                                           "grid_center_activation": "none"}
    assert cfg["app"]["options"]["events_out"] == "state/events_out.jsonl"
    assert cfg["app"]["options"]["plate"]["stage2"]["color"] == "rgb"


def test_plate_rec_parity_fixture_has_200_real_png_crops():
    manifest = ROOT / "evaluation/data/parity/plate_rec_cn/crops.jsonl"
    rows = [json.loads(line) for line in manifest.read_text().splitlines() if line.strip()]
    assert len(rows) == 200
    assert all((ROOT / "evaluation/data/parity/plate_rec_cn" / row["image"]).is_file()
               for row in rows)
