#!/usr/bin/env python3
"""Bitrate watchdog for meta-streamer (MediaMTX sidecar).

Polls MediaMTX Control API `/v3/paths/list`, computes ingress bitrate
(video+audio combined) from `inboundBytes` growth, and kicks the publisher
when it significantly exceeds the limit for a sustained period.

Death countermeasures (fail-open must not go unnoticed):
  - per-cycle exception guard: one bad sample never kills the loop.
  - schema validation: unexpected API shape is a loud failure, not silence.
  - consecutive-failure exit: N failed polls in a row -> exit(1) so Docker
    `restart: unless-stopped` recycles a possibly wedged process.
  - heartbeat file + `--check`: Docker HEALTHCHECK watches freshness.
  - VM cron `scripts/watchdog-health.sh` restarts missing/unhealthy
    containers and optionally alerts via webhook.

Env:
  MTX_API_BASE   Control API base (default http://mediamtx:9997)
  MAX_TOTAL_KBPS Kick when combined ingress exceeds this (default 2500).
                 Nominal Topaz max is ~2320 (2000 video + 320 audio),
                 so 2500 gives small headroom for CBR overshoot / GOP bursts.
  POLL_INTERVAL  Seconds between polls (default 5)
  VIOLATION_LIMIT Consecutive over-limit samples before kick (default 3,
                 i.e. ~15s sustained)
  PATH_PREFIX    Only watch paths with this prefix (default live/)
  MAX_CONSECUTIVE_FAILURES Exit(1) after this many failed polls in a row
                 (default 12, i.e. ~60s) to trigger container restart.
  HEARTBEAT_FILE Path of heartbeat JSON (default /tmp/watchdog.heartbeat)
  HEARTBEAT_MAX_AGE `--check` fails if heartbeat older than this (default 90s)
"""

import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request

API_BASE = os.environ.get("MTX_API_BASE", "http://mediamtx:9997")
PATH_PREFIX = os.environ.get("PATH_PREFIX", "live/")
HEARTBEAT_FILE = os.environ.get("HEARTBEAT_FILE", "/tmp/watchdog.heartbeat")


def _fenv(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _ienv(name: str, default: int) -> int:
    try:
        return int(float(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


MAX_TOTAL_KBPS = _fenv("MAX_TOTAL_KBPS", 2500.0)
POLL_INTERVAL = _fenv("POLL_INTERVAL", 5.0)
VIOLATION_LIMIT = _ienv("VIOLATION_LIMIT", 3)
MAX_CONSECUTIVE_FAILURES = _ienv("MAX_CONSECUTIVE_FAILURES", 12)
HEARTBEAT_MAX_AGE = _fenv("HEARTBEAT_MAX_AGE", 90.0)

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


def validate_config() -> list[str]:
    """Clamp globals to sane values. Returns warnings (also logged)."""
    global MAX_TOTAL_KBPS, POLL_INTERVAL, VIOLATION_LIMIT
    global MAX_CONSECUTIVE_FAILURES, HEARTBEAT_MAX_AGE
    fixed = []
    if not MAX_TOTAL_KBPS > 0:
        fixed.append(f"MAX_TOTAL_KBPS={MAX_TOTAL_KBPS} -> 2500")
        MAX_TOTAL_KBPS = 2500.0
    if not POLL_INTERVAL > 0:
        fixed.append(f"POLL_INTERVAL={POLL_INTERVAL} -> 5")
        POLL_INTERVAL = 5.0
    if VIOLATION_LIMIT < 1:
        fixed.append(f"VIOLATION_LIMIT={VIOLATION_LIMIT} -> 3")
        VIOLATION_LIMIT = 3
    if MAX_CONSECUTIVE_FAILURES < 1:
        fixed.append(f"MAX_CONSECUTIVE_FAILURES={MAX_CONSECUTIVE_FAILURES} -> 12")
        MAX_CONSECUTIVE_FAILURES = 12
    if not HEARTBEAT_MAX_AGE > 0:
        fixed.append(f"HEARTBEAT_MAX_AGE={HEARTBEAT_MAX_AGE} -> 90")
        HEARTBEAT_MAX_AGE = 90.0
    for w in fixed:
        log(f"config clamped: {w}")
    return fixed


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


def poll_once(state: dict) -> tuple[bool, int]:
    """One poll cycle. Returns (ok, kicks).

    `ok` is False when the API could not be read or its shape is
    unexpected (loud failure -> counts toward restart threshold).
    """
    try:
        data = api_get("/v3/paths/list?itemsPerPage=1000")
    except Exception as e:  # network/API down: keep state, retry next tick
        log(f"paths/list failed: {e}")
        return False, 0
    if not isinstance(data, dict) or "items" not in data:
        log("paths/list failed: unexpected response shape")
        return False, 0

    now = time.time()
    seen: set[str] = set()
    kicks = 0

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
            if api_kick(stype, sid):
                kicks += 1
            state[name]["violations"] = 0  # avoid kick spam if path lingers

    # drop state for paths that disappeared
    for name in list(state):
        if name not in seen:
            state.pop(name, None)
    return True, kicks


def write_heartbeat(stats: dict) -> None:
    try:
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ts": time.time(), **stats}, f)
        os.replace(tmp, HEARTBEAT_FILE)
    except OSError as e:
        log(f"heartbeat write failed: {e}")


def heartbeat_fresh(now: float | None = None) -> bool:
    try:
        age = (time.time() if now is None else now) - os.path.getmtime(HEARTBEAT_FILE)
    except OSError:
        return False
    return 0 <= age <= HEARTBEAT_MAX_AGE


def healthcheck() -> int:
    """Exit 0 when alive, 1 otherwise (used by Docker HEALTHCHECK)."""
    if heartbeat_fresh():
        print("watchdog: alive")
        return 0
    print("watchdog: STALE or missing heartbeat", file=sys.stderr)
    return 1


def run() -> int:
    validate_config()
    log(f"start api={API_BASE} max={MAX_TOTAL_KBPS:.0f}kbps "
        f"interval={POLL_INTERVAL:g}s sustained={VIOLATION_LIMIT} prefix={PATH_PREFIX!r} "
        f"maxfail={MAX_CONSECUTIVE_FAILURES}")
    state: dict = {}
    failures = 0
    polls = 0
    kicks = 0
    write_heartbeat({"polls": polls, "kicks": kicks, "failures": failures})
    while True:
        try:
            ok, n = poll_once(state)
            polls += 1
            kicks += n
            failures = 0 if ok else failures + 1
        except Exception:  # must never die on one bad sample
            failures += 1
            log("poll crashed (continuing):\n" + traceback.format_exc(limit=3))
        write_heartbeat({"polls": polls, "kicks": kicks, "failures": failures})
        if failures >= MAX_CONSECUTIVE_FAILURES:
            log(f"fatal: {failures} consecutive failures, exiting for container restart")
            return 1
        time.sleep(POLL_INTERVAL)
    return 0  # unreachable


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--check":
        sys.exit(healthcheck())
    sys.exit(run())
