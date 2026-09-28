#!/usr/bin/env python3
"""合成真值回放视频与轨迹表（规格 §S9「真值回放」行、§8.1 C4）。

生成 `truth_line.mp4`：纯色背景上一个车辆图块按脚本轨迹水平移动；
计数线为 x=0.5 竖线。同时输出 `truth.json`，记录每次越线的时刻与方向
（含一次正好压线停留 ≥5 帧再离开，只记 1 次越线）。

轨迹（fps=10，640×360，图块中心 x 归一化）：
  1. 0.15 → 0.85（left→right，越线 1 次，direction=in）
  2. 0.85 → 0.15（right→left，越线 1 次，direction=out）
  3. 0.30 → 0.50，压线停留 8 帧，退回 0.30（只计 1 次，direction=in）
  4. 0.20 → 0.80（direction=in）

用法：
  uv run python tools/rtsp-fixture/make_truth.py \
      [--out-video tools/rtsp-fixture/truth_line.mp4] \
      [--out-truth tools/rtsp-fixture/truth.json]

视频编码调本机 ffmpeg（rawvideo 管道输入，mpeg4 编码，容器上可用 -c copy 推流）。
truth.json 无论 ffmpeg 是否存在都会写出。仅标准库。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

FPS = 10
WIDTH, HEIGHT = 640, 360
RECT_W, RECT_H = 80, 44          # 车辆图块（像素）
RECT_Y = HEIGHT // 2 - RECT_H // 2
BG = (52, 58, 64)                # 深灰背景
FG = (198, 60, 48)               # 车辆图块
LINE_COLOR = (240, 240, 240)

# 轨迹段：(起始 x, 结束 x, 持续 s, 段末停留帧数)
SEGMENTS = [
    (0.15, 0.85, 6.0, FPS),      # 越线 in，末停 1 s
    (0.85, 0.15, 6.0, FPS),      # 越线 out，末停 1 s
    (0.30, 0.50, 2.0, 8),        # 压线停留 8 帧（≥5）
    (0.50, 0.30, 1.0, FPS // 2), # 退回（不再记越线；压线段整体只计 1 次 in）
    (0.30, 0.20, 0.5, FPS // 2),
    (0.20, 0.80, 5.0, FPS),      # 越线 in
]
DWELL_SEGMENT = 2                # 压线停留所在段（truth 里记 1 次越线）


def build_frames() -> tuple[list[float], list[dict]]:
    """逐帧中心 x（归一化）+ 解析出的真值越线表。"""
    xs: list[float] = []
    for x0, x1, dur_s, hold in SEGMENTS:
        n = max(2, int(round(dur_s * FPS)))
        for i in range(n):
            xs.append(x0 + (x1 - x0) * i / (n - 1))
        xs.extend([x1] * hold)

    crossings: list[dict] = []
    for i in range(1, len(xs)):
        prev, cur = xs[i - 1], xs[i]
        if prev < 0.5 <= cur:
            crossings.append({"frame": i, "t_ms": round(i * 1000 / FPS), "direction": "in"})
        elif prev > 0.5 >= cur:
            crossings.append({"frame": i, "t_ms": round(i * 1000 / FPS), "direction": "out"})
    # 压线停留：到达 0.5 的那一帧记为一次 in，停留与退回不产生新记录
    # （SEGMENTS 第 3 段末 x=0.5，prev<0.5<=cur 已记 1 次；退回段 prev=0.5 不触发）。
    for c in crossings:
        del c["frame"]
    crossings[2]["note"] = "dwell: 压线停留 8 帧再退回，只计 1 次"
    return xs, crossings


def render_frame(x_center: float) -> bytes:
    cx = int(round(x_center * WIDTH))
    x0 = max(0, cx - RECT_W // 2)
    x1 = min(WIDTH, cx + RECT_W // 2)
    buf = bytearray(BG * (WIDTH * HEIGHT))
    # 计数线 x=0.5
    lx = WIDTH // 2
    for y in range(HEIGHT):
        o = (y * WIDTH + lx) * 3
        buf[o:o + 3] = bytes(LINE_COLOR)
    for y in range(RECT_Y, RECT_Y + RECT_H):
        o = (y * WIDTH + x0) * 3
        buf[o:o + (x1 - x0) * 3] = bytes(FG) * (x1 - x0)
    return bytes(buf)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    here = Path(__file__).resolve().parent
    ap.add_argument("--out-video", default=str(here / "truth_line.mp4"))
    ap.add_argument("--out-truth", default=str(here / "truth.json"))
    args = ap.parse_args(argv)

    xs, crossings = build_frames()
    truth = {
        "video": Path(args.out_video).name,
        "fps": FPS,
        "width": WIDTH,
        "height": HEIGHT,
        "line": {"a": [0.5, 0.0], "b": [0.5, 1.0]},
        "note": "合成真值回放：车辆图块水平移动，计数线 x=0.5 竖线；"
                "direction: left->right=in, right->left=out",
        "crossings": crossings,
    }
    truth_path = Path(args.out_truth)
    truth_path.parent.mkdir(parents=True, exist_ok=True)
    with open(truth_path, "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False, indent=2)
    print(f"truth.json -> {truth_path}（{len(crossings)} 条越线，{len(xs)} 帧）")

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        print("make_truth.py: 未找到 ffmpeg，跳过视频生成（truth.json 已写出）", file=sys.stderr)
        return 0
    video_path = Path(args.out_video)
    video_path.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{WIDTH}x{HEIGHT}",
         "-r", str(FPS), "-i", "-",
         "-c:v", "mpeg4", "-q:v", "5", "-pix_fmt", "yuv420p", str(video_path)],
        input=b"".join(render_frame(x) for x in xs),
    )
    if proc.returncode != 0:
        print("make_truth.py: ffmpeg 编码失败", file=sys.stderr)
        return 1
    print(f"video -> {video_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
