"""事件信封、seq 分配与持久化、event_id（§S3 公共信封、§6.3 seq 规则）。

仅标准库。信封字段由 contracts/envelope.schema.json 定义，本模块负责生成
合规 dict（tests/test_envelope.py 用 contracts/validate_payload.py 校验）。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")

# §S3：occupancy / slots 快照 retain，其余事件不 retain
RETAINED_KINDS = frozenset({"occupancy", "slots"})

# §6.3：为减少写放大，每 20 个 seq 预留一次（boot 时从预留上限继续）
SEQ_RESERVE = 20


def topic_for(site_id: str, camera_id: str, kind: str) -> str:
    """事件 topic：``<site_id>/parking/<camera_id>/<kind>``（§S3）。"""
    return f"{site_id}/parking/{camera_id}/{kind}"


def _check_id(name: str, value: str) -> None:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise ValueError(f"{name} 必须匹配 ^[a-z0-9-]{{1,32}}$，得到 {value!r}")


def build_envelope(kind: str, *, site_id: str, camera_id: str, ts: int, seq: int,
                   body: dict, replayed: bool = False,
                   event_id: str | None = None) -> dict:
    """生成公共信封 + 事件 body（§S3）。

    ``kind`` ∈ crossing | occupancy | plate | plate_crop | gate | slots，
    schema 串为 ``parking.<kind>/1``。``ts`` 为 epoch ms（VBE1 wall_ms 取整）。
    """
    _check_id("site_id", site_id)
    _check_id("camera_id", camera_id)
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise ValueError(f"seq 必须为 >= 0 的整数，得到 {seq!r}")
    env = {
        "schema": f"parking.{kind}/1",
        "event_id": event_id or str(uuid.uuid4()),
        "site_id": site_id,
        "camera_id": camera_id,
        "ts": int(ts),
        "seq": seq,
        "replayed": bool(replayed),
    }
    env.update(body)
    return env


class SeqAllocator:
    """每 camera 单调递增、持久化、重启（含 kill -9）不回退的 seq 分配器（§S3、§6.3）。

    状态文件 ``<state_dir>/seq.json`` 存 ``{"reserved": n}``：已安全使用
    的上界。分配只在 ``next`` 触及预留上界时原子写一次（预留 SEQ_RESERVE 个），
    崩溃重启后从上界继续——允许跳号，绝不回退。
    """

    def __init__(self, state_dir, *, reserve: int = SEQ_RESERVE) -> None:
        self._dir = Path(state_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "seq.json"
        self._reserve = int(reserve)
        if self._reserve < 1:
            raise ValueError("reserve 必须 >= 1")
        self._next = 0
        self._reserved = 0  # next 可安全用到 reserved-1；达到 reserved 时需落盘
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._reserved = int(data["reserved"])
            except (ValueError, KeyError, OSError) as exc:
                raise RuntimeError(f"seq.json 损坏（拒绝回退，需人工处理）: {self._path}: {exc}")
            self._next = self._reserved  # boot 时从预留上限继续

    @property
    def path(self) -> Path:
        return self._path

    def next(self) -> int:
        """分配下一个 seq（单调递增；触预留界时原子落盘）。"""
        seq = self._next
        self._next += 1
        if self._next >= self._reserved:
            self._persist(self._next + self._reserve - 1)
        return seq

    def _persist(self, reserved: int) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"reserved": reserved}))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        self._reserved = reserved
