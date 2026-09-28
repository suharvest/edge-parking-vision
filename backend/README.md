# NRD-2 P7 —— 演示看板（可选，仅演示）

规格：`docs/rd-specs/NRD-2-parking-vehicle-counting.md` §8.3 P7、§S3（信封）、§S12（R1124 broker 与 ACL）。

**此目录只作演示，不作产品能力。**

组成：telegraf + InfluxDB 2 + Grafana，**不含 broker**——正式部署时 broker 是 R1124 上的
systemd mosquitto（§S12），telegraf 以 `viewer` 角色连接（ACL 只读 `+/parking/#`），
订阅 `+/parking/+/occupancy`，把 §S3 的 `parking.occupancy/1` 信封写入 InfluxDB。

## 运行

在 R1124（或能连到 broker 的机器）上：

```bash
MQTT_HOST=r1124.local MQTT_USERNAME=viewer MQTT_PASSWORD=<viewer口令> \
docker compose -f backend/docker-compose.yml up -d
```

Grafana 在 `:3000`（默认 demo/demodemo，用环境变量改）。看板已随目录
`grafana/dashboards/parking-occupancy.json` 自动 provisioning。

## 本地验收（无真机时）

```bash
bash backend/publish_fixture.sh   # 起替身 broker、灌 fixture、查 Influx
```
