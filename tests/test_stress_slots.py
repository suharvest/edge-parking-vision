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
                                     main, poll_healthz)

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


def _capacity_sample(mono, processed=(10, 12), *, state="running",
                     pid=1234, stats_pid=1234, ids=("cam-01", "cam-02")):
    return {
        "schema": "vb.healthz/1",
        "status": "ok",
        "_stress_sample_monotonic_ms": mono,
        "_stress_receipt_wall_ms": 1700000000000 + mono,
        "shards": [{"index": 1, "pid": 1200, "alive": True,
                    "runtime": {"pid": pid, "stats_pid": stats_pid,
                                  "effective_contexts": 2,
                                  "stats_monotonic_ms": mono,
                                  "stats_wall_ms": 1700000000000 + mono,
                                  "restarts": 0, "alive": True, "failed": False,
                                  "cpu_s": 1.0, "rss_kb": 1000}}],
        "streams": [
            {"stream_id": sid, "shard": 1, "state": state, "fps": 1.0,
             "inference_ms_p50": 40.0, "inference_ms_p95": 50.0,
             "queue_delay_ms_p95": 100, "dropped_frames": 0,
             "processed_frames": count}
            for sid, count in zip(ids, processed)
        ],
    }


def test_capacity_integrity_expected_ids_counter_delta_and_window():
    samples = [_capacity_sample(0), _capacity_sample(600000, processed=(610, 612))]
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True)
    assert agg["integrity_reasons"] == []
    assert agg["actual_elapsed_s"] == pytest.approx(600)
    assert agg["coverage_sufficient"] is True
    assert agg["streams"]["cam-01"]["processed_delta"] == 600
    assert agg["streams"]["cam-01"]["throughput_fps"] == pytest.approx(1.0)


@pytest.mark.parametrize("kwargs,needle", [
    ({"ids": ("cam-01", "cam-01")}, "duplicate"),
    ({"ids": ("cam-01", "cam-03")}, "expected set"),
    ({"state": "reconnecting"}, "state"),
    ({"processed": (20, 10)}, "decreased"),
    ({"stats_pid": 9999}, "pid"),
])
def test_capacity_integrity_rejects_bad_samples(kwargs, needle):
    first = _capacity_sample(0, **kwargs)
    second_kwargs = dict(kwargs)
    if "processed" in kwargs:
        second_kwargs["processed"] = (10, 11)
    samples = [first, _capacity_sample(600000, **second_kwargs)]
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True)
    assert any(needle in reason for reason in agg["integrity_reasons"])
    assert not judge(agg, target_fps=1.0, platform="cpu",
                     rga_fail_count=None)["pass"]


def test_capacity_integrity_rejects_nonmonotonic_stats_time():
    samples = [_capacity_sample(600000), _capacity_sample(500000)]
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True)
    assert any("strictly increasing" in reason
               for reason in agg["integrity_reasons"])


def test_cached_native_snapshot_is_allowed_with_independent_poll_coverage():
    first = _capacity_sample(1000, processed=(10, 12))
    cached = _capacity_sample(1000, processed=(10, 12))
    final = _capacity_sample(601000, processed=(610, 612))
    first["_stress_sample_monotonic_ms"] = 0
    cached["_stress_sample_monotonic_ms"] = 100
    final["_stress_sample_monotonic_ms"] = 600000
    agg = aggregate_samples([first, cached, final], expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True)
    assert agg["integrity_reasons"] == []
    assert agg["streams"]["cam-01"]["native_segments"][0]["processed_delta"] == 0
    assert agg["streams"]["cam-01"]["native_segments"][1]["processed_delta"] == 600
    assert agg["streams"]["cam-01"]["native_cached_snapshot_count"] == 1
    assert agg["streams"]["cam-01"]["native_cached_snapshot_longest_run"] == 1


def test_strict_single_sample_without_receipt_cannot_pass():
    sample = _capacity_sample(1000)
    sample.pop("_stress_sample_monotonic_ms")
    sample.pop("_stress_receipt_wall_ms")
    agg = aggregate_samples([sample], expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True)
    verdict = judge(agg, target_fps=1.0, platform="cpu", rga_fail_count=None)
    assert not verdict["pass"]
    assert any("poll" in reason or "elapsed" in reason
               for reason in verdict["reasons"])


def test_nonfinite_telemetry_is_structured_fail_and_safe_json():
    sample_a = _capacity_sample(0)
    sample_b = _capacity_sample(600000, processed=(610, 612))
    sample_a["streams"][0]["fps"] = float("nan")
    agg = aggregate_samples([sample_a, sample_b], expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True)
    verdict = judge(agg, target_fps=1.0, platform="cpu", rga_fail_count=None)
    assert not verdict["pass"]
    conclusion = build_conclusion([{"n": 2, "pass": False, "reasons": verdict["reasons"]}],
                                  platform="cpu", target_fps=1.0,
                                  raw_samples=[sample_a])
    encoded = json.dumps(conclusion, allow_nan=False)
    assert "<nonfinite>" in encoded


def test_malformed_native_timestamp_is_structured_fail_not_exception():
    first = _capacity_sample(0)
    second = _capacity_sample(600000, processed=(610, 612))
    first["shards"][0]["runtime"]["stats_monotonic_ms"] = "bad"
    agg = aggregate_samples([first, second], expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True)
    assert any("invalid runtime.stats_monotonic_ms" in reason
               for reason in agg["integrity_reasons"])


def test_cli_fail_on_rejection_is_opt_in_and_records_raw(tmp_path):
    out = tmp_path / "strict.json"
    rc = main(["--platform", "cpu", "--n", "2", "--fail-on-rejection",
               "--sample", str(FIXTURES / "slots_n2_ok.json"),
               "--out", str(out)])
    assert rc == 1
    conclusion = json.loads(out.read_text(encoding="utf-8"))
    assert conclusion["raw_samples"]
    assert conclusion["rows"][0]["integrity_reasons"]


def _two_shard_capacity_samples():
    samples = [_capacity_sample(0), _capacity_sample(600000, processed=(610, 614))]
    for sample in samples:
        second_shard = json.loads(json.dumps(sample["shards"][0]))
        sample["shards"][0]["index"] = 0
        second_shard["index"] = 1
        second_shard["runtime"]["pid"] = 1235
        second_shard["runtime"]["stats_pid"] = 1235
        sample["shards"].append(second_shard)
        sample["streams"][0]["shard"] = 0
        sample["streams"][1]["shard"] = 1
    samples[-1]["shards"][1]["runtime"]["stats_monotonic_ms"] = 602000
    return samples


def test_native_throughput_uses_each_stream_clock_and_accepts_shard_zero():
    agg = aggregate_samples(_two_shard_capacity_samples(), expected_n=2, strict=True)
    assert agg["integrity_reasons"] == []
    assert agg["actual_elapsed_s"] == 600
    assert agg["streams"]["cam-01"]["interval_s"] == 600
    assert agg["streams"]["cam-02"]["interval_s"] == 602
    assert agg["streams"]["cam-02"]["processed_delta"] == 602
    assert agg["streams"]["cam-02"]["throughput_fps"] == 1.0
    assert agg["streams"]["cam-02"]["poll_interval_s"] == 600


@pytest.mark.parametrize("elapsed_ms", [0, 100000])
def test_poll_coverage_does_not_hide_stalled_or_short_native_coverage(elapsed_ms):
    samples = _two_shard_capacity_samples()
    samples[-1]["shards"][0]["runtime"]["stats_monotonic_ms"] = elapsed_ms
    if elapsed_ms == 0:
        samples[-1]["streams"][0]["processed_frames"] = 10
    agg = aggregate_samples(samples, expected_n=2, strict=True)
    verdict = judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)
    assert not verdict["pass"]
    assert any("native elapsed" in reason for reason in verdict["reasons"])


@pytest.mark.parametrize("key", ["stats_wall_ms", "cpu_s", "failed"])
def test_invalid_runtime_values_produce_rejection_without_traceback(key):
    samples = _two_shard_capacity_samples()
    samples[-1]["shards"][0]["runtime"][key] = None
    agg = aggregate_samples(samples, expected_n=2, strict=True)
    assert not judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)["pass"]


def test_fractional_native_counter_is_rejected():
    samples = _two_shard_capacity_samples()
    samples[-1]["streams"][0]["dropped_frames"] = 0.5
    agg = aggregate_samples(samples, expected_n=2, strict=True)
    assert not judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)["pass"]


@pytest.mark.parametrize("delta", [0, 60, 539, 540])
def test_native_throughput_gate_checks_each_stream(delta):
    samples = _two_shard_capacity_samples()
    samples[-1]["streams"][0]["processed_frames"] = 10 + delta
    agg = aggregate_samples(samples, expected_n=2, strict=True)
    verdict = judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)
    assert verdict["pass"] is (delta >= 540)
    if delta < 540:
        assert any("cam-01" in reason and "throughput" in reason
                   for reason in verdict["reasons"])


@pytest.mark.parametrize("target", [float("nan"), float("inf"), 0, -1, None, True])
def test_judge_rejects_invalid_target(target):
    verdict = judge(_agg(1, 100), target_fps=target,
                    platform="cpu", rga_fail_count=None)
    assert not verdict["pass"]
    assert "target_fps" in verdict["reasons"][0]


@pytest.mark.parametrize("target", ["nan", "inf", "0", "-1"])
def test_cli_rejects_invalid_target_before_loading_samples(target, capsys):
    assert main(["--platform", "cpu", "--target-fps", target,
                 "--sample", "/missing-file.json"]) == 2
    assert "--target-fps" in capsys.readouterr().err


@pytest.mark.parametrize("kind", ["sample", "streams", "stream", "shards", "shard"])
@pytest.mark.parametrize("value", [None, 7, "bad", []])
def test_invalid_container_is_structured_fail_and_cli_records_raw(kind, value, tmp_path):
    samples = _two_shard_capacity_samples()
    if kind == "sample":
        samples[0] = value
    elif kind in ("streams", "shards"):
        samples[0][kind] = value
    else:
        samples[0][kind + "s"][0] = value
    sample_path = tmp_path / "sample.json"
    sample_path.write_text(json.dumps(samples))
    out = tmp_path / "out.json"
    assert main(["--platform", "cpu", "--n", "2", "--fail-on-rejection",
                 "--sample", str(sample_path), "--out", str(out)]) == 1
    conclusion = json.loads(out.read_text())
    assert not conclusion["rows"][0]["pass"]
    assert conclusion["rows"][0]["integrity_reasons"]
    assert conclusion["raw_samples"] == samples


class _FakeHTTPResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        if isinstance(self.payload, BaseException):
            raise self.payload
        return self.payload


def test_live_journal_flushes_sample_then_http503_failure(monkeypatch, tmp_path):
    import urllib.error
    import evaluation.stress_slots as stress_slots

    sample = _capacity_sample(0)
    responses = [_FakeHTTPResponse(json.dumps(sample).encode()),
                 urllib.error.HTTPError("http://127.0.0.1:1/healthz", 503,
                                        "busy", {}, None)]
    monkeypatch.setattr(stress_slots.urllib.request, "urlopen",
                        lambda *args, **kwargs: (
                            (_ for _ in ()).throw(responses.pop(0))
                            if isinstance(responses[0], BaseException)
                            else responses.pop(0)))
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    monkeypatch.setattr(stress_slots.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(stress_slots.time, "sleep", lambda _: None)
    monkeypatch.setattr(stress_slots.time, "time", lambda: 1700000000.0)
    raw = tmp_path / "run.raw.jsonl"
    with pytest.raises(stress_slots.PollHealthzFailure) as raised:
        poll_healthz("http://127.0.0.1:1/healthz", 1.0, raw_path=raw)
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    assert records[0]["schema"] == "vb.healthz/1"
    assert records[0]["request_started_monotonic_ms"] <= records[0]["request_ended_monotonic_ms"]
    assert records[0]["request_started_wall_ms"] <= records[0]["request_ended_wall_ms"]
    assert records[1]["record_kind"] == "failure"
    assert raised.value.samples and raised.value.events[0]["error_type"] == "HTTPError"
    assert raised.value.events[0]["http_status"] == 503
    assert raised.value.events[0]["request_started_wall_ms"] <= raised.value.events[0]["request_ended_wall_ms"]


@pytest.mark.parametrize("status", [401, 503])
def test_live_http_status_is_persisted_without_url_or_exception_text(
        monkeypatch, tmp_path, status):
    import urllib.error
    import evaluation.stress_slots as stress_slots

    responses = [urllib.error.HTTPError(
        "http://user:secret@127.0.0.1/healthz", status, "private details", {}, None)]
    monkeypatch.setattr(stress_slots.urllib.request, "urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(responses.pop(0)))
    clock = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr(stress_slots.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(stress_slots.time, "time", lambda: 1700000000.0)
    raw = tmp_path / f"status-{status}.raw.jsonl"
    with pytest.raises(stress_slots.PollHealthzFailure) as raised:
        poll_healthz("http://user:secret@127.0.0.1/healthz", 0, raw_path=raw)
    event = raised.value.events[0]
    assert event["http_status"] == status
    assert "127.0.0.1" not in json.dumps(event)
    assert "secret" not in json.dumps(event)
    assert "private details" not in json.dumps(event)


def test_journal_error_does_not_mask_primary_http_failure(monkeypatch, tmp_path):
    import urllib.error
    import evaluation.stress_slots as stress_slots

    monkeypatch.setattr(stress_slots.urllib.request, "urlopen",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            urllib.error.HTTPError("http://127.0.0.1/healthz", 503,
                                                   "details", {}, None)))
    monkeypatch.setattr(stress_slots._RawJournal, "append",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")))
    clock = iter([0.0, 0.1, 0.2])
    monkeypatch.setattr(stress_slots.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(stress_slots.time, "time", lambda: 1700000000.0)
    raw = tmp_path / "io.raw.jsonl"
    with pytest.raises(stress_slots.PollHealthzFailure) as raised:
        poll_healthz("http://127.0.0.1/healthz", 0, raw_path=raw)
    assert raised.value.events[0]["error_type"] == "HTTPError"
    assert raised.value.events[0]["http_status"] == 503
    assert raised.value.events[1]["record_kind"] == "journal_error"
    assert raised.value.events[1]["error_type"] == "OSError"


def test_cli_journal_write_failure_keeps_received_sample_and_returns_one(
        monkeypatch, tmp_path, capsys):
    import evaluation.stress_slots as stress_slots

    sample = _capacity_sample(0)
    monkeypatch.setattr(stress_slots.urllib.request, "urlopen",
                        lambda *args, **kwargs: _FakeHTTPResponse(
                            json.dumps(sample).encode()))
    monkeypatch.setattr(stress_slots._RawJournal, "append",
                        lambda *args, **kwargs: (_ for _ in ()).throw(
                            OSError("fsync failed")))
    monkeypatch.setattr(stress_slots.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(stress_slots.time, "time", lambda: 1700000000.0)
    out = tmp_path / "journal-fail.json"
    rc = main(["--platform", "cpu", "--healthz",
               "http://127.0.0.1/healthz", "--window-s", "0",
               "--out", str(out)])
    assert rc == 1
    assert "Traceback" not in capsys.readouterr().err
    conclusion = json.loads(out.read_text())
    assert conclusion["raw_samples"][0]["schema"] == "vb.healthz/1"
    assert conclusion["raw_failure_events"][0]["record_kind"] == "journal_error"


@pytest.mark.parametrize("error", [TimeoutError("timed out"), ValueError("bad json")])
def test_live_journal_flushes_prior_sample_on_timeout_or_malformed_json(
        monkeypatch, tmp_path, error):
    import evaluation.stress_slots as stress_slots

    sample = _capacity_sample(0)
    responses = [_FakeHTTPResponse(json.dumps(sample).encode()),
                 _FakeHTTPResponse(error if isinstance(error, BaseException)
                                   else str(error).encode())]
    monkeypatch.setattr(stress_slots.urllib.request, "urlopen",
                        lambda *args, **kwargs: responses.pop(0))
    clock = iter([0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7])
    monkeypatch.setattr(stress_slots.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(stress_slots.time, "sleep", lambda _: None)
    monkeypatch.setattr(stress_slots.time, "time", lambda: 1700000000.0)
    raw = tmp_path / "run.raw.jsonl"
    with pytest.raises(stress_slots.PollHealthzFailure):
        poll_healthz("http://127.0.0.1:1/healthz", 1.0, raw_path=raw)
    records = [json.loads(line) for line in raw.read_text().splitlines()]
    assert len(records) == 2
    assert records[0]["streams"][0]["stream_id"] == "cam-01"
    assert records[1]["record_kind"] == "failure"


def test_live_out_refuses_existing_raw_journal(monkeypatch, tmp_path, capsys):
    import evaluation.stress_slots as stress_slots

    out = tmp_path / "run.json"
    raw = tmp_path / "run.raw.jsonl"
    raw.write_text('{"record_kind":"sample"}\n')
    monkeypatch.setattr(stress_slots.urllib.request, "urlopen",
                        lambda *args, **kwargs: pytest.fail("must not poll"))
    assert main(["--platform", "cpu", "--healthz", "http://127.0.0.1:1/healthz",
                 "--window-s", "0", "--out", str(out)]) == 2
    assert "raw journal already exists" in capsys.readouterr().err


def test_offline_raw_summary_fields_remain_backward_compatible(tmp_path):
    out = tmp_path / "offline.json"
    assert main(["--platform", "cpu", "--sample",
                 str(FIXTURES / "slots_n2_ok.json"), "--out", str(out)]) == 0
    conclusion = json.loads(out.read_text())
    assert conclusion["raw_samples"]
    assert conclusion["raw_jsonl"]
    assert conclusion["raw_jsonl_path"] is None
    assert conclusion["raw_failure_events"] == []


def _window_samples(deltas, *, extra_tail=False):
    """Six boundary samples make five real 120 s windows."""
    times = [0, 120, 240, 360, 480, 600]
    if extra_tail:
        times.append(650)
    counts = [10]
    for delta in deltas:
        counts.append(counts[-1] + delta)
    if extra_tail:
        counts.append(counts[-1] + 50)
    return [_capacity_sample(t * 1000, processed=(c, c + 2))
            for t, c in zip(times, counts)]


def test_opt_in_capacity_windows_use_native_delta_and_ignore_health_zeroes():
    samples = _window_samples([120] * 5)
    for sample in samples:
        for stream in sample["streams"]:
            stream["fps"] = 0.0
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    verdict = judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)
    assert verdict["pass"]
    assert len(agg["streams"]["cam-01"]["fps_windows"]) == 5
    assert all(w["elapsed_s"] == pytest.approx(120)
               for w in agg["streams"]["cam-01"]["fps_windows"])


@pytest.mark.parametrize("delta,expected", [(107, False), (108, True)])
def test_capacity_window_boundary_is_exact_without_epsilon(delta, expected):
    samples = _window_samples([delta, 120, 120, 120, 120])
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    verdict = judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)
    assert verdict["pass"] is expected


def test_low_complete_window_cannot_be_hidden_by_high_overall_mean():
    samples = _window_samples([60, 180, 180, 180, 180])
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    assert agg["streams"]["cam-01"]["throughput_fps"] == pytest.approx(1.3)
    verdict = judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)
    assert not verdict["pass"]
    assert any("FPS window 1" in reason for reason in verdict["reasons"])


def test_capacity_tail_is_merged_and_recorded_without_dropping_coverage():
    agg = aggregate_samples(_window_samples([120] * 5, extra_tail=True),
                            expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    stream = agg["streams"]["cam-01"]
    assert stream["tail_diagnostic"]["elapsed_s"] == pytest.approx(50)
    assert stream["coverage_windows"][-1]["elapsed_s"] == pytest.approx(170)
    assert stream["coverage_windows"][-1]["merged_tail"] is True
    assert stream["coverage_windows"][-1]["tail"]["processed_delta"] == 50
    assert judge(agg, target_fps=1, platform="cpu",
                 rga_fail_count=None)["pass"]


def test_low_last_full_window_cannot_be_rescued_by_good_tail():
    agg = aggregate_samples(_window_samples([120, 120, 120, 120, 107],
                                             extra_tail=True),
                            expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    stream = agg["streams"]["cam-01"]
    assert stream["fps_windows"][-1]["processed_delta"] == 107
    assert stream["fps_windows"][-1]["elapsed_s"] == pytest.approx(120)
    assert stream["coverage_windows"][-1]["processed_delta"] == 157
    assert stream["coverage_windows"][-1]["elapsed_s"] == pytest.approx(170)
    verdict = judge(agg, target_fps=1, platform="cpu", rga_fail_count=None)
    assert not verdict["pass"]
    assert any("FPS window 5" in reason for reason in verdict["reasons"])


def test_cli_fps_window_requires_positive_value(capsys):
    assert main(["--platform", "cpu", "--fps-window-secs", "0",
                 "--sample", str(FIXTURES / "slots_n2_ok.json")]) == 2
    assert "fps-window-secs" in capsys.readouterr().err


def test_duplicate_native_timestamp_with_conflicting_counter_fails_capacity():
    samples = _window_samples([120] * 5)
    duplicate = json.loads(json.dumps(samples[1]))
    duplicate["_stress_sample_monotonic_ms"] = 121000
    duplicate["streams"][0]["processed_frames"] += 1
    samples.insert(2, duplicate)
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    assert any("duplicate native timestamp content conflict" in reason
               for reason in agg["integrity_reasons"])
    assert not judge(agg, target_fps=1, platform="cpu",
                     rga_fail_count=None)["pass"]


def test_capacity_window_uses_native_clock_when_wall_clock_jumps():
    samples = _window_samples([120] * 5)
    for sample in samples:
        sample["_stress_receipt_wall_ms"] += 3600 * 1000
        for shard in sample["shards"]:
            shard["runtime"]["stats_wall_ms"] += 3600 * 1000
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    assert agg["actual_elapsed_s"] == pytest.approx(600)
    assert judge(agg, target_fps=1, platform="cpu",
                 rga_fail_count=None)["pass"]


def test_capacity_diagnostics_report_plateau_sampling_gaps_and_health_zeros():
    samples = _window_samples([120] * 5)
    cached = json.loads(json.dumps(samples[2]))
    cached["_stress_sample_monotonic_ms"] = 241000
    samples.insert(3, cached)
    samples[1]["streams"][0]["fps"] = 0.0
    agg = aggregate_samples(samples, expected_n=2,
                            expected_ids=["cam-01", "cam-02"], strict=True,
                            fps_window_secs=120)
    stream = agg["streams"]["cam-01"]
    assert stream["health_zero_count"] == 1
    assert stream["health_zero_timestamps"] == [120000]
    assert agg["max_native_sampling_gap_s"] == pytest.approx(120)
    assert agg["max_poll_sampling_gap_s"] == pytest.approx(120)
    assert agg["poll_native_gap_timebase"] == "wall_ms"
    assert stream["native_cached_snapshot_count"] == 1
