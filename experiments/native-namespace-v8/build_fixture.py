"""Proposed bounded tiny-graph builder, run only inside an approved container."""

import hashlib
import json
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))

from prepare_fixture import MAX_BYTES, ROOT, synthetic_pbf

OUTPUT = Path("/tmp/native-output")
TILE_DIR = OUTPUT / "tiles"
READY_SCHEMA = "modo-native-build-ready-v1"
TILE_PATTERN = r"tiles/[0-2]/(?:[0-9]{3}/){1,3}[0-9]{3}\.gph"


def tile_files(output):
    root = output / "tiles"
    if output.is_symlink() or root.is_symlink() or not root.is_dir():
        raise ValueError("missing/linked synthetic tile directory")
    entries = list(root.rglob("*"))
    if any(p.is_symlink() for p in entries):
        raise ValueError("linked synthetic graph output")
    tiles = sorted(p for p in entries if p.is_file() and p.suffix == ".gph")
    if not 1 <= len(tiles) <= 256:
        raise ValueError("built graph exceeds tile count bounds")
    sizes = [p.stat().st_size for p in tiles]
    if any(size <= 0 for size in sizes) or sum(sizes) > MAX_BYTES:
        raise ValueError("built graph exceeds byte bounds")
    if any(
        not re.fullmatch(TILE_PATTERN, p.relative_to(output).as_posix()) for p in tiles
    ):
        raise ValueError("unexpected synthetic graph path")
    return tiles, sum(sizes)


def export(output=OUTPUT, stream=None):
    """Read only known tiny graph files in the running container's namespace."""
    tiles, _ = tile_files(output)
    stream = sys.stdout.buffer if stream is None else stream
    with tarfile.open(
        fileobj=stream, mode="w|", format=tarfile.USTAR_FORMAT
    ) as archive:
        for path in tiles:
            row = tarfile.TarInfo(path.relative_to(output).as_posix())
            row.size = path.stat().st_size
            row.mode, row.uid, row.gid, row.mtime = 0o444, 65532, 65532, 0
            with path.open("rb") as source:
                archive.addfile(row, source)


def build(source=Path("/input"), output=OUTPUT):
    if (source / "synthetic.osm.pbf").read_bytes() != synthetic_pbf():
        raise ValueError("source is not the reviewed synthetic grid")
    if (source / "config.json").read_bytes() != (
        ROOT / "fixture-config.json"
    ).read_bytes():
        raise ValueError("configuration differs from proposal")
    output.mkdir(mode=0o755)
    config = json.loads((source / "config.json").read_text())
    config["mjolnir"]["tile_dir"] = str(output / "tiles")
    config_path = output.parent / "native-build.json"
    config_path.write_text(json.dumps(config))
    with (output.parent / "native-build.log").open("wb") as log:
        subprocess.run(
            [
                "valhalla_build_tiles",
                "-c",
                str(config_path),
                "-j",
                "1",
                str(source / "synthetic.osm.pbf"),
            ],
            stdout=log,
            stderr=log,
            check=True,
            timeout=60,
        )
    tiles, total = tile_files(output)
    print(
        json.dumps(
            {
                "event": "synthetic-graph-ready",
                "schema": READY_SCHEMA,
                "tile_dir": str(output / "tiles"),
                "tiles": len(tiles),
                "tile_bytes": total,
                "pbf_sha256": hashlib.sha256(synthetic_pbf()).hexdigest(),
            }
        ),
        flush=True,
    )
    # Finite window for a bounded in-namespace export; no service or routing traffic.
    time.sleep(20)


if __name__ == "__main__":
    if sys.argv[1:] == ["--export"]:
        export()
    elif len(sys.argv) == 1:
        build()
    else:
        raise SystemExit("no configurable build/export scope")
