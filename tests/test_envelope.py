"""信封与 SeqAllocator 测试（§S3、§6.3；§8.1 C2）。

- 生成的 crossing / occupancy 信封必须通过 contracts/validate_payload.py；
- seq 单调递增、持久化，kill -9 后不回退（子进程验证）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import validate_payload
from core_parking.envelope import (RETAINED_KINDS, SeqAllocator, build_envelope,
                                   topic_for)

REPO = Path(__file__).resolve().parent.parent
CORE_PY = REPO / "core-py"

CROSSING_BODY = {
    "direction": "in", "track_id": 381, "vehicle_class": "car",
    "confidence": 0.82, "occupancy_after": 57, "free_after": 63,
}
OCCUPANCY_BODY = {
    "occupancy": 57, "capacity": 120, "free": 63,
    "in_today": 60, "out_today": 3, "reason": "crossing",
}


def test_crossing_envelope_passes_contract():
    env = build_envelope("crossing", site_id="demo-lot", camera_id="gate-a",
                         ts=1790000000123, seq=1042, body=CROSSING_BODY)
    assert validate_payload.validate("crossing", env) == []
    assert env["schema"] == "parking.crossing/1"
    assert env["replayed"] is False


def test_occupancy_envelope_passes_contract():
    env = build_envelope("occupancy", site_id="demo-lot", camera_id="gate-a",
                         ts=1790000000123, seq=7, body=OCCUPANCY_BODY)
    assert validate_payload.validate("occupancy", env) == []


def test_envelope_event_id_unique_and_replayed_flag():
    a = build_envelope("crossing", site_id="demo-lot", camera_id="gate-a",
                       ts=1, seq=0, body=CROSSING_BODY)
    b = build_envelope("crossing", site_id="demo-lot", camera_id="gate-a",
                       ts=1, seq=1, body=CROSSING_BODY, replayed=True)
    assert a["event_id"] != b["event_id"]
    assert b["replayed"] is True


def test_envelope_rejects_bad_ids_and_seq():
    with pytest.raises(ValueError):
        build_envelope("crossing", site_id="Demo_Lot", camera_id="gate-a",
                       ts=1, seq=0, body=CROSSING_BODY)
    with pytest.raises(ValueError):
        build_envelope("crossing", site_id="demo-lot", camera_id="gate a",
                       ts=1, seq=0, body=CROSSING_BODY)
    with pytest.raises(ValueError):
        build_envelope("crossing", site_id="demo-lot", camera_id="gate-a",
                       ts=1, seq=-1, body=CROSSING_BODY)


def test_topic_and_retain_rules():
    assert topic_for("demo-lot", "gate-a", "crossing") == "demo-lot/parking/gate-a/crossing"
    assert "occupancy" in RETAINED_KINDS and "slots" in RETAINED_KINDS
    assert "crossing" not in RETAINED_KINDS


def test_seq_monotonic_and_persisted(tmp_path):
    alloc = SeqAllocator(tmp_path / "gate-a")
    first = [alloc.next() for _ in range(3)]
    assert first == [0, 1, 2]
    # 重新打开（正常重启）：不回退
    alloc2 = SeqAllocator(tmp_path / "gate-a")
    assert alloc2.next() > first[-1]


def test_seq_write_amplification_reserve(tmp_path):
    d = tmp_path / "gate-a"
    alloc = SeqAllocator(d, reserve=5)
    alloc.next()  # 触发一次预留落盘
    data = json.loads((d / "seq.json").read_text())
    assert data["reserved"] >= 5
    mtime_reads = data["reserved"]
    for _ in range(3):  # 预留区间内不再改写上界
        alloc.next()
    assert json.loads((d / "seq.json").read_text())["reserved"] == mtime_reads


def test_seq_no_regression_after_kill9(tmp_path):
    """子进程分配若干 seq 后被 kill -9；重启后 seq 严格大于已分配值。"""
    code = (
        "from core_parking.envelope import SeqAllocator\n"
        f"a = SeqAllocator({str(tmp_path / 'cam')!r}, reserve=4)\n"
        "print(' '.join(str(a.next()) for _ in range(3)), flush=True)\n"
        "import time; time.sleep(30)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(CORE_PY))
    proc = subprocess.Popen([sys.executable, "-c", code], env=env,
                            stdout=subprocess.PIPE, text=True)
    line = proc.stdout.readline()
    proc.kill()  # kill -9：无任何清理路径
    proc.wait()
    assert proc.returncode != 0
    allocated = [int(x) for x in line.split()]
    assert len(allocated) == 3
    alloc = SeqAllocator(tmp_path / "cam", reserve=4)
    assert alloc.next() > max(allocated)
