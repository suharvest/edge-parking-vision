#!/usr/bin/env python3
"""事件回放比对工具（规格 §S9「RTSP 回放」行、§8.1 C4）。

比对参考事件流（CPU 基座生成、入库）与某平台实际产出的事件流：
  1. 两个文件的每条事件都过 contracts/validate_payload.py 校验；
  2. crossing：in / out 各自计数差 ≤ max(1, tolerance * ref_count)
     （--count-tolerance 缺省 0 = 必须完全一致）；
  3. 两文件事件的字段集合必须相同（§S3：payload 与平台无关）。

用法：
  uv run python tools/compare_events.py --ref tests/fixtures/replay/gate_120s.cpu.jsonl \
      --got replay.jsonl --kind crossing [--count-tolerance 0.02]

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

VALID_KINDS = ("crossing", "occupancy")


def _load_events(path: str, kind: str, label: str, problems: list[str]) -> list[dict]:
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError as e:
                    problems.append(f"{label}:{lineno}: JSON 解析失败: {e}")
                    continue
                if not isinstance(ev, dict):
                    problems.append(f"{label}:{lineno}: 事件不是 object")
                    continue
                for err in validate_payload.validate(kind, ev):
                    problems.append(f"{label}:{lineno}: 校验失败: {err}")
                events.append(ev)
    except OSError as e:
        problems.append(f"{label}: 无法读取 {path}: {e}")
    return events


def compare(ref_path: str, got_path: str, kind: str, tolerance: float) -> tuple[bool, dict]:
    problems: list[str] = []
    ref = _load_events(ref_path, kind, "ref", problems)
    got = _load_events(got_path, kind, "got", problems)

    # 字段集合一致性（§S3：同一段视频在任意平台产生的事件字段集合相同）
    ref_keysets = {tuple(sorted(e.keys())) for e in ref}
    got_keysets = {tuple(sorted(e.keys())) for e in got}
    if ref and got and ref_keysets != got_keysets:
        problems.append(
            f"字段集合不一致: ref-only={ref_keysets - got_keysets} got-only={got_keysets - ref_keysets}"
        )

    summary: dict = {"kind": kind, "ref": len(ref), "got": len(got)}
    if kind == "crossing":
        ok_counts = True
        per_dir: dict = {}
        for direction in ("in", "out"):
            n_ref = sum(1 for e in ref if e.get("direction") == direction)
            n_got = sum(1 for e in got if e.get("direction") == direction)
            allowed = max(1, int(round(tolerance * n_ref))) if tolerance else 0
            diff = abs(n_ref - n_got)
            per_dir[direction] = {"ref": n_ref, "got": n_got, "allowed_diff": allowed}
            if diff > allowed:
                ok_counts = False
                problems.append(
                    f"direction={direction} 计数差 {diff} > 允许 {allowed} (ref={n_ref} got={n_got})"
                )
        summary["directions"] = per_dir
        passed = not problems and ok_counts
    else:
        # occupancy：条数差按同一容差口径
        allowed = max(1, int(round(tolerance * len(ref)))) if tolerance else 0
        if abs(len(ref) - len(got)) > allowed:
            problems.append(f"事件条数差 {abs(len(ref) - len(got))} > 允许 {allowed}")
        passed = not problems

    summary["problems"] = problems
    summary["passed"] = passed
    return passed, summary


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ref", required=True, help="参考事件 JSONL（CPU 基座生成）")
    ap.add_argument("--got", required=True, help="被测平台事件 JSONL")
    ap.add_argument("--kind", required=True, choices=VALID_KINDS)
    ap.add_argument("--count-tolerance", type=float, default=0.0,
                    help="计数容差比例（0.02 = 每方向允许 max(1, 2%%) 差）")
    args = ap.parse_args(argv)
    if args.count_tolerance < 0:
        print("--count-tolerance 不能为负", file=sys.stderr)
        return 1
    passed, summary = compare(args.ref, args.got, args.kind, args.count_tolerance)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
