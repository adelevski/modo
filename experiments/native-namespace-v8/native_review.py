"""Native-specific review proposal. Default mode makes zero Docker/HTTP calls."""

import argparse
import contextlib
import hashlib
import json
import math
import os
import platform
import re
import selectors
import signal
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.dont_write_bytecode = True

from namespace_client import Budget
from namespace_transport import NamespaceSession
from prepare_fixture import MAX_BYTES, synthetic_pbf

ROOT = Path(__file__).resolve().parent
IMAGE_ID = "sha256:f182f45e9e71cbe737e6c394d68808c67a7bd12e2a4cc47c4aba864de77ecdc6"
IMAGE = "ghcr.io/valhalla/valhalla-scripted:3.9.1@" + IMAGE_ID
ROUTER = "modo-native-review-router"
BUILDER = "modo-native-review-build"
NETWORK = "modo-native-review-network"
LIVE = ("valhalla-us-261011", "valhalla-illinois")
MAX_DOCKER_CALLS = 128
MAX_LOG_POLLS = 64
LABEL_KEY = "modo.native.review"
FAIRWAY_NAMES = {
    "fairway-safety-prepare-review",
    "fairway-safety-api-review",
    "fairway-safety-router-review",
}
TMPFS = "/tmp:rw,noexec,nosuid,nodev,size=32m,uid=65532,gid=65532,mode=0700"
CHECKS = """import errno,json,os
assert os.getuid()==65532
s=dict(x.split(':',1) for x in open('/proc/self/status') if ':' in x)
assert s['CapEff'].strip()=='0000000000000000'
assert s['NoNewPrivs'].strip()=='1' and s['Seccomp'].strip()=='2'
try:
 open('/native-review-write-denied','w').close()
except OSError as e: assert e.errno==errno.EROFS
else: raise AssertionError('root filesystem writable')
print(json.dumps({p:open('/sys/fs/cgroup/'+p).read().strip()
 for p in ('memory.max','cpu.max','pids.max')}))
"""
SOURCE_FILES = {
    "README.md",
    "fixture-config.json",
    "native_review.py",
    "prepare_review.py",
    "prepare_fixture.py",
    "build_fixture.py",
    "tests/test_native_review.py",
    "namespace_client.py",
    "namespace_transport.py",
    "tests/test_namespace_client.py",
    "tests/test_namespace_transport.py",
}


def verify_proposal():
    rows = (ROOT / "SHA256SUMS").read_text().splitlines()
    seen = set()
    for row in rows:
        digest, name = row.split("  ", 1)
        if (
            name not in SOURCE_FILES
            or name in seen
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise ValueError("unreviewed proposal manifest entry")
        path = ROOT / name
        if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("proposal source digest mismatch")
        seen.add(name)
    if seen != SOURCE_FILES:
        raise ValueError("incomplete proposal source manifest")
    allowed = {p.split("/")[0] for p in SOURCE_FILES} | {
        "SHA256SUMS",
        "prep-input",
        "sealed-fixture",
        "preparation-receipt.json",
    }
    if any(p.is_symlink() or p.name not in allowed for p in ROOT.iterdir()):
        raise ValueError("unexpected proposal top-level entry")
    actual = set()
    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {
            "prep-input",
            "sealed-fixture",
            "preparation-receipt.json",
        }:
            continue
        if path.is_symlink():
            raise ValueError("linked proposal source tree")
        if path.is_file():
            actual.add(str(relative))
    if actual != SOURCE_FILES | {"SHA256SUMS"}:
        raise ValueError("unexpected source file in proposal")


def diagnostic_stderr(raw, truncated=False):
    """Persist a bounded redacted preview, never argv, environment or stdout."""
    raw = raw or b""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    text = raw[:8192].decode("utf-8", "replace")
    text = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "?", text)
    text = re.sub(r"(?is)-----BEGIN[^\n]*PRIVATE KEY-----.*", "[redacted key]", text)
    text = re.sub(
        r"(?im)\b([\w-]*(?:password|passwd|token|secret|credential|key|authorization)[\w-]*)[\"']?\s*[:=][^\n]*",
        r"\1=[redacted]",
        text,
    )
    text = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [redacted]", text)
    text = re.sub(r"(?:https?|tcp|ssh)://[^\s\"']+", "[url]", text)
    text = re.sub(
        r"/(?:home|root|Users|tmp|run/user)/[^\s\"';,]+", "[private-path]", text
    )
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"';,]+", "[private-path]", text)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email]", text)
    encoded = text.encode("utf-8")
    return encoded[:4096].decode("utf-8", "ignore"), bool(
        truncated or len(raw) > 8192 or len(encoded) > 4096
    )


def emit(event, **fields):
    print(json.dumps({"event": event, **fields}), flush=True)


def fixture_manifest(directory):
    if (
        not directory.is_absolute()
        or directory.resolve() != directory
        or any(c in str(directory) for c in ",\n\r")
    ):
        raise ValueError("fixture must be an absolute ordinary directory")
    manifest_path = directory / "manifest.json"
    if manifest_path.is_symlink() or manifest_path.stat().st_size > 65536:
        raise ValueError("untrusted manifest file")
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("linked fixture tree")
    manifest = json.loads(manifest_path.read_text())
    if (
        manifest.get("schema") != "modo-native-synthetic-v1"
        or manifest.get("grid_side") != 64
    ):
        raise ValueError("unreviewed fixture schema/grid")
    if manifest.get("fixture_date") != "2026-10-13":
        raise ValueError("unreviewed fixture date")
    rows = manifest["files"]
    if not 3 <= len(rows) <= 258:
        raise ValueError("fixture file count")
    seen, total = set(), 0
    for row in rows:
        name = row["file"]
        if name not in {"config.json", "synthetic.osm.pbf"} and not re.fullmatch(
            r"tiles/[0-2]/(?:[0-9]{3}/){1,3}[0-9]{3}\.gph", name
        ):
            raise ValueError("unapproved fixture path")
        if name in seen or type(row["bytes"]) is not int or row["bytes"] <= 0:
            raise ValueError("duplicate/empty fixture input")
        if not re.fullmatch(r"[0-9a-f]{64}", row["sha256"]):
            raise ValueError("invalid fixture digest")
        seen.add(name)
        path = directory / name
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != row["bytes"]
        ):
            raise ValueError("missing/linked/wrong-size fixture input")
        if path.stat().st_mode & 0o222:
            raise ValueError("fixture inputs must be sealed read-only")
        total += row["bytes"]
        if (
            total > MAX_BYTES
            or hashlib.sha256(path.read_bytes()).hexdigest() != row["sha256"]
        ):
            raise ValueError("fixture size/digest mismatch")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink() or path.stat().st_mode & 0o222:
            raise ValueError("linked or writable fixture tree")
        if path.is_file():
            actual.add(str(path.relative_to(directory)))
    if directory.stat().st_mode & 0o222 or actual != seen | {"manifest.json"}:
        raise ValueError("unexpected or writable fixture tree")
    if (directory / "config.json").read_bytes() != (
        ROOT / "fixture-config.json"
    ).read_bytes():
        raise ValueError("config differs from proposal")
    if (directory / "synthetic.osm.pbf").read_bytes() != synthetic_pbf():
        raise ValueError("not the reviewed synthetic graph input")
    return manifest


def thor_ids(log):
    return {int(n) for n in re.findall(r"Got Thor Request ([0-9]+)\b", log)}


def loki_ids(log):
    return {int(n) for n in re.findall(r"Got Loki Request ([0-9]+)\b", log)}


def interrupted(log, request_id):
    return re.search(rf"\bRequest {request_id} was interrupted\b", log) is not None


def interruption_count(log, request_id):
    return len(re.findall(rf"\bRequest {request_id} was interrupted\b", log))


class NativeReview:
    def __init__(self, fixture, runner=subprocess.run):
        self.fixture = fixture
        self.runner = runner
        self.token = uuid.uuid4().hex
        self.deadline = time.monotonic() + 90
        self.command = [
            "docker",
            "--host",
            f"unix:///run/user/{os.getuid()}/docker.sock",
        ]
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("DOCKER_")}
        self.created = []
        self.uncertain_creations = set()
        self.owned_ids = {}
        self.docker_calls = self.log_polls = self.cleanup_calls = 0
        self.cleanup_mode = False
        self.stream_clients = []
        self.baseline = None
        self.budget = Budget()
        self.session = None
        self.proposal_sha256 = hashlib.sha256(
            (ROOT / "SHA256SUMS").read_bytes()
        ).hexdigest()

    def namespace_source(self):
        manifest = (ROOT / "SHA256SUMS").read_bytes()
        if hashlib.sha256(manifest).hexdigest() != self.proposal_sha256:
            raise ValueError("proposal manifest changed before client launch")
        rows = {
            name: digest
            for digest, name in (
                line.split("  ", 1) for line in manifest.decode().splitlines()
            )
        }
        path = ROOT / "namespace_client.py"
        if path.is_symlink():
            raise ValueError("linked namespace client source")
        source = path.read_bytes()
        if hashlib.sha256(source).hexdigest() != rows["namespace_client.py"]:
            raise ValueError("namespace client source changed before launch")
        return source.decode()

    def time_left(self, limit):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native stage deadline")
        return min(limit, remaining)

    def command_budget(self, args):
        if self.cleanup_mode:
            if self.cleanup_calls >= 16:
                raise RuntimeError("cleanup command budget exhausted")
            self.cleanup_calls += 1
        else:
            if self.docker_calls >= MAX_DOCKER_CALLS:
                raise RuntimeError("Docker command budget exhausted")
            self.docker_calls += 1
        if args[0] == "logs":
            if self.log_polls >= MAX_LOG_POLLS:
                raise RuntimeError("log polling budget exhausted")
            self.log_polls += 1

    def failed_command(
        self,
        args,
        timeout,
        returncode=None,
        stderr=None,
        exception=None,
        checked=True,
        truncated=False,
    ):
        preview, clipped = diagnostic_stderr(stderr, truncated)
        known = (
            set(LIVE)
            | {BUILDER, ROUTER, NETWORK}
            | FAIRWAY_NAMES
            | set(self.owned_ids.values())
        )
        operation = list(
            args[:2] if args[0] in {"container", "network", "image"} else args[:1]
        )
        resources = []
        for arg in args:
            target = arg.split(":", 1)[0]
            if target in known and target not in resources:
                resources.append(target)
        emit(
            "docker-command-failed",
            operation=operation,
            resources=resources[:4],
            timeout_seconds=timeout,
            returncode=returncode,
            exception_type=type(exception).__name__ if exception else None,
            checked=checked,
            cleanup=self.cleanup_mode,
            stderr_preview=preview,
            stderr_truncated=clipped,
        )

    def docker(self, *args, check=True, timeout=5):
        remaining = self.time_left(timeout)
        self.command_budget(args)
        creation = None
        if args[0] == "create":
            creation = ("container", args[args.index("--name") + 1])
        elif args[:2] == ("network", "create"):
            creation = ("network", args[-1])
        if creation:
            emit(
                "creation-intent",
                kind=creation[0],
                name=creation[1],
                owner_token=self.token,
            )
        try:
            result = self.runner(
                self.command + list(args),
                env=self.env,
                capture_output=True,
                text=True,
                timeout=min(remaining, timeout),
                check=False,
            )
        except BaseException as error:  # Retain uncertainty on timeout/INT/TERM too.
            if creation:
                self.uncertain_creations.add(creation)
            self.failed_command(
                args,
                min(remaining, timeout),
                stderr=getattr(error, "stderr", None),
                exception=error,
                checked=check,
            )
            raise
        if creation and result.returncode:
            self.uncertain_creations.add(creation)
        if result.returncode:
            self.failed_command(
                args,
                min(remaining, timeout),
                returncode=result.returncode,
                stderr=result.stderr,
                checked=check,
            )
        if creation and not result.returncode:
            identifier = result.stdout.strip()
            if not re.fullmatch(r"[0-9a-f]{64}", identifier):
                self.uncertain_creations.add(creation)
                error = RuntimeError("creation returned no confirmed full resource ID")
                self.failed_command(
                    args,
                    min(remaining, timeout),
                    returncode=0,
                    stderr=result.stderr,
                    exception=error,
                    checked=check,
                )
                raise error
            self.owned_ids[creation] = identifier
            emit(
                "creation-confirmed",
                kind=creation[0],
                name=creation[1],
                resource_id=identifier,
                owner_token=self.token,
            )
        if check and result.returncode:
            raise RuntimeError(f"Docker {args[0]} failed, status {result.returncode}")
        return result

    def stream_archive(self, args, limit=101 * 1024**2, popen=subprocess.Popen):
        timeout = self.time_left(10)
        deadline = time.monotonic() + timeout
        self.command_budget(args)
        try:
            process = popen(
                self.command + list(args),
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except BaseException as error:
            self.failed_command(args, timeout, exception=error)
            raise
        self.stream_clients.append(process)
        raw, stderr = bytearray(), bytearray()
        clipped, returncode = False, None
        try:
            with selectors.DefaultSelector() as selector:
                for name in ("stdout", "stderr"):
                    stream = getattr(process, name)
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, data=name)
                open_streams = 2
                for _ in range(32768):
                    remaining = min(self.time_left(10), deadline - time.monotonic())
                    if remaining <= 0:
                        raise TimeoutError("archive copy deadline")
                    ready = selector.select(timeout=remaining)
                    if not ready:
                        raise TimeoutError("archive copy deadline")
                    for key, _ in ready:
                        size = (
                            min(65536, limit - len(raw) + 1)
                            if key.data == "stdout"
                            else 65536
                        )
                        chunk = os.read(key.fileobj.fileno(), size)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            open_streams -= 1
                        elif key.data == "stdout":
                            raw.extend(chunk)
                            if len(raw) > limit:
                                raise ValueError(
                                    "archive byte cap exceeded while streaming"
                                )
                        else:
                            available = max(0, 8192 - len(stderr))
                            stderr.extend(chunk[:available])
                            clipped |= len(chunk) > available
                    if not open_streams:
                        break
                else:
                    raise RuntimeError("archive streaming poll budget exhausted")
            remaining = min(self.time_left(10), deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("archive copy deadline")
            returncode = process.wait(timeout=remaining)
            if returncode:
                raise RuntimeError("bounded graph copy failed")
            self.stream_clients.remove(process)
            return bytes(raw)
        except BaseException as error:
            self.failed_command(
                args,
                timeout,
                returncode=returncode,
                stderr=bytes(stderr),
                truncated=clipped,
                exception=error,
            )
            raise
        finally:
            if process.poll() is None:
                process.kill()
            process.stdout.close()
            process.stderr.close()
            # Reap remaining owned clients only within cleanup's separate budget.

    def document(self, *args, timeout=5):
        return json.loads(self.docker(*args, timeout=timeout).stdout)

    def snapshot(self):
        return [
            {
                "id": r["Id"],
                "running": r["State"]["Running"],
                "networks": r["NetworkSettings"]["Networks"],
                "ports": r["HostConfig"]["PortBindings"],
            }
            for r in self.document("inspect", *LIVE)
        ]

    def preflight(self):
        fixture_manifest(self.fixture)  # Before any Docker call.
        receipt_path = ROOT / "preparation-receipt.json"
        if receipt_path.is_symlink() or receipt_path.stat().st_size > 4096:
            raise ValueError("untrusted preparation receipt")
        receipt = json.loads(receipt_path.read_text())
        if (
            receipt.get("schema") != "modo-native-preparation-v1"
            or receipt.get("image") != IMAGE_ID
            or receipt.get("proposal_sha256")
            != hashlib.sha256((ROOT / "SHA256SUMS").read_bytes()).hexdigest()
            or receipt.get("manifest_sha256")
            != hashlib.sha256((self.fixture / "manifest.json").read_bytes()).hexdigest()
            or receipt.get("cleanup_passed") is not True
        ):
            raise ValueError(
                "preparation was not successfully completed and cleaned up"
            )
        self.host_preflight(
            [("container", BUILDER), ("container", ROUTER), ("network", NETWORK)],
            self.fixture,
        )

    def host_preflight(self, reserved, filesystem):
        if os.getuid() == 0 or platform.machine() != "aarch64":
            raise RuntimeError("normal user/native aarch64 required")
        path = Path(f"/run/user/{os.getuid()}/docker.sock")
        metadata = path.stat()
        if not stat.S_ISSOCK(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise RuntimeError("expected owned local rootless socket")
        info = self.document("info", "--format", "{{json .}}")
        if (
            "name=rootless" not in info["SecurityOptions"]
            or info["CgroupVersion"] != "2"
            or info["CgroupDriver"] != "systemd"
        ):
            raise RuntimeError("rootless/systemd/cgroupv2 required")
        self.ensure_other_stage_idle()
        for kind, name in reserved:
            if self.docker(kind, "inspect", name, check=False).returncode == 0:
                raise RuntimeError("reserved name already exists")
        image = self.document("image", "inspect", IMAGE)[0]
        if (
            image["Id"] != IMAGE_ID
            or image["Architecture"] != "arm64"
            or image["Os"] != "linux"
            or IMAGE.split(":3.9.1")[0] + "@" + IMAGE_ID not in image["RepoDigests"]
        ):
            raise RuntimeError("installed image differs from reviewed pin")
        self.baseline = self.snapshot()
        if not all(row["running"] for row in self.baseline):
            raise RuntimeError("live engines were not both running before test")
        if os.stat(info["DockerRootDir"]).st_dev != filesystem.stat().st_dev:
            raise RuntimeError(
                "fixture and Docker storage must share filesystem accounting"
            )
        space = os.statvfs(filesystem)
        self.initial_space = space.f_bavail * space.f_frsize
        if self.initial_space < 4 * 1024**3:
            raise RuntimeError("at least 4 GiB free required")

    def ensure_other_stage_idle(self):
        labelled = self.docker(
            "container",
            "ls",
            "--filter",
            "status=running",
            "--filter",
            "label=fairway.safety.run=review",
            "--format",
            "{{.ID}}",
        )
        if labelled.stdout.strip():
            raise RuntimeError(
                "Fairway synthetic review stage is running; stages must be sequential"
            )
        names = self.docker(
            "container", "ls", "--filter", "status=running", "--format", "{{.Names}}"
        )
        if FAIRWAY_NAMES.intersection(names.stdout.splitlines()):
            raise RuntimeError(
                "Fairway review container is running, regardless of label"
            )

    def create(self):
        self.ensure_other_stage_idle()  # Recheck immediately before first resource creation.
        label = f"{LABEL_KEY}={self.token}"
        self.created.append(
            ("network", NETWORK)
        )  # Recover unknown command outcomes by token.
        self.docker("network", "create", "--internal", "--label", label, NETWORK)
        self.network_id = self.owned_ids[("network", NETWORK)]
        self.validate_network(
            self.document("network", "inspect", self.network_id)[0], set()
        )
        self.created.append(("container", ROUTER))
        self.docker(
            "create",
            "--pull=never",
            "--name",
            ROUTER,
            "--label",
            label,
            "--network",
            self.network_id,
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
            f"type=bind,src={self.fixture},dst=/fixture,readonly",
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
            IMAGE,
            "--signal=TERM",
            "--kill-after=2s",
            "90s",
            "/usr/local/bin/valhalla_service",
            "/fixture/config.json",
            "1",
        )
        self.container_id = self.owned_ids[("container", ROUTER)]
        row = self.document("container", "inspect", self.container_id)[0]
        if row["Id"] != self.container_id:
            raise RuntimeError("created container ID changed")
        self.validate_container(row, pending=True)
        self.validate_network(
            self.document("network", "inspect", self.network_id)[0], {self.container_id}
        )
        self.docker("start", row["Id"])
        self.validate_container(
            self.document("container", "inspect", self.container_id)[0]
        )
        controls = self.document(
            "exec", self.container_id, "python3", "-I", "-c", CHECKS
        )
        if controls != {
            "memory.max": str(512 * 1024**2),
            "cpu.max": "100000 100000",
            "pids.max": "64",
        }:
            raise RuntimeError("effective cgroup limits differ from proposal")
        network = self.document("network", "inspect", self.network_id)[0]
        self.validate_network(network, {self.container_id})
        members = network["Containers"]
        if set(members) != {self.container_id}:
            raise RuntimeError("unexpected member on test bridge")

    def validate_network(self, row, allowed_members):
        if (
            row.get("Id") != self.network_id
            or row.get("Name") != NETWORK
            or not row.get("Internal")
            or (row.get("Labels") or {}).get(LABEL_KEY) != self.token
            or not set(row.get("Containers", {})).issubset(allowed_members)
        ):
            raise RuntimeError("network identity/ownership/membership mismatch")

    def validate_container(self, row, pending=False):
        host = row["HostConfig"]
        mounts = row["Mounts"]
        if (
            row["Image"] != IMAGE_ID
            or row["Config"]["User"] != "65532:65532"
            or row["Config"]["Labels"].get(LABEL_KEY) != self.token
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
            or set(row["NetworkSettings"]["Networks"]) != {NETWORK}
            or row["NetworkSettings"]["Networks"][NETWORK].get("NetworkID")
            not in ({"", self.network_id} if pending else {self.network_id})
            or host.get("NetworkMode") != self.network_id
            or host.get("PortBindings") not in ({}, None)
            or any(row["NetworkSettings"].get("Ports", {}).values())
            or host.get("PublishAllPorts", False)
        ):
            raise RuntimeError("container isolation differs from proposal")
        binds = [m for m in mounts if m["Type"] == "bind"]
        if (
            len(binds) != 1
            or binds[0]["Source"] != str(self.fixture)
            or binds[0]["Destination"] != "/fixture"
            or binds[0]["RW"]
            or any(m["Type"] not in {"bind", "tmpfs"} for m in mounts)
            or any(m["Type"] == "tmpfs" and m["Destination"] != "/tmp" for m in mounts)
            or host["Tmpfs"] != {"/tmp": TMPFS.split(":", 1)[1]}
            or host["LogConfig"]
            != {
                "Type": "local",
                "Config": {"max-size": "1m", "max-file": "1", "compress": "false"},
            }
        ):
            raise RuntimeError("unexpected/writable mount")

    def logs(self, deadline=None):
        timeout = self.time_left(5)
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("diagnostic observation window exhausted")
        raw = self.docker("logs", "--tail", "200", self.container_id, timeout=timeout)
        text = raw.stdout + raw.stderr
        if len(text.encode()) > 2 * 1024**2:
            raise RuntimeError("stage log response bound")
        return text

    def wait_for(self, check, seconds=3, attempts=6, deadline=None):
        end = min(self.deadline, time.monotonic() + seconds)
        if deadline is not None:
            end = min(end, deadline)
        for _ in range(attempts):
            if time.monotonic() >= end:
                break
            result = check()
            if result:
                return result
            time.sleep(
                min(
                    max(0.1, seconds / max(1, attempts - 1)),
                    max(0, end - time.monotonic()),
                )
            )
        return None

    def diagnostic_deadline(self, *numbers):
        # Reserve >=1s for close/read rather than consume an HTTP deadline on
        # missing diagnostic log IDs. Queue observers reuse this SAME deadline.
        return min(
            self.deadline,
            time.monotonic() + 3,
            *(self.session.slot_deadline(number) - 1 for number in numbers),
        )

    def diagnostic_logs(self, deadline):
        if time.monotonic() >= deadline:
            return None
        try:
            result = self.logs(deadline=deadline)
            return result if time.monotonic() < deadline else None
        except (TimeoutError, subprocess.TimeoutExpired):
            return (
                None  # Bounded missing diagnostic evidence, never cancellation proof.
            )

    def new_warning_after_close(self, request_id, before_close):
        before_count = interruption_count(before_close, request_id)
        return bool(
            self.wait_for(
                lambda: interruption_count(self.logs(), request_id) > before_count
            )
        )

    @contextlib.contextmanager
    def connection(self, costing="auto"):
        self.time_left(5)
        number = self.session.open(costing)
        try:
            yield number
        finally:
            if number in self.session.slots and not self.session.failed:
                self.session.close_slot(number)

    def response(self, number):
        return self.session.read_slot(number)

    def pending(self, number):
        return self.session.pending(number)

    def client_event(self, **fields):
        emit("namespace-client-event", **fields)

    def matrix(self, costing="auto"):
        with self.connection(costing) as client:
            status, body = self.response(client)
        if status != 200 or len(body.get("sources_to_targets", [])) not in {2, 4}:
            raise RuntimeError("normal/recovery matrix failed")
        rows = body["sources_to_targets"]
        cells = [cell for row in rows for cell in row] if len(rows) == 2 else rows
        if len(cells) != 4 or any(
            not isinstance(cell, dict)
            or type(cell.get("time")) not in {int, float}
            or not math.isfinite(cell["time"])
            or cell["time"] < 0
            for cell in cells
        ):
            raise RuntimeError(
                "synthetic recovery matrix has missing/unreachable cells"
            )
        return body

    def runtime_state(self, checkpoint, timeout):
        row = self.document("container", "inspect", self.container_id, timeout=timeout)[
            0
        ]
        if (
            row["Id"] != self.container_id
            or row["Image"] != IMAGE_ID
            or row["Config"]["Labels"].get(LABEL_KEY) != self.token
        ):
            raise RuntimeError("startup resource identity/ownership mismatch")
        state = row["State"]
        result = {
            "running": state["Running"],
            "oom_killed": state["OOMKilled"],
            "exit_code": state["ExitCode"],
        }
        if (
            type(result["running"]) is not bool
            or type(result["oom_killed"]) is not bool
            or type(result["exit_code"]) is not int
        ):
            raise RuntimeError("invalid startup process state")
        emit(
            "runtime-state",
            checkpoint=checkpoint,
            container_id=self.container_id,
            **result,
        )
        return result

    def startup_diagnostics(self, error, attempts):
        # Two bounded owned-resource reads; no probe retry or unrelated logs.
        state, preview, clipped, available = None, "", False, False
        diagnostic_error_type = None
        try:
            state = self.runtime_state("startup-failure", min(1, self.time_left(1)))
            log = self.docker(
                "logs",
                "--tail",
                "20",
                self.container_id,
                timeout=min(1, self.time_left(1)),
                check=False,
            )
            if log.returncode == 0:
                preview, clipped = diagnostic_stderr(log.stdout + log.stderr)
                available = True
        except Exception as diagnostic_error:  # noqa: BLE001 - preserve original
            diagnostic_error_type = type(diagnostic_error).__name__
        emit(
            "runtime-startup-failed",
            exception_type=type(error).__name__,
            status_attempts=attempts,
            process_state=state,
            owned_log_preview=preview,
            log_truncated=clipped,
            log_available=available,
            diagnostic_error_type=diagnostic_error_type,
        )

    def await_startup(self):
        self.time_left(5)
        self.session = NamespaceSession(self)
        try:
            self.session.start()
            result = self.session.startup()
            emit("runtime-ready", client_network="own-ipv4-loopback", **result)
        except Exception as error:
            try:
                self.startup_diagnostics(error, self.budget.requests)
            except Exception:  # noqa: BLE001, S110 - preserve original if evidence sink fails
                pass
            raise

    def scenarios(self):
        self.await_startup()
        self.matrix("auto")
        self.matrix("pedestrian")
        before = thor_ids(self.logs())
        started = time.monotonic()
        with self.connection("auto") as client:
            observation_end = self.diagnostic_deadline(client)
            ids = self.wait_for(
                lambda: thor_ids(self.diagnostic_logs(observation_end) or "") - before,
                deadline=observation_end,
            )
            pending = ids and self.pending(client)
            before_close = self.diagnostic_logs(observation_end)
        fresh = bool(
            ids
            and before_close is not None
            and self.new_warning_after_close(min(ids), before_close)
        )
        self.matrix()
        emit(
            "disconnect",
            request_id=min(ids) if ids else None,
            pending_before_close=bool(pending),
            warning_present_before_close=bool(
                ids and before_close is not None and interrupted(before_close, min(ids))
            ),
            new_warning_after_close=fresh,
            stage_and_cause_proven=False,
            recovery_matrix_succeeded=True,
            elapsed_seconds=round(time.monotonic() - started, 3),
            outcome="inconclusive",
        )

        before = thor_ids(self.logs())
        with self.connection("auto") as first:
            observation_end = self.diagnostic_deadline(first)
            first_ids = self.wait_for(
                lambda: thor_ids(self.diagnostic_logs(observation_end) or "") - before,
                deadline=observation_end,
            )
            loki_baseline = self.diagnostic_logs(observation_end)
            before_loki = loki_ids(loki_baseline or "")
            with self.connection("auto") as second:
                observation_end = min(
                    observation_end, self.diagnostic_deadline(first, second)
                )
                second_ids = (
                    self.wait_for(
                        lambda: (
                            loki_ids(self.diagnostic_logs(observation_end) or "")
                            - before_loki
                        ),
                        deadline=observation_end,
                    )
                    if loki_baseline is not None
                    else None
                )
                thor_snapshot = self.diagnostic_logs(observation_end)
                before_thor = bool(
                    first_ids
                    and second_ids
                    and thor_snapshot is not None
                    and self.pending(first)
                    and not (second_ids & thor_ids(thor_snapshot))
                )
                before_second_close = self.diagnostic_logs(observation_end)
            self.response(first)
        fresh = bool(
            second_ids
            and before_second_close is not None
            and self.new_warning_after_close(min(second_ids), before_second_close)
        )
        self.matrix()
        emit(
            "queued-disconnect",
            request_id=min(second_ids) if second_ids else None,
            loki_started_without_thor_start=before_thor,
            before_close_baseline_available=before_second_close is not None,
            shared_observation_window_exhausted=time.monotonic() >= observation_end,
            warning_present_before_close=bool(
                second_ids
                and before_second_close is not None
                and interrupted(before_second_close, min(second_ids))
            ),
            new_warning_after_close=fresh,
            thor_queue_admission_proven=False,
            stage_and_cause_proven=False,
            recovery_matrix_succeeded=True,
            outcome="inconclusive",
        )

        before = thor_ids(self.logs())
        with self.connection("auto") as client:
            observation_end = self.diagnostic_deadline(client)
            ids = self.wait_for(
                lambda: thor_ids(self.diagnostic_logs(observation_end) or "") - before,
                deadline=observation_end,
            )
            status, _ = self.response(client)
        warning = bool(
            status == 504
            and ids
            and self.wait_for(lambda: interrupted(self.logs(), min(ids)))
        )
        self.matrix()
        emit(
            "server-timeout",
            request_id=min(ids) if ids else None,
            http_status=status,
            matching_warning_observed=warning,
            stage_and_cause_proven=False,
            native_worker_reclamation_proven=False,
            recovery_matrix_succeeded=True,
            outcome="inconclusive",
        )
        self.session.finish()
        emit(
            "native-smoke-finished",
            requests=self.budget.requests,
            cells=self.budget.cells,
            maximum_client_connections=2,
            native_capacity_proven=False,
            native_cancellation_proven=False,
            thor_queue_admission_or_skipping_proven=False,
            reclamation_latency_proven=False,
        )

    def require_full_id_absent(self, kind, identifier):
        if not re.fullmatch(r"[0-9a-f]{64}", identifier):
            raise RuntimeError("invalid recorded resource ID")
        listing = self.docker(
            kind,
            "ls",
            *(["--all", "--no-trunc"] if kind == "container" else ["--no-trunc"]),
            "--format",
            "{{.ID}}",
        )
        identifiers = listing.stdout.splitlines()
        if any(not re.fullmatch(r"[0-9a-f]{64}", value) for value in identifiers):
            raise RuntimeError("full-ID absence query returned malformed identifiers")
        if identifier in identifiers:
            raise RuntimeError("recorded full ID remains; name absence is insufficient")

    def cleanup(self):
        self.deadline = time.monotonic() + 20
        self.cleanup_mode = True
        failures = []
        verified_absent = set()
        if self.session is not None:
            try:
                self.session.abort_cli()
            except (OSError, subprocess.TimeoutExpired, TimeoutError):
                failures.append("namespace-cli-not-reaped")
        for process in self.stream_clients:
            try:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=self.time_left(1))
            except (subprocess.TimeoutExpired, TimeoutError):
                failures.append("archive-client-not-reaped")
        for kind, name in reversed(self.created):
            try:
                identifier = self.owned_ids.get((kind, name), name)
                result = self.docker(kind, "inspect", identifier, check=False)
                if (kind, name) in self.uncertain_creations and result.returncode:
                    # Bounded reconciliation may find a late-created owned object.
                    # Empty listings cannot settle a still-running daemon operation.
                    for _ in range(2):
                        time.sleep(0.1)
                        result = self.docker(kind, "inspect", identifier, check=False)
                        if result.returncode == 0:
                            break
                if result.returncode:
                    # Absence must be confirmed by successful list, not daemon failure.
                    known_id = self.owned_ids.get((kind, name))
                    if known_id is not None:
                        self.require_full_id_absent(kind, known_id)
                        verified_absent.add((kind, known_id))
                        continue
                    names = self.docker(
                        kind,
                        "ls",
                        "--format",
                        "{{.Names}}" if kind == "container" else "{{.Name}}",
                        *(("--all",) if kind == "container" else ()),
                    )
                    if name in names.stdout.splitlines():
                        raise RuntimeError("resource existence unresolved")
                    continue
                row = json.loads(result.stdout)[0]
                labels = (
                    row["Config"].get("Labels")
                    if kind == "container"
                    else row.get("Labels")
                )
                if not labels or labels.get(LABEL_KEY) != self.token:
                    raise RuntimeError("cleanup ownership mismatch")
                if not re.fullmatch(r"[0-9a-f]{64}", row["Id"]):
                    raise RuntimeError("invalid owned resource ID")
                if (kind, name) in self.owned_ids and row["Id"] != self.owned_ids[
                    (kind, name)
                ]:
                    raise RuntimeError("owned resource ID changed")
                self.owned_ids[(kind, name)] = row["Id"]
                if kind == "container":
                    self.docker("stop", "--time", "2", row["Id"], check=False)
                self.docker(
                    kind, "rm", *(["--force"] if kind == "container" else []), row["Id"]
                )
                self.require_full_id_absent(kind, row["Id"])
                verified_absent.add((kind, row["Id"]))
            except Exception:  # noqa: BLE001 - finish owned cleanup, never attempt live repair
                failures.append(name)
        for _, name in sorted(self.uncertain_creations):
            if name not in failures:
                failures.append(name)
        for (kind, name), identifier in self.owned_ids.items():
            if (kind, identifier) not in verified_absent and name not in failures:
                failures.append(name)
        if self.baseline is not None:
            try:
                if self.snapshot() != self.baseline:
                    failures.append("live-engine-metadata-changed")
            except Exception:  # noqa: BLE001 - cleanup must report verification failure
                failures.append("live-engine-metadata-unverified")
        emit(
            "cleanup",
            failures=failures,
            installed_image_retained=True,
            namespace_exec_attempted=bool(self.session and self.session.attempted),
            namespace_exec_finished=bool(self.session and self.session.finished),
            cli_reap_alone_proves_remote_termination=False,
            verified_absent_full_ids=[
                {"kind": kind, "resource_id": identifier}
                for kind, identifier in sorted(verified_absent)
            ],
            recorded_resource_ids=[
                {"kind": kind, "name": name, "resource_id": identifier}
                for (kind, name), identifier in sorted(self.owned_ids.items())
            ],
            uncertain_creation_outcomes=[
                {"kind": kind, "name": name, "owner_token": self.token}
                for kind, name in sorted(self.uncertain_creations)
            ],
        )
        if failures:
            raise RuntimeError("cleanup needs operator attention; no automatic repair")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--fixture-dir", type=Path)
    args = parser.parse_args(argv)
    verify_proposal()
    if not __debug__:
        raise RuntimeError("Python optimization unsupported by isolation checks")
    if not args.execute:
        emit(
            "proposal-only",
            docker_calls=0,
            http_requests=0,
            image=IMAGE,
            container=ROUTER,
            internal_bridge=NETWORK,
            published_ports=0,
            client_endpoint="owned-container IPv4 loopback8002",
            client_seconds=60,
            memory_mib=512,
            cpus=1,
            pids=64,
            tmpfs_mib=32,
            request_limit=12,
            cell_limit=48,
            container_seconds=90,
            cleanup_seconds=20,
        )
        return
    if args.fixture_dir is None:
        parser.error("--execute requires reviewed --fixture-dir")
    review = NativeReview(args.fixture_dir)

    def stop(signum, frame):
        raise InterruptedError("controller interrupted")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        review.preflight()
        review.create()
        review.scenarios()
        state = review.document("container", "inspect", review.container_id)[0]["State"]
        space = os.statvfs(review.fixture)
        if not state["Running"] or state["OOMKilled"]:
            raise RuntimeError("native engine exited or was OOM killed")
        if review.initial_space - space.f_bavail * space.f_frsize > 2 * 1024**3:
            raise RuntimeError("2 GiB added-space stop threshold")
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        review.cleanup()


if __name__ == "__main__":
    main()
