"""C3 HTTP 端点测试（§8.1 C3 行 / §S4）。

用 `tests/fakes.RecordingPublisher` 顶替 SpoolPublisher、`FakeStreamContext`
顶替基座 StreamContext（request_snapshot 打桩），验证 8080 业务端口行为：
`POST /occupancy` 产生 `manual_set`；`PUT /config/counting` 非法坐标 400。
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core_parking.config import ParkingApp

from fakes import FakeStreamContext, RecordingPublisher


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _options(tmp_path: Path) -> dict:
    return {
        "site_id": "demo-lot",
        "mqtt": {"host": "localhost", "port": 1883, "username": "u",
                 "password": "", "keepalive_s": 30},
        "http": {"port": _free_port()},
        "classes": [2, 3, 5, 7],
        "spool": {"max_events": 1000},
        "preview_frame_stride": 2,
        "state_dir": str(tmp_path),
        "counting": {
            "line": {"a": [0.10, 0.60], "b": [0.90, 0.60]},
            "in_side": "positive",
            "count_directions": ["in", "out"],
            "band": 0.03, "segment_margin": 0.05, "min_track_frames": 3,
            "min_travel": 0.06, "reborn_window_s": 2.0, "reborn_dist": 0.08,
            "capacity": 10, "initial_occupancy": 0, "publish_interval_ms": 1000,
        },
    }


@pytest.fixture()
def app(tmp_path):
    a = ParkingApp()
    a.tx = RecordingPublisher()           # 注入 fake：不建 MQTT 会话
    a.configure(_options(tmp_path), "dev-1")
    ctx = FakeStreamContext(stream_id="gate-a")
    ctx.request_snapshot = lambda **kw: ({"seq": 0}, b"\xff\xd8fakejpeg")
    a.on_stream_added(ctx)
    yield a
    a.close()


def _base(app) -> str:
    return f"http://127.0.0.1:{app.http.port}"


def _req(app, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(_base(app) + path, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


# ------------------------------------------------------------------- /events

def test_events_empty_then_recent(app):
    assert _req(app, "GET", "/events") == (200, [])
    ctx = list(app._streams.values())[0]
    app._tap_event(("t", "crossing", {"schema": "parking.crossing/1", "seq": 1}))
    status, arr = _req(app, "GET", "/events?limit=1")
    assert status == 200 and len(arr) == 1 and arr[0]["seq"] == 1


def test_events_bad_limit_is_400(app):
    status, _ = _req(app, "GET", "/events?limit=abc")
    assert status == 400


# ------------------------------------------------------------------ /preview

def test_preview_serves_static_html(app):
    with urllib.request.urlopen(_base(app) + "/preview", timeout=5) as r:
        body = r.read().decode()
    assert r.status == 200 and "preview.json" in body and "preview.jpg" in body


def test_preview_json_without_frame(app):
    status, js = _req(app, "GET", "/preview.json?stream=gate-a")
    assert status == 200
    assert js["stream_id"] == "gate-a"
    assert js["detections"] == []
    assert js["line"] == {"a": [0.10, 0.60], "b": [0.90, 0.60]}


def test_preview_json_maps_boxes_to_source_norm(app):
    from vision_base.letterbox import fit
    ctx = list(app._streams.values())[0]

    class D:
        cx, cy, w, h = 0.5, 0.5, 0.2, 0.2
        score, class_id, track_id = 0.9, 2, 7

    class R:
        stream_id, seq, wall_ms, inference_ms = "gate-a", 3, 1234.0, 5.5
        geom = fit(416, 416, 416, 416, "top_left")
        detections = [D()]

    ctx.state["last"] = R()
    status, js = _req(app, "GET", "/preview.json?stream=gate-a")
    assert status == 200
    assert js["detections"][0]["box"] == [0.4, 0.4, 0.6, 0.6]


def test_preview_jpg_rate_limited(app):
    with urllib.request.urlopen(_base(app) + "/preview.jpg?stream=gate-a",
                                timeout=5) as r:
        assert r.status == 200 and r.read() == b"\xff\xd8fakejpeg"
    status, _ = _req(app, "GET", "/preview.jpg?stream=gate-a")
    assert status == 429


# ---------------------------------------------------------- /config/counting

def test_get_config_counting(app):
    status, js = _req(app, "GET", "/config/counting")
    assert status == 200
    assert js["line"] == {"a": [0.10, 0.60], "b": [0.90, 0.60]}
    assert js["capacity"] == 10


def test_put_config_counting_invalid_coords_400(app):
    counting = _options(Path(app.options["state_dir"]))["counting"]
    counting["line"] = {"a": [1.5, 0.6], "b": [0.9, 0.6]}
    status, js = _req(app, "PUT", "/config/counting", body=counting)
    assert status == 400
    assert "counting.line.a" in js["error"]


def test_put_config_counting_unknown_key_400(app):
    status, _ = _req(app, "PUT", "/config/counting", body={"foo": 1})
    assert status == 400


def test_put_config_counting_applies_and_persists(app, tmp_path):
    new_line = {"a": [0.2, 0.4], "b": [0.8, 0.4]}
    counting = _options(Path(app.options["state_dir"]))["counting"]
    counting["line"] = new_line
    status, js = _req(app, "PUT", "/config/counting", body=counting)
    assert status == 200 and js["line"] == new_line
    # 即时生效：line_cross 已按新配置下发
    ctx = list(app._streams.values())[0]
    name, cfg = ctx.configured[-1]
    assert name == "line_cross" and cfg["lines"][0]["a"] == [0.2, 0.4]
    # 持久化：state_dir/app-options.override.json
    override = json.loads((tmp_path / "app-options.override.json")
                          .read_text(encoding="utf-8"))
    assert override["counting"]["line"]["a"] == [0.2, 0.4]
    # GET 反映新值
    assert _req(app, "GET", "/config/counting")[1]["line"] == new_line


# ---------------------------------------------------------------- /occupancy

def test_get_occupancy(app):
    status, js = _req(app, "GET", "/occupancy?stream=gate-a")
    assert status == 200
    assert js["stream_id"] == "gate-a"
    assert js["capacity"] == 10 and js["occupancy"] == 0


def test_post_occupancy_manual_set(app):
    status, js = _req(app, "POST", "/occupancy", body={"occupancy": 3})
    assert status == 200
    assert js["occupancy"] == 3 and js["free"] == 7
    # 产生 reason=manual_set 的 occupancy 快照（发布走注入的 fake）
    occ_events = [r for r in app.tx.records
                  if r["kind"] == "occupancy" and r["body"]["reason"] == "manual_set"]
    assert occ_events and occ_events[-1]["body"]["occupancy"] == 3
    ctx = list(app._streams.values())[0]
    assert ctx.state["occ"].last_manual_set["operator"] == "http"


def test_post_occupancy_validates_body(app):
    for bad in ({"occupancy": -1}, {"occupancy": 1.5}, {"occupancy": True}, {}):
        status, _ = _req(app, "POST", "/occupancy", body=bad)
        assert status == 400, bad


def test_unknown_path_404(app):
    assert _req(app, "GET", "/nope")[0] == 404
