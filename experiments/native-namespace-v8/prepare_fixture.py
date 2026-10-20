"""Deterministic synthetic OSM PBF; no downloads, Docker calls or road extraction."""

import argparse
import hashlib
import json
import shutil
import struct
from itertools import pairwise
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SIDE = 64
MAX_BYTES = 100 * 1024**2


def varint(value):
    raw = bytearray()
    while value >= 128:
        raw.append((value & 127) | 128)
        value >>= 7
    raw.append(value)
    return bytes(raw)


def number(field, value, signed=False):
    if signed:
        value = value * 2 if value >= 0 else -value * 2 - 1
    return varint(field << 3) + varint(value)


def blob(field, value):
    return varint((field << 3) | 2) + varint(len(value)) + value


def block(kind, value):
    raw = blob(1, value)
    header = blob(1, kind.encode()) + number(3, len(raw))
    return struct.pack(">I", len(header)) + header + raw


def synthetic_pbf(side=SIDE):
    if type(side) is not int or not 2 <= side <= SIDE:
        raise ValueError("grid must have 2..64 nodes per side")
    nodes = bytearray()
    for row in range(side):
        for column in range(side):
            node = number(1, row * side + column + 1, True)
            node += number(8, 419000000 + row * 10000, True)
            node += number(9, -876700000 + column * 10000, True)
            nodes += blob(1, node)
    ways = bytearray()
    sequences = [[r * side + c + 1 for c in range(side)] for r in range(side)]
    sequences += [[r * side + c + 1 for r in range(side)] for c in range(side)]
    for identifier, refs in enumerate(sequences, 1):
        deltas = [refs[0]] + [b - a for a, b in pairwise(refs)]
        way = number(1, identifier)
        way += blob(2, varint(1) + varint(3))
        way += blob(3, varint(2) + varint(4))
        way += blob(8, b"".join(varint(v * 2) for v in deltas))
        ways += blob(3, way)
    strings = b"".join(
        blob(1, s) for s in [b"", b"highway", b"residential", b"maxspeed", b"25"]
    )
    primitives = blob(1, strings) + blob(2, nodes) + blob(2, ways)
    header = blob(4, b"OsmSchema-V0.6") + blob(16, b"modo-synthetic-grid-v1")
    return block("OSMHeader", header) + block("OSMData", primitives)


def prepare(destination):
    destination.mkdir(mode=0o755)  # Refuse an existing preparation directory.
    destination.chmod(0o755)  # Explicit synthetic-only traversal, independent of umask.
    raw = synthetic_pbf()
    if len(raw) > 1024**2:
        raise ValueError("PBF exceeds 1 MiB preparation budget")
    (destination / "synthetic.osm.pbf").write_bytes(raw)
    shutil.copyfile(ROOT / "fixture-config.json", destination / "config.json")
    for path in destination.iterdir():
        path.chmod(
            0o444
        )  # Synthetic inputs readable by the unprivileged container UID.


def seal(build, destination):
    """Copy only reviewed inputs and built graph tiles into a new small artifact."""
    files = [build / "config.json", build / "synthetic.osm.pbf"]
    files += sorted((build / "tiles").rglob("*.gph"))
    if not 3 <= len(files) <= 258 or any(p.is_symlink() for p in build.rglob("*")):
        raise ValueError("missing/oversized/linked tile generation")
    if sum(p.stat().st_size for p in files) > MAX_BYTES:
        raise ValueError("artifact exceeds 100 MiB")
    if (build / "config.json").read_bytes() != (
        ROOT / "fixture-config.json"
    ).read_bytes():
        raise ValueError("fixture config differs from proposal")
    if (build / "synthetic.osm.pbf").read_bytes() != synthetic_pbf():
        raise ValueError("source differs from deterministic synthetic grid")
    destination.mkdir(mode=0o755)
    rows = []
    for path in files:
        relative = path.relative_to(build)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        target.chmod(0o444)
        rows.append(
            {
                "file": str(relative),
                "bytes": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
        )
    manifest = {
        "schema": "modo-native-synthetic-v1",
        "grid_side": SIDE,
        "fixture_date": "2026-10-13",
        "files": rows,
    }
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (destination / "manifest.json").chmod(0o444)
    for directory in sorted(destination.rglob("*"), reverse=True):
        if directory.is_dir():
            directory.chmod(0o555)
    destination.chmod(0o555)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["prepare", "seal"])
    parser.add_argument("destination", type=Path)
    parser.add_argument("--build-directory", type=Path)
    args = parser.parse_args()
    if args.operation == "prepare":
        prepare(args.destination)
    elif args.build_directory:
        seal(args.build_directory, args.destination)
    else:
        parser.error("seal requires --build-directory")


if __name__ == "__main__":
    main()
