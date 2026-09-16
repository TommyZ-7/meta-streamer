# meta-streamer (Private)

TopazChat-compatible self-hosted relay. `ezStreamer` -> RTMP -> this server -> VRChat AVPro (`rtspt/rtsp`).

```
[ezStreamer] --RTMP 1935--> [live.meta-note-ex.com : MediaMTX] --RTSP/TCP 554--> [VRChat]
```

No transcode (remux only). Key model = Topaz (key is public, part of URL).

## URLs

| Use | URL |
|---|---|
| Ingest (ezStreamer/OBS) | `rtmp://live.meta-note-ex.com/live` + key `{key}` |
| Playback PC (AVPro Low Latency) | `rtspt://live.meta-note-ex.com/live/{key}` |
| Playback Quest | `rtsp://live.meta-note-ex.com/live/{key}` |
| Explicit-port fallback | `rtsp://live.meta-note-ex.com:8554/live/{key}` (`-rtsp_transport tcp`) |

Limits (Topaz parity, enforced server-side by watchdog): video <=2000k, audio <=320k, H.264+AAC, GOP 2s, B-frames 0.
Combined ingress >`MAX_TOTAL_KBPS` (default 2500k) sustained over `POLL_INTERVAL`x`VIOLATION_LIMIT` (~15s) is kicked.

## Prereqs

- Oracle Always Free A1 VM (Ubuntu 24.04 ARM, 1OCPU/6GB), public IP `<OCI_IP>`
- Cloudflare DNS for `meta-note-ex.com` (already in use)
- This repo cloned to `/opt/meta-streamer` on the VM

## 1. Network (two layers required)

Oracle VCN ingress (Security List / NSG), allow from `0.0.0.0/0`:

- `1935/tcp` RTMP, `554/tcp` RTSP, `8554/tcp` RTSP alias, `8000-8001/udp` RTP/RTCP, `22/tcp` SSH, `80/tcp` traffic page

Cloudflare DNS:

- `A live -> <OCI_IP>`, Proxy **OFF** (gray cloud, DNS only). Proxy (orange) breaks RTMP/RTSP.
- `A stats -> <OCI_IP>`, Proxy **ON** (orange cloud) for the public traffic page (`http://stats.meta-note-ex.com/`). HTTP only; no sensitive data.

VM firewall is set by `scripts/provision.sh` (UFW same ports).

## 2. Deploy

```bash
# on VM, first time
bash scripts/provision.sh
mkdir -p /opt/meta-streamer/scripts /opt/meta-streamer/public /opt/meta-streamer/data && cp docker-compose.yml mediamtx.yml /opt/meta-streamer/ && cp scripts/bitrate-watchdog.py scripts/traffic.py scripts/watchdog-health.sh /opt/meta-streamer/scripts/
cd /opt/meta-streamer && docker compose up -d && docker compose logs -f
```

Update: `cp` changed files the same way, then `docker compose pull && docker compose up -d`. Config is `mediamtx.yml` only; no DB.
Watchdog thresholds via `.env` (`MAX_TOTAL_KBPS`, `POLL_INTERVAL`, `VIOLATION_LIMIT`).

## 3. Test

```bash
# ingest (PC with ezStreamer or OBS: server rtmp://live.meta-note-ex.com/live, key test-123)
ffprobe -rtsp_transport tcp rtsp://live.meta-note-ex.com/live/test-123
# expect H.264 + AAC, ~2000k
```

VRChat: AVPro player, `Use Low Latency` ON, `Allow Untrusted URLs` ON (SDK3), URL `rtspt://live.meta-note-ex.com/live/test-123`, Global Sync/Resync to reconnect.

ezStreamer: set Ingest URL to `rtmp://live.meta-note-ex.com/live`; PC/Quest copy buttons work unchanged (no port in URL).

## 4. Ops

- Logs: `docker compose logs -f` (json-file 10m x3). No recordings (`record: false`).
- Watchdog: `docker compose logs -f watchdog` for `over limit` / `KICK` lines. Tunables in `.env.example`.
  Supervision: crash -> auto-restart (`unless-stopped`); 12 consecutive poll failures -> exit(1) for recycle;
  Docker HEALTHCHECK (heartbeat freshness) + VM cron `watchdog-health.sh` (*/2) restarts missing/unhealthy containers,
  optional `ALERT_WEBHOOK_URL` alert. Check with `docker inspect -f '{{.State.Health.Status}}' meta-watchdog`.
- Traffic page: public `http://stats.meta-note-ex.com/` (host totals/day, JST). Built by cron (`traffic.py render` every 5min, `snapshot` 23:55). History before deploy is limited to vnstat's ~30d daily retention; monthly totals go back 12 months.
- Monitor: UptimeRobot TCP `1935` + `554` (free). OCI Billing alarm recommended (egress ~0.9GB/h/viewer at 2M).
- OS auto-update: `unattended-upgrades` (provision.sh). MediaMTX: manual `pull` (pin `MTX_IMAGE` on release).
- Publish path is restricted to `live/*` (`mediamtx.yml`). Key collision = same as Topaz (use unique key).

## Troubleshooting

| Symptom | Check |
|---|---|
| RTMP connect fail | VCN ingress + `ufw status` + `docker compose ps`. Cloudflare must be gray cloud. |
| RTSP plays in VLC but gray in VRChat | Encoder preset: x264 `zerolatency` / NVENC Low Latency NG (Topaz parity). Use ezStreamer defaults. |
| `:554 permission denied` | `cap_add: NET_BIND_SERVICE` required (already in compose). |
| No port in URL fails, `:8554` works | Container `rtspAddress: :554` + host map `554:554` missing. `docker compose config` to verify. |
| High-bitrate publisher not kicked | `docker compose logs watchdog` + `curl http://127.0.0.1:9997/v3/paths/list` on VM via `docker compose exec mediamtx` network. Check `.env` thresholds. |
| Watchdog `paths/list failed` | `mediamtx` not ready or `api: true` missing. `docker compose logs mediamtx`. API is compose-internal only (9997 unpublished). |

## P1 (not in MVP)

- Publish auth (password/JWT) + RTSPS (`:8322`) + HLS preview. See `mediamtx.yml` comments.
