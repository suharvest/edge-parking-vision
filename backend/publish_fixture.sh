#!/usr/bin/env bash
# NRD-2 P7 演示看板本地验收：起替身 broker（viewer/device 角色 ACL 同 §S12）、
# 起看板 compose、以 device 角色灌 fixture occupancy、查询 InfluxDB 确认有数据。
# 仅本机验收用；正式部署 broker 在 R1124（§S12）。
set -euo pipefail
cd "$(dirname "$0")/.."

BROKER=ep-demo-broker
MQTT_PORT_LOCAL=18831
CLEANUP=0
finish() { [ "$CLEANUP" = 1 ] && docker compose -f backend/docker-compose.yml down -v >/dev/null 2>&1; docker rm -f "$BROKER" >/dev/null 2>&1 || true; }
trap finish EXIT

# ---------- 替身 broker：ACL 与 §S12 acl.tmpl 的 viewer/device 角色一致 ----------
TD=$(mktemp -d)
cat > "$TD/mosquitto.conf" <<'EOF'
listener 1883 0.0.0.0
allow_anonymous false
password_file /mosquitto/config/passwd
acl_file /mosquitto/config/acl
EOF
cat > "$TD/acl" <<'EOF'
user viewer
topic read +/parking/#
user device1
topic write +/parking/+/occupancy
topic write +/parking/+/status
EOF
docker run --rm -v "$TD":/w eclipse-mosquitto:2 mosquitto_passwd -b -c /w/passwd viewer viewerpw >/dev/null
docker run --rm -v "$TD":/w eclipse-mosquitto:2 mosquitto_passwd -b /w/passwd device1 devicepw >/dev/null
docker rm -f "$BROKER" >/dev/null 2>&1 || true
docker run -d --name "$BROKER" -p 127.0.0.1:${MQTT_PORT_LOCAL}:1883 \
  -v "$TD":/mosquitto/config eclipse-mosquitto:2 >/dev/null
sleep 2

# ---------- 看板 compose（telegraf 以 viewer 角色连替身 broker） ----------
export MQTT_HOST=host.docker.internal MQTT_PORT=${MQTT_PORT_LOCAL}
export MQTT_USERNAME=viewer MQTT_PASSWORD=viewerpw
export INFLUX_TOKEN=demo-token-not-for-production
CLEANUP=1
docker compose -f backend/docker-compose.yml up -d --quiet-pull >/dev/null
echo "waiting for telegraf..."
for i in $(seq 1 60); do
  docker compose -f backend/docker-compose.yml ps telegraf | grep -q "running\|healthy" && \
    c=$(docker compose -f backend/docker-compose.yml logs telegraf 2>&1 | grep -c "MQTT connection established" || true); \
  [ "${c:-0}" -ge 1 ] && break
  sleep 2
done

# ---------- 以 device 角色灌 fixture（§S3 信封 + occupancy 字段） ----------
pub() {  # site camera occupancy capacity free in out reason replayed
  docker exec "$BROKER" mosquitto_pub -h 127.0.0.1 -u device1 -P devicepw \
    -t "$1/parking/$2/occupancy" -q 1 -m "{\"schema\":\"parking.occupancy/1\",\"event_id\":\"$3\",\"site_id\":\"$1\",\"camera_id\":\"$2\",\"ts\":$(date +%s)000,\"seq\":$4,\"replayed\":false,\"occupancy\":$5,\"capacity\":$6,\"free\":$7,\"in_today\":$8,\"out_today\":$9,\"reason\":\"${10}\"}"
}
for i in 1 2 3 4 5; do
  pub demo-lot gate-a "$(uuidgen)" $i $((10+i)) 50 $((40-i)) $((30+i)) $((25+i)) crossing
  sleep 1
done

sleep 8   # 等 telegraf flush

# ---------- Influx 查询 ----------
docker compose -f backend/docker-compose.yml exec -T influxdb \
  influx query --org edge-parking 'from(bucket:"parking") |> range(start:-5m) |> filter(fn:(r)=> r._measurement=="mqtt_consumer" and r._field=="occupancy") |> keep(columns:["_time","site_id","camera_id","_field","_value"]) |> sort(columns:["_time"])'
