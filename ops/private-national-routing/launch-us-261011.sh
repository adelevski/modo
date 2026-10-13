#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE='ghcr.io/valhalla/valhalla-scripted:3.9.1@sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
EXPECTED_IMAGE_DIGEST='sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
CONTAINER="${VALHALLA_CONTAINER:-valhalla-us-261011}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_DIR="${ROUTING_DATA_DIR:-$SCRIPT_DIR/data}"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v docker >/dev/null || die 'docker is not installed'
command -v python3 >/dev/null || die 'python3 is required'
[[ "$EUID" -ne 0 ]] || die 'run as the normal user, not root'
[[ "$(uname -m)" == 'aarch64' ]] || die "expected aarch64"
[[ "${XDG_RUNTIME_DIR:-}" == "/run/user/$(id -u)" ]] || die 'expected the current user runtime directory'
expected_docker_host="unix://$XDG_RUNTIME_DIR/docker.sock"
[[ "${DOCKER_HOST:-$expected_docker_host}" == "$expected_docker_host" ]] || die 'DOCKER_HOST must select the local rootless socket'
[[ -z "${DOCKER_CONTEXT:-}" ]] || die 'unset DOCKER_CONTEXT before selecting the rootless socket'
[[ -S "$XDG_RUNTIME_DIR/docker.sock" ]] || die 'rootless Docker socket is missing'
[[ "$(stat -c '%u' "$XDG_RUNTIME_DIR/docker.sock")" == "$(id -u)" ]] || die 'rootless Docker socket has the wrong owner'
export DOCKER_HOST="$expected_docker_host"

python3 "$SCRIPT_DIR/verify-runtime-files.py" "$DATA_DIR" "$SCRIPT_DIR/data/generation-manifest.json" "$IMAGE"
security_options="$(docker info --format '{{json .SecurityOptions}}')" || die 'rootless Docker is unavailable'
printf '%s\n' "$security_options" | python3 -c 'import json, sys; sys.exit("name=rootless" not in json.load(sys.stdin))' || die 'Docker daemon does not report rootless SecurityOptions'
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
  --env build_admins=False
  --env build_time_zones=False
  --env build_elevation=False
  --env build_transit=False
  --env build_tar=False
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
