import hashlib
import json
import sys
from pathlib import Path

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
pytest.importorskip("torch")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "training"))
from train_platerec import (  # noqa: E402
    axis_crop_native_contract,
    build_axis_items,
    _checkpoint_meta,
    _load_init_weights,
    _validate_resume_meta,
    main,
    warp_crop,
)


def _manifest(tmp_path: Path, *, bbox=None, selection="highest_score") -> Path:
    image = np.zeros((30, 80, 3), dtype=np.uint8)
    image[:, :, 0] = np.arange(80, dtype=np.uint8)[None, :]
    image[:, :, 1] = np.arange(30, dtype=np.uint8)[:, None]
    source = tmp_path / "source.jpg"
    assert cv2.imwrite(str(source), image)
    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    detector = tmp_path / "detector.onnx"
    detector.write_bytes(b"test-detector")
    detector_sha = hashlib.sha256(detector.read_bytes()).hexdigest()
    row = {
        "index": 0, "split": "train", "source_path": str(source), "source_sha256": sha,
        "source_width": 80, "source_height": 30,
        "det_bbox_xyxy": bbox or [2.5, 3.5, 30.5, 20.5],
        "det_score": 0.91, "selection": selection, "label": "皖AB12345",
    }
    val = dict(row); val["index"] = 1; val["split"] = "val"
    p = tmp_path / "axis.json"
    p.write_text(json.dumps({"schema": "nrd3.axis-crop/1",
                             "bbox_space": "source_pixels",
                             "detector_model_path": detector.name,
                             "detector_model_sha256": detector_sha,
                             "expand": 0.08, "rows": [row, val]}, ensure_ascii=False))
    return p


def test_axis_crop_non_square_and_boundary_is_finite():
    img = np.zeros((7, 13, 3), dtype=np.uint8)
    img[:, :, 0] = 255
    out = axis_crop_native_contract(img, [0, 0, 2, 2], 0.08)
    assert out.shape == (32, 96, 3)
    assert out.dtype == np.float32
    assert np.isfinite(out).all()
    # Source is BGR; native stage2 contract emits RGB.
    assert float(out[:, :, 2].min()) == pytest.approx(255.0)
    assert float(out[:, :, 2].max()) == pytest.approx(255.0)


def test_axis_manifest_requires_detector_selection_and_checks_sha(tmp_path):
    p = _manifest(tmp_path)
    paths, labels, boxes, expand, model_sha = build_axis_items(p, "train", tmp_path)
    assert len(paths) == 1 and labels == ["皖AB12345"]
    assert boxes == [[2.5, 3.5, 30.5, 20.5]]
    assert expand == pytest.approx(0.08)
    assert model_sha == hashlib.sha256((tmp_path / "detector.onnx").read_bytes()).hexdigest()

    bad = json.loads(p.read_text())
    bad["rows"][0]["selection"] = "best_iou"
    p.write_text(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(ValueError, match="highest_score"):
        build_axis_items(p, "train", tmp_path)


def test_axis_manifest_rejects_missing_or_corrupt_source(tmp_path):
    p = _manifest(tmp_path)
    bad = json.loads(p.read_text())
    bad["rows"][0]["source_sha256"] = "0" * 64
    p.write_text(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(ValueError, match="SHA mismatch"):
        build_axis_items(p, "train", tmp_path)

    p = _manifest(tmp_path, bbox=[-1, 0, 4, 4])
    with pytest.raises(ValueError, match="outside source"):
        build_axis_items(p, "train", tmp_path)


def test_axis_manifest_requires_declared_source_bbox_space_and_model_hash(tmp_path):
    p = _manifest(tmp_path)
    bad = json.loads(p.read_text())
    bad.pop("bbox_space")
    p.write_text(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(ValueError, match="bbox_space"):
        build_axis_items(p, "train", tmp_path)

    p = _manifest(tmp_path)
    bad = json.loads(p.read_text())
    bad["bbox_space"] = "source_normalized"
    p.write_text(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(ValueError, match="bbox_space"):
        build_axis_items(p, "train", tmp_path)

    p = _manifest(tmp_path)
    bad = json.loads(p.read_text())
    bad["detector_model_sha256"] = "0" * 64
    p.write_text(json.dumps(bad, ensure_ascii=False))
    with pytest.raises(ValueError, match="detector model path/SHA"):
        build_axis_items(p, "train", tmp_path)


def test_default_quad_contract_remains_warp_crop(tmp_path):
    img = np.zeros((40, 80, 3), dtype=np.uint8)
    img[10:30, 20:60] = (10, 20, 30)
    corners = np.float32([[58, 28], [22, 28], [20, 12], [60, 12]])
    source = tmp_path / "quad.jpg"
    assert cv2.imwrite(str(source), img)
    decoded = cv2.imread(str(source), cv2.IMREAD_COLOR)
    expected = cv2.cvtColor(warp_crop(decoded, corners), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    import train_platerec
    ds = train_platerec.RecDataset([(str(source), "皖AB12345")],
                                   {c: i for i, c in enumerate("皖AB12345")}, False)
    ds.with_corners([corners])
    actual, _ = ds[0]
    assert actual.shape == (3, 32, 96)
    assert np.array_equal(actual.numpy(), expected.transpose(2, 0, 1))


def test_axis_fractional_interpolation_and_independent_edge_clamp():
    img = np.zeros((2, 2, 3), dtype=np.uint8)
    img[:, :, 0] = [[0, 100], [200, 255]]  # B channel; RGB output is channel 2
    out = axis_crop_native_contract(img, [0.25, 0.25, 0.75, 0.75], 0.0)
    assert out.dtype == np.float32
    fx, fy = 1 / 384, 1 / 128
    expected = (100 * fx) * (1 - fy) + (200 + 55 * fx) * fy
    assert expected != int(expected)
    assert float(out[16, 48, 2]) == pytest.approx(expected, abs=1e-6)
    edge = axis_crop_native_contract(img, [0, 0, 1, 1], 0.0)
    # At the top-left, native sourcePixel clamps both x0 and x0+1 independently.
    assert float(edge[0, 0, 2]) == pytest.approx(0.0)


def test_quad_train_augmentation_stays_before_bgr_to_rgb(tmp_path, monkeypatch):
    import train_platerec
    img = np.zeros((40, 80, 3), dtype=np.uint8)
    img[10:30, 20:60] = (10, 20, 30)
    source = tmp_path / "source.jpg"
    assert cv2.imwrite(str(source), img)
    corners = np.float32([[58, 28], [22, 28], [20, 12], [60, 12]])
    def fake_augment(x):
        return np.clip(x.astype(np.int16) + np.array([1, 2, 3]), 0, 255).astype(np.uint8)
    monkeypatch.setattr(train_platerec, "augment", fake_augment)
    ds = train_platerec.RecDataset([(str(source), "皖AB12345")],
                                   {c: i for i, c in enumerate("皖AB12345")}, True)
    ds.with_corners([corners])
    decoded = cv2.imread(str(source), cv2.IMREAD_COLOR)
    expected_bgr = fake_augment(train_platerec.warp_crop(decoded, corners))
    expected_rgb = cv2.cvtColor(expected_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    actual, _ = ds[0]
    assert np.array_equal(actual.numpy(), expected_rgb.transpose(2, 0, 1))


def test_axis_resume_rejects_legacy_and_mismatched_crop_meta():
    meta = _checkpoint_meta("axis", "a" * 64, "b" * 64, 0.08, "c" * 64)
    with pytest.raises(ValueError, match="legacy checkpoint"):
        _validate_resume_meta({"model": {}}, meta)
    with pytest.raises(ValueError, match="mismatch"):
        _validate_resume_meta({"crop_meta": {**meta, "axis_expand": 0.1}}, meta)
    _validate_resume_meta({"model": {}}, _checkpoint_meta("quad", None, None, None, "c" * 64))


def test_init_weights_loads_only_model_state(tmp_path):
    import torch
    import train_platerec
    source = train_platerec.PlateRecNet(66)
    path = tmp_path / "init.pth"
    torch.save({"model": source.state_dict(), "optimizer": {"sentinel": 1}, "best_acc": 0.953}, path)
    target = train_platerec.PlateRecNet(66)
    _load_init_weights(target, path)
    assert all(torch.equal(a, b) for a, b in zip(source.parameters(), target.parameters()))


def test_resume_and_init_weights_are_mutually_exclusive():
    with pytest.raises(SystemExit, match="mutually exclusive"):
        main(["--resume", "old.pth", "--init-weights", "init.pth"])
