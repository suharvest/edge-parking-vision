#!/usr/bin/env python3
"""NRD-4 P6 路数压测工具（规格 §6.3、§8.1 P6 行）。

读基座 ``/healthz``（vb-healthz，BASE-1 §6.8；本工具只读这一处，§5.1），
对每个候选路数 N 聚合压测窗口内的每路统计并按 §6.3 判定：

- 所有路实际 fps ≥ ``0.9 × target_fps``（取窗口内每路 fps 最小值）；
- 所有路 ``queue_delay_ms_p95`` ≤ 1000 ms（取窗口内最大值）；
- RK 平台（``--platform rk3588|rk3576``）另要求对应窗口的容器日志中
  ``grep -c "RGA_BLIT fail"`` == 0（``--container-log`` 必填，§6.3 第三行；
  依据 NRD-2 §S10：压力上升时先坏 RGA 而非 NPU）；
- ``dropped_frames`` 窗口增量只记录、不参与判定
  （§6.3：「dropped_frames 增速不影响 fps 判据」）。

输入两种用法：

- 现场（每个 N 跑一次，流数由操作员用 push_multi.sh + 预设调整）::

    uv run python evaluation/stress_slots.py --platform rk3588 --n 4 \
        --healthz http://<host>:8099/healthz --window-s 60 \
        --container-log /tmp/n4.log --out runs/n4.json

- 喂录制样本（CI / 单元测试；``--sample`` 可重复，与 ``--container-log``
  按顺序一一对应；每个 sample 是单个 healthz JSON 或 JSONL 多样本）::

    uv run python evaluation/stress_slots.py --platform cpu \
        --sample tests/fixtures/healthz/slots_n2_ok.json \
        --sample tests/fixtures/healthz/slots_n4_queue.json

输出：stdout 打印 N 行表 + 结论 JSON（``--out`` 同时写文件）。
退出码：0 = 正常出结论（含存在不通过的 N）；2 = 用法/输入错误。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

__all__ = ["aggregate_samples", "judge", "build_conclusion", "main",
           "FPS_RATIO", "QUEUE_DELAY_P95_MAX_MS", "RGA_NEEDLE", "RK_PLATFORMS"]

FPS_RATIO = 0.9                    # §6.3：fps ≥ 0.9 × target_fps
QUEUE_DELAY_P95_MAX_MS = 1000.0    # §6.3：queue_delay_ms_p95 ≤ 1000 ms
RGA_NEEDLE = "RGA_BLIT fail"       # §6.3 / NRD-2 §S10
RK_PLATFORMS = ("rk3588", "rk3576")


# ------------------------------------------------------------------ 采样读取

def _extract_samples(doc) -> list[dict]:
    """healthz JSON 或 JSONL 文本 → 样本列表（每个样本含 streams[]）。"""
    doc = doc.strip()
    if not doc:
        return []
    try:
        obj = json.loads(doc)
        return [obj] if isinstance(obj, dict) else list(obj)
    except ValueError:
        pass
    return [json.loads(line) for line in doc.splitlines() if line.strip()]


def load_sample_file(path: str) -> list[dict]:
    return _extract_samples(Path(path).read_text(encoding="utf-8"))


def poll_healthz(url: str, window_s: float, interval_s: float = 1.0) -> list[dict]:
    """现场模式：固定间隔轮询 /healthz，收集窗口内全部样本。"""
    samples = []
    deadline = time.monotonic() + window_s
    while True:
        with urllib.request.urlopen(url, timeout=5) as r:
            samples.append(json.loads(r.read().decode("utf-8")))
        if time.monotonic() >= deadline:
            return samples
        time.sleep(min(interval_s, max(0.0, deadline - time.monotonic())))


# ------------------------------------------------------------------ 聚合与判定

def aggregate_samples(samples: list[dict]) -> dict:
    """一个 N 的窗口样本 → 每路聚合 + 总体指标。

    返回 ``{"n": int, "streams": {sid: {...}}, "fps_min": float|None,
    "queue_delay_ms_p95_max": float|None, "dropped_frames_delta": int}``。
    缺字段的样本跳过该字段（getattr 式防御，基座字段见 BASE-1 §6.8）。
    """
    per: dict[str, dict] = {}
    for smp in samples:
        for st in smp.get("streams") or []:
            sid = str(st.get("stream_id") or st.get("id") or "?")
            agg = per.setdefault(sid, {"fps_min": None,
                                       "queue_delay_ms_p95_max": None,
                                       "dropped_first": None, "dropped_last": None})
            fps = st.get("fps")
            if fps is not None:
                fps = float(fps)
                agg["fps_min"] = fps if agg["fps_min"] is None \
                    else min(agg["fps_min"], fps)
            q = st.get("queue_delay_ms_p95")
            if q is not None:
                q = float(q)
                agg["queue_delay_ms_p95_max"] = q \
                    if agg["queue_delay_ms_p95_max"] is None \
                    else max(agg["queue_delay_ms_p95_max"], q)
            d = st.get("dropped_frames")
            if d is not None:
                d = int(d)
                if agg["dropped_first"] is None:
                    agg["dropped_first"] = d
                agg["dropped_last"] = d
    fps_vals = [a["fps_min"] for a in per.values() if a["fps_min"] is not None]
    q_vals = [a["queue_delay_ms_p95_max"] for a in per.values()
              if a["queue_delay_ms_p95_max"] is not None]
    dropped = sum(max(0, a["dropped_last"] - a["dropped_first"])
                  for a in per.values()
                  if a["dropped_first"] is not None and a["dropped_last"] is not None)
    return {
        "n": len(per),
        "streams": per,
        "fps_min": min(fps_vals) if fps_vals else None,
        "queue_delay_ms_p95_max": max(q_vals) if q_vals else None,
        "dropped_frames_delta": dropped,
    }


def count_rga_fails(log_text: str) -> int:
    """§6.3 第三行：``grep -c "RGA_BLIT fail"`` 等价。"""
    return sum(1 for line in log_text.splitlines() if RGA_NEEDLE in line)


def judge(agg: dict, *, target_fps: float, platform: str,
          rga_fail_count: int | None) -> dict:
    """§6.3 判据 → 行结果（pass + 可读原因）。"""
    reasons = []
    fps_line = FPS_RATIO * target_fps
    if agg["fps_min"] is None:
        reasons.append("healthz 无 fps 字段，无法判定")
    elif agg["fps_min"] < fps_line:
        reasons.append(f"fps_min {agg['fps_min']:.3f} < "
                       f"{FPS_RATIO}×target_fps={fps_line:.3f}")
    if agg["queue_delay_ms_p95_max"] is None:
        reasons.append("healthz 无 queue_delay_ms_p95 字段，无法判定")
    elif agg["queue_delay_ms_p95_max"] > QUEUE_DELAY_P95_MAX_MS:
        reasons.append(f"queue_delay_ms_p95_max "
                       f"{agg['queue_delay_ms_p95_max']:.0f} > "
                       f"{QUEUE_DELAY_P95_MAX_MS:.0f} ms")
    if platform in RK_PLATFORMS:
        if rga_fail_count is None:
            reasons.append("RK 平台缺 --container-log，无法判定 RGA_BLIT")
        elif rga_fail_count > 0:
            reasons.append(f"容器日志 {RGA_NEEDLE!r} × {rga_fail_count}")
    return {"pass": not reasons, "reasons": reasons}


def build_conclusion(rows: list[dict], *, platform: str,
                     target_fps: float) -> dict:
    """行列表 → 结论 JSON（§6.3：满足判据的最大 N）。"""
    passing = [r["n"] for r in rows if r["pass"]]
    return {
        "schema": "parking.stress_slots/1",
        "platform": platform,
        "target_fps": target_fps,
        "rows": rows,
        "max_streams_ok": max(passing) if passing else None,
        "criteria": {
            "fps_min_gte": f"{FPS_RATIO} * target_fps",
            "queue_delay_ms_p95_lte": QUEUE_DELAY_P95_MAX_MS,
            "rga_blit_fail_count_eq_0": platform in RK_PLATFORMS,
            "dropped_frames": "只记录，不参与判定（§6.3）",
        },
    }


# ------------------------------------------------------------------ CLI

def _print_table(rows: list[dict]) -> None:
    head = ("N", "streams", "fps_min", "qdelay_p95_max_ms", "dropped_delta",
            "rga_fail", "pass", "reasons")
    print("%-4s %-7s %-8s %-17s %-13s %-8s %-5s %s" % head)
    for r in rows:
        print("%-4d %-7d %-8s %-17s %-13d %-8s %-5s %s" % (
            r["n"], r["streams"],
            f"{r['fps_min']:.3f}" if r["fps_min"] is not None else "-",
            f"{r['queue_delay_ms_p95_max']:.0f}"
            if r["queue_delay_ms_p95_max"] is not None else "-",
            r["dropped_frames_delta"],
            "-" if r["rga_fail_count"] is None else r["rga_fail_count"],
            "PASS" if r["pass"] else "FAIL",
            "; ".join(r["reasons"])))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", required=True,
                    help="cpu|jetson|rk3588|rk3576|hailo|recamera-pro|…")
    ap.add_argument("--target-fps", type=float, default=1.0,
                    help="预设 app.options.target_fps（默认 1.0）")
    ap.add_argument("--sample", action="append", default=[],
                    help="录制的 healthz 样本文件（JSON 或 JSONL），可重复")
    ap.add_argument("--container-log", action="append", default=[],
                    help="对应窗口的容器日志（rk3588/rk3576 必填；与 --sample "
                         "按顺序一一对应；现场模式给 1 个）")
    ap.add_argument("--healthz", help="现场模式：基座 /healthz URL")
    ap.add_argument("--n", type=int, help="现场模式：本次路数（仅记录）")
    ap.add_argument("--window-s", type=float, default=60.0,
                    help="现场模式轮询窗口（默认 60 s）")
    ap.add_argument("--out", help="结论 JSON 输出路径（缺省只打 stdout）")
    args = ap.parse_args(argv)

    logs = args.container_log
    if args.platform in RK_PLATFORMS and not logs:
        print(f"stress_slots: --platform {args.platform} 需要 "
              f"--container-log <file>（§6.3 RGA 判据）", file=sys.stderr)
        return 2

    runs: list[tuple[list[dict], int | None]] = []   # (samples, rga_count)
    if args.healthz:
        samples = poll_healthz(args.healthz, args.window_s)
        if not samples:
            print("stress_slots: /healthz 窗口内无样本", file=sys.stderr)
            return 2
        rga = None
        if logs:
            rga = count_rga_fails(Path(logs[0]).read_text(
                encoding="utf-8", errors="replace"))
        runs.append((samples, rga))
    elif args.sample:
        if logs and len(logs) != len(args.sample):
            print("stress_slots: --container-log 数量须与 --sample 一致",
                  file=sys.stderr)
            return 2
        for i, path in enumerate(args.sample):
            samples = load_sample_file(path)
            if not samples:
                print(f"stress_slots: 样本为空: {path}", file=sys.stderr)
                return 2
            rga = None
            if logs:
                rga = count_rga_fails(Path(logs[i]).read_text(
                    encoding="utf-8", errors="replace"))
            runs.append((samples, rga))
    else:
        print("stress_slots: 需要 --healthz（现场）或 --sample（录制样本）",
              file=sys.stderr)
        return 2

    rows = []
    for samples, rga in runs:
        agg = aggregate_samples(samples)
        verdict = judge(agg, target_fps=args.target_fps,
                        platform=args.platform, rga_fail_count=rga)
        rows.append({"n": agg["n"], "streams": agg["n"],
                     "fps_min": agg["fps_min"],
                     "queue_delay_ms_p95_max": agg["queue_delay_ms_p95_max"],
                     "dropped_frames_delta": agg["dropped_frames_delta"],
                     "rga_fail_count": rga, **verdict})
    rows.sort(key=lambda r: r["n"])
    conclusion = build_conclusion(rows, platform=args.platform,
                                  target_fps=args.target_fps)

    _print_table(rows)
    text = json.dumps(conclusion, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
