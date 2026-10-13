#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE='ghcr.io/valhalla/valhalla-scripted:3.9.1@sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA="${ROUTING_DATA_DIR:-$SCRIPT_DIR/data}"
CONTAINER="${VALHALLA_CONTAINER:-valhalla-us-261011}"
export DOCKER_HOST="${DOCKER_HOST:-unix://${XDG_RUNTIME_DIR:?XDG_RUNTIME_DIR is required}/docker.sock}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
[[ "$EUID" -ne 0 ]] || die 'run as normal user'
[[ "$(realpath -- "$DATA")" != '/' ]] || die 'refusing root as data path'
[[ -f "$DATA/admins.sqlite" ]] || die 'missing active admin database'
[[ -d "$DATA/valhalla_tiles" ]] || die 'missing active tiles'
[[ -f "$DATA/valhalla_tiles.tar" ]] || die 'missing active tile extract'
[[ ! -e "$DATA/admins.sqlite.pre-us-parent" ]] || die 'admin backup already exists'
[[ ! -e "$DATA/valhalla_tiles.pre-admin-repair" ]] || die 'tile backup already exists'
[[ ! -e "$DATA/valhalla_tiles.tar.pre-admin-repair" ]] || die 'tar backup already exists'

docker stop "$CONTAINER"
cp -- "$DATA/admins.sqlite" "$DATA/admins.sqlite.pre-us-parent"
docker run --rm --mount "type=bind,src=$DATA,dst=/custom_files" --entrypoint spatialite "$IMAGE" /custom_files/admins.sqlite '.read /custom_files/repair-us-admin-parent.sql'

mv -- "$DATA/valhalla_tiles" "$DATA/valhalla_tiles.pre-admin-repair"
mv -- "$DATA/valhalla_tiles.tar" "$DATA/valhalla_tiles.tar.pre-admin-repair"

docker run --rm --name valhalla-us-admin-rebuild --cpus 12 --memory 80g --memory-swap 80g --pids-limit 8192 --mount "type=bind,src=$DATA,dst=/custom_files" --entrypoint valhalla_build_tiles "$IMAGE" -c /custom_files/valhalla.json -j 12 /custom_files/us-261011.osm.pbf

docker run --rm --name valhalla-us-extract-rebuild --cpus 4 --memory 16g --memory-swap 16g --pids-limit 2048 --mount "type=bind,src=$DATA,dst=/custom_files" --entrypoint valhalla_build_extract "$IMAGE" -c /custom_files/valhalla.json -v

docker start "$CONTAINER"
printf 'Admin-aware rebuild complete; service container restarted.\n'
