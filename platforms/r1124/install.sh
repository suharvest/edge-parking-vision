#!/usr/bin/env bash
# install.sh — R1124-10: mosquitto broker (+ gate 应用环境) 安装，幂等（§S12）。
# 在 R1124 上以有 sudo 权限的用户执行：
#   ./install.sh [--broker-only] [--devices devices.csv]
# --broker-only：只装/配置 mosquitto broker，跳过 uv 与应用目录。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEVICES_CSV="${DEVICES_CSV:-$SCRIPT_DIR/devices.csv}"
BROKER_ONLY=0

while [ $# -gt 0 ]; do
  case "$1" in
    --broker-only) BROKER_ONLY=1; shift ;;
    --devices) DEVICES_CSV="$2"; shift 2 ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done

[ -f "$DEVICES_CSV" ] || { echo "devices csv not found: $DEVICES_CSV" >&2; exit 1; }

echo "==> apt: mosquitto + mosquitto-clients"
sudo apt-get update
sudo apt-get install -y mosquitto mosquitto-clients

echo "==> install broker config"
sudo install -m 0644 "$SCRIPT_DIR/mosquitto/edge-parking.conf" \
  /etc/mosquitto/conf.d/edge-parking.conf

echo "==> create passwords from $DEVICES_CSV"
first=1
while IFS=, read -r user role pw || [ -n "${user:-}" ]; do
  user="${user%%$'\r'}"; role="${role%%$'\r'}"
  case "$user" in ''|'#'*) continue ;; esac
  case "$role" in device|gate|operator|viewer) ;; *)
    echo "bad role '$role' for user '$user'" >&2; exit 1 ;;
  esac
  if [ -z "${pw:-}" ]; then
    pw="$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)"
    echo "    generated password for $user ($role): $pw"
  fi
  if [ "$first" -eq 1 ]; then
    sudo mosquitto_passwd -b -c /etc/mosquitto/edge-parking.passwd "$user" "$pw"
    first=0
  else
    sudo mosquitto_passwd -b /etc/mosquitto/edge-parking.passwd "$user" "$pw"
  fi
  unset pw
done < "$DEVICES_CSV"

echo "==> render ACL"
python3 "$SCRIPT_DIR/render_acl.py" "$DEVICES_CSV" "$SCRIPT_DIR/mosquitto/acl.tmpl" \
  | sudo tee /etc/mosquitto/edge-parking.acl >/dev/null

sudo chown root:mosquitto /etc/mosquitto/edge-parking.passwd /etc/mosquitto/edge-parking.acl
sudo chmod 0640 /etc/mosquitto/edge-parking.passwd /etc/mosquitto/edge-parking.acl

echo "==> (re)start mosquitto"
sudo systemctl enable --now mosquitto
sudo systemctl restart mosquitto

if [ "$BROKER_ONLY" -eq 1 ]; then
  echo "==> --broker-only: broker done"
  exit 0
fi

echo "==> install uv"
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

sudo install -d /opt/edge-parking /etc/edge-parking /var/lib/edge-parking

# 应用代码由上游 preset 下发到 /opt/edge-parking；已下发则同步依赖。
if [ -f /opt/edge-parking/pyproject.toml ]; then
  (cd /opt/edge-parking && uv sync --no-dev)
fi

# systemd 单元（NRD-3 gate-controller / plate-host）存在才安装。
for unit in "$SCRIPT_DIR"/systemd/*.service; do
  [ -e "$unit" ] || continue
  sudo install -m 0644 "$unit" /etc/systemd/system/
  sudo systemctl daemon-reload
  sudo systemctl enable --now "$(basename "$unit")"
done

echo "==> done"
