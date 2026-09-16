#!/usr/bin/env bash
# MediaMTX Control API helper via the watchdog container (API is not published).
# Usage:
#   scripts/mtx-api.sh GET  /v3/paths/list
#   scripts/mtx-api.sh POST /v3/rtmp/conns/kick/<id>
set -euo pipefail
METHOD="${1:?usage: mtx-api.sh GET|POST /v3/...}"
PATH_="${2:?usage: mtx-api.sh GET|POST /v3/...}"
docker compose exec -T watchdog python3 - "$METHOD" "$PATH_" <<'EOF'
import json, sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
req = urllib.request.Request("http://mediamtx:9997" + path,
                             data=b"" if method == "POST" else None,
                             method=method)
try:
    with urllib.request.urlopen(req, timeout=10) as res:
        print(json.dumps(json.load(res), indent=1, ensure_ascii=False))
except urllib.error.HTTPError as e:
    print(f"HTTP {e.code}: {e.read().decode()[:500]}", file=sys.stderr)
    sys.exit(1)
EOF
