"""预览与编辑页端点测试：帧/框对齐、车牌读数、slots 实时状态、计数线编辑页与持久化。

- `/preview.jpg` 取最近有框那一帧并回 `X-Frame-Seq`；`/preview.json?seq=` 回查同一帧的框；
- 车牌预设：`preset=plate`、无计数线、检测框附 `plate`、顶层 `recent_reads`；
- slots 预设：`/preview.json` 返回每个车位的多边形、状态与计数；
- `/counting/editor` 页面与 `PUT /config/counting` 的结果跨重启保留。
"""

from __future__ import annotations

import json
import logging
import types
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core_parking.config import ParkingApp
from core_parking.slots.app import SlotsApp

from fakes import FakeStreamContext, RecordingPublisher
from test_http import _options as _counting_options
from test_slots_http import _options as _slots_options


def _base(app) -> str:
    return f"http://127.0.0.1:{app.http.port}"


def _get(app, path: str):
    try:
        with urllib.request.urlopen(_base(app) + path, timeout=5) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def _det(track_id, cx=0.5):
    return types.SimpleNamespace(cx=cx, cy=0.5, w=0.2, h=0.2, score=0.9,
                                 class_id=0, track_id=track_id)


def _res(seq, dets):
    from vision_base.letterbox import fit
    return types.SimpleNamespace(stream_id="gate-a", seq=seq, wall_ms=1000.0 + seq,
                                 inference_ms=5.0, geom=fit(416, 416, 416, 416, "top_left"),
                                 detections=dets)


@pytest.fixture()
def app(tmp_path):
    a = ParkingApp()
    a.tx = RecordingPublisher()
    a.configure(_counting_options(tmp_path), "dev-1")
    ctx = FakeStreamContext(stream_id="gate-a")
    calls = []

    def snap(**kw):
        calls.append(kw)
        return {"seq": kw.get("seq") or 99}, b"\xff\xd8fakejpeg"
    ctx.request_snapshot = snap
    ctx.snap_calls = calls
    a.on_stream_added(ctx)
    yield a
    a.close()


# ------------------------------------------------------------- frame/box sync

def test_preview_jpg_requests_last_boxed_frame_and_reports_seq(app):
    ctx = app._streams["gate-a"]
    for seq in (4, 6, 8):                       # preview_frame_stride=2
        app.on_frame(ctx, _res(seq, [_det(seq)]))
    status, headers, body = _get(app, "/preview.jpg?stream=gate-a")
    assert status == 200 and body.startswith(b"\xff\xd8")
    assert ctx.snap_calls[-1]["seq"] == 8 and ctx.snap_calls[-1]["crop"] is False
    assert headers["X-Frame-Seq"] == "8"


def test_preview_json_seq_returns_that_frames_boxes(app):
    ctx = app._streams["gate-a"]
    app.on_frame(ctx, _res(4, [_det(1, cx=0.3)]))
    app.on_frame(ctx, _res(6, [_det(2, cx=0.6)]))
    _, _, raw = _get(app, "/preview.json?stream=gate-a&seq=4")
    js = json.loads(raw)
    assert js["seq"] == 4 and js["detections"][0]["track_id"] == 1
    _, _, raw = _get(app, "/preview.json?stream=gate-a")
    assert json.loads(raw)["seq"] == 6
    _, _, raw = _get(app, "/preview.json?stream=gate-a&seq=12345")   # 不在环里 → 最近一帧
    assert json.loads(raw)["seq"] == 6
    status, _, _ = _get(app, "/preview.json?stream=gate-a&seq=x")
    assert status == 400


def test_preview_json_counting_preset_unchanged(app):
    _, _, raw = _get(app, "/preview.json?stream=gate-a")
    js = json.loads(raw)
    assert js["preset"] == "counting"
    assert js["line"] == {"a": [0.10, 0.60], "b": [0.90, 0.60]}
    assert "recent_reads" not in js


# ------------------------------------------------------------------- plate

def _plate_env(seq, track_id, plate, ptype="blue", conf=0.95, cam="gate-a"):
    return {"schema": "parking.plate/1", "camera_id": cam, "ts": 1_700_000_000_000 + seq,
            "seq": seq, "plate": plate, "plate_type": ptype, "confidence": conf,
            "track_id": track_id}


def test_preview_json_plate_preset_attaches_reads(app):
    app.plate_hooks = types.SimpleNamespace(close=lambda: None)
    try:
        ctx = app._streams["gate-a"]
        app._tap_event(("t", "plate", _plate_env(1, 7, "皖AD06151", "green", 0.91)))
        app._tap_event(("t", "plate", _plate_env(2, 9, "京A12345")))
        app._tap_event(("t", "plate", _plate_env(3, 7, "皖AD06151", "green", 0.97)))
        app._tap_event(("t", "plate", _plate_env(4, 5, "沪B00001", cam="other")))
        app._tap_event(("t", "crossing", {"schema": "parking.crossing/1", "camera_id": "gate-a"}))
        app.on_frame(ctx, _res(10, [_det(7), _det(8)]))
        _, _, raw = _get(app, "/preview.json?stream=gate-a")
        js = json.loads(raw)
        assert js["preset"] == "plate" and js["line"] is None
        d7, d8 = js["detections"]
        assert d7["plate"] == {"ts_ms": 1_700_000_000_003, "plate": "皖AD06151",
                               "plate_type": "green", "confidence": 0.97}
        assert "plate" not in d8
        assert [r["plate"] for r in js["recent_reads"]] == ["皖AD06151", "京A12345", "皖AD06151"]
        for i in range(10):
            app._tap_event(("t", "plate", _plate_env(10 + i, 20 + i, f"粤B{i:05d}")))
        _, _, raw = _get(app, "/preview.json?stream=gate-a")
        reads = json.loads(raw)["recent_reads"]
        assert len(reads) == 5 and reads[0]["plate"] == "粤B00009"
    finally:
        app.plate_hooks = None


# --------------------------------------------------------- counting editor

def test_counting_editor_page_served(app):
    status, headers, body = _get(app, "/counting/editor")
    page = body.decode()
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert "/config/counting" in page and "/preview.jpg" in page and "/occupancy" in page
    assert "交换进出" in page and "Swap in/out" in page


def test_counting_editor_404_on_plate_preset(app):
    app.plate_hooks = types.SimpleNamespace(close=lambda: None)
    try:
        status, _, _ = _get(app, "/counting/editor")
        assert status == 404
    finally:
        app.plate_hooks = None


def test_counting_override_survives_restart(tmp_path):
    opts = _counting_options(tmp_path)
    first = ParkingApp()
    first.tx = RecordingPublisher()
    first.configure(opts, "dev-1")
    ctx = FakeStreamContext(stream_id="gate-a")
    first.on_stream_added(ctx)
    try:
        new = json.loads(json.dumps(opts["counting"]))
        new["line"] = {"a": [0.2, 0.1], "b": [0.3, 0.9]}
        new["in_side"] = "negative"
        first.set_counting(new)
    finally:
        first.close()

    second = ParkingApp()
    second.tx = RecordingPublisher()
    second.configure(_counting_options(tmp_path), "dev-1")   # 部署配置仍是旧线
    try:
        assert second.counting.line == {"a": [0.2, 0.1], "b": [0.3, 0.9]}
        assert second.counting.in_side == "negative"
    finally:
        second.close()


def test_corrupt_counting_override_falls_back(tmp_path):
    (tmp_path / "app-options.override.json").write_text("{not json", encoding="utf-8")
    a = ParkingApp()
    a.tx = RecordingPublisher()
    a.configure(_counting_options(tmp_path), "dev-1")
    try:
        assert a.counting.line == {"a": [0.10, 0.60], "b": [0.90, 0.60]}
    finally:
        a.close()


# ------------------------------------------------------------------ slots

@pytest.fixture()
def slots_app(tmp_path):
    a = SlotsApp()
    a.tx = RecordingPublisher()
    a.configure(_slots_options(tmp_path), "dev-1")
    ctx = FakeStreamContext(stream_id="cam-b1-01")
    ctx.request_snapshot = lambda **kw: ({"seq": 3}, b"\xff\xd8fakejpeg")
    a.on_stream_added(ctx)
    yield a
    a.close()


def test_slots_preview_json_reports_states_and_counts(slots_app):
    ctx = slots_app._streams["cam-b1-01"]
    ctx.state["slots"]["B1-001"].update(state="occupied", since=123, cover=0.61)
    _, _, raw = _get(slots_app, "/preview.json?stream=cam-b1-01")
    js = json.loads(raw)
    assert js["preset"] == "slots" and js["line"] is None
    assert js["counts"] == {"occupied": 1, "free": 0, "unknown": 0}
    s = js["slots"][0]
    assert s["id"] == "B1-001" and s["state"] == "occupied" and s["cover"] == 0.61
    assert s["polygon"][0] == [0.05, 0.55]


def test_slots_preview_jpg_reports_frame_seq(slots_app):
    status, headers, _ = _get(slots_app, "/preview.jpg?stream=cam-b1-01")
    assert status == 200 and headers["X-Frame-Seq"] == "3"


# ------------------------------------------- counting override: editor keys only

def _counting_app(tmp_path, capacity=None):
    opts = _counting_options(tmp_path)
    if capacity is not None:
        opts["counting"]["capacity"] = capacity
    a = ParkingApp()
    a.tx = RecordingPublisher()
    a.configure(opts, "dev-1")
    return a, opts


def test_counting_override_stores_only_editor_keys(tmp_path):
    a, opts = _counting_app(tmp_path)
    try:
        body = json.loads(json.dumps(opts["counting"]))
        body["line"] = {"a": [0.2, 0.1], "b": [0.3, 0.9]}
        body["capacity"] = opts["counting"]["capacity"] + 50   # 编辑页不改 capacity：忽略
        applied = a.set_counting(body)
        assert applied["capacity"] == opts["counting"]["capacity"]
        assert a.counting.capacity == opts["counting"]["capacity"]
    finally:
        a.close()
    data = json.loads((tmp_path / "app-options.override.json").read_text(encoding="utf-8"))
    assert set(data["counting"]) == {"line", "in_side"}
    assert data["counting"]["line"] == {"a": [0.2, 0.1], "b": [0.3, 0.9]}


def test_redeploy_capacity_wins_over_saved_line(tmp_path):
    a, opts = _counting_app(tmp_path)
    try:
        body = json.loads(json.dumps(opts["counting"]))
        body["line"] = {"a": [0.2, 0.1], "b": [0.3, 0.9]}
        body["in_side"] = "negative"
        a.set_counting(body)
    finally:
        a.close()
    new_cap = opts["counting"]["capacity"] + 7
    b, _ = _counting_app(tmp_path, capacity=new_cap)          # 重新部署改总车位数
    try:
        assert b.counting.capacity == new_cap
        assert b.counting.line == {"a": [0.2, 0.1], "b": [0.3, 0.9]}
        assert b.counting.in_side == "negative"
        ctx = FakeStreamContext(stream_id="gate-a")
        b.on_stream_added(ctx)
        assert ctx.state["occ"].capacity == new_cap
    finally:
        b.close()


def test_partial_put_keeps_saved_line(tmp_path):
    a, _ = _counting_app(tmp_path)
    try:
        a.set_counting({"line": {"a": [0.2, 0.1], "b": [0.3, 0.9]}})
        a.set_counting({"in_side": "negative"})
        assert a.counting.line == {"a": [0.2, 0.1], "b": [0.3, 0.9]}
        assert a.counting.in_side == "negative"
    finally:
        a.close()


def test_put_without_editor_keys_rejected(tmp_path):
    from core_parking.config import ConfigError
    a, _ = _counting_app(tmp_path)
    try:
        with pytest.raises(ConfigError):
            a.set_counting({"capacity": 99})
    finally:
        a.close()
    assert not (tmp_path / "app-options.override.json").exists()


def test_legacy_full_override_ignores_extra_keys_logs_once(tmp_path, caplog):
    legacy = json.loads(json.dumps(_counting_options(tmp_path)["counting"]))
    legacy["line"] = {"a": [0.2, 0.1], "b": [0.3, 0.9]}
    legacy["capacity"] = 3                                    # 旧版本写下的整段 counting
    (tmp_path / "app-options.override.json").write_text(
        json.dumps({"counting": legacy}), encoding="utf-8")
    caplog.set_level(logging.WARNING, logger="core_parking.hooks_counting")
    for cap in (40, 41):
        a, _ = _counting_app(tmp_path, capacity=cap)
        try:
            assert a.counting.capacity == cap
            assert a.counting.line == {"a": [0.2, 0.1], "b": [0.3, 0.9]}
        finally:
            a.close()
    msgs = [r.getMessage() for r in caplog.records if "non-editor counting keys" in r.getMessage()]
    assert len(msgs) == 1 and "capacity" in msgs[0]
    # 下一次保存把文件收敛成只含 editor 键
    a, _ = _counting_app(tmp_path, capacity=40)
    try:
        a.set_counting({"in_side": "negative"})
    finally:
        a.close()
    data = json.loads((tmp_path / "app-options.override.json").read_text(encoding="utf-8"))
    assert set(data["counting"]) == {"line", "in_side"}


def test_invalid_override_line_falls_back_to_deploy(tmp_path):
    (tmp_path / "app-options.override.json").write_text(
        json.dumps({"counting": {"line": {"a": [1.5, 0.1], "b": [0.3, 0.9]}}}), encoding="utf-8")
    a, opts = _counting_app(tmp_path)
    try:
        assert a.counting.line == opts["counting"]["line"]
    finally:
        a.close()
