"""NRD-3 车牌事件钩子。

识别、CTC 解码和多帧投票由 vb-runtime 完成；本模块只处理 ``text_read``
事件、快照引用和跨轨迹去重。发布仍经停车事件层的 SpoolPublisher。
"""
from __future__ import annotations

import hashlib
import logging
import math
import os
import time
from pathlib import Path

from ..envelope import SeqAllocator
from .charset import accept_patterns, charset, normalize, plate_type

log = logging.getLogger(__name__)


class PlateHooks:
    name = "edge-parking-plate/0.1.0"
    wants_frames = False

    def __init__(self) -> None:
        self.options: dict = {}
        self.device_id = ""
        self.plate: dict = {}
        self.tx = None
        self.state_dir = Path("state")
        self.snapshot_dir = Path("snapshots")
        self._last_emit: dict[tuple[str, str], float] = {}
        self._last_cleanup = 0.0
        self._snapshot_misses = 0
        self._streams: dict[str, object] = {}
        self._owns_tx = False
        self.http = None

    def configure(self, options: dict, device_id: str) -> None:
        self.options = dict(options or {})
        self.device_id = device_id
        self.plate = dict(self.options.get("plate") or {})
        if self.plate.get("region", "cn") != "cn":
            raise ValueError("plate.region must be 'cn' in NRD-3 P2")
        self.state_dir = Path(self.options.get("state_dir", "state"))
        self.snapshot_dir = Path(self.plate.get("snapshot_dir", self.state_dir / "snapshots"))
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        if self.tx is None:
            self.tx = self._make_default_tx()
            self._owns_tx = True
            start = getattr(self.tx, "start", None)
            if callable(start):
                start()

    def _make_default_tx(self):
        from ..transport import SpoolPublisher
        spool = dict(self.options.get("spool") or {})
        return SpoolPublisher(
            dict(self.options["mqtt"]),
            client_id=f"{self.options['site_id']}-{self.device_id}-app",
            site_id=self.options["site_id"],
            spool_path=self.state_dir / "spool.jsonl",
            max_events=int(spool.get("max_events", 50000)),
        )

    def close(self) -> None:
        if self._owns_tx:
            close = getattr(self.tx, "close", None)
            if callable(close):
                close()

    def snapshot_path(self, seq: int, camera_id: str | None = None) -> Path | None:
        """Return a safe existing snapshot path for the HTTP adapter."""
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            return None
        path = self.snapshot_dir / camera_id / f"{seq}.jpg" if camera_id else None
        if path is None:
            return None
        try:
            path.resolve().relative_to(self.snapshot_dir.resolve())
        except ValueError:
            return None
        return path if path.is_file() else None

    def analyzers(self, spec) -> list[dict]:
        region = self.plate.get("region", "cn")
        if self.plate.get("mode", "full") == "detect_only":
            cfg = dict(self.plate.get("crop_picker") or {})
            cfg["roi"] = self.plate.get("roi", [])
            cfg["classes"] = [0]
            return [{"name": "plate_crop_picker", "config": cfg}]
        stage2 = dict(self.plate.get("stage2") or {})
        symbols = list(charset(region))
        symbols[0] = ""  # BASE stage2 reserves the empty first symbol for blank.
        stage2.update({"roi": self.plate.get("roi") or None, "charset": symbols})
        vote = dict(self.plate.get("vote") or {})
        vote.setdefault("source", "stage2")
        vote.setdefault("accept", accept_patterns(region))
        return [{"name": "stage2", "config": stage2}, {"name": "text_vote", "config": vote}]

    def plugins(self) -> list[str]:
        if self.plate.get("mode", "full") == "detect_only":
            return ["/opt/edge-parking/analyzers/libvb_plate_crop_picker.so"]
        return []

    def on_stream_added(self, ctx) -> None:
        ctx.state["seq"] = SeqAllocator(self.state_dir / ctx.stream_id)
        self._streams[ctx.stream_id] = ctx

    def on_stream_removed(self, ctx) -> None:
        self._streams.pop(ctx.stream_id, None)

    def on_event(self, ctx, ev) -> list:
        if ev.type != "text_read":
            return []
        fields = dict(ev.fields or {})
        text = str(fields.get("text", ""))
        norm = normalize(text, "cn")
        ptype = plate_type(norm, "cn")
        if not norm or ptype is None:
            return []
        if (isinstance(ev.track_id, bool) or not isinstance(ev.track_id, int) or ev.track_id < 0 or
                isinstance(ev.seq, bool) or not isinstance(ev.seq, int) or ev.seq < 0 or
                isinstance(ev.wall_ms, bool) or not isinstance(ev.wall_ms, (int, float)) or
                not math.isfinite(float(ev.wall_ms))):
            return []
        try:
            confidence = fields["min_char_conf"]
            votes, frames = fields["votes"], fields["frames"]
            bbox = fields["bbox"]
            if (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or
                    not math.isfinite(float(confidence)) or not 0 <= confidence <= 1 or
                    isinstance(votes, bool) or not isinstance(votes, int) or votes < 1 or
                    isinstance(frames, bool) or not isinstance(frames, int) or frames < 1 or
                    not isinstance(bbox, list) or len(bbox) != 4 or any(
                        isinstance(x, bool) or not isinstance(x, (int, float)) or
                        not math.isfinite(float(x)) or not 0 <= x <= 1 for x in bbox)):
                return []
        except (KeyError, TypeError, ValueError, OverflowError):
            return []
        now = time.monotonic()
        dedup_s = float(self.plate.get("dedup_s", 30.0))
        key = (ctx.stream_id, norm)
        previous = self._last_emit.get(key)
        if previous is not None and now - previous < dedup_s:
            return []
        seq = ctx.state["seq"].next()
        snapshot = self._snapshot(ctx, ev, seq)
        self._cleanup(now)
        body = {
            "plate": text,
            "plate_normalized": norm,
            "region": "cn",
            "plate_type": ptype,
            "confidence": float(confidence), "votes": votes, "frames": frames,
            "bbox": list(bbox),
            "track_id": ev.track_id,
            "snapshot": snapshot,
        }
        self._publish(kind="plate", ctx=ctx, body=body, ts=int(ev.wall_ms), seq=seq)
        self._last_emit[key] = now
        return []

    def _publish(self, *, kind, ctx, body, ts, seq):
        try:
            return self.tx.publish(kind=kind, ctx=ctx, body=body, ts=ts, seq=seq)
        except TypeError as exc:
            # Keep the small P1 test publisher interface usable while the real
            # SpoolPublisher receives the explicit wire sequence above.
            if "seq" not in str(exc) and "unexpected keyword" not in str(exc):
                raise
            return self.tx.publish(kind=kind, ctx=ctx, body=body, ts=ts)

    def _snapshot(self, ctx, ev, seq: int) -> dict | None:
        try:
            result = ctx.request_snapshot(seq=0, track_id=0, crop=False)
        except Exception:
            self._snapshot_misses += 1
            log.warning("snapshot request failed", exc_info=True)
            return None
        if not result or len(result) != 2:
            self._snapshot_misses += 1
            return None
        meta, jpeg = result
        meta = dict(meta or {})
        if not isinstance(jpeg, (bytes, bytearray)) or not jpeg or meta.get("ok", True) is False:
            self._snapshot_misses += 1
            return None
        width = meta["w"] if "w" in meta else meta.get("width")
        height = meta["h"] if "h" in meta else meta.get("height")
        if (isinstance(width, bool) or not isinstance(width, int) or width < 1 or
                isinstance(height, bool) or not isinstance(height, int) or height < 1):
            self._snapshot_misses += 1
            log.warning("snapshot metadata has invalid geometry: w=%r h=%r", width, height)
            return None
        camera_dir = self.snapshot_dir / ctx.stream_id
        camera_dir.mkdir(parents=True, exist_ok=True)
        path = camera_dir / f"{seq}.jpg"
        tmp = path.with_suffix(".jpg.tmp")
        try:
            with open(tmp, "wb") as f:
                f.write(jpeg)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError:
            self._snapshot_misses += 1
            log.warning("snapshot write failed", exc_info=True)
            return None
        base = str(self.plate.get("snapshot_base_url", "")).rstrip("/")
        if not base:
            host = str((self.options.get("http") or {}).get("host", "localhost"))
            port = int((self.options.get("http") or {}).get("port", 8080))
            base = f"http://{host}:{port}"
        return {"url": f"{base}/snapshots/{ctx.stream_id}/{seq}.jpg",
                "sha256": hashlib.sha256(jpeg).hexdigest(),
                "width": width, "height": height}

    def _cleanup(self, now: float) -> None:
        if now - self._last_cleanup < 3600:
            return
        self._last_cleanup = now
        retention = int(self.plate.get("snapshot_retention_days", 7))
        cutoff = time.time() - retention * 86400
        files = self._snapshot_files()
        for p in files:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                log.warning("could not remove expired snapshot %s", p)
        max_bytes = int(self.plate.get("snapshot_max_mb", 2048)) * 1024 * 1024
        files = self._snapshot_files()
        total = sum(p.stat().st_size for p in files)
        for p in sorted(files, key=lambda x: x.stat().st_mtime):
            if total <= max_bytes:
                break
            size = p.stat().st_size
            try:
                p.unlink()
                total -= size
            except OSError:
                pass

    def _snapshot_files(self) -> list[Path]:
        root = self.snapshot_dir.resolve()
        return [p for pattern in ("*.jpg", "*/*.jpg") for p in self.snapshot_dir.glob(pattern)
                if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(root)]

    def health(self) -> dict:
        return {"spool_depth": getattr(self.tx, "spool_depth", None),
                "spool_overflow": getattr(self.tx, "spool_overflow", None),
                "mqtt_app_connected": getattr(self.tx, "mqtt_app_connected", None),
                "snapshot_misses": self._snapshot_misses}
