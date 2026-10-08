"""§8.1 C4 验收：tools/{compare_events,verify_truth,accept_platform,lock_get}.py。

参考文件自比对必过；扰动后必不过。全部离线，不需要 vb-runtime。
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
REPLAY = ROOT / "tests/fixtures/replay/gate_120s.cpu.jsonl"
TRUTH = ROOT / "tools/rtsp-fixture/truth.json"


def run_tool(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "tools" / script), *args],
        cwd=ROOT, capture_output=True, text=True,
    )


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _write_jsonl(path: Path, events: list[dict]) -> None:
    path.write_text("".join(json.dumps(e) + "\n" for e in events))


def _mk_event(ts: int, direction: str, seq: int) -> dict:
    return {
        "schema": "parking.crossing/1", "event_id": str(uuid.uuid4()),
        "site_id": "demo-lot", "camera_id": "gate-a",
        "ts": ts, "seq": seq, "replayed": False,
        "direction": direction, "track_id": 900 + seq,
        "vehicle_class": "car", "confidence": 0.9,
        "occupancy_after": 10, "free_after": 110,
    }


def _mk_plate(ts: int = 1790000001000, seq: int = 0,
              event_id: str = "00000000-0000-4000-8000-000000000001") -> dict:
    return {
        "schema": "parking.plate/1", "event_id": event_id,
        "site_id": "demo-lot", "camera_id": "gate-a", "ts": ts,
        "seq": seq, "replayed": False, "plate": "京A12345",
        "plate_normalized": "京A12345", "region": "cn",
        "plate_type": "cn_blue", "confidence": 0.99, "votes": 3,
        "frames": 5, "bbox": [0.1, 0.2, 0.8, 0.6], "track_id": 1,
        "snapshot": None,
    }


def _mk_gate(plate_event_id: str) -> dict:
    return {
        "schema": "parking.gate/1", "event_id": "00000000-0000-4000-8000-000000000002",
        "site_id": "demo-lot", "camera_id": "gate-a", "ts": 1790000001100,
        "seq": 1, "replayed": False, "action": "open_pulse",
        "trigger": "whitelist", "plate_event_id": plate_event_id,
        "plate_normalized": "京A12345", "whitelist_label": "staff",
        "pulse_ms": 300, "latency_ms": 100,
    }


def _mk_slots() -> dict:
    return {
        "schema": "parking.slots/1", "event_id": "00000000-0000-4000-8000-000000000003",
        "site_id": "demo-lot", "camera_id": "gate-a", "ts": 1790000001200,
        "seq": 2, "replayed": False, "changed": ["A1"],
        "slots": [{"id": "A1", "state": "free", "since": 1790000001200, "cover": 0.1}],
        "occupied": 0, "free": 1, "unknown": 0, "stream_ok": True,
    }


# ---------------------------------------------------------------- compare_events

def test_compare_events_self_compare_passes():
    """C4 验收命令原样：参考文件自比对退出 0。"""
    r = run_tool("compare_events.py", "--ref", str(REPLAY), "--got", str(REPLAY),
                 "--kind", "crossing")
    assert r.returncode == 0, r.stderr + r.stdout
    summary = json.loads(r.stdout)
    assert summary["passed"] is True
    assert summary["ref"] == summary["got"] > 0


def test_compare_events_dropped_events_fail(tmp_path):
    events = _read_jsonl(REPLAY)
    # 去掉 2 条 out：超过 max(1, 2%) 容差
    kept = [e for e in events if e["direction"] == "in"]
    kept += [e for e in events if e["direction"] == "out"][:-2]
    got = tmp_path / "got.jsonl"
    _write_jsonl(got, kept)
    r = run_tool("compare_events.py", "--ref", str(REPLAY), "--got", str(got),
                 "--kind", "crossing", "--count-tolerance", "0.02")
    assert r.returncode == 1
    assert "direction=out" in r.stdout


def test_compare_events_within_tolerance_passes(tmp_path):
    events = _read_jsonl(REPLAY)
    kept = [e for e in events if e["direction"] == "in"]
    kept += [e for e in events if e["direction"] == "out"][:-1]  # 少 1 条 out，容差内
    got = tmp_path / "got.jsonl"
    _write_jsonl(got, kept)
    r = run_tool("compare_events.py", "--ref", str(REPLAY), "--got", str(got),
                 "--kind", "crossing", "--count-tolerance", "0.02")
    assert r.returncode == 0, r.stdout


def test_compare_events_invalid_payload_fails(tmp_path):
    events = _read_jsonl(REPLAY)
    events[0]["direction"] = "sideways"  # 违反契约枚举
    got = tmp_path / "got.jsonl"
    _write_jsonl(got, events)
    r = run_tool("compare_events.py", "--ref", str(REPLAY), "--got", str(got),
                 "--kind", "crossing")
    assert r.returncode == 1
    assert "校验失败" in r.stdout


# ---------------------------------------------------------------- verify_truth

def _got_from_truth() -> list[dict]:
    truth = json.loads(TRUTH.read_text())
    return [_mk_event(c["t_ms"], c["direction"], i)
            for i, c in enumerate(truth["crossings"])]


def test_verify_truth_perfect_match_passes(tmp_path):
    got = tmp_path / "got.jsonl"
    _write_jsonl(got, _got_from_truth())
    r = run_tool("verify_truth.py", "--truth", str(TRUTH), "--got", str(got))
    assert r.returncode == 0, r.stdout
    summary = json.loads(r.stdout)
    assert summary["matched"] == summary["truth_crossings"]
    assert summary["extra_events"] == 0


def test_verify_truth_extra_event_fails(tmp_path):
    """压线停留被计 2 次 → 出现多余事件 → 必不过。"""
    events = _got_from_truth()
    dwell_ts = json.loads(TRUTH.read_text())["crossings"][2]["t_ms"]
    events.append(_mk_event(dwell_ts + 100, "in", 99))  # 压线段多计一次
    got = tmp_path / "got.jsonl"
    _write_jsonl(got, events)
    r = run_tool("verify_truth.py", "--truth", str(TRUTH), "--got", str(got))
    assert r.returncode == 1
    assert "多余事件" in r.stdout


def test_verify_truth_wrong_direction_fails(tmp_path):
    events = _got_from_truth()
    events[0]["direction"] = "out" if events[0]["direction"] == "in" else "in"
    got = tmp_path / "got.jsonl"
    _write_jsonl(got, events)
    r = run_tool("verify_truth.py", "--truth", str(TRUTH), "--got", str(got))
    assert r.returncode == 1
    assert "无匹配事件" in r.stdout


def test_verify_truth_outside_window_fails(tmp_path):
    events = _got_from_truth()
    events[0]["ts"] += 600  # > ±500 ms 窗口
    got = tmp_path / "got.jsonl"
    _write_jsonl(got, events)
    r = run_tool("verify_truth.py", "--truth", str(TRUTH), "--got", str(got))
    assert r.returncode == 1


# ---------------------------------------------------------------- lock_get

LOCK_YAML = """\
retail_vision_repo: https://github.com/Seeed-Solution/retail-vision
retail_vision_tag: vision-base-v0.1.0
bases:
  cpu:
    ref: sensecraft-missionpack.seeed.cn/solution/vision-base-cpu:0.1.0
    digest: "sha256:{d}"
  rk:
    ref: sensecraft-missionpack.seeed.cn/solution/vision-base-rk:0.1.0
    digest: "需核实"
"""


def test_lock_get_outputs_ref_at_digest(tmp_path):
    lock = tmp_path / "lock.yaml"
    lock.write_text(LOCK_YAML.format(d="a" * 64))
    r = run_tool("lock_get.py", "base", "cpu", "--lock", str(lock))
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == (
        "sensecraft-missionpack.seeed.cn/solution/vision-base-cpu@sha256:" + "a" * 64
    )


def test_lock_get_placeholder_digest_fails(tmp_path):
    """X0 之前 digest 为「需核实」：按设计失败（§8.0 X0 行）。"""
    lock = tmp_path / "lock.yaml"
    lock.write_text(LOCK_YAML.format(d="b" * 64))
    r = run_tool("lock_get.py", "base", "rk", "--lock", str(lock))
    assert r.returncode == 1
    assert "需核实" in r.stderr


def test_lock_get_unknown_platform_fails(tmp_path):
    lock = tmp_path / "lock.yaml"
    lock.write_text(LOCK_YAML.format(d="c" * 64))
    r = run_tool("lock_get.py", "base", "jetson", "--lock", str(lock))
    assert r.returncode == 1


# ---------------------------------------------------------------- accept_platform

def _mk_healthz(fps: float, with_app: bool = True) -> dict:
    body = {
        "status": "ok", "device_id": "gate-a", "backend": "rknn",
        "base_version": "0.1.0", "uptime_s": 60.0,
        "mqtt": {"control_connected": True, "publish_connected": [True],
                 "queue_depth": [0], "dropped": [0]},
        "supervisor": {"pid": 1, "rss_kb": 1000, "cpu_s": 1.0},
        "shards": [], "rss_kb_total": 1000,
        "streams": [{"stream_id": "gate-a", "state": "running", "fps": fps,
                     "decode": "mpp"}],
    }
    if with_app:
        body["app"] = {"spool_depth": 0, "spool_overflow": False,
                       "mqtt_app_connected": True, "last_event_ts": 1790000000000}
    return body


def _mk_dump() -> list[dict]:
    return [
        {"topic": "demo-lot/parking/gate-a/status",
         "payload": {"schema": "vb.status/1", "timestamp": 1790000000000,
                     "session_id": "s1", "device_id": "gate-a", "online": True,
                     "versions": {"base": "0.1.0", "app": "edge-parking-counting",
                                  "backend": "rknn", "model_sha256": ""},
                     "uptime_s": 60.0, "shards": 1, "streams": []}},
        {"topic": "demo-lot/parking/gate-a/crossing", "payload": _mk_event(1790000001000, "in", 0)},
        {"topic": "demo-lot/parking/gate-a/crossing", "payload": _mk_event(1790000005000, "out", 1)},
    ]


def _accept(tmp_path: Path, healthz: dict, dump: list[dict], *extra: str) -> subprocess.CompletedProcess:
    hz = tmp_path / "healthz.json"
    hz.write_text(json.dumps(healthz))
    dp = tmp_path / "dump.jsonl"
    dp.write_text("".join(json.dumps(r) + "\n" for r in dump))
    return run_tool("accept_platform.py", "--platform", "rk3576",
                    "--healthz", str(hz), "--mqtt-dump", str(dp),
                    "--minutes", "0", "--min-fps", "9",
                    "--out", str(tmp_path / "accept.json"), *extra)


def test_accept_platform_passes(tmp_path):
    r = _accept(tmp_path, _mk_healthz(18.5), _mk_dump())
    assert r.returncode == 0, r.stdout
    report = json.loads((tmp_path / "accept.json").read_text())
    assert report["passed"] is True
    assert report["mqtt_dump"]["invalid"] == 0
    assert report["healthz_samples"][0]["min_fps_seen"] == 18.5


def test_accept_platform_invalid_event_fails(tmp_path):
    dump = _mk_dump()
    dump[1]["payload"]["direction"] = "north"  # 违反契约
    r = _accept(tmp_path, _mk_healthz(18.5), dump)
    assert r.returncode == 1
    assert "crossing 校验失败" in r.stdout


def test_accept_platform_low_fps_fails(tmp_path):
    r = _accept(tmp_path, _mk_healthz(5.0), _mk_dump())
    assert r.returncode == 1
    assert "--min-fps" in r.stdout


def test_accept_platform_bad_status_schema_fails(tmp_path):
    dump = _mk_dump()
    dump[0]["payload"]["schema"] = "vb.status/2"
    r = _accept(tmp_path, _mk_healthz(18.5), dump)
    assert r.returncode == 1
    assert "vb.status/1" in r.stdout


def test_accept_platform_missing_app_warns_but_passes(tmp_path):
    """§S13 B2 未实现时跳过 app 检查并告警（不判失败）。"""
    r = _accept(tmp_path, _mk_healthz(18.5, with_app=False), _mk_dump())
    assert r.returncode == 0, r.stdout
    report = json.loads((tmp_path / "accept.json").read_text())
    assert any("B2" in w for w in report["warnings"])


def test_accept_platform_valid_plate_gate_slots_are_counted_and_validated(tmp_path):
    plate = _mk_plate()
    dump = _mk_dump()
    dump.extend([
        {"topic": "demo-lot/parking/gate-a/plate", "payload": plate},
        {"topic": "demo-lot/parking/gate-a/gate", "payload": _mk_gate(plate["event_id"])},
        {"topic": "demo-lot/parking/gate-a/slots", "payload": _mk_slots()},
    ])
    r = _accept(tmp_path, _mk_healthz(18.5), dump)
    assert r.returncode == 0, r.stdout
    report = json.loads((tmp_path / "accept.json").read_text())
    by_kind = report["mqtt_dump"]["by_kind"]
    assert by_kind["plate"] == {"count": 1, "invalid": 0}
    assert by_kind["gate"] == {"count": 1, "invalid": 0}
    assert by_kind["slots"] == {"count": 1, "invalid": 0}


def test_accept_platform_declared_kind_invalid_payload_fails_and_counts(tmp_path):
    dump = _mk_dump()
    dump.append({"topic": "demo-lot/parking/gate-a/plate",
                 "payload": {**_mk_plate(), "schema": "parking.plate/2"}})
    r = _accept(tmp_path, _mk_healthz(18.5), dump)
    assert r.returncode == 1
    report = json.loads((tmp_path / "accept.json").read_text())
    assert report["mqtt_dump"]["by_kind"]["plate"] == {"count": 1, "invalid": 1}
    assert "plate 校验失败" in r.stdout


def test_accept_platform_status_only_has_no_plate_count(tmp_path):
    status_only = [_mk_dump()[0]]
    r = _accept(tmp_path, _mk_healthz(18.5), status_only)
    assert r.returncode == 0, r.stdout
    report = json.loads((tmp_path / "accept.json").read_text())
    assert "plate" not in report["mqtt_dump"]["by_kind"]
    assert report["mqtt_dump"]["by_kind"]["status"]["count"] == 1


def test_accept_platform_duplicate_plate_event_ids_are_message_counts_only(tmp_path):
    plate = _mk_plate()
    dump = _mk_dump() + [
        {"topic": "demo-lot/parking/gate-a/plate", "payload": plate},
        {"topic": "demo-lot/parking/gate-a/plate", "payload": plate},
    ]
    r = _accept(tmp_path, _mk_healthz(18.5), dump)
    assert r.returncode == 0, r.stdout
    report = json.loads((tmp_path / "accept.json").read_text())
    assert report["mqtt_dump"]["by_kind"]["plate"] == {"count": 2, "invalid": 0}
    assert "independent" not in report["mqtt_dump"]


@pytest.mark.parametrize("record", [[], 1, None])
def test_accept_platform_nonobject_json_records_fail_with_structured_report(tmp_path, record):
    r = _accept(tmp_path, _mk_healthz(18.5), [record])
    assert r.returncode == 1
    report = json.loads((tmp_path / "accept.json").read_text())
    assert report["passed"] is False
    assert report["mqtt_dump"]["invalid"] == 1
    assert any("dump:1" in problem and "不是 object" in problem
               for problem in report["problems"])


def test_accept_platform_unknown_kind_is_invalid_even_with_warning(tmp_path):
    dump = _mk_dump() + [{"topic": "demo-lot/parking/gate-a/typo_kind", "payload": {}}]
    r = _accept(tmp_path, _mk_healthz(18.5), dump)
    assert r.returncode == 1
    report = json.loads((tmp_path / "accept.json").read_text())
    assert report["passed"] is False
    assert report["mqtt_dump"]["by_kind"]["typo_kind"] == {"count": 1, "invalid": 1}
    assert any("typo_kind" in warning for warning in report["warnings"])
    assert any("typo_kind" in problem for problem in report["problems"])


def test_accept_platform_invalid_record_shape_fails(tmp_path):
    r = _accept(tmp_path, _mk_healthz(18.5), [
        {"topic": "demo-lot/parking/gate-a/plate", "payload": []},
    ])
    assert r.returncode == 1
    report = json.loads((tmp_path / "accept.json").read_text())
    assert report["mqtt_dump"]["invalid"] == 1
    assert report["mqtt_dump"]["by_kind"]["plate"] == {"count": 1, "invalid": 1}
    assert any("dump:1: plate 校验失败" in problem for problem in report["problems"])


def test_accept_platform_ack_is_explicit_compatibility_exception(tmp_path):
    dump = _mk_dump() + [{"topic": "demo-lot/parking/gate-a/ack", "payload": {}}]
    r = _accept(tmp_path, _mk_healthz(18.5), dump)
    assert r.returncode == 0, r.stdout
    report = json.loads((tmp_path / "accept.json").read_text())
    assert report["mqtt_dump"]["by_kind"]["ack"] == {"count": 1, "invalid": 0}
