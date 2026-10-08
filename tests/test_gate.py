from __future__ import annotations

from pathlib import Path
import json
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor
import pytest

from core_parking.plate.gate import FakeGateOutput, GateController
from fakes import RecordingPublisher


CSV = "plate,label,valid_from,valid_until\n粤B12345,员工,,\n"
EVENT_ID = "00000000-0000-4000-8000-000000000001"


def _controller(tmp_path: Path, now=100.0):
    wl = tmp_path / "whitelist.csv"
    wl.write_text(CSV, encoding="utf-8")
    out = FakeGateOutput()
    c = GateController({"site_id": "demo-lot", "device_id": "r1124-gate",
                        "state_dir": str(tmp_path), "whitelist_path": str(wl),
                        "do_line": "unused", "pulse_ms": 500, "min_interval_s": 5,
                        "subscribe_cameras": ["gate-a"], "gate_id": "gate-a-barrier",
                        "http": {"port": 18081},
                        "mqtt": {"host": "localhost", "port": 1883, "username": "",
                                  "password": "", "keepalive_s": 30}}, output=out,
                       tx=RecordingPublisher(), clock=lambda: now)
    return c, out


def _event(ts=97000, **kw):
    ev = {"schema": "parking.plate/1", "event_id": EVENT_ID, "site_id": "demo-lot",
          "camera_id": "gate-a", "ts": ts, "seq": 0, "replayed": False,
          "plate": "粤B12345", "plate_normalized": "粤B12345", "region": "cn",
          "plate_type": "cn_blue", "confidence": .9, "votes": 1, "frames": 1,
          "bbox": [0, 0, 1, 1], "track_id": 1, "snapshot": None}
    ev.update(kw)
    return ev


def test_whitelist_hit_pulses_and_publishes(tmp_path):
    c, out = _controller(tmp_path)
    result = c.on_plate_event(_event(ts=97000))
    assert out.pulses == [500]
    assert result["action"] == "open_pulse"
    assert c.tx.records[0]["kind"] == "gate"


def test_replayed_expired_and_non_whitelist_do_not_open(tmp_path):
    c, out = _controller(tmp_path)
    assert c.on_plate_event(_event(replayed=True)) is None
    assert c.on_plate_event(_event(ts=95000)) is None
    assert c.on_plate_event(_event(plate_normalized="粤A12345")) is None
    assert out.pulses == []


def test_cooldown_is_suppressed(tmp_path):
    c, out = _controller(tmp_path)
    first = c.on_plate_event(_event())
    second = c.on_plate_event(_event(event_id="00000000-0000-4000-8000-000000000002", seq=1))
    assert first["action"] == "open_pulse"
    assert second["action"] == "suppressed"
    assert out.pulses == [500]
    assert [r["body"]["action"] for r in c.tx.records] == ["open_pulse", "suppressed"]


def test_output_exception_does_not_publish_open_event(tmp_path):
    c, out = _controller(tmp_path)
    def fail(_ms):
        raise RuntimeError("do failed")
    out.pulse = fail
    assert c.on_plate_event(_event()) is None
    assert c.tx.records == []


def test_manual_test_uses_cooldown_and_publishes_gate_event(tmp_path):
    c, out = _controller(tmp_path)
    first = c.manual_test()
    second = c.manual_test()
    assert first["trigger"] == "manual_test" and first["action"] == "open_pulse"
    assert second["action"] == "suppressed"
    assert out.pulses == [500]
    assert [r["body"]["trigger"] for r in c.tx.records] == ["manual_test", "manual_test"]


def test_invalid_contract_numbers_are_rejected(tmp_path):
    c, out = _controller(tmp_path)
    for key, value in (("confidence", -1), ("confidence", 2), ("ts", 97000.5),
                       ("track_id", -1), ("seq", -1), ("bbox", [0, 0, 2, 1])):
        ev = _event(event_id=str(__import__("uuid").uuid4()), **{key: value})
        assert c.on_plate_event(ev) is None
    assert out.pulses == []


def test_manual_pulses_are_serialized_under_concurrent_requests(tmp_path):
    c, out = _controller(tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: c.manual_test(), range(8)))
    assert len(out.pulses) == 1
    assert sum(r["action"] == "suppressed" for r in results if r) == 7


def test_mqtt_three_argument_callback_accepts_bytes(tmp_path):
    c, out = _controller(tmp_path)
    ev = _event()
    result = c.on_mqtt_message("demo-lot/parking/gate-a/plate",
                               json.dumps(ev).encode("utf-8"), False)
    assert result["action"] == "open_pulse"
    assert out.pulses == [500]


def test_gate_rejects_each_schema_violation_before_pulse(tmp_path):
    c, out = _controller(tmp_path)
    invalid = []
    for field in _event():
        event = _event()
        del event[field]
        invalid.append(event)
    for patch in ({"extra": True}, {"plate_type": "de"}, {"snapshot": {}},
                  {"snapshot": {"url": "x", "sha256": "0" * 64, "width": False, "height": 1}},
                  {"snapshot": {"url": "x", "sha256": "bad", "width": 1, "height": 1}},
                  {"event_id": "00000000000040008000000000000001"},
                  {"confidence": float("nan")}, {"confidence": 10 ** 400}):
        invalid.append(_event(**patch))
    for event in invalid:
        assert c.on_plate_event(event) is None
    assert out.pulses == []
    assert c.on_plate_event(_event(track_id=0))["action"] == "open_pulse"


def test_gate_config_schema_and_startup_reset(tmp_path):
    import copy
    import pytest
    c, out = _controller(tmp_path)
    before = out.reset_calls
    for location, key, value in ((None, "extra", 1), ("mqtt", "extra", 1),
                                 ("http", "extra", 1), ("mqtt", "port", 70000),
                                 (None, "subscribe_cameras", ("gate-a",)),
                                 (None, "min_interval_s", 10 ** 400)):
        options = copy.deepcopy(c.options)
        (options if location is None else options[location])[key] = value
        with pytest.raises(ValueError):
            c.configure(options)
        assert c.options["mqtt"]["port"] == 1883
    assert out.reset_calls == before + 6


def test_duplicate_events_are_serialized_even_without_cooldown(tmp_path):
    c, out = _controller(tmp_path)
    c.config.min_interval_s = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: c.on_plate_event(_event()), range(8)))
    assert out.pulses == [500]
    assert sum(result is not None for result in results) == 1


def test_seen_event_cache_evicts_oldest_id(tmp_path):
    c, _ = _controller(tmp_path)
    for index in range(10001):
        c._remember_event(str(index))
    assert len(c._seen_order) == len(c._seen_event_ids) == 10000
    assert "0" not in c._seen_event_ids
    assert "1" in c._seen_event_ids


def test_gpio_abi_startup_zero_and_line_lifetime(monkeypatch):
    import ctypes
    from core_parking.plate import gate
    calls = []
    class Function:
        def __init__(self, name, result):
            self.name, self.result = name, result
            self.argtypes = self.restype = None
        def __call__(self, *args):
            calls.append((self.name, args))
            return self.result
    class Library:
        pass
    lib = Library()
    chip, line = 0x1234567887654321, 0x1234567887654322
    for name, result in (("gpiod_chip_open_by_name", chip), ("gpiod_chip_get_line", line),
                         ("gpiod_line_request_output", 0), ("gpiod_line_set_value", 0),
                         ("gpiod_line_release", None), ("gpiod_chip_close", None)):
        setattr(lib, name, Function(name, result))
    monkeypatch.setattr(gate.ctypes.util, "find_library", lambda _: "mock")
    monkeypatch.setattr(gate.ctypes, "CDLL", lambda *a, **kw: lib)
    monkeypatch.setattr(gate.time, "sleep", lambda seconds: calls.append(("sleep", seconds)))
    output = gate.GpiodGateOutput("gpiochip0:4")
    assert lib.gpiod_chip_get_line.argtypes == [ctypes.c_void_p, ctypes.c_uint]
    assert lib.gpiod_line_request_output.argtypes == [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    for name in ("gpiod_line_set_value", "gpiod_line_release", "gpiod_chip_close"):
        assert getattr(lib, name).argtypes[0] == ctypes.c_void_p
    output.reset()
    assert calls[-1] == ("gpiod_line_set_value", (line, 0))
    output.pulse(500)
    assert calls[-3:] == [("gpiod_line_set_value", (line, 1)), ("sleep", .5),
                          ("gpiod_line_set_value", (line, 0))]
    assert not any(name == "gpiod_line_release" for name, _ in calls)
    output.close()
    assert calls[-2:] == [("gpiod_line_release", (line,)), ("gpiod_chip_close", (chip,))]
    count = len(calls)
    output.close()
    assert len(calls) == count


def test_constructor_failure_closes_new_resources(tmp_path, monkeypatch):
    import pytest
    from core_parking.plate import gate
    c, output = _controller(tmp_path)
    c.close()
    created = []
    class Publisher:
        def __init__(self, *args, **kwargs):
            self.started = False
            self.closed = 0
            created.append(self)
        def start(self):
            self.started = True
        def close(self):
            self.closed += 1
    monkeypatch.setattr(gate, "SpoolPublisher", Publisher)
    monkeypatch.setattr(gate, "GpiodGateOutput", lambda _: output)
    def subscribe(self):
        raise RuntimeError("subscribe failure")
    monkeypatch.setattr(GateController, "subscribe", subscribe)
    with pytest.raises(RuntimeError, match="subscribe failure"):
        GateController(c.options)
    assert len(created) == 1 and created[0].started and created[0].closed == 1


@pytest.mark.parametrize("subscribe_fails", [False, True])
def test_main_closes_real_publisher_and_subscriber(tmp_path, monkeypatch, subscribe_fails):
    import threading
    from core_parking.plate import gate
    from core_parking import transport
    import vision_base.mqtt
    controller, _ = _controller(tmp_path)
    options = controller.options
    controller.close()
    config = tmp_path / "config.json"
    config.write_text(json.dumps(options))
    clients = []
    class Client:
        def __init__(self, *args, **kwargs):
            self.connected = False
            self.closed = 0
            clients.append(self)
        def connect(self):
            self.connected = True
        def subscribe(self, *args, **kwargs):
            if subscribe_fails:
                raise RuntimeError("subscribe failure")
        def publish(self, *args, **kwargs):
            return True
        def close(self):
            self.closed += 1
            self.connected = False
    monkeypatch.setattr(transport, "default_client_factory", lambda *args: Client())
    monkeypatch.setattr(vision_base.mqtt, "MqttClient", Client)
    output = FakeGateOutput()
    monkeypatch.setattr(gate, "GpiodGateOutput", lambda _: output)
    monkeypatch.setattr(gate.signal, "signal", lambda *args: None)
    original_http = gate.GateHttp.__init__
    monkeypatch.setattr(gate.GateHttp, "__init__", lambda self, c, **kwargs:
                        original_http(self, c, host="127.0.0.1", port=0))
    real_event = threading.Event
    first = True
    class ImmediateStop:
        def wait(self, *args):
            return True
    def event_factory():
        nonlocal first
        if first:
            first = False
            return ImmediateStop()
        return real_event()
    # Only main's stop event is replaced; real publisher/thread events remain.
    monkeypatch.setattr(gate.threading, "Event", event_factory)
    before = set(threading.enumerate())
    if subscribe_fails:
        with pytest.raises(RuntimeError, match="subscribe failure"):
            gate.main(["--config", str(config)])
    else:
        assert gate.main(["--config", str(config)]) == 0
    assert [c.closed for c in clients] == [1, 1]
    assert not [t for t in threading.enumerate() if t not in before]
    assert output.reset_calls >= 1


def test_whitelist_put_reports_bad_row_and_keeps_previous_file(tmp_path):
    from urllib.error import HTTPError
    c, _ = _controller(tmp_path)
    http = c.start_http(port=0)
    try:
        request = Request(f"http://127.0.0.1:{http.port}/whitelist",
                          data=b"plate,label,valid_from,valid_until\nTEST,test,not-a-date,\n", method="PUT")
        with pytest.raises(HTTPError) as failure:
            urlopen(request, timeout=2)
        assert failure.value.code == 400
        detail = json.loads(failure.value.read())["error"]
        assert "2" in detail
        assert Path(c.config.whitelist_path).read_text() == CSV
        assert c.on_plate_event(_event())["action"] == "open_pulse"
    finally:
        c.close()
