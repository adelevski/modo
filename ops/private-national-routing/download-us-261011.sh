#!/usr/bin/env bash
set -Eeuo pipefail

URL='https://download.geofabrik.de/north-america/us-261011.osm.pbf'
NAME='us-261011.osm.pbf'
EXPECTED_BYTES='12199919524'
EXPECTED_MD5='f9a56ae3ef76583cf892e5bfe23a18d4'
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
DATA_DIR="${ROUTING_DATA_DIR:-$SCRIPT_DIR/data}"
PART="$DATA_DIR/$NAME.partial"
FINAL="$DATA_DIR/$NAME"

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
command -v wget >/dev/null || die 'wget is required'
[[ "$EUID" -ne 0 ]] || die 'run as the normal user, not root'
mkdir -p -- "$DATA_DIR"
[[ ! -e "$FINAL" ]] || die "$FINAL already exists"

available="$(df --output=avail -B1 "$DATA_DIR" | tail -1 | tr -d ' ')"
(( available >= 250000000000 )) || die "need at least 250 GB free before download; found $available bytes"

printf 'Downloading %s to resumable partial file\n' "$URL"
wget --continue --output-document="$PART" "$URL"

actual_bytes="$(stat -c '%s' "$PART")"
[[ "$actual_bytes" == "$EXPECTED_BYTES" ]] || die "download is $actual_bytes bytes, expected $EXPECTED_BYTES"
printf '%s  %s\n' "$EXPECTED_MD5" "$PART" | md5sum --check --status || die 'MD5 verification failed'
sha256sum "$PART" >"$DATA_DIR/$NAME.sha256"
mv -- "$PART" "$FINAL"
printf '%s  %s\n' "$EXPECTED_MD5" "$NAME" >"$DATA_DIR/$NAME.md5"
printf 'Verified and installed %s (%s bytes)\n' "$FINAL" "$actual_bytes"
