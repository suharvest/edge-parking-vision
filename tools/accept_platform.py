#!/usr/bin/env python3
"""平台验收工具（规格 §S9「accept_platform.py」行、§8.1 C4）。

在 Mac / spark 上运行，连到被测设备：
  - 在 --minutes 内周期读取基座 /healthz（含停车 app 子对象，§S4）：
    每路 streams[].fps ≥ --min-fps；body 必须是可解析的 JSON 且 status == "ok"。
  - 读 MQTT 抓包 JSONL（每行 {"topic": str, "payload": object}）：
    * .../crossing、.../occupancy 过 contracts/validate_payload.py；
    * .../status 校验为 vb.status/1 的最小结构（schema 字段 + streams 数组）；
    * 0 条无效才通过。
  - healthz 无 app 子对象时按 §S13 B2 跳过 app 字段检查并告警（不判失败）。

结果写 evaluation/runs/<date>-<platform>/accept.json（可用 --out 覆盖）。

用法：
  uv run python tools/accept_platform.py --platform rk3576 \
      --healthz http://192.0.2.10:8099/healthz --mqtt-dump dump.jsonl \
      --minutes 10 --min-fps 9

--healthz 也接受本地文件路径（每次采样重新读取，供离线测试）。
退出码：0 通过；1 不通过 / 用法错误。
仅标准库。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
import validate_payload  # noqa: E402

EVENT_KINDS = ("crossing", "occupancy", "plate", "plate_crop", "gate", "slots")
VALIDATOR_KINDS = {
    "crossing": "crossing",
    "occupancy": "occupancy",
    "plate": "plate-event",
    "plate_crop": "plate-crop",
    "gate": "gate-event",
    "slots": "slots",
}


def _fetch_healthz(source: str, timeout_s: float = 5.0) -> dict:
    if source.startswith("http://") or source.startswith("https://"):
        with urllib.request.urlopen(source, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    with open(source, encoding="utf-8") as f:
        return json.load(f)


def _check_healthz(body: dict, min_fps: float, warnings: list[str], problems: list[str]) -> dict:
    if not isinstance(body, dict):
        problems.append("healthz 不是 JSON object")
        return {"streams": 0, "min_fps_seen": None}
    if body.get("status") != "ok":
        problems.append(f"healthz status={body.get('status')!r}，期望 'ok'")
    if "app" not in body:
        warnings.append("healthz 无 app 子对象（§S13 B2 未实现？），跳过 app 字段检查")
    else:
        app = body["app"]
        for key in ("spool_depth", "spool_overflow", "mqtt_app_connected", "last_event_ts"):
            if key not in app:
                warnings.append(f"healthz app 缺少字段 {key}")
    streams = body.get("streams")
    if not isinstance(streams, list) or not streams:
        problems.append("healthz 缺少非空 streams 数组")
        return {"streams": 0, "min_fps_seen": None}
    min_seen = None
    for s in streams:
        sid = s.get("stream_id", "?")
        fps = s.get("fps")
        if not isinstance(fps, (int, float)):
            problems.append(f"healthz streams[{sid}].fps 缺失或非数值")
            continue
        min_seen = fps if min_seen is None else min(min_seen, fps)
        if fps < min_fps:
            problems.append(f"streams[{sid}].fps={fps} < --min-fps {min_fps}")
    return {"streams": len(streams), "min_fps_seen": min_seen}


def _check_status_payload(payload: object, problems: list[str], label: str) -> None:
    """vb.status/1 最小结构校验（完整 schema 属 BASE-1 §6.7）。"""
    if not isinstance(payload, dict):
        problems.append(f"{label}: status payload 不是 object")
        return
    if payload.get("schema") != "vb.status/1":
        problems.append(f"{label}: status schema={payload.get('schema')!r}，期望 'vb.status/1'")
    if not isinstance(payload.get("streams"), list):
        problems.append(f"{label}: status 缺少 streams 数组")
    if not isinstance(payload.get("device_id"), str):
        problems.append(f"{label}: status 缺少 device_id")


def _check_dump(path: str, warnings: list[str], problems: list[str]) -> dict:
    counts: dict[str, int] = {}
    by_kind: dict[str, dict[str, int]] = {}
    invalid = 0
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            label = f"dump:{lineno}"
            try:
                rec = json.loads(line)
            except json.JSONDecodeError as e:
                problems.append(f"{label}: JSON 解析失败: {e}")
                invalid += 1
                continue
            if not isinstance(rec, dict):
                problems.append(f"{label}: 记录不是 object")
                invalid += 1
                continue
            topic = rec.get("topic")
            payload = rec.get("payload")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    problems.append(f"{label}: payload 字符串不是 JSON")
                    invalid += 1
                    continue
            if not isinstance(topic, str):
                problems.append(f"{label}: 缺少 topic")
                invalid += 1
                continue
            kind = topic.rsplit("/", 1)[-1]
            counts[kind] = counts.get(kind, 0) + 1
            stats = by_kind.setdefault(kind, {"count": 0, "invalid": 0})
            stats["count"] += 1
            if kind in VALIDATOR_KINDS:
                errs = validate_payload.validate(VALIDATOR_KINDS[kind], payload) \
                    if isinstance(payload, dict) \
                    else ["payload 不是 object"]
                if errs:
                    stats["invalid"] += 1
                for err in errs:
                    problems.append(f"{label}: {kind} 校验失败: {err}")
                    invalid += 1
            elif kind == "status":
                before = len(problems)
                _check_status_payload(payload, problems, label)
                row_invalid = len(problems) - before
                invalid += row_invalid
                stats["invalid"] += bool(row_invalid)
            elif kind in ("ack",):
                pass  # vb.ack/1 属基座，这里不校验
            else:
                warning = f"{label}: 未识别的 topic kind {kind!r}，跳过校验"
                warnings.append(warning)
                problems.append(f"{label}: 未识别的 topic kind {kind!r}")
                invalid += 1
                stats["invalid"] += 1
    return {"topics": counts, "by_kind": by_kind, "invalid": invalid}


def run(platform: str, healthz: str, dump: str, minutes: float, min_fps: float,
        interval_s: float, out: str | None) -> tuple[bool, dict]:
    problems: list[str] = []
    warnings: list[str] = []
    samples = []

    deadline = time.monotonic() + max(0.0, minutes) * 60.0
    while True:
        try:
            body = _fetch_healthz(healthz)
        except (OSError, json.JSONDecodeError) as e:
            problems.append(f"healthz 读取失败: {e}")
            body = None
        if body is not None:
            samples.append(_check_healthz(body, min_fps, warnings, problems))
        if time.monotonic() >= deadline:
            break
        time.sleep(interval_s)

    try:
        dump_summary = _check_dump(dump, warnings, problems)
    except OSError as e:
        problems.append(f"mqtt-dump 读取失败: {e}")
        dump_summary = {"topics": {}, "invalid": -1}

    passed = not problems
    report = {
        "platform": platform,
        "date": str(date.today()),
        "minutes": minutes,
        "min_fps": min_fps,
        "healthz_samples": samples,
        "mqtt_dump": dump_summary,
        "warnings": warnings,
        "problems": problems,
        "passed": passed,
    }
    out_path = Path(out) if out else \
        Path("evaluation/runs") / f"{date.today()}-{platform}" / "accept.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return passed, report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--platform", required=True)
    ap.add_argument("--healthz", required=True, help="http(s)://…/healthz 或本地文件路径")
    ap.add_argument("--mqtt-dump", required=True, help="MQTT 抓包 JSONL")
    ap.add_argument("--minutes", type=float, default=10.0)
    ap.add_argument("--min-fps", type=float, required=True)
    ap.add_argument("--interval-s", type=float, default=5.0, help="healthz 采样间隔")
    ap.add_argument("--out", default=None, help="accept.json 输出路径")
    args = ap.parse_args(argv)
    passed, report = run(args.platform, args.healthz, args.mqtt_dump,
                         args.minutes, args.min_fps, args.interval_s, args.out)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
