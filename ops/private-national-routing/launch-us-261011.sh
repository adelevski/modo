#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE='ghcr.io/valhalla/valhalla-scripted:3.9.1@sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
EXPECTED_IMAGE_DIGEST='sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
PBF='us-261011.osm.pbf'
EXPECTED_PBF_BYTES='12199919524'
EXPECTED_PBF_MD5='f9a56ae3ef76583cf892e5bfe23a18d4'
CONTAINER='valhalla-us-261011'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_DIR="${ROUTING_DATA_DIR:-$SCRIPT_DIR/data}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null || die 'docker is not installed'
[[ "$EUID" -ne 0 ]] || die 'run as the normal user, not root'
[[ "$(uname -m)" == 'aarch64' ]] || die "expected aarch64"
[[ -n "${XDG_RUNTIME_DIR:-}" ]] || die 'XDG_RUNTIME_DIR is unset'
[[ -f "$DATA_DIR/$PBF" ]] || die "missing verified $DATA_DIR/$PBF"
export DOCKER_HOST="${DOCKER_HOST:-unix://$XDG_RUNTIME_DIR/docker.sock}"

[[ "$(stat -c '%s' "$DATA_DIR/$PBF")" == "$EXPECTED_PBF_BYTES" ]] || die 'PBF byte count mismatch'
printf '%s  %s\n' "$EXPECTED_PBF_MD5" "$DATA_DIR/$PBF" | md5sum --check --status || die 'PBF MD5 mismatch'
docker info >/dev/null 2>&1 || die 'rootless Docker is unavailable'
docker container inspect "$CONTAINER" >/dev/null 2>&1 && die "$CONTAINER already exists"
docker image inspect "$IMAGE" --format '{{join .RepoDigests "\n"}}' | grep -F -- "$EXPECTED_IMAGE_DIGEST" >/dev/null || die 'pinned image is missing or digest does not match'

docker_args=(
  --detach
  --name "$CONTAINER"
  --restart unless-stopped
  --cpus 12
  --memory 80g
  --memory-swap 80g
  --pids-limit 8192
  --publish "127.0.0.1:${VALHALLA_HOST_PORT:-8003}:8002"
  --mount "type=bind,src=$DATA_DIR,dst=/custom_files"
  --env use_tiles_ignore_pbf=True
  --env force_rebuild=False
  --env build_admins=True
  --env build_time_zones=True
  --env build_elevation=False
  --env build_transit=False
  --env build_tar=True
  --env traffic_name=
  --env use_default_speeds_config=False
  --env serve_tiles=True
  --env server_threads=12
  --health-cmd 'curl -fsS http://127.0.0.1:8002/status >/dev/null || exit 1'
  --health-interval 30s
  --health-timeout 5s
  --health-retries 5
  --health-start-period 6h
)
docker run "${docker_args[@]}" "$IMAGE"

printf 'Started %s on 127.0.0.1:%s with 12 CPUs and 80 GiB RAM.\n' "$CONTAINER" "${VALHALLA_HOST_PORT:-8003}"
