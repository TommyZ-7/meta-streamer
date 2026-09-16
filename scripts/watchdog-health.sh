#!/usr/bin/env bash
# VM-local supervisor for the bitrate watchdog (Docker does not restart
# unhealthy containers by itself). Run from cron every 2 min (provision.sh).
# Restarts meta-watchdog when missing, stopped, or unhealthy.
# Optionally alerts via ALERT_WEBHOOK_URL (Slack-compatible {"text":...}).
set -u

DEPLOY_DIR="${DEPLOY_DIR:-/opt/meta-streamer}"
LOG="${DEPLOY_DIR}/watchdog-health.log"
CONTAINER="meta-watchdog"

cd "${DEPLOY_DIR}" || exit 0
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

STATUS="$(docker inspect -f '{{.State.Health.Status}}' "${CONTAINER}" 2>/dev/null || echo missing)"
RUNNING="$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo false)"

if [ "${STATUS}" = "healthy" ] && [ "${RUNNING}" = "true" ]; then
  exit 0
fi

echo "$(date '+%F %T') watchdog status=${STATUS} running=${RUNNING} -> restart" >> "${LOG}"
if docker compose restart watchdog >> "${LOG}" 2>&1; then
  echo "$(date '+%F %T') watchdog restarted" >> "${LOG}"
else
  echo "$(date '+%F %T') watchdog restart FAILED" >> "${LOG}"
fi

if [ -n "${ALERT_WEBHOOK_URL:-}" ]; then
  curl -m 10 -s -X POST -H 'Content-Type: application/json' \
    -d "{\"text\":\"[meta-streamer] watchdog ${STATUS}/${RUNNING} -> restarted\"}" \
    "${ALERT_WEBHOOK_URL}" >/dev/null 2>&1 || true
fi
