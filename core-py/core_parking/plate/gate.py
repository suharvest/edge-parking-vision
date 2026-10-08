"""NRD-3 R1124 gate controller (MQTT + a safe, pulsed DO output)."""
from __future__ import annotations

import json
import argparse
import ctypes
import ctypes.util
import logging
import math
import re
import signal
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from ..envelope import SeqAllocator
from ..transport import SpoolPublisher
from .whitelist import Whitelist

log = logging.getLogger(__name__)


class GateOutput:
    def pulse(self, ms: int) -> None:
        raise NotImplementedError


class GpiodGateOutput(GateOutput):
    def __init__(self, line: str):
        if not line:
            raise ValueError("do_line is required")
        if ":" not in line:
            raise ValueError("do_line must be a verified libgpiod v1 chip:offset")
        chip, offset = line.rsplit(":", 1)
        if not chip or not offset.isdigit() or int(offset) > 0xffffffff:
            raise ValueError("do_line must be a verified libgpiod v1 chip:offset")
        libname = ctypes.util.find_library("gpiod")
        if not libname:
            raise RuntimeError("libgpiod is unavailable; refusing to start GPIO output")
        lib = ctypes.CDLL(libname, use_errno=True)
        required = ("gpiod_chip_open_by_name", "gpiod_chip_get_line",
                    "gpiod_line_request_output", "gpiod_line_set_value",
                    "gpiod_line_release", "gpiod_chip_close")
        if any(not hasattr(lib, name) for name in required):
            raise RuntimeError("unsupported libgpiod ABI (verified v1 symbols required)")
        # libgpiod v1.6 include/gpiod.h: all chip/line handles are pointers.
        signatures = {
            "gpiod_chip_open_by_name": ([ctypes.c_char_p], ctypes.c_void_p),
            "gpiod_chip_get_line": ([ctypes.c_void_p, ctypes.c_uint], ctypes.c_void_p),
            "gpiod_line_request_output": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
            "gpiod_line_set_value": ([ctypes.c_void_p, ctypes.c_int], ctypes.c_int),
            "gpiod_line_release": ([ctypes.c_void_p], None),
            "gpiod_chip_close": ([ctypes.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            getattr(lib, name).argtypes = args
            getattr(lib, name).restype = result
        self.line = line
        self._lib, self._chip_name, self._offset = lib, chip, int(offset)
        self._chip = None
        self._line = None

    def _request(self) -> None:
        if self._line is not None:
            return
        lib = self._lib
        chip = lib.gpiod_chip_open_by_name(self._chip_name.encode())
        if not chip:
            raise OSError("gpiod_chip_open_by_name failed")
        line = lib.gpiod_chip_get_line(chip, self._offset)
        if not line or lib.gpiod_line_request_output(line, b"edge-parking", 0) != 0:
            lib.gpiod_chip_close(chip)
            raise OSError("gpiod line request failed")
        self._chip, self._line = chip, line

    def _release(self) -> None:
        if self._line:
            self._lib.gpiod_line_release(self._line)
        if self._chip:
            self._lib.gpiod_chip_close(self._chip)
        self._line = self._chip = None

    def pulse(self, ms: int) -> None:
        self._request()
        try:
            if self._lib.gpiod_line_set_value(self._line, 1) != 0:
                raise OSError("gpiod_line_set_value(1) failed")
            time.sleep(ms / 1000.0)
        finally:
            if self._lib.gpiod_line_set_value(self._line, 0) != 0:
                raise OSError("gpiod_line_set_value(0) failed")

    def reset(self) -> None:
        self._request()
        if self._lib.gpiod_line_set_value(self._line, 0) != 0:
            raise OSError("gpiod_line_set_value(0) failed")

    def close(self) -> None:
        try:
            if self._line:
                self.reset()
        finally:
            self._release()


class FakeGateOutput(GateOutput):
    def __init__(self):
        self.pulses: list[int] = []
        self.reset_calls = 0

    def pulse(self, ms: int) -> None:
        self.pulses.append(int(ms))

    def reset(self) -> None:
        self.reset_calls += 1


@dataclass
class GateConfig:
    site_id: str
    device_id: str
    whitelist_path: str
    pulse_ms: int
    min_interval_s: float
    subscribe_cameras: tuple[str, ...]
    gate_id: str


class GateController:
    def __init__(self, config: dict | None = None, *, output=None, tx=None,
                 clock=None, mqtt_client=None):
        self.options = dict(config or {})
        self.config: GateConfig | None = None
        self.output = output
        self.tx = tx
        self.clock = clock or time.time
        self.mqtt = mqtt_client
        self.whitelist = Whitelist([])
        self._last_pulse = 0.0
        self._seq = None
        self._seen_event_ids: set[str] = set()
        self._seen_order = deque()
        self._pulse_lock = threading.RLock()
        self._http = None
        self._closed = False
        if config:
            try:
                self.configure(config)
            except Exception:
                self.close()
                raise

    def configure(self, config: dict) -> None:
        # Force a safe state before any later startup step can fail.
        if self.output is not None:
            reset = getattr(self.output, "reset", None)
            if callable(reset):
                reset()
        self._validate_config(config)
        new_config = GateConfig(
            site_id=str(config["site_id"]), device_id=str(config["device_id"]),
            whitelist_path=str(config["whitelist_path"]), pulse_ms=int(config["pulse_ms"]),
            min_interval_s=float(config["min_interval_s"]),
            subscribe_cameras=tuple(config["subscribe_cameras"]), gate_id=str(config["gate_id"]))
        new_output = self.output or GpiodGateOutput(str(config["do_line"]))
        try:
            reset = getattr(new_output, "reset", None)
            if callable(reset):
                reset()
            path = Path(new_config.whitelist_path)
            new_whitelist = Whitelist.from_csv(path.read_text(encoding="utf-8"))
            new_seq = SeqAllocator(Path(config.get("state_dir", "state")) / "gate")
        except Exception:
            cleanup = getattr(new_output, "close", None) or getattr(new_output, "reset", None)
            if callable(cleanup):
                cleanup()
            raise
        old = (self.options, self.config, self.whitelist, self._seq, self.output, self.tx, self.mqtt)
        new_tx = self.tx
        created_tx = new_tx is None
        if created_tx:
            try:
                new_tx = SpoolPublisher(dict(config["mqtt"]), client_id=f"{new_config.site_id}-{new_config.device_id}",
                                        site_id=new_config.site_id,
                                        spool_path=Path(config["state_dir"]) / "spool.jsonl",
                                        max_events=50000)
            except Exception:
                if new_output is not self.output:
                    new_output.close()
                raise
        self.options, self.config, self.whitelist, self._seq, self.output, self.tx = (
            dict(config), new_config, new_whitelist, new_seq, new_output, new_tx)
        try:
            if created_tx:
                new_tx.start()
            if self.mqtt is not None or isinstance(self.tx, SpoolPublisher):
                self.subscribe()
        except Exception:
            resources = (self.mqtt if self.mqtt is not old[6] else None,
                         new_tx if created_tx else None,
                         new_output if new_output is not old[4] else None)
            self.options, self.config, self.whitelist, self._seq, self.output, self.tx, self.mqtt = old
            for resource in resources:
                cleanup = getattr(resource, "close", None) or getattr(resource, "reset", None)
                if callable(cleanup):
                    try:
                        cleanup()
                    except Exception:
                        log.exception("gate startup cleanup failed")
            raise

    @staticmethod
    def _validate_config(config: dict) -> None:
        required = {"site_id", "device_id", "whitelist_path", "state_dir", "do_line",
                    "pulse_ms", "min_interval_s", "subscribe_cameras", "gate_id",
                    "mqtt", "http"}
        if not isinstance(config, dict):
            raise ValueError("gate config must be an object")
        missing = required - set(config)
        if missing:
            raise ValueError(f"missing gate config: {sorted(missing)}")
        if set(config) - required:
            raise ValueError("unknown gate config fields")
        if not isinstance(config["state_dir"], str) or not config["state_dir"]:
            raise ValueError("state_dir must be a non-empty string")
        if not isinstance(config["whitelist_path"], str) or not config["whitelist_path"]:
            raise ValueError("whitelist_path must be a non-empty string")
        if not isinstance(config["do_line"], str) or not config["do_line"]:
            raise ValueError("do_line must be a non-empty string")
        mqtt = config["mqtt"]
        if (not isinstance(mqtt, dict) or
                any(key not in mqtt for key in ("host", "port", "username", "password", "keepalive_s")) or
                not isinstance(mqtt.get("host"), str) or not mqtt.get("host") or
                not isinstance(mqtt.get("username"), str) or not isinstance(mqtt.get("password"), str)):
            raise ValueError("mqtt.host must be a non-empty string")
        for key in ("port", "keepalive_s"):
            if key in mqtt and (isinstance(mqtt[key], bool) or not isinstance(mqtt[key], int) or mqtt[key] < 1):
                raise ValueError(f"mqtt.{key} must be a positive integer")
        if set(mqtt) != {"host", "port", "username", "password", "keepalive_s"} or mqtt["port"] > 65535:
            raise ValueError("invalid mqtt fields or port")
        http = config.get("http")
        if not isinstance(http, dict) or isinstance(http.get("port"), bool) or not isinstance(http.get("port"), int) or not 1 <= http["port"] <= 65535:
            raise ValueError("http.port must be an integer in 1..65535")
        if set(http) != {"port"}:
            raise ValueError("unknown http fields")
        ident = re.compile(r"^[a-z0-9-]{1,32}$")
        for key in ("site_id", "device_id", "gate_id"):
            if not isinstance(config[key], str) or not ident.fullmatch(config[key]):
                raise ValueError(f"{key} must match ^[a-z0-9-]{{1,32}}$")
        cameras = config["subscribe_cameras"]
        if (not isinstance(cameras, list) or not cameras
                or any(not isinstance(x, str) or not ident.fullmatch(x) for x in cameras)):
            raise ValueError("subscribe_cameras must contain valid camera ids")
        if (isinstance(config["pulse_ms"], bool) or not isinstance(config["pulse_ms"], int)
                or config["pulse_ms"] < 1):
            raise ValueError("pulse_ms must be a positive integer")
        interval = config["min_interval_s"]
        try:
            finite_interval = math.isfinite(float(interval))
        except (TypeError, ValueError, OverflowError):
            finite_interval = False
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not finite_interval or interval < 0):
            raise ValueError("min_interval_s must be a finite non-negative number")

    def subscribe(self, mqtt=None) -> None:
        client = mqtt or self.mqtt
        if client is None:
            from vision_base.mqtt import MqttClient
            m = self.options["mqtt"]
            client = MqttClient(m["host"], int(m.get("port", 1883)),
                                f"{self.config.site_id}-{self.config.device_id}-gate",
                                username=m.get("username", ""), password=m.get("password", ""),
                                keepalive_s=int(m.get("keepalive_s", 30)),
                                on_connect=lambda: None)
        self.mqtt = client
        connect = getattr(client, "connect", None)
        if callable(connect) and not bool(getattr(client, "connected", False)):
            connect()
        client.subscribe("+/parking/+/plate", qos=1, callback=self.on_mqtt_message)

    def on_mqtt_message(self, *args) -> dict | None:
        # vision_base delivers (topic, payload, retain).  Older adapters may
        # deliver a message object or just payload; never treat retain=False as
        # the JSON payload.
        payload = None
        if len(args) >= 2 and (isinstance(args[1], (dict, bytes, str))
                               or hasattr(args[1], "payload")):
            payload = args[1]
        if payload is None:
            payload = next((arg for arg in reversed(args)
                            if isinstance(arg, (dict, bytes)) or hasattr(arg, "payload")), None)
        if payload is None:
            return None
        if isinstance(payload, dict):
            return self.on_plate_event(payload)
        if hasattr(payload, "payload"):
            payload = payload.payload
        try:
            value = json.loads(payload.decode("utf-8") if isinstance(payload, bytes) else payload)
        except (TypeError, ValueError, UnicodeDecodeError):
            return None
        return self.on_plate_event(value) if isinstance(value, dict) else None

    def on_plate_event(self, ev: dict) -> dict | None:
        with self._pulse_lock:
            return self._on_plate_event(ev)

    def _on_plate_event(self, ev: dict) -> dict | None:
        c = self.config
        if self._closed or c is None or not isinstance(ev, dict):
            return None
        if (ev.get("schema") != "parking.plate/1" or ev.get("site_id") != c.site_id
                or ev.get("camera_id") not in c.subscribe_cameras):
            return None
        if ev.get("replayed") is not False:
            return None
        event_id = ev.get("event_id")
        if not isinstance(event_id, str) or not re.fullmatch(
                r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", event_id):
            return None
        try:
            canonical_id = str(uuid.UUID(event_id))
        except (AttributeError, TypeError, ValueError):
            return None
        if canonical_id in self._seen_event_ids:
            return None
        required = {"schema", "event_id", "site_id", "camera_id", "ts", "seq", "replayed",
                    "plate", "plate_normalized", "region", "plate_type", "confidence",
                    "votes", "frames", "bbox", "track_id", "snapshot"}
        if set(ev) != required or ev["plate_type"] not in ("cn_blue", "cn_green"):
            return None
        snapshot = ev["snapshot"]
        if snapshot is not None:
            if (not isinstance(snapshot, dict) or set(snapshot) != {"url", "sha256", "width", "height"}
                    or not isinstance(snapshot["url"], str) or not snapshot["url"]
                    or not isinstance(snapshot["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", snapshot["sha256"])
                    or any(isinstance(snapshot[k], bool) or not isinstance(snapshot[k], int) or snapshot[k] < 1
                           for k in ("width", "height"))):
                return None
        if not isinstance(ev.get("plate"), str) or not ev["plate"]:
            return None
        if not isinstance(ev.get("region"), str) or ev.get("region") != "cn":
            return None
        if not isinstance(ev.get("plate_normalized"), str) or not ev["plate_normalized"]:
            return None
        confidence = ev["confidence"]
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not 0 <= confidence <= 1 or not math.isfinite(float(confidence))):
            return None
        for key in ("votes", "frames"):
            if key in ev and (isinstance(ev[key], bool) or not isinstance(ev[key], int)
                              or ev[key] < 1):
                return None
        if isinstance(ev["track_id"], bool) or not isinstance(ev["track_id"], int) or ev["track_id"] < 0:
            return None
        if isinstance(ev["seq"], bool) or not isinstance(ev["seq"], int) or ev["seq"] < 0:
            return None
        bbox = ev["bbox"]
        if (not isinstance(bbox, list) or len(bbox) != 4 or any(
                        isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 1 or not math.isfinite(float(x))
                for x in bbox)):
            return None
        ts = ev["ts"]
        if isinstance(ts, bool) or not isinstance(ts, int):
            return None
        age = int(self.clock() * 1000) - int(ts)
        if age > 3000 or age < 0:
            return None
        self._remember_event(canonical_id)
        entry = self.whitelist.match(str(ev.get("plate_normalized", ev.get("plate", ""))), "cn")
        if entry is None:
            return None
        now = float(self.clock())
        common = {"plate_event_id": str(ev.get("event_id", "")),
                  "plate_normalized": ev.get("plate_normalized", ""),
                  "whitelist_label": entry.label, "pulse_ms": c.pulse_ms,
                  "latency_ms": max(0, int(now * 1000) - int(ev.get("ts", 0)))}
        return self._pulse(ev, common, "whitelist")

    def _remember_event(self, event_id: str) -> None:
        self._seen_event_ids.add(event_id)
        self._seen_order.append(event_id)
        while len(self._seen_order) > 10000:
            self._seen_event_ids.discard(self._seen_order.popleft())

    def _pulse(self, plate_ev: dict, common: dict, trigger: str) -> dict | None:
        with self._pulse_lock:
            if self._closed:
                return None
            now = float(self.clock())
            if now - self._last_pulse < self.config.min_interval_s:
                return self._publish_gate(plate_ev, {**common, "action": "suppressed", "trigger": trigger})
            try:
                self.output.pulse(self.config.pulse_ms)
            except Exception:
                reset = getattr(self.output, "reset", None)
                if callable(reset): reset()
                log.exception("gate pulse failed; refusing open event")
                return None
            self._last_pulse = now
            return self._publish_gate(plate_ev, {**common, "action": "open_pulse", "trigger": trigger})

    def manual_test(self) -> dict | None:
        ev_id = str(uuid.uuid4())
        ev = {"event_id": ev_id}
        common = {"plate_event_id": ev_id, "plate_normalized": "manual-test",
                  "whitelist_label": "manual_test", "pulse_ms": self.config.pulse_ms,
                  "latency_ms": 0}
        return self._pulse(ev, common, "manual_test")

    def _publish_gate(self, plate_ev: dict, body: dict) -> dict:
        event = {"schema": "parking.gate/1", "event_id": str(uuid.uuid4()),
                 "site_id": self.config.site_id, "camera_id": self.config.gate_id,
                 "ts": int(self.clock() * 1000), "seq": self._seq.next(),
                 "replayed": False, **body}
        # Keep the publisher contract while allowing gate_id to be the event camera.
        # SpoolPublisher allocates the wire sequence itself.  The returned event
        # is a compact inspection value; published payload is built by tx.
        ctx = SimpleNamespace(stream_id=self.config.gate_id, state={"seq": self._seq})
        if self.tx is not None:
            try:
                wire = self.tx.publish(kind="gate", ctx=ctx, body=body, ts=event["ts"],
                                        seq=event["seq"], event_id=event["event_id"])
            except TypeError as exc:
                if "seq" not in str(exc) and "unexpected keyword" not in str(exc):
                    raise
                wire = self.tx.publish(kind="gate", ctx=ctx, body=body, ts=event["ts"])
            if isinstance(wire, dict):
                event.update({k: wire[k] for k in ("event_id", "seq", "ts", "site_id", "camera_id")
                              if k in wire})
        return event

    def close(self) -> None:
        with self._pulse_lock:
            if self._closed:
                return
            self._closed = True
        for resource in (self._http, self.mqtt, self.tx, self.output):
            cleanup = getattr(resource, "close", None)
            if resource is self.output and cleanup is None:
                cleanup = getattr(resource, "reset", None)
            if callable(cleanup):
                try:
                    cleanup()
                except Exception:
                    log.exception("gate resource cleanup failed")

    def start_http(self, host: str = "127.0.0.1", port: int | None = None):
        """Start the gate's small local control API (no GPIO test in HTTP code)."""
        if self._http is not None:
            return self._http
        selected_port = (int(self.options.get("http", {}).get("port", 8081))
                         if port is None else int(port))
        http = GateHttp(self, host=host, port=selected_port)
        http.start()
        self._http = http
        return http


class GateHttp:
    """HTTP adapter for whitelist replacement and an explicit test pulse."""

    def __init__(self, controller: GateController, *, host: str, port: int):
        self.controller = controller
        self.server = ThreadingHTTPServer((host, port), self._handler(controller))
        self.thread = None

    @staticmethod
    def _handler(controller):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("gate http: " + fmt, *args)

            def send_json(self, code, value):
                data = json.dumps(value, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):  # noqa: N802
                if self.path != "/whitelist":
                    return self.send_json(404, {"error": "no such path"})
                try:
                    data = Path(controller.config.whitelist_path).read_bytes()
                except OSError as exc:
                    return self.send_json(500, {"error": str(exc)})
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):  # noqa: N802
                if self.path != "/whitelist":
                    return self.send_json(404, {"error": "no such path"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n <= 0 or n > 1 << 20:
                        raise ValueError("CSV body is empty or too large")
                    raw = self.rfile.read(n)
                    text = raw.decode("utf-8-sig")
                    parsed = Whitelist.from_csv(text)
                    path = Path(controller.config.whitelist_path)
                    tmp = path.with_suffix(path.suffix + ".tmp")
                    tmp.write_text(text, encoding="utf-8")
                    tmp.replace(path)
                    controller.whitelist = parsed
                except (UnicodeDecodeError, ValueError, OSError) as exc:
                    return self.send_json(400, {"error": str(exc)})
                self.send_json(200, {"entries": len(parsed)})

            def do_POST(self):  # noqa: N802
                if self.path != "/gate/test":
                    return self.send_json(404, {"error": "no such path"})
                result = controller.manual_test()
                if result is None:
                    return self.send_json(503, {"error": "pulse failed"})
                self.send_json(200, result)

        return Handler

    def start(self):
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       name="gate-http", daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_address[1])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=2)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="NRD-3 R1124 gate service")
    parser.add_argument("--config", default="/etc/edge-parking/gate.json")
    args = parser.parse_args(argv)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    controller = None
    try:
        config = json.loads(Path(args.config).read_text(encoding="utf-8"))
        controller = GateController()
        controller.configure(config)
        controller.start_http(port=int(config["http"]["port"]))
        while not stop.wait(0.5):
            pass
        return 0
    finally:
        if controller is not None:
            controller.close()


if __name__ == "__main__":  # pragma: no cover - exercised by service smoke tests
    raise SystemExit(main())
