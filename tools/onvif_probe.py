#!/usr/bin/env python3
"""NRD-4 P3 ONVIF 设备探测（规格 §2「只做 WS-Discovery 列出设备地址」、§5.1）。

标准库 socket 实现 WS-Discovery Probe：向 UDP 组播 ``239.255.255.250:3702``
发 SOAP Probe（Types = ``dn:NetworkVideoTransmitter``），收集窗口内的
ProbeMatches，去重后打印每台设备的 XAddrs（服务地址；RTSP 地址由用户
在编辑页/预设里填写，本工具不做 GetStreamUri 认证流程，§2「不做」）。

用法::

    uv run python tools/onvif_probe.py [--timeout 3.0] [--json]

- 需在待探测网段的直连网络上运行（容器内运行须 ``network_mode: host``，
  §5.1；docker 桥接下组播不出宿主机）。
- 退出码恒 0（未发现设备也是正常结果）；用法错误退 2。
"""
from __future__ import annotations

import argparse
import json
import socket
import sys
import uuid
import xml.etree.ElementTree as ET

__all__ = ["build_probe", "parse_probe_matches", "probe", "main",
           "MCAST_GROUP", "MCAST_PORT"]

MCAST_GROUP = "239.255.255.250"      # WS-Discovery IPv4 组播地址（§5.1）
MCAST_PORT = 3702

_PROBE = """<?xml version="1.0" encoding="utf-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
 xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
 xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
 <e:Header>
  <w:MessageID>uuid:{message_id}</w:MessageID>
  <w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
  <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
 </e:Header>
 <e:Body>
  <d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe>
 </e:Body>
</e:Envelope>
"""


def build_probe(message_id: str | None = None) -> bytes:
    """构造 WS-Discovery Probe 报文（ONVIF 设备类型过滤）。"""
    return _PROBE.format(message_id=message_id or uuid.uuid4()).encode("utf-8")


def _local(tag: str) -> str:
    """剥命名空间：``{ns}XAddrs`` → ``XAddrs``（各固件前缀/URI 差异大）。"""
    return tag.rsplit("}", 1)[-1]


def parse_probe_matches(data: bytes) -> list[dict]:
    """解析一份 ProbeMatches 应答 → [{xaddrs, types, scopes, epr}, ...]。

    XML 非法返回空列表（网络上来路不明的报文不致命）。
    """
    try:
        root = ET.fromstring(data.decode("utf-8", errors="replace"))
    except ET.ParseError:
        return []
    out = []
    for match in root.iter():
        if _local(match.tag) != "ProbeMatch":
            continue
        dev = {"xaddrs": [], "types": "", "scopes": "", "epr": ""}
        for el in match.iter():
            name = _local(el.tag)
            text = (el.text or "").strip()
            if name == "XAddrs" and text:
                dev["xaddrs"] = text.split()
            elif name == "Types":
                dev["types"] = text
            elif name == "Scopes":
                dev["scopes"] = text
            elif name == "Address" and not dev["epr"]:
                dev["epr"] = text      # EndpointReference 的 Address（首个）
        if dev["xaddrs"] or dev["epr"]:
            out.append(dev)
    return out


def probe(timeout_s: float = 3.0, *, mcast_group: str = MCAST_GROUP,
          mcast_port: int = MCAST_PORT, send_from: tuple | None = None) -> list[dict]:
    """发 Probe 并收集 ``timeout_s`` 窗口内的应答，按 EPR/XAddrs 去重。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        sock.settimeout(0.2)
        if send_from is not None:                       # 测试注入：本地回环收
            sock.bind(send_from)
        sock.sendto(build_probe(), (mcast_group, mcast_port))
        found: dict[str, dict] = {}
        deadline = timeout_s
        import time
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                data, _addr = sock.recvfrom(65535)
            except TimeoutError:
                continue
            for dev in parse_probe_matches(data):
                key = dev["epr"] or " ".join(sorted(dev["xaddrs"]))
                found.setdefault(key, dev)
        return list(found.values())
    finally:
        sock.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="ONVIF WS-Discovery 设备探测（列出 XAddrs，NRD-4 §2）")
    ap.add_argument("--timeout", type=float, default=3.0,
                    help="收答窗口秒数（默认 3.0）")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非文本")
    args = ap.parse_args(argv)
    if args.timeout <= 0:
        print("onvif_probe: --timeout 必须 > 0", file=sys.stderr)
        return 2
    devices = probe(args.timeout)
    if args.json:
        print(json.dumps(devices, ensure_ascii=False, indent=2))
    elif devices:
        for d in devices:
            for x in d["xaddrs"]:
                print(x)
    else:
        print(f"onvif_probe: {args.timeout:g}s 内未发现 ONVIF 设备"
              f"（检查组播可达 / network_mode: host）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
