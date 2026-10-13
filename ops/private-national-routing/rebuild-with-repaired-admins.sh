#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE='ghcr.io/valhalla/valhalla-scripted:3.9.1@sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA="${ROUTING_DATA_DIR:-$SCRIPT_DIR/data}"
CONTAINER="${VALHALLA_CONTAINER:-valhalla-us-261011}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null || die 'docker is not installed'
command -v python3 >/dev/null || die 'python3 is required'
[[ "$EUID" -ne 0 ]] || die 'run as normal user'
[[ "${XDG_RUNTIME_DIR:-}" == "/run/user/$(id -u)" ]] || die 'expected the current user runtime directory'
expected_docker_host="unix://$XDG_RUNTIME_DIR/docker.sock"
[[ "${DOCKER_HOST:-$expected_docker_host}" == "$expected_docker_host" ]] || die 'DOCKER_HOST must select the local rootless socket'
[[ -z "${DOCKER_CONTEXT:-}" ]] || die 'unset DOCKER_CONTEXT before selecting the rootless socket'
[[ -S "$XDG_RUNTIME_DIR/docker.sock" ]] || die 'rootless Docker socket is missing'
[[ "$(stat -c '%u' "$XDG_RUNTIME_DIR/docker.sock")" == "$(id -u)" ]] || die 'rootless Docker socket has the wrong owner'
export DOCKER_HOST="$expected_docker_host"
security_options="$(docker info --format '{{json .SecurityOptions}}')" || die 'rootless Docker is unavailable'
printf '%s\n' "$security_options" | python3 -c 'import json, sys; sys.exit("name=rootless" not in json.load(sys.stdin))' || die 'Docker daemon does not report rootless SecurityOptions'
[[ "$(realpath -- "$DATA")" != '/' ]] || die 'refusing root as data path'
[[ -f "$DATA/admins.sqlite" && -s "$DATA/admins.sqlite" ]] || die 'missing or empty active admin database'
[[ -d "$DATA/valhalla_tiles" ]] || die 'missing active tiles'
[[ -f "$DATA/valhalla_tiles.tar" ]] || die 'missing active tile extract'
[[ ! -e "$DATA/admins.sqlite.pre-us-parent" ]] || die 'admin backup already exists'
[[ ! -e "$DATA/valhalla_tiles.pre-admin-repair" ]] || die 'tile backup already exists'
[[ ! -e "$DATA/valhalla_tiles.tar.pre-admin-repair" ]] || die 'tar backup already exists'

# Read the database without creating it or writing a journal, before any stop.
python3 - "$DATA/admins.sqlite" <<'PY'
import pathlib
import sqlite3
import sys

path = pathlib.Path(sys.argv[1]).resolve()
with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
    states = db.execute("SELECT COUNT(*) FROM admins WHERE admin_level = 4").fetchone()[0]
    parents = db.execute(
        "SELECT COUNT(*) FROM admins WHERE admin_level = 2 AND iso_code = 'US'"
    ).fetchone()[0]
if parents:
    sys.exit("ERROR: admin database already has a US parent; refusing repair")
if not states:
    sys.exit("ERROR: admin database has no state rows; refusing repair")
PY

docker stop "$CONTAINER"
cp -- "$DATA/admins.sqlite" "$DATA/admins.sqlite.pre-us-parent"
docker run --rm --mount "type=bind,src=$DATA,dst=/custom_files" --entrypoint spatialite "$IMAGE" /custom_files/admins.sqlite '.read /custom_files/repair-us-admin-parent.sql'

mv -- "$DATA/valhalla_tiles" "$DATA/valhalla_tiles.pre-admin-repair"
mv -- "$DATA/valhalla_tiles.tar" "$DATA/valhalla_tiles.tar.pre-admin-repair"

docker run --rm --name valhalla-us-admin-rebuild --cpus 12 --memory 80g --memory-swap 80g --pids-limit 8192 --mount "type=bind,src=$DATA,dst=/custom_files" --entrypoint valhalla_build_tiles "$IMAGE" -c /custom_files/valhalla.json -j 12 /custom_files/us-261011.osm.pbf

docker run --rm --name valhalla-us-extract-rebuild --cpus 4 --memory 16g --memory-swap 16g --pids-limit 2048 --mount "type=bind,src=$DATA,dst=/custom_files" --entrypoint valhalla_build_extract "$IMAGE" -c /custom_files/valhalla.json -v

docker start "$CONTAINER"
printf 'Admin-aware rebuild complete; service container restarted.\n'
