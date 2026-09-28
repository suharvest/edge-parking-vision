"""Occupancy 与 SeqAllocator 单测（规格 §6.3、§8.1 C1 行验收点）。"""
from __future__ import annotations

import datetime
import json
import os
import signal
import subprocess
import sys

import pytest

from core_parking.occupancy import Occupancy, SeqAllocator


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / "gate-a"


# ------------------------------------------------------------------ apply / clamp

def test_apply_in_out_and_today_counters(state_dir):
    occ = Occupancy.boot(state_dir, capacity=10, initial_occupancy=5)
    occ.apply("in")
    assert (occ.occupancy, occ.in_today, occ.out_today) == (6, 1, 0)
    occ.apply("out")
    occ.apply("out")
    assert (occ.occupancy, occ.in_today, occ.out_today) == (4, 1, 2)


def test_apply_clamps_at_zero_and_capacity_hard_max(state_dir):
    occ = Occupancy.boot(state_dir, capacity=2, initial_occupancy=0)
    occ.apply("out")
    assert occ.occupancy == 0  # 下界 0
    for _ in range(6):
        occ.apply("in")
    assert occ.occupancy == 4  # 上界 capacity * 2
    assert occ.free == 0       # free = max(capacity - occupancy, 0)


def test_apply_rejects_bad_direction(state_dir):
    occ = Occupancy.boot(state_dir, capacity=1)
    with pytest.raises(ValueError):
        occ.apply("sideways")


# ------------------------------------------------------------------ 持久化

def test_boot_roundtrip_persisted_state(state_dir):
    occ = Occupancy.boot(state_dir, capacity=10, initial_occupancy=0)
    occ.apply("in")
    occ.apply("in")
    occ.apply("out")
    again = Occupancy.boot(state_dir, capacity=10, initial_occupancy=0)
    assert (again.occupancy, again.in_today, again.out_today) == (1, 2, 1)
    data = json.loads((state_dir / "occupancy.json").read_text())
    assert data["occupancy"] == 1 and data["day"] == datetime.date.today().isoformat()


def test_boot_missing_file_uses_initial_occupancy(state_dir):
    occ = Occupancy.boot(state_dir, capacity=10, initial_occupancy=7)
    assert occ.occupancy == 7


def test_boot_corrupt_file_falls_back_to_initial(state_dir):
    state_dir.mkdir(parents=True)
    (state_dir / "occupancy.json").write_text("{not json")
    occ = Occupancy.boot(state_dir, capacity=10, initial_occupancy=3)
    assert occ.occupancy == 3


def test_persist_is_atomic_no_tmp_left(state_dir):
    occ = Occupancy.boot(state_dir, capacity=10)
    occ.apply("in")
    names = os.listdir(state_dir)
    assert "occupancy.json" in names
    assert not any(n.endswith(".tmp") for n in names)


# ------------------------------------------------------------------ 跨日

def test_daily_rollover_clears_today_counters_keeps_occupancy(state_dir):
    occ = Occupancy.boot(state_dir, capacity=10, initial_occupancy=5,
                         today_fn=lambda: "2026-01-01")
    occ.apply("in")
    occ.apply("out")
    assert occ.maybe_rollover() is False
    occ._today_fn = lambda: "2026-01-02"
    assert occ.maybe_rollover() is True
    assert (occ.in_today, occ.out_today) == (0, 0)
    assert occ.occupancy == 5            # occupancy 跨日保留
    assert occ.day == "2026-01-02"
    assert occ.dirty and occ.pending_reason == "daily_rollover"
    # 重启后（boot 时日期已变）同样清零
    again = Occupancy.boot(state_dir, capacity=10, today_fn=lambda: "2026-01-03")
    assert (again.in_today, again.out_today, again.occupancy) == (0, 0, 5)
    assert again.day == "2026-01-03"


# ------------------------------------------------------------------ manual_set

def test_manual_set_records_operator_and_reason(state_dir):
    occ = Occupancy.boot(state_dir, capacity=100, initial_occupancy=10)
    occ.manual_set(42, operator="op-zhang", reason="open-day audit")
    assert occ.occupancy == 42
    assert occ.pending_reason == "manual_set"
    rec = json.loads((state_dir / "occupancy.json").read_text())["last_manual_set"]
    assert rec["operator"] == "op-zhang" and rec["reason"] == "open-day audit"
    assert isinstance(rec["ts"], int)
    again = Occupancy.boot(state_dir, capacity=100)
    assert again.last_manual_set["operator"] == "op-zhang"


def test_manual_set_requires_operator_and_reason(state_dir):
    occ = Occupancy.boot(state_dir, capacity=10)
    with pytest.raises(ValueError):
        occ.manual_set(5, operator="", reason="x")
    with pytest.raises(ValueError):
        occ.manual_set(5, operator="op", reason="")


def test_manual_set_clamps_to_hard_max(state_dir):
    occ = Occupancy.boot(state_dir, capacity=10)
    occ.manual_set(999, operator="op", reason="r")
    assert occ.occupancy == 20


# ------------------------------------------------------------------ SeqAllocator

def test_seq_starts_at_zero_and_increments(state_dir):
    seq = SeqAllocator(state_dir)
    assert [seq.next(), seq.next(), seq.next()] == [0, 1, 2]


def test_seq_boot_continues_from_reserved_high_water(state_dir):
    seq = SeqAllocator(state_dir)
    seq.next()  # 首次分配落盘 reserved=20
    assert json.loads((state_dir / "seq.json").read_text())["reserved"] == SeqAllocator.RESERVE
    again = SeqAllocator(state_dir)
    assert again.peek == SeqAllocator.RESERVE  # boot 从预留上限继续


def test_seq_never_rolls_back_after_kill9(tmp_path):
    """kill -9 子进程后 seq 不回退（§8.1 C1 验收点）。"""
    d = tmp_path / "cam-1"
    d.mkdir()
    script = (
        "import sys, time; sys.path.insert(0, 'core-py');"
        "from core_parking.occupancy import SeqAllocator;"
        f"s = SeqAllocator({str(d)!r});"
        "print(s.next(), s.next(), s.next(), flush=True);"
        "time.sleep(60)"
    )
    proc = subprocess.Popen([sys.executable, "-c", script],
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "0 1 2"
    proc.send_signal(signal.SIGKILL)
    proc.wait()
    assert proc.returncode == -signal.SIGKILL
    again = SeqAllocator(d)
    assert again.next() >= 3  # 不回退（实际跳到预留上限 20）


def test_seq_allocations_do_not_write_every_time(state_dir):
    seq = SeqAllocator(state_dir)
    seq.next()  # 写一次（reserved 0→20）
    mtime = os.stat(state_dir / "seq.json").st_mtime_ns
    for _ in range(5):
        seq.next()
    assert os.stat(state_dir / "seq.json").st_mtime_ns == mtime  # 预留内不再落盘
