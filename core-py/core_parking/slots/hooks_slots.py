"""NRD-4 车位钩子（规格 §6.1/§6.2，§8.1 P1 行）。

职责边界（§6.2）：车位判定与去抖在基座内置分析器 `slot_coverage`
（BASE-1 M4.1，本仓库不实现）；本钩子是纯事件级汇总：

- ``on_stream_added``：按 ``options.slots[stream_id]`` 初始化车位状态表
  （全部 ``unknown``）；``stream_ok=True``、``down_since=None``；
- ``on_event``（``slot_change``）：更新单个车位的 ``state/since/cover``
  并立即发布 §6.1 payload（``changed=[slot_id]``）；
- ``on_stream_state``（NRD-2 §S13 B1，基座 M4.1b 尚未实现，P1 对 fake
  开发）：``reconnecting``/``error`` 记 ``down_since``；``running`` 时
  若处于断流态则恢复断流前保存的最后已知状态（与 vb-runtime 内分析器
  状态一致；vb-runtime 重启后分析器重建、首帧会重发全部 slot_change），
  并发布一次 ``changed=[]`` 的快照；
- 1 s 定时线程：``down_since`` 超过 ``unknown_after_s``（默认 10 s）且
  ``stream_ok`` 为真 → 保存最后已知状态、全部车位置 ``unknown``、
  ``stream_ok=False``、立即发布一次；
- ``update_slots``：运行中新增/删除车位（``PUT /config/slots`` 的钩子侧
  部分，HTTP 端点是 P3）：``configure_analyzer`` 重配（NRD-2 §S13 B3），
  新增车位初始 ``unknown``、删除车位从 payload 移除，发布一次。

发布走注入的 ``self.tx``（C2 ``transport.SpoolPublisher``，接口
``publish(kind, ctx, body, ts)``；``slots`` 在 envelope.RETAINED_KINDS
中，retain 由发布器负责）。实例化约定同 ``hooks_counting.ParkingHooks``：
``SlotHooks()`` 后由基座调 ``configure(options, device_id)``。

测试注入：``tx``（RecordingPublisher）、``now_ms``（可控时钟）、
``tick_s``（定时周期）。
"""
from __future__ import annotations

import copy
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..classes import CLASS_IDS
from ..envelope import SeqAllocator

log = logging.getLogger(__name__)

__all__ = ["SlotHooks", "SlotsOptions", "slot_coverage_config", "is_convex"]

_STATES = ("occupied", "free", "unknown")


def is_convex(poly) -> bool:
    """多边形凸性校验（§6.2：只在配置时调用一次，HTTP 层与基座 configure 双侧校验）。

    ``poly`` 为 ``[[x, y], ...]``（归一化坐标）。≥3 个顶点、所有相邻边叉积
    同号（允许共线边）才算凸；非凸或退化（全共线）返回 ``False``。
    配置输入校验，不属于逐帧计算。
    """
    try:
        pts = [(float(p[0]), float(p[1])) for p in poly]
    except (TypeError, ValueError, IndexError):
        return False
    n = len(pts)
    if n < 3:
        return False
    sign = 0
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        x3, y3 = pts[(i + 2) % n]
        cross = (x2 - x1) * (y3 - y2) - (y2 - y1) * (x3 - x2)
        if abs(cross) < 1e-12:
            continue                      # 共线边不影响凸性
        s = 1 if cross > 0 else -1
        if sign and s != sign:
            return False
        sign = s
    return sign != 0


@dataclass
class SlotsOptions:
    """``app.options`` 中 slots 相关键的解析结果（全量校验归 P0/C3；事件级兜底）。"""

    slots: dict = field(default_factory=dict)   # stream_id -> [{"id","polygon"}]
    unknown_after_s: float = 10.0
    occupied_ratio: float = 0.30                # §6.2：cover_threshold 0.30 映射
    free_ratio: float = 0.20                    # 滞回带下沿
    hold_s: float = 3.0                         # enter_k 3 @1fps → 按时间
    free_hold_s: float = 5.0                    # leave_k 5 @1fps → 按时间

    @classmethod
    def from_dict(cls, d: dict) -> "SlotsOptions":
        d = dict(d or {})
        coverage = dict(d.get("slot_coverage") or {})
        slots = {str(k): [dict(s) for s in v] for k, v in (d.get("slots") or {}).items()}
        for stream_id, lst in slots.items():
            ids = [s.get("id") for s in lst]
            if len(set(ids)) != len(ids):
                raise ValueError(f"slots[{stream_id!r}] has duplicate slot ids: {ids!r}")
            for s in lst:
                if not s.get("id"):
                    raise ValueError(f"slots[{stream_id!r}] entry missing 'id': {s!r}")
        opt = cls(
            slots=slots,
            unknown_after_s=float(d.get("unknown_after_s", 10.0)),
            occupied_ratio=float(coverage.get("occupied_ratio", d.get("occupied_ratio", 0.30))),
            free_ratio=float(coverage.get("free_ratio", d.get("free_ratio", 0.20))),
            hold_s=float(coverage.get("hold_s", d.get("hold_s", 3.0))),
            free_hold_s=float(coverage.get("free_hold_s", d.get("free_hold_s", 5.0))),
        )
        if opt.unknown_after_s <= 0:
            raise ValueError("unknown_after_s must be > 0")
        if not 0.0 <= opt.free_ratio <= opt.occupied_ratio <= 1.0:
            raise ValueError("require 0 <= free_ratio <= occupied_ratio <= 1")
        return opt


def slot_coverage_config(opts: SlotsOptions, stream_id: str,
                         classes=CLASS_IDS) -> dict:
    """``app.options`` → 基座 `slot_coverage` 分析器配置（§6.2 参数映射）。

    ``unknown_after_s`` 是事件级（钩子断流计时），不下发给分析器。
    """
    return {
        "slots": [{"id": s["id"], "polygon": [list(p) for p in s.get("polygon", [])]}
                  for s in opts.slots.get(stream_id, [])],
        "occupied_ratio": opts.occupied_ratio,
        "free_ratio": opts.free_ratio,
        "hold_s": opts.hold_s,
        "free_hold_s": opts.free_hold_s,
        "classes": list(classes),
    }


def _slot_entry(now_ms: int) -> dict:
    return {"state": "unknown", "since": now_ms, "cover": 0.0}


class SlotHooks:
    """`app.module` 指向的钩子类：``core_parking.slots.hooks_slots:SlotHooks``。"""

    name = "edge-parking-slots/0.1.0"
    wants_frames = False

    def __init__(self) -> None:
        self.options: dict = {}
        self.device_id = ""
        self.slots_opt = SlotsOptions()
        self.classes: tuple[int, ...] = CLASS_IDS
        self.tx = None               # 发布者（C2 SpoolPublisher；测试注入 fake）
        self._owns_tx = False
        self.tick_s = 1.0            # 断流检查周期（§6.2：1 s 定时线程）
        self.now_ms = lambda: int(time.time() * 1000)  # 测试注入可控时钟
        self.now_mono_ms = lambda: int(time.monotonic() * 1000)
        self._wake = threading.Event()
        self._streams_lock = threading.RLock()
        self._deadlines_mono_ms: dict[str, int] = {}
        self._streams: dict[str, object] = {}
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None

    # ------------------------------------------------------------------ 基座约定

    def configure(self, options: dict, device_id: str) -> None:
        self.options = dict(options or {})
        self.device_id = device_id
        self.slots_opt = SlotsOptions.from_dict(self.options)
        coverage = dict(self.options.get("slot_coverage") or {})
        classes = coverage.get("classes", self.options.get("classes", CLASS_IDS))
        self.classes = tuple(int(c) for c in classes)
        if self.tx is None:
            self.tx = self._make_default_tx()
            self._owns_tx = True
            self.tx.start()
        self._stop.clear()
        self._wake.clear()
        self._timer = threading.Thread(target=self._tick_loop,
                                       name="parking-slots-tick", daemon=True)
        self._timer.start()

    def _make_default_tx(self):
        from ..transport import SpoolPublisher  # 惰性 import：P1 测试注入 self.tx
        spool = dict(self.options.get("spool") or {})
        state_dir = self.options.get("state_dir", "state")
        return SpoolPublisher(
            dict(self.options["mqtt"]),
            client_id=f"{self.options['site_id']}-{self.device_id}-app",
            site_id=self.options["site_id"],
            spool_path=Path(state_dir) / "spool.jsonl",
            max_events=int(spool.get("max_events", 50000)),
        )

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._timer is not None:
            self._timer.join(timeout=2.0)
            self._timer = None
        if self._owns_tx:
            close = getattr(self.tx, "close", None)
            if callable(close):
                close()
            self.tx = None
            self._owns_tx = False

    # ------------------------------------------------------------------ AppHooks

    def analyzers(self, spec) -> list[dict]:
        stream_id = getattr(spec, "stream_id", spec)
        if not self.slots_opt.slots.get(stream_id):
            return []
        return [{"name": "slot_coverage",
                 "config": slot_coverage_config(self.slots_opt, stream_id,
                                                self.classes)}]

    def plugins(self) -> list[str]:
        return []

    def on_stream_added(self, ctx) -> None:
        with self._streams_lock:
            now = self.now_ms()
            ctx.state["seq"] = SeqAllocator(Path(self.options.get("state_dir", "state")) /
                                             ctx.stream_id)
            ctx.state["slots"] = {s["id"]: _slot_entry(now)
                                  for s in self.slots_opt.slots.get(ctx.stream_id, [])}
            ctx.state["stream_ok"] = True
            ctx.state["down_since"] = None
            ctx.state["saved_slots"] = None
            self._streams[ctx.stream_id] = ctx
            self._deadlines_mono_ms.pop(ctx.stream_id, None)
        self._wake.set()

    def on_stream_removed(self, ctx) -> None:
        with self._streams_lock:
            if self._streams.get(ctx.stream_id) is not ctx:
                return
            self._streams.pop(ctx.stream_id, None)
            self._deadlines_mono_ms.pop(ctx.stream_id, None)
        self._wake.set()

    def on_event(self, ctx, ev) -> list:
        if ev.type != "slot_change":
            return []
        with self._streams_lock:
            return self._on_event_locked(ctx, ev)

    def _on_event_locked(self, ctx, ev) -> list:
        if self._streams.get(ctx.stream_id) is not ctx:
            return []
        slots = ctx.state["slots"]
        slot_id = ev.fields["slot_id"]
        if slot_id not in slots:
            # 分析器配置与钩子状态不一致：报错比静默吞掉好排查
            raise KeyError(f"slot_change for unknown slot {slot_id!r} "
                           f"on stream {ctx.stream_id!r}")
        state = ev.fields["state"]
        if state not in _STATES:
            raise ValueError(f"slot_change state must be one of {_STATES}, got {state!r}")
        slots[slot_id].update(state=state, since=int(ev.wall_ms),
                              cover=float(ev.fields["coverage"]))
        self.publish_slots(ctx, changed=[slot_id], ts=int(ev.wall_ms))
        return []  # 停车事件走 SpoolPublisher，不经基座 PublishWorker

    def on_stream_state(self, ctx, state: str, error=None) -> list:
        """NRD-2 §S13 B1（基座 M4.1b 未实现；P1 由测试直接调用，fake 顶替）。"""
        with self._streams_lock:
            if self._streams.get(ctx.stream_id) is not ctx:
                return []
            if state in ("reconnecting", "error"):
                if ctx.state["down_since"] is None:
                    ctx.state["down_since"] = self.now_ms()
                    self._deadlines_mono_ms[ctx.stream_id] = (
                        self.now_mono_ms() + int(self.slots_opt.unknown_after_s * 1000))
            elif state == "running":
                if not ctx.state["stream_ok"]:
                    saved = ctx.state.get("saved_slots")
                    if saved is not None:
                        ctx.state["slots"] = saved
                        ctx.state["saved_slots"] = None
                ctx.state["down_since"] = None
                ctx.state["stream_ok"] = True
                self._deadlines_mono_ms.pop(ctx.stream_id, None)
                self.publish_slots(ctx, changed=[], ts=self.now_ms())
        self._wake.set()
        return []

    # ------------------------------------------------------------------ 车位增删

    def update_slots(self, stream_id: str, new_slots: list[dict]) -> dict:
        """运行中重配一路的车位（``PUT /config/slots`` 的钩子侧；HTTP 端点是 P3）。

        校验 id 唯一 → ``ctx.configure_analyzer("slot_coverage", …)``（B3）
        → 新增车位初始 ``unknown``、删除车位从状态表移除 → 发布一次全量。
        返回新的状态表。多边形凸性校验在 P3 的 HTTP 层与基座 configure
        双侧做（§6.2），这里不重复。
        """
        ids = [s.get("id") for s in new_slots]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate slot ids: {ids!r}")
        for s in new_slots:
            if not s.get("id"):
                raise ValueError(f"slot entry missing 'id': {s!r}")
        with self._streams_lock:
            ctx = self._streams.get(stream_id)
            if ctx is None:
                raise KeyError(f"unknown stream {stream_id!r}")
            self.slots_opt.slots[stream_id] = [dict(s) for s in new_slots]
            ctx.configure_analyzer("slot_coverage",
                                   slot_coverage_config(self.slots_opt, stream_id,
                                                        self.classes))
            now = self.now_ms()
            old = ctx.state["slots"]
            ctx.state["slots"] = {s["id"]: old.get(s["id"], _slot_entry(now))
                                  for s in new_slots}
            self.publish_slots(ctx, changed=[], ts=now)
        self._wake.set()
        return ctx.state["slots"]

    # ------------------------------------------------------------------ 断流计时

    def _next_deadline_wait_s(self) -> float:
        with self._streams_lock:
            active_deadlines = [deadline for sid, deadline in self._deadlines_mono_ms.items()
                                if self._streams.get(sid) is not None]
            if not active_deadlines:
                return self.tick_s
            remaining = min(active_deadlines) - self.now_mono_ms()
            return max(0.0, remaining / 1000.0)

    def _tick_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self._next_deadline_wait_s())
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                self._tick(self.now_ms(), self.now_mono_ms())
            except Exception:  # 定时线程不能死；错误留日志
                log.exception("slots tick failed")

    def _tick(self, now_ms: int, mono_ms: int | None = None) -> None:
        with self._streams_lock:
            for ctx in list(self._streams.values()):
                if self._streams.get(ctx.stream_id) is not ctx:
                    continue
                down_since = ctx.state.get("down_since")
                if down_since is None or not ctx.state.get("stream_ok", True):
                    continue
                deadline = self._deadlines_mono_ms.get(ctx.stream_id)
                expired = ((mono_ms is not None and deadline is not None and mono_ms >= deadline) or
                           (mono_ms is None and now_ms - down_since >= self.slots_opt.unknown_after_s * 1000))
                if not expired:
                    continue
                # 保存最后已知状态（恢复 running 时回滚，§6.2）
                ctx.state["saved_slots"] = copy.deepcopy(ctx.state["slots"])
                now = self.now_ms()
                for entry in ctx.state["slots"].values():
                    entry.update(state="unknown", since=now)
                ctx.state["stream_ok"] = False
                self._deadlines_mono_ms.pop(ctx.stream_id, None)
                self.publish_slots(ctx, changed=[], ts=now)

    # ------------------------------------------------------------------ 发布

    def publish_slots(self, ctx, changed: list[str], ts: int) -> None:
        """组 §6.1 payload（信封字段 event_id/site_id/ts/seq 由 SpoolPublisher 补）。"""
        slots = ctx.state["slots"]
        body = {
            "changed": list(changed),
            "slots": [{"id": sid, "state": e["state"], "since": int(e["since"]),
                       "cover": float(e["cover"])}
                      for sid, e in slots.items()],
            "occupied": sum(1 for e in slots.values() if e["state"] == "occupied"),
            "free": sum(1 for e in slots.values() if e["state"] == "free"),
            "unknown": sum(1 for e in slots.values() if e["state"] == "unknown"),
            "stream_ok": bool(ctx.state.get("stream_ok", True)),
        }
        self.tx.publish(kind="slots", ctx=ctx, body=body, ts=ts)

    def health(self) -> dict:  # §S13 B2：基座放入 /healthz 的 app 子对象
        tx = self.tx
        transport_health = {}
        health_fn = getattr(tx, "health", None)
        if callable(health_fn):
            try:
                reported = health_fn()
            except Exception:  # keep legacy/fake attribute fallback below
                reported = {}
            if isinstance(reported, dict):
                transport_health = reported
        with self._streams_lock:
            streams_ok = {sid: bool(ctx.state.get("stream_ok", True))
                          for sid, ctx in self._streams.items()}
        return {
            "spool_depth": transport_health.get(
                "spool_depth", getattr(tx, "spool_depth", None)),
            "spool_overflow": transport_health.get(
                "spool_overflow", getattr(tx, "spool_overflow", None)),
            "mqtt_app_connected": transport_health.get(
                "mqtt_app_connected", getattr(tx, "mqtt_app_connected", None)),
            "streams_ok": streams_ok,
        }
