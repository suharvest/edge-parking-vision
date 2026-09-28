#!/usr/bin/env bash
# test_acl.sh — 离线 ACL 验收（§S12）：docker 起带同一份 conf + 测试口令/ACL 的 broker，
# 断言四条规则。退出 0 = 全过。
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ETC="$ROOT/tests/r1124/etc"
NAME="ep-mqtt"
IMAGE="eclipse-mosquitto:2"
TMP="$(mktemp -d)"
FAIL=0

cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  rm -rf "$TMP"
}
trap cleanup EXIT

pass() { echo "PASS: $1"; }
fail() { echo "FAIL: $1"; FAIL=1; }

# --- 生成 ACL（用仓库里的 render_acl.py + acl.tmpl）与口令文件 ---
python3 "$ROOT/platforms/r1124/render_acl.py" \
  "$ETC/devices.csv" "$ROOT/platforms/r1124/mosquitto/acl.tmpl" \
  > "$TMP/edge-parking.acl" || { echo "render_acl failed"; exit 1; }

PW_CMD=""
while IFS=, read -r user role pw; do
  case "$user" in ''|'#'*) continue ;; esac
  if [ -z "$PW_CMD" ]; then
    PW_CMD="mosquitto_passwd -b -c /work/edge-parking.passwd '$user' '$pw'"
  else
    PW_CMD="$PW_CMD && mosquitto_passwd -b /work/edge-parking.passwd '$user' '$pw'"
  fi
done < "$ETC/devices.csv"
docker run --rm -v "$TMP:/work" "$IMAGE" sh -c "$PW_CMD" >/dev/null \
  || { echo "mosquitto_passwd failed"; exit 1; }
chmod 0644 "$TMP/edge-parking.acl" "$TMP/edge-parking.passwd"

# --- 起 broker：同一份 edge-parking.conf 原样挂载 ---
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p 18830:1883 \
  -v "$ETC:/mosquitto/config" \
  -v "$ROOT/platforms/r1124/mosquitto/edge-parking.conf:/etc/mosquitto/conf.d/edge-parking.conf:ro" \
  -v "$TMP/edge-parking.passwd:/etc/mosquitto/edge-parking.passwd:ro" \
  -v "$TMP/edge-parking.acl:/etc/mosquitto/edge-parking.acl:ro" \
  "$IMAGE" >/dev/null || { echo "docker run failed"; exit 1; }

# 等 broker 就绪
ready=0
for _ in $(seq 1 30); do
  if docker logs "$NAME" 2>&1 | grep -q "running"; then ready=1; break; fi
  sleep 1
done
[ "$ready" -eq 1 ] || { echo "broker not ready"; docker logs "$NAME"; exit 1; }

sub() { docker exec "$NAME" mosquitto_sub -h 127.0.0.1 -u "$1" -P "$2" -t "$3" -C 1 -W 5; }
pub() { docker exec "$NAME" mosquitto_pub -h 127.0.0.1 ${4:+-u "$4"} ${5:+-P "$5"} -t "$3" -m "$1" -q "$2"; }

# ① device 发 crossing，viewer 收到且退出 0
out="$TMP/a1.out"
sub viewer-1 view-pass 'demo-lot/parking/gate-a/crossing' >"$out" 2>&1 &
pid=$!
sleep 1
pub '{"schema":"parking.crossing/1"}' 1 'demo-lot/parking/gate-a/crossing' dev-a dev-pass
if wait "$pid" && grep -q 'parking.crossing/1' "$out"; then
  pass "① device→crossing, viewer 收到"
else
  fail "① device→crossing, viewer 未收到 (out: $(cat "$out"))"
fi

# ② device 发 gate（ACL 不允许写），viewer 5 s 内收不到且退出非 0、无输出
out="$TMP/a2.out"
pub '{"test":"gate"}' 1 'demo-lot/parking/gate-a/gate' dev-a dev-pass >/dev/null 2>&1
if sub viewer-1 view-pass 'demo-lot/parking/gate-a/gate' >"$out" 2>/dev/null; then
  fail "② viewer 收到了 device 发的 gate"
elif [ -s "$out" ]; then
  fail "② sub 有输出: $(cat "$out")"
else
  pass "② device→gate 被拒，viewer 5 s 收不到（退出非 0 且无输出）"
fi

# ③ 匿名连接被拒
out="$TMP/a3.out"
if pub '{"anon":1}' 1 'demo-lot/parking/gate-a/crossing' >"$out" 2>&1; then
  fail "③ 匿名发布成功"
else
  pass "③ 匿名连接被拒（mosquitto_pub 退出非 0）"
fi

# ④ operator 发 cmd/control，device 收到；viewer 发同 topic（只读，被拒），device 5 s 收不到
out="$TMP/a4a.out"
sub dev-a dev-pass 'demo-lot/parking/gate-a/cmd/control' >"$out" 2>&1 &
pid=$!
sleep 1
pub '{"schema":"vb.cmd_control/1"}' 1 'demo-lot/parking/gate-a/cmd/control' op-1 op-pass
if wait "$pid" && grep -q 'vb.cmd_control/1' "$out"; then
  pass "④a operator→cmd/control, device 收到"
else
  fail "④a device 未收到 operator 的 cmd/control (out: $(cat "$out"))"
fi

out="$TMP/a4b.out"
pub '{"bad":1}' 1 'demo-lot/parking/gate-a/cmd/control' viewer-1 view-pass >/dev/null 2>&1
if sub dev-a dev-pass 'demo-lot/parking/gate-a/cmd/control' >"$out" 2>/dev/null; then
  fail "④b device 收到了 viewer 发的 cmd/control"
elif [ -s "$out" ]; then
  fail "④b sub 有输出: $(cat "$out")"
else
  pass "④b viewer→cmd/control 被拒，device 5 s 收不到（退出非 0 且无输出）"
fi

echo "---"
if [ "$FAIL" -eq 0 ]; then
  echo "ALL ASSERTIONS PASSED"
  exit 0
else
  echo "SOME ASSERTIONS FAILED"
  exit 1
fi
