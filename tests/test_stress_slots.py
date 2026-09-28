"""NRD-4 P6 压测工具测试（§8.1 P6 行：喂录制的 healthz 样本）。

样本在 `tests/fixtures/healthz/`（vb-healthz 形状，BASE-1 §6.8 字段），
不经真机：覆盖 §6.3 三条判据（fps 0.9×target、queue_delay_ms_p95 ≤1000、
RK 平台 RGA_BLIT fail == 0）与 JSONL 窗口聚合、dropped_frames 只记录。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from evaluation.stress_slots import (FPS_RATIO, QUEUE_DELAY_P95_MAX_MS,
                                     aggregate_samples, build_conclusion,
                                     count_rga_fails, judge, load_sample_file,
                                     main)

FIXTURES = Path(__file__).parent / "fixtures" / "healthz"


# ------------------------------------------------------------------ 聚合

def test_aggregate_single_sample():
    agg = aggregate_samples(load_sample_file(str(FIXTURES / "slots_n2_ok.json")))
    assert agg["n"] == 2
    assert agg["fps_min"] == pytest.approx(0.98)
    assert agg["queue_delay_ms_p95_max"] == 180
    assert agg["dropped_frames_delta"] == 0     # 单样本无增量


def test_aggregate_jsonl_window():
    agg = aggregate_samples(
        load_sample_file(str(FIXTURES / "slots_n2_fpsdrop.jsonl")))
    assert agg["n"] == 2
    assert agg["fps_min"] == pytest.approx(0.89)          # 窗口内取最小
    assert agg["queue_delay_ms_p95_max"] == 350           # 窗口内取最大
    assert agg["dropped_frames_delta"] == 7               # (14-10)+(12-9)


# ------------------------------------------------------------------ 判据

def _agg(fps, q):
    return {"n": 1, "streams": {}, "fps_min": fps,
            "queue_delay_ms_p95_max": q, "dropped_frames_delta": 0}


def test_judge_pass_and_fail():
    ok = judge(_agg(0.9, 1000), target_fps=1.0, platform="cpu",
               rga_fail_count=None)
    assert ok["pass"]                                     # 边界值取等号
    low_fps = judge(_agg(0.9 - 1e-6, 100), target_fps=1.0,
                    platform="cpu", rga_fail_count=None)
    assert not low_fps["pass"] and "fps" in low_fps["reasons"][0]
    high_q = judge(_agg(1.0, QUEUE_DELAY_P95_MAX_MS + 1), target_fps=1.0,
                   platform="cpu", rga_fail_count=None)
    assert not high_q["pass"] and "queue_delay" in high_q["reasons"][0]
    assert FPS_RATIO == 0.9 and QUEUE_DELAY_P95_MAX_MS == 1000.0  # §6.3 常数


def test_judge_rk_rga():
    base = dict(target_fps=1.0, platform="rk3588")
    assert judge(_agg(1.0, 100), rga_fail_count=0, **base)["pass"]
    bad = judge(_agg(1.0, 100), rga_fail_count=3, **base)
    assert not bad["pass"] and "RGA_BLIT" in bad["reasons"][0]
    missing = judge(_agg(1.0, 100), rga_fail_count=None, **base)
    assert not missing["pass"]                            # rk 缺日志无法判
    # 非 RK 平台不看 RGA
    assert judge(_agg(1.0, 100), target_fps=1.0, platform="jetson",
                 rga_fail_count=None)["pass"]


def test_count_rga_fails():
    assert count_rga_fails((FIXTURES / "rga_fail.log").read_text()) == 2
    assert count_rga_fails((FIXTURES / "rga_clean.log").read_text()) == 0


def test_conclusion_max_streams_ok():
    rows = [{"n": 2, "pass": True, "reasons": []},
            {"n": 4, "pass": False, "reasons": ["x"]}]
    c = build_conclusion(rows, platform="cpu", target_fps=1.0)
    assert c["schema"] == "parking.stress_slots/1"
    assert c["max_streams_ok"] == 2
    assert build_conclusion([], platform="cpu",
                            target_fps=1.0)["max_streams_ok"] is None


# ------------------------------------------------------------------ CLI

def test_main_samples_table_and_json(tmp_path, capsys):
    out = tmp_path / "conclusion.json"
    rc = main(["--platform", "cpu",
               "--sample", str(FIXTURES / "slots_n2_ok.json"),
               "--sample", str(FIXTURES / "slots_n4_queue.json"),
               "--out", str(out)])
    assert rc == 0
    text = capsys.readouterr().out
    assert "PASS" in text and "FAIL" in text              # N 行表
    conclusion = json.loads(out.read_text(encoding="utf-8"))
    assert [r["n"] for r in conclusion["rows"]] == [2, 4]  # 按 N 排序
    assert conclusion["rows"][1]["pass"] is False
    assert "1500" in conclusion["rows"][1]["reasons"][0]
    assert conclusion["max_streams_ok"] == 2


def test_main_jsonl_fpsdrop_fails():
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = main(["--platform", "cpu",
                   "--sample", str(FIXTURES / "slots_n2_fpsdrop.jsonl")])
    assert rc == 0
    out = buf.getvalue()
    conclusion = json.loads(out[out.index("{"):])
    assert conclusion["max_streams_ok"] is None
    assert conclusion["rows"][0]["dropped_frames_delta"] == 7  # 只记录


def test_main_rk_requires_container_log(capsys):
    rc = main(["--platform", "rk3588",
               "--sample", str(FIXTURES / "slots_n2_ok.json")])
    assert rc == 2
    assert "--container-log" in capsys.readouterr().err


def test_main_rk_rga_fail_row(tmp_path):
    out = tmp_path / "c.json"
    rc = main(["--platform", "rk3576",
               "--sample", str(FIXTURES / "slots_n2_ok.json"),
               "--container-log", str(FIXTURES / "rga_fail.log"),
               "--out", str(out)])
    assert rc == 0
    conclusion = json.loads(out.read_text(encoding="utf-8"))
    row = conclusion["rows"][0]
    assert row["rga_fail_count"] == 2
    assert row["pass"] is False
    assert conclusion["max_streams_ok"] is None


def test_main_log_sample_count_mismatch():
    rc = main(["--platform", "rk3588",
               "--sample", str(FIXTURES / "slots_n2_ok.json"),
               "--sample", str(FIXTURES / "slots_n4_queue.json"),
               "--container-log", str(FIXTURES / "rga_clean.log")])
    assert rc == 2
