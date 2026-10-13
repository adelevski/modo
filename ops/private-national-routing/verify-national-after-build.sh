#!/usr/bin/env bash
set -Eeuo pipefail

BASE_URL="${VALHALLA_URL:-http://127.0.0.1:8003}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl --fail --silent --show-error "$BASE_URL/status" >"$work/status.json"

cat >"$work/matrix-request.json" <<'JSON'
{
  "sources": [
    {"lat": 47.6062, "lon": -122.3321},
    {"lat": 40.7128, "lon": -74.0060},
    {"lat": 25.7617, "lon": -80.1918},
    {"lat": 34.0522, "lon": -118.2437}
  ],
  "targets": [
    {"lat": 41.665534, "lon": -87.960593},
    {"lat": 39.770209, "lon": -89.640643},
    {"lat": 39.905702, "lon": -91.358706}
  ],
  "costing": "auto",
  "units": "miles",
  "verbose": true
}
JSON

curl --fail --silent --show-error \
  --header 'Content-Type: application/json' \
  --data-binary @"$work/matrix-request.json" \
  "$BASE_URL/sources_to_targets" >"$work/matrix.json"

python3 - "$BASE_URL" "$work" <<'PY'
import json
import pathlib
import subprocess
import sys

base, work = sys.argv[1], pathlib.Path(sys.argv[2])
matrix = json.loads((work / "matrix.json").read_text())
rows = matrix["sources_to_targets"]
assert len(rows) == 4 and all(len(row) == 3 for row in rows), rows
for i, row in enumerate(rows):
    for j, cell in enumerate(row):
        assert cell.get("distance") is not None and cell.get("time") is not None, (i, j, cell)
        assert cell["distance"] > 0 and cell["time"] > 0, (i, j, cell)

sources = [
    ("Seattle", 47.6062, -122.3321),
    ("New York", 40.7128, -74.0060),
    ("Miami", 25.7617, -80.1918),
    ("Los Angeles", 34.0522, -118.2437),
]
targets = [
    ("Cog Hill", 41.665534, -87.960593),
    ("Bunn", 39.770209, -89.640643),
    ("Westview", 39.905702, -91.358706),
]
checks = [(0, 0), (1, 1), (2, 2), (3, 0)]
for source_index, target_index in checks:
    s, t = sources[source_index], targets[target_index]
    payload = json.dumps({
        "locations": [{"lat": s[1], "lon": s[2]}, {"lat": t[1], "lon": t[2]}],
        "costing": "auto",
        "units": "miles",
    })
    response = subprocess.run(
        ["curl", "--fail", "--silent", "--show-error", "--header", "Content-Type: application/json", "--data", payload, f"{base}/route"],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(response.stdout)["trip"]["summary"]
    cell = rows[source_index][target_index]
    # Matrix and detailed route use different search paths; require broad agreement,
    # not byte-for-byte equality on a continental graph.
    assert abs(cell["distance"] - summary["length"]) <= max(25, summary["length"] * 0.03), (s[0], t[0], cell, summary)
    assert abs(cell["time"] - summary["time"]) <= max(1800, summary["time"] * 0.08), (s[0], t[0], cell, summary)
    print(f"{s[0]} -> {t[0]}: route {summary['length']:.1f} mi; matrix {cell['distance']:.1f} mi")

print("4x3 bounded national matrix: OK")
PY
