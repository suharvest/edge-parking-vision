"""停车业务 HTTP 端点（§S4，端口 ``app.options.http.port``，默认 8080）。

`ThreadingHTTPServer` + 标准库，Python 不碰像素（服务端不画图，JPEG 由
基座 ``ctx.request_snapshot`` 原生编码）。端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/events?limit=50` | 最近事件数组（`config.ParkingApp.recent_events`） |
| GET | `/preview` | 静态 `static/preview.html`（浏览器画框与计数线） |
| GET | `/preview.jpg?stream=<id>` | 基座快照 JPEG，同一路 ≤ 1 次/秒 |
| GET | `/preview.json?stream=<id>` | 最近抽样 FrameResult 框（源归一化）+ 计数线 |
| GET/PUT | `/config/counting` | counting 段；PUT 校验后写 override 并即时生效 |
| GET | `/occupancy` | occupancy 快照（余位屏直连） |
| POST | `/occupancy` | `{"occupancy": int}` → `manual_set` 快照 |
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

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
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
            if u.path == "/preview.jpg":
                return self._get_preview_jpg(q)
            if u.path == "/preview.json":
                return self._get_preview_json(q)
            if u.path == "/config/counting":
                return self._get_config_counting()
            if u.path == "/occupancy":
                return self._get_occupancy(q)
        except Exception:  # noqa: BLE001
            log.exception("GET %s failed", u.path)
            return self._error(500, "internal error")
        self._error(404, f"no such path: {u.path}")

    def do_PUT(self):  # noqa: N802
        if urlparse(self.path).path != "/config/counting":
            return self._error(404, "no such path")
        self._put_config_counting()

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

    def _get_preview_jpg(self, q: dict) -> None:
        picked = self._stream_ctx(q)
        if picked is None:
            return self._error(404, "no stream")
        ctx, sid = picked
        now = time.monotonic()
        last = getattr(self.server, "_jpg_last", None) or {}
        if now - last.get(sid, 0.0) < _PREVIEW_MIN_INTERVAL_S:
            return self._error(429, "rate limit: 1 req/s per stream")
        _meta, jpeg = ctx.request_snapshot(seq=0, track_id=0, crop=False)
        last[sid] = now
        self.server._jpg_last = last
        self._send(200, bytes(jpeg), "image/jpeg")

    def _get_preview_json(self, q: dict) -> None:
        from vision_base.letterbox import box_to_source_norm
        picked = self._stream_ctx(q)
        if picked is None:
            return self._error(404, "no stream")
        ctx, sid = picked
        res = ctx.state.get("last")
        out = {"schema": "parking.preview/1", "stream_id": sid,
               "line": {"a": list(self.app.counting.line["a"]),
                        "b": list(self.app.counting.line["b"])},
               "detections": []}
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
            out["detections"] = dets
        self._json(out)

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
        body = dict(ctx.state["occ"].snapshot_body())
        body["stream_id"] = sid
        self._json(body)

    def _post_occupancy(self) -> None:
        picked = self._stream_ctx(parse_qs(urlparse(self.path).query))
        if picked is None:
            return self._error(404, "no stream")
        ctx, sid = picked
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
        occ = ctx.state["occ"]
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
