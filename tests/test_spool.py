"""spool 与 SpoolPublisher 测试（§6.4；§8.1 C2 验收行）。

- FakeMqtt：断开 → 入 spool → 重连 → 补发 replayed=true → 游标推进；
- spool 满：丢最旧 occupancy、不丢 crossing；
- kill -9 后游标与 seq 不回退（子进程）；
- -m broker：对真实 mosquitto 走 vision_base.mqtt.MqttClient 补发。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_parking.envelope import SeqAllocator
from core_parking.spool import Spool
from core_parking.transport import SpoolPublisher
from fake_mqtt import FakeMqtt

REPO = Path(__file__).resolve().parent.parent
CORE_PY = REPO / "core-py"

MQTT_OPTS = {"host": "127.0.0.1", "port": 1, "username": "", "password": "",
             "keepalive_s": 30}


def _ctx(tmp_path, stream_id="gate-a"):
    return SimpleNamespace(stream_id=stream_id,
                           state={"seq": SeqAllocator(tmp_path / stream_id)})


def _make_publisher(tmp_path, max_events=100):
    return SpoolPublisher(MQTT_OPTS, client_id="t-app", site_id="demo-lot",
                          spool_path=tmp_path / "spool.jsonl",
                          max_events=max_events, client_factory=FakeMqtt)


def _wait(cond, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ------------------------------------------------------------------ spool 本体

def test_spool_append_ack_persist(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", 10)
    a = spool.append("t/crossing", "crossing", {"seq": 0})
    b = spool.append("t/occupancy", "occupancy", {"seq": 1})
    assert spool.depth == 2
    assert [e.id for e in spool.pending()] == [a, b]
    spool.ack(a)
    assert [e.id for e in spool.pending()] == [b]
    # 重启：游标不回退
    spool2 = Spool(tmp_path / "s.jsonl", 10)
    assert [e.id for e in spool2.pending()] == [b]
    assert spool2.depth == 1
    spool2.ack(b)
    assert Spool(tmp_path / "s.jsonl", 10).depth == 0


def test_spool_full_drops_oldest_occupancy_keeps_crossing(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", 3)
    spool.append("t/occupancy", "occupancy", {"seq": 0})
    spool.append("t/crossing", "crossing", {"seq": 1})
    spool.append("t/occupancy", "occupancy", {"seq": 2})
    # 满：新 crossing 触发丢最旧 occupancy（seq 0）
    spool.append("t/crossing", "crossing", {"seq": 3})
    kinds = [(e.kind, e.payload["seq"]) for e in spool.pending()]
    assert kinds == [("crossing", 1), ("occupancy", 2), ("crossing", 3)]
    assert spool.overflow is True
    # 重启后状态一致（淘汰已落盘）
    assert [(e.kind, e.payload["seq"]) for e in Spool(tmp_path / "s.jsonl", 3).pending()] == kinds


def test_spool_full_no_droppable_never_loses_crossing(tmp_path):
    spool = Spool(tmp_path / "s.jsonl", 2)
    spool.append("t/crossing", "crossing", {"seq": 0})
    spool.append("t/crossing", "crossing", {"seq": 1})
    spool.append("t/crossing", "crossing", {"seq": 2})  # 无 occupancy 可丢：仍保留
    assert [e.payload["seq"] for e in spool.pending()] == [0, 1, 2]
    assert spool.overflow is True


def test_spool_cursor_no_regression_after_kill9(tmp_path):
    """子进程 append 3 条、ack 第 1 条后被 kill -9；重启游标不回退。"""
    code = (
        "from core_parking.spool import Spool\n"
        f"s = Spool({str(tmp_path / 's.jsonl')!r}, 10)\n"
        "ids = [s.append('t/crossing', 'crossing', {'seq': i}) for i in range(3)]\n"
        "s.ack(ids[0])\n"
        "print('ready', flush=True)\n"
        "import time; time.sleep(30)\n"
    )
    env = dict(os.environ, PYTHONPATH=str(CORE_PY))
    proc = subprocess.Popen([sys.executable, "-c", code], env=env,
                            stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "ready"
    proc.kill()
    proc.wait()
    spool = Spool(tmp_path / "s.jsonl", 10)
    assert [e.payload["seq"] for e in spool.pending()] == [1, 2]


# ------------------------------------------------------------ SpoolPublisher

def test_disconnect_spool_reconnect_replay_cursor_advances(tmp_path):
    pub = _make_publisher(tmp_path)
    client = pub._client
    pub.start()
    try:
        assert client.connected  # FakeMqtt connect() 即连上
        client.set_connected(False)  # 断网
        ctx = _ctx(tmp_path)
        for i in range(3):
            pub.publish("crossing", ctx, {"n": i}, ts=1000 + i)
        assert _wait(lambda: pub.spool.depth == 3)
        assert client.published == []  # 断开期间没有任何网络发送
        client.set_connected(True)   # 重连 → on_connect → drain
        assert _wait(lambda: len(client.published) == 3)
        assert _wait(lambda: pub.spool.depth == 0)  # 游标推进到清空
        topics = [t for t, _, qos, retain in client.published]
        assert topics == ["demo-lot/parking/gate-a/crossing"] * 3
        payloads = [json.loads(p) for _, p, _, _ in client.published]
        assert [p["replayed"] for p in payloads] == [True, True, True]
        assert [p["seq"] for p in payloads] == [0, 1, 2]  # 顺序保持
        assert all(qos == 1 and retain is False
                   for _, _, qos, retain in client.published)
        # 重启：游标不回退（spool 已空）
        assert Spool(tmp_path / "spool.jsonl", 100).depth == 0
    finally:
        pub.close()


def test_occupancy_snapshot_published_retained(tmp_path):
    pub = _make_publisher(tmp_path)
    pub.start()
    try:
        pub.publish("occupancy", _ctx(tmp_path),
                    {"occupancy": 1, "capacity": 120, "free": 119,
                     "in_today": 1, "out_today": 0, "reason": "crossing"}, ts=1)
        assert _wait(lambda: len(pub._client.published) == 1)
        topic, payload, qos, retain = pub._client.published[0]
        assert retain is True and qos == 1
        assert json.loads(payload)["replayed"] is False
        assert pub.spool.depth == 0
    finally:
        pub.close()


def test_publish_failure_goes_to_spool_then_replays(tmp_path):
    pub = _make_publisher(tmp_path)
    client = pub._client
    pub.start()
    try:
        client.fail_publish = True  # 连着但 PUBACK 失败
        pub.publish("crossing", _ctx(tmp_path), {"n": 0}, ts=7)
        assert _wait(lambda: pub.spool.depth == 1)
        client.fail_publish = False
        client.set_connected(True)   # 重新触发 drain
        assert _wait(lambda: client.published and pub.spool.depth == 0)
        assert json.loads(client.published[0][1])["replayed"] is True
    finally:
        pub.close()


def test_health_fields(tmp_path):
    pub = _make_publisher(tmp_path)
    pub.start()
    try:
        h = pub.health()
        assert set(h) == {"spool_depth", "spool_overflow",
                          "mqtt_app_connected", "last_event_ts"}
        assert h["mqtt_app_connected"] is True
    finally:
        pub.close()


# -------------------------------------------------------------- 真实 broker

@pytest.mark.broker
def test_broker_replay_with_real_client(tmp_path):
    """spool 预置断网期间事件 → 真实 MqttClient 连接 mosquitto → 补发 replayed=true。"""
    addr = os.environ.get("VB_TEST_BROKER")
    if not addr:
        pytest.skip("VB_TEST_BROKER=host:port 未设置")
    host, port = addr.split(":")
    from vision_base.mqtt import MqttClient

    sub = MqttClient(host, int(port), "c2-test-sub", keepalive_s=30)
    received: list[tuple[str, bytes]] = []
    got = threading_event = __import__("threading").Event()
    sub.subscribe("demo-lot/parking/gate-a/crossing", 1,
                  lambda topic, payload, retain: (
                      received.append((topic, payload)), threading_event.set()))
    assert sub.connect(10.0)
    try:
        spool = Spool(tmp_path / "spool.jsonl", 100)
        spool.append("demo-lot/parking/gate-a/crossing", "crossing",
                     {"schema": "parking.crossing/1", "event_id": "4b1c7c1e-2f7a-4a8e-9a55-2f1f1d3b9e10",
                      "site_id": "demo-lot", "camera_id": "gate-a", "ts": 1,
                      "seq": 0, "replayed": False, "direction": "in", "track_id": 1,
                      "vehicle_class": "car", "confidence": 0.9,
                      "occupancy_after": 1, "free_after": 119})
        pub = SpoolPublisher({"host": host, "port": int(port)},
                             client_id="c2-test-app", site_id="demo-lot",
                             spool_path=tmp_path / "spool.jsonl", max_events=100)
        pub.start()
        try:
            assert got.wait(10.0), "broker 未收到补发事件"
            assert _wait(lambda: pub.spool.depth == 0)
            payload = json.loads(received[0][1])
            assert payload["replayed"] is True
            assert payload["seq"] == 0
        finally:
            pub.close()
    finally:
        sub.close()
