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

from ..classes import CLASS_IDS

log = logging.getLogger(__name__)

__all__ = ["SlotHooks", "SlotsOptions", "slot_coverage_config"]

_STATES = ("occupied", "free", "unknown")


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
            occupied_ratio=float(d.get("occupied_ratio", 0.30)),
            free_ratio=float(d.get("free_ratio", 0.20)),
            hold_s=float(d.get("hold_s", 3.0)),
            free_hold_s=float(d.get("free_hold_s", 5.0)),
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
        self.tick_s = 1.0            # 断流检查周期（§6.2：1 s 定时线程）
        self.now_ms = lambda: int(time.time() * 1000)  # 测试注入可控时钟
        self._streams: dict[str, object] = {}
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None

    # ------------------------------------------------------------------ 基座约定

    def configure(self, options: dict, device_id: str) -> None:
        self.options = dict(options or {})
        self.device_id = device_id
        self.slots_opt = SlotsOptions.from_dict(self.options)
        self.classes = tuple(int(c) for c in self.options.get("classes", CLASS_IDS))
        if self.tx is None:
            self.tx = self._make_default_tx()
        self._stop.clear()
        self._timer = threading.Thread(target=self._tick_loop,
                                       name="parking-slots-tick", daemon=True)
        self._timer.start()

    def _make_default_tx(self):
        from ..transport import SpoolPublisher  # 惰性 import：P1 测试注入 self.tx
        return SpoolPublisher.from_options(self.options, self.device_id)

    def close(self) -> None:
        self._stop.set()
        if self._timer is not None:
            self._timer.join(timeout=2.0)
            self._timer = None

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
        now = self.now_ms()
        ctx.state["slots"] = {s["id"]: _slot_entry(now)
                              for s in self.slots_opt.slots.get(ctx.stream_id, [])}
        ctx.state["stream_ok"] = True
        ctx.state["down_since"] = None
        ctx.state["saved_slots"] = None
        self._streams[ctx.stream_id] = ctx

    def on_stream_removed(self, ctx) -> None:
        self._streams.pop(ctx.stream_id, None)

    def on_event(self, ctx, ev) -> list:
        if ev.type != "slot_change":
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
        if state in ("reconnecting", "error"):
            if ctx.state["down_since"] is None:
                ctx.state["down_since"] = self.now_ms()
        elif state == "running":
            if not ctx.state["stream_ok"]:
                saved = ctx.state.get("saved_slots")
                if saved is not None:
                    ctx.state["slots"] = saved
                    ctx.state["saved_slots"] = None
            ctx.state["down_since"] = None
            ctx.state["stream_ok"] = True
            self.publish_slots(ctx, changed=[], ts=self.now_ms())
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
        return ctx.state["slots"]

    # ------------------------------------------------------------------ 断流计时

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self._tick(self.now_ms())
            except Exception:  # 定时线程不能死；错误留日志
                log.exception("slots tick failed")

    def _tick(self, now_ms: int) -> None:
        for ctx in list(self._streams.values()):
            down_since = ctx.state.get("down_since")
            if down_since is None or not ctx.state.get("stream_ok", True):
                continue
            if now_ms - down_since >= self.slots_opt.unknown_after_s * 1000:
                # 保存最后已知状态（恢复 running 时回滚，§6.2）
                ctx.state["saved_slots"] = copy.deepcopy(ctx.state["slots"])
                now = self.now_ms()
                for entry in ctx.state["slots"].values():
                    entry.update(state="unknown", since=now)
                ctx.state["stream_ok"] = False
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
        return {
            "spool_depth": getattr(tx, "spool_depth", None),
            "spool_overflow": getattr(tx, "spool_overflow", None),
            "mqtt_app_connected": getattr(tx, "mqtt_app_connected", None),
            "streams_ok": {sid: bool(ctx.state.get("stream_ok", True))
                           for sid, ctx in self._streams.items()},
        }
