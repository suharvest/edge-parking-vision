"""NRD-4 P3 ONVIF 探测工具测试（`tools/onvif_probe.py`，§8.1 P3 行）。

不发真组播：单测 Probe 报文构造与 ProbeMatches 解析（罐装 XML，来自
典型 ONVIF 设备应答形状）；`probe()` 用注入的本地回环 socket 验证收发
闭环与去重。真实局域网发现只能真机/现场验证（见报告缺口说明）。
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import onvif_probe  # noqa: E402

# 典型 ONVIF ProbeMatches（Hikvision 风格命名空间前缀；XAddrs 两个地址）
MATCH_XML = b"""<?xml version="1.0" encoding="utf-8"?>
<env:Envelope xmlns:env="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl"
 xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
 <env:Header><wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action></env:Header>
 <env:Body>
  <d:ProbeMatches>
   <d:ProbeMatch>
    <wsa:EndpointReference><wsa:Address>urn:uuid:81386d1e-4714-4e7c-970a-612d190b0208</wsa:Address></wsa:EndpointReference>
    <d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>
    <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/IPC</d:Scopes>
    <d:XAddrs>http://192.0.2.64:8000/onvif/device_service http://[fe80::1]:8000/onvif/device_service</d:XAddrs>
    <d:MetadataVersion>10</d:MetadataVersion>
   </d:ProbeMatch>
  </d:ProbeMatches>
 </env:Body>
</env:Envelope>
"""


def test_build_probe_contains_ws_discovery_urns():
    msg = onvif_probe.build_probe("test-uuid").decode()
    assert "uuid:test-uuid" in msg
    assert "urn:schemas-xmlsoap-org:ws:2005:04:discovery" in msg
    assert "discovery/Probe" in msg
    assert "dn:NetworkVideoTransmitter" in msg


def test_parse_probe_matches_extracts_xaddrs():
    devs = onvif_probe.parse_probe_matches(MATCH_XML)
    assert len(devs) == 1
    d = devs[0]
    assert d["xaddrs"] == ["http://192.0.2.64:8000/onvif/device_service",
                           "http://[fe80::1]:8000/onvif/device_service"]
    assert "NetworkVideoTransmitter" in d["types"]
    assert "video_encoder" in d["scopes"]
    assert d["epr"].startswith("urn:uuid:")


def test_parse_probe_matches_garbage_is_empty():
    assert onvif_probe.parse_probe_matches(b"not xml at all") == []
    assert onvif_probe.parse_probe_matches(b"") == []
    assert onvif_probe.parse_probe_matches(b"<a><b/></a>") == []


def test_probe_loopback_dedup():
    """回环闭环：replay 线程重复发同一应答 → probe 收到并按 EPR 去重为 1 台。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()                                # 让 probe 的 socket 绑这个端口

    stop = threading.Event()

    def replay():
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        while not stop.is_set():                # 重复发：考验去重
            try:
                sender.sendto(MATCH_XML, ("127.0.0.1", port))
            except OSError:
                break
            time.sleep(0.02)
        sender.close()

    t = threading.Thread(target=replay, daemon=True)
    t.start()
    try:
        # 组播发出指向无收端的 127.0.0.1:1（只验收路径）；收 socket 绑到 port
        devs = onvif_probe.probe(0.4, mcast_group="127.0.0.1",
                                 mcast_port=1, send_from=("127.0.0.1", port))
        assert len(devs) == 1                   # 去重后只剩 1 台
        assert devs[0]["xaddrs"][0].startswith("http://192.0.2.64")
    finally:
        stop.set()
        t.join(timeout=1)


def test_parse_then_dedup_key_stable():
    """同一 EPR 的两份应答应去重（直接验 found dict 语义通过 probe 的 key）。"""
    d1 = onvif_probe.parse_probe_matches(MATCH_XML)[0]
    d2 = onvif_probe.parse_probe_matches(MATCH_XML)[0]
    found = {}
    for d in (d1, d2):
        key = d["epr"] or " ".join(sorted(d["xaddrs"]))
        found.setdefault(key, d)
    assert len(found) == 1


def test_main_json_empty_result(capsys, monkeypatch):
    monkeypatch.setattr(onvif_probe, "probe", lambda timeout_s: [])
    assert onvif_probe.main(["--timeout", "1"]) == 0
    err = capsys.readouterr().err
    assert "未发现" in err


def test_main_json_output(capsys, monkeypatch):
    monkeypatch.setattr(onvif_probe, "probe",
                        lambda timeout_s: onvif_probe.parse_probe_matches(MATCH_XML))
    assert onvif_probe.main(["--timeout", "1", "--json"]) == 0
    import json
    out = json.loads(capsys.readouterr().out)
    assert out[0]["xaddrs"][0].startswith("http://192.0.2.64")
