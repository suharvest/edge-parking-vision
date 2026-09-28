"""本地 jsonl 事件队列 + 游标（§6.4 断网补发的持久化部分）。

仅标准库。崩溃安全规则：
- append：追加一行 jsonl 并 fsync；
- ack：游标原子写 ``<spool>.cursor``（tmp → fsync → os.replace）；
- 淘汰/压实：整文件原子重写（tmp → fsync → os.replace）。

溢出规则（§6.4）：spool 满（待发送条数 >= max_events）时丢**最旧的 occupancy
快照**；crossing / plate / slots 事件永不丢——无 occupancy 可丢时仍追加，
写日志并置 ``overflow``（health() 上报 spool_overflow）。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("core_parking.spool")

DROPPABLE_KINDS = frozenset({"occupancy"})


@dataclass
class SpoolEntry:
    id: int
    topic: str
    kind: str
    payload: dict


class Spool:
    def __init__(self, path, max_events: int) -> None:
        if max_events < 1:
            raise ValueError("max_events 必须 >= 1")
        self._path = Path(path)
        self._cursor_path = Path(str(self._path) + ".cursor")
        self._max = int(max_events)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._entries: list[SpoolEntry] = []  # 文件顺序
        self._acked = 0                       # 已确认（游标）的最大 id
        self._next_id = 1
        self.overflow = False
        self._load()

    # ------------------------------------------------------------- 持久化加载

    def _load(self) -> None:
        if self._cursor_path.exists():
            try:
                data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
                self._acked = int(data["acked"])
            except (ValueError, KeyError, OSError) as exc:
                raise RuntimeError(f"spool 游标损坏（拒绝回退，需人工处理）: {self._cursor_path}: {exc}")
        if self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    self._entries.append(SpoolEntry(
                        id=int(rec["id"]), topic=rec["topic"],
                        kind=rec["kind"], payload=rec["payload"]))
        if self._entries:
            self._next_id = max(e.id for e in self._entries) + 1
        # 游标之后的内容才是待发送；文件里残留的已确认行下次压实时清掉
        if any(e.id <= self._acked for e in self._entries):
            self._rewrite()

    def _rewrite(self) -> None:
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            for e in self._entries:
                if e.id <= self._acked:
                    continue
                f.write(json.dumps({"id": e.id, "topic": e.topic, "kind": e.kind,
                                    "payload": e.payload}, ensure_ascii=False))
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._path)
        self._entries = [e for e in self._entries if e.id > self._acked]

    def _write_cursor(self) -> None:
        tmp = self._cursor_path.with_suffix(".cursor.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps({"acked": self._acked}))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self._cursor_path)

    # ------------------------------------------------------------------- API

    @property
    def depth(self) -> int:
        return len(self._entries)

    def append(self, topic: str, kind: str, payload: dict) -> int:
        """追加一条待发送事件，返回 spool id。满时按 §6.4 淘汰最旧 occupancy。"""
        if self.depth >= self._max:
            self._evict_oldest_droppable()
            if self.depth >= self._max:
                # 没有可丢的 occupancy：crossing/plate/slots 永不丢，仍追加并上报
                self.overflow = True
                log.error("spool 超过上限 max_events=%d 且无可丢 occupancy；"
                          "事件仍保留并标 spool_overflow", self._max)
        entry = SpoolEntry(id=self._next_id, topic=topic, kind=kind, payload=payload)
        self._next_id += 1
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"id": entry.id, "topic": entry.topic, "kind": entry.kind,
                                "payload": entry.payload}, ensure_ascii=False))
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        self._entries.append(entry)
        return entry.id

    def _evict_oldest_droppable(self) -> bool:
        for i, e in enumerate(self._entries):
            if e.kind in DROPPABLE_KINDS:
                del self._entries[i]
                self.overflow = True
                log.warning("spool 满：丢弃最旧 occupancy 快照 id=%d topic=%s", e.id, e.topic)
                self._rewrite()
                return True
        return False

    def pending(self) -> list[SpoolEntry]:
        """按入队顺序返回未确认（id > 游标）的条目。"""
        return [e for e in self._entries if e.id > self._acked]

    def ack(self, entry_id: int) -> None:
        """确认已送达：推进游标（原子写），并压实文件。"""
        if entry_id <= self._acked:
            return
        self._acked = entry_id
        self._write_cursor()
        self._rewrite()
