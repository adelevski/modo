#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE='ghcr.io/valhalla/valhalla-scripted:3.9.1@sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
PBF='us-261011.osm.pbf'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA="${ROUTING_DATA_DIR:-$SCRIPT_DIR/data}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null || die 'docker is not installed'
command -v python3 >/dev/null || die 'python3 is required'
command -v timeout >/dev/null || die 'timeout is required'
[[ "$EUID" -ne 0 ]] || die 'run as normal user'
[[ "$(uname -m)" == 'aarch64' ]] || die 'expected aarch64'
[[ "${XDG_RUNTIME_DIR:-}" == "/run/user/$(id -u)" ]] || die 'expected the current user runtime directory'
expected_docker_host="unix://$XDG_RUNTIME_DIR/docker.sock"
[[ "${DOCKER_HOST:-$expected_docker_host}" == "$expected_docker_host" ]] || die 'DOCKER_HOST must select the local rootless socket'
[[ -z "${DOCKER_CONTEXT:-}" ]] || die 'unset DOCKER_CONTEXT before selecting the rootless socket'
[[ -S "$XDG_RUNTIME_DIR/docker.sock" ]] || die 'rootless Docker socket is missing'
[[ "$(stat -c '%u' "$XDG_RUNTIME_DIR/docker.sock")" == "$(id -u)" ]] || die 'rootless Docker socket has the wrong owner'
export DOCKER_HOST="$expected_docker_host"
security_options="$(docker info --format '{{json .SecurityOptions}}')" || die 'rootless Docker is unavailable'
printf '%s\n' "$security_options" | python3 -c 'import json, sys; sys.exit("name=rootless" not in json.load(sys.stdin))' || die 'Docker daemon does not report rootless SecurityOptions'
DATA="$(realpath -- "$DATA")"
[[ "$DATA" != '/' ]] || die 'refusing root as data path'
for file in admins.sqlite valhalla.json timezones.sqlite "$PBF" valhalla_tiles.tar; do
  [[ -f "$DATA/$file" && -s "$DATA/$file" ]] || die "missing or empty source file: $file"
done
[[ -d "$DATA/valhalla_tiles" ]] || die 'missing active tiles'
[[ -s "$SCRIPT_DIR/data/repair-us-admin-parent.sql" ]] || die 'missing reviewed repair SQL'

# Read the database without creating it or writing a journal, before any build.
python3 - "$DATA/admins.sqlite" <<'PY'
import pathlib
import sqlite3
import sys

path = pathlib.Path(sys.argv[1]).resolve()
with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
    states = db.execute("SELECT COUNT(*) FROM admins WHERE admin_level = 4").fetchone()[
        0
    ]
    parents = db.execute(
        "SELECT COUNT(*) FROM admins WHERE admin_level = 2 AND iso_code = 'US'"
    ).fetchone()[0]
if parents:
    sys.exit("ERROR: admin database already has a US parent; refusing repair")
if not states:
    sys.exit("ERROR: admin database has no state rows; refusing repair")
PY

source_identities="$(python3 "$SCRIPT_DIR/verify-runtime-files.py" source-identities "$SCRIPT_DIR/data/generation-manifest.json" "$IMAGE")" || die 'invalid reviewed generation manifest'
read -r EXPECTED_PBF_BYTES EXPECTED_PBF_SHA256 EXPECTED_CONFIG_SHA256 EXPECTED_TIMEZONE_SHA256 <<< "$source_identities"
[[ "$(stat -c '%s' "$DATA/$PBF")" == "$EXPECTED_PBF_BYTES" ]] || die 'PBF byte count mismatch'
printf '%s  %s\n' "$EXPECTED_PBF_SHA256" "$DATA/$PBF" | sha256sum --check --status || die 'PBF SHA-256 mismatch'
printf '%s  %s\n' "$EXPECTED_CONFIG_SHA256" "$DATA/valhalla.json" | sha256sum --check --status || die 'source config differs from the reviewed config'
printf '%s  %s\n' "$EXPECTED_TIMEZONE_SHA256" "$DATA/timezones.sqlite" | sha256sum --check --status || die 'source timezone database SHA-256 mismatch'
docker image inspect "$IMAGE" >/dev/null || die 'pinned ARM64 image is missing'
STAGING_ROOT="$(realpath -- "${ROUTING_STAGING_ROOT:-$(dirname -- "$DATA")}")"
[[ -d "$STAGING_ROOT" && "$STAGING_ROOT" != '/' ]] || die 'staging root must be an existing non-root directory'
available="$(df --output=avail -B1 "$STAGING_ROOT" | tail -1 | tr -d ' ')"
(( available >= 250000000000 )) || die 'need at least 250 GB free for a separate build generation'

# Staging implementation.
stop_build_client() {
  local attempt
  kill -- "$build_client" 2>/dev/null || true
  for attempt in {1..20}; do
    kill -0 -- "$build_client" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 -- "$build_client" 2>/dev/null; then
    kill -KILL -- "$build_client" 2>/dev/null || true
    for attempt in {1..10}; do
      kill -0 -- "$build_client" 2>/dev/null || break
      sleep 0.1
    done
  fi
  if kill -0 -- "$build_client" 2>/dev/null; then
    printf 'Builder client did not exit after KILL; operator cleanup required.\n' >&2
  else
    # Reap only after exit: an uninterruptible client must not block cleanup.
    wait "$build_client" 2>/dev/null || true
  fi
  build_client=''
}

cleanup_staged_build() {
  local result="$1"
  trap '' INT TERM
  if [[ -n "${build_client:-}" ]]; then
    stop_build_client
  fi
  # Remove only IDs recorded by this invocation, never a pre-existing name.
  local cid_file cid
  for cid_file in "$stage/admin-container.cid" "$stage/tile-container.cid" "$stage/extract-container.cid"; do
    [[ -s "$cid_file" ]] || continue
    IFS= read -r cid < "$cid_file" || true
    [[ "$cid" =~ ^[0-9a-f]{64}$ ]] || continue
    timeout --kill-after=2s 10s docker rm -f -- "$cid" >/dev/null 2>&1 || true
  done
  if (( result != 0 )); then
    printf 'Build failed; active data and service state were untouched. Staging retained at %s\n' "$stage" >&2
  fi
}

run_staged_container() {
  docker run "$@" &
  build_client="$!"
  wait "$build_client"
  build_client=''
}

stage_admin_repair() {
  stage="$(mktemp -d -- "$STAGING_ROOT/valhalla-admin-repair.XXXXXXXX")"
  admin_container="$(basename -- "$stage")-admins"
  tile_container="$(basename -- "$stage")-tiles"
  extract_container="$(basename -- "$stage")-extract"
  build_client=''
  trap 'cleanup_staged_build "$?"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM

  cp -- "$DATA/valhalla.json" "$DATA/timezones.sqlite" "$stage/"
  cp -- "$SCRIPT_DIR/data/repair-us-admin-parent.sql" "$stage/"
  # A consistent SQLite snapshot is written only into the new generation.
  python3 - "$DATA/admins.sqlite" "$stage/admins.sqlite" <<'PY'
import pathlib
import sqlite3
import sys

source = pathlib.Path(sys.argv[1]).resolve()
with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as old:
    with sqlite3.connect(sys.argv[2]) as new:
        old.backup(new)
PY

  run_staged_container --rm --name "$admin_container" --cidfile "$stage/admin-container.cid" --network none --cpus 2 --memory 4g --memory-swap 4g --pids-limit 256 --mount "type=bind,src=$stage,dst=/custom_files" --entrypoint spatialite "$IMAGE" /custom_files/admins.sqlite '.read /custom_files/repair-us-admin-parent.sql'

  run_staged_container --rm --name "$tile_container" --cidfile "$stage/tile-container.cid" --network none --cpus 12 --memory 80g --memory-swap 80g --pids-limit 8192 --mount "type=bind,src=$stage,dst=/custom_files" --mount "type=bind,src=$DATA/$PBF,dst=/custom_files/$PBF,readonly" --entrypoint valhalla_build_tiles "$IMAGE" -c /custom_files/valhalla.json -j 12 "/custom_files/$PBF"

  run_staged_container --rm --name "$extract_container" --cidfile "$stage/extract-container.cid" --network none --cpus 4 --memory 16g --memory-swap 16g --pids-limit 2048 --mount "type=bind,src=$stage,dst=/custom_files" --entrypoint valhalla_build_extract "$IMAGE" -c /custom_files/valhalla.json -v

  python3 - "$stage" "$IMAGE" "$DATA/$PBF" "$EXPECTED_PBF_SHA256" <<'PY'
import hashlib
import json
import pathlib
import sqlite3
import sys

stage = pathlib.Path(sys.argv[1])
with sqlite3.connect((stage / "admins.sqlite").as_uri() + "?mode=ro", uri=True) as db:
    parents = db.execute(
        "SELECT rowid FROM admins WHERE admin_level = 2 AND iso_code = 'US'"
    ).fetchall()
    states = db.execute("SELECT COUNT(*) FROM admins WHERE admin_level = 4").fetchone()[
        0
    ]
    attached = db.execute(
        "SELECT COUNT(*) FROM admins WHERE admin_level = 4 AND parent_admin = ?",
        parents[0] if len(parents) == 1 else (None,),
    ).fetchone()[0]
if len(parents) != 1 or not states or attached != states:
    sys.exit("ERROR: staged admin repair did not attach every state to one US parent")
if not any((stage / "valhalla_tiles").rglob("*.gph")):
    sys.exit("ERROR: staged tiles are missing")
assets = []
for name in [
    "valhalla.json",
    "admins.sqlite",
    "timezones.sqlite",
    "valhalla_tiles.tar",
]:
    path = stage / name
    if not path.is_file() or not path.stat().st_size:
        sys.exit(f"ERROR: staged runtime file is missing or empty: {name}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(8 * 1024 * 1024):
            digest.update(chunk)
    assets.append(
        {"file_name": name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    )
source = pathlib.Path(sys.argv[3])
manifest = {
    "schema": "modo-routing-generation-v1",
    "status": "staged-unverified",
    "platform": "linux/arm64",
    "image": sys.argv[2],
    "assets": assets,
    "external_assets": [
        {
            "file_name": source.name,
            "bytes": source.stat().st_size,
            "sha256": sys.argv[4],
        }
    ],
}
(stage / "generation-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n"
)
PY

  printf 'Staged generation: %s\n' "$stage"
  printf 'Active data and routing containers were untouched. Supply the verified source PBF and review generation-manifest.json, metadata and route/matrix checks before any separately approved switch.\n'
}

stage_admin_repair
