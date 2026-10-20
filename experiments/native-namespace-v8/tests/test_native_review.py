"""Local fixtures/mocked Docker only; no engine, Docker daemon or HTTP traffic."""

import contextlib
import copy
import hashlib
import io
import json
import os
import shutil
import struct
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import build_fixture
import namespace_client as namespace
import native_review as native
import prepare_fixture as prepare
import prepare_review


def fields(raw):
    """Independent wire reader for the limited standard PBF fixture structure."""
    offset = 0

    def integer():
        nonlocal offset
        result = shift = 0
        while True:
            byte = raw[offset]
            offset += 1
            result |= (byte & 127) << shift
            if byte < 128:
                return result
            shift += 7

    while offset < len(raw):
        tag = integer()
        if tag & 7 == 0:
            value = integer()
        elif tag & 7 == 2:
            size = integer()
            value = raw[offset : offset + size]
            offset += size
        else:
            raise AssertionError("unexpected wire type")
        yield tag >> 3, value


def unseal(path):
    path.chmod(0o755)
    for child in path.rglob("*"):
        if not child.is_symlink():
            child.chmod(0o755 if child.is_dir() else 0o644)


class FixtureTests(unittest.TestCase):
    def test_pbf_is_bounded_and_every_way_refers_to_existing_grid_nodes(self):
        raw = prepare.synthetic_pbf()
        self.assertLess(len(raw), 1024**2)
        blocks = []
        stream = io.BytesIO(raw)
        while prefix := stream.read(4):
            header = dict(fields(stream.read(struct.unpack(">I", prefix)[0])))
            block = dict(fields(stream.read(header[3])))
            blocks.append((header[1], block[1]))
        self.assertEqual([b[0] for b in blocks], [b"OSMHeader", b"OSMData"])
        self.assertIn((4, b"OsmSchema-V0.6"), list(fields(blocks[0][1])))
        groups = [g for field, g in fields(blocks[1][1]) if field == 2]
        nodes = [dict(fields(node)) for field, node in fields(groups[0]) if field == 1]
        ways = [dict(fields(way)) for field, way in fields(groups[1]) if field == 3]
        self.assertEqual(len(nodes), 4096)
        self.assertEqual(len(ways), 128)
        ids = {n[1] // 2 for n in nodes}
        for way in ways:
            # Packed fields contain raw varints: use the independent reader with tags.
            data = way[8]
            stream = io.BytesIO(data)
            current = 0
            while stream.tell() < len(data):
                shift = encoded = 0
                while True:
                    b = stream.read(1)[0]
                    encoded |= (b & 127) << shift
                    if b < 128:
                        break
                    shift += 7
                current += (encoded >> 1) ^ -(encoded & 1)
                self.assertIn(current, ids)
        with self.assertRaises(ValueError):
            prepare.synthetic_pbf(65)

    def test_sealed_generation_rejects_extra_tampered_and_linked_inputs(self):
        for problem in [
            "none",
            "extra",
            "tamper",
            "linked",
            "writable",
            "manifest-link",
        ]:
            with self.subTest(problem=problem), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                build = root / "build"
                prepare.prepare(build)
                tile = build / "tiles/2/000/001/002.gph"
                tile.parent.mkdir(parents=True)
                tile.write_bytes(b"synthetic mock tile, not native build evidence")
                sealed = root / "sealed"
                prepare.seal(build, sealed)
                try:
                    if problem != "none":
                        unseal(sealed)
                        if problem == "extra":
                            (sealed / "extra.txt").write_text("unexpected")
                        elif problem == "tamper":
                            (sealed / "tiles/2/000/001/002.gph").write_bytes(
                                b"x" * tile.stat().st_size
                            )
                        elif problem == "linked":
                            target = sealed / "tiles/2/000/001/002.gph"
                            target.unlink()
                            target.symlink_to(tile)
                        elif problem == "manifest-link":
                            manifest = root / "outside.json"
                            shutil.copyfile(sealed / "manifest.json", manifest)
                            (sealed / "manifest.json").unlink()
                            (sealed / "manifest.json").symlink_to(manifest)
                        if problem != "writable":
                            for item in sealed.rglob("*"):
                                if not item.is_symlink():
                                    item.chmod(0o555 if item.is_dir() else 0o444)
                            sealed.chmod(0o555)
                        with self.assertRaises(ValueError):
                            native.fixture_manifest(sealed)
                    else:
                        self.assertEqual(
                            native.fixture_manifest(sealed)["grid_side"], 64
                        )
                finally:
                    unseal(sealed)

    def test_builder_uses_only_one_bounded_native_process_and_synthetic_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "source", Path(tmp) / "output"
            prepare.prepare(source)

            def fake(command, **kwargs):
                self.assertEqual(command[0], "valhalla_build_tiles")
                self.assertEqual(
                    command[-3:], ["-j", "1", str(source / "synthetic.osm.pbf")]
                )
                self.assertEqual(kwargs["timeout"], 60)
                config = json.loads(Path(command[2]).read_text())
                self.assertEqual(config["mjolnir"]["tile_dir"], str(output / "tiles"))
                self.assertEqual(
                    json.loads((source / "config.json").read_text())["mjolnir"][
                        "tile_dir"
                    ],
                    "/fixture/tiles",
                )
                tile = output / "tiles/2/000/001/002.gph"
                tile.parent.mkdir(parents=True)
                tile.write_bytes(b"mock tile")

            with (
                patch.object(build_fixture.subprocess, "run", side_effect=fake),
                patch.object(build_fixture.time, "sleep") as sleep,
                contextlib.redirect_stdout(io.StringIO()) as printed,
            ):
                build_fixture.build(source, output)
            row = json.loads(printed.getvalue())
            self.assertEqual(row["schema"], build_fixture.READY_SCHEMA)
            self.assertEqual(row["tile_dir"], str(output / "tiles"))
            self.assertEqual(row["tiles"], 1)
            self.assertEqual(row["tile_bytes"], len(b"mock tile"))
            sleep.assert_called_once_with(20)

    def test_in_namespace_export_is_bounded_regular_tar_without_native_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            tile = output / "tiles/2/000/001/002.gph"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b"mock tile; not native graph evidence")
            raw = io.BytesIO()
            with patch.object(
                build_fixture.subprocess,
                "run",
                side_effect=AssertionError("native job forbidden"),
            ):
                build_fixture.export(output, raw)
            with tarfile.open(fileobj=io.BytesIO(raw.getvalue())) as tar:
                entries = tar.getmembers()
                self.assertEqual([e.name for e in entries], ["tiles/2/000/001/002.gph"])
                self.assertTrue(all(e.isfile() for e in entries))
                self.assertEqual(tar.extractfile(entries[0]).read(), tile.read_bytes())
            copied = output / "copy"
            prepare_review.unpack_tiles(raw.getvalue(), copied)
            self.assertEqual(
                (copied / "tiles/2/000/001/002.gph").read_bytes(), tile.read_bytes()
            )

    def test_export_rejects_empty_linked_unexpected_or_oversized_graph(self):
        for fault in ["missing", "empty", "linked", "path", "size", "count"]:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp)
                tile = output / "tiles/2/000/001/002.gph"
                if fault != "missing":
                    tile.parent.mkdir(parents=True)
                    tile.write_bytes(b"" if fault == "empty" else b"mock")
                if fault == "linked":
                    tile.unlink()
                    tile.symlink_to(output / "outside")
                if fault == "path":
                    tile.rename(output / "tiles/unexpected.gph")
                if fault == "count":
                    for i in range(257):
                        (tile.parent / f"{i:03}.gph").write_bytes(b"x")
                raw = io.BytesIO()
                with (
                    patch.object(
                        build_fixture,
                        "MAX_BYTES",
                        3 if fault == "size" else prepare.MAX_BYTES,
                    ),
                    self.assertRaises(ValueError),
                ):
                    build_fixture.export(output, raw)
                self.assertEqual(raw.getvalue(), b"")


class SafetyTests(unittest.TestCase):
    def ready_record(self):
        return {
            "event": "synthetic-graph-ready",
            "schema": build_fixture.READY_SCHEMA,
            "tile_dir": str(build_fixture.TILE_DIR),
            "tiles": 1,
            "tile_bytes": 4,
            "pbf_sha256": hashlib.sha256(prepare.synthetic_pbf()).hexdigest(),
        }

    def test_readiness_does_not_accept_traceback_or_source_substrings(self):
        for log in [
            'Traceback: "event": "synthetic-graph-ready"',
            "source says synthetic-graph-ready but failed",
            json.dumps({"message": "synthetic-graph-ready"}),
        ]:
            with self.subTest(log=log):
                self.assertIn(
                    "synthetic-graph-ready", log
                )  # Version4's false-positive condition.
                self.assertIsNone(prepare_review.build_readiness(log))
        self.assertEqual(
            prepare_review.build_readiness(json.dumps(self.ready_record())),
            self.ready_record(),
        )

    def test_readiness_rejects_incomplete_wrong_or_ambiguous_records(self):
        bad = [{"event": "synthetic-graph-ready"}]
        for key, value in [
            ("schema", "wrong"),
            ("tile_dir", "/fixture/tiles"),
            ("tiles", True),
            ("tiles", 257),
            ("tile_bytes", 0),
            ("pbf_sha256", "0" * 64),
            ("extra", "x"),
        ]:
            row = self.ready_record()
            row[key] = value
            bad.append(row)
        for row in bad:
            with self.subTest(row=row), self.assertRaises(ValueError):
                prepare_review.build_readiness(json.dumps(row))
        with self.assertRaises(ValueError):
            prepare_review.build_readiness(
                json.dumps(self.ready_record()) + "\n" + json.dumps(self.ready_record())
            )

    def test_export_count_or_bytes_mismatch_cannot_be_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tile = root / "tiles/2/000/001/002.gph"
            tile.parent.mkdir(parents=True)
            tile.write_bytes(b"mock")
            self.assertEqual(prepare_review.verify_export(root, self.ready_record()), 1)
            for key, value in [("tiles", 2), ("tile_bytes", 5)]:
                row = self.ready_record()
                row[key] = value
                with self.subTest(key=key), self.assertRaises(ValueError):
                    prepare_review.verify_export(root, row)

    def test_dead_or_oom_builder_cannot_advance_to_export_and_state_is_retained(self):
        for state in [
            {"Running": False, "OOMKilled": False, "ExitCode": 0},
            {"Running": True, "OOMKilled": True, "ExitCode": 0},
        ]:
            with self.subTest(state=state):
                review = prepare_review.Preparation(Path("/mock"))
                review.container_id = "a" * 64
                row = {
                    "Id": review.container_id,
                    "Config": {"Labels": {native.LABEL_KEY: review.token}},
                    "State": state,
                }
                with (
                    patch.object(
                        review, "logs", return_value=json.dumps(self.ready_record())
                    ),
                    patch.object(review, "document", return_value=[row]),
                    patch.object(review, "stream_archive") as export,
                    contextlib.redirect_stdout(io.StringIO()) as printed,
                    self.assertRaises(RuntimeError),
                ):
                    review.ready()
                export.assert_not_called()
                event = json.loads(printed.getvalue())
                self.assertEqual(event["checkpoint"], "readiness")
                self.assertEqual(event["running"], state["Running"])
                self.assertEqual(event["oom_killed"], state["OOMKilled"])

    def test_failed_start_retains_sanitized_bounded_result_context(self):
        reason = "failed to initialize logging driver: compression cannot be enabled when max file count is 1"
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                "unused", 1, "DO-NOT-LOG-STDOUT", reason
            )
        )
        review = native.NativeReview(Path("/mock"), runner)
        review.owned_ids[("container", native.BUILDER)] = "a" * 64
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(RuntimeError),
        ):
            review.docker("start", "a" * 64)
        event = json.loads(output.getvalue())
        self.assertEqual(event["operation"], ["start"])
        self.assertEqual(event["resources"], ["a" * 64])
        self.assertEqual(event["returncode"], 1)
        self.assertEqual(event["timeout_seconds"], 5)
        self.assertEqual(event["stderr_preview"], reason)
        self.assertNotIn("DO-NOT-LOG-STDOUT", output.getvalue())
        self.assertNotIn("env", event)
        self.assertNotIn("argv", event)
        runner.assert_called_once()

    def test_unchecked_failure_keeps_its_result_and_diagnostics(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess(
                "unused", 1, "", "No such container"
            )
        )
        review = native.NativeReview(Path("/mock"), runner)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = review.docker("container", "inspect", native.ROUTER, check=False)
        event = json.loads(output.getvalue())
        self.assertEqual(result.returncode, 1)
        self.assertEqual(event["operation"], ["container", "inspect"])
        self.assertFalse(event["checked"])
        self.assertEqual(event["resources"], [native.ROUTER])

    def test_timeout_logs_partial_stderr_without_settling_creation(self):
        failure = subprocess.TimeoutExpired(
            "never-log-argv",
            5,
            output=b"NEVER-LOG-STDOUT",
            stderr=b"token=SECRET-VALUE\npermission denied",
        )
        review = native.NativeReview(Path("/mock"), Mock(side_effect=failure))
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            review.docker("create", "--name", native.BUILDER)
        event = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual(event["exception_type"], "TimeoutExpired")
        self.assertIsNone(event["returncode"])
        self.assertIn(("container", native.BUILDER), review.uncertain_creations)
        self.assertIn("permission denied", event["stderr_preview"])
        for secret in ["SECRET-VALUE", "never-log-argv", "NEVER-LOG-STDOUT"]:
            self.assertNotIn(secret, output.getvalue())

    def test_diagnostic_redaction_and_utf8_byte_cap(self):
        raw = (
            "permission denied\x1b\x00\n"
            "Authorization: Bearer AUTH-VALUE\n"
            '"password": "PASS-VALUE"\n'
            "AWS_ACCESS_KEY_ID=KEY-VALUE\n"
            "/home/private/.ssh/id_key C:\\Users\\private\\id_key\n"
            "https://user:URL-VALUE@example.test/x?secret=QUERY-VALUE\n"
            "someone@example.test\n"
            "-----BEGIN PRIVATE KEY-----\nPRIVATE-KEY-VALUE\n-----END PRIVATE KEY-----"
        )
        preview, _ = native.diagnostic_stderr(raw)
        for value in [
            "AUTH-VALUE",
            "PASS-VALUE",
            "KEY-VALUE",
            "URL-VALUE",
            "QUERY-VALUE",
            "PRIVATE-KEY-VALUE",
            "/home/private",
            "C:\\Users",
            "someone@example.test",
            "\x1b",
            "\x00",
        ]:
            self.assertNotIn(value, preview)
        self.assertIn("permission denied", preview)
        preview, truncated = native.diagnostic_stderr(("å" * 10000).encode())
        self.assertTrue(truncated)
        self.assertLessEqual(len(preview.encode()), 4096)

    def test_archive_failure_drains_both_pipes_and_retains_only_bounded_stderr(self):
        review = native.NativeReview(Path("/mock"))
        review.owned_ids[("container", native.BUILDER)] = "a" * 64
        process = Mock()
        process.stdout.fileno.return_value = 9
        process.stderr.fileno.return_value = 10
        process.poll.return_value = 7
        process.wait.return_value = 7
        selector = Mock()
        selector.select.return_value = [
            (SimpleNamespace(fileobj=process.stdout, data="stdout"), 0),
            (SimpleNamespace(fileobj=process.stderr, data="stderr"), 0),
        ]
        chunks = {
            9: iter([b"DO-NOT-LOG-ARCHIVE", b""]),
            10: iter([b"password=HIDDEN\n" + b"x" * 10000, b""]),
        }
        factory = Mock(return_value=process)
        with (
            patch.object(native.selectors, "DefaultSelector") as selectors,
            patch.object(native.os, "set_blocking"),
            patch.object(
                native.os, "read", side_effect=lambda fd, size: next(chunks[fd])
            ),
            contextlib.redirect_stdout(io.StringIO()) as output,
            self.assertRaises(RuntimeError),
        ):
            selectors.return_value.__enter__.return_value = selector
            review.stream_archive(
                ["cp", "a" * 64 + ":/tmp/native-output/tiles", "-"], popen=factory
            )
        event = json.loads(output.getvalue())
        self.assertEqual(event["operation"], ["cp"])
        self.assertEqual(event["resources"], ["a" * 64])
        self.assertEqual(event["returncode"], 7)
        self.assertTrue(event["stderr_truncated"])
        self.assertLessEqual(len(event["stderr_preview"].encode()), 4096)
        self.assertNotIn("HIDDEN", output.getvalue())
        self.assertNotIn("DO-NOT-LOG-ARCHIVE", output.getvalue())
        self.assertEqual(factory.call_args.kwargs["stderr"], subprocess.PIPE)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()
        self.assertEqual(selector.unregister.call_count, 2)
        self.assertLessEqual(process.wait.call_args.kwargs["timeout"], 10)

    def test_diagnostic_failure_cannot_clear_unknown_creation_outcome(self):
        runner = Mock(
            return_value=subprocess.CompletedProcess("unused", 1, "", "create failed")
        )
        review = native.NativeReview(Path("/mock"), runner)
        with (
            patch.object(review, "failed_command", side_effect=BrokenPipeError()),
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(BrokenPipeError),
        ):
            review.docker("create", "--name", native.BUILDER)
        self.assertIn(("container", native.BUILDER), review.uncertain_creations)

    def test_expired_stage_refuses_http_before_constructor_and_budget(self):
        review = native.NativeReview(Path("/mock"))
        review.deadline = native.time.monotonic() - 1
        with (
            patch.object(namespace.http.client, "HTTPConnection") as constructor,
            self.assertRaises(TimeoutError),
            review.connection("auto"),
        ):
            self.fail("expired HTTP context entered")
        constructor.assert_not_called()
        self.assertEqual(review.budget.requests, 0)

    def test_docker_and_log_poll_budgets_refuse_further_client_calls(self):
        for attr, cap, args in [
            ("docker_calls", native.MAX_DOCKER_CALLS, ("inspect", "x")),
            ("log_polls", native.MAX_LOG_POLLS, ("logs", "x")),
        ]:
            with self.subTest(attr=attr):
                runner = Mock()
                review = native.NativeReview(Path("/mock"), runner)
                setattr(review, attr, cap)
                with self.assertRaises(RuntimeError):
                    review.docker(*args)
                runner.assert_not_called()

    def test_preclose_warning_is_not_a_fresh_disconnect_warning(self):
        review = native.NativeReview(Path("/mock"))
        log = "Request 11 was interrupted\n"
        with (
            patch.object(review, "logs", return_value=log),
            patch.object(native.time, "sleep"),
        ):
            self.assertFalse(review.new_warning_after_close(11, log))
        with patch.object(review, "logs", return_value=log + log):
            self.assertTrue(review.new_warning_after_close(11, log))

    def test_network_full_id_owner_and_membership_are_required(self):
        review = native.NativeReview(Path("/mock"))
        row = self.row(review)
        bridge = {
            "Id": review.network_id,
            "Name": native.NETWORK,
            "Internal": True,
            "Labels": {native.LABEL_KEY: review.token},
            "Containers": {},
        }
        review.validate_network(bridge, set())
        for field, value in [
            ("Id", "c" * 64),
            ("Labels", {native.LABEL_KEY: "foreign"}),
            ("Containers", {"c" * 64: {}}),
        ]:
            with self.subTest(field=field):
                bad = copy.deepcopy(bridge)
                bad[field] = value
                with self.assertRaises(RuntimeError):
                    review.validate_network(bad, {"a" * 64})
        row["NetworkSettings"]["Networks"][native.NETWORK]["NetworkID"] = ""
        review.validate_container(row, pending=True)
        with self.assertRaises(RuntimeError):
            review.validate_container(row)
        for field in ["NetworkMode", "NetworkID"]:
            bad = copy.deepcopy(row)
            if field == "NetworkMode":
                bad["HostConfig"][field] = native.NETWORK
            else:
                bad["NetworkSettings"]["Networks"][native.NETWORK][field] = "c" * 64
            with self.assertRaises(RuntimeError):
                review.validate_container(bad, pending=True)

    def test_creation_refuses_foreign_bridge_or_endpoint_before_further_work(self):
        for failure in ["bridge", "pending", "running"]:
            with self.subTest(failure=failure):
                review = native.NativeReview(Path("/mock"))
                pending = self.row(review)
                running = copy.deepcopy(pending)
                bridge = {
                    "Id": review.network_id,
                    "Name": native.NETWORK,
                    "Internal": True,
                    "Labels": {native.LABEL_KEY: review.token},
                    "Containers": {},
                }
                if failure == "bridge":
                    bridge["Labels"][native.LABEL_KEY] = "foreign"
                if failure == "pending":
                    pending["NetworkSettings"]["Networks"][native.NETWORK][
                        "NetworkID"
                    ] = "c" * 64
                if failure == "running":
                    running["NetworkSettings"]["Networks"][native.NETWORK][
                        "NetworkID"
                    ] = ""
                calls = []

                def docker(*args, calls=calls, review=review, **kwargs):
                    calls.append(args)
                    if args[:2] == ("network", "create"):
                        review.owned_ids[("network", native.NETWORK)] = "b" * 64
                    if args[0] == "create":
                        review.owned_ids[("container", native.ROUTER)] = "a" * 64

                with (
                    patch.object(review, "ensure_other_stage_idle"),
                    patch.object(review, "docker", side_effect=docker),
                    patch.object(
                        review,
                        "document",
                        side_effect=[[bridge], [pending], [bridge], [running]],
                    ),
                    self.assertRaises(RuntimeError),
                ):
                    review.create()
                self.assertFalse(any(call[0] == "exec" for call in calls))
                self.assertEqual(
                    any(call[0] == "start" for call in calls), failure == "running"
                )

    def test_archive_stream_caps_bytes_and_uses_remaining_deadline(self):
        for expired in [False, True]:
            with self.subTest(expired=expired):
                review = native.NativeReview(Path("/mock"))
                if expired:
                    review.deadline = native.time.monotonic() - 1
                process = Mock()
                process.poll.return_value = None
                process.stdout.fileno.return_value = 9
                selector = Mock()
                process.stderr.fileno.return_value = 10
                selector.select.return_value = [
                    (SimpleNamespace(fileobj=process.stdout, data="stdout"), 0),
                    (SimpleNamespace(fileobj=process.stderr, data="stderr"), 0),
                ]
                factory = Mock(return_value=process)
                with (
                    patch.object(native.selectors, "DefaultSelector") as selectors,
                    patch.object(native.os, "set_blocking"),
                    patch.object(native.os, "read", return_value=b"x" * 5),
                ):
                    selectors.return_value.__enter__.return_value = selector
                    with self.assertRaises(TimeoutError if expired else ValueError):
                        review.stream_archive(["cp", "x", "-"], limit=4, popen=factory)
                if expired:
                    factory.assert_not_called()
                else:
                    self.assertLessEqual(
                        selector.select.call_args.kwargs["timeout"], 10
                    )
                    process.kill.assert_called_once()
                    process.stdout.close.assert_called_once()

    def test_archive_stream_slow_eof_does_not_receive_a_new_wait_deadline(self):
        review = native.NativeReview(Path("/mock"))
        review.deadline = 100
        process = Mock()
        process.poll.return_value = 0
        process.wait.return_value = 0
        process.stdout.fileno.return_value = 9
        selector = Mock()
        process.stderr.fileno.return_value = 10
        selector.select.return_value = [
            (SimpleNamespace(fileobj=process.stdout, data="stdout"), 0),
            (SimpleNamespace(fileobj=process.stderr, data="stderr"), 0),
        ]
        with (
            patch.object(native.time, "monotonic", side_effect=[0, 0, 0, 0, 9, 9]),
            patch.object(native.selectors, "DefaultSelector") as selectors,
            patch.object(native.os, "set_blocking"),
            patch.object(native.os, "read", return_value=b""),
        ):
            selectors.return_value.__enter__.return_value = selector
            self.assertEqual(
                review.stream_archive(
                    ["cp", "x", "-"], popen=Mock(return_value=process)
                ),
                b"",
            )
        self.assertEqual(process.wait.call_args.kwargs["timeout"], 1)

    def test_public_input_and_code_permissions_ignore_restrictive_umask(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "proposal"
            root.mkdir(mode=0o700)
            neighbor = Path(directory) / "unrelated"
            neighbor.mkdir(mode=0o700)
            for name in native.SOURCE_FILES | {"SHA256SUMS"}:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes((native.ROOT / name).read_bytes())
                path.chmod(0o600)
            previous = os.umask(0o077)
            try:
                with patch.object(native, "ROOT", root):
                    prepare_review.prepare_code_permissions()
                    prepare.prepare(root / "prep-input")
            finally:
                os.umask(previous)
            self.assertEqual(root.stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / "tests").stat().st_mode & 0o777, 0o755)
            self.assertEqual((root / "prep-input").stat().st_mode & 0o777, 0o755)
            self.assertTrue(
                all(
                    (root / name).stat().st_mode & 0o777 == 0o644
                    for name in native.SOURCE_FILES
                )
            )
            self.assertEqual(neighbor.stat().st_mode & 0o777, 0o700)

    def test_late_uncertain_owned_creation_removed_by_id_remains_unsettled(self):
        calls = []
        review = native.NativeReview(Path("/mock"))
        review.created = [("container", native.ROUTER)]
        review.uncertain_creations.add(("container", native.ROUTER))

        def runner(command, **kwargs):
            args = command[3:]
            calls.append(args)
            if args[1] == "inspect":
                if len(calls) == 1:
                    return subprocess.CompletedProcess(command, 1, "", "")
                row = {
                    "Id": "a" * 64,
                    "Config": {"Labels": {native.LABEL_KEY: review.token}},
                }
                return subprocess.CompletedProcess(command, 0, json.dumps([row]), "")
            return subprocess.CompletedProcess(command, 0, "", "")

        review.runner = runner
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            patch.object(native.time, "sleep"),
            self.assertRaises(RuntimeError),
        ):
            review.cleanup()
        self.assertIn(["container", "rm", "--force", "a" * 64], calls)
        self.assertTrue(
            json.loads(output.getvalue().splitlines()[-1])[
                "uncertain_creation_outcomes"
            ]
        )

    def test_create_timeout_empty_listing_does_not_confirm_cleanup(self):
        calls = []

        def runner(command, **kwargs):
            args = command[3:]
            calls.append(args)
            if args[0] == "create":
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return subprocess.CompletedProcess(
                command, 1 if args[1] == "inspect" else 0, "", ""
            )

        review = native.NativeReview(Path("/mock"), runner)
        review.created = [("container", native.ROUTER)]
        with self.assertRaises(subprocess.TimeoutExpired):
            review.docker("create", "--name", native.ROUTER)
        with (
            contextlib.redirect_stdout(io.StringIO()) as output,
            patch.object(native.time, "sleep"),
            self.assertRaises(RuntimeError),
        ):
            review.cleanup()
        event = json.loads(output.getvalue().splitlines()[-1])
        self.assertIn(native.ROUTER, event["failures"])
        self.assertTrue(event["uncertain_creation_outcomes"])
        self.assertLessEqual(
            sum(args[:2] == ["container", "inspect"] for args in calls), 3
        )

    def test_loki_only_interruption_does_not_claim_queued_thor_cancellation(self):
        review = native.NativeReview(Path("/mock"))

        @contextlib.contextmanager
        def connection(costing=None):
            yield SimpleNamespace(sock=object())

        waits = [{11}, True, {21}, {22}, True, {31}, True]
        events = []
        with (
            patch.object(review, "connection", connection),
            patch.object(review, "await_startup"),
            patch.object(
                review,
                "session",
                Mock(slot_deadline=Mock(return_value=native.time.monotonic() + 5)),
            ),
            patch.object(review, "matrix"),
            patch.object(
                review,
                "logs",
                return_value="Got Thor Request 11\nRequest 22 was interrupted",
            ),
            patch.object(review, "wait_for", side_effect=waits),
            patch.object(
                review,
                "response",
                side_effect=[(200, {}), (504, {})],
            ),
            patch.object(review, "pending", return_value=True),
            patch.object(
                native,
                "emit",
                side_effect=lambda event, **fields: events.append((event, fields)),
            ),
        ):
            review.scenarios()
        fields = next(
            fields for event, fields in events if event == "queued-disconnect"
        )
        self.assertEqual(fields["outcome"], "inconclusive")
        self.assertFalse(fields["thor_queue_admission_proven"])
        self.assertNotIn("observed_queued", fields)

    def test_failed_preparation_cleanup_cannot_issue_a_success_receipt(self):
        review = Mock()
        review.cleanup.side_effect = RuntimeError("owned cleanup failed")
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(native, "ROOT", Path(tmp)),
                patch.object(native, "verify_proposal"),
                patch.object(prepare_review, "Preparation", return_value=review),
                patch.object(prepare_review.signal, "signal"),
                self.assertRaises(RuntimeError),
            ):
                prepare_review.main(["--execute"])
            self.assertFalse((Path(tmp) / "preparation-receipt.json").exists())
        review.cleanup.assert_called_once()

    def test_stale_or_failed_preparation_receipt_refuses_docker_before_runtime(self):
        for fault in ["missing", "failed-cleanup", "stale-source", "stale-artifact"]:
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                sealed = root / "sealed"
                sealed.mkdir()
                (sealed / "manifest.json").write_text("{}")
                (root / "SHA256SUMS").write_text("reviewed proposal")
                receipt = {
                    "schema": "modo-native-preparation-v1",
                    "image": native.IMAGE_ID,
                    "proposal_sha256": hashlib.sha256(b"reviewed proposal").hexdigest(),
                    "manifest_sha256": hashlib.sha256(b"{}").hexdigest(),
                    "cleanup_passed": True,
                }
                if fault == "failed-cleanup":
                    receipt["cleanup_passed"] = False
                elif fault == "stale-source":
                    receipt["proposal_sha256"] = "0" * 64
                elif fault == "stale-artifact":
                    receipt["manifest_sha256"] = "0" * 64
                if fault != "missing":
                    (root / "preparation-receipt.json").write_text(json.dumps(receipt))
                runner = Mock()
                with (
                    patch.object(native, "ROOT", root),
                    patch.object(native, "fixture_manifest"),
                    self.assertRaises((FileNotFoundError, ValueError)),
                ):
                    native.NativeReview(sealed, runner).preflight()
                runner.assert_not_called()

    def test_preparation_default_has_zero_docker_calls_and_zero_input_writes(self):
        with (
            patch.object(native.subprocess, "run") as docker,
            patch.object(prepare_review.fixture, "prepare") as write,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            prepare_review.main([])
        docker.assert_not_called()
        write.assert_not_called()

    def test_archive_copy_refuses_traversal_and_links(self):
        for name, kind in [
            ("tiles/../../escape", tarfile.REGTYPE),
            ("tiles/2/000/001/002.gph", tarfile.SYMTYPE),
        ]:
            with (
                self.subTest(name=name, kind=kind),
                tempfile.TemporaryDirectory() as tmp,
            ):
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode="w") as archive:
                    entry = tarfile.TarInfo(name)
                    entry.type = kind
                    if kind == tarfile.SYMTYPE:
                        entry.linkname = "/outside"
                    else:
                        entry.size = 1
                    archive.addfile(entry, io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    prepare_review.unpack_tiles(raw.getvalue(), Path(tmp) / "copy")
                self.assertFalse((Path(tmp) / "escape").exists())

    def test_default_mode_cannot_call_docker_or_http(self):
        with (
            patch.object(native.subprocess, "run") as docker,
            patch.object(namespace.http.client, "HTTPConnection") as http,
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            native.main([])
        docker.assert_not_called()
        http.assert_not_called()
        self.assertEqual(json.loads(output.getvalue())["docker_calls"], 0)

    def test_missing_fixture_fails_before_any_docker_call(self):
        runner = Mock()
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(FileNotFoundError):
            native.NativeReview(Path(tmp) / "missing", runner).preflight()
        runner.assert_not_called()

    def test_budget_counts_failures_and_refuses_more_than_two_clients(self):
        budget = native.Budget()
        budget.begin(4)
        budget.begin(4)
        with self.assertRaises(RuntimeError):
            budget.begin(4)
        budget.end()
        budget.end()
        for _ in range(10):
            budget.begin(4)
            budget.end()
        self.assertEqual((budget.requests, budget.cells), (12, 48))
        with self.assertRaises(RuntimeError):
            budget.begin(0)

    def test_request_ids_are_exact_and_warning_is_not_success_or_other_request(self):
        log = "Got Thor Request 10\nGot Loki Request 11\nRequest 10 was interrupted\n"
        self.assertEqual(native.thor_ids(log), {10})
        self.assertEqual(native.loki_ids(log), {11})
        self.assertTrue(native.interrupted(log, 10))
        self.assertFalse(native.interrupted(log, 1))
        self.assertFalse(native.interrupted(log, 11))

    def memory_http_client(self, wire, send=True):
        # Real HTTPConnection/HTTPResponse parser and request state; only transport
        # methods are in-memory. Model socket makefile keeping transport alive
        # across connection.close(), then closing it when the response releases it.
        class MemorySocket:
            def __init__(self):
                self.buffer = io.BytesIO(wire)
                self.requests = bytearray()
                self.timeouts = []
                self.detached = False

            def makefile(self, mode):
                self.assert_mode = mode
                return self.buffer

            def sendall(self, raw):
                self.requests.extend(raw)

            def close(self):
                self.detached = True

            def settimeout(self, timeout):
                if self.buffer.closed:
                    raise OSError(
                        "transport closed after HTTPResponse released makefile"
                    )
                self.timeouts.append(timeout)

        transport = MemorySocket()
        client = namespace.http.client.HTTPConnection("in-memory.invalid")
        client.auto_open = 0
        client.sock = transport
        if send:
            client.request("GET", "/status", headers={"Connection": "close"})
        return client, transport

    def test_real_http_parser_reads_detached_close_and_http10_responses(self):
        body = b'{"version":"3.9.1"}'
        wires = [
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
            + body,
            b"HTTP/1.0 200 OK\r\n\r\n" + body,
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n"
            + f"{len(body):x}".encode()
            + b"\r\n"
            + body
            + b"\r\n0\r\n\r\n",
        ]
        for wire in wires:
            with self.subTest(headers=wire.split(b"\r\n\r\n")[0]):
                client, transport = self.memory_http_client(wire)
                self.assertEqual(
                    namespace.HTTPClient().response(client),
                    (200, {"version": "3.9.1"}),
                )
                self.assertIsNone(client.sock)
                self.assertTrue(transport.detached)
                self.assertTrue(transport.buffer.closed)
                self.assertIn(b"Connection: close", transport.requests)
                self.assertGreaterEqual(len(transport.timeouts), 2)

    def test_real_http_parser_rejects_truncated_length_even_with_valid_json_prefix(
        self,
    ):
        for status, body in [(b"200 OK", b"{}"), (b"504 Gateway Timeout", b"text")]:
            with self.subTest(status=status):
                client, transport = self.memory_http_client(
                    b"HTTP/1.1 "
                    + status
                    + b"\r\nConnection: close\r\nContent-Length: 20\r\n\r\n"
                    + body
                )
                with self.assertRaisesRegex(RuntimeError, "incomplete Content-Length"):
                    namespace.HTTPClient().response(client)
                self.assertTrue(transport.buffer.closed)

    def test_real_http_parser_rejects_incomplete_or_invalid_chunk_framing(self):
        for body in [b"2\r\n{}\r\n", b"2\r\n{}\r\n0\r\n", b"2\r\n{}xx0\r\n\r\n"]:
            with self.subTest(body=body):
                client, transport = self.memory_http_client(
                    b"HTTP/1.1 200 OK\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n"
                    + body
                )
                with self.assertRaises(namespace.http.client.HTTPException):
                    namespace.HTTPClient().response(client)
                self.assertTrue(transport.buffer.closed)

    def test_real_http_parser_reads_detached_non_json_504(self):
        client, transport = self.memory_http_client(
            b"HTTP/1.0 504 Gateway Timeout\r\n\r\nThe server did not respond"
        )
        self.assertEqual(
            namespace.HTTPClient().response(client),
            (504, {"non_json_error": True}),
        )
        self.assertIsNone(client.sock)
        self.assertTrue(transport.buffer.closed)

    def test_real_http_parser_closes_detached_response_after_byte_cap(self):
        body = b"x" * 1048577
        client, transport = self.memory_http_client(
            b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 1048577\r\n\r\n"
            + body
        )
        with self.assertRaisesRegex(RuntimeError, "exceeds 1 MiB"):
            namespace.HTTPClient().response(client)
        self.assertTrue(transport.buffer.closed)

    def test_real_http_parser_closes_detached_response_after_expired_body_deadline(
        self,
    ):
        client, transport = self.memory_http_client(b"HTTP/1.0 200 OK\r\n\r\n{}")
        review = namespace.HTTPClient()
        with (
            patch.object(review, "time_left", side_effect=[5, TimeoutError("expired")]),
            self.assertRaises(TimeoutError),
        ):
            review.response(client)
        self.assertIsNone(client.sock)
        self.assertTrue(transport.buffer.closed)

    def test_real_http_parser_cannot_accept_timer_shutdown_eof_as_success(self):
        for cause in ["timer", "clock", "early-timer", "none"]:
            with self.subTest(cause=cause):
                clock, callbacks = [0.0], []
                timer = Mock()
                wire = b"HTTP/1.0 200 OK\r\n\r\n{}"
                client, transport = self.memory_http_client(wire, send=False)
                transport.shutdown_happened = False
                transport.shutdown = Mock(
                    side_effect=lambda how, transport=transport: setattr(
                        transport, "shutdown_happened", True
                    )
                )

                class WaitingBody(io.BytesIO):
                    def read1(
                        self, size, clock=clock, cause=cause, callbacks=callbacks
                    ):
                        chunk = super().read1(size)
                        if not chunk:
                            clock[0] = (
                                1.0
                                if cause == "none"
                                else (4.999 if cause == "early-timer" else 5.001)
                            )
                            if cause in {"timer", "early-timer"}:
                                callbacks[0]()  # Absolute timer manufactures EOF.
                        return chunk

                transport.buffer = WaitingBody(wire)
                review = namespace.HTTPClient()
                review.deadline = 90

                def capture_timer(seconds, callback, callbacks=callbacks, timer=timer):
                    self.assertEqual(seconds, 5)
                    callbacks.append(callback)
                    return timer

                with (
                    patch.object(
                        native.time,
                        "monotonic",
                        side_effect=lambda clock=clock: clock[0],
                    ),
                    patch.object(namespace, "Timer", side_effect=capture_timer),
                    patch.object(
                        namespace.http.client, "HTTPConnection", return_value=client
                    ),
                    patch.object(client, "connect"),
                    patch.object(
                        namespace.socket,
                        "create_connection",
                        side_effect=AssertionError("network forbidden"),
                    ),
                    review.connection() as active,
                ):
                    if cause == "none":
                        self.assertEqual(review.response(active), (200, {}))
                    else:
                        with self.assertRaises(TimeoutError):
                            review.response(active)
                self.assertEqual(
                    transport.shutdown_happened, cause in {"timer", "early-timer"}
                )
                self.assertEqual(
                    client._native_http_expired.is_set(), transport.shutdown_happened
                )
                self.assertTrue(transport.buffer.closed)
                timer.cancel.assert_called_once()
                self.assertEqual(review.budget.active, 0)

    def test_real_http_parser_rechecks_absolute_deadline_after_json_parse(self):
        clock = [0.0]
        timer = Mock()
        client, transport = self.memory_http_client(
            b"HTTP/1.0 200 OK\r\n\r\n{}", send=False
        )
        review = namespace.HTTPClient()
        review.deadline = 90
        real_loads = native.json.loads

        def late_parse(raw):
            body = real_loads(raw)
            clock[0] = 5.001
            return body

        with (
            patch.object(
                native.time, "monotonic", side_effect=lambda clock=clock: clock[0]
            ),
            patch.object(namespace, "Timer", return_value=timer),
            patch.object(namespace.http.client, "HTTPConnection", return_value=client),
            patch.object(client, "connect"),
            patch.object(native.json, "loads", side_effect=late_parse),
            review.connection() as active,
            self.assertRaises(TimeoutError),
        ):
            review.response(active)
        self.assertTrue(transport.buffer.closed)
        timer.cancel.assert_called_once()

    def test_native_504_text_body_is_classified_without_json_failure(self):
        review = namespace.HTTPClient()
        response = Mock(status=504)
        response.isclosed.return_value = False
        response.length = None
        response.chunked = False
        response.read1.side_effect = [b"The server didn't respond in time", b""]
        client = Mock()
        client._native_http_deadline = None
        client._native_http_expired = None
        client.getresponse.return_value = response
        self.assertEqual(review.response(client), (504, {"non_json_error": True}))

    def row(self, review):
        review.network_id = "b" * 64
        return {
            "Id": "a" * 64,
            "Image": native.IMAGE_ID,
            "Config": {
                "User": "65532:65532",
                "Labels": {native.LABEL_KEY: review.token},
            },
            "Mounts": [
                {
                    "Type": "bind",
                    "Source": "/mock",
                    "Destination": "/fixture",
                    "RW": False,
                }
            ],
            "NetworkSettings": {
                "Ports": {},
                "Networks": {native.NETWORK: {"NetworkID": review.network_id}},
            },
            "HostConfig": {
                "NetworkMode": review.network_id,
                "ReadonlyRootfs": True,
                "Privileged": False,
                "CapDrop": ["ALL"],
                "CapAdd": None,
                "SecurityOpt": ["no-new-privileges:true"],
                "RestartPolicy": {"Name": "no"},
                "PidsLimit": 64,
                "Memory": 512 * 1024**2,
                "MemorySwap": 512 * 1024**2,
                "NanoCpus": 10**9,
                "Tmpfs": {"/tmp": native.TMPFS.split(":", 1)[1]},
                "LogConfig": {
                    "Type": "local",
                    "Config": {"max-size": "1m", "max-file": "1", "compress": "false"},
                },
                "PortBindings": {},
            },
        }

    def test_create_uses_existing_image_direct_native_entrypoint_and_exact_isolation(
        self,
    ):
        calls = []
        review = native.NativeReview(Path("/mock"))
        row = self.row(review)

        def runner(command, **kwargs):
            args = command[3:]
            calls.append(args)
            self.assertFalse(any(k.startswith("DOCKER_") for k in kwargs["env"]))
            if args[:2] == ["container", "ls"]:
                return subprocess.CompletedProcess(command, 0, "", "")
            if args[0] == "create" or args[:2] == ["network", "create"]:
                return subprocess.CompletedProcess(
                    command, 0, ("a" if args[0] == "create" else "b") * 64, ""
                )
            if args[:2] == ["network", "inspect"]:
                data = [
                    {
                        "Id": "b" * 64,
                        "Name": native.NETWORK,
                        "Labels": {native.LABEL_KEY: review.token},
                        "Internal": True,
                        "Containers": {"a" * 64: {"Name": native.ROUTER}}
                        if any(call[0] == "start" for call in calls)
                        else {},
                    }
                ]
            elif args[:2] == ["container", "inspect"]:
                data = [row]
            elif args[0] == "exec":
                data = {
                    "memory.max": str(512 * 1024**2),
                    "cpu.max": "100000 100000",
                    "pids.max": "64",
                }
            else:
                data = "a" * 64
            return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

        review.runner = runner
        review.create()
        create = next(c for c in calls if c[0] == "create")
        self.assertIn(native.IMAGE, create)
        self.assertIn("--pull=never", create)
        self.assertIn("compress=false", create)
        self.assertEqual(
            create[-4:],
            ["90s", "/usr/local/bin/valhalla_service", "/fixture/config.json", "1"],
        )
        self.assertNotIn("build_tiles", create)
        self.assertNotIn("pull", [c[0] for c in calls])
        self.assertEqual(create[create.index("--network") + 1], "b" * 64)
        self.assertNotIn("--publish", create)

    def test_preparation_docker_scope_is_networkless_and_bounded_with_safe_archive_copy(
        self,
    ):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, artifact = root / "prep-input", root / "sealed-fixture"
            for name in native.SOURCE_FILES | {"SHA256SUMS"}:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(native.ROOT / name, target)
            calls = []
            with (
                patch.object(native, "ROOT", root),
                patch.object(prepare_review, "INPUT", source),
                patch.object(prepare_review, "ARTIFACT", artifact),
            ):
                review = prepare_review.Preparation(artifact)
                row = self.row(review)
                row["HostConfig"].update(
                    {
                        "NetworkMode": "none",
                        "PortBindings": {},
                        "Tmpfs": {"/tmp": prepare_review.TMPFS.split(":", 1)[1]},
                    }
                )
                row["Mounts"] = [
                    {
                        "Type": "bind",
                        "Source": str(root),
                        "Destination": "/proposal",
                        "RW": False,
                    },
                    {
                        "Type": "bind",
                        "Source": str(source),
                        "Destination": "/input",
                        "RW": False,
                    },
                ]
                row["State"] = {"Running": True, "OOMKilled": False, "ExitCode": 0}
                archive_bytes = io.BytesIO()
                with tarfile.open(fileobj=archive_bytes, mode="w") as archive:
                    entry = tarfile.TarInfo("tiles/2/000/001/002.gph")
                    entry.size = 4
                    archive.addfile(entry, io.BytesIO(b"mock"))

                def runner(command, **kwargs):
                    args = command[3:]
                    calls.append(args)
                    if args[:2] == ["container", "ls"]:
                        return subprocess.CompletedProcess(command, 0, "", "")
                    if args[0] == "create":
                        return subprocess.CompletedProcess(command, 0, "a" * 64, "")
                    if args[0] == "cp":
                        return subprocess.CompletedProcess(
                            command, 0, archive_bytes.getvalue(), b""
                        )
                    if args[:2] == ["container", "inspect"]:
                        data = [row]
                    elif args[0] == "exec":
                        data = {
                            "memory.max": str(512 * 1024**2),
                            "cpu.max": "100000 100000",
                            "pids.max": "64",
                        }
                    elif args[0] == "logs":
                        data = self.ready_record()
                    else:
                        data = "a" * 64
                    return subprocess.CompletedProcess(command, 0, json.dumps(data), "")

                review.runner = runner
                review.initial_space = (
                    0  # Accounting is outside this mocked creation case.
                )
                try:
                    with (
                        patch.object(review, "host_preflight"),
                        patch.object(
                            review,
                            "stream_archive",
                            return_value=archive_bytes.getvalue(),
                        ) as export,
                        contextlib.redirect_stdout(io.StringIO()),
                    ):
                        review.prepare()
                    self.assertEqual(native.fixture_manifest(artifact)["grid_side"], 64)
                    export.assert_called_once_with(
                        [
                            "exec",
                            "a" * 64,
                            "python3",
                            "-I",
                            "/proposal/build_fixture.py",
                            "--export",
                        ]
                    )
                    self.assertFalse(any(args[0] == "cp" for args in calls))
                    create = next(args for args in calls if args[0] == "create")
                    self.assertEqual(create[create.index("--network") + 1], "none")
                    self.assertIn("--pull=never", create)
                    self.assertIn("compress=false", create)
                    self.assertNotIn("--publish", create)
                    self.assertEqual(create.count("--mount"), 2)
                    self.assertFalse(
                        any(args[0] in {"pull", "build", "network"} for args in calls)
                    )
                finally:
                    if artifact.exists():
                        unseal(artifact)

    def test_active_synthetic_stage_is_refused_by_label_or_fixed_name_before_mutations(
        self,
    ):
        for active in ["none", "label", "name"]:
            with self.subTest(active=active):
                calls = []

                def runner(command, active=active, calls=calls, **kwargs):
                    args = command[3:]
                    calls.append(args)
                    labelled = "label=fairway.safety.run=review" in args
                    output = (
                        "other-owned-id"
                        if active == "label" and labelled
                        else "fairway-safety-prepare-review"
                        if active == "name" and not labelled
                        else ""
                    )
                    return subprocess.CompletedProcess(command, 0, output, "")

                review = native.NativeReview(Path("/mock"), runner)
                if active == "none":
                    review.ensure_other_stage_idle()
                else:
                    with self.assertRaises(RuntimeError):
                        review.create()
                    self.assertFalse(review.created)
                self.assertTrue(all(args[:2] == ["container", "ls"] for args in calls))

    def test_wrong_mount_public_port_privileges_or_limits_fail_validation(self):
        review = native.NativeReview(Path("/mock"))
        good = self.row(review)
        for change in [
            "writable",
            "extra-mount",
            "public-port",
            "privileged",
            "unbounded-tmpfs",
            "wrong-memory",
        ]:
            with self.subTest(change=change):
                row = copy.deepcopy(good)
                if change == "writable":
                    row["Mounts"][0]["RW"] = True
                elif change == "extra-mount":
                    row["Mounts"].append({"Type": "volume"})
                elif change == "public-port":
                    row["HostConfig"]["PortBindings"] = {
                        "8002/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18082"}]
                    }
                elif change == "privileged":
                    row["HostConfig"]["Privileged"] = True
                elif change == "unbounded-tmpfs":
                    row["HostConfig"]["Tmpfs"]["/tmp"] = "rw"
                else:
                    row["HostConfig"]["Memory"] = 0
                with self.assertRaises(RuntimeError):
                    review.validate_container(row)

    def test_cleanup_preserves_a_preexisting_name_and_only_removes_owned_ids(self):
        for other_owner in [False, True]:
            with self.subTest(other_owner=other_owner):
                calls = []
                review = native.NativeReview(Path("/mock"))
                review.created = [
                    ("network", native.NETWORK),
                    ("container", native.ROUTER),
                ]

                def runner(
                    command,
                    calls=calls,
                    review=review,
                    other_owner=other_owner,
                    **kwargs,
                ):
                    args = command[3:]
                    calls.append(args)
                    if args[1] == "inspect":
                        token = (
                            "preexisting"
                            if other_owner and args[0] == "container"
                            else review.token
                        )
                        labels = {native.LABEL_KEY: token}
                        row = {
                            "Id": ("a" if args[0] == "container" else "b") * 64,
                            "Config": {"Labels": labels},
                            "Labels": labels,
                        }
                        return subprocess.CompletedProcess(
                            command, 0, json.dumps([row]), ""
                        )
                    return subprocess.CompletedProcess(command, 0, "", "")

                review.runner = runner
                with contextlib.redirect_stdout(io.StringIO()):
                    if other_owner:
                        with self.assertRaises(RuntimeError):
                            review.cleanup()
                    else:
                        review.cleanup()
                removed = [args for args in calls if "rm" in args]
                self.assertEqual(len(removed), 1 if other_owner else 2)
                self.assertTrue(all(len(args[-1]) == 64 for args in removed))
                self.assertFalse(
                    any(name in args for args in removed for name in native.LIVE)
                )


if __name__ == "__main__":
    unittest.main()
