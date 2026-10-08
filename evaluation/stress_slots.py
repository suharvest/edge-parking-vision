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
退出码：默认 0 = 正常出结论（含存在不通过的 N）；
``--fail-on-rejection`` 时存在 FAIL 返回 1；2 = 用法/输入错误。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
import urllib.request
from pathlib import Path

__all__ = ["aggregate_samples", "judge", "build_conclusion", "main",
           "FPS_RATIO", "QUEUE_DELAY_P95_MAX_MS", "RGA_NEEDLE", "RK_PLATFORMS"]

FPS_RATIO = 0.9                    # §6.3：fps ≥ 0.9 × target_fps
QUEUE_DELAY_P95_MAX_MS = 1000.0    # §6.3：queue_delay_ms_p95 ≤ 1000 ms
WALL_FRESHNESS_MAX_S = 10.0       # native wall clock vs poll receipt tolerance
RGA_NEEDLE = "RGA_BLIT fail"       # §6.3 / NRD-2 §S10
RK_PLATFORMS = ("rk3588", "rk3576")


class PollHealthzFailure(RuntimeError):
    """现场轮询失败，携带已收到的样本和结构化失败记录。"""

    def __init__(self, message: str, *, samples: list[dict], events: list[dict]):
        super().__init__(message)
        self.samples = samples
        self.events = events


class _RawJournal:
    """每条记录独占写入，避免 live 轮询异常丢失前面的窗口。"""

    def __init__(self, path: Path):
        if path.exists():
            raise FileExistsError(f"raw journal already exists: {path}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("x", encoding="utf-8")

    def append(self, record: dict) -> None:
        self._file.write(json.dumps(_json_safe(record), ensure_ascii=False,
                                    sort_keys=True, allow_nan=False) + "\n")
        self._file.flush()
        import os
        os.fsync(self._file.fileno())

    def close(self) -> None:
        self._file.close()


def _journal_error(exc: Exception, *, operation: str) -> dict:
    """可安全放入 summary 的 journal 错误，不暴露路径/异常文本。"""
    return {
        "record_kind": "journal_error",
        "operation": operation,
        "error_type": type(exc).__name__,
        "error": "raw journal persistence failed",
    }


# ------------------------------------------------------------------ 采样读取

def _extract_samples(doc) -> list[dict]:
    """healthz JSON 或 JSONL 文本 → 样本列表（每个样本含 streams[]）。"""
    doc = doc.strip()
    if not doc:
        return []
    try:
        obj = json.loads(doc)
        return obj if isinstance(obj, list) else [obj]
    except ValueError:
        pass
    return [json.loads(line) for line in doc.splitlines() if line.strip()]


def load_sample_file(path: str) -> list[dict]:
    return _extract_samples(Path(path).read_text(encoding="utf-8"))


def poll_healthz(url: str, window_s: float, interval_s: float = 1.0,
                 *, raw_path: str | Path | None = None) -> list[dict]:
    """现场模式：固定间隔轮询 /healthz，收集窗口内全部样本。"""
    samples = []
    events = []
    journal = _RawJournal(Path(raw_path)) if raw_path is not None else None
    deadline = time.monotonic() + window_s
    active_failure = None
    try:
        while True:
            request_started_mono = time.monotonic() * 1000
            request_started_wall = time.time() * 1000
            try:
                response_status = None
                with urllib.request.urlopen(url, timeout=5) as r:
                    response_status = getattr(r, "status", None)
                    sample = json.loads(r.read().decode("utf-8"))
                if not isinstance(sample, dict):
                    raise ValueError("healthz response is not a JSON object")
                request_ended_mono = time.monotonic() * 1000
                request_ended_wall = time.time() * 1000
                sample["request_started_monotonic_ms"] = request_started_mono
                sample["request_ended_monotonic_ms"] = request_ended_mono
                sample["request_started_wall_ms"] = request_started_wall
                sample["request_ended_wall_ms"] = request_ended_wall
                if isinstance(response_status, int) and not isinstance(response_status, bool):
                    sample["http_status"] = response_status
                sample["_stress_sample_monotonic_ms"] = time.monotonic() * 1000
                sample["_stress_receipt_wall_ms"] = time.time() * 1000
                samples.append(sample)
                if journal is not None:
                    try:
                        journal.append(sample)
                    except Exception as exc:
                        event = _journal_error(exc, operation="append_sample")
                        events.append(event)
                        raise PollHealthzFailure(
                            "stress_slots: raw journal write failed",
                            samples=samples, events=events) from None
            except Exception as exc:
                if isinstance(exc, PollHealthzFailure):
                    active_failure = exc
                    raise
                # Do not include URL or exception text: URLs may contain credentials.
                request_ended_mono = time.monotonic() * 1000
                request_ended_wall = time.time() * 1000
                event = {
                    "record_kind": "failure",
                    "poll_index": len(samples),
                    "error_type": type(exc).__name__,
                    "error": "healthz request/parse failed",
                    "http_status": (getattr(exc, "code", None)
                                    if isinstance(getattr(exc, "code", None), int)
                                    and not isinstance(getattr(exc, "code", None), bool)
                                    else None),
                    "request_started_monotonic_ms": request_started_mono,
                    "request_ended_monotonic_ms": request_ended_mono,
                    "request_started_wall_ms": request_started_wall,
                    "request_ended_wall_ms": request_ended_wall,
                }
                events.append(event)
                if journal is not None:
                    try:
                        journal.append(event)
                    except Exception as journal_exc:
                        events.append(_journal_error(journal_exc,
                                                     operation="append_failure"))
                active_failure = PollHealthzFailure(
                    "stress_slots: live healthz poll failed",
                    samples=samples, events=events)
                raise active_failure from None
            if time.monotonic() >= deadline:
                return samples
            time.sleep(min(interval_s, max(0.0, deadline - time.monotonic())))
    finally:
        if journal is not None:
            try:
                journal.close()
            except Exception as exc:
                event = _journal_error(exc, operation="close")
                if active_failure is not None:
                    active_failure.events.append(event)
                else:
                    raise PollHealthzFailure(
                        "stress_slots: raw journal close failed",
                        samples=samples, events=events + [event]) from None


# ------------------------------------------------------------------ 聚合与判定

def _valid_num(value, *, integer=False, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if not math.isfinite(value) or (positive and value <= 0):
        return False
    return (not integer or isinstance(value, int)) and value >= 0


def _json_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return "<nonfinite>"
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _runtime_metadata(sample: dict) -> tuple[dict[int, dict], list[str]]:
    runtimes: dict[int, dict] = {}
    errors: list[str] = []
    shards = sample.get("shards")
    if not isinstance(shards, list):
        return {}, ["shards is not an array"]
    for shard in shards:
        if not isinstance(shard, dict):
            errors.append("shard is not an object")
            continue
        index = shard.get("index")
        if not _valid_num(index, integer=True):
            errors.append("invalid shard index")
            continue
        if index in runtimes:
            errors.append(f"duplicate shard index {index}")
        runtime = shard.get("runtime") or {}
        if not isinstance(runtime, dict):
            errors.append("runtime metadata is not an object")
            continue
        runtimes[index] = runtime
        required = ("pid", "stats_pid", "effective_contexts",
                    "stats_monotonic_ms", "stats_wall_ms", "cpu_s", "rss_kb",
                    "restarts")
        for key in required:
            if key not in runtime:
                errors.append(f"missing runtime.{key}")
        for key in ("pid", "stats_pid", "effective_contexts", "restarts"):
            if key in runtime and not _valid_num(runtime[key], integer=True,
                                                 positive=key != "restarts"):
                errors.append(f"invalid runtime.{key}")
        for key in ("stats_monotonic_ms", "stats_wall_ms", "cpu_s", "rss_kb"):
            if key in runtime and not _valid_num(runtime[key]):
                errors.append(f"invalid runtime.{key}")
        if runtime.get("pid") != runtime.get("stats_pid"):
            errors.append("runtime pid differs from stats_pid")
        if runtime.get("restarts") != 0:
            errors.append("runtime restart detected")
        if runtime.get("alive") is not True or runtime.get("failed") is not False:
            errors.append("runtime is not healthy")
    if not runtimes:
        errors.append("missing health.shards[].runtime")
    return runtimes, errors


def aggregate_samples(samples: list[dict], *, expected_n: int | None = None,
                       expected_ids: list[str] | None = None,
                       strict: bool = False,
                       fps_window_secs: float | None = None) -> dict:
    """一个 N 的窗口样本 → 每路聚合 + 总体指标。

    返回 ``{"n": int, "streams": {sid: {...}}, "fps_min": float|None,
    "queue_delay_ms_p95_max": float|None, "dropped_frames_delta": int}``。
    缺字段的样本跳过该字段（getattr 式防御，基座字段见 BASE-1 §6.8）。
    """
    per: dict[str, dict] = {}
    integrity: list[str] = []
    expected = set(expected_ids or [])
    if fps_window_secs is not None and not expected:
        integrity.append("capacity mode requires explicit expected stream IDs")
    sample_mono: list[float] = []
    native_series: dict[str, list[tuple[int, dict, int]]] = {}
    shard_series: dict[int, list[dict]] = {}
    poll_native_wall_gaps: list[float] = []
    for sample_i, smp in enumerate(samples):
        if not isinstance(smp, dict):
            integrity.append(f"sample {sample_i}: sample is not an object")
            continue
        if strict and smp.get("status") != "ok":
            integrity.append(f"sample {sample_i}: health status is not ok")
        if not strict and "shards" in smp:
            shards = smp["shards"]
            if not isinstance(shards, list):
                integrity.append(f"sample {sample_i}: shards is not an array")
            elif any(not isinstance(shard, dict) for shard in shards):
                integrity.append(f"sample {sample_i}: shard is not an object")
        streams = smp.get("streams")
        if not isinstance(streams, list):
            integrity.append(f"sample {sample_i}: streams is not an array")
            streams = []
        if any(not isinstance(st, dict) for st in streams):
            integrity.append(f"sample {sample_i}: stream is not an object")
            streams = [st for st in streams if isinstance(st, dict)]
        ids = [str(st.get("stream_id") or st.get("id") or "")
               for st in streams]
        if len(ids) != len(set(ids)):
            integrity.append(f"sample {sample_i}: duplicate stream_id")
        if expected_n is not None and len(ids) != expected_n:
            integrity.append(f"sample {sample_i}: stream count {len(ids)} != expected {expected_n}")
        if expected and set(ids) != expected:
            integrity.append(f"sample {sample_i}: stream IDs do not match expected set")
        runtimes, runtime_errors = _runtime_metadata(smp) if strict else ({}, [])
        integrity.extend(f"sample {sample_i}: {e}" for e in runtime_errors)
        if strict:
            for shard_index, runtime in runtimes.items():
                shard_series.setdefault(shard_index, []).append(runtime)
            receipt = smp.get("_stress_sample_monotonic_ms")
            wall = smp.get("_stress_receipt_wall_ms")
            if not _valid_num(receipt) or not _valid_num(wall):
                integrity.append(f"sample {sample_i}: missing or invalid poll receipt")
            else:
                sample_mono.append(float(receipt))
            if _valid_num(wall) and runtimes:
                for runtime in runtimes.values():
                    if not _valid_num(runtime.get("stats_wall_ms")):
                        continue
                    age_s = (float(wall) - float(runtime["stats_wall_ms"])) / 1000.0
                    poll_native_wall_gaps.append(abs(age_s))
                    if abs(age_s) > WALL_FRESHNESS_MAX_S:
                        integrity.append(f"sample {sample_i}: stale/future stats_wall_ms ({age_s:.3f}s)")
        seen_sample = set()
        for st in streams:
            sid = str(st.get("stream_id") or st.get("id") or "")
            if not sid:
                integrity.append(f"sample {sample_i}: missing stream_id")
                continue
            seen_sample.add(sid)
            agg = per.setdefault(sid, {"fps_min": None,
                                       "queue_delay_ms_p95_max": None,
                                       "inference_ms_p50_min": None,
                                       "inference_ms_p50_max": None,
                                       "inference_ms_p95_min": None,
                                       "inference_ms_p95_max": None,
                                       "dropped_first": None, "dropped_last": None,
                                       "processed_first": None, "processed_last": None,
                                       "states": [], "sample_count": 0,
                                       "shard_indices": set(), "native_segments": [],
                                       "health_zero_timestamps": []})
            agg["sample_count"] += 1
            state = st.get("state")
            agg["states"].append(state)
            shard_index = st.get("shard")
            if strict and (not _valid_num(shard_index, integer=True)
                           or shard_index not in runtimes):
                integrity.append(f"sample {sample_i} stream {sid}: unknown shard mapping")
            elif strict:
                agg["shard_indices"].add(shard_index)
                native_series.setdefault(sid, []).append(
                    (shard_index, runtimes[shard_index], sample_i,
                     st.get("processed_frames")
                     if _valid_num(st.get("processed_frames"), integer=True)
                     else None,
                     st.get("dropped_frames")
                     if _valid_num(st.get("dropped_frames"), integer=True)
                     else None))
            if strict and state != "running":
                integrity.append(f"sample {sample_i} stream {sid}: state is {state!r}")
            fps = st.get("fps")
            if fps is not None and _valid_num(fps):
                fps = float(fps)
                if fps == 0.0:
                    zero_ts = smp.get("_stress_sample_monotonic_ms")
                    if _valid_num(zero_ts):
                        agg["health_zero_timestamps"].append(zero_ts)
                agg["fps_min"] = fps if agg["fps_min"] is None \
                    else min(agg["fps_min"], fps)
            q = st.get("queue_delay_ms_p95")
            if q is not None and _valid_num(q):
                q = float(q)
                agg["queue_delay_ms_p95_max"] = q \
                    if agg["queue_delay_ms_p95_max"] is None \
                    else max(agg["queue_delay_ms_p95_max"], q)
            for key in ("inference_ms_p50", "inference_ms_p95"):
                value = st.get(key)
                if value is not None and _valid_num(value):
                    value = float(value)
                    agg[f"{key}_min"] = value if agg[f"{key}_min"] is None else min(agg[f"{key}_min"], value)
                    agg[f"{key}_max"] = value if agg[f"{key}_max"] is None else max(agg[f"{key}_max"], value)
            d = st.get("dropped_frames")
            if d is not None and _valid_num(d, integer=True):
                d = int(d)
                if agg["dropped_last"] is not None and d < agg["dropped_last"]:
                    integrity.append(f"stream {sid}: dropped_frames decreased")
                if agg["dropped_first"] is None:
                    agg["dropped_first"] = d
                agg["dropped_last"] = d
            processed = st.get("processed_frames")
            if processed is not None and _valid_num(processed, integer=True):
                processed = int(processed)
                if agg["processed_last"] is not None and processed < agg["processed_last"]:
                    integrity.append(f"stream {sid}: processed_frames decreased")
                if agg["processed_first"] is None:
                    agg["processed_first"] = processed
                agg["processed_last"] = processed
            elif strict:
                integrity.append(f"sample {sample_i} stream {sid}: missing processed_frames")
            if strict:
                for key in ("fps", "inference_ms_p50", "inference_ms_p95",
                            "queue_delay_ms_p95", "dropped_frames",
                            "processed_frames"):
                    if key not in st or not _valid_num(
                            st.get(key), integer=key in ("dropped_frames", "processed_frames")):
                        integrity.append(f"sample {sample_i} stream {sid}: missing {key}")
        if strict and expected and seen_sample != expected:
            integrity.append(f"sample {sample_i}: missing or unexpected stream IDs")
    fps_vals = [a["fps_min"] for a in per.values() if a["fps_min"] is not None]
    q_vals = [a["queue_delay_ms_p95_max"] for a in per.values()
              if a["queue_delay_ms_p95_max"] is not None]
    dropped = sum(max(0, a["dropped_last"] - a["dropped_first"])
                  for a in per.values()
                  if a["dropped_first"] is not None and a["dropped_last"] is not None)
    if strict and len(sample_mono) < 2:
        integrity.append("fewer than two independent poll receipts")
    if strict and sample_mono and any(b <= a for a, b in zip(sample_mono, sample_mono[1:])):
        integrity.append("poll monotonic receipts are not strictly increasing")
    interval_s = ((sample_mono[-1] - sample_mono[0]) / 1000.0
                  if len(sample_mono) >= 2 else None)
    poll_gaps = [(b - a) / 1000.0 for a, b in zip(sample_mono, sample_mono[1:])]
    if strict and (interval_s is None or interval_s < 600.0):
        integrity.append(f"actual poll elapsed {interval_s} < required 600 s")
    for agg in per.values():
        first, last = agg["processed_first"], agg["processed_last"]
        agg["processed_delta"] = (last - first if first is not None and last is not None else None)
        agg["interval_s"] = interval_s
        agg["throughput_fps"] = (agg["processed_delta"] / interval_s
                                  if agg["processed_delta"] is not None and interval_s and interval_s > 0
                                  else None)
    window_errors: list[str] = []
    for sid, series in native_series.items():
        if len({item[0] for item in series}) != 1:
            integrity.append(f"stream {sid}: multiple shard owners")
            continue
        series.sort(key=lambda item: item[2])
        raw_series = list(series)
        # A repeated native (pid, restart generation, timestamp) is a cached
        # snapshot, not another observation.  Conflicting counters at that
        # identity are an integrity failure rather than an interpolation hint.
        fresh_series = []
        seen_native: dict[tuple, tuple] = {}
        duplicate_run = 0
        duplicate_longest_run = 0
        for item in series:
            rt, counter = item[1], item[3]
            key = (item[0], rt.get("pid"), rt.get("restarts"),
                   rt.get("stats_monotonic_ms"))
            if key in seen_native:
                duplicate_run += 1
                duplicate_longest_run = max(duplicate_longest_run, duplicate_run)
                prior = seen_native[key]
                if prior[0] != counter or prior[1] != item[4]:
                    integrity.append(f"stream {sid}: duplicate native timestamp content conflict")
                continue
            duplicate_run = 0
            seen_native[key] = (counter, item[4])
            fresh_series.append(item)
        series = fresh_series
        per[sid]["native_fresh_snapshot_count"] = len(series)
        per[sid]["native_cached_snapshot_count"] = len(native_series[sid]) - len(series)
        per[sid]["native_cached_snapshot_longest_run"] = duplicate_longest_run
        segments = per[sid]["native_segments"]
        for prev, cur in zip(raw_series, raw_series[1:]):
            p_rt, c_rt = prev[1], cur[1]
            if (p_rt.get("pid") != c_rt.get("pid") or
                    p_rt.get("restarts") != c_rt.get("restarts")):
                integrity.append(f"stream {sid}: native generation changed")
            if p_rt.get("effective_contexts") != c_rt.get("effective_contexts"):
                integrity.append(f"stream {sid}: effective_contexts changed")
            if (_valid_num(c_rt.get("stats_monotonic_ms")) and
                    _valid_num(p_rt.get("stats_monotonic_ms")) and
                    c_rt["stats_monotonic_ms"] < p_rt["stats_monotonic_ms"]):
                integrity.append(f"stream {sid}: native stats clock not increasing")
            if (_valid_num(c_rt.get("stats_monotonic_ms")) and
                    _valid_num(p_rt.get("stats_monotonic_ms")) and
                    c_rt.get("stats_monotonic_ms") == p_rt.get("stats_monotonic_ms") and
                    cur[3] != prev[3]):
                integrity.append(f"stream {sid}: counter advanced on cached native clock")
            if (_valid_num(c_rt.get("cpu_s")) and
                    _valid_num(p_rt.get("cpu_s")) and
                    c_rt["cpu_s"] < p_rt["cpu_s"]):
                integrity.append(f"stream {sid}: native cpu_s decreased")
            clock_delta = (c_rt.get("stats_monotonic_ms", 0) -
                           p_rt.get("stats_monotonic_ms", 0)
                           if _valid_num(c_rt.get("stats_monotonic_ms")) and
                           _valid_num(p_rt.get("stats_monotonic_ms")) else None)
            cpu_delta = (c_rt.get("cpu_s", 0) - p_rt.get("cpu_s", 0)
                         if _valid_num(c_rt.get("cpu_s")) and
                         _valid_num(p_rt.get("cpu_s")) else None)
            segments.append({"processed_delta": cur[3] - prev[3]
                             if cur[3] is not None and prev[3] is not None else None,
                             "native_clock_delta_ms": clock_delta,
                             "pid": c_rt.get("pid"),
                             "restart_generation": c_rt.get("restarts"),
                             "cpu_s_delta": cpu_delta,
                             "rss_kb_start": p_rt.get("rss_kb"),
                             "rss_kb_end": c_rt.get("rss_kb")})
            segment = segments[-1]
            delta_s = ((segment["native_clock_delta_ms"] or 0) / 1000.0)
            segment["cpu_percent"] = (segment["cpu_s_delta"] / delta_s * 100.0
                                       if segment["cpu_s_delta"] is not None and delta_s > 0
                                       else None)
        first_clock = series[0][1].get("stats_monotonic_ms")
        last_clock = series[-1][1].get("stats_monotonic_ms")
        native_elapsed_s = ((last_clock - first_clock) / 1000.0
                            if _valid_num(first_clock) and _valid_num(last_clock)
                            else None)
        agg = per[sid]
        agg["poll_interval_s"] = interval_s
        agg["interval_s"] = native_elapsed_s
        agg["throughput_fps"] = (agg["processed_delta"] / native_elapsed_s
                                 if agg["processed_delta"] is not None and
                                 native_elapsed_s is not None and native_elapsed_s > 0
                                 else None)
        if native_elapsed_s is None or native_elapsed_s < 600.0:
            integrity.append(f"stream {sid}: native elapsed {native_elapsed_s} < required 600 s")
        native_points = [(float(item[1]["stats_monotonic_ms"]) / 1000.0,
                          item[3]) for item in series
                         if _valid_num(item[1].get("stats_monotonic_ms")) and
                         item[3] is not None]
        native_gaps = [b[0] - a[0] for a, b in zip(native_points, native_points[1:])]
        agg["max_native_sampling_gap_s"] = max(native_gaps) if native_gaps else None
        plateaus = []
        plateau_start = None
        for (point_t, count), next_point in zip(native_points, native_points[1:] + [(None, None)]):
            if next_point[0] is not None and next_point[1] == count:
                if plateau_start is None:
                    plateau_start = (point_t, count)
                continue
            if plateau_start is not None:
                end_t = point_t
                plateaus.append({"start_native_s": plateau_start[0],
                                 "end_native_s": end_t,
                                 "duration_s": max(0.0, end_t - plateau_start[0]),
                                 "processed_frames": plateau_start[1]})
                plateau_start = None
        agg["native_counter_plateaus"] = plateaus
        agg["health_zero_count"] = len(agg["health_zero_timestamps"])
        if fps_window_secs is not None and _valid_num(fps_window_secs, positive=True):
            points = [(float(item[1]["stats_monotonic_ms"]) / 1000.0,
                       item[3], item[1].get("pid"), item[0], item[2])
                      for item in series
                      if _valid_num(item[1].get("stats_monotonic_ms")) and
                      item[3] is not None]
            full_windows = []
            start_i = 0
            while start_i < len(points):
                start_t = points[start_i][0]
                end_i = next((i for i in range(start_i + 1, len(points))
                              if points[i][0] >= start_t + fps_window_secs), None)
                if end_i is None:
                    break
                elapsed = points[end_i][0] - start_t
                full_windows.append({
                    "start_native_s": start_t, "end_native_s": points[end_i][0],
                    "elapsed_s": elapsed, "processed_delta": points[end_i][1] - points[start_i][1],
                    "throughput_fps": ((points[end_i][1] - points[start_i][1]) / elapsed
                                       if elapsed > 0 else None),
                    "start_sample_index": points[start_i][4],
                    "end_sample_index": points[end_i][4],
                })
                start_i = end_i
            tail = None
            if full_windows and start_i < len(points):
                last_end = points[-1][0]
                tail = {"start_native_s": points[start_i][0],
                        "end_native_s": last_end,
                        "elapsed_s": max(0.0, last_end - points[start_i][0]),
                        "processed_delta": points[-1][1] - points[start_i][1]}
            coverage_windows = [dict(window) for window in full_windows]
            if tail is not None:
                coverage_windows[-1]["end_native_s"] = tail["end_native_s"]
                coverage_windows[-1]["elapsed_s"] = (
                    tail["end_native_s"] - coverage_windows[-1]["start_native_s"])
                coverage_windows[-1]["processed_delta"] += tail["processed_delta"]
                coverage_windows[-1]["throughput_fps"] = (
                    coverage_windows[-1]["processed_delta"] /
                    coverage_windows[-1]["elapsed_s"]
                    if coverage_windows[-1]["elapsed_s"] > 0 else None)
                coverage_windows[-1]["merged_tail"] = True
                coverage_windows[-1]["tail"] = tail
            for w in full_windows:
                w.setdefault("merged_tail", False)
            if len(full_windows) < 3:
                window_errors.append(f"stream {sid}: fewer than 3 complete {fps_window_secs:g}s windows")
            if not full_windows:
                window_errors.append(f"stream {sid}: no complete FPS windows")
            per[sid]["fps_windows"] = full_windows
            per[sid]["coverage_windows"] = coverage_windows
            per[sid]["tail_diagnostic"] = tail
    for shard_index, series in shard_series.items():
        for prev, cur in zip(series, series[1:]):
            if (prev.get("pid") != cur.get("pid") or
                    prev.get("restarts") != cur.get("restarts")):
                integrity.append(f"shard {shard_index}: native generation changed")
    for agg in per.values():
        agg["shard_indices"] = sorted(agg["shard_indices"])
    return {
        "n": len(per),
        "strict": strict,
        "streams": per,
        "fps_min": min(fps_vals) if fps_vals else None,
        "queue_delay_ms_p95_max": max(q_vals) if q_vals else None,
        "dropped_frames_delta": dropped,
        "integrity_reasons": integrity,
        "poll_monotonic_start_ms": sample_mono[0] if sample_mono else None,
        "poll_monotonic_end_ms": sample_mono[-1] if sample_mono else None,
        "actual_elapsed_s": interval_s,
        "coverage_sufficient": bool(interval_s is not None and interval_s >= 600.0),
        "max_native_sampling_gap_s": max(
            (a["max_native_sampling_gap_s"] for a in per.values()
             if a.get("max_native_sampling_gap_s") is not None), default=None),
        "max_poll_sampling_gap_s": max(poll_gaps) if poll_gaps else None,
        "max_poll_native_wall_gap_s": (max(poll_native_wall_gaps)
                                       if poll_native_wall_gaps else None),
        "poll_native_gap_timebase": "wall_ms" if poll_native_wall_gaps else "unavailable",
        "fps_window_secs": fps_window_secs,
        "measurement_version": ("nrd4-capacity-native-window-v1"
                                 if fps_window_secs is not None else None),
        "timebase": "native stats_monotonic_ms" if fps_window_secs is not None else None,
        "aggregation": ("per-stream fresh native processed_frames delta / measured native elapsed; "
                         "full windows plus final tail-merged coverage window"
                         if fps_window_secs is not None else None),
        "native_revision": next((s.get("native_revision") for s in samples
                                  if isinstance(s, dict) and s.get("native_revision") is not None), None),
        "collector_sha": next((s.get("collector_sha") for s in samples
                               if isinstance(s, dict) and s.get("collector_sha") is not None), None),
        "window_errors": window_errors,
    }


def count_rga_fails(log_text: str) -> int:
    """§6.3 第三行：``grep -c "RGA_BLIT fail"`` 等价。"""
    return sum(1 for line in log_text.splitlines() if RGA_NEEDLE in line)


def judge(agg: dict, *, target_fps: float, platform: str,
          rga_fail_count: int | None) -> dict:
    """§6.3 判据 → 行结果（pass + 可读原因）。"""
    reasons = []
    if not _valid_num(target_fps, positive=True):
        return {"pass": False, "reasons": ["target_fps must be finite and positive"]}
    fps_line = FPS_RATIO * target_fps
    if agg.get("strict"):
        for sid, stream in agg["streams"].items():
            throughput = stream.get("throughput_fps")
            if not _valid_num(throughput):
                reasons.append(f"stream {sid}: invalid native throughput_fps")
            elif throughput < fps_line:
                reasons.append(f"stream {sid}: native throughput_fps {throughput:.3f} < {fps_line:.3f}")
    capacity_mode = agg.get("fps_window_secs") is not None
    if not capacity_mode:
        if agg["fps_min"] is None:
            reasons.append("healthz 无 fps 字段，无法判定")
        elif agg["fps_min"] < fps_line:
            reasons.append(f"fps_min {agg['fps_min']:.3f} < "
                           f"{FPS_RATIO}×target_fps={fps_line:.3f}")
    else:
        for sid, stream in agg["streams"].items():
            windows = stream.get("fps_windows", [])
            if len(windows) < 3:
                reasons.append(f"stream {sid}: fewer than 3 complete FPS windows")
            for index, window in enumerate(windows):
                rate = window.get("throughput_fps")
                if rate is None or rate < fps_line:
                    reasons.append(f"stream {sid}: FPS window {index + 1} "
                                   f"{rate if rate is not None else 'invalid'} < {fps_line:.3f}")
            for index, window in enumerate(stream.get("coverage_windows", [])):
                rate = window.get("throughput_fps")
                if rate is None or rate < fps_line:
                    reasons.append(f"stream {sid}: coverage FPS window {index + 1} "
                                   f"{rate if rate is not None else 'invalid'} < {fps_line:.3f}")
        reasons.extend(agg.get("window_errors", []))
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
    reasons.extend(agg.get("integrity_reasons", []))
    return {"pass": not reasons, "reasons": reasons}


def build_conclusion(rows: list[dict], *, platform: str,
                     target_fps: float, raw_samples: list[dict] | None = None,
                     raw_jsonl_path: str | None = None,
                     raw_failure_events: list[dict] | None = None) -> dict:
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
            "queue_delay_ms_p95": "每路最近256帧滑窗最大值，不是全时段分位数",
            "processed_frames": "原生处理完成计数，不是业务 hook/MQTT 完成数",
            "stats_wall_freshness_max_s": WALL_FRESHNESS_MAX_S,
            "fps_window_measurement": "opt-in only; native steady counter deltas over real boundaries",
        },
        "raw_samples": _json_safe(raw_samples or []),
        "raw_jsonl": "\n".join(json.dumps(sample, ensure_ascii=False,
                                             sort_keys=True)
                                for sample in _json_safe(raw_samples or [])),
        "raw_jsonl_path": raw_jsonl_path,
        "raw_failure_events": _json_safe(raw_failure_events or []),
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
    ap.add_argument("--expected-id", action="append", default=[],
                    help="期望的 stream_id，可重复；与 --n 一起严格核对")
    ap.add_argument("--window-s", type=float, default=60.0,
                    help="现场模式轮询窗口（默认 60 s）")
    ap.add_argument("--fps-window-secs", type=float, default=None,
                    help="显式启用 native 容量 FPS 长窗；必须为正值，建议 120 s")
    ap.add_argument("--out", help="结论 JSON 输出路径（缺省只打 stdout）")
    ap.add_argument("--fail-on-rejection", action="store_true",
                    help="存在 FAIL 时退出 1；默认保持旧的 rc=0")
    args = ap.parse_args(argv)
    if not _valid_num(args.target_fps, positive=True):
        print("stress_slots: --target-fps must be finite and positive", file=sys.stderr)
        return 2
    if args.fps_window_secs is not None and not _valid_num(
            args.fps_window_secs, positive=True):
        print("stress_slots: --fps-window-secs must be finite and positive",
              file=sys.stderr)
        return 2
    if args.fps_window_secs is not None and not args.expected_id:
        print("stress_slots: --fps-window-secs requires at least one --expected-id",
              file=sys.stderr)
        return 2

    logs = args.container_log
    if args.platform in RK_PLATFORMS and not logs:
        print(f"stress_slots: --platform {args.platform} 需要 "
              f"--container-log <file>（§6.3 RGA 判据）", file=sys.stderr)
        return 2

    raw_jsonl_path = None
    if args.healthz and args.out:
        out_path = Path(args.out)
        raw_jsonl_path = str(out_path.with_suffix(".raw.jsonl"))
        if out_path.exists() and out_path.stat().st_size > 0:
            print(f"stress_slots: --out already contains data: {out_path}",
                  file=sys.stderr)
            return 2
        if Path(raw_jsonl_path).exists():
            print(f"stress_slots: raw journal already exists: {raw_jsonl_path}",
                  file=sys.stderr)
            return 2

    runs: list[tuple[list[dict], int | None]] = []   # (samples, rga_count)
    poll_failure = None
    if args.healthz:
        try:
            samples = poll_healthz(args.healthz, args.window_s,
                                  raw_path=raw_jsonl_path)
        except PollHealthzFailure as exc:
            samples = exc.samples
            poll_failure = exc
        if not samples:
            if poll_failure is None:
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
    all_raw_samples: list[dict] = []
    for samples, rga in runs:
        all_raw_samples.extend(samples)
        expected_ids = args.expected_id or None
        if (args.fps_window_secs is None and args.n is not None and expected_ids is None and samples
                and isinstance(samples[0], dict)
                and isinstance(samples[0].get("streams"), list)):
            expected_ids = [str(st.get("stream_id") or st.get("id") or "")
                            for st in samples[0]["streams"] if isinstance(st, dict)]
        strict = (args.n is not None or bool(args.expected_id) or
                  args.fps_window_secs is not None)
        agg = aggregate_samples(samples, expected_n=args.n,
                                expected_ids=expected_ids, strict=strict,
                                fps_window_secs=args.fps_window_secs)
        verdict = judge(agg, target_fps=args.target_fps,
                        platform=args.platform, rga_fail_count=rga)
        if poll_failure is not None:
            verdict["pass"] = False
            verdict["reasons"] = ["live healthz poll failed"] + verdict["reasons"]
        rows.append({"n": args.n if args.n is not None else agg["n"],
                     "streams": agg["n"],
                     "stream_details": agg["streams"],
                     "fps_min": agg["fps_min"],
                     "queue_delay_ms_p95_max": agg["queue_delay_ms_p95_max"],
                     "dropped_frames_delta": agg["dropped_frames_delta"],
                     "actual_elapsed_s": agg["actual_elapsed_s"],
                     "coverage_sufficient": agg["coverage_sufficient"],
                     "max_native_sampling_gap_s": agg["max_native_sampling_gap_s"],
                     "max_poll_sampling_gap_s": agg["max_poll_sampling_gap_s"],
                     "max_poll_native_wall_gap_s": agg["max_poll_native_wall_gap_s"],
                     "poll_native_gap_timebase": agg["poll_native_gap_timebase"],
                     "poll_monotonic_start_ms": agg["poll_monotonic_start_ms"],
                     "poll_monotonic_end_ms": agg["poll_monotonic_end_ms"],
                     "integrity_reasons": agg["integrity_reasons"],
                     "fps_window_secs": agg["fps_window_secs"],
                     "measurement_version": agg["measurement_version"],
                     "timebase": agg["timebase"],
                     "aggregation": agg["aggregation"],
                     "native_revision": agg["native_revision"],
                     "collector_sha": agg["collector_sha"],
                     "window_errors": agg["window_errors"],
                     "rga_fail_count": rga, **verdict})
    rows.sort(key=lambda r: r["n"])
    conclusion = build_conclusion(rows, platform=args.platform,
                                  target_fps=args.target_fps,
                                  raw_samples=all_raw_samples,
                                  raw_jsonl_path=raw_jsonl_path,
                                  raw_failure_events=(poll_failure.events
                                                       if poll_failure else None))

    _print_table(rows)
    text = json.dumps(conclusion, ensure_ascii=False, indent=2)
    print(text)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    if poll_failure is not None:
        return 1
    return 1 if args.fail_on_rejection and any(not r["pass"] for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
