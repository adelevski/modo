#!/usr/bin/env bash
set -Eeuo pipefail

IMAGE='ghcr.io/valhalla/valhalla-scripted:3.9.1@sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
EXPECTED_IMAGE_DIGEST='sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6'
PBF='us-261011.osm.pbf'
EXPECTED_PBF_BYTES='12199919524'
EXPECTED_PBF_MD5='f9a56ae3ef76583cf892e5bfe23a18d4'
EXPECTED_PBF_SHA256='de48784f65c1160eec691abf47821c7d8deab685c8cc984ba3720ce410c8b5df'
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

for file in "$PBF" valhalla.json valhalla_tiles.tar admins.sqlite timezones.sqlite; do
  [[ -f "$DATA_DIR/$file" && -s "$DATA_DIR/$file" ]] || die "missing or empty runtime file: $file"
done
printf '%s  %s\n' '0ba4757cdc8f61be80953ecaec7eb7500513e82febc24d1551140b66891dee83' "$DATA_DIR/valhalla.json" | sha256sum --check --status || die 'runtime config differs from the reviewed config'
# Require the active repaired generation measured and hashed on Spark.
[[ "$(stat -c '%s' "$DATA_DIR/valhalla_tiles.tar")" == '21079121920' ]] || die 'tile archive generation mismatch; restore remains blocked'
printf '%s  %s\n' '0df937bf9044ab043e38b3844fcd65a41f9319389a9d40325497f5432412c846' "$DATA_DIR/valhalla_tiles.tar" | sha256sum --check --status || die 'tile archive SHA-256 mismatch; restore remains blocked'
printf '%s  %s\n' '73ebbdeaa7304d5b1a88d7a26a8ba191261ae7700b1baf5aea9f011f651baa98' "$DATA_DIR/admins.sqlite" | sha256sum --check --status || die 'repaired admin database SHA-256 mismatch'
[[ "$(stat -c '%s' "$DATA_DIR/timezones.sqlite")" == '122478592' ]] || die 'timezone database byte count mismatch'
printf '%s  %s\n' '7861983249b9165f133f414145e4021fb3e05f18034458ef3b1987faac8a6b87' "$DATA_DIR/timezones.sqlite" | sha256sum --check --status || die 'timezone database SHA-256 mismatch'

[[ "$(stat -c '%s' "$DATA_DIR/$PBF")" == "$EXPECTED_PBF_BYTES" ]] || die 'PBF byte count mismatch'
printf '%s  %s\n' "$EXPECTED_PBF_MD5" "$DATA_DIR/$PBF" | md5sum --check --status || die 'PBF MD5 mismatch'
printf '%s  %s\n' "$EXPECTED_PBF_SHA256" "$DATA_DIR/$PBF" | sha256sum --check --status || die 'PBF SHA-256 mismatch'
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
