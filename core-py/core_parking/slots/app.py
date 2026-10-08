"""NRD-4 车位应用组装（规格 §5.1/§5.3、§6.2 `PUT /config/slots` 段）。

`SlotsApp(SlotHooks)` 是 slots 预设的 ``app.module``
（``core_parking.slots.app:SlotsApp``）：在事件级钩子之上补 §S4 的 8080
HTTP 端点后端——``GET /slots/editor``、``GET /snapshot/{stream}.jpg`` 由
`http_app.ParkingHttp` 直接提供；``GET/PUT /config/slots`` 调本类的
``get_slots_config`` / ``set_slots``。

``set_slots`` 流程（§6.2）：校验（stream_id 存在、多边形凸、id 唯一、
顶点为 [0,1] 归一化坐标）→ 原子写 ``state_dir/slots.override.json``
（全量生效表，未提及的路保持现状）→ 逐路 ``update_slots``（内部走
``ctx.configure_analyzer("slot_coverage", …)``，B3，并发布一次全量）。
校验失败抛 ``ValueError``（HTTP 层转 400，消息可读）；``configure_analyzer``
失败抛原异常（HTTP 层转 503）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .hooks_slots import SlotHooks, is_convex

log = logging.getLogger(__name__)

__all__ = ["SlotsApp", "validate_slots_map"]

_OVERRIDE_NAME = "slots.override.json"   # §6.2：state_dir/slots.override.json


def _validate_point(p, where: str) -> list:
    if (not isinstance(p, (list, tuple)) or len(p) != 2
            or isinstance(p[0], bool) or isinstance(p[1], bool)
            or not isinstance(p[0], (int, float))
            or not isinstance(p[1], (int, float))):
        raise ValueError(f"{where}: 顶点必须为 [x, y] 数值对，得到 {p!r}")
    x, y = float(p[0]), float(p[1])
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise ValueError(f"{where}: 顶点坐标必须在 [0,1] 归一化范围内，得到 {p!r}")
    return [x, y]


def validate_slots_map(body, known_streams) -> dict:
    """校验 ``PUT /config/slots`` body，返回规范化后的 ``{stream_id: [slot]}``。

    body 形如 ``{"cam-b1-01": [{"id": "B1-023", "polygon": [[x, y], ...]}, ...]}``
    （与 ``GET /config/slots`` 的返回同构，编辑页整表回写）。校验规则（§6.2）：
    ``stream_id`` 必须存在、每个多边形 ≥3 顶点且凸、同一路内 id 唯一、
    顶点为 [0,1] 归一化坐标。任一不满足抛 ``ValueError``（HTTP 400）。
    """
    if not isinstance(body, dict) or not body:
        raise ValueError("body 必须为 {stream_id: [slot, ...]} 的非空对象")
    out: dict = {}
    for stream_id, lst in body.items():
        if stream_id not in known_streams:
            raise ValueError(
                f"unknown stream {stream_id!r}（已知：{sorted(known_streams)}）")
        if not isinstance(lst, list):
            raise ValueError(f"slots[{stream_id!r}] 必须为数组")
        seen: set = set()
        slots = []
        for i, s in enumerate(lst):
            where = f"slots[{stream_id!r}][{i}]"
            if not isinstance(s, dict):
                raise ValueError(f"{where}: 车位必须为对象")
            slot_id = s.get("id")
            if not isinstance(slot_id, str) or not slot_id:
                raise ValueError(f"{where}: 'id' 必须为非空字符串")
            if slot_id in seen:
                raise ValueError(f"slots[{stream_id!r}]: 车位 id 重复 {slot_id!r}")
            seen.add(slot_id)
            poly = s.get("polygon")
            if not isinstance(poly, (list, tuple)) or len(poly) < 3:
                raise ValueError(f"{where} ({slot_id}): polygon 必须 ≥3 个顶点")
            poly = [_validate_point(p, f"{where} ({slot_id})") for p in poly]
            if not is_convex(poly):
                raise ValueError(f"{where} ({slot_id}): polygon 非凸（或退化）")
            slots.append({"id": slot_id, "polygon": poly})
        out[stream_id] = slots
    return out


class SlotsApp(SlotHooks):
    """slots 预设的应用类：``SlotHooks`` + HTTP 后端（§5.1 http_app 行）。"""

    def __init__(self) -> None:
        super().__init__()
        self.http = None

    # ------------------------------------------------------------ 基座约定

    def configure(self, options: dict, device_id: str) -> None:
        opts = dict(options or {})
        opts.setdefault("state_dir", "state")
        override_path = Path(opts["state_dir"]) / _OVERRIDE_NAME
        if override_path.is_file():
            with override_path.open(encoding="utf-8") as f:
                override = json.load(f)
            if not isinstance(override, dict):
                raise ValueError(f"{override_path}: slots override must be an object")
            merged = dict(opts.get("slots") or {})
            merged.update(override)
            opts["slots"] = merged
        super().configure(opts, device_id)
        from ..http_app import ParkingHttp    # 惰性：tests 可只测本类
        self.http = ParkingHttp(self)
        self.http.start()
        log.info("SlotsApp configured: device=%s http=%s",
                 device_id, self.http.port)

    def close(self) -> None:
        if self.http is not None:
            self.http.stop()
            self.http = None
        super().close()

    # ------------------------------------------------------------ HTTP 后端

    def get_slots_config(self) -> dict:
        """``GET /config/slots``：当前生效的车位表（与 PUT body 同构）。"""
        return {sid: [{"id": s["id"],
                       "polygon": [list(p) for p in s.get("polygon", [])]}
                      for s in lst]
                for sid, lst in self.slots_opt.slots.items()}

    def set_slots(self, body) -> dict:
        """``PUT /config/slots``（§6.2）：校验 → 原子写 override → 逐路重配。

        body 只含要改的路；未提及的路保持现状。返回生效后的全量车位表。
        """
        new = validate_slots_map(body, set(self._streams))
        effective = self.get_slots_config()
        effective.update(new)

        state_dir = Path(self.options.get("state_dir", "state"))
        state_dir.mkdir(parents=True, exist_ok=True)
        override_path = state_dir / _OVERRIDE_NAME
        tmp = str(override_path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:      # 原子写（tmp + rename）
            json.dump(effective, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, override_path)

        for stream_id, lst in new.items():               # B3 重配 + 发布
            self.update_slots(stream_id, lst)
        return self.get_slots_config()
