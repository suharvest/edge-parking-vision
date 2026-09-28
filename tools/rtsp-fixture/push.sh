#!/usr/bin/env bash
# RTSP 回放推流（规格 §S9）：spark 上 MediaMTX 收流，ffmpeg -re 单次推流（不循环）。
#
# 用法：
#   bash tools/rtsp-fixture/push.sh evaluation/data/fixture/gate_120s.mp4 ep-gate [spark-host]
#   LOOP=1 bash tools/rtsp-fixture/push.sh tools/rtsp-fixture/truth_line.mp4 ep-truth
#
# 依赖：docker（mediamtx 容器）、本机 ffmpeg。
set -euo pipefail

FILE=${1:?"usage: push.sh <mp4> <stream-name> [host]"}
NAME=${2:?"usage: push.sh <mp4> <stream-name> [host]"}
HOST=${3:-127.0.0.1}
PORT=${PORT:-8554}
MTX_CONTAINER=${MTX_CONTAINER:-ep-mediamtx}
MEDIAMTX_IMAGE=${MEDIAMTX_IMAGE:-bluenviron/mediamtx:1.9.3}

if [ ! -f "$FILE" ]; then
  echo "push.sh: 文件不存在: $FILE" >&2
  exit 1
fi

# mediamtx 已在跑则复用（LOOP=1 循环推流时跨调用保持）
if ! docker ps --format '{{.Names}}' | grep -qx "$MTX_CONTAINER"; then
  echo "push.sh: 启动 mediamtx 容器 $MTX_CONTAINER ($MEDIAMTX_IMAGE)" >&2
  docker run -d --rm --name "$MTX_CONTAINER" \
    -p "$PORT:8554" -p 8000:8000/udp "$MEDIAMTX_IMAGE" >/dev/null
  sleep 1
fi

LOOP_ARGS=()
if [ "${LOOP:-0}" = "1" ]; then
  LOOP_ARGS=(-stream_loop -1)   # 真值回放：循环推流（§S9）
fi

echo "push.sh: 推流 $FILE -> rtsp://$HOST:$PORT/$NAME （-re${LOOP_ARGS:+ 循环}）" >&2
exec ffmpeg -hide_banner -loglevel warning "${LOOP_ARGS[@]}" -re -i "$FILE" \
  -c copy -f rtsp "rtsp://$HOST:$PORT/$NAME"
