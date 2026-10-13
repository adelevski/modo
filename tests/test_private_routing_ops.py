"""Exercise repair safety on synthetic SQLite data without running maintenance."""

import hashlib
import importlib.util
import json
import os
import pathlib
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

OPS = pathlib.Path(__file__).resolve().parents[1] / "ops" / "private-national-routing"
SCHEMA = """
CREATE TABLE admins (
    admin_level INTEGER, iso_code TEXT, parent_admin INTEGER,
    name TEXT, name_en TEXT, drive_on_right INTEGER,
    allow_intersection_names INTEGER, default_language TEXT,
    supported_languages TEXT, geom TEXT
);
"""


class Collect:
    """Stand in for geometry aggregation; these tests concern parent selection."""

    def __init__(self):
        self.value = None

    def step(self, value):
        self.value = value

    def finalize(self):
        return self.value


@pytest.mark.parametrize("states,existing_parent", [(0, False), (2, False), (2, True)])
def test_admin_parent_insert_is_guarded(states, existing_parent):
    with sqlite3.connect(":memory:") as db:
        db.executescript(SCHEMA)
        db.create_aggregate("ST_Collect", 1, Collect)
        db.create_function("ST_UnaryUnion", 1, lambda geom: geom)
        db.create_function("CastToMultiPolygon", 1, lambda geom: geom)
        for _ in range(states):
            db.execute("INSERT INTO admins (admin_level, geom) VALUES (4, 'state')")
        if existing_parent:
            db.execute("INSERT INTO admins (admin_level, iso_code) VALUES (2, 'US')")
        db.commit()
        sql = (OPS / "data" / "repair-us-admin-parent.sql").read_text()
        for _ in range(2):
            db.executescript(sql)
            parents = db.execute(
                "SELECT rowid FROM admins WHERE admin_level = 2 AND iso_code = 'US'"
            ).fetchall()
            assert len(parents) == bool(states or existing_parent)
            if states:
                assert (
                    db.execute(
                        "SELECT COUNT(*) FROM admins WHERE admin_level = 4 AND parent_admin = ?",
                        parents[0],
                    ).fetchone()[0]
                    == states
                )


@pytest.mark.parametrize("states,existing_parent", [(0, False), (2, False), (2, True)])
def test_repair_database_preflight_is_read_only(tmp_path, states, existing_parent):
    path = tmp_path / "admins.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        for _ in range(states):
            db.execute("INSERT INTO admins (admin_level) VALUES (4)")
        if existing_parent:
            db.execute("INSERT INTO admins (admin_level, iso_code) VALUES (2, 'US')")
    before = path.read_bytes()
    script = (OPS / "rebuild-with-repaired-admins.sh").read_text()
    # Execute only the read-only Python preflight, never the maintenance shell.
    preflight = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    result = subprocess.run(
        [sys.executable, "-", str(path)],
        input=preflight,
        text=True,
        capture_output=True,
        check=False,
    )
    assert (result.returncode == 0) == bool(states and not existing_parent)
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["admins.sqlite"]
    if existing_parent:
        assert "already has a US parent" in result.stderr
    elif not states:
        assert "no state rows" in result.stderr


FAKE_DOCKER = """
import hashlib, json, os, pathlib, signal, sqlite3, sys, time
args = sys.argv[1:]
with open(os.environ["DOCKER_CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[0] == "rm":
    assert len(args[-1]) == 64 and all(c in "0123456789abcdef" for c in args[-1])
    sys.exit(0)
assert args[0] == "run"
phase = args[args.index("--entrypoint") + 1]
failure = os.environ.get("BUILD_FAILURE", "")
if failure == "name-collision":
    sys.exit(7)  # No container was created and no CID was recorded.
cid = hashlib.sha256(phase.encode()).hexdigest()
pathlib.Path(args[args.index("--cidfile") + 1]).write_text(cid + "\\n")
if failure == phase:
    sys.exit(7)
mounts = [args[i + 1] for i, arg in enumerate(args) if arg == "--mount"]
stage = next(pathlib.Path(part[4:]) for mount in mounts if "dst=/custom_files," not in mount and "dst=/custom_files" in mount and "readonly" not in mount for part in mount.split(",") if part.startswith("src="))
if phase == "spatialite":
    with sqlite3.connect(stage / "admins.sqlite") as db:
        db.execute("INSERT INTO admins (admin_level, iso_code) VALUES (2, 'US')")
        db.execute("UPDATE admins SET parent_admin = (SELECT rowid FROM admins WHERE admin_level = 2) WHERE admin_level = 4")
elif phase == "valhalla_build_tiles":
    if failure in {"signal", "ignore-term"}:
        if failure == "ignore-term":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        pathlib.Path(os.environ["BUILD_CLIENT_PID"]).write_text(str(os.getpid()))
        time.sleep(60)
    (stage / "valhalla_tiles").mkdir()
    (stage / "valhalla_tiles" / "new.gph").write_bytes(b"new tiles")
elif phase == "valhalla_build_extract" and failure != "empty-extract":
    (stage / "valhalla_tiles.tar").write_bytes(b"new archive")
"""


@pytest.fixture
def staged_build(tmp_path):
    source = tmp_path / "active"
    source.mkdir()
    with sqlite3.connect(source / "admins.sqlite") as db:
        db.executescript(SCHEMA)
        db.execute("INSERT INTO admins (admin_level) VALUES (4)")
    for name, content in {
        "valhalla.json": (OPS / "data" / "valhalla.json").read_bytes(),
        "repair-us-admin-parent.sql": (
            OPS / "data" / "repair-us-admin-parent.sql"
        ).read_bytes(),
        "timezones.sqlite": b"timezone fixture",
        "us-261011.osm.pbf": b"synthetic source fixture",
        "valhalla_tiles.tar": b"old archive",
        "valhalla_tiles/old.gph": b"old tiles",
    }.items():
        path = source / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(content)
    before = {
        str(p.relative_to(source)): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    }
    staging = tmp_path / "staged"
    staging.mkdir()
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("#!" + sys.executable + "\n" + FAKE_DOCKER)
    docker.chmod(0o755)
    calls = tmp_path / "docker-calls.jsonl"
    calls.touch()
    client_pid = tmp_path / "client.pid"
    env = {
        **os.environ,
        "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
        "DATA": str(source),
        "SCRIPT_DIR": str(OPS),
        "STAGING_ROOT": str(staging),
        "PBF": "us-261011.osm.pbf",
        "EXPECTED_PBF_SHA256": hashlib.sha256(before["us-261011.osm.pbf"]).hexdigest(),
        "IMAGE": "fixture-image",
        "DOCKER_CALLS": str(calls),
        "BUILD_CLIENT_PID": str(client_pid),
    }
    # Execute the staging functions against fixtures and a fake CLI only.
    # Production rootless/PBF preflight and actual Docker are never executed.
    functions = (
        (OPS / "rebuild-with-repaired-admins.sh")
        .read_text()
        .split("# Staging implementation.\n", 1)[1]
        .rsplit("\nstage_admin_repair\n", 1)[0]
    )
    command = ["bash", "-Eeuo", "pipefail", "-c", functions + "\nstage_admin_repair\n"]
    return source, before, staging, calls, client_pid, env, command


def assert_active_release_preserved(source, before, calls):
    assert {
        str(p.relative_to(source)): p.read_bytes()
        for p in source.rglob("*")
        if p.is_file()
    } == before
    commands = [json.loads(line) for line in calls.read_text().splitlines()]
    assert all(command[0] in {"run", "rm"} for command in commands)
    for command in commands:
        if command[0] != "run":
            assert len(command[-1]) == 64
            continue
        assert command[command.index("--network") + 1] == "none"
        assert "--publish" not in command
        for index, arg in enumerate(command):
            if arg == "--mount" and "src=" + str(source) in command[index + 1]:
                assert command[index + 1].endswith(
                    "src="
                    + str(source / "us-261011.osm.pbf")
                    + ",dst=/custom_files/us-261011.osm.pbf,readonly"
                )
    return commands


@pytest.mark.parametrize(
    "failure",
    [
        "",
        "spatialite",
        "valhalla_build_tiles",
        "valhalla_build_extract",
        "empty-extract",
        "name-collision",
    ],
)
def test_admin_rebuild_stages_without_mutating_the_active_release(
    staged_build, failure
):
    source, before, staging, calls, _, env, command = staged_build
    result = subprocess.run(
        command,
        env={**env, "BUILD_FAILURE": failure},
        capture_output=True,
        text=True,
        check=False,
    )
    commands = assert_active_release_preserved(source, before, calls)
    stages = list(staging.iterdir())
    assert len(stages) == 1
    manifest = stages[0] / "generation-manifest.json"
    if failure:
        assert result.returncode != 0
        assert "active data and service state were untouched" in result.stderr
        assert not manifest.exists()
        if failure == "name-collision":
            assert not any(command[0] == "rm" for command in commands)
    else:
        assert result.returncode == 0, result.stderr
        document = json.loads(manifest.read_text())
        assert document["status"] == "staged-unverified"
        assert not (stages[0] / "us-261011.osm.pbf").exists()
        for asset in document["assets"]:
            raw = (stages[0] / asset["file_name"]).read_bytes()
            assert asset["bytes"] == len(raw)
            assert asset["sha256"] == hashlib.sha256(raw).hexdigest()


@pytest.mark.parametrize("failure", ["signal", "ignore-term"])
def test_staged_rebuild_terminates_its_client_on_sigterm(staged_build, failure):
    source, before, _, calls, client_pid, env, command = staged_build
    process = subprocess.Popen(
        command,
        env={**env, "BUILD_FAILURE": failure},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 5
        while (
            not client_pid.exists()
            and process.poll() is None
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert client_pid.exists(), process.communicate(timeout=1)
        process.send_signal(signal.SIGTERM)
        _, stderr = process.communicate(timeout=5)
        assert process.returncode == 143, stderr
        with pytest.raises(ProcessLookupError):
            os.kill(int(client_pid.read_text()), 0)
        commands = assert_active_release_preserved(source, before, calls)
        assert {call[-1] for call in commands if call[0] == "rm"} == {
            hashlib.sha256(phase.encode()).hexdigest()
            for phase in ["spatialite", "valhalla_build_tiles"]
        }
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)


@pytest.mark.parametrize(
    "problem",
    [
        "",
        "missing",
        "empty",
        "corrupt",
        "linked",
        "staged",
        "duplicate",
        "traversal",
        "boolean-size",
    ],
)
def test_runtime_manifest_verification_fails_closed_on_partial_or_wrong_release(
    staged_build, tmp_path, problem
):
    source, before, _, _, _, _, _ = staged_build
    spec = importlib.util.spec_from_file_location(
        "runtime_verifier", OPS / "verify-runtime-files.py"
    )
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    manifest = json.loads((OPS / "data" / "generation-manifest.json").read_text())
    manifest["image"] = "fixture-image"
    for asset in [*manifest["assets"], *manifest["external_assets"]]:
        raw = before[asset["file_name"]]
        asset["bytes"] = len(raw)
        asset["sha256"] = hashlib.sha256(raw).hexdigest()
    archive = source / "valhalla_tiles.tar"
    if problem == "missing":
        archive.unlink()
    elif problem == "empty":
        archive.write_bytes(b"")
    elif problem == "corrupt":
        archive.write_bytes(b"x" * len(before[archive.name]))
    elif problem == "linked":
        other = tmp_path / "external-archive"
        other.write_bytes(archive.read_bytes())
        archive.unlink()
        archive.symlink_to(other)
    elif problem == "staged":
        manifest["status"] = "staged-unverified"
    elif problem == "duplicate":
        manifest["assets"][1] = manifest["assets"][0]
    elif problem == "traversal":
        manifest["assets"][0]["file_name"] = "../valhalla.json"
    elif problem == "boolean-size":
        manifest["assets"][0]["bytes"] = True
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    if problem:
        with pytest.raises(ValueError):
            verifier.verify_runtime_files(source, path, "fixture-image")
    else:
        verifier.verify_runtime_files(source, path, "fixture-image")
