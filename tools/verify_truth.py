#!/usr/bin/env python3
"""真值回放校验（规格 §S9「真值回放」行、§8.1 C4）。

`tools/rtsp-fixture/make_truth.py` 合成的视频带一张轨迹表 `truth.json`，
记录每次越线的时刻与方向。本工具校验设备产出的事件流与之吻合：
  - 每条真值越线恰好匹配 1 条事件：时刻误差 ≤ ±window（缺省 0.5 s）、方向正确；
  - 多余事件 0 条（压线停留段被计 2 次会在这里暴露）；
  - 每条事件也过 contracts/validate_payload.py 的 crossing 校验。

用法：
  uv run python tools/verify_truth.py --truth tools/rtsp-fixture/truth.json --got out.jsonl \
      [--window-ms 500]

退出码：0 通过；1 不通过 / 用法或文件错误。
仅标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
import validate_payload  # noqa: E402


def _load_truth(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        doc = json.load(f)
    crossings = doc.get("crossings")
    if not isinstance(crossings, list) or not crossings:
        raise ValueError(f"{path}: 缺少非空 crossings 数组")
    out = []
    for i, c in enumerate(crossings):
        if not isinstance(c.get("t_ms"), int) or c.get("direction") not in ("in", "out"):
            raise ValueError(f"{path}: crossings[{i}] 需要 {{t_ms:int, direction:in|out}}")
        out.append(c)
    return sorted(out, key=lambda c: c["t_ms"])


def _load_got(path: str, problems: list[str]) -> list[dict]:
    events: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError as e:
                problems.append(f"got:{lineno}: JSON 解析失败: {e}")
                continue
            for err in validate_payload.validate("crossing", ev):
                problems.append(f"got:{lineno}: 校验失败: {err}")
            events.append(ev)
    return sorted(events, key=lambda e: e.get("ts", 0))


def verify(truth_path: str, got_path: str, window_ms: int) -> tuple[bool, dict]:
    problems: list[str] = []
    try:
        truth = _load_truth(truth_path)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        return False, {"passed": False, "problems": [str(e)]}
    try:
        got = _load_got(got_path, problems)
    except OSError as e:
        return False, {"passed": False, "problems": [str(e)]}

    matched_got: set[int] = set()
    matches = []
    for c in truth:
        best = None
        best_dt = window_ms + 1
        for i, ev in enumerate(got):
            if i in matched_got:
                continue
            if ev.get("direction") != c["direction"]:
                continue
            dt = abs(int(ev.get("ts", 0)) - c["t_ms"])
            if dt <= window_ms and dt < best_dt:
                best, best_dt = i, dt
        if best is None:
            problems.append(
                f"真值越线 t={c['t_ms']}ms direction={c['direction']} 无匹配事件"
                f"（窗口 ±{window_ms}ms）"
            )
        else:
            matched_got.add(best)
            matches.append({"truth_t_ms": c["t_ms"], "event_ts": got[best].get("ts"),
                            "direction": c["direction"], "dt_ms": best_dt})

    extra = [got[i] for i in range(len(got)) if i not in matched_got]
    for ev in extra:
        problems.append(
            f"多余事件 ts={ev.get('ts')} direction={ev.get('direction')} event_id={ev.get('event_id')}"
        )

    passed = not problems
    return passed, {
        "passed": passed,
        "truth_crossings": len(truth),
        "got_events": len(got),
        "matched": len(matches),
        "extra_events": len(extra),
        "window_ms": window_ms,
        "matches": matches,
        "problems": problems,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--truth", required=True, help="make_truth.py 生成的 truth.json")
    ap.add_argument("--got", required=True, help="设备产出的 crossing 事件 JSONL")
    ap.add_argument("--window-ms", type=int, default=500, help="时刻匹配窗口（缺省 ±500ms）")
    args = ap.parse_args(argv)
    if args.window_ms < 0:
        print("--window-ms 不能为负", file=sys.stderr)
        return 1
    passed, summary = verify(args.truth, args.got, args.window_ms)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
