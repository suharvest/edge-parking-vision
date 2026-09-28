#!/usr/bin/env bash
# NRD-4 P6 多路压测回放（规格 §4「多路压测用 mediamtx 回放同一份实拍视频
# 模拟 N 路」、§6.3；与 NRD-2 §S9 共用 tools/rtsp-fixture/ 目录）。
#
# 用法：
#   bash tools/rtsp-fixture/push_multi.sh <mp4> <N> [host] [base-name]
#   bash tools/rtsp-fixture/push_multi.sh evaluation/data/fixture/lot_300s.mp4 8 spark cam-b1
#
# 效果：mediamtx 收流（复用/自启容器，同 push.sh），N 个 ffmpeg -re 循环
# 推同一份视频到 rtsp://<host>:8554/<base>-01 … <base>-NN。Ctrl-C 全停。
# 依赖：docker（mediamtx 容器）、本机 ffmpeg。仅测试用途（mediamtx MIT）。
set -euo pipefail

FILE=${1:?"usage: push_multi.sh <mp4> <N> [host] [base-name]"}
N=${2:?"usage: push_multi.sh <mp4> <N> [host] [base-name]"}
HOST=${3:-127.0.0.1}
BASE=${4:-cam}
PORT=${PORT:-8554}
MTX_CONTAINER=${MTX_CONTAINER:-ep-mediamtx}
MEDIAMTX_IMAGE=${MEDIAMTX_IMAGE:-bluenviron/mediamtx:1.9.3}

if [ ! -f "$FILE" ]; then
  echo "push_multi.sh: 文件不存在: $FILE" >&2
  exit 1
fi
case "$N" in
  ''|*[!0-9]*) echo "push_multi.sh: N 必须为正整数: $N" >&2; exit 1 ;;
esac
if [ "$N" -lt 1 ]; then
  echo "push_multi.sh: N 必须 >= 1" >&2
  exit 1
fi

if ! docker ps --format '{{.Names}}' | grep -qx "$MTX_CONTAINER"; then
  echo "push_multi.sh: 启动 mediamtx 容器 $MTX_CONTAINER ($MEDIAMTX_IMAGE)" >&2
  docker run -d --rm --name "$MTX_CONTAINER" \
    -p "$PORT:8554" -p 8000:8000/udp "$MEDIAMTX_IMAGE" >/dev/null
  sleep 1
fi

PIDS=()
cleanup() {
  echo "push_multi.sh: 停止 ${#PIDS[@]} 路推流" >&2
  kill "${PIDS[@]}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for i in $(seq 1 "$N"); do
  name=$(printf "%s-%02d" "$BASE" "$i")
  echo "push_multi.sh: 推流 $FILE -> rtsp://$HOST:$PORT/$name （-re 循环）" >&2
  ffmpeg -hide_banner -loglevel warning -stream_loop -1 -re -i "$FILE" \
    -c copy -f rtsp "rtsp://$HOST:$PORT/$name" &
  PIDS+=($!)
done

echo "push_multi.sh: $N 路推流中（Ctrl-C 停止全部）" >&2
wait
