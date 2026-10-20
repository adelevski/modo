"""Optional approved preparation only. Default mode has no Docker or filesystem mutations."""

import argparse
import hashlib
import io
import json
import os
import re
import signal
import sys
import tarfile

sys.dont_write_bytecode = True

import build_fixture as builder
import native_review as native
import prepare_fixture as fixture

BUILDER = native.BUILDER
INPUT = native.ROOT / "prep-input"
ARTIFACT = native.ROOT / "sealed-fixture"
TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=128m,uid=65532,gid=65532,mode=0700"


def build_readiness(log):
    """A traceback/source substring must never serve as build success evidence."""
    records = []
    for line in log.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        if not isinstance(row, dict) or row.get("event") != "synthetic-graph-ready":
            continue
        if (
            set(row)
            != {"event", "schema", "tile_dir", "tiles", "tile_bytes", "pbf_sha256"}
            or row["schema"] != builder.READY_SCHEMA
            or row["tile_dir"] != str(builder.TILE_DIR)
            or type(row["tiles"]) is not int
            or not 1 <= row["tiles"] <= 256
            or type(row["tile_bytes"]) is not int
            or not 1 <= row["tile_bytes"] <= fixture.MAX_BYTES
            or row["pbf_sha256"] != hashlib.sha256(fixture.synthetic_pbf()).hexdigest()
        ):
            raise ValueError("invalid build readiness record")
        records.append(row)
    if len(records) > 1:
        raise ValueError("ambiguous build readiness records")
    return records[0] if records else None


def verify_export(directory, ready):
    files = sorted((directory / "tiles").rglob("*.gph"))
    if (
        len(files) != ready["tiles"]
        or sum(p.stat().st_size for p in files) != ready["tile_bytes"]
    ):
        raise ValueError("export differs from build readiness count/bytes")
    return len(files)


def prepare_code_permissions():
    # Only checksum-verified public proposal code/config; never parent/home paths.
    native.ROOT.chmod(0o755)
    for name in sorted(native.SOURCE_FILES | {"SHA256SUMS"}):
        path = native.ROOT / name
        if path.is_symlink():
            raise ValueError("linked proposal source")
        if path.parent != native.ROOT:
            path.parent.chmod(0o755)
        path.chmod(0o644)


def unpack_tiles(raw, destination):
    if len(raw) > 101 * 1024**2 or destination.exists():
        raise ValueError("archive/destination outside preparation bounds")
    destination.mkdir()
    total = count = 0
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
        for index, entry in enumerate(archive):
            if index >= 1024:
                raise ValueError("too many archive entries")
            if entry.isdir():
                if not re.fullmatch(
                    r"tiles(?:/[0-2](?:/[0-9]{3}){0,3})?", entry.name.rstrip("/")
                ):
                    raise ValueError("unapproved archive directory")
                continue  # Never restore archive permissions/ownership.
            if not entry.isfile() or not re.fullmatch(
                r"tiles/[0-2]/(?:[0-9]{3}/){1,3}[0-9]{3}\.gph", entry.name
            ):
                raise ValueError("unapproved archive entry")
            total += entry.size
            count += 1
            if (
                entry.name in seen
                or entry.size <= 0
                or total > fixture.MAX_BYTES
                or count > 256
            ):
                raise ValueError("duplicate/empty/oversized graph archive")
            seen.add(entry.name)
            target = destination / entry.name
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as output:
                source = archive.extractfile(entry)
                while chunk := source.read(1024**2):
                    output.write(chunk)
    if not count:
        raise ValueError("archive contains no graph tiles")


class Preparation(native.NativeReview):
    def builder_state(self, checkpoint, require_running=True):
        row = self.document("container", "inspect", self.container_id)[0]
        if (
            row["Id"] != self.container_id
            or row["Config"]["Labels"].get(native.LABEL_KEY) != self.token
        ):
            raise RuntimeError("builder identity/ownership changed")
        state = row["State"]
        native.emit(
            "builder-state",
            checkpoint=checkpoint,
            container_id=self.container_id,
            running=state["Running"],
            oom_killed=state["OOMKilled"],
            exit_code=state["ExitCode"],
        )
        if state["OOMKilled"] or (
            not state["Running"] and (require_running or state["ExitCode"] != 0)
        ):
            raise RuntimeError(
                "builder not running or exceeded bounds; no export/retry"
            )
        return state

    def ready(self):
        record = build_readiness(self.logs())
        self.builder_state("readiness")
        if record:
            native.emit(
                "build-ready", **{k: v for k, v in record.items() if k != "event"}
            )
        return record

    def prepare(self):
        if INPUT.exists() or ARTIFACT.exists() or (INPUT / "copy").exists():
            raise RuntimeError("preparation paths already exist; no overwrite")
        self.host_preflight(
            [
                ("container", BUILDER),
                ("container", native.ROUTER),
                ("network", native.NETWORK),
            ],
            native.ROOT,
        )
        prepare_code_permissions()
        fixture.prepare(INPUT)
        self.ensure_other_stage_idle()
        label = f"{native.LABEL_KEY}={self.token}"
        self.created.append(("container", BUILDER))
        self.docker(
            "create",
            "--pull=never",
            "--name",
            BUILDER,
            "--label",
            label,
            "--network",
            "none",
            "--restart=no",
            "--init",
            "--user",
            "65532:65532",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges:true",
            "--memory",
            "512m",
            "--memory-swap",
            "512m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--ulimit",
            "core=0",
            "--ulimit",
            "nofile=256:256",
            "--mount",
            f"type=bind,src={native.ROOT},dst=/proposal,readonly",
            "--mount",
            f"type=bind,src={INPUT},dst=/input,readonly",
            "--tmpfs",
            TMPFS,
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
            "--entrypoint",
            "/usr/bin/timeout",
            native.IMAGE,
            "--signal=TERM",
            "--kill-after=2s",
            "90s",
            "python3",
            "-I",
            "/proposal/build_fixture.py",
        )
        row = self.document(
            "container", "inspect", self.owned_ids[("container", BUILDER)]
        )[0]
        host = row["HostConfig"]
        if (
            row["Image"] != native.IMAGE_ID
            or row["Config"]["User"] != "65532:65532"
            or row["Config"]["Labels"].get(native.LABEL_KEY) != self.token
            or not host["ReadonlyRootfs"]
            or host["Privileged"]
            or host["CapDrop"] != ["ALL"]
            or host["CapAdd"]
            or "no-new-privileges:true" not in host["SecurityOpt"]
            or host["RestartPolicy"]["Name"] != "no"
            or host["PidsLimit"] != 64
            or host["Memory"] != 512 * 1024**2
            or host["MemorySwap"] != 512 * 1024**2
            or host["NanoCpus"] != 10**9
            or host["NetworkMode"] != "none"
            or host["PortBindings"]
            or host["Tmpfs"] != {"/tmp": TMPFS.split(":", 1)[1]}
            or host["LogConfig"]
            != {
                "Type": "local",
                "Config": {"max-size": "1m", "max-file": "1", "compress": "false"},
            }
        ):
            raise RuntimeError("preparation isolation mismatch")
        binds = {
            (m["Source"], m["Destination"], m["RW"])
            for m in row["Mounts"]
            if m["Type"] == "bind"
        }
        if binds != {
            (str(native.ROOT), "/proposal", False),
            (str(INPUT), "/input", False),
        }:
            raise RuntimeError("preparation bind mounts differ")
        if any(m["Type"] not in {"bind", "tmpfs"} for m in row["Mounts"]):
            raise RuntimeError("unexpected preparation mount")
        self.container_id = row["Id"]
        self.docker("start", self.container_id)
        controls = self.document(
            "exec", self.container_id, "python3", "-I", "-c", native.CHECKS
        )
        if controls != {
            "memory.max": str(512 * 1024**2),
            "cpu.max": "100000 100000",
            "pids.max": "64",
        }:
            raise RuntimeError("preparation effective cgroups mismatch")
        ready = self.wait_for(self.ready, 65, attempts=16)
        if not ready:
            raise TimeoutError("tiny build did not finish; no enlargement/retry")
        self.builder_state("before-export")
        raw = self.stream_archive(
            [
                "exec",
                self.container_id,
                "python3",
                "-I",
                "/proposal/build_fixture.py",
                "--export",
            ]
        )
        self.builder_state("after-export", require_running=False)
        unpack_tiles(raw, INPUT / "copy")
        copied_count = verify_export(INPUT / "copy", ready)
        native.emit(
            "graph-export-verified",
            tiles=copied_count,
            tile_bytes=ready["tile_bytes"],
            transport="bounded-in-container-python-tar",
        )
        (INPUT / "copy/tiles").rename(INPUT / "tiles")
        fixture.seal(INPUT, ARTIFACT)
        native.fixture_manifest(ARTIFACT)
        self.builder_state("after-seal", require_running=False)
        space = os.statvfs(native.ROOT)
        if self.initial_space - space.f_bavail * space.f_frsize > 2 * 1024**3:
            raise RuntimeError("2 GiB preparation added-space stop threshold")
        native.emit(
            "prepared",
            artifact="sealed-fixture",
            native_graph_built=True,
            generated_inputs_and_artifact_retained=True,
        )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    native.verify_proposal()
    if not args.execute:
        native.emit(
            "preparation-proposal-only",
            docker_calls=0,
            writes=0,
            builder=BUILDER,
            image=native.IMAGE,
            network="none",
            memory_mib=512,
            cpus=1,
            pids=64,
            tmpfs_mib=128,
            pbf_max_mib=1,
            artifact_max_mib=100,
            build_seconds=60,
        )
        return
    if not __debug__:
        raise RuntimeError("Python optimization unsupported")
    review = Preparation(ARTIFACT)

    def stop(signum, frame):
        raise InterruptedError("preparation interrupted")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        review.prepare()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        review.cleanup()
    receipt = {
        "schema": "modo-native-preparation-v1",
        "image": native.IMAGE_ID,
        "proposal_sha256": hashlib.sha256(
            (native.ROOT / "SHA256SUMS").read_bytes()
        ).hexdigest(),
        "manifest_sha256": hashlib.sha256(
            (ARTIFACT / "manifest.json").read_bytes()
        ).hexdigest(),
        "cleanup_passed": True,
    }
    path = native.ROOT / "preparation-receipt.json"
    with path.open("x") as stream:
        json.dump(receipt, stream, indent=2)
        stream.write("\n")
    path.chmod(0o444)


if __name__ == "__main__":
    main()
