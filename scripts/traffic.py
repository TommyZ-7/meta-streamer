#!/usr/bin/env python3
"""Host traffic daily/monthly stats -> static public page (stdlib only).

Data sources (merged):
  1. `vnstat --json` recent days (~30 days retention) + monthly totals.
     vnstat is authoritative for overlapping dates (includes today in progress).
  2. `data/traffic-daily.csv` long-term history, appended by daily cron
     (`snapshot`). Needed because vnstat drops daily detail older than ~30d.

Privacy: host totals only (rx/tx bytes per day). No per-stream keys, no IPs.

Usage (called from cron on the VM, TZ=Asia/Tokyo):
  python3 scripts/traffic.py snapshot   # append/update today's row (23:55 daily)
  python3 scripts/traffic.py render     # rebuild public/index.html + traffic.json (*/5)

Env:
  TRAFFIC_IFACE  vnstat interface (default: auto = non-lo with max total)
  TRAFFIC_CSV    history csv (default: <repo>/data/traffic-daily.csv)
  PUBLIC_DIR     output dir (default: <repo>/public)
  VNSTAT_BIN     vnstat binary (default: vnstat)
"""

import csv
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CSV_PATH = Path(os.environ.get("TRAFFIC_CSV", REPO / "data" / "traffic-daily.csv"))
PUBLIC_DIR = Path(os.environ.get("PUBLIC_DIR", REPO / "public"))
OUT_HTML = PUBLIC_DIR / "index.html"
OUT_JSON = PUBLIC_DIR / "traffic.json"
VNSTAT_BIN = os.environ.get("VNSTAT_BIN", "vnstat")
IFACE_ENV = os.environ.get("TRAFFIC_IFACE", "")


def run_vnstat() -> dict:
    try:
        p = subprocess.run([VNSTAT_BIN, "--json"],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"[traffic] vnstat failed: {e}", flush=True)
        return {}
    if p.returncode != 0:
        print(f"[traffic] vnstat rc={p.returncode}: {p.stderr.strip()}", flush=True)
        return {}
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError as e:
        print(f"[traffic] vnstat json parse failed: {e}", flush=True)
        return {}


def _iface_total(iface: dict) -> int:
    tot = ((iface.get("traffic") or {}).get("total") or {})
    return int(tot.get("rx") or 0) + int(tot.get("tx") or 0)


def pick_iface(doc: dict) -> dict | None:
    ifaces = doc.get("interfaces") or []
    if IFACE_ENV:
        for i in ifaces:
            if i.get("name") == IFACE_ENV:
                return i
        return None
    cands = [i for i in ifaces if i.get("name") != "lo"]
    if not cands:
        return None
    return max(cands, key=_iface_total)


def vnstat_days(iface: dict) -> dict[str, tuple[int, int]]:
    """{YYYY-MM-DD: (rx, tx)} from vnstat day entries."""
    out: dict[str, tuple[int, int]] = {}
    days = ((iface.get("traffic") or {}).get("day") or [])
    for d in days:
        dt = d.get("date") or {}
        try:
            key = f"{int(dt['year']):04d}-{int(dt['month']):02d}-{int(dt['day']):02d}"
        except (KeyError, TypeError, ValueError):
            continue
        out[key] = (int(d.get("rx") or 0), int(d.get("tx") or 0))
    return out


def load_csv(path: Path, iface_name: str) -> dict[str, tuple[int, int]]:
    rows: dict[str, tuple[int, int]] = {}
    if not path.exists():
        return rows
    with path.open(newline="") as f:
        for r in csv.DictReader(f):
            if r.get("iface") != iface_name:
                continue
            try:
                rows[r["date"]] = (int(r["rx_bytes"]), int(r["tx_bytes"]))
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def save_snapshot(csv_path: Path, iface_name: str, day_map: dict) -> bool:
    today = datetime.now().strftime("%Y-%m-%d")
    if today not in day_map:
        print("[traffic] snapshot: today not in vnstat data", flush=True)
        return False
    rx, tx = day_map[today]
    rows: list[dict] = []
    if csv_path.exists():
        with csv_path.open(newline="") as f:
            rows = [r for r in csv.DictReader(f)
                    if not (r.get("date") == today and r.get("iface") == iface_name)]
    rows.append({"date": today, "iface": iface_name,
                 "rx_bytes": str(rx), "tx_bytes": str(tx)})
    rows.sort(key=lambda r: (r.get("date", ""), r.get("iface", "")))
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["date", "iface", "rx_bytes", "tx_bytes"])
        w.writeheader()
        w.writerows(rows)
    print(f"[traffic] snapshot: {today} {iface_name} rx={rx} tx={tx}", flush=True)
    return True


def merge(csv_rows: dict, live_days: dict) -> dict[str, tuple[int, int]]:
    return {**csv_rows, **live_days}  # vnstat wins on overlap


def build_months(merged: dict) -> dict[str, dict]:
    months: dict[str, dict] = {}
    for day, (rx, tx) in sorted(merged.items()):
        m = day[:7]
        e = months.setdefault(m, {"days": [], "rx": 0, "tx": 0})
        e["days"].append({"day": day, "rx": rx, "tx": tx})
        e["rx"] += rx
        e["tx"] += tx
    return months


def fmt_gb(v: int) -> str:
    return f"{v / 1e9:.2f}"


def render_html(payload: dict) -> str:
    data_json = json.dumps(payload, separators=(",", ":"))
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>meta-streamer 通信量</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:720px;margin:2em auto;padding:0 1em;color:#222}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:.3em .6em;text-align:right}}
td:first-child,th:first-child{{text-align:left}}tfoot td{{font-weight:bold}}
.muted{{color:#666;font-size:.9em}}
</style>
</head>
<body>
<h1>meta-streamer 通信量</h1>
<p class="muted">ホスト全体の日別送受信量 (JST, GB=10^9B)。更新: <span id="updated"></span> / IF: <span id="iface"></span></p>
<label>月: <select id="month"></select></label>
<table>
<thead><tr><th>日</th><th>受信(GB)</th><th>送信(GB)</th><th>合計(GB)</th></tr></thead>
<tbody id="rows"></tbody>
<tfoot><tr><td>月計</td><td id="t-rx"></td><td id="t-tx"></td><td id="t-all"></td></tr></tfoot>
</table>
<p class="muted">配信キー等の個別情報は含みません。<a href="traffic.json">JSON</a></p>
<script>
var D={data_json};
function gb(v){{return (v/1e9).toFixed(2)}}
var sel=document.getElementById("month");
Object.keys(D.months).sort().reverse().forEach(function(m){{
  var o=document.createElement("option");o.value=m;o.textContent=m;sel.appendChild(o);
}});
function show(){{
  var m=D.months[sel.value];var tb=document.getElementById("rows");tb.innerHTML="";
  m.days.forEach(function(d){{
    var tr=document.createElement("tr");
    tr.innerHTML="<td>"+d.day+"</td><td>"+gb(d.rx)+"</td><td>"+gb(d.tx)+"</td><td>"+gb(d.rx+d.tx)+"</td>";
    tb.appendChild(tr);
  }});
  document.getElementById("t-rx").textContent=gb(m.rx);
  document.getElementById("t-tx").textContent=gb(m.tx);
  document.getElementById("t-all").textContent=gb(m.rx+m.tx);
}}
sel.onchange=show;
document.getElementById("updated").textContent=D.updated;
document.getElementById("iface").textContent=D.iface;
show();
</script>
</body>
</html>
"""


def cmd_snapshot() -> int:
    doc = run_vnstat()
    iface = pick_iface(doc)
    if iface is None:
        print("[traffic] snapshot: no interface found", flush=True)
        return 1
    ok = save_snapshot(CSV_PATH, iface.get("name", ""), vnstat_days(iface))
    return 0 if ok else 1


def cmd_render() -> int:
    doc = run_vnstat()
    iface = pick_iface(doc)
    if iface is None:
        print("[traffic] render: no interface found", flush=True)
        return 1
    name = iface.get("name", "")
    merged = merge(load_csv(CSV_PATH, name), vnstat_days(iface))
    payload = {
        "updated": datetime.now().strftime("%Y-%m-%d %H:%M %Z"),
        "iface": name,
        "unit": "bytes",
        "months": build_months(merged),
    }
    PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(payload, indent=1))
    OUT_HTML.write_text(render_html(payload))
    print(f"[traffic] render: {len(merged)} days -> {OUT_HTML}", flush=True)
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in ("snapshot", "render"):
        print("usage: traffic.py [snapshot|render]", file=sys.stderr)
        sys.exit(2)
    sys.exit(cmd_snapshot() if sys.argv[1] == "snapshot" else cmd_render())
