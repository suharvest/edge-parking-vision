"""C1/C2 共用测试替身（规格 §8.0：基座未完成部分先以 fake 顶替）。

- `FakeMqtt`：顶替 `vision_base.mqtt.MqttClient` 公开 API（§S13 B7）：
  `connect()` / `connected` / `publish(topic, payload, qos, retain, timeout_s)`
  / `subscribe(topic, qos, callback)` / `on_connect` 回调。C2 的
  SpoolPublisher 对它开发；行为（QoS1 等 PUBACK 返回 bool、断线返回 False、
  IO 线程禁止 QoS1 publish）与真身对齐。
- `FakeStreamContext`：顶替 `vision_base.hooks.StreamContext` 中停车用到的
  部分（`stream_id` / `spec` / `state`）。
- `RecordingPublisher`：顶替 C2 `transport.SpoolPublisher` 的
  `publish(kind, ctx, body, ts)` 接口，记录全部调用供断言。

`Event` / `StreamSpec` 直接用 `vision_base.types` 的真 dataclass（M1.12 已完成）。
"""
from __future__ import annotations

import threading

from vision_base.types import Event, StreamSpec

__all__ = ["FakeMqtt", "FakeStreamContext", "RecordingPublisher", "make_event",
           "make_line_cross_event"]


class FakeMqtt:
    """`vision_base.mqtt.MqttClient` 的可控替身（B7 公开 API 子集）。"""

    def __init__(self, host: str = "fake", port: int = 1883, client_id: str = "fake-app",
                 *, username: str = "", password: str = "", tls: bool = False,
                 ca_file: str = "", keepalive_s: int = 30, will=None,
                 reconnect_min_s: float = 1.0, reconnect_max_s: float = 60.0,
                 max_inflight: int = 32, on_connect=None, on_disconnect=None):
        self.host = host
        self.port = port
        self.client_id = client_id
        self.username = username
        self.password = password
        self.tls = tls
        self.keepalive_s = keepalive_s
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect
        self._connected = False
        self.published: list[dict] = []      # {topic, payload(bytes), qos, retain}
        self.subscriptions: list[tuple[str, int, object]] = []
        self.publish_result = True           # 断线/失败注入：置 False 后 publish 返回 False
        self.connect_calls = 0
        self._io_thread = threading.current_thread()  # 主线程即"非 IO 线程"，QoS1 不抛

    # ---- MqttClient 公开 API ----

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, timeout_s: float = 10.0) -> bool:
        self.connect_calls += 1
        self.set_connected(True)
        return True

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False,
                timeout_s: float = 5.0) -> bool:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not isinstance(payload, (bytes, bytearray)):
            raise TypeError("payload must be bytes or str")
        if qos not in (0, 1):
            raise ValueError("qos must be 0 or 1")
        if not self._connected or not self.publish_result:
            return False
        self.published.append({"topic": topic, "payload": bytes(payload),
                               "qos": qos, "retain": retain})
        return True

    def subscribe(self, topic_filter: str, qos: int = 0, callback=None) -> None:
        self.subscriptions.append((topic_filter, qos, callback))

    # ---- 测试控制 ----

    def set_connected(self, value: bool) -> None:
        was = self._connected
        self._connected = bool(value)
        if value and not was and self.on_connect is not None:
            self.on_connect()
        if was and not value and self.on_disconnect is not None:
            self.on_disconnect("test disconnect")


class FakeStreamContext:
    """`vision_base.hooks.StreamContext` 替身：停车钩子只用 stream_id/spec/state。"""

    def __init__(self, stream_id: str = "gate-a", spec: StreamSpec | None = None):
        self.stream_id = stream_id
        self.spec = spec or StreamSpec(stream_id=stream_id, url="file://fake")
        self.state: dict = {}
        self.configured: list[tuple[str, dict]] = []  # configure_analyzer 记录（B3）

    def configure_analyzer(self, name: str, config: dict, timeout_s: float = 3.0) -> dict:
        self.configured.append((name, config))
        return {"applied": True}


class RecordingPublisher:
    """C2 `transport.SpoolPublisher` 替身：记录 publish(kind, ctx, body, ts) 调用。"""

    def __init__(self) -> None:
        self.records: list[dict] = []   # {kind, stream_id, body, ts}
        # health() 读取的属性（§S13 B2 字段）
        self.spool_depth = 0
        self.spool_overflow = False
        self.mqtt_app_connected = True

    def publish(self, *, kind: str, ctx, body: dict, ts: int) -> None:
        self.records.append({"kind": kind, "stream_id": ctx.stream_id,
                             "body": dict(body), "ts": ts})


def make_event(stream_id: str = "gate-a", seq: int = 1, wall_ms: float = 0.0,
               analyzer: str = "line_cross", type: str = "line_cross",
               track_id: int = 1, fields: dict | None = None) -> Event:
    return Event(stream_id=stream_id, seq=seq, wall_ms=wall_ms,
                 analyzer=analyzer, type=type, track_id=track_id,
                 fields=dict(fields or {}))


def make_line_cross_event(direction: str, *, stream_id: str = "gate-a", seq: int = 1,
                          wall_ms: float = 0.0, track_id: int = 1,
                          class_id: int = 2, score: float = 0.9,
                          line_id: str = "main") -> Event:
    return make_event(stream_id=stream_id, seq=seq, wall_ms=wall_ms,
                      track_id=track_id,
                      fields={"line_id": line_id, "direction": direction,
                              "anchor": [0.5, 0.6], "class_id": class_id,
                              "score": score})
