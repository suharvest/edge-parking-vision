"""NRD-4 P3 车位编辑页与 /config/slots 端点测试（§8.1 P3 行、§6.2）。

用 `SlotsApp` + `tests/fakes.RecordingPublisher` / `FakeStreamContext`
（request_snapshot 打桩）验证：

- `GET /slots/editor` 返回原生 JS 编辑页（引用 `/snapshot/` 与 canvas）；
- `GET /snapshot/{stream}.jpg` 返回基座 JPEG，同一路 1/s 限流 429；
- `GET /config/slots` 返回当前车位表（与 PUT body 同构）；
- `PUT /config/slots` 好样例 200：原子写 `slots.override.json`、
  `configure_analyzer("slot_coverage", …)` 被调、新增车位初始 `unknown`
  并发布一次全量；
- 非凸（验收命令的 bad 样例）、未知流、id 重复、顶点越界 → 400 且可读原因。

编辑页的浏览器交互（画多边形/拖顶点）无法在无浏览器环境验证，见报告。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from core_parking.slots.app import SlotsApp
from core_parking.slots.hooks_slots import is_convex

from fakes import FakeStreamContext, RecordingPublisher

FIXTURES = Path(__file__).parent / "fixtures"


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
        "state_dir": str(tmp_path),
        "unknown_after_s": 10,
        "slots": {
            "cam-b1-01": [
                {"id": "B1-001", "polygon": [[0.05, 0.55], [0.20, 0.55],
                                             [0.20, 0.80], [0.05, 0.80]]},
            ],
        },
    }


@pytest.fixture()
def app(tmp_path):
    a = SlotsApp()
    a.tx = RecordingPublisher()           # 注入 fake：不建 MQTT 会话
    a.configure(_options(tmp_path), "dev-1")
    ctx = FakeStreamContext(stream_id="cam-b1-01")
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


# ----------------------------------------------------------------- is_convex

def test_is_convex_basic():
    assert is_convex([[0, 0], [1, 0], [1, 1], [0, 1]])
    assert is_convex([[0, 0], [1, 0], [0.5, 1]])            # 三角形
    assert not is_convex([[0.1, 0.1], [0.6, 0.1],
                          [0.3, 0.3], [0.1, 0.6]])          # 凹（bad 样例）
    assert not is_convex([[0, 0], [1, 1]])                  # <3 顶点
    assert not is_convex([[0, 0], [0.5, 0.5], [1, 1]])      # 全共线退化
    assert not is_convex([["a", 0], [1, 0], [0, 1]])        # 非数值


# ------------------------------------------------------------ /slots/editor

def test_editor_page_served(app):
    with urllib.request.urlopen(_base(app) + "/slots/editor", timeout=5) as r:
        body = r.read().decode()
    assert r.status == 200
    assert "<canvas" in body and "/preview.jpg" in body and "/preview.json" in body
    assert "/config/slots" in body
    assert "<script" in body and "src=" not in body.split("<script")[1].split(">")[0]


# -------------------------------------------------------- /snapshot/{id}.jpg

def test_snapshot_jpg_and_rate_limit(app):
    with urllib.request.urlopen(_base(app) + "/snapshot/cam-b1-01.jpg",
                                timeout=5) as r:
        assert r.status == 200 and r.headers["Content-Type"] == "image/jpeg"
        assert r.read() == b"\xff\xd8fakejpeg"
    status, body = _req(app, "GET", "/snapshot/cam-b1-01.jpg")
    assert status == 429                                    # 同一路 ≤1 次/秒


def test_snapshot_unknown_stream_404(app):
    status, _ = _req(app, "GET", "/snapshot/nope.jpg")
    assert status == 404


# ------------------------------------------------------------ /config/slots

def test_get_config_slots(app):
    status, body = _req(app, "GET", "/config/slots")
    assert status == 200
    assert body == {"cam-b1-01": [
        {"id": "B1-001", "polygon": [[0.05, 0.55], [0.20, 0.55],
                                     [0.20, 0.80], [0.05, 0.80]]}]}


def test_put_config_slots_good(app, tmp_path):
    good = json.loads((FIXTURES / "slots.good.json").read_text())
    status, body = _req(app, "PUT", "/config/slots", good)
    assert status == 200
    assert [s["id"] for s in body["cam-b1-01"]] == ["B1-023", "B1-024"]

    # 原子写 state_dir/slots.override.json（§6.2），内容为全量生效表
    override = json.loads(
        (tmp_path / "slots.override.json").read_text(encoding="utf-8"))
    assert override == body

    # B3：configure_analyzer("slot_coverage", …) 即时生效
    ctx = list(app._streams.values())[0]
    assert ctx.configured[-1][0] == "slot_coverage"
    assert [s["id"] for s in ctx.configured[-1][1]["slots"]] == ["B1-023", "B1-024"]

    # 新增车位初始 unknown、被删车位（B1-001）从状态表移除；发布一次全量
    slots = ctx.state["slots"]
    assert set(slots) == {"B1-023", "B1-024"}
    assert all(e["state"] == "unknown" for e in slots.values())
    pub = [r for r in app.tx.records if r["kind"] == "slots"]
    assert pub and pub[-1]["body"]["changed"] == []
    assert pub[-1]["body"]["unknown"] == 2

    # GET 与 PUT 回读一致（编辑页整表回写的前提）
    assert _req(app, "GET", "/config/slots")[1] == body


def test_put_config_slots_non_convex_400(app):
    bad = json.loads((FIXTURES / "slots.bad.json").read_text())
    status, body = _req(app, "PUT", "/config/slots", bad)
    assert status == 400
    assert "凸" in body["error"]


def test_put_config_slots_unknown_stream_400(app):
    status, body = _req(app, "PUT", "/config/slots", {"nope": []})
    assert status == 400
    assert "unknown stream" in body["error"]


def test_put_config_slots_duplicate_id_400(app):
    slot = {"id": "B1-023", "polygon": [[0, 0], [0.5, 0], [0.5, 0.5]]}
    status, body = _req(app, "PUT", "/config/slots",
                        {"cam-b1-01": [slot, slot]})
    assert status == 400
    assert "重复" in body["error"]


@pytest.mark.parametrize("polygon", [
    [[0, 0], [1, 0]],                          # <3 顶点
    [[0, 0], [1.2, 0], [0, 1]],                # 坐标越界（非归一化）
    [[0, 0], ["x", 0], [0, 1]],                # 非数值
    [[0, 0], [True, 0], [0, 1]],               # bool 不是数值
])
def test_put_config_slots_bad_polygon_400(app, polygon):
    status, body = _req(app, "PUT", "/config/slots",
                        {"cam-b1-01": [{"id": "X", "polygon": polygon}]})
    assert status == 400
    assert "error" in body


def test_put_config_slots_empty_body_400(app):
    status, _ = _req(app, "PUT", "/config/slots", {})
    assert status == 400


def test_slots_override_is_loaded_after_restart(tmp_path):
    """The editor's atomic override survives an app restart."""
    opts = _options(tmp_path)
    first = SlotsApp()
    first.tx = RecordingPublisher()
    first.configure(opts, "dev-1")
    ctx = FakeStreamContext(stream_id="cam-b1-01")
    first.on_stream_added(ctx)
    try:
        body = {"cam-b1-01": [{"id": "persisted",
                               "polygon": [[0, 0], [1, 0], [1, 1]]}]}
        assert first.set_slots(body) == body
    finally:
        first.close()

    second = SlotsApp()
    second.tx = RecordingPublisher()
    second.configure(opts, "dev-1")
    try:
        assert second.get_slots_config() == body
    finally:
        second.close()


def test_slots_endpoints_404_on_counting_app(tmp_path):
    """counting 预设的 ParkingApp 没有 slots 后端 → 404（不影响既有端点）。"""
    from core_parking.config import ParkingApp
    a = ParkingApp()
    a.tx = RecordingPublisher()
    opts = {
        "site_id": "demo-lot",
        "mqtt": {"host": "localhost", "port": 1883, "username": "u",
                 "password": "", "keepalive_s": 30},
        "http": {"port": _free_port()},
        "classes": [2, 3, 5, 7],
        "spool": {"max_events": 100},
        "preview_frame_stride": 2,
        "state_dir": str(tmp_path),
        "counting": {
            "line": {"a": [0.1, 0.6], "b": [0.9, 0.6]}, "in_side": "positive",
            "count_directions": ["in", "out"], "band": 0.03,
            "segment_margin": 0.05, "min_track_frames": 3, "min_travel": 0.06,
            "reborn_window_s": 2.0, "reborn_dist": 0.08, "capacity": 10,
            "initial_occupancy": 0, "publish_interval_ms": 1000,
        },
    }
    a.configure(opts, "dev-1")
    try:
        assert _req(a, "GET", "/config/slots")[0] == 404
        assert _req(a, "PUT", "/config/slots", {"x": []})[0] == 404
    finally:
        a.close()
