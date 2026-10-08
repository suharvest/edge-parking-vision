from __future__ import annotations

from pathlib import Path
import hashlib
import json
from urllib.request import urlopen
import pytest

from core_parking.plate.hooks_plate import PlateHooks
from core_parking.http_app import ParkingHttp
from fakes import FakeStreamContext, RecordingPublisher, make_event


def _hooks(tmp_path: Path):
    h = PlateHooks()
    h.tx = RecordingPublisher()
    h.configure({"site_id": "demo-lot", "state_dir": str(tmp_path),
                 "mqtt": {"host": "localhost"},
                 "plate": {"region": "cn", "snapshot_dir": str(tmp_path / "snapshots"),
                            "snapshot_base_url": "http://camera:8080"}}, "dev-1")
    return h


def test_text_read_publishes_plate_and_snapshot(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    ctx.request_snapshot = lambda **_: ({"w": 1920, "h": 1080}, b"jpeg")
    h.on_stream_added(ctx)
    h.on_event(ctx, make_event(type="text_read", analyzer="text_vote", seq=7,
                               wall_ms=1000, track_id=4,
                               fields={"text": "粤B1234O", "min_char_conf": .93,
                                       "votes": 5, "frames": 7, "bbox": [.1, .2, .3, .4]}))
    record = h.tx.records[0]
    assert record["kind"] == "plate"
    assert record["body"]["plate_normalized"] == "粤B12340"
    assert record["body"]["plate_type"] == "cn_blue"
    assert record["body"]["snapshot"]["url"] == "http://camera:8080/snapshots/gate-a/0.jpg"
    assert record["body"]["snapshot"]["width"] == 1920
    assert record["body"]["snapshot"]["height"] == 1080
    assert (tmp_path / "snapshots" / "gate-a" / "0.jpg").read_bytes() == b"jpeg"


def test_snapshot_geometry_prefers_canonical_vbs1_keys(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    ctx.request_snapshot = lambda **_: ({"w": 1280, "h": 720,
                                         "width": 1, "height": 1}, b"jpeg")
    h.on_stream_added(ctx)
    h.on_event(ctx, make_event(type="text_read", wall_ms=1000, track_id=1,
                               fields={"text": "粤B12345", "min_char_conf": .9,
                                       "votes": 1, "frames": 1, "bbox": [0, 0, 1, 1]}))
    snapshot = h.tx.records[0]["body"]["snapshot"]
    assert (snapshot["width"], snapshot["height"]) == (1280, 720)


def test_snapshot_geometry_falls_back_to_legacy_keys(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    ctx.request_snapshot = lambda **_: ({"width": 640, "height": 480}, b"jpeg")
    h.on_stream_added(ctx)
    h.on_event(ctx, make_event(type="text_read", wall_ms=1000, track_id=1,
                               fields={"text": "粤B12345", "min_char_conf": .9,
                                       "votes": 1, "frames": 1, "bbox": [0, 0, 1, 1]}))
    snapshot = h.tx.records[0]["body"]["snapshot"]
    assert (snapshot["width"], snapshot["height"]) == (640, 480)


@pytest.mark.parametrize("meta", [
    {}, {"w": 1280}, {"h": 720}, {"w": True, "h": 720},
    {"w": 1280, "h": False}, {"w": 0, "h": 720}, {"w": 1280, "h": -1},
    {"w": 1280.0, "h": 720}, {"w": "1280", "h": 720},
])
def test_invalid_snapshot_geometry_publishes_null_without_persisting(tmp_path, meta):
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    ctx.request_snapshot = lambda **_: (meta, b"jpeg")
    h.on_stream_added(ctx)
    h.on_event(ctx, make_event(type="text_read", wall_ms=1000, track_id=1,
                               fields={"text": "粤B12345", "min_char_conf": .9,
                                       "votes": 1, "frames": 1, "bbox": [0, 0, 1, 1]}))
    assert h.tx.records[0]["body"]["snapshot"] is None
    assert not (tmp_path / "snapshots" / "gate-a" / "0.jpg").exists()
    assert h.health()["snapshot_misses"] == 1


def test_same_camera_plate_is_deduplicated_across_tracks(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    ctx.request_snapshot = lambda **_: ({"width": 1, "height": 1}, b"j")
    h.on_stream_added(ctx)
    for track in (1, 2):
        h.on_event(ctx, make_event(type="text_read", seq=track, wall_ms=1000,
                                   track_id=track, fields={"text": "粤B12345", "min_char_conf": .9,
                                                           "votes": 1, "frames": 1,
                                                           "bbox": [0, 0, 1, 1]}))
    assert len(h.tx.records) == 1


def test_plate_jsonl_replay_dedup_boundary(tmp_path, monkeypatch):
    from core_parking.plate import hooks_plate
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    ctx.request_snapshot = lambda **_: ({"width": 1, "height": 1}, b"j")
    h.on_stream_added(ctx)
    fixture = Path(__file__).parent / "fixtures/vbe/plate_dedup_boundary.jsonl"
    for line in fixture.read_text().splitlines():
        row = json.loads(line)
        monkeypatch.setattr(hooks_plate.time, "monotonic", lambda: row["mono_s"])
        before = len(h.tx.records)
        h.on_event(ctx, make_event(type="text_read", analyzer="text_vote",
                                   track_id=row["track_id"], seq=row["seq"], wall_ms=row["wall_ms"],
                                   fields={"text": row["text"], "min_char_conf": .9,
                                           "votes": 1, "frames": 1, "bbox": [0, 0, 1, 1]}))
        assert len(h.tx.records) - before == int(row["emit"])
    assert [r["ts"] for r in h.tx.records] == [1000, 3000, 5000]


def test_snapshot_failure_publishes_null_reference(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    ctx.request_snapshot = lambda **_: ({"ok": False}, b"")
    h.on_stream_added(ctx)
    h.on_event(ctx, make_event(type="text_read", seq=1, wall_ms=1000,
                               fields={"text": "粤B12345", "min_char_conf": .9,
                                       "votes": 1, "frames": 1,
                                       "bbox": [0, 0, 1, 1]}))
    assert h.tx.records[0]["body"]["snapshot"] is None
    assert h.health()["snapshot_misses"] == 1


def test_analyzers_use_existing_cn_charset(tmp_path):
    h = _hooks(tmp_path)
    full = h.analyzers(None)
    assert [x["name"] for x in full] == ["stage2", "text_vote"]
    assert full[0]["config"]["charset"][0] == ""
    assert full[0]["config"]["roi"] is None


def test_camera_scoped_snapshots_do_not_overwrite_and_http_serves_hashes(tmp_path):
    h = _hooks(tmp_path)
    contexts = []
    for camera, payload in (("gate-a", b"camera-a"), ("gate-b", b"camera-b")):
        ctx = FakeStreamContext(camera)
        ctx.request_snapshot = lambda payload=payload, **_: ({"width": 1, "height": 1}, payload)
        h.on_stream_added(ctx)
        h.on_event(ctx, make_event(type="text_read", wall_ms=1000, track_id=1,
                                   fields={"text": "粤B12345", "min_char_conf": .9,
                                           "votes": 1, "frames": 1, "bbox": [0, 0, 1, 1]}))
        contexts.append(ctx)
    refs = [r["body"]["snapshot"] for r in h.tx.records]
    assert refs[0]["url"] != refs[1]["url"]
    assert refs[0]["sha256"] != refs[1]["sha256"]
    assert (tmp_path / "snapshots" / "gate-a" / "0.jpg").read_bytes() == b"camera-a"
    assert (tmp_path / "snapshots" / "gate-b" / "0.jpg").read_bytes() == b"camera-b"
    assert refs[0]["sha256"] == hashlib.sha256(b"camera-a").hexdigest()
    app = type("App", (), {"options": {}, "plate_hooks": h, "_streams": {},
                            "recent_events": [], "recent_limit": 50})()
    http = ParkingHttp(app, host="127.0.0.1", port=0)
    http.start()
    try:
        body = urlopen(f"http://127.0.0.1:{http.port}/snapshots/gate-a/0.jpg").read()
        assert body == b"camera-a"
        assert hashlib.sha256(body).hexdigest() == refs[0]["sha256"]
    finally:
        http.stop()


def test_cleanup_includes_camera_directories_and_preserves_recent_files(tmp_path):
    import os
    import time
    h = _hooks(tmp_path)
    old = h.snapshot_dir / "gate-a" / "0.jpg"
    recent = h.snapshot_dir / "gate-b" / "0.jpg"
    for path in (old, recent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"jpeg")
    os.utime(old, (time.time() - 8 * 86400,) * 2)
    h._cleanup(3601)
    assert not old.exists()
    assert recent.read_bytes() == b"jpeg"
    h.plate["snapshot_max_mb"] = 0
    h._cleanup(7202)
    assert not recent.exists()


def test_invalid_read_does_not_poison_dedup_or_request_snapshot(tmp_path):
    h = _hooks(tmp_path)
    ctx = FakeStreamContext("gate-a")
    calls = []
    ctx.request_snapshot = lambda **_: (calls.append(1) or ({"width": 1, "height": 1}, b"j"))
    h.on_stream_added(ctx)
    fields = {"text": "粤B12345", "min_char_conf": .9, "votes": 1, "frames": 1, "bbox": [0, 0, 1, 1]}
    h.on_event(ctx, make_event(type="text_read", fields={**fields, "votes": False}))
    assert calls == [] and h.tx.records == []
    h.on_event(ctx, make_event(type="text_read", fields=fields))
    assert len(calls) == len(h.tx.records) == 1


def test_production_parking_app_forwards_plate_chain_and_serves_snapshot(tmp_path, monkeypatch):
    from core_parking.config import ParkingApp
    from tests.test_config import _counting_options
    options = _counting_options()
    del options["counting"]
    options["state_dir"] = str(tmp_path)
    options["plate"] = {"region": "cn", "snapshot_base_url": "http://unused", "mode": "full"}
    # The actual configure path uses the existing HTTP class, bound to an
    # ephemeral local port for this test without weakening production schema.
    original = ParkingHttp.__init__
    monkeypatch.setattr(ParkingHttp, "__init__", lambda self, app: original(self, app, host="127.0.0.1", port=0))
    app = ParkingApp()
    app.tx = RecordingPublisher()
    app.configure(options, "dev-1")
    try:
        assert [a["name"] for a in app.analyzers(None)] == ["stage2", "text_vote"]
        assert app.analyzers(None)[0]["config"]["roi"] is None
        assert app.analyzers(None)[0]["config"]["charset"][0] == ""
        assert app._timer is None
        for camera, data in (("gate-a", b"a"), ("gate-b", b"b")):
            ctx = FakeStreamContext(camera)
            ctx.request_snapshot = lambda data=data, **_: ({"width": 1, "height": 1}, data)
            app.on_stream_added(ctx)
            assert "occ" not in ctx.state
            app.on_event(ctx, make_event(type="text_read", wall_ms=1000,
                                        fields={"text": "粤B12345", "min_char_conf": .9, "votes": 1,
                                                "frames": 1, "bbox": [0, 0, 1, 1]}))
            reference = app.tx.records[-1]["body"]["snapshot"]
            payload = urlopen(f"http://127.0.0.1:{app.http.port}/snapshots/{camera}/0.jpg").read()
            assert payload == data and hashlib.sha256(payload).hexdigest() == reference["sha256"]
        assert [r["kind"] for r in app.tx.records] == ["plate", "plate"]
        app.plate_hooks.plate["mode"] = "detect_only"
        assert app.analyzers(None)[0]["name"] == "plate_crop_picker"
        assert app.plugins() == ["/opt/edge-parking/analyzers/libvb_plate_crop_picker.so"]
    finally:
        app.close()


def test_parking_app_default_publisher_connect_failure_closes_thread_and_client(tmp_path, monkeypatch):
    import threading
    import pytest
    from core_parking.config import ParkingApp
    from core_parking import transport
    from tests.test_config import _counting_options
    clients = []
    class FailingClient:
        connected = False
        def __init__(self):
            self.closed = 0
            clients.append(self)
        def connect(self):
            raise RuntimeError("connect failure")
        def close(self):
            self.closed += 1
    monkeypatch.setattr(transport, "default_client_factory", lambda *args: FailingClient())
    before = set(threading.enumerate())
    options = _counting_options()
    options["state_dir"] = str(tmp_path)
    app = ParkingApp()
    with pytest.raises(RuntimeError, match="connect failure"):
        app.configure(options, "dev-1")
    assert app.tx is None
    assert len(clients) == 1 and clients[0].closed == 1
    assert not [t for t in threading.enumerate() if t not in before and t.name == "spool-publisher"]
