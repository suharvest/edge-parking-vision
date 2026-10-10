"""compare_events plate replay pairing contract tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import compare_events  # noqa: E402


def event(*, event_id: str, ts: int, site: str = "demo-lot", camera: str = "gate-a",
          plate: str = "粤B12345", seq: int = 1) -> dict:
    return {
        "schema": "parking.plate/1", "event_id": event_id, "site_id": site,
        "camera_id": camera, "ts": ts, "seq": seq, "replayed": False,
        "plate": plate, "plate_normalized": plate, "region": "cn",
        "plate_type": "cn_blue", "confidence": 0.9, "votes": 2, "frames": 2,
        "bbox": [0.1, 0.1, 0.5, 0.5], "track_id": 3, "snapshot": None,
    }


def write_jsonl(path: Path, rows: list[dict] | str) -> None:
    path.write_text(rows if isinstance(rows, str) else "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows), encoding="utf-8")


def run_compare(tmp_path: Path, ref: list[dict], got: list[dict], **kwargs):
    ref_path, got_path = tmp_path / "ref.jsonl", tmp_path / "got.jsonl"
    write_jsonl(ref_path, ref)
    write_jsonl(got_path, got)
    return compare_events.compare(str(ref_path), str(got_path), "plate", 0, **kwargs)


def gate(*, event_id: str, plate_event_id: str, ts: int, site: str = "demo-lot",
         camera: str = "gate-a-barrier", action: str = "open_pulse",
         trigger: str = "whitelist", normalized: str = "粤B12345",
         label: str = "staff", pulse_ms: int = 300, latency_ms: int = 100) -> dict:
    return {
        "schema": "parking.gate/1", "event_id": event_id, "site_id": site,
        "camera_id": camera, "ts": ts, "seq": 1, "replayed": False,
        "action": action, "trigger": trigger, "plate_event_id": plate_event_id,
        "plate_normalized": normalized, "whitelist_label": label,
        "pulse_ms": pulse_ms, "latency_ms": latency_ms,
    }


def run_gate_compare(tmp_path: Path, ref_gates: list[dict], got_gates: list[dict],
                     ref_plates: list[dict], got_plates: list[dict], **kwargs):
    paths = [tmp_path / name for name in ("ref-gate.jsonl", "got-gate.jsonl", "ref-plate.jsonl", "got-plate.jsonl")]
    for path, rows in zip(paths, (ref_gates, got_gates, ref_plates, got_plates)):
        write_jsonl(path, rows)
    return compare_events.compare(str(paths[0]), str(paths[1]), "gate", 0,
                                  ref_plates_path=str(paths[2]), got_plates_path=str(paths[3]), **kwargs)


def test_gate_pairs_via_plate_mapping_and_ignores_uuid_seq_latency(tmp_path: Path):
    ref_plate = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    got_plate = event(event_id="00000000-0000-4000-8000-000000000002", ts=1250)
    ref_gate = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=ref_plate["event_id"], ts=1100)
    got_gate = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=got_plate["event_id"], ts=1350,
                    latency_ms=999)
    ok, summary = run_gate_compare(tmp_path, [ref_gate], [got_gate], [ref_plate], [got_plate],
                                   time_offset_ms=250, match_window_ms=2)
    assert ok and summary["matched"] == summary["agreed"] == 1
    assert summary["paired"][0]["ref_latency_ms"] != summary["paired"][0]["got_latency_ms"]


def test_gate_wrong_action_same_count_fails(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    rg = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=rp["event_id"], ts=1100)
    gg = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=gp["event_id"], ts=1100, action="suppressed")
    ok, summary = run_gate_compare(tmp_path, [rg], [gg], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["matched"] == 1 and summary["agreed"] == 0


@pytest.mark.parametrize("change", ["orphan", "wrong_site", "wrong_normalized"])
def test_gate_reference_and_identity_constraints_fail(tmp_path: Path, change: str):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    rg = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=rp["event_id"], ts=1100)
    gg = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=gp["event_id"], ts=1100)
    if change == "orphan":
        gg["plate_event_id"] = "00000000-0000-4000-8000-000000000099"
    elif change == "wrong_site":
        gg["site_id"] = "other-lot"
    elif change == "wrong_normalized":
        gg["plate_normalized"] = "粤A99999"
    else:
        gg["camera_id"] = gp["camera_id"]
    ok, summary = run_gate_compare(tmp_path, [rg], [gg], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok
    assert summary["orphan"] or summary["problems"]


def test_gate_camera_equal_to_plate_camera_is_still_pairable(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    rg = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=rp["event_id"], ts=1100,
              camera=rp["camera_id"])
    gg = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=gp["event_id"], ts=1100,
              camera=gp["camera_id"])
    ok, summary = run_gate_compare(tmp_path, [rg], [gg], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert ok and summary["matched"] == 1


def test_gate_duplicate_key_is_ambiguous_and_missing_extra_are_reported(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    rg1 = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=rp["event_id"], ts=1100)
    rg2 = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=rp["event_id"], ts=1101)
    gg = gate(event_id="00000000-0000-4000-8000-000000000005", plate_event_id=gp["event_id"], ts=1100)
    ok, summary = run_gate_compare(tmp_path, [rg1, rg2], [gg], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["ambiguous"]
    assert summary["missing"] == []  # ambiguity is reported separately from a greedy missing pair


def test_gate_empty_requires_explicit_allow_and_manual_test_fails(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    ok, summary = run_gate_compare(tmp_path, [], [], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and any("ref gate 事件为空" in p for p in summary["problems"])
    ok, summary = run_gate_compare(tmp_path, [], [], [rp], [gp], time_offset_ms=0, match_window_ms=5,
                                   allow_empty_gates=True)
    assert ok and summary["empty_gate_observation"] and "不能证明" in summary["empty_gate_observation_note"]
    manual = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id="00000000-0000-4000-8000-000000000099",
                  ts=1000, trigger="manual_test", normalized="manual-test", label="manual_test")
    ok, summary = run_gate_compare(tmp_path, [manual], [manual], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["manual_unsupported"]


def test_gate_allow_empty_cannot_accept_plate_problem(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000, plate="粤A99999")
    gp["plate_type"] = "cn_green"
    ok, summary = run_gate_compare(tmp_path, [], [], [rp], [gp], time_offset_ms=0, match_window_ms=5,
                                   allow_empty_gates=True)
    assert not ok and not summary["empty_gate_observation"]


def test_gate_ref_nonempty_got_empty_is_readable_and_counts_missing(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    rg = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=rp["event_id"], ts=1100)
    ok, summary = run_gate_compare(tmp_path, [rg], [], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and len(summary["missing"]) == 1 and not any("Traceback" in p for p in summary["problems"])


def test_gate_ref_empty_got_nonempty_counts_extra_once(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    gg = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=gp["event_id"], ts=1100)
    ok, summary = run_gate_compare(tmp_path, [], [gg], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["extra"] == [gg["event_id"]]


def test_gate_schema_bad_reference_is_error_without_traceback(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    rg = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=rp["event_id"], ts=1100)
    gg = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=gp["event_id"], ts=1100)
    gg["plate_event_id"] = []
    ok, summary = run_gate_compare(tmp_path, [rg], [gg], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and any("校验失败" in p for p in summary["problems"])
    assert all("Traceback" not in p for p in summary["problems"])


def test_gate_required_field_error_does_not_use_keyset_parity(tmp_path: Path):
    rp = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    gp = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)
    rg = gate(event_id="00000000-0000-4000-8000-000000000003", plate_event_id=rp["event_id"], ts=1100)
    gg = gate(event_id="00000000-0000-4000-8000-000000000004", plate_event_id=gp["event_id"], ts=1100)
    del gg["latency_ms"]
    ok, summary = run_gate_compare(tmp_path, [rg], [gg], [rp], [gp], time_offset_ms=0, match_window_ms=5)
    assert not ok and any("校验失败" in p for p in summary["problems"])
    assert not any("字段集合不一致" in p for p in summary["problems"])


def test_plate_pairs_different_uuid_and_explicit_offset(tmp_path: Path):
    ok, summary = run_compare(
        tmp_path, [event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)],
        [event(event_id="00000000-0000-4000-8000-000000000002", ts=1250)],
        time_offset_ms=250, match_window_ms=10,
    )
    assert ok and summary["matched"] == summary["agreed"] == 1


def test_plate_wrong_text_same_count_fails(tmp_path: Path):
    got = event(event_id="00000000-0000-4000-8000-000000000002", ts=1000, plate="粤A99999")
    ok, summary = run_compare(tmp_path, [event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)], [got], time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["matched"] == 1 and summary["agreed"] == 0


def test_plate_stream_and_window_are_strict(tmp_path: Path):
    ref = [event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)]
    got = [event(event_id="00000000-0000-4000-8000-000000000002", ts=1000, camera="other")]
    ok, summary = run_compare(tmp_path, ref, got, time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["missing"] and summary["extra"]
    got[0]["camera_id"] = "gate-a"
    got[0]["ts"] = 1006
    ok, summary = run_compare(tmp_path, ref, got, time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["missing"] and summary["extra"]


def test_plate_ambiguity_rejects_greedy_pair(tmp_path: Path):
    ref = [event(event_id="00000000-0000-4000-8000-000000000001", ts=1000),
           event(event_id="00000000-0000-4000-8000-000000000003", ts=1002)]
    got = [event(event_id="00000000-0000-4000-8000-000000000002", ts=1001)]
    ok, summary = run_compare(tmp_path, ref, got, time_offset_ms=0, match_window_ms=5)
    assert not ok and summary["ambiguous"] and not summary["paired"]


def test_plate_mixed_ambiguity_keeps_unique_pair_and_classifies_zero_degree(tmp_path: Path):
    ref = [
        event(event_id="00000000-0000-4000-8000-000000000001", ts=1000),
        event(event_id="00000000-0000-4000-8000-000000000003", ts=1002),
        event(event_id="00000000-0000-4000-8000-000000000004", ts=5000),
    ]
    got = [
        event(event_id="00000000-0000-4000-8000-000000000002", ts=1001),
        event(event_id="00000000-0000-4000-8000-000000000005", ts=2000),
        event(event_id="00000000-0000-4000-8000-000000000006", ts=6000),
    ]
    # The first two refs are ambiguous with got[0]; got[1] is extra and ref[2] missing.
    # Add an independent unique pair in another stream to prove it is still compared.
    ref.append(event(event_id="00000000-0000-4000-8000-000000000007", ts=8000, camera="side"))
    got.append(event(event_id="00000000-0000-4000-8000-000000000008", ts=8000, camera="side", plate="粤A00000"))
    ok, summary = run_compare(tmp_path, ref, got, time_offset_ms=0, match_window_ms=5)
    assert not ok
    assert summary["matched"] == 1 and summary["agreed"] == 0
    assert summary["ambiguous"]
    assert "00000000-0000-4000-8000-000000000004" in summary["missing"]
    assert "00000000-0000-4000-8000-000000000005" in summary["extra"]


def test_plate_duplicate_ids_and_empty_fail(tmp_path: Path):
    one = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    ok, summary = run_compare(tmp_path, [one, dict(one)], [dict(one)], time_offset_ms=0, match_window_ms=5)
    assert not ok and any("重复 event_id" in x for x in summary["problems"])
    ok, summary = run_compare(tmp_path, [], [dict(one)], time_offset_ms=0, match_window_ms=5)
    assert not ok and any("ref 事件为空" in x for x in summary["problems"])


@pytest.mark.parametrize("offset,window", [(None, 5), (0, None), (float("nan"), 5), (0, 0), (0, float("inf"))])
def test_plate_parameters_are_explicit_finite_and_window_positive(tmp_path: Path, offset, window):
    one = event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)
    ok, summary = run_compare(tmp_path, [one], [dict(one)], time_offset_ms=offset, match_window_ms=window)
    assert not ok and summary["problems"]


def test_plate_cli_missing_parameters_is_readable(tmp_path: Path):
    ref = tmp_path / "ref.jsonl"
    got = tmp_path / "got.jsonl"
    write_jsonl(ref, [event(event_id="00000000-0000-4000-8000-000000000001", ts=1000)])
    write_jsonl(got, [event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)])
    proc = subprocess.run(
        ["uv", "run", "python", "tools/compare_events.py", "--ref", str(ref), "--got", str(got), "--kind", "plate"],
        cwd=Path(__file__).parents[1], text=True, capture_output=True,
    )
    assert proc.returncode == 1
    assert "必须显式提供" in proc.stdout and "Traceback" not in proc.stderr


def test_plate_bad_json_and_schema_do_not_traceback(tmp_path: Path):
    ref, got = tmp_path / "ref.jsonl", tmp_path / "got.jsonl"
    write_jsonl(ref, "not-json\n")
    write_jsonl(got, [event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)])
    ok, summary = compare_events.compare(str(ref), str(got), "plate", 0,
                                         time_offset_ms=0, match_window_ms=5)
    assert not ok
    assert all("Traceback" not in x for x in summary["problems"])
    malformed = event(event_id="00000000-0000-4000-8000-000000000003", ts=1000)
    malformed["site_id"] = []
    write_jsonl(ref, [malformed])
    ok, summary = compare_events.compare(str(ref), str(got), "plate", 0,
                                         time_offset_ms=0, match_window_ms=5)
    assert not ok and any("校验失败" in x for x in summary["problems"])


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_plate_cli_rejects_nonfinite_json_values(tmp_path: Path, constant: str):
    ref = tmp_path / "ref.jsonl"
    got = tmp_path / "got.jsonl"
    row = json.dumps(event(event_id="00000000-0000-4000-8000-000000000001", ts=1000))
    write_jsonl(ref, row.replace("0.9", constant, 1) + "\n")
    write_jsonl(got, [event(event_id="00000000-0000-4000-8000-000000000002", ts=1000)])
    proc = subprocess.run(
        ["uv", "run", "python", "tools/compare_events.py", "--ref", str(ref), "--got", str(got),
         "--kind", "plate", "--time-offset-ms", "0", "--match-window-ms", "5"],
        cwd=Path(__file__).parents[1], text=True, capture_output=True,
    )
    assert proc.returncode == 1
    assert "JSON 解析失败" in proc.stdout and "Traceback" not in proc.stderr
