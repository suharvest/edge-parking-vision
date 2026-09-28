"""C2 自有的 FakeMqtt（§8.0 C2 行：发布部分先对 FakeMqtt 开发）。

与 tests/fakes.py（归 C1）无关。实现 §S13 B7 的接口形状：
connect() / connected 属性 / publish(topic, payload, qos=1, retain=False,
timeout_s=5.0) -> bool / close()，构造签名与 SpoolPublisher 的
client_factory(mqtt_opts, client_id, on_connect) 约定一致。
"""

from __future__ import annotations


class FakeMqtt:
    def __init__(self, mqtt_opts: dict | None = None, client_id: str = "",
                 on_connect=None) -> None:
        self.mqtt_opts = mqtt_opts or {}
        self.client_id = client_id
        self.on_connect = on_connect
        self._connected = False
        self.fail_publish = False     # 模拟 PUBACK 超时 / 发送失败
        self.published: list[tuple[str, bytes, int, bool]] = []  # topic, payload, qos, retain
        self.connect_calls = 0

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self, timeout_s: float = 10.0) -> bool:
        self.connect_calls += 1
        self.set_connected(True)
        return True

    def set_connected(self, flag: bool) -> None:
        was = self._connected
        self._connected = bool(flag)
        if self._connected and not was and self.on_connect is not None:
            self.on_connect()

    def publish(self, topic: str, payload, qos: int = 0, retain: bool = False,
                timeout_s: float = 5.0) -> bool:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        if not self._connected or self.fail_publish:
            return False
        self.published.append((topic, bytes(payload), qos, retain))
        return True

    def close(self, timeout_s: float = 2.0) -> None:
        self._connected = False
