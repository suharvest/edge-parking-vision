"""Regression for mem-guard checkpoint crop metadata."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "training"))
import train_platerec  # noqa: E402


def test_mem_guard_checkpoint_preserves_axis_crop_meta_for_resume(tmp_path, monkeypatch):
    image = tmp_path / "axis.jpg"
    assert cv2.imwrite(str(image), np.full((32, 96, 3), 128, dtype=np.uint8))
    manifest = tmp_path / "axis.json"
    manifest.write_text(json.dumps({"schema": "nrd3.axis-crop/1"}))
    detector_sha = "d" * 64
    boxes = [[2.0, 3.0, 30.0, 20.0]]

    def fake_build_axis_items(*_args, **_kwargs):
        return [str(image)], ["皖A12345"], boxes, 0.08, detector_sha

    class TinyNet(torch.nn.Module):
        def __init__(self, classes):
            super().__init__()
            self.logits = torch.nn.Parameter(torch.zeros(1, classes, 1, 20))

        def forward(self, x):
            return self.logits.expand(x.shape[0], -1, -1, -1)

    monkeypatch.setattr(train_platerec, "build_axis_items", fake_build_axis_items)
    monkeypatch.setattr(train_platerec, "PlateRecNet", TinyNet)
    monkeypatch.setattr(train_platerec, "mem_available_gb", lambda: 999.0)
    monkeypatch.setattr(train_platerec, "rss_gb", lambda: 999.0)
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)

    out = tmp_path / "out"
    rc = train_platerec.main([
        "--data-root", str(tmp_path), "--epochs", "1", "--batch-size", "1",
        "--num-workers", "0", "--crop-mode", "axis",
        "--axis-crop-manifest", str(manifest), "--out", str(out),
        "--min-mem-available-gb", "0", "--max-rss-gb", "1",
        "--mem-check-interval", "1",
    ])
    assert rc == 3

    ckpt_path = out / "last_ckpt.pth"
    ck = torch.load(ckpt_path, map_location="cpu", weights_only=False)
    expected = train_platerec._checkpoint_meta(
        "axis", hashlib.sha256(manifest.read_bytes()).hexdigest(), detector_sha,
        0.08, train_platerec._charset_sha256(train_platerec.cn_charset()),
    )
    assert ck["crop_meta"] == expected
    train_platerec._validate_resume_meta(ck, expected)
