"""NRD-2 计数钩子（规格 §6.2/§6.3）：基座 `line_cross` 事件 → 进/出映射 → Occupancy → 发布。

实例化约定按 BASE-1 M1.12/M1.20（`vision_base.hooks.EchoApp` 同款）：
``ParkingHooks()`` 后由基座调用 ``configure(options, device_id)``；
``options`` 为 ``vb.config/1`` 的 ``app.options``（§S2）。

发布走注入的 ``self.tx``（C2 的 ``transport.SpoolPublisher``，接口
``publish(kind, ctx, body, ts)``）；未注入时首次使用前惰性 import
``core_parking.transport``（C1 阶段测试注入 `tests/fakes.RecordingPublisher`）。

`state_dir` 来自 ``options["state_dir"]``（顶层 ``vb.config/1`` 的同名键由
C3 的 config.py 注入 app.options；缺省 ``"state"``）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .classes import CLASS_IDS, vehicle_class
from .occupancy import Occupancy, SeqAllocator

log = logging.getLogger(__name__)

_PREVIEW_FRAMES = 64   # 每路保留的最近抽样帧数（/preview.json?seq= 对齐快照）

__all__ = ["CountingOptions", "ParkingHooks", "line_cross_config", "map_direction"]


def map_direction(bdir: str, in_side: str) -> str:
    """基座 line_cross 方向 → 停车 in/out（规格 §6.2）。

    BASE-1：``forward`` = 正侧 → 负侧（正侧 = 有向线 a→b 左侧，已由 BASE-1
    M1.6 fixture `line_cross_basic.json` 核实）；``backward`` 反之。
    终点落在 ``in_side`` 一侧即 "in"。
    """
    if bdir not in ("forward", "backward"):
        raise ValueError(f"unknown line_cross direction {bdir!r}")
    if in_side not in ("positive", "negative"):
        raise ValueError(f"in_side must be 'positive' or 'negative', got {in_side!r}")
    dest = "negative" if bdir == "forward" else "positive"
    return "in" if dest == in_side else "out"


@dataclass
class CountingOptions:
    """``app.options.counting`` 的解析结果（全量校验在 C3 config.py；这里做事件级兜底）。"""

    line: dict = field(default_factory=lambda: {"a": [0.10, 0.60], "b": [0.90, 0.60]})
    in_side: str = "positive"
    count_directions: tuple[str, ...] = ("in", "out")
    band: float = 0.03
    segment_margin: float = 0.05
    min_track_frames: int = 3
    min_travel: float = 0.06
    reborn_window_s: float = 2.0
    reborn_dist: float = 0.08
    capacity: int = 0
    initial_occupancy: int = 0
    publish_interval_ms: int = 1000

    @classmethod
    def from_dict(cls, d: dict) -> "CountingOptions":
        d = dict(d or {})
        opt = cls(
            line=dict(d.get("line", {"a": [0.10, 0.60], "b": [0.90, 0.60]})),
            in_side=str(d.get("in_side", "positive")),
            count_directions=tuple(d.get("count_directions", ("in", "out"))),
            band=float(d.get("band", 0.03)),
            segment_margin=float(d.get("segment_margin", 0.05)),
            min_track_frames=int(d.get("min_track_frames", 3)),
            min_travel=float(d.get("min_travel", 0.06)),
            reborn_window_s=float(d.get("reborn_window_s", 2.0)),
            reborn_dist=float(d.get("reborn_dist", 0.08)),
            capacity=int(d.get("capacity", 0)),
            initial_occupancy=int(d.get("initial_occupancy", 0)),
            publish_interval_ms=int(d.get("publish_interval_ms", 1000)),
        )
        if opt.in_side not in ("positive", "negative"):
            raise ValueError(f"counting.in_side must be 'positive' or 'negative', got {opt.in_side!r}")
        bad = set(opt.count_directions) - {"in", "out"}
        if bad or not opt.count_directions:
            raise ValueError(f"counting.count_directions invalid: {opt.count_directions!r}")
        if opt.capacity < 0:
            raise ValueError("counting.capacity must be >= 0")
        return opt


COUNTING_OVERRIDE_NAME = "app-options.override.json"


def load_counting_override(state_dir: Path) -> dict | None:
    """读 ``state_dir/app-options.override.json`` 的 ``counting`` 段（``PUT /config/counting`` 写入）。

    文件不存在返回 None；内容损坏或校验失败记 warning 并返回 None（回落部署时的配置），
    不让一份坏的 override 把应用卡在启动阶段。
    """
    path = Path(state_dir) / COUNTING_OVERRIDE_NAME
    if not path.is_file():
        return None
    try:
        import json
        data = json.loads(path.read_text(encoding="utf-8"))
        counting = data.get("counting") if isinstance(data, dict) else None
        if not isinstance(counting, dict):
            return None
        CountingOptions.from_dict(counting)
        return counting
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.warning("ignoring %s: %s", path, exc)
        return None


def line_cross_config(counting: CountingOptions, classes=CLASS_IDS) -> dict:
    """``app.options.counting`` → 基座 `line_cross` 分析器配置（规格 §6.2 映射表）。

    `in_side` / `count_directions` / `capacity` / `initial_occupancy` /
    `publish_interval_ms` 不下发（事件级，钩子使用）。
    """
    return {
        "lines": [{"id": "main", "a": list(counting.line["a"]), "b": list(counting.line["b"])}],
        "band": counting.band,
        "segment_margin": counting.segment_margin,
        "min_track_frames": counting.min_track_frames,
        "min_travel": counting.min_travel,
        "reborn_window_s": counting.reborn_window_s,
        "reborn_dist": counting.reborn_dist,
        "classes": list(classes),
    }


class ParkingHooks:
    """`app.module` 指向的钩子类（§S2）：``core_parking.hooks_counting:ParkingHooks``。"""

    name = "edge-parking-counting/0.1.0"
    wants_frames = True  # 只为 /preview.json 保留最近一帧的框（C3）；抽样见 frame_stride

    def __init__(self) -> None:
        self.options: dict = {}
        self.device_id = ""
        self.counting = CountingOptions()
        self.classes: tuple[int, ...] = CLASS_IDS
        self.preview_frame_stride = 1
        self.state_dir = Path("state")
        self.tx = None               # 发布者（C2 SpoolPublisher；测试注入 fake）
        self.tick_s = 1.0            # 定时线程周期（测试可调小）
        self._streams: dict[str, object] = {}
        self._last_event_ts: int | None = None
        self._stop = threading.Event()
        self._timer: threading.Thread | None = None

    # ------------------------------------------------------------------ 基座约定

    def configure(self, options: dict, device_id: str) -> None:
        self.options = dict(options or {})
        self.device_id = device_id
        override = load_counting_override(Path(self.options.get("state_dir", "state")))
        if override is not None:
            self.options["counting"] = override   # PUT /config/counting 的结果跨重启保留
        self.counting = CountingOptions.from_dict(self.options.get("counting", {}))
        self.classes = tuple(int(c) for c in self.options.get("classes", CLASS_IDS))
        self.preview_frame_stride = max(1, int(self.options.get("preview_frame_stride", 1)))
        self.state_dir = Path(self.options.get("state_dir", "state"))
        if self.tx is None:
            self.tx = self._make_default_tx()
        self._stop.clear()
        self._timer = threading.Thread(target=self._tick_loop,
                                       name="parking-occupancy-tick", daemon=True)
        self._timer.start()

    def _make_default_tx(self):
        # C2 的 transport.SpoolPublisher；C1 阶段不存在，测试必须注入 self.tx。
        from .transport import SpoolPublisher  # 惰性 import：C1 不依赖 C2 文件存在
        return SpoolPublisher.from_options(self.options, self.device_id)

    def close(self) -> None:
        self._stop.set()
        if self._timer is not None:
            self._timer.join(timeout=2.0)
            self._timer = None

    # ------------------------------------------------------------------ AppHooks

    def analyzers(self, spec) -> list[dict]:
        return [{"name": "line_cross",
                 "config": line_cross_config(self.counting, self.classes)}]

    def plugins(self) -> list[str]:
        return []

    def on_stream_added(self, ctx) -> None:
        occ = Occupancy.boot(self.state_dir / ctx.stream_id,
                             self.counting.capacity,
                             self.counting.initial_occupancy)
        ctx.state["occ"] = occ
        ctx.state["seq"] = SeqAllocator(self.state_dir / ctx.stream_id)
        ctx.state["last_occ_pub_ms"] = 0
        self._streams[ctx.stream_id] = ctx
        self._publish_occupancy(ctx, "boot")

    def on_stream_removed(self, ctx) -> None:
        occ = ctx.state.get("occ")
        if occ is not None:
            occ.persist()
        self._streams.pop(ctx.stream_id, None)

    def on_event(self, ctx, ev) -> list:
        if ev.type != "line_cross":
            return []
        d = map_direction(ev.fields["direction"], self.counting.in_side)
        if d not in self.counting.count_directions:
            return []  # 不发事件、不改 occupancy（基座已记方向，防重复）
        occ: Occupancy = ctx.state["occ"]
        occ.apply(d)
        ts = int(ev.wall_ms)
        self._last_event_ts = ts
        self.tx.publish(kind="crossing", ctx=ctx, body={
            "direction": d,
            "track_id": ev.track_id,
            "vehicle_class": vehicle_class(ev.fields["class_id"]),
            "confidence": float(ev.fields["score"]),
            "occupancy_after": occ.occupancy,
            "free_after": occ.free,
        }, ts=ts)
        # 占用快照由定时线程按 publish_interval_ms 节流发布
        return []  # 停车事件走 SpoolPublisher，不经基座 PublishWorker

    def on_frame(self, ctx, res) -> list:
        if res.seq % self.preview_frame_stride == 0:
            ctx.state["last"] = res  # 只保存引用，不做计算
            frames = ctx.state.get("frames")
            if frames is None:
                frames = ctx.state["frames"] = deque(maxlen=_PREVIEW_FRAMES)
            frames.append(res)       # /preview.json?seq= 按快照帧号回查框
        return []

    def health(self) -> dict:  # §S13 B2：基座放入 /healthz 的 app 子对象
        tx = self.tx
        return {
            "spool_depth": getattr(tx, "spool_depth", None),
            "spool_overflow": getattr(tx, "spool_overflow", None),
            "mqtt_app_connected": getattr(tx, "mqtt_app_connected", None),
            "last_event_ts": self._last_event_ts,
        }

    # ------------------------------------------------------------------ 定时发布

    def _tick_loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            try:
                self._tick()
            except Exception:  # 定时线程不能死；错误留日志
                log.exception("occupancy tick failed")

    def _tick(self) -> None:
        now_ms = int(time.time() * 1000)
        interval = self.counting.publish_interval_ms
        for ctx in list(self._streams.values()):
            occ: Occupancy = ctx.state["occ"]
            rolled = occ.maybe_rollover()
            if not occ.dirty:
                continue
            if not rolled and now_ms - ctx.state["last_occ_pub_ms"] < interval:
                continue
            self._publish_occupancy(ctx, occ.pending_reason or "crossing")

    def _publish_occupancy(self, ctx, reason: str) -> None:
        occ: Occupancy = ctx.state["occ"]
        body = occ.snapshot_body()
        body["reason"] = reason
        self.tx.publish(kind="occupancy", ctx=ctx, body=body,
                        ts=int(time.time() * 1000))
        ctx.state["last_occ_pub_ms"] = int(time.time() * 1000)
        occ.clear_dirty()
