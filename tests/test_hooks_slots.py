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
import time
import threading
from pathlib import Path

import pytest

from core_parking.classes import CLASS_IDS
from core_parking.transport import SpoolPublisher
from core_parking.slots import SlotHooks, SlotsOptions, slot_coverage_config
from vision_base.types import Event

from fakes import FakeStreamContext, RecordingPublisher, make_event
from fake_mqtt import FakeMqtt

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


def test_nested_slot_coverage_is_forwarded_and_overrides_legacy_flat_keys():
    class Injected:
        def publish(self, **kwargs):
            pass

    h = SlotHooks()
    h.tx = Injected()
    h.configure({
        "site_id": "demo-lot", "mqtt": {},
        "occupied_ratio": 0.91, "free_ratio": 0.81,
        "hold_s": 9.0, "free_hold_s": 8.0, "classes": [1],
        "slot_coverage": {
            "occupied_ratio": 0.41, "free_ratio": 0.21,
            "hold_s": 4.0, "free_hold_s": 6.0, "classes": [2, 5],
        },
        "slots": {"cam": [{"id": "A", "polygon": []}]},
    }, "dev-1")
    try:
        cfg = h.analyzers("cam")[0]["config"]
        assert cfg == {
            "slots": [{"id": "A", "polygon": []}],
            "occupied_ratio": 0.41, "free_ratio": 0.21,
            "hold_s": 4.0, "free_hold_s": 6.0, "classes": [2, 5],
        }
    finally:
        h.close()


def test_real_spool_publisher_slot_event_has_persistent_seq(tmp_path):
    class FakeClient:
        connected = False
        def connect(self): pass
        def close(self): pass
        def publish(self, *args, **kwargs): return True

    def factory(opts, client_id, on_connect):
        return FakeClient()

    tx = SpoolPublisher({}, client_id="demo-app", site_id="demo",
                        spool_path=tmp_path / "spool.jsonl", max_events=10,
                        client_factory=factory)
    h = SlotHooks()
    h.tx = tx
    opts = {"site_id": "demo", "mqtt": {}, "state_dir": str(tmp_path),
            "slots": {"cam": [{"id": "A", "polygon": []}]}}
    h.configure(opts, "dev")
    ctx = type("Ctx", (), {"stream_id": "cam", "state": {}})()
    h.on_stream_added(ctx)
    ev = Event("cam", 1, 1.0, "slot_coverage", "slot_change", 0,
               fields={"slot_id": "A", "state": "free", "coverage": 0.0})
    h.on_event(ctx, ev)
    try:
        _, kind, env = tx._q.get_nowait()
        assert kind == "slots" and env["seq"] == 0
        assert (tmp_path / "cam" / "seq.json").exists()
    finally:
        h.close()
        tx.close()

    tx2 = SpoolPublisher({}, client_id="demo-app-2", site_id="demo",
                         spool_path=tmp_path / "spool2.jsonl", max_events=10,
                         client_factory=factory)
    h2 = SlotHooks(); h2.tx = tx2; h2.configure(opts, "dev-2")
    ctx2 = type("Ctx", (), {"stream_id": "cam", "state": {}})()
    h2.on_stream_added(ctx2)
    h2.on_event(ctx2, ev)
    try:
        _, _, env2 = tx2._q.get_nowait()
        assert env2["seq"] > env["seq"]
    finally:
        h2.close()
        tx2.close()


def test_default_publisher_uses_transport_constructor(monkeypatch, tmp_path):
    calls = {}
    instances = []

    class FakePublisher:
        def __init__(self, mqtt, **kwargs):
            calls["mqtt"] = mqtt
            calls.update(kwargs)
            self.started = False
            self.closed = False
            instances.append(self)

        def start(self):
            self.started = True

        def close(self):
            self.closed = True

    import core_parking.transport as transport
    monkeypatch.setattr(transport, "SpoolPublisher", FakePublisher)
    h = SlotHooks()
    h.configure({"site_id": "demo-lot", "device_id": "unused",
                 "state_dir": str(tmp_path),
                 "mqtt": {"host": "localhost", "port": 1883,
                          "username": "u", "password": "", "keepalive_s": 30},
                 "spool": {"max_events": 17},
                 "slots": {"cam-1": [{"id": "A", "polygon": []}]}}, "dev-1")
    try:
        assert calls["client_id"] == "demo-lot-dev-1-app"
        assert calls["site_id"] == "demo-lot"
        assert calls["spool_path"] == tmp_path / "spool.jsonl"
        assert calls["max_events"] == 17
        assert h.tx.started is True
        first = h.tx
    finally:
        h.close()
    assert first.closed is True
    assert h.tx is None

    # A closed hook can be configured again; a fresh owned publisher is made.
    h.configure({"site_id": "demo-lot", "state_dir": str(tmp_path),
                 "mqtt": {"host": "localhost", "port": 1883,
                          "username": "u", "password": "", "keepalive_s": 30},
                 "spool": {"max_events": 17}, "slots": {}}, "dev-2")
    try:
        assert len(instances) == 2
        assert h.tx is instances[1] and h.tx.started is True
    finally:
        h.close()
    assert instances[1].closed is True

    # Injected publishers remain test-owned and are never started or closed.
    injected = FakePublisher({}, client_id="x")
    h = SlotHooks()
    h.tx = injected
    h.configure({"site_id": "demo-lot", "state_dir": str(tmp_path),
                 "mqtt": {"host": "localhost", "port": 1883,
                          "username": "u", "password": "", "keepalive_s": 30},
                 "spool": {"max_events": 17}, "slots": {}}, "dev-3")
    h.close()
    assert injected.started is False and injected.closed is False


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


def test_health_uses_spool_publisher_health_before_connection(tmp_path):
    tx = SpoolPublisher({"host": "127.0.0.1", "port": 1},
                        client_id="slots-health", site_id="demo-lot",
                        spool_path=tmp_path / "spool.jsonl", max_events=10,
                        client_factory=FakeMqtt)
    hooks = SlotHooks()
    hooks.tx = tx
    try:
        assert hooks.health() == {"spool_depth": 0, "spool_overflow": False,
                                  "mqtt_app_connected": False,
                                  "streams_ok": {}}
    finally:
        tx.close()


def test_health_keeps_fake_and_legacy_attribute_fallbacks():
    class HealthPublisher:
        spool_depth = 99
        spool_overflow = True
        mqtt_app_connected = False

        def health(self):
            return {"spool_depth": 3, "spool_overflow": False,
                    "mqtt_app_connected": True}

    hooks = SlotHooks()
    hooks.tx = HealthPublisher()
    assert hooks.health()["spool_depth"] == 3
    assert hooks.health()["mqtt_app_connected"] is True

    class LegacyPublisher:
        spool_depth = 4
        spool_overflow = True
        mqtt_app_connected = False

    hooks.tx = LegacyPublisher()
    assert hooks.health()["spool_depth"] == 4
    assert hooks.health()["spool_overflow"] is True
    assert hooks.health()["mqtt_app_connected"] is False


def test_protocol_attrs():
    hooks = SlotHooks()
    assert hooks.plugins() == []
    assert hooks.name == "edge-parking-slots/0.1.0"
    assert hooks.wants_frames is False

# ------------------------------------------------------------------ monotonic deadline candidate tests

def _timer_candidate_hooks(unknown_after_s=10.0):
    _timer_wall[0] = 0
    _timer_mono[0] = 0
    h = SlotHooks(); h.tx = RecordingPublisher(); h.tick_s = 60.0
    h.now_ms = lambda: _timer_wall[0]
    h.now_mono_ms = lambda: _timer_mono[0]
    h.configure({"unknown_after_s": unknown_after_s,
                 "slots": {"cam": [{"id": "A", "polygon": []}]}}, "dev-1")
    ctx = FakeStreamContext("cam"); h.on_stream_added(ctx)
    return h, ctx

_timer_wall = [0]
_timer_mono = [0]


def test_deadline_before_boundary_and_exactly_once():
    h, ctx = _timer_candidate_hooks()
    try:
        h.on_stream_state(ctx, "reconnecting")
        h._tick(9_999, 9_999)
        assert ctx.state["stream_ok"] is True and len(h.tx.records) == 0
        h._tick(10_000, 10_000)
        assert ctx.state["stream_ok"] is False and len(h.tx.records) == 1
        h._tick(10_001, 10_001)
        assert len(h.tx.records) == 1
    finally:
        h.close()


def test_next_wait_uses_earliest_stream_deadline_and_new_earlier_wakes():
    h = SlotHooks(); h.tx = RecordingPublisher(); h.tick_s = 60.0
    h.now_ms = lambda: _timer_wall[0]; h.now_mono_ms = lambda: _timer_mono[0]
    h.configure({"unknown_after_s": 10, "slots": {"a": [{"id": "A"}],
                 "b": [{"id": "B"}]}}, "dev-1")
    a, b = FakeStreamContext("a"), FakeStreamContext("b")
    h.on_stream_added(a); h.on_stream_added(b)
    try:
        _timer_mono[0] = 0; h.on_stream_state(a, "reconnecting")
        _timer_mono[0] = 5_000; h.on_stream_state(b, "reconnecting")
        _timer_mono[0] = 5_000
        assert 4.9 <= h._next_deadline_wait_s() <= 5.1
        _timer_mono[0] = 1_000
        h._deadlines_mono_ms["b"] = 2_000; h._wake.set()
        assert h._next_deadline_wait_s() <= 1.1
    finally:
        h.close()


def test_recovery_cancels_deadline_and_restores_before_expiry():
    h, ctx = _timer_candidate_hooks()
    try:
        h.on_event(ctx, make_event(type="slot_change",
                                   fields={"slot_id": "A", "state": "occupied",
                                           "coverage": 0.8}))
        h.on_stream_state(ctx, "reconnecting")
        _timer_mono[0] = 9_999
        h.on_stream_state(ctx, "running")
        h._tick(1_000_000, 20_000)
        assert ctx.state["stream_ok"] is True
        assert ctx.state["slots"]["A"]["state"] == "occupied"
        assert len(h.tx.records) == 2  # slot event + recovery snapshot
    finally:
        h.close()


def test_wake_event_breaks_long_wait_at_deadline_and_close_is_prompt():
    h, ctx = _timer_candidate_hooks()
    try:
        h.on_stream_state(ctx, "reconnecting")
        _timer_mono[0] = 10_000
        started = time.monotonic()
        h._wake.set()
        while not h.tx.records and time.monotonic() - started < 0.5:
            time.sleep(0.005)
        assert ctx.state["stream_ok"] is False
        assert time.monotonic() - started < 0.5
    finally:
        started = time.monotonic(); h.close(); assert time.monotonic() - started < 0.5


def test_monotonic_deadline_ignores_wall_clock_jump():
    h, ctx = _timer_candidate_hooks()
    try:
        h.on_stream_state(ctx, "reconnecting")
        h._tick(10_000_000, 9_999)
        assert ctx.state["stream_ok"] is True
        h._tick(10_000_000, 10_000)
        assert ctx.state["stream_ok"] is False
    finally:
        h.close()


# ------------------------------------------------------------------ r2 membership and natural timer tests

def test_removed_context_late_state_event_cannot_leave_deadline_or_publish():
    h, old = _timer_candidate_hooks()
    try:
        h.on_stream_state(old, "reconnecting")
        h.on_stream_removed(old)
        h.on_stream_state(old, "error")
        assert h.on_event(old, make_event(type="slot_change", fields={
            "slot_id": "A", "state": "occupied", "coverage": 0.8})) == []
        assert h._deadlines_mono_ms == {}
        assert h._next_deadline_wait_s() == h.tick_s
        assert h.tx.records == []
    finally:
        h.close()


def test_replacement_identity_blocks_old_remove_state_and_event():
    h, old = _timer_candidate_hooks()
    new = FakeStreamContext("cam")
    try:
        h.on_stream_added(old)
        h.on_stream_state(old, "reconnecting")
        h.on_stream_added(new)
        h.on_stream_removed(old)
        h.on_stream_state(old, "error")
        assert h.on_event(old, make_event(type="slot_change", fields={
            "slot_id": "A", "state": "occupied", "coverage": 0.8})) == []
        assert h._streams["cam"] is new
        assert h._deadlines_mono_ms == {}
        h.on_stream_state(new, "reconnecting")
        assert "cam" in h._deadlines_mono_ms
    finally:
        h.close()


def test_repeated_down_does_not_reset_monotonic_deadline():
    h, ctx = _timer_candidate_hooks()
    try:
        _timer_mono[0] = 0
        h.on_stream_state(ctx, "reconnecting")
        first = h._deadlines_mono_ms["cam"]
        _timer_mono[0] = 5_000
        h.on_stream_state(ctx, "error")
        assert h._deadlines_mono_ms["cam"] == first
    finally:
        h.close()


def test_production_state_api_natural_timer_wakes_without_manual_tick():
    h = SlotHooks(); h.tx = RecordingPublisher(); h.tick_s = 60.0
    h.configure({"unknown_after_s": 0.05,
                 "slots": {"cam": [{"id": "A", "polygon": []}]}}, "dev-1")
    ctx = FakeStreamContext("cam"); h.on_stream_added(ctx)
    try:
        h.on_stream_state(ctx, "reconnecting")
        started = time.monotonic()
        while not h.tx.records and time.monotonic() - started < 0.5:
            time.sleep(0.005)
        assert ctx.state["stream_ok"] is False
        assert h.tx.records and time.monotonic() - started < 0.5
    finally:
        h.close()


def test_health_snapshot_survives_concurrent_add_remove():
    h = SlotHooks(); h.tx = RecordingPublisher(); h.tick_s = 60.0
    h.configure({"slots": {"cam": [{"id": "A", "polygon": []}]}}, "dev-1")
    ctx = FakeStreamContext("cam"); errors = []
    def mutate():
        try:
            for _ in range(100):
                h.on_stream_added(ctx); h.on_stream_removed(ctx)
        except Exception as exc:
            errors.append(exc)
    t = threading.Thread(target=mutate); t.start()
    while t.is_alive():
        h.health()
    t.join()
    try:
        assert errors == []
        assert h.health()["streams_ok"] == {}
    finally:
        h.close()
