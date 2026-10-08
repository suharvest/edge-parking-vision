"""`vb.config/1` 中 `app.options`（停车部分）的解析与运行时组装（§S2、§8.1 C3）。

两个职责：

1. **逐字段校验**（`parse_app_options` / `validate_counting`）：标准库实现，
   未知键报错、错误信息带字段路径，与 `contracts/app-options.schema.json`
   同义（CI 另用 jsonschema 双向校验）。`site_id` 等额外要求
   ``^[a-z0-9-]{1,32}$``（比基座严，因为它们进入 topic 与信封）。
   counting 分支（NRD-2）全量校验；plate / slots 分支的详细校验分别属于
   NRD-3 / NRD-4，这里只做公共键校验后原样放行。

2. **运行时组装**（`ParkingApp`）：基座按 ``app.module`` 实例化的钩子类。
   C1 交付的 ``ParkingHooks``（只读）已实现计数逻辑；按 §6.2 它还应
   「建 SpoolPublisher、ParkingHttp」——这些组装归 C3 本文件：
   ``ParkingApp(ParkingHooks)`` 在 ``configure`` 时注入 ``state_dir``、
   构造并启动 `SpoolPublisher`（含 ``events_out`` 落盘与最近事件环形缓冲，
   供 §S9 回放与 §S4 ``GET /events`` 使用）与 `http_app.ParkingHttp`
   （§S4 的 8080 端点）。预设的 ``app.module`` 因此指向
   ``core_parking.config:ParkingApp``。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from collections import deque
from pathlib import Path

from . import envelope as _envelope
from .hooks_counting import ParkingHooks

# C1/C2 接口缝合：hooks_counting 用 occupancy.SeqAllocator 建 seq 号，而
# transport.SpoolPublisher.publish 的 isinstance 哨兵只认 envelope.SeqAllocator
#（两者 seq.json 格式与鸭型接口等同，均为他人交付的只读文件）。这里在
# import 期把 hooks 模块引用的类换成 envelope 版，使真实发布路径可用。
import core_parking.hooks_counting as _hooks_module
if _hooks_module.SeqAllocator is not _envelope.SeqAllocator:
    _hooks_module.SeqAllocator = _envelope.SeqAllocator

log = logging.getLogger("core_parking.config")

__all__ = ["ConfigError", "parse_app_options", "validate_counting",
           "effective_options", "ParkingApp"]

_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")

# 与 contracts/app-options.schema.json 的 properties ∪ (oneOf 分支键) 一致。
# publish_hz / frame_stride 是基座 DEFAULTS["app"]["options"] 注入的默认键
# （vision_base.config._merge），不属于停车 schema，这里容忍并剔除。
_COMMON_KEYS = {"site_id", "mqtt", "http", "classes", "spool",
                "preview_frame_stride", "events_out", "state_dir"}
_BASE_INJECTED_KEYS = {"publish_hz", "frame_stride"}
_BRANCH_KEYS = {"counting", "plate", "target_fps", "unknown_after_s",
                "slot_coverage", "slots"}

_MQTT_KEYS = {"host", "port", "username", "password", "keepalive_s",
              "tls", "ca_file"}
_COUNTING_NUM_KEYS = ("band", "segment_margin", "min_travel",
                      "reborn_window_s", "reborn_dist")
_COUNTING_INT_KEYS = ("min_track_frames", "capacity", "initial_occupancy",
                      "publish_interval_ms")


class ConfigError(ValueError):
    """app.options 校验失败；message 带字段路径（§S2）。"""


def _err(path: str, msg: str) -> None:
    raise ConfigError(f"{path}: {msg}")


def _check_id(value, path: str) -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        _err(path, f"必须匹配 ^[a-z0-9-]{{1,32}}$，得到 {value!r}")
    return value


def _check_int(value, path: str, *, lo: int | None = None,
               hi: int | None = None, allow_bool: bool = False) -> int:
    if isinstance(value, bool) and not allow_bool or not isinstance(value, int):
        _err(path, f"必须为整数，得到 {value!r}")
    if lo is not None and value < lo:
        _err(path, f"必须 >= {lo}")
    if hi is not None and value > hi:
        _err(path, f"必须 <= {hi}")
    return value


def _check_num(value, path: str, *, lo=None, hi=None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _err(path, f"必须为数字，得到 {value!r}")
    if lo is not None and value < lo:
        _err(path, f"必须 >= {lo}")
    if hi is not None and value > hi:
        _err(path, f"必须 <= {hi}")
    return value


def _check_keys(d: dict, allowed: set, path: str) -> None:
    if not isinstance(d, dict):
        _err(path, "必须为对象")
    for k in d:
        if k not in allowed:
            _err(f"{path}.{k}", "未知键")


def validate_counting(d, *, path: str = "counting") -> dict:
    """校验 ``app.options.counting``（与 schema #/$defs/counting 同义）。

    返回深拷贝的规范化 dict（坐标转 float）；非法时抛 `ConfigError`。
    供本模块与 ``PUT /config/counting`` 共用。
    """
    if not isinstance(d, dict):
        _err(path, "必须为对象")
    required = ["line", "in_side", "count_directions", "band", "segment_margin",
                "min_track_frames", "min_travel", "reborn_window_s",
                "reborn_dist", "capacity", "initial_occupancy",
                "publish_interval_ms"]
    for k in required:
        if k not in d:
            _err(f"{path}.{k}", "required")
    _check_keys(d, set(required), path)

    out = json.loads(json.dumps(d))  # 深拷贝

    line = out["line"]
    if not isinstance(line, dict):
        _err(f"{path}.line", "必须为对象")
    for k in ("a", "b"):
        if k not in line:
            _err(f"{path}.line.{k}", "required")
    _check_keys(line, {"a", "b"}, f"{path}.line")
    for k in ("a", "b"):
        _check_point(line[k], f"{path}.line.{k}")

    if out["in_side"] not in ("positive", "negative"):
        _err(f"{path}.in_side",
             f"必须为 'positive' 或 'negative'，得到 {out['in_side']!r}")
    cds = out["count_directions"]
    if not isinstance(cds, list) or not cds \
            or any(c not in ("in", "out") for c in cds):
        _err(f"{path}.count_directions",
             f"必须为非空数组且元素 ∈ ['in','out']，得到 {cds!r}")
    for k in _COUNTING_NUM_KEYS:
        _check_num(out[k], f"{path}.{k}", lo=0)
    for k in _COUNTING_INT_KEYS:
        _check_int(out[k], f"{path}.{k}", lo=1 if k == "min_track_frames"
                   or k == "publish_interval_ms" else 0)
    return out


def _check_point(value, path: str) -> None:
    if (not isinstance(value, list) or len(value) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float))
                   or v < 0 or v > 1 for v in value)):
        _err(path, f"必须为 [x, y]（0–1 源归一化坐标），得到 {value!r}")


def parse_app_options(options, *, path: str = "app.options") -> dict:
    """校验 ``app.options``（counting 分支全量；plate/slots 分支放行）。

    返回通过校验的浅拷贝；非法时抛 `ConfigError`（信息带字段路径）。
    """
    if not isinstance(options, dict):
        _err(path, "必须为对象")
    _check_keys(options, _COMMON_KEYS | _BRANCH_KEYS | _BASE_INJECTED_KEYS, path)
    out = {k: v for k, v in options.items() if k not in _BASE_INJECTED_KEYS}

    if "site_id" not in out:
        _err(f"{path}.site_id", "required")
    _check_id(out["site_id"], f"{path}.site_id")

    # mqtt（必填）
    if "mqtt" not in out:
        _err(f"{path}.mqtt", "required")
    mqtt = out["mqtt"]
    _check_keys(mqtt, _MQTT_KEYS, f"{path}.mqtt")
    for k in ("host", "port", "username", "password", "keepalive_s"):
        if k not in mqtt:
            _err(f"{path}.mqtt.{k}", "required")
    if not isinstance(mqtt["host"], str) or not mqtt["host"]:
        _err(f"{path}.mqtt.host", "必须为非空字符串")
    _check_int(mqtt["port"], f"{path}.mqtt.port", lo=1, hi=65535)
    for k in ("username", "password"):
        if not isinstance(mqtt[k], str):
            _err(f"{path}.mqtt.{k}", "必须为字符串")
    _check_int(mqtt["keepalive_s"], f"{path}.mqtt.keepalive_s", lo=1)
    if "tls" in mqtt and not isinstance(mqtt["tls"], bool):
        _err(f"{path}.mqtt.tls", "必须为布尔值")
    if "ca_file" in mqtt and not isinstance(mqtt["ca_file"], str):
        _err(f"{path}.mqtt.ca_file", "必须为字符串")

    # http（必填）
    if "http" not in out:
        _err(f"{path}.http", "required")
    _check_keys(out["http"], {"port"}, f"{path}.http")
    if "port" not in out["http"]:
        _err(f"{path}.http.port", "required")
    _check_int(out["http"]["port"], f"{path}.http.port", lo=1, hi=65535)

    # spool（必填）
    if "spool" not in out:
        _err(f"{path}.spool", "required")
    _check_keys(out["spool"], {"max_events"}, f"{path}.spool")
    if "max_events" not in out["spool"]:
        _err(f"{path}.spool.max_events", "required")
    _check_int(out["spool"]["max_events"], f"{path}.spool.max_events", lo=1)

    if "classes" in out:
        cls = out["classes"]
        if (not isinstance(cls, list) or not cls
                or any(isinstance(c, bool) or not isinstance(c, int)
                       or c not in (2, 3, 5, 7) for c in cls)):
            _err(f"{path}.classes", "必须为非空数组且元素 ∈ [2,3,5,7]")
    if "preview_frame_stride" in out:
        _check_int(out["preview_frame_stride"], f"{path}.preview_frame_stride",
                   lo=1)
    if "events_out" in out and not isinstance(out["events_out"], str):
        _err(f"{path}.events_out", "必须为字符串（jsonl 路径）")
    if "state_dir" in out and not isinstance(out["state_dir"], str):
        _err(f"{path}.state_dir", "必须为字符串")

    # 分支（oneOf）：counting 全量校验；plate / slots 交给 NRD-3 / NRD-4
    if "counting" in out:
        for k in ("classes", "preview_frame_stride"):
            if k not in out:
                _err(f"{path}.{k}", "counting 分支 required")
        out["counting"] = validate_counting(out["counting"],
                                            path=f"{path}.counting")
    elif "plate" not in out and "slots" not in out and "target_fps" not in out:
        _err(path, "缺少分支：counting / plate / slots 三选一")
    return out


def effective_options(app_options: dict, state_dir: str) -> dict:
    """注入顶层 ``state_dir`` 到 app.options（§6.2 hooks 读 ``options["state_dir"]``）。

    基座只把 ``app.options`` 原样传给钩子（BASE-1 §6.5），顶层键不可见；
    ``ParkingApp`` 用本函数在 ``configure`` 前合并。
    """
    out = dict(app_options or {})
    out.setdefault("state_dir", state_dir)
    return out


class _TappingQueue(queue.Queue):
    """替换 SpoolPublisher 内部队列：入队前把事件写 events_out + 环形缓冲。

    §S9 回放（``app.options.events_out``）与 §S4 ``GET /events`` 共用此 taps。
    只重载 ``put_nowait``（钩子线程唯一的入队路径，transport.publish）。
    """

    def __init__(self, app: "ParkingApp") -> None:
        super().__init__()
        self._app = app

    def put_nowait(self, item):
        try:
            self._app._tap_event(item)
        except Exception:  # noqa: BLE001  # 落盘失败不能阻塞发布
            log.exception("events_out tap failed")
        super().put_nowait(item)


class ParkingApp(ParkingHooks):
    """``app.module = core_parking.config:ParkingApp``（§6.2 组装，§8.1 C3）。"""

    recent_limit = 500   # GET /events 的环形缓冲上限（limit 参数另截取）

    def __init__(self) -> None:
        super().__init__()
        self.recent_events: deque = deque(maxlen=self.recent_limit)
        self.events_out: str | None = None
        self._events_file = None
        self._events_mu = threading.Lock()
        self.http = None

    # ------------------------------------------------------------ 基座约定

    def configure(self, options: dict, device_id: str) -> None:
        opts = effective_options(parse_app_options(options), "state")
        _check_id(device_id, "device_id")
        self.events_out = opts.get("events_out") or None
        if self.events_out:
            Path(self.events_out).parent.mkdir(parents=True, exist_ok=True)
            self._events_file = open(self.events_out, "a", encoding="utf-8")
        super().configure(opts, device_id)   # 建 tx（_make_default_tx）、启定时线程
        from .http_app import ParkingHttp    # 惰性：tests 可只测 config
        self.http = ParkingHttp(self)
        self.http.start()
        log.info("ParkingApp configured: device=%s http=%s events_out=%s",
                 device_id, self.http.port, self.events_out)

    def close(self) -> None:
        if self.http is not None:
            self.http.stop()
            self.http = None
        super().close()
        tx = self.tx
        close = getattr(tx, "close", None)
        if callable(close):
            close()
        with self._events_mu:
            if self._events_file is not None:
                self._events_file.close()
                self._events_file = None

    # ------------------------------------------------------------ 组装

    def health(self) -> dict:
        """§S13 B2：优先用 SpoolPublisher.health()（真字段），无则退回钩子版。"""
        h = super().health()
        tx_health = getattr(self.tx, "health", None)
        if callable(tx_health):
            try:
                h = {**h, **tx_health()}
            except Exception:  # noqa: BLE001
                pass
        return h
    def _make_default_tx(self):
        from .transport import SpoolPublisher
        mqtt_opts = dict(self.options["mqtt"])
        spool_opts = self.options["spool"]
        state_dir = Path(self.options.get("state_dir", "state"))
        tx = SpoolPublisher(
            mqtt_opts,
            client_id=f"{self.options['site_id']}-{self.device_id}-app",
            site_id=self.options["site_id"],
            spool_path=state_dir / "spool.jsonl",
            max_events=int(spool_opts["max_events"]),
        )
        tx._q = _TappingQueue(self)   # events_out + /events tap（start 前替换）
        tx.start()
        return tx

    def _tap_event(self, item) -> None:
        """queue item = (topic, kind, env)；写一行 jsonl + 环形缓冲。"""
        _topic, _kind, env = item
        self.recent_events.append(env)
        with self._events_mu:
            if self._events_file is not None:
                self._events_file.write(
                    json.dumps(env, ensure_ascii=False) + "\n")
                self._events_file.flush()

    # ------------------------------------------------------------ HTTP 后端

    def set_counting(self, counting: dict) -> dict:
        """``PUT /config/counting`` 后端：校验 → 写 override → 即时生效（§S4）。

        只采纳编辑页可改的键（``line`` / ``in_side``，见
        `hooks_counting.COUNTING_EDITOR_KEYS`），叠加到部署配置上；body 里其余
        counting 键（编辑页 GET 后原样回传的 capacity 等）不生效也不落盘，未知键仍 400。
        override 文件只写 editor 键，所以重新部署改总车位数不会被旧 override 盖住。

        返回生效的 counting dict；校验失败抛 `ConfigError`（HTTP 层转 400），
        ``configure_analyzer`` 失败抛 `vision_base.hooks.ConfigureError`
        （HTTP 层转 503）。
        """
        from .hooks_counting import COUNTING_EDITOR_KEYS
        if not isinstance(counting, dict):
            _err("counting", "必须为对象")
        allowed = {"line", "in_side", "count_directions", "capacity",
                   *_COUNTING_NUM_KEYS, *_COUNTING_INT_KEYS}
        _check_keys(counting, allowed, "counting")
        edits = {k: counting[k] for k in COUNTING_EDITOR_KEYS if k in counting}
        if not edits:
            _err("counting", f"至少需要 {list(COUNTING_EDITOR_KEYS)} 之一")
        current = dict(self.options.get("counting") or self.deploy_counting)
        base = {**self.deploy_counting,
                **{k: current[k] for k in COUNTING_EDITOR_KEYS if k in current}}
        new = validate_counting({**base, **edits})
        ignored = sorted(k for k in counting
                         if k not in COUNTING_EDITOR_KEYS and counting[k] != base.get(k))
        if ignored:
            log.info("PUT /config/counting: ignoring non-editor keys %s "
                     "(deploy config is authoritative)", ignored)
        override_path = Path(self.options.get("state_dir", "state")) \
            / "app-options.override.json"
        override_path.parent.mkdir(parents=True, exist_ok=True)
        merged: dict = {}
        if override_path.exists():
            try:
                merged = json.loads(override_path.read_text(encoding="utf-8"))
            except ValueError:
                merged = {}
            if not isinstance(merged, dict):
                merged = {}
        merged["counting"] = {k: new[k] for k in COUNTING_EDITOR_KEYS}
        tmp = str(override_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, override_path)
        self.options["counting"] = new

        from .hooks_counting import CountingOptions, line_cross_config
        self.counting = CountingOptions.from_dict(new)
        cfg = line_cross_config(self.counting, self.classes)
        for ctx in list(self._streams.values()):
            ctx.configure_analyzer("line_cross", cfg)
        return new
