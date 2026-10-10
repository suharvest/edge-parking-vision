"""停车业务 HTTP 端点（§S4，端口 ``app.options.http.port``，默认 8080）。

`ThreadingHTTPServer` + 标准库，Python 不碰像素（服务端不画图，JPEG 由
基座 ``ctx.request_snapshot`` 原生编码）。端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/events?limit=50` | 最近事件数组（`config.ParkingApp.recent_events`） |
| GET | `/preview` | 静态 `static/preview.html`（浏览器画框、计数线、读牌或车位状态） |
| GET | `/counting/editor` | 计数线编辑页（`static/counting_editor.html`）：拖线、交换进出、保存走 `PUT /config/counting` |
| GET | `/preview.jpg?stream=<id>` | 基座快照 JPEG（最近有框的那一帧，`X-Frame-Seq` 头给帧号），同一路 ≤ 1 次/秒 |
| GET | `/preview.json?stream=<id>` | 最近抽样 FrameResult 框（源归一化，`?seq=` 取指定帧）+ 计数线；slots 预设返回车位状态；车牌预设改为附 `plate` / `recent_reads` |
| GET/PUT | `/config/counting` | counting 段；PUT 校验后写 `app-options.override.json` 并即时生效，重启时读回 |
| GET | `/occupancy` | occupancy 快照（余位屏直连） |
| POST | `/occupancy` | `{"occupancy": int}` → `manual_set` 快照 |
| GET | `/slots/editor` | NRD-4 车位编辑 + 实时状态页（`slots/editor.html`，原生 JS canvas） |
| GET | `/snapshot/{stream}.jpg` | 基座快照 JPEG（编辑页背景，同一路 ≤ 1 次/秒） |
| GET/PUT | `/config/slots` | 车位表；PUT 校验后原子写 `slots.override.json` 并即时重配 |

slots 端点只在 app 提供 ``get_slots_config`` / ``set_slots``（如
`slots.app.SlotsApp`）时可用，否则 404。
"""

from __future__ import annotations

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

__all__ = ["ParkingHttp"]

log = logging.getLogger("core_parking.http_app")

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_PREVIEW_MIN_INTERVAL_S = 1.0    # 同一路 /preview.jpg ≤ 1 次/秒（§S4）
_PREVIEW_MAX_SIDE = 1280         # /preview.jpg 长边像素（runtime 默认 640，截图放大后发糊）
_RECENT_READS = 5                # /preview.json recent_reads 条数（车牌预设）

# 端口 → 存活的服务器。§S2 runtime.workers=1 时分片在未fork线程内运行，
# 分片崩溃重启会重新 load_app：新 ParkingApp 必须复用已在监听的服务器
# （旧实例未 close，直接再 bind 会 EADDRINUSE），并把 server.app 换成新实例。
_SERVERS: dict[int, "ParkingHttp._ServerRef"] = {}


def _clip01(v: float) -> float:
    return 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)


class _Handler(BaseHTTPRequestHandler):
    """路由 handler；``self.server.app`` 为 `config.ParkingApp`。"""

    server_version = "edge-parking-http/0.1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------- utilities

    @property
    def app(self):
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # 安静些：业务日志走 logging
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, ctype: str,
              headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json({"error": message}, code)

    def _stream_ctx(self, query: dict):
        """选流：?stream=<id>，缺省取第一路。返回 (ctx, stream_id) 或 None。"""
        streams = self.app._streams
        if not streams:
            return None
        sid = (query.get("stream") or [next(iter(streams))])[0]
        return streams.get(sid), sid

    def _read_json_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 1 << 20:
            raise ValueError("request body 为空或超过 1 MiB")
        return json.loads(self.rfile.read(n).decode("utf-8"))

    # -------------------------------------------------------------- routing

    def do_GET(self):  # noqa: N802
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/events":
                return self._get_events(q)
            if u.path == "/preview":
                return self._get_preview()
            if u.path == "/counting/editor":
                return self._get_counting_editor()
            if u.path == "/preview.jpg":
                return self._get_preview_jpg(q)
            if u.path == "/preview.json":
                return self._get_preview_json(q)
            if u.path == "/config/counting":
                return self._get_config_counting()
            if u.path == "/occupancy":
                return self._get_occupancy(q)
            if u.path == "/slots/editor":
                return self._get_slots_editor()
            if u.path == "/config/slots":
                return self._get_config_slots()
            if u.path.startswith("/snapshot/") and u.path.endswith(".jpg"):
                return self._get_snapshot_jpg(u.path)
            if u.path.startswith("/snapshots/") and u.path.endswith(".jpg"):
                return self._get_plate_snapshot_jpg(u.path)
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", u.path)
            return self._error(500, "internal error")
        self._error(404, f"no such path: {u.path}")

    def do_PUT(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/config/counting":
            return self._put_config_counting()
        if path == "/config/slots":
            return self._put_config_slots()
        self._error(404, "no such path")

    def do_POST(self):  # noqa: N802
        if urlparse(self.path).path != "/occupancy":
            return self._error(404, "no such path")
        self._post_occupancy()

    # -------------------------------------------------------------- handlers

    def _get_events(self, q: dict) -> None:
        try:
            limit = int((q.get("limit") or ["50"])[0])
        except ValueError:
            return self._error(400, "limit 必须为整数")
        limit = max(1, min(limit, self.app.recent_limit))
        self._json(list(self.app.recent_events)[-limit:])

    def _get_preview(self) -> None:
        page = (_STATIC_DIR / "preview.html").read_bytes()
        self._send(200, page, "text/html; charset=utf-8")

    def _get_counting_editor(self) -> None:
        if getattr(self.app, "plate_hooks", None) is not None or \
                not callable(getattr(self.app, "set_counting", None)):
            return self._error(404, "counting not configured on this app")
        page = (_STATIC_DIR / "counting_editor.html").read_bytes()
        self._send(200, page, "text/html; charset=utf-8")

    def _get_preview_jpg(self, q: dict) -> None:
        picked = self._stream_ctx(q)
        if picked is None:
            return self._error(404, "no stream")
        ctx, sid = picked
        self._send_snapshot(ctx, sid)

    def _send_snapshot(self, ctx, sid: str) -> None:
        now = time.monotonic()
        last = getattr(self.server, "_jpg_last", None) or {}
        if now - last.get(sid, 0.0) < _PREVIEW_MIN_INTERVAL_S:
            return self._error(429, "rate limit: 1 req/s per stream")
        # 取最近一次保存框的那一帧（runtime 环形缓冲按 seq 查找，找不到回落最新帧），
        # 回包 X-Frame-Seq 为实际帧号，页面据此取 /preview.json?seq= 的框，框与帧对齐。
        res = ctx.state.get("last")
        want = int(res.seq) if res is not None else 0
        meta, jpeg = ctx.request_snapshot(seq=want, track_id=0, crop=False,
                                          max_side=_PREVIEW_MAX_SIDE)
        last[sid] = now
        self.server._jpg_last = last
        headers = {}
        used = (meta or {}).get("seq") if isinstance(meta, dict) else None
        if isinstance(used, int) and not isinstance(used, bool):
            headers["X-Frame-Seq"] = used
        self._send(200, bytes(jpeg), "image/jpeg", headers)

    # -------------------------------------------------------------- slots（NRD-4 P3）

    def _get_slots_editor(self) -> None:
        page = (_STATIC_DIR.parent / "slots" / "editor.html").read_bytes()
        self._send(200, page, "text/html; charset=utf-8")

    def _get_snapshot_jpg(self, path: str) -> None:
        sid = path[len("/snapshot/"):-len(".jpg")]
        if not sid or "/" in sid:
            return self._error(404, f"no such path: {path}")
        ctx = getattr(self.app, "_streams", {}).get(sid)
        if ctx is None:
            return self._error(404, f"no stream: {sid}")
        self._send_snapshot(ctx, sid)

    def _get_plate_snapshot_jpg(self, path: str) -> None:
        raw = path[len("/snapshots/"):-len(".jpg")]
        parts = raw.split("/")
        if len(parts) == 2:
            camera_id, seq_text = parts
            if not camera_id or "/" in camera_id:
                return self._error(404, "invalid snapshot camera")
        elif len(parts) == 1:
            camera_id, seq_text = None, parts[0]
        else:
            return self._error(404, "invalid snapshot path")
        try:
            seq = int(seq_text)
        except ValueError:
            return self._error(404, "invalid snapshot sequence")
        if seq < 0 or str(seq) != seq_text:
            return self._error(404, "invalid snapshot sequence")
        owner = getattr(self.app, "plate_hooks", None)
        if owner is None:
            owner = getattr(self.app, "hooks", None)
        if owner is None and callable(getattr(self.app, "snapshot_path", None)):
            owner = self.app
        getter = getattr(owner, "snapshot_path", None)
        if callable(getter):
            try:
                path_obj = getter(seq, camera_id)
            except TypeError:
                path_obj = getter(seq)
        else:
            path_obj = None
        if path_obj is None:
            return self._error(404, "snapshot not found")
        try:
            data = Path(path_obj).read_bytes()
        except OSError:
            return self._error(404, "snapshot not found")
        self._send(200, data, "image/jpeg")

    def _get_config_slots(self) -> None:
        fn = getattr(self.app, "get_slots_config", None)
        if fn is None:
            return self._error(404, "slots not configured on this app")
        self._json(fn())

    def _put_config_slots(self) -> None:
        fn = getattr(self.app, "set_slots", None)
        if fn is None:
            return self._error(404, "slots not configured on this app")
        try:
            body = self._read_json_body()
            applied = fn(body)
        except ValueError as exc:      # json 解析失败与校验失败 → 400（可读原因）
            return self._error(400, f"invalid slots config: {exc}")
        except Exception as exc:       # ConfigureError / TimeoutError 等
            return self._error(503, f"apply failed: {type(exc).__name__}: {exc}")
        self._json(applied)

    def _get_preview_json(self, q: dict) -> None:
        from vision_base.letterbox import box_to_source_norm
        picked = self._stream_ctx(q)
        if picked is None:
            return self._error(404, "no stream")
        ctx, sid = picked
        if callable(getattr(self.app, "get_slots_config", None)):
            return self._json(self._slots_preview(ctx, sid))
        res = ctx.state.get("last")
        want = (q.get("seq") or [None])[0]
        if want is not None:
            try:
                want_seq = int(want)
            except ValueError:
                return self._error(400, "seq must be an integer")
            for r in reversed(list(ctx.state.get("frames") or ())):
                if r.seq == want_seq:
                    res = r
                    break
        plate_preset = getattr(self.app, "plate_hooks", None) is not None
        out = {"schema": "parking.preview/1", "stream_id": sid,
               "preset": "counting", "detections": []}
        if plate_preset:
            # 车牌预设没有计数线；附最近读牌结果，供页面在框旁标注
            reads_by_track, recent = self._plate_reads(sid)
            out["preset"] = "plate"
            out["line"] = None
            out["recent_reads"] = recent
        else:
            reads_by_track = {}
            out["line"] = {"a": list(self.app.counting.line["a"]),
                           "b": list(self.app.counting.line["b"])}
        if res is not None:
            out["seq"] = res.seq
            out["ts_ms"] = int(round(res.wall_ms))
            out["inference_ms"] = round(float(res.inference_ms), 6)
            dets = []
            for d in res.detections:
                scx, scy, sw, sh = box_to_source_norm(res.geom, d.cx, d.cy,
                                                      d.w, d.h)
                dets.append({
                    "track_id": d.track_id, "class_id": d.class_id,
                    "score": round(float(d.score), 6),
                    "box": [round(_clip01(scx - sw / 2), 6),
                            round(_clip01(scy - sh / 2), 6),
                            round(_clip01(scx + sw / 2), 6),
                            round(_clip01(scy + sh / 2), 6)],
                })
                read = reads_by_track.get(d.track_id)
                if read is not None:
                    dets[-1]["plate"] = read
            out["detections"] = dets
        self._json(out)

    def _plate_reads(self, sid: str) -> tuple[dict, list]:
        """从 /events 环形缓冲取本路的 ``parking.plate/1`` 事件。

        返回 (track_id → 该轨迹最新一次读牌, 最近 _RECENT_READS 次读牌，新→旧)。
        读牌经 dedup 后才发布，因此同一车牌在 dedup_s 内只出现一次。
        """
        by_track: dict = {}
        recent: list = []
        for env in reversed(list(getattr(self.app, "recent_events", ()))):
            if (not isinstance(env, dict) or env.get("schema") != "parking.plate/1"
                    or env.get("camera_id") != sid):
                continue
            read = {"ts_ms": env.get("ts"), "plate": env.get("plate"),
                    "plate_type": env.get("plate_type"),
                    "confidence": env.get("confidence")}
            if len(recent) < _RECENT_READS:
                recent.append(read)
            tid = env.get("track_id")
            if isinstance(tid, int) and tid not in by_track:
                by_track[tid] = read
        return by_track, recent

    def _slots_preview(self, ctx, sid: str) -> dict:
        """slots 预设：每个车位的多边形（源归一化）+ 当前状态与计数。"""
        polys = {s["id"]: s["polygon"]
                 for s in (self.app.get_slots_config().get(sid) or [])}
        lock = getattr(self.app, "_streams_lock", None)
        if lock is not None:
            with lock:
                states = {k: dict(v) for k, v in (ctx.state.get("slots") or {}).items()}
        else:
            states = {k: dict(v) for k, v in (ctx.state.get("slots") or {}).items()}
        slots, counts = [], {"occupied": 0, "free": 0, "unknown": 0}
        for slot_id, poly in polys.items():
            st = states.get(slot_id) or {}
            state = st.get("state", "unknown")
            if state not in counts:
                state = "unknown"
            counts[state] += 1
            slots.append({"id": slot_id, "polygon": poly, "state": state,
                          "since_ms": st.get("since"),
                          "cover": round(float(st.get("cover") or 0.0), 4)})
        return {"schema": "parking.preview/1", "stream_id": sid,
                "preset": "slots", "line": None, "detections": [],
                "slots": slots, "counts": counts}


    def _counting_dict(self) -> dict:
        c = self.app.counting
        return {"line": {"a": list(c.line["a"]), "b": list(c.line["b"])},
                "in_side": c.in_side, "count_directions": list(c.count_directions),
                "band": c.band, "segment_margin": c.segment_margin,
                "min_track_frames": c.min_track_frames, "min_travel": c.min_travel,
                "reborn_window_s": c.reborn_window_s, "reborn_dist": c.reborn_dist,
                "capacity": c.capacity, "initial_occupancy": c.initial_occupancy,
                "publish_interval_ms": c.publish_interval_ms}

    def _get_config_counting(self) -> None:
        self._json(self._counting_dict())

    def _put_config_counting(self) -> None:
        try:
            body = self._read_json_body()
            applied = self.app.set_counting(body)
        except ValueError as exc:      # json 解析失败与 ConfigError（子类）→ 400
            return self._error(400, f"invalid config: {exc}")
        except Exception as exc:       # ConfigureError / TimeoutError 等
            return self._error(503, f"apply failed: {type(exc).__name__}: {exc}")
        self._json(applied)

    def _get_occupancy(self, q: dict) -> None:
        picked = self._stream_ctx(q)
        if picked is None:
            return self._error(404, "no stream")
        ctx, sid = picked
        occ = ctx.state.get("occ")
        if occ is None:   # 当前预设未加载计数模块（slots / plate）
            return self._error(404, "occupancy not available: counting module not loaded")
        body = dict(occ.snapshot_body())
        body["stream_id"] = sid
        self._json(body)

    def _post_occupancy(self) -> None:
        picked = self._stream_ctx(parse_qs(urlparse(self.path).query))
        if picked is None:
            return self._error(404, "no stream")
        ctx, sid = picked
        occ = ctx.state.get("occ")
        if occ is None:   # 当前预设未加载计数模块（slots / plate）
            return self._error(404, "occupancy not available: counting module not loaded")
        try:
            body = self._read_json_body()
        except ValueError as exc:
            return self._error(400, f"invalid body: {exc}")
        if not isinstance(body, dict) or "occupancy" not in body:
            return self._error(400, "body 必须为 {\"occupancy\": int}")
        value = body["occupancy"]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return self._error(400, "occupancy 必须为 >= 0 的整数")
        operator = str(body.get("operator") or "http")
        reason = str(body.get("reason") or "manual_set")
        occ.manual_set(value, operator, reason)
        self.app._publish_occupancy(ctx, "manual_set")
        out = dict(occ.snapshot_body())
        out["stream_id"] = sid
        out["reason"] = "manual_set"
        self._json(out)


class ParkingHttp:
    """§S4 的 8080 业务端口；由 `config.ParkingApp.configure` 创建并启动。"""

    class _ServerRef:
        def __init__(self, server: ThreadingHTTPServer):
            self.server = server
            self.thread = None

    def __init__(self, app, *, host: str = "0.0.0.0", port: int | None = None) -> None:
        self.app = app
        opts = getattr(app, "options", {}) or {}
        self.host = str(opts.get("_http_host") or host)
        self.port = int((opts.get("http") or {}).get("port", 8080)
                        if port is None else port)
        self._ref = None

    def start(self) -> None:
        ref = _SERVERS.get(self.port)
        if ref is not None and ref.thread is not None and ref.thread.is_alive():
            # 分片重启：复用已在监听的服务器，改指到新的 app 实例
            ref.server.app = self.app          # type: ignore[attr-defined]
            self._ref = ref
            return
        import threading
        server = ThreadingHTTPServer((self.host, self.port), _Handler)
        server.daemon_threads = True
        server.app = self.app                  # type: ignore[attr-defined]
        server._jpg_last = {}                  # type: ignore[attr-defined]
        ref = self._ServerRef(server)
        ref.thread = threading.Thread(
            target=server.serve_forever, name="parking-http", daemon=True)
        ref.thread.start()
        _SERVERS[self.port] = ref
        self._ref = ref
        self.port = server.server_address[1]

    def stop(self) -> None:
        ref = self._ref
        if ref is None:
            return
        # 只有当前仍指向本 app 的服务器才停（新实例已接管时不动它）
        if getattr(ref.server, "app", None) is not self.app:
            self._ref = None
            return
        ref.server.shutdown()
        ref.server.server_close()
        if _SERVERS.get(self.port) is ref:
            del _SERVERS[self.port]
        if ref.thread is not None:
            ref.thread.join(timeout=5.0)
        self._ref = None
