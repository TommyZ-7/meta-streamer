#!/usr/bin/env python3
"""Bitrate watchdog for meta-streamer (MediaMTX sidecar).

Polls MediaMTX Control API `/v3/paths/list`, computes ingress bitrate
(video+audio combined) from `inboundBytes` growth, and kicks the publisher
when it significantly exceeds the limit for a sustained period.

Why inboundBytes:
  - RTMP ingest is remuxed to RTSP without transcode, so the published
    bytes on a `live/*` path == video+audio combined ingress.
  - Per-track bitrate is not exposed by the API; byte-counter delta is
    the reliable signal.

Env:
  MTX_API_BASE   Control API base (default http://mediamtx:9997)
  MAX_TOTAL_KBPS Kick when combined ingress exceeds this (default 2500).
                 Nominal Topaz max is ~2320 (2000 video + 320 audio),
                 so 2500 gives small headroom for CBR overshoot / GOP bursts.
  POLL_INTERVAL  Seconds between polls (default 5)
  VIOLATION_LIMIT Consecutive over-limit samples before kick (default 3,
                 i.e. ~15s sustained)
  PATH_PREFIX    Only watch paths with this prefix (default live/)
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request

API_BASE = os.environ.get("MTX_API_BASE", "http://mediamtx:9997")
MAX_TOTAL_KBPS = float(os.environ.get("MAX_TOTAL_KBPS", "2500"))
POLL_INTERVAL = float(os.environ.get("POLL_INTERVAL", "5"))
VIOLATION_LIMIT = int(os.environ.get("VIOLATION_LIMIT", "3"))
PATH_PREFIX = os.environ.get("PATH_PREFIX", "live/")

# publisher source type -> kick endpoint (POST, no body)
KICK_ENDPOINTS = {
    "rtmpConn": "/v3/rtmp/conns/kick/{id}",
    "rtmpsConn": "/v3/rtmps/conns/kick/{id}",
    "rtspSession": "/v3/rtsp/sessions/kick/{id}",
    "rtspsSession": "/v3/rtsps/sessions/kick/{id}",
    "srtConn": "/v3/srt/conns/kick/{id}",
    "webRTCSession": "/v3/webrtc/sessions/kick/{id}",
    "moqSession": "/v3/moq/sessions/kick/{id}",
}


def log(msg: str) -> None:
    print(f"[watchdog] {msg}", flush=True)


def compute_kbps(prev_bytes: int, cur_bytes: int, dt: float) -> float:
    """Combined ingress kbps between two samples. Negative delta => 0."""
    if dt <= 0 or cur_bytes < prev_bytes:
        return 0.0
    return (cur_bytes - prev_bytes) * 8.0 / 1000.0 / dt


def kick_endpoint(source_type: str, source_id: str) -> str | None:
    tmpl = KICK_ENDPOINTS.get(source_type or "")
    if not tmpl or not source_id:
        return None
    return tmpl.format(id=source_id)


def should_watch(path_name: str, source: dict | None) -> bool:
    return (
        bool(path_name)
        and path_name.startswith(PATH_PREFIX)
        and isinstance(source, dict)
        and bool(source.get("id"))
    )


def api_get(path: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(API_BASE + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.load(res)


def api_kick(source_type: str, source_id: str, timeout: float = 10.0) -> bool:
    ep = kick_endpoint(source_type, source_id)
    if ep is None:
        log(f"skip kick: unsupported source type={source_type!r} id={source_id!r}")
        return False
    req = urllib.request.Request(API_BASE + ep, data=b"", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return 200 <= res.status < 300
    except urllib.error.HTTPError as e:
        if e.code == 404:
            log(f"already gone: {source_type} {source_id}")
            return True
        log(f"kick failed: {source_type} {source_id}: HTTP {e.code}")
        return False


def poll_once(state: dict) -> None:
    """One poll cycle. `state` maps path -> {prev_bytes, prev_time, violations}."""
    try:
        data = api_get("/v3/paths/list?itemsPerPage=1000")
    except Exception as e:  # network/API down: keep state, retry next tick
        log(f"paths/list failed: {e}")
        return

    now = time.time()
    seen: set[str] = set()

    for item in data.get("items", []):
        name = item.get("name", "")
        source = item.get("source")
        if not should_watch(name, source):
            continue
        seen.add(name)
        cur_bytes = int(item.get("inboundBytes") or 0)
        sid = source.get("id")
        stype = source.get("type")

        prev = state.get(name)
        if prev is None or cur_bytes < prev["prev_bytes"]:
            # first sight or republish (counter reset): need 2 samples
            state[name] = {"prev_bytes": cur_bytes, "prev_time": now, "violations": 0}
            continue

        kbps = compute_kbps(prev["prev_bytes"], cur_bytes, now - prev["prev_time"])
        violations = prev["violations"] + 1 if kbps > MAX_TOTAL_KBPS else 0
        state[name] = {"prev_bytes": cur_bytes, "prev_time": now, "violations": violations}

        if kbps > MAX_TOTAL_KBPS:
            log(f"over limit: path={name} {kbps:.0f}kbps > {MAX_TOTAL_KBPS:.0f} "
                f"({violations}/{VIOLATION_LIMIT}) src={stype}")
        if violations >= VIOLATION_LIMIT:
            log(f"KICK: path={name} sustained {kbps:.0f}kbps > {MAX_TOTAL_KBPS:.0f} "
                f"src={stype} id={sid}")
            api_kick(stype, sid)
            state[name]["violations"] = 0  # avoid kick spam if path lingers

    # drop state for paths that disappeared
    for name in list(state):
        if name not in seen:
            state.pop(name, None)


def main() -> None:
    log(f"start api={API_BASE} max={MAX_TOTAL_KBPS:.0f}kbps "
        f"interval={POLL_INTERVAL:g}s sustained={VIOLATION_LIMIT} prefix={PATH_PREFIX!r}")
    state: dict = {}
    while True:
        poll_once(state)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
