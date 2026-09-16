#!/usr/bin/env bash
# Provision Ubuntu 24.04 ARM (Oracle A1) for meta-streamer.
# Run once as ubuntu user with sudo: bash scripts/provision.sh
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/opt/meta-streamer}"

echo "[1/5] apt base + auto-upgrades + vnstat"
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg ufw unattended-upgrades vnstat
sudo systemctl enable --now unattended-upgrades
sudo systemctl enable --now vnstat || true

echo "[1.5/5] timezone JST (traffic daily page aggregates in JST)"
sudo timedatectl set-timezone Asia/Tokyo || true

echo "[2/5] docker (official repo)"
if ! command -v docker >/dev/null 2>&1; then
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  sudo usermod -aG docker "$USER" || true
fi
docker --version
docker compose version

echo "[3/5] ufw: 22/1935/554/8554/80 tcp + 8000-8001 udp (RTSP/RTP + traffic page)"
sudo ufw allow 22/tcp
sudo ufw allow 1935/tcp
sudo ufw allow 554/tcp
sudo ufw allow 8554/tcp
sudo ufw allow 80/tcp
sudo ufw allow 8000:8001/udp
sudo ufw --force enable
sudo ufw status verbose

echo "[4/5] deploy dir: ${DEPLOY_DIR}"
sudo mkdir -p "${DEPLOY_DIR}/scripts" "${DEPLOY_DIR}/public" "${DEPLOY_DIR}/data"
sudo chown -R "$USER":"$USER" "${DEPLOY_DIR}"
echo "Copy compose+yml+scripts+traffic conf+docs to ${DEPLOY_DIR} (keep layout), then:"
echo "  cd ${DEPLOY_DIR} && docker compose up -d && docker compose logs -f"

echo "[4.5/5] cron: traffic page render (*/5) + daily snapshot (23:55 JST) + watchdog health (*/2)"
CRON_RENDER="*/5 * * * * TZ=Asia/Tokyo python3 ${DEPLOY_DIR}/scripts/traffic.py render >> ${DEPLOY_DIR}/traffic-cron.log 2>&1"
CRON_SNAP="55 23 * * * TZ=Asia/Tokyo python3 ${DEPLOY_DIR}/scripts/traffic.py snapshot >> ${DEPLOY_DIR}/traffic-cron.log 2>&1"
CRON_WD="*/2 * * * * DEPLOY_DIR=${DEPLOY_DIR} bash ${DEPLOY_DIR}/scripts/watchdog-health.sh"
( crontab -l 2>/dev/null | grep -v 'scripts/traffic.py' | grep -v 'scripts/watchdog-health.sh' ; echo "$CRON_RENDER" ; echo "$CRON_SNAP" ; echo "$CRON_WD" ) | crontab -
crontab -l | grep -E 'scripts/(traffic.py|watchdog-health.sh)'

echo "[4.6/5] initial traffic page render (best effort; empty until vnstat has data)"
TZ=Asia/Tokyo python3 "${DEPLOY_DIR}/scripts/traffic.py" render >> "${DEPLOY_DIR}/traffic-cron.log" 2>&1 || true

echo "[5/5] done. Next: Oracle VCN ingress (same ports) + Cloudflare DNS (gray cloud)."
