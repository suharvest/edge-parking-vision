#!/usr/bin/env python3
"""事件回放比对工具（规格 §S9「RTSP 回放」行、§8.1 C4）。

比对参考事件流（CPU 基座生成、入库）与某平台实际产出的事件流：
  1. 两个文件的每条事件都过 contracts/validate_payload.py 校验；
  2. crossing：in / out 各自计数差 ≤ max(1, tolerance * ref_count)
     （--count-tolerance 缺省 0 = 必须完全一致）；
  3. 两文件事件的字段集合必须相同（§S3：payload 与平台无关）。

plate kind 是 CPU/platform 回放事件 parity 工具。它要求调用者显式提供
`--time-offset-ms`（got.ts 减去该值后与 ref.ts 同一时间基准）和
`--match-window-ms`，只在相同 site_id+camera_id 的 stream 内按时间构造候选，
再比较 plate_normalized/region/plate_type。它不代表正式视频 truth 准确率。

用法：
  uv run python tools/compare_events.py --ref tests/fixtures/replay/gate_120s.cpu.jsonl \
      --got replay.jsonl --kind crossing [--count-tolerance 0.02]

退出码：0 通过；1 不通过 / 用法或文件错误。
仅标准库。
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
import validate_payload  # noqa: E402

VALID_KINDS = ("crossing", "occupancy", "plate", "gate")


def _load_events(path: str, kind: str, label: str, problems: list[str]) -> list[dict]:
    events: list[dict] = []
    try:
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    if kind in ("plate", "gate"):
                        def reject_nonfinite(value: str) -> None:
                            raise ValueError(f"不允许 JSON 常量 {value}")
                        ev = json.loads(line, parse_constant=reject_nonfinite)
                    else:
                        ev = json.loads(line)
                except (json.JSONDecodeError, ValueError) as e:
                    problems.append(f"{label}:{lineno}: JSON 解析失败: {e}")
                    continue
                if not isinstance(ev, dict):
                    problems.append(f"{label}:{lineno}: 事件不是 object")
                    continue
                validator_kind = {"plate": "plate-event", "gate": "gate-event"}.get(kind, kind)
                for err in validate_payload.validate(validator_kind, ev):
                    problems.append(f"{label}:{lineno}: 校验失败: {err}")
                events.append(ev)
    except OSError as e:
        problems.append(f"{label}: 无法读取 {path}: {e}")
    return events


def _event_id_key(value: object) -> str:
    """可比较的 event_id 键；schema 错误仍由 validator 报告。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _duplicate_event_ids(events: list[dict], label: str, problems: list[str]) -> None:
    seen: dict[str, int] = {}
    for index, event in enumerate(events, 1):
        key = _event_id_key(event.get("event_id"))
        if key in seen:
            problems.append(f"{label}:重复 event_id（第 {seen[key]} 与第 {index} 条）")
        else:
            seen[key] = index


def _compare_plate(
    ref: list[dict],
    got: list[dict],
    problems: list[str],
    time_offset_ms: float | None,
    match_window_ms: float | None,
) -> dict:
    """严格的一对一 plate event 配对；返回 summary 的 plate 细节。"""
    if time_offset_ms is None:
        problems.append("plate 必须显式提供 --time-offset-ms（可为 0）")
    elif isinstance(time_offset_ms, bool) or not isinstance(time_offset_ms, (int, float)):
        problems.append("--time-offset-ms 必须是有限数字")
    elif not math.isfinite(time_offset_ms):
        problems.append("--time-offset-ms 必须是有限数字")
    if match_window_ms is None:
        problems.append("plate 必须显式提供 --match-window-ms")
    elif isinstance(match_window_ms, bool) or not isinstance(match_window_ms, (int, float)):
        problems.append("--match-window-ms 必须是有限正数")
    elif not math.isfinite(match_window_ms) or match_window_ms <= 0:
        problems.append("--match-window-ms 必须是有限正数")

    detail = {
        "paired": [],
        "matched": 0,
        "agreed": 0,
        "missing": [],
        "extra": [],
        "ambiguous": [],
    }
    if problems or not ref or not got:
        if not ref:
            problems.append("ref 事件为空")
        if not got:
            problems.append("got 事件为空")
        return detail

    # Invalid schema/timestamps are already reported, and cannot safely form candidates.
    candidates: dict[tuple[str, int], list[tuple[str, int, float]]] = {}
    ref_groups: dict[tuple[object, object], list[int]] = {}
    got_groups: dict[tuple[object, object], list[int]] = {}
    for i, event in enumerate(ref):
        site, camera = event.get("site_id"), event.get("camera_id")
        if isinstance(site, str) and isinstance(camera, str):
            ref_groups.setdefault((site, camera), []).append(i)
    for j, event in enumerate(got):
        site, camera = event.get("site_id"), event.get("camera_id")
        if isinstance(site, str) and isinstance(camera, str):
            got_groups.setdefault((site, camera), []).append(j)
    for group, ref_indexes in ref_groups.items():
        for i in ref_indexes:
            ts_ref = ref[i].get("ts")
            if isinstance(ts_ref, bool) or not isinstance(ts_ref, int):
                continue
            for j in got_groups.get(group, []):
                ts_got = got[j].get("ts")
                if isinstance(ts_got, bool) or not isinstance(ts_got, int):
                    continue
                delta = (ts_got - time_offset_ms) - ts_ref  # type: ignore[operator]
                if abs(delta) <= match_window_ms:  # type: ignore[operator]
                    candidates[("ref", i)] = candidates.get(("ref", i), []) + [("got", j, delta)]
                    candidates[("got", j)] = candidates.get(("got", j), []) + [("ref", i, delta)]

    ambiguous_refs = {i for side, i in candidates if side == "ref" and len(candidates[(side, i)]) > 1}
    ambiguous_gots = {j for side, j in candidates if side == "got" and len(candidates[(side, j)]) > 1}
    # Degree-one endpoints connected to a degree>1 endpoint are part of the
    # ambiguous component and must not be greedily consumed.
    for i in tuple(ambiguous_refs):
        for _, j, _ in candidates[("ref", i)]:
            ambiguous_gots.add(j)
    for j in tuple(ambiguous_gots):
        for _, i, _ in candidates[("got", j)]:
            ambiguous_refs.add(i)
    for i in sorted(ambiguous_refs):
        detail["ambiguous"].append({"side": "ref", "event_id": ref[i].get("event_id"),
                                    "candidate_count": len(candidates[("ref", i)])})
    for j in sorted(ambiguous_gots):
        detail["ambiguous"].append({"side": "got", "event_id": got[j].get("event_id"),
                                    "candidate_count": len(candidates[("got", j)])})
    if detail["ambiguous"]:
        problems.append(f"存在歧义候选，拒绝 greedy 配对（{len(detail['ambiguous'])} 条）")

    paired_got: set[int] = set()
    for i in range(len(ref)):
        options = candidates.get(("ref", i), [])
        if not options:
            detail["missing"].append(ref[i].get("event_id"))
            continue
        if i in ambiguous_refs:
            continue
        _, j, delta = options[0]
        if j in ambiguous_gots or len(candidates.get(("got", j), [])) != 1:
            continue
        paired_got.add(j)
        differences = {
            field: {"ref": ref[i].get(field), "got": got[j].get(field)}
            for field in ("plate_normalized", "region", "plate_type")
            if ref[i].get(field) != got[j].get(field)
        }
        agreed = not differences
        detail["matched"] += 1
        if agreed:
            detail["agreed"] += 1
        else:
            problems.append(f"配对事件业务字段不一致: ref={ref[i].get('event_id')} got={got[j].get('event_id')}")
        detail["paired"].append({"ref_event_id": ref[i].get("event_id"),
                                 "got_event_id": got[j].get("event_id"),
                                 "delta_ms": delta, "agreed": agreed,
                                 "differences": differences})
    detail["extra"] = [got[j].get("event_id") for j in range(len(got))
                        if not candidates.get(("got", j), [])]
    if detail["missing"]:
        problems.append(f"缺失 plate 事件: {len(detail['missing'])}")
    if detail["extra"]:
        problems.append(f"多余 plate 事件: {len(detail['extra'])}")
    return detail


def _check_pair_parameters(kind: str, time_offset_ms: float | None,
                           match_window_ms: float | None,
                           problems: list[str]) -> bool:
    valid = True
    if time_offset_ms is None:
        problems.append(f"{kind} 必须显式提供 --time-offset-ms（可为 0）")
        valid = False
    elif isinstance(time_offset_ms, bool) or not isinstance(time_offset_ms, (int, float)):
        problems.append("--time-offset-ms 必须是有限数字")
        valid = False
    elif not math.isfinite(time_offset_ms):
        problems.append("--time-offset-ms 必须是有限数字")
        valid = False
    if match_window_ms is None:
        problems.append(f"{kind} 必须显式提供 --match-window-ms")
        valid = False
    elif isinstance(match_window_ms, bool) or not isinstance(match_window_ms, (int, float)):
        problems.append("--match-window-ms 必须是有限正数")
        valid = False
    elif not math.isfinite(match_window_ms) or match_window_ms <= 0:
        problems.append("--match-window-ms 必须是有限正数")
        valid = False
    return valid


def _compare_gate(
    ref: list[dict], got: list[dict], ref_plates: list[dict], got_plates: list[dict],
    plate_detail: dict, plate_ok: bool, problems: list[str], time_offset_ms: float | None,
    match_window_ms: float | None, allow_empty_gates: bool,
) -> dict:
    """Pair gate events through the already paired plate events.

    Gate event UUIDs, seq values and latency values are platform observations;
    they are schema-checked and reported, but are deliberately not parity keys.
    """
    detail = {
        "paired": [], "matched": 0, "agreed": 0, "missing": [], "extra": [],
        "ambiguous": [], "orphan": [], "manual_unsupported": [],
        "empty_gate_observation": False,
    }
    params_ok = _check_pair_parameters("gate", time_offset_ms, match_window_ms, problems)

    plate_map: dict[str, str] = {}
    for pair in plate_detail.get("paired", []):
        ref_id, got_id = pair.get("ref_event_id"), pair.get("got_event_id")
        if isinstance(ref_id, str) and isinstance(got_id, str):
            plate_map[ref_id] = got_id
    ref_by_id = {e.get("event_id"): e for e in ref_plates if isinstance(e.get("event_id"), str)}
    got_by_id = {e.get("event_id"): e for e in got_plates if isinstance(e.get("event_id"), str)}

    def inspect(events: list[dict], plates: dict[str, dict], label: str) -> dict[tuple[str, str, str], list[int]]:
        groups: dict[tuple[str, str, str], list[int]] = {}
        for index, event in enumerate(events):
            if event.get("trigger") != "whitelist":
                detail["manual_unsupported"].append({"side": label, "event_id": event.get("event_id"),
                                                       "trigger": event.get("trigger")})
                continue
            plate_id = event.get("plate_event_id")
            # Keep schema-invalid events in the loaded stream so the overall
            # result fails, but never let an unhashable reference crash replay.
            plate = plates.get(plate_id) if isinstance(plate_id, str) else None
            if plate is None:
                detail["orphan"].append({"side": label, "event_id": event.get("event_id"),
                                         "plate_event_id": plate_id})
                continue
            if event.get("site_id") != plate.get("site_id"):
                problems.append(f"{label} gate site_id 不等于 plate site_id: {event.get('event_id')}")
            if event.get("plate_normalized") != plate.get("plate_normalized"):
                problems.append(f"{label} gate plate_normalized 不等于 plate: {event.get('event_id')}")
            if isinstance(plate_id, str) and isinstance(event.get("site_id"), str) and isinstance(event.get("camera_id"), str):
                key = (plate_id, event["site_id"], event["camera_id"])
                groups.setdefault(key, []).append(index)
        return groups

    ref_groups = inspect(ref, ref_by_id, "ref")
    got_groups = inspect(got, got_by_id, "got")
    for side, groups in (("ref", ref_groups), ("got", got_groups)):
        for key, indexes in groups.items():
            if len(indexes) > 1:
                detail["ambiguous"].append({"side": side, "key": list(key), "count": len(indexes)})
    if detail["ambiguous"]:
        problems.append(f"gate 同一 plate/site/camera 存在多个事件，拒绝歧义配对（{len(detail['ambiguous'])} 组）")
    if detail["manual_unsupported"]:
        problems.append("gate manual_test 没有可映射 plate，当前 replay scope 不支持")
    if detail["orphan"]:
        problems.append(f"gate 存在 orphan plate_event_id: {len(detail['orphan'])}")

    # Map ref keys to got keys through the one-to-one plate mapping.
    got_groups_by_key: dict[tuple[str, str, str], list[int]] = {}
    for (plate_id, site, camera), indexes in got_groups.items():
        got_groups_by_key[(plate_id, site, camera)] = indexes
    for ref_key, ref_indexes in ref_groups.items():
        ref_plate, site, camera = ref_key
        got_plate = plate_map.get(ref_plate)
        candidates = got_groups_by_key.get((got_plate, site, camera), []) if got_plate else []
        if len(ref_indexes) != 1 or len(candidates) != 1:
            if len(ref_indexes) != 1:
                continue
            detail["missing"].append(ref[ref_indexes[0]].get("event_id"))
            continue
        i, j = ref_indexes[0], candidates[0]
        ref_ts, got_ts = ref[i].get("ts"), got[j].get("ts")
        if not params_ok or isinstance(ref_ts, bool) or not isinstance(ref_ts, int) \
                or isinstance(got_ts, bool) or not isinstance(got_ts, int):
            continue
        delta = (got_ts - time_offset_ms) - ref_ts  # type: ignore[operator]
        if abs(delta) > match_window_ms:  # type: ignore[operator]
            detail["missing"].append(ref[i].get("event_id"))
            continue
        differences = {field: {"ref": ref[i].get(field), "got": got[j].get(field)}
                       for field in ("action", "trigger", "plate_normalized", "whitelist_label", "pulse_ms")
                       if ref[i].get(field) != got[j].get(field)}
        agreed = not differences
        detail["matched"] += 1
        if agreed:
            detail["agreed"] += 1
        else:
            problems.append(f"配对 gate 业务字段不一致: ref={ref[i].get('event_id')} got={got[j].get('event_id')}")
        detail["paired"].append({"ref_event_id": ref[i].get("event_id"), "got_event_id": got[j].get("event_id"),
                                 "delta_ms": delta, "agreed": agreed, "differences": differences,
                                 "ref_latency_ms": ref[i].get("latency_ms"), "got_latency_ms": got[j].get("latency_ms")})

    used_got = {p["got_event_id"] for p in detail["paired"]}
    for key, indexes in got_groups.items():
        if len(indexes) == 1 and got[indexes[0]].get("event_id") not in used_got:
            detail["extra"].append(got[indexes[0]].get("event_id"))
    if detail["missing"]:
        problems.append(f"缺失 gate 事件: {len(detail['missing'])}")
    if detail["extra"]:
        problems.append(f"多余 gate 事件: {len(detail['extra'])}")
    if not ref and not got:
        if allow_empty_gates and ref_plates and got_plates and plate_ok:
            detail["empty_gate_observation"] = True
            detail["empty_gate_observation_note"] = "0/0 gate parity 不能证明物理 DO/白名单验收"
        else:
            problems.append("ref gate 事件为空")
            problems.append("got gate 事件为空")
    elif not ref:
        problems.append("ref gate 事件为空")
    elif not got:
        problems.append("got gate 事件为空")
    return detail


def compare(ref_path: str, got_path: str, kind: str, tolerance: float,
            time_offset_ms: float | None = None,
            match_window_ms: float | None = None,
            ref_plates_path: str | None = None,
            got_plates_path: str | None = None,
            allow_empty_gates: bool = False) -> tuple[bool, dict]:
    problems: list[str] = []
    event_problems: list[str] = []
    ref = _load_events(ref_path, kind, "ref", event_problems)
    got = _load_events(got_path, kind, "got", event_problems)

    # plate 只比较业务 required 字段；UUID/seq/snapshot/hash/track_id 等可平台不同。
    plate_detail = None
    if kind == "plate":
        _duplicate_event_ids(ref, "ref", problems)
        _duplicate_event_ids(got, "got", problems)
        plate_detail = _compare_plate(ref, got, problems, time_offset_ms, match_window_ms)
    elif kind == "gate":
        _duplicate_event_ids(ref, "ref", problems)
        _duplicate_event_ids(got, "got", problems)
        if not ref_plates_path or not got_plates_path:
            problems.append("gate 必须显式提供 --ref-plates 与 --got-plates JSONL")
            ref_plates, got_plates = [], []
        else:
            ref_plates = _load_events(ref_plates_path, "plate", "ref-plates", problems)
            got_plates = _load_events(got_plates_path, "plate", "got-plates", problems)
            _duplicate_event_ids(ref_plates, "ref-plates", problems)
            _duplicate_event_ids(got_plates, "got-plates", problems)
        # Plate matching is the authority for cross-run gate references.
        plate_detail = _compare_plate(ref_plates, got_plates, problems, time_offset_ms, match_window_ms)
        plate_ok = not problems
        gate_detail = _compare_gate(ref, got, ref_plates, got_plates, plate_detail, plate_ok, problems,
                                    time_offset_ms, match_window_ms, allow_empty_gates)
    problems.extend(event_problems)

    # 字段集合一致性（§S3：同一段视频在任意平台产生的事件字段集合相同）
    ref_keysets = {tuple(sorted(e.keys())) for e in ref}
    got_keysets = {tuple(sorted(e.keys())) for e in got}
    if kind in ("crossing", "occupancy") and ref and got and ref_keysets != got_keysets:
        problems.append(
            f"字段集合不一致: ref-only={ref_keysets - got_keysets} got-only={got_keysets - ref_keysets}"
        )

    summary: dict = {"kind": kind, "ref": len(ref), "got": len(got)}
    if kind == "plate":
        summary.update(plate_detail or {})
        passed = not problems and bool(ref) and bool(got)
    elif kind == "gate":
        summary["plates"] = {"ref": len(ref_plates), "got": len(got_plates), **(plate_detail or {})}
        summary.update(gate_detail)
        passed = not problems and bool(ref_plates) and bool(got_plates) and bool(ref) and bool(got)
        if allow_empty_gates and gate_detail.get("empty_gate_observation"):
            passed = not problems and bool(ref_plates) and bool(got_plates)
    elif kind == "crossing":
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
    ap.add_argument("--time-offset-ms", type=float,
                    help="plate 必填：从 got.ts 减去的时间偏移（可为负数或 0）")
    ap.add_argument("--match-window-ms", type=float,
                    help="plate/gate 必填：时间匹配窗口，必须为有限正数")
    ap.add_argument("--ref-plates", help="gate 必填：参考 plate 事件 JSONL")
    ap.add_argument("--got-plates", help="gate 必填：被测 plate 事件 JSONL")
    ap.add_argument("--allow-empty-gates", action="store_true",
                    help="仅在两边有效 plate 比较通过时允许 gate 0/0（不能证明物理 DO/白名单验收）")
    args = ap.parse_args(argv)
    if args.count_tolerance < 0:
        print("--count-tolerance 不能为负", file=sys.stderr)
        return 1
    if args.kind == "gate" and (not args.ref_plates or not args.got_plates):
        print("gate 必须显式提供 --ref-plates 与 --got-plates JSONL")
        return 1
    passed, summary = compare(args.ref, args.got, args.kind, args.count_tolerance,
                              args.time_offset_ms, args.match_window_ms,
                              args.ref_plates, args.got_plates, args.allow_empty_gates)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
