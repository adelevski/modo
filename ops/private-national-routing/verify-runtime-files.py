"""Verify one reviewed routing generation without inspecting or changing Docker."""

import hashlib
import json
import pathlib
import re
import sys


def load_manifest(manifest_path, expected_image):
    manifest = json.loads(pathlib.Path(manifest_path).read_text())
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "modo-routing-generation-v1"
        or manifest.get("status") != "verified-identities"
        or manifest.get("platform") != "linux/arm64"
        or manifest.get("image") != expected_image
    ):
        raise ValueError("routing manifest is not the reviewed ARM64 release")
    for group, names in [
        (
            "assets",
            {
                "valhalla.json",
                "admins.sqlite",
                "timezones.sqlite",
                "valhalla_tiles.tar",
            },
        ),
        ("external_assets", {"us-261011.osm.pbf"}),
    ]:
        assets = manifest.get(group)
        if (
            not isinstance(assets, list)
            or len(assets) != len(names)
            or any(not isinstance(asset, dict) for asset in assets)
            or any(not isinstance(asset.get("file_name"), str) for asset in assets)
            or {asset.get("file_name") for asset in assets} != names
        ):
            raise ValueError(
                "routing manifest has missing or duplicate asset identities"
            )
        for asset in assets:
            size, digest = asset.get("bytes"), asset.get("sha256")
            if (
                type(size) is not int
                or size <= 0
                or not isinstance(digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", digest)
            ):
                raise ValueError("routing manifest has an invalid asset identity")
    return manifest


def verify_runtime_files(data_dir, manifest_path, expected_image):
    data_dir = pathlib.Path(data_dir)
    manifest = load_manifest(manifest_path, expected_image)
    for assets in [manifest["assets"], manifest["external_assets"]]:
        for asset in assets:
            size, digest = asset["bytes"], asset["sha256"]
            path = data_dir / asset["file_name"]
            if path.is_symlink() or not path.is_file() or path.stat().st_size != size:
                raise ValueError(
                    f"missing, linked or wrong-size runtime file: {path.name}"
                )
            before = path.stat()
            sha256 = hashlib.sha256()
            with path.open("rb") as stream:
                while chunk := stream.read(8 * 1024 * 1024):
                    sha256.update(chunk)
            after = path.stat()
            if sha256.hexdigest() != digest:
                raise ValueError(f"runtime SHA-256 mismatch: {path.name}")
            identity = lambda stat: (
                stat.st_dev,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
            if identity(before) != identity(after):
                raise ValueError(
                    f"runtime file changed during verification: {path.name}"
                )


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["source-identities"]:
            manifest = load_manifest(*sys.argv[2:])
            source = manifest["external_assets"][0]
            digests = {
                asset["file_name"]: asset["sha256"] for asset in manifest["assets"]
            }
            print(
                source["bytes"],
                source["sha256"],
                digests["valhalla.json"],
                digests["timezones.sqlite"],
            )
        else:
            verify_runtime_files(*sys.argv[1:])
    except (OSError, ValueError, TypeError) as error:
        sys.exit(f"ERROR: {error}")
