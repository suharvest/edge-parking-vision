"""场内车辆数与 seq 分配器（规格 §6.3，事件级，标准库 + vision_base.atomicio）。

持久化布局（每路一个目录 ``state_dir/<stream_id>/``）：

- ``occupancy.json``：``{occupancy, in_today, out_today, day, last_manual_set}``，
  原子写（tmp → fsync → os.replace，复用基座 ``vision_base.atomicio``）。
- ``seq.json``：``{"reserved": R}``，seq 分配器的高水位预留（见 SeqAllocator）。

时区：`in_today/out_today` 按设备本地时区 00:00 归零（`day` 为本地日期），
`occupancy` 跨日保留；总量重置只能走显式 `manual_set`（记录操作者与原因）。
"""
from __future__ import annotations

import datetime
import json
import time
from pathlib import Path

from vision_base.atomicio import write_json_atomic

__all__ = ["Occupancy", "SeqAllocator"]

#: occupancy 硬上限 = capacity * HARD_MAX_FACTOR（规格 §6.3 clamp 上界）
HARD_MAX_FACTOR = 2


def _local_today() -> str:
    return datetime.date.today().isoformat()


class Occupancy:
    """一路流的场内车辆数（可变状态 + 原子持久化）。

    参数 ``today_fn`` 仅供测试注入（跨日用例）；生产用默认本地日期。
    """

    def __init__(self, state_dir: str | Path, capacity: int, occupancy: int = 0,
                 in_today: int = 0, out_today: int = 0, day: str | None = None,
                 last_manual_set: dict | None = None, *, today_fn=_local_today):
        self.state_dir = Path(state_dir)
        self.capacity = int(capacity)
        self.occupancy = int(occupancy)
        self.in_today = int(in_today)
        self.out_today = int(out_today)
        self.day = day if day is not None else today_fn()
        self.last_manual_set = last_manual_set
        self._today_fn = today_fn
        self.dirty = False
        self.pending_reason: str | None = None

    # ------------------------------------------------------------------ boot

    @classmethod
    def boot(cls, state_dir: str | Path, capacity: int, initial_occupancy: int = 0,
             *, today_fn=_local_today) -> "Occupancy":
        """读 ``occupancy.json`` 恢复；不存在或损坏则取 ``initial_occupancy``。

        capacity 以当前配置为准（不持久化）；`day` 与持久化值不同则按跨日处理
        （清零 in_today/out_today，保留 occupancy）。
        """
        path = Path(state_dir) / "occupancy.json"
        occ = cls(state_dir, capacity, initial_occupancy, today_fn=today_fn)
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            occ.occupancy = int(data["occupancy"])
            occ.in_today = int(data.get("in_today", 0))
            occ.out_today = int(data.get("out_today", 0))
            occ.day = str(data.get("day") or occ.day)
            occ.last_manual_set = data.get("last_manual_set")
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError):
            # 损坏文件不阻塞启动，按 initial_occupancy 重新计数
            occ.occupancy = int(initial_occupancy)
        occ.occupancy = occ._clamp(occ.occupancy)
        occ.maybe_rollover()
        occ.persist()
        return occ

    # ------------------------------------------------------------------ 更新

    def _clamp(self, value: int) -> int:
        return max(0, min(int(value), self.capacity * HARD_MAX_FACTOR))

    def apply(self, direction: str) -> None:
        """一次越线：in +1 / out -1（clamp 到 [0, capacity*2]），同步累计今日计数。"""
        if direction not in ("in", "out"):
            raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
        self.maybe_rollover()
        self.occupancy = self._clamp(self.occupancy + (1 if direction == "in" else -1))
        if direction == "in":
            self.in_today += 1
        else:
            self.out_today += 1
        self.mark_dirty("crossing")
        self.persist()

    def manual_set(self, value: int, operator: str, reason: str) -> None:
        """总量重置的唯一入口（规格 §8.1 C1 行）；记录操作者、原因与时刻。"""
        if not operator:
            raise ValueError("manual_set requires an operator")
        if not reason:
            raise ValueError("manual_set requires a reason")
        self.maybe_rollover()
        self.occupancy = self._clamp(value)
        self.last_manual_set = {"operator": str(operator), "reason": str(reason),
                                "ts": int(time.time() * 1000)}
        self.mark_dirty("manual_set")
        self.persist()

    def maybe_rollover(self) -> bool:
        """本地日期变化时清零 in_today/out_today（occupancy 保留）。返回是否发生。"""
        today = self._today_fn()
        if today == self.day:
            return False
        self.day = today
        self.in_today = 0
        self.out_today = 0
        self.mark_dirty("daily_rollover")
        self.persist()
        return True

    # ------------------------------------------------------------------ 快照

    @property
    def free(self) -> int:
        return max(self.capacity - self.occupancy, 0)

    def snapshot_body(self) -> dict:
        """parking.occupancy/1 的 payload 部分（不含信封）。"""
        return {"occupancy": self.occupancy, "capacity": self.capacity,
                "free": self.free, "in_today": self.in_today, "out_today": self.out_today,
                "reason": self.pending_reason or "crossing"}

    def mark_dirty(self, reason: str = "crossing") -> None:
        self.dirty = True
        self.pending_reason = reason

    def clear_dirty(self) -> None:
        self.dirty = False
        self.pending_reason = None

    # ------------------------------------------------------------------ 持久化

    def persist(self) -> None:
        write_json_atomic(str(self.state_dir / "occupancy.json"), {
            "occupancy": self.occupancy,
            "in_today": self.in_today,
            "out_today": self.out_today,
            "day": self.day,
            "last_manual_set": self.last_manual_set,
        })


class SeqAllocator:
    """每 camera 单调递增 seq（规格 §S3 信封、§6.3）。

    为减少写放大，每 ``RESERVE`` 个 seq 预留一次：分配触达预留上限时先把
    ``reserved += RESERVE`` 原子落盘再发号；boot 从预留上限继续。进程被杀
    （kill -9）后重启 seq 可能向前跳，但绝不回退。
    """

    RESERVE = 20

    def __init__(self, state_dir: str | Path):
        self.state_dir = Path(state_dir)
        self._path = self.state_dir / "seq.json"
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                self._reserved = int(json.load(f)["reserved"])
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError):
            self._reserved = 0
        self._next = self._reserved

    def next(self) -> int:
        if self._next >= self._reserved:
            self._reserved += self.RESERVE
            write_json_atomic(str(self._path), {"reserved": self._reserved})
        value = self._next
        self._next += 1
        return value

    @property
    def peek(self) -> int:
        """下一个将分配的 seq（不消耗）。"""
        return self._next
