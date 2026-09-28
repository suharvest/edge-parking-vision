"""ParkingHooks 计数钩子测试（规格 §6.2/§6.3、§8.1 C1 行）。

VBE fixture 回放：`tests/fixtures/vbe/counting_*.jsonl` 首行 meta（counting
选项），其余行为 `line_cross` 事件；`*.expected.jsonl` 为期望的 crossing
发布（精确比对）+ 末行 `final`（最终 occupancy 状态）。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from core_parking.classes import CLASS_IDS, CLASS_NAMES, vehicle_class
from core_parking.hooks_counting import (CountingOptions, ParkingHooks,
                                         line_cross_config, map_direction)
from core_parking.occupancy import Occupancy

from fakes import FakeStreamContext, RecordingPublisher, make_event

FIXTURES = Path(__file__).parent / "fixtures"
VBE = FIXTURES / "vbe"


# ------------------------------------------------------------------ classes

def test_class_mapping():
    assert CLASS_NAMES == {2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
    assert CLASS_IDS == (2, 3, 5, 7)
    assert vehicle_class(7) == "truck"
    with pytest.raises(KeyError):
        vehicle_class(1)


# ------------------------------------------------------------------ map_direction

@pytest.mark.parametrize("bdir,in_side,expected", [
    ("forward", "negative", "in"),    # forward 终点在负侧
    ("forward", "positive", "out"),
    ("backward", "positive", "in"),
    ("backward", "negative", "out"),
])
def test_map_direction_truth_table(bdir, in_side, expected):
    assert map_direction(bdir, in_side) == expected


def test_map_direction_rejects_garbage():
    with pytest.raises(ValueError):
        map_direction("left", "positive")
    with pytest.raises(ValueError):
        map_direction("forward", "west")


def test_forward_semantics_pinned_by_base_fixture():
    """BASE-1 M1.6 fixture `line_cross_basic.json`（retail-vision）核实：
    竖线 a=(0.5,0.2) b=(0.5,0.8)，锚点 x 0.40→0.60 的事件 direction=forward，
    即 forward = 正侧(a→b 左侧, x<0.5) → 负侧。这里锁定与该语义一致的映射。"""
    assert map_direction("forward", "negative") == "in"


# ------------------------------------------------------------------ 配置映射

def test_line_cross_config_matches_parking_fixture():
    fx = json.loads((FIXTURES / "line_cross_parking.json").read_text())
    counting = CountingOptions.from_dict(fx["options_counting"])
    hooks = ParkingHooks()
    hooks.tx = RecordingPublisher()
    hooks.configure({"counting": fx["options_counting"],
                     "classes": fx["classes"],
                     "state_dir": "/tmp/unused"}, "dev-1")
    try:
        got = hooks.analyzers(FakeStreamContext("cam-9").spec)
    finally:
        hooks.close()
    assert got == fx["expected_analyzers"]
    cfg = got[0]["config"]
    for key in ("in_side", "count_directions", "capacity",
                "initial_occupancy", "publish_interval_ms"):
        assert key not in cfg  # 事件级键不下发给分析器


def test_counting_options_validation():
    with pytest.raises(ValueError):
        CountingOptions.from_dict({"in_side": "west"})
    with pytest.raises(ValueError):
        CountingOptions.from_dict({"count_directions": ["sideways"]})
    with pytest.raises(ValueError):
        CountingOptions.from_dict({"count_directions": []})
    with pytest.raises(ValueError):
        CountingOptions.from_dict({"capacity": -1})
    opt = CountingOptions.from_dict({})  # 全缺省可用
    assert opt.in_side == "positive" and opt.count_directions == ("in", "out")


# ------------------------------------------------------------------ VBE fixture 回放

def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _replay(tmp_path: Path, fixture: str):
    lines = _load_jsonl(VBE / fixture)
    meta = lines[0]["meta"]
    hooks = ParkingHooks()
    hooks.tx = RecordingPublisher()
    hooks.tick_s = 3600.0  # 回放期间不跑定时线程逻辑
    hooks.configure({"counting": meta["counting"],
                     "classes": meta.get("classes", list(CLASS_IDS)),
                     "state_dir": str(tmp_path)}, "dev-1")
    ctx = FakeStreamContext(meta.get("stream_id", "gate-a"))
    hooks.on_stream_added(ctx)
    for e in lines[1:]:
        out = hooks.on_event(ctx, make_event(
            stream_id=ctx.stream_id, seq=e.get("seq", 0),
            wall_ms=e.get("wall_ms", 0.0), type=e.get("type", "line_cross"),
            track_id=e.get("track_id", 0), fields=e.get("fields", {})))
        assert out == []  # 停车事件不经基座 PublishWorker
    hooks.close()
    return hooks, ctx


@pytest.mark.parametrize("stem", [
    "counting_forward_in",           # 正向 → in
    "counting_backward_out",         # 反向 → out
    "counting_in_side_positive",     # in_side: positive 时 backward → in
    "counting_count_in_only",        # count_directions ["in"]：out 被忽略且 occupancy 不变
    "counting_reverse_in_out",       # 倒车产生 in+out，净值正确
    "counting_capacity_clamp",       # 容量上限钳位（hard max = capacity*2）
])
def test_vbe_replay(tmp_path, stem):
    hooks, ctx = _replay(tmp_path, f"{stem}.jsonl")
    expected = _load_jsonl(VBE / f"{stem}.expected.jsonl")
    want_crossings = [e for e in expected if e.get("kind") == "crossing"]
    want_final = next(e["final"] for e in expected if "final" in e)

    got_crossings = [{"kind": r["kind"], "body": r["body"], "ts": r["ts"]}
                     for r in hooks.tx.records if r["kind"] == "crossing"]
    assert got_crossings == want_crossings

    occ: Occupancy = ctx.state["occ"]
    assert {"occupancy": occ.occupancy, "in_today": occ.in_today,
            "out_today": occ.out_today, "free": occ.free} == want_final


def test_ignored_out_event_leaves_occupancy_untouched(tmp_path):
    """count_directions ["in"] 时 out 事件：无发布、occupancy 与今日计数均不变。"""
    hooks, ctx = _replay(tmp_path, "counting_count_in_only.jsonl")
    occ: Occupancy = ctx.state["occ"]
    assert occ.out_today == 0
    assert not any(r["body"].get("direction") == "out"
                   for r in hooks.tx.records if r["kind"] == "crossing")


# ------------------------------------------------------------------ 钩子行为

def _make_hooks(tmp_path, counting=None, **opts):
    hooks = ParkingHooks()
    hooks.tx = RecordingPublisher()
    hooks.tick_s = opts.pop("tick_s", 3600.0)
    options = {"counting": counting or {"in_side": "negative", "capacity": 10},
               "state_dir": str(tmp_path)}
    options.update(opts)
    hooks.configure(options, "dev-1")
    return hooks


def test_on_stream_added_publishes_boot_snapshot(tmp_path):
    hooks = _make_hooks(tmp_path, counting={"in_side": "negative", "capacity": 10,
                                            "initial_occupancy": 4})
    ctx = FakeStreamContext("gate-a")
    hooks.on_stream_added(ctx)
    hooks.close()
    boots = [r for r in hooks.tx.records if r["kind"] == "occupancy"]
    assert len(boots) == 1
    assert boots[0]["body"] == {"occupancy": 4, "capacity": 10, "free": 6,
                                "in_today": 0, "out_today": 0, "reason": "boot"}
    assert "seq" in ctx.state  # SeqAllocator 已就位（C2 信封使用）


def test_on_event_ignores_non_line_cross(tmp_path):
    hooks = _make_hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    hooks.on_stream_added(ctx)
    ev = make_event(type="zone", fields={"direction": "forward"})
    assert hooks.on_event(ctx, ev) == []
    assert not [r for r in hooks.tx.records if r["kind"] == "crossing"]
    hooks.close()


def test_occupancy_snapshot_throttled_by_publish_interval(tmp_path):
    hooks = _make_hooks(tmp_path, tick_s=0.01,
                        counting={"in_side": "negative", "capacity": 10,
                                  "publish_interval_ms": 10_000})
    ctx = FakeStreamContext("gate-a")
    hooks.on_stream_added(ctx)
    ev = make_event(wall_ms=1790000000000.0,
                    fields={"line_id": "main", "direction": "forward",
                            "anchor": [0.5, 0.6], "class_id": 2, "score": 0.9})
    hooks.on_event(ctx, ev)
    time.sleep(0.15)  # 多个 tick 过去，但距 boot 快照 < publish_interval_ms
    snapshots = [r for r in hooks.tx.records
                 if r["kind"] == "occupancy" and r["body"]["reason"] == "crossing"]
    assert snapshots == []
    hooks.counting.publish_interval_ms = 0  # 放开节流后下一 tick 即补发
    deadline = time.time() + 2.0
    while time.time() < deadline:
        snapshots = [r for r in hooks.tx.records
                     if r["kind"] == "occupancy" and r["body"]["reason"] == "crossing"]
        if snapshots:
            break
        time.sleep(0.02)
    hooks.close()
    assert len(snapshots) == 1
    assert snapshots[0]["body"]["occupancy"] == 1
    assert snapshots[0]["body"]["reason"] == "crossing"


def test_health_fields(tmp_path):
    hooks = _make_hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    hooks.on_stream_added(ctx)
    hooks.on_event(ctx, make_event(wall_ms=1790000000999.9,
                                   fields={"line_id": "main", "direction": "forward",
                                           "anchor": [0.5, 0.6], "class_id": 2,
                                           "score": 0.9}))
    h = hooks.health()
    hooks.close()
    assert h == {"spool_depth": 0, "spool_overflow": False,
                 "mqtt_app_connected": True, "last_event_ts": 1790000000999}


def test_on_frame_stride_keeps_reference_only(tmp_path):
    class Res:  # 最小 FrameResult 替身（只用到 seq）
        def __init__(self, seq):
            self.seq = seq
    hooks = _make_hooks(tmp_path, preview_frame_stride=15)
    ctx = FakeStreamContext("gate-a")
    hooks.on_stream_added(ctx)
    for seq in range(1, 31):
        assert hooks.on_frame(ctx, Res(seq)) == []
    assert ctx.state["last"].seq == 30
    hooks.close()


def test_plugins_empty_and_protocol_attrs():
    hooks = ParkingHooks()
    assert hooks.plugins() == []
    assert hooks.name == "edge-parking-counting/0.1.0"
    assert hooks.wants_frames is True


def test_occupancy_persisted_on_stream_removed(tmp_path):
    hooks = _make_hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    hooks.on_stream_added(ctx)
    hooks.on_event(ctx, make_event(wall_ms=1.0,
                                   fields={"line_id": "main", "direction": "forward",
                                           "anchor": [0.5, 0.6], "class_id": 2,
                                           "score": 0.9}))
    hooks.on_stream_removed(ctx)
    hooks.close()
    data = json.loads((tmp_path / "gate-a" / "occupancy.json").read_text())
    assert data["occupancy"] == 1
