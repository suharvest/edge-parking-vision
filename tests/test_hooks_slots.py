"""SlotHooks 车位钩子测试（规格 §6.1/§6.2、§8.1 P1 行）。

VBE fixture 回放：`tests/fixtures/vbe/slots_*.jsonl` 首行 meta（stream_id +
slots 选项 + t0_ms），其余行为 op（``event`` slot_change / ``stream_state`` /
``tick`` 断流计时 / ``update_slots`` 增删车位），每步带内联 ``expect``
（精确比对发布内容）与可选 ``expect_state``（钩子内部状态表）；
`*.expected.jsonl` 为整份 fixture 的期望发布扁平序列。

基座依赖按 §8.0 用 fake 顶替：``on_stream_state``（B1）由测试直接调用，
``configure_analyzer``（B3）由 `tests/fakes.FakeStreamContext` 记录，
`slot_change` 事件由 `tests/fakes.make_event` 构造（真 Event dataclass）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core_parking.classes import CLASS_IDS
from core_parking.slots import SlotHooks, SlotsOptions, slot_coverage_config

from fakes import FakeStreamContext, RecordingPublisher, make_event

FIXTURES = Path(__file__).parent / "fixtures"
VBE = FIXTURES / "vbe"

SLOTS_STEMS = [
    "slots_change_sequence",       # slot_change 序列 → 期望 slots payload
    "slots_stream_down_9s_11s",    # 断流 9 s 不置 unknown、11 s 置 unknown
    "slots_stream_recover",        # 恢复 running 后回到最后已知状态
    "slots_add_remove",            # 运行中新增/删除车位
]


# ------------------------------------------------------------------ 配置

def test_slots_options_defaults_and_validation():
    opt = SlotsOptions.from_dict({})
    assert opt.slots == {} and opt.unknown_after_s == 10.0
    assert (opt.occupied_ratio, opt.free_ratio) == (0.30, 0.20)
    assert (opt.hold_s, opt.free_hold_s) == (3.0, 5.0)
    with pytest.raises(ValueError):
        SlotsOptions.from_dict({"unknown_after_s": 0})
    with pytest.raises(ValueError):
        SlotsOptions.from_dict({"occupied_ratio": 0.1, "free_ratio": 0.2})
    with pytest.raises(ValueError):
        SlotsOptions.from_dict({"slots": {"cam": [{"id": "A"},
                                                {"id": "A"}]}})   # 重复 id
    with pytest.raises(ValueError):
        SlotsOptions.from_dict({"slots": {"cam": [{"polygon": []}]}})  # 缺 id


def test_slot_coverage_config_maps_spec_params():
    opts = SlotsOptions.from_dict({
        "slots": {"cam-1": [{"id": "B1-023", "polygon": [[0.1, 0.5], [0.3, 0.5]]}]},
        "occupied_ratio": 0.35, "free_ratio": 0.25,
        "hold_s": 2.0, "free_hold_s": 4.0})
    cfg = slot_coverage_config(opts, "cam-1")
    assert cfg == {"slots": [{"id": "B1-023",
                              "polygon": [[0.1, 0.5], [0.3, 0.5]]}],
                   "occupied_ratio": 0.35, "free_ratio": 0.25,
                   "hold_s": 2.0, "free_hold_s": 4.0,
                   "classes": list(CLASS_IDS)}
    assert "unknown_after_s" not in cfg   # 事件级键不下发分析器（§6.2）


def test_analyzers_empty_for_stream_without_slots():
    hooks = _make_hooks({"slots": {"cam-1": [{"id": "A"}]}})
    try:
        assert hooks.analyzers(FakeStreamContext("cam-2").spec) == []
        got = hooks.analyzers(FakeStreamContext("cam-1").spec)
        assert [a["name"] for a in got] == ["slot_coverage"]
    finally:
        hooks.close()


# ------------------------------------------------------------------ VBE fixture 回放

def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def _make_hooks(options: dict) -> SlotHooks:
    hooks = SlotHooks()
    hooks.tx = RecordingPublisher()
    hooks.tick_s = 3600.0     # 定时线程不触发；tick 由 fixture 显式驱动
    hooks.now_ms = lambda: _now[0]
    hooks.configure(options, "dev-1")
    return hooks


_now = [0]


@pytest.mark.parametrize("stem", SLOTS_STEMS)
def test_vbe_replay(stem):
    lines = _load_jsonl(VBE / f"{stem}.jsonl")
    meta = lines[0]["meta"]
    _now[0] = meta["t0_ms"]
    hooks = _make_hooks(meta["options"])
    ctx = FakeStreamContext(meta["stream_id"])
    hooks.on_stream_added(ctx)
    seen = 0
    try:
        for op in lines[1:]:
            _now[0] = op["now_ms"]
            if op["op"] == "event":
                assert hooks.on_event(ctx, make_event(
                    stream_id=ctx.stream_id, seq=op.get("seq", 0),
                    wall_ms=op["wall_ms"], analyzer="slot_coverage",
                    type="slot_change", track_id=op.get("track_id", 0),
                    fields=op["fields"])) == []
            elif op["op"] == "stream_state":
                hooks.on_stream_state(ctx, op["state"], op.get("error"))
            elif op["op"] == "tick":
                hooks._tick(op["now_ms"])
            elif op["op"] == "update_slots":
                hooks.update_slots(ctx.stream_id, op["slots"])
            else:
                raise AssertionError(f"unknown op {op['op']!r}")
            # 每步：期望发布精确比对（本步新增部分）
            got = [{"kind": r["kind"], "ts": r["ts"], "body": r["body"]}
                   for r in hooks.tx.records[seen:]]
            assert got == op.get("expect", []), f"{stem}: after op {op['op']}"
            seen = len(hooks.tx.records)
            # 每步：期望钩子内部状态（slot_id -> state, stream_ok）
            if "expect_state" in op:
                assert {sid: e["state"]
                        for sid, e in ctx.state["slots"].items()} == \
                    op["expect_state"]["slots"]
                assert ctx.state["stream_ok"] == op["expect_state"]["stream_ok"]
        # 整体：与 .expected.jsonl 扁平序列一致
        expected = _load_jsonl(VBE / f"{stem}.expected.jsonl")
        assert [{"kind": r["kind"], "ts": r["ts"], "body": r["body"]}
                for r in hooks.tx.records] == expected
    finally:
        hooks.close()


# ------------------------------------------------------------------ 钩子行为

def test_on_event_ignores_non_slot_change():
    hooks = _make_hooks({"slots": {"gate-a": [{"id": "A", "polygon": []}]}})
    try:
        ctx = FakeStreamContext("gate-a")
        hooks.on_stream_added(ctx)
        assert hooks.on_event(ctx, make_event(type="line_cross",
                                              fields={"direction": "in"})) == []
        assert hooks.tx.records == []
    finally:
        hooks.close()


def test_on_event_rejects_unknown_slot_and_bad_state():
    hooks = _make_hooks({"slots": {"gate-a": [{"id": "A", "polygon": []}]}})
    try:
        ctx = FakeStreamContext("gate-a")
        hooks.on_stream_added(ctx)
        with pytest.raises(KeyError):
            hooks.on_event(ctx, make_event(type="slot_change",
                                           fields={"slot_id": "Z",
                                                   "state": "free",
                                                   "coverage": 0.0}))
        with pytest.raises(ValueError):
            hooks.on_event(ctx, make_event(type="slot_change",
                                           fields={"slot_id": "A",
                                                   "state": "sideways",
                                                   "coverage": 0.5}))
    finally:
        hooks.close()


def test_update_slots_reconfigures_analyzer_and_validates():
    hooks = _make_hooks({"slots": {"gate-a": [{"id": "A", "polygon": []}]}})
    try:
        ctx = FakeStreamContext("gate-a")
        hooks.on_stream_added(ctx)
        new = [{"id": "A", "polygon": []}, {"id": "B", "polygon": []}]
        hooks.update_slots("gate-a", new)
        assert len(ctx.configured) == 1                     # B3 fake 记录
        name, cfg = ctx.configured[0]
        assert name == "slot_coverage"
        assert [s["id"] for s in cfg["slots"]] == ["A", "B"]
        assert ctx.state["slots"]["B"]["state"] == "unknown"  # 新增初始 unknown
        with pytest.raises(ValueError):
            hooks.update_slots("gate-a", [{"id": "A"}, {"id": "A"}])
        with pytest.raises(KeyError):
            hooks.update_slots("gate-nope", new)
    finally:
        hooks.close()


def test_health_fields():
    hooks = _make_hooks({"slots": {"gate-a": [{"id": "A", "polygon": []}]}})
    try:
        ctx = FakeStreamContext("gate-a")
        hooks.on_stream_added(ctx)
        assert hooks.health() == {"spool_depth": 0, "spool_overflow": False,
                                  "mqtt_app_connected": True,
                                  "streams_ok": {"gate-a": True}}
        hooks.on_stream_removed(ctx)
        assert hooks.health()["streams_ok"] == {}
    finally:
        hooks.close()


def test_protocol_attrs():
    hooks = SlotHooks()
    assert hooks.plugins() == []
    assert hooks.name == "edge-parking-slots/0.1.0"
    assert hooks.wants_frames is False
