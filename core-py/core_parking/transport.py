"""SpoolPublisher：vision_base.mqtt 独立会话（QoS1）+ 断网补发（§6.4）。

仅标准库 + vision_base。结构（§6.4）：

- 钩子线程只调 ``publish()``：生成信封 → 非阻塞入内存队列，不做网络 IO；
- 发送线程串行：MQTT 已连接且 spool 为空则直发；失败或断开则入 spool；
- ``on_connect`` 是 IO 线程回调（BASE-1 §6.7.1 禁止在 IO 线程上调 QoS1
  publish），只置 ``drain_pending``；发送线程收到后串行 drain：逐条读 spool、
  置 ``replayed=true``、QoS1 publish 成功后推进游标（spool.ack 原子写）；
- 重连退避由 vision_base.mqtt MqttClient 自带（reconnect_min_s=1.0 /
  reconnect_max_s=60.0，±20% 抖动），本类不再自写退避。

测试可注入 ``client_factory``（FakeMqtt，见 tests/fake_mqtt.py）；默认工厂
创建真实的 vision_base.mqtt.MqttClient（B7 签名，BASE-1 M1.10）。
"""

from __future__ import annotations

import json
import logging
import queue
import threading

from .envelope import RETAINED_KINDS, SeqAllocator, build_envelope, topic_for
from .spool import Spool

log = logging.getLogger("core_parking.transport")

_PUBLISH_TIMEOUT_S = 5.0


def default_client_factory(mqtt_opts: dict, client_id: str, on_connect):
    """真实会话：vision_base.mqtt.MqttClient（BASE-1 §6.7.1 / §S13 B7）。"""
    from vision_base.mqtt import MqttClient

    return MqttClient(
        mqtt_opts["host"], int(mqtt_opts.get("port", 1883)), client_id,
        username=mqtt_opts.get("username", ""), password=mqtt_opts.get("password", ""),
        tls=bool(mqtt_opts.get("tls", False)), ca_file=mqtt_opts.get("ca_file", ""),
        keepalive_s=int(mqtt_opts.get("keepalive_s", 30)),
        on_connect=on_connect,
    )


class SpoolPublisher:
    """停车事件发布器：QoS1 独立会话 + 断网 spool + 恢复补发（replayed=true）。"""

    def __init__(self, mqtt_opts: dict, *, client_id: str, site_id: str,
                 spool_path, max_events: int, client_factory=None) -> None:
        self._site_id = site_id
        self.spool = Spool(spool_path, max_events)
        self._q: queue.Queue = queue.Queue()
        self._drain_pending = threading.Event()
        self._stop = threading.Event()
        self._last_event_ts = 0
        factory = client_factory or default_client_factory
        self._client = factory(mqtt_opts, client_id, self._on_connect)
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="spool-publisher",
                                        daemon=True)
        self._thread.start()
        self._client.connect()
        if self.spool.depth:
            self._drain_pending.set()

    def close(self, timeout_s: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout_s)
        close = getattr(self._client, "close", None)
        if close is not None:
            close()

    # ------------------------------------------------------------- 钩子侧

    def publish(self, kind: str, ctx, body: dict, ts: int) -> None:
        """非阻塞：分配 seq、生成信封、入内存队列。ctx 需有 stream_id 与
        state["seq"]（SeqAllocator）——即 §6.2 ParkingHooks 的 StreamContext。"""
        seq_alloc = ctx.state["seq"]
        if not isinstance(seq_alloc, SeqAllocator):
            raise TypeError('ctx.state["seq"] 必须是 envelope.SeqAllocator')
        env = build_envelope(kind, site_id=self._site_id, camera_id=ctx.stream_id,
                             ts=ts, seq=seq_alloc.next(), body=body)
        self._last_event_ts = int(ts)
        self._q.put_nowait((topic_for(self._site_id, ctx.stream_id, kind), kind, env))

    def health(self) -> dict:
        """§S13 B2：/healthz 的 app 子对象字段。"""
        return {
            "spool_depth": self.spool.depth,
            "spool_overflow": self.spool.overflow,
            "mqtt_app_connected": bool(self._client.connected),
            "last_event_ts": self._last_event_ts,
        }

    # ----------------------------------------------------------- 发送线程

    def _on_connect(self) -> None:
        # IO 线程回调：禁止在这里做 QoS1 publish（BASE-1 §6.7.1），只置标志
        self._drain_pending.set()

    def _publish_wire(self, topic: str, kind: str, env: dict) -> bool:
        payload = json.dumps(env, ensure_ascii=False).encode("utf-8")
        return bool(self._client.publish(topic, payload, qos=1,
                                         retain=kind in RETAINED_KINDS,
                                         timeout_s=_PUBLISH_TIMEOUT_S))

    def _drain(self) -> None:
        """补发 spool：置 replayed=true，QoS1 成功后推进游标。保持顺序。"""
        self._drain_pending.clear()
        for entry in self.spool.pending():
            if not self._client.connected:
                break
            env = dict(entry.payload)
            env["replayed"] = True
            if self._publish_wire(entry.topic, entry.kind, env):
                self.spool.ack(entry.id)
            else:
                break
        if self.spool.depth:
            self._drain_pending.set()  # 未发完，稍后再试

    def _run(self) -> None:
        while not self._stop.is_set():
            if self._client.connected and (
                    self._drain_pending.is_set() or self.spool.depth):
                self._drain()
            try:
                topic, kind, env = self._q.get(timeout=0.1)
            except queue.Empty:
                continue
            if self.spool.depth == 0 and self._client.connected \
                    and self._publish_wire(topic, kind, env):
                continue
            self.spool.append(topic, kind, env)
            if self._client.connected:
                self._drain_pending.set()
