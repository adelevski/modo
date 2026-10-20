"""Fixed exec argv and real local pipe framing; no process/container/network launch."""

import contextlib
import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_native_review as legacy

import namespace_client as client
import namespace_transport as transport
import native_review as native

ISOLATION = {
    "uid": 65532,
    "gid": 65532,
    "cap_eff": "0000000000000000",
    "no_new_privs": "1",
    "seccomp": "2",
    "memory.max": str(512 * 1024**2),
    "cpu.max": "100000 100000",
    "pids.max": "64",
}
OBS = {
    "poll": 1,
    "tcp4": True,
    "tcp6": False,
    "wildcard4": True,
    "wildcard6": False,
    "status_outcome": "http-response",
}


def reply(seq, op, result, requests=0, cells=0, active=0):
    return {
        "seq": seq,
        "op": op,
        "result": result,
        "budget": {"requests": requests, "cells": cells, "active": active},
    }


def handshake():
    return reply(
        0,
        "handshake",
        {"schema": "modo-native-client-v1", "isolation": ISOLATION.copy()},
    )


def startup():
    return reply(
        1,
        "startup",
        {"status_attempts": 1, "ipv4_http_ready": True, "observations": [OBS.copy()]},
        1,
    )


class PipeProcess:
    def __init__(self, rows=(), raw=None, stderr=b"", code=0, hold=False):
        input_read, input_write = os.pipe()
        output_read, output_write = os.pipe()
        error_read, error_write = os.pipe()
        self.input_read = input_read
        self.stdin = os.fdopen(input_write, "wb", buffering=0)
        self.stdout = os.fdopen(output_read, "rb", buffering=0)
        self.stderr = os.fdopen(error_read, "rb", buffering=0)
        self.writers = [output_write, error_write] if hold else []
        wire = (
            raw
            if raw is not None
            else b"".join((json.dumps(row) + "\n").encode() for row in rows)
        )
        self.wire = wire
        self.code = code
        self.killed = False
        if wire:
            os.write(output_write, wire)
        if stderr:
            os.write(error_write, stderr)
        if not hold:
            os.close(output_write)
            os.close(error_write)
        self.wait = Mock(side_effect=lambda **kwargs: self.code)

    def poll(self):
        return None if self.writers and not self.killed else self.code

    def kill(self):
        self.killed = True

    def close(self):
        for stream in [self.stdin, self.stdout, self.stderr]:
            if not stream.closed:
                stream.close()
        os.close(self.input_read)
        for fd in self.writers:
            os.close(fd)
        self.writers = []


class TransportTests(unittest.TestCase):
    def owner(self):
        owner = native.NativeReview(Path("/mock"))
        owner.container_id = "a" * 64
        owner.network_id = "b" * 64
        row = legacy.SafetyTests().row(owner)
        network = {
            "Id": owner.network_id,
            "Name": native.NETWORK,
            "Internal": True,
            "Labels": {native.LABEL_KEY: owner.token},
            "Containers": {owner.container_id: {}},
        }
        owner.runtime_state = Mock(
            return_value={"running": True, "oom_killed": False, "exit_code": 0}
        )
        owner.ensure_other_stage_idle = Mock()
        owner.document = Mock(side_effect=[[row], [network]])
        return owner

    @contextlib.contextmanager
    def session(self, rows=(), **options):
        owner = self.owner()
        process = PipeProcess(rows, **options)
        factory = Mock(return_value=process)
        instance = transport.NamespaceSession(owner, factory)
        try:
            yield owner, process, factory, instance
        finally:
            process.close()

    def test_exec_argv_is_fixed_nonroot_nondetached_and_shares_existing_container(self):
        with self.session([handshake()]) as (owner, _process, factory, session):
            with patch.object(
                client.http.client,
                "HTTPConnection",
                side_effect=AssertionError("host HTTP forbidden"),
            ):
                session.start()
            args = factory.call_args.args[0]
            self.assertEqual(args[:3], owner.command)
            self.assertEqual(
                args[3:8],
                ["exec", "--interactive", "--user", "65532:65532", owner.container_id],
            )
            self.assertEqual(
                args[8:11], ["/usr/bin/timeout", "--signal=TERM", "--kill-after=2s"]
            )
            self.assertLessEqual(float(args[11][:-1]), 60)
            self.assertEqual(args[12:16], ["python3", "-I", "-B", "-c"])
            self.assertEqual(args[16], Path(client.__file__).read_text())
            self.assertNotIn("--privileged", args)
            self.assertNotIn("--detach", args)
            self.assertNotIn("--tty", args)
            self.assertNotIn("--publish", args)
            self.assertFalse(
                any(k.startswith("DOCKER_") for k in factory.call_args.kwargs["env"])
            )
            self.assertEqual(owner.docker_calls, 1)

    def test_full_pipe_recipe_exact_slot_close_and_counter_mirroring(self):
        rows = [
            handshake(),
            startup(),
            reply(2, "open", {"slot": 2}, 2, 4, 1),
            reply(3, "open", {"slot": 3}, 3, 8, 2),
            reply(4, "close", {"closed": 3}, 3, 8, 1),
            reply(
                5,
                "read",
                {
                    "status": 200,
                    "body": {"sources_to_targets": [{"time": n} for n in [1, 2, 3, 4]]},
                },
                3,
                8,
            ),
            reply(6, "finish", {"finished": True}, 3, 8),
        ]
        with self.session(rows) as (owner, process, _factory, session):
            session.start()
            self.assertTrue(session.startup()["ipv4_http_ready"])
            one = session.open("auto")
            two = session.open("pedestrian")
            session.close_slot(two)
            self.assertEqual(session.slots, {one})
            self.assertEqual(owner.budget.active, 1)
            self.assertEqual(session.read_slot(one)[0], 200)
            session.finish()
            self.assertTrue(session.finished)
            self.assertEqual(owner.budget.requests, 3)
            self.assertEqual(owner.budget.cells, 8)
            self.assertEqual(owner.budget.active, 0)
            commands = os.read(process.input_read, 8192).splitlines()
            self.assertEqual(
                [json.loads(raw)["op"] for raw in commands],
                ["startup", "open", "open", "close", "read", "finish"],
            )
            self.assertEqual(json.loads(commands[3])["slot"], two)

    def test_handshake_cannot_accept_larger_cgroups_or_root_uid(self):
        for field, value in [
            ("uid", 0),
            ("memory.max", "max"),
            ("cpu.max", "max 100000"),
            ("pids.max", "max"),
        ]:
            row = handshake()
            row["result"]["isolation"][field] = value
            with (
                self.subTest(field=field),
                self.session([row]) as (_owner, _p, _f, session),
                self.assertRaises(ValueError),
            ):
                session.start()
            self.assertTrue(session.failed)

    def test_requested_or_realized_publication_is_refused_before_client_launch(self):
        for kind in ["requested", "realized", "publish-all"]:
            owner = self.owner()
            row = legacy.SafetyTests().row(owner)
            if kind == "requested":
                row["HostConfig"]["PortBindings"] = {
                    "8002/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18082"}]
                }
            elif kind == "realized":
                row["NetworkSettings"]["Ports"] = {
                    "8002/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18082"}]
                }
            else:
                row["HostConfig"]["PublishAllPorts"] = True
            with self.subTest(kind=kind), self.assertRaises(RuntimeError):
                owner.validate_container(row)

    def test_ambiguous_exec_launch_can_never_be_replaced(self):
        owner = self.owner()
        factory = Mock(side_effect=subprocess.TimeoutExpired("exec", 1))
        session = transport.NamespaceSession(owner, factory)
        with (
            contextlib.redirect_stdout(io.StringIO()),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            session.start()
        with self.assertRaises(RuntimeError):
            session.start()
        self.assertTrue(session.attempted)
        self.assertTrue(session.failed)
        factory.assert_called_once()

    def test_lost_handshake_or_reply_marks_session_failed_without_retry(self):
        with self.session([], hold=True) as (_owner, _p, factory, session):
            with (
                patch.object(transport.select, "select", return_value=([], [], [])),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(TimeoutError),
            ):
                session.start()
            self.assertTrue(session.failed)
            with self.assertRaises(RuntimeError):
                session.start()
            factory.assert_called_once()
        with self.session([handshake()]) as (_owner, _p, _f, session):
            session.start()
            with self.assertRaises(EOFError):
                session.startup()
            self.assertTrue(session.failed)
            with self.assertRaises(RuntimeError):
                session.open("auto")

    def test_partial_record_eof_and_oversized_records_are_failures(self):
        for raw in [b"{", b"x" * 4097 + b"\n"]:
            with (
                self.subTest(raw=raw[:10]),
                self.session(raw=raw) as (_owner, _p, _f, session),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises((EOFError, ValueError)),
            ):
                session.start()

    def test_output_and_stderr_caps_refuse_success_instead_of_silent_truncation(self):
        for name in ["stdout", "stderr", "events"]:
            with (
                self.subTest(name=name),
                self.session(
                    [handshake()], stderr=b"x" if name == "stderr" else b""
                ) as (_owner, _p, _f, session),
            ):
                if name == "stdout":
                    session.stdout_bytes = 65536
                elif name == "stderr":
                    session.stderr.extend(b"x" * 8192)
                else:
                    session.events = 64
                with (
                    contextlib.redirect_stdout(io.StringIO()),
                    self.assertRaises(ValueError),
                ):
                    session.start()
                self.assertTrue(session.failed)

    def test_wrong_sequence_boolean_counter_and_budget_divergence_are_refused(self):
        for fault in ["sequence", "counter", "budget"]:
            row = startup()
            if fault == "sequence":
                row["seq"] = 7
            elif fault == "counter":
                row["budget"]["requests"] = True
            else:
                row["budget"]["requests"] = 2
            with (
                self.subTest(fault=fault),
                self.session([handshake(), row]) as (_owner, _p, _f, session),
            ):
                session.start()
                with self.assertRaises(ValueError):
                    session.startup()

    def test_error_preserves_counted_attempts_and_bounded_family_observations(self):
        row = {
            "seq": 1,
            "op": "startup",
            "error_type": "RuntimeError",
            "observations": [OBS | {"status_outcome": "connection-refused"}],
            "budget": {"requests": 1, "cells": 0, "active": 0},
        }
        with (
            self.session([handshake(), row]) as (owner, _p, _f, session),
            patch.object(owner, "client_event") as emitted,
        ):
            session.start()
            with self.assertRaises(RuntimeError):
                session.startup()
            self.assertEqual(owner.budget.requests, 1)
            emitted.assert_called_once()
            self.assertTrue(session.failed)

    def test_finish_requires_clean_eof_and_successful_exec_exit(self):
        for fault in ["extra", "exit"]:
            rows = [handshake(), startup(), reply(2, "finish", {"finished": True}, 1)]
            raw = b"".join((json.dumps(row) + "\n").encode() for row in rows) + (
                b"extra" if fault == "extra" else b""
            )
            with (
                self.subTest(fault=fault),
                self.session(raw=raw, code=1 if fault == "exit" else 0) as (
                    _owner,
                    _p,
                    _f,
                    session,
                ),
            ):
                session.start()
                session.startup()
                with self.assertRaises(RuntimeError):
                    session.finish()
                self.assertFalse(session.finished)

    def test_deadline_before_exec_or_control_prevents_launch_and_send(self):
        owner = self.owner()
        owner.deadline = 0
        factory = Mock()
        session = transport.NamespaceSession(owner, factory)
        with self.assertRaises(TimeoutError):
            session.start()
        factory.assert_not_called()
        with self.session([handshake()]) as (owner, _p, _f, session):
            session.start()
            owner.deadline = 0
            with self.assertRaises(TimeoutError):
                session.startup()
            self.assertEqual(session.seq, 0)

    def test_cli_kill_is_not_used_as_remote_termination_proof(self):
        with self.session([], hold=True) as (owner, process, _factory, session):
            session.attempted = True
            session.process = process
            owner.session = session
            session.abort_cli()
            self.assertTrue(process.killed)
            self.assertFalse(session.finished)
            self.assertTrue(session.attempted)

    def test_cleanup_fails_if_owned_full_id_remains_after_removal(self):
        owner = native.NativeReview(Path("/mock"))
        owner.created = [("container", native.ROUTER)]
        owner.owned_ids[("container", native.ROUTER)] = "a" * 64

        def runner(args, **kwargs):
            command = args[3:]
            if command[:2] == ["container", "inspect"]:
                out = json.dumps(
                    [
                        {
                            "Id": "a" * 64,
                            "Config": {"Labels": {native.LABEL_KEY: owner.token}},
                        }
                    ]
                )
            elif command[:2] == ["container", "ls"]:
                out = "a" * 64
            else:
                out = ""
            return subprocess.CompletedProcess(args, 0, out, "")

        owner.runner = runner
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            owner.cleanup()


class JointPipeTests(unittest.TestCase):
    def test_actual_client_and_host_protocol_over_local_pipes_closes_exact_socket(self):
        import threading

        import test_namespace_client as fixtures

        owner = TransportTests().owner()
        instance = client.Client()
        instance.deadline = client.time.monotonic() + 3
        process = PipeProcess(hold=True)
        error = []
        control = client.ControlIO(instance, process.input_read, process.writers[0])
        status, _status_sock = fixtures.memory_http(b'{"version":"3.9.1"}')
        first, a = fixtures.memory_http(
            json.dumps(
                {"sources_to_targets": [{"time": n} for n in [1, 2, 3, 4]]}
            ).encode()
        )
        second, b = fixtures.memory_http()

        def worker():
            try:
                client.run()
                process.code = 0
            except Exception as problem:  # noqa: BLE001 - thread outcome asserted below
                error.append(problem)
                process.code = 1
            finally:
                for fd in process.writers:
                    os.close(fd)
                process.writers = []

        original_poll = process.poll
        thread = threading.Thread(target=worker, daemon=True)
        process.poll = lambda: None if thread.is_alive() else process.code

        def wait(timeout):
            thread.join(timeout)
            if thread.is_alive():
                raise subprocess.TimeoutExpired("local pipe fixture", timeout)
            return process.code

        process.wait = Mock(side_effect=wait)
        session = transport.NamespaceSession(owner, Mock(return_value=process))
        try:
            with (
                patch.object(client, "Client", return_value=instance),
                patch.object(client, "ControlIO", return_value=control),
                patch.object(client, "isolation", return_value=ISOLATION.copy()),
                patch.object(
                    client,
                    "listener_families",
                    return_value={
                        k: OBS[k] for k in ["tcp4", "tcp6", "wildcard4", "wildcard6"]
                    },
                ),
                patch.object(
                    client.http.client,
                    "HTTPConnection",
                    side_effect=[status, first, second],
                ),
                patch.object(client, "Timer", return_value=Mock()),
                patch.object(client.signal, "signal"),
                patch.object(client.signal, "setitimer"),
                patch.object(
                    client.socket,
                    "create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
            ):
                thread.start()
                session.start()
                session.startup()
                one = session.open("auto")
                two = session.open("pedestrian")
                session.close_slot(two)
                self.assertFalse(a.buffer.closed)
                self.assertTrue(b.buffer.closed)
                self.assertEqual(
                    instance.counters(), {"requests": 3, "cells": 8, "active": 1}
                )
                self.assertEqual(session.read_slot(one)[0], 200)
                self.assertTrue(a.buffer.closed)
                session.finish()
                self.assertTrue(session.finished)
                self.assertEqual(
                    instance.counters(), {"requests": 3, "cells": 8, "active": 0}
                )
                self.assertFalse(error)
        finally:
            if thread.is_alive():
                process.stdin.close()
                thread.join(4)
            process.poll = original_poll
            process.close()
        self.assertFalse(thread.is_alive())


class SourcePinTests(unittest.TestCase):
    def test_changed_manifest_or_client_source_refuses_exec_launch(self):
        for fault in ["manifest", "client"]:
            owner = TransportTests().owner()
            if fault == "manifest":
                owner.proposal_sha256 = "0" * 64
            else:
                original = Path.read_bytes

                def changed(path, original=original):
                    raw = original(path)
                    return raw + b"\n" if path.name == "namespace_client.py" else raw

            factory = Mock()
            session = transport.NamespaceSession(owner, factory)
            context = (
                patch.object(Path, "read_bytes", changed)
                if fault == "client"
                else contextlib.nullcontext()
            )
            with self.subTest(fault=fault), context, self.assertRaises(ValueError):
                session.start()
            factory.assert_not_called()


class CleanupAbsenceTests(unittest.TestCase):
    def cleanup_case(
        self, kind, *, inspect_fails=True, query_fails=False, listed="", tracked=True
    ):
        owner = native.NativeReview(Path("/mock"))
        name = native.ROUTER if kind == "container" else native.NETWORK
        identifier = "a" * 64
        owner.created = [(kind, name)] if tracked else []
        owner.owned_ids[(kind, name)] = identifier
        calls = []

        def runner(command, **kwargs):
            args = command[3:]
            calls.append(args)
            if args[1] == "inspect":
                if inspect_fails:
                    return subprocess.CompletedProcess(command, 1, "", "inspect failed")
                row = {
                    "Id": identifier,
                    "Config": {"Labels": {native.LABEL_KEY: owner.token}},
                    "Labels": {native.LABEL_KEY: owner.token},
                }
                return subprocess.CompletedProcess(command, 0, json.dumps([row]), "")
            if args[1] == "ls":
                # Its mutable name is different. A name-only absence check would
                # wrongly succeed even though the recorded full ID survives.
                if "{{.ID}}" not in args:
                    return subprocess.CompletedProcess(
                        command, 0, "renamed-owned-resource", ""
                    )
                return subprocess.CompletedProcess(
                    command,
                    1 if query_fails else 0,
                    listed,
                    "absence query failed" if query_fails else "",
                )
            return subprocess.CompletedProcess(command, 0, "", "")

        owner.runner = runner
        failure = None
        with contextlib.redirect_stdout(io.StringIO()) as printed:
            try:
                owner.cleanup()
            except RuntimeError as error:
                failure = error
        events = [json.loads(line) for line in printed.getvalue().splitlines()]
        return failure, next(e for e in events if e["event"] == "cleanup"), calls

    def test_failed_inspect_and_renamed_surviving_full_id_cannot_report_clean(self):
        for kind in ["container", "network"]:
            with self.subTest(kind=kind):
                failure, event, calls = self.cleanup_case(kind, listed="a" * 64)
                self.assertIsNotNone(failure)
                self.assertTrue(event["failures"])
                self.assertEqual(event["verified_absent_full_ids"], [])
                query = next(args for args in calls if args[1] == "ls")
                self.assertIn("--no-trunc", query)
                self.assertIn("{{.ID}}", query)
                self.assertFalse(
                    any(args[0] in {"stop", "rm"} or "rm" in args for args in calls)
                )

    def test_failed_absence_query_blocks_both_early_and_post_remove_success(self):
        for kind in ["container", "network"]:
            for inspect_fails in [True, False]:
                with self.subTest(kind=kind, inspect_fails=inspect_fails):
                    failure, event, _calls = self.cleanup_case(
                        kind, inspect_fails=inspect_fails, query_fails=True
                    )
                    self.assertIsNotNone(failure)
                    self.assertTrue(event["failures"])
                    self.assertFalse(event["verified_absent_full_ids"])

    def test_successful_known_id_absence_preserves_other_ids_without_name_lookup(self):
        for kind in ["container", "network"]:
            with self.subTest(kind=kind):
                failure, event, calls = self.cleanup_case(kind, listed="b" * 64)
                self.assertIsNone(failure)
                self.assertFalse(event["failures"])
                self.assertEqual(
                    event["verified_absent_full_ids"],
                    [{"kind": kind, "resource_id": "a" * 64}],
                )
                self.assertTrue(
                    all("{{.ID}}" in args for args in calls if args[1] == "ls")
                )
                self.assertFalse(
                    any("rm" in args or args[0] == "stop" for args in calls)
                )

    def test_malformed_or_truncated_id_listing_is_not_absence_proof(self):
        for listed in ["a" * 12, "not-json-or-an-id", "a" * 64 + "\nmalformed"]:
            with self.subTest(listed=listed):
                failure, event, _calls = self.cleanup_case("container", listed=listed)
                self.assertIsNotNone(failure)
                self.assertTrue(event["failures"])
                self.assertFalse(event["verified_absent_full_ids"])

    def test_known_id_omitted_from_created_loop_cannot_report_unverified_success(self):
        failure, event, calls = self.cleanup_case("container", tracked=False)
        self.assertIsNotNone(failure)
        self.assertTrue(event["failures"])
        self.assertFalse(event["verified_absent_full_ids"])
        self.assertFalse(calls)


class StrictReviewTests(unittest.TestCase):
    def test_float_uid_gid_cannot_satisfy_integer_handshake_contract(self):
        fixture = TransportTests()
        for field in ["uid", "gid"]:
            row = handshake()
            row["result"]["isolation"][field] = 65532.0
            with (
                self.subTest(field=field),
                fixture.session([row]) as (_owner, _p, _f, session),
                contextlib.redirect_stdout(io.StringIO()),
                self.assertRaises(ValueError),
            ):
                session.start()
            self.assertTrue(session.failed)

    def test_error_replies_cannot_corrupt_counter_evidence_with_impossible_transition(
        self,
    ):
        fixtures = TransportTests()
        cases = [
            ("startup", (0, 0, 0), (1, 4, 0)),
            ("startup", (0, 0, 0), (1, 0, 1)),
            ("open", (1, 0, 0), (2, 0, 0)),
            ("open", (1, 0, 0), (2, 4, 1)),
            ("pending", (3, 8, 2), (4, 12, 2)),
            ("pending", (3, 8, 2), (3, 8, 1)),
            ("read", (3, 8, 2), (4, 12, 1)),
            ("close", (3, 8, 2), (3, 8, 0)),
            ("finish", (3, 8, 0), (4, 8, 0)),
        ]
        for op, before, after in cases:
            row = {
                "seq": 1,
                "op": op,
                "error_type": "RuntimeError",
                "observations": [],
                "budget": dict(
                    zip(["requests", "cells", "active"], after, strict=True)
                ),
            }
            with (
                self.subTest(op=op, after=after),
                fixtures.session([handshake(), row]) as (owner, _p, _f, session),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                session.start()
                owner.budget.requests, owner.budget.cells, owner.budget.active = before
                with (
                    patch.object(owner, "client_event") as emitted,
                    self.assertRaises(ValueError),
                ):
                    session.call(op)
                self.assertEqual(
                    (owner.budget.requests, owner.budget.cells, owner.budget.active),
                    before,
                )
                emitted.assert_not_called()
                self.assertTrue(session.failed)

    def test_valid_terminal_error_transitions_remain_truthful_and_do_not_reset_budget(
        self,
    ):
        fixture = TransportTests()
        cases = [
            ("startup", (0, 0, 0), (2, 0, 0)),
            ("open", (1, 0, 0), (2, 4, 0)),
            ("pending", (3, 8, 2), (3, 8, 2)),
            ("read", (3, 8, 2), (3, 8, 1)),
            ("close", (3, 8, 2), (3, 8, 1)),
            ("finish", (3, 8, 0), (3, 8, 0)),
        ]
        for op, before, after in cases:
            row = {
                "seq": 1,
                "op": op,
                "error_type": "RuntimeError",
                "observations": [],
                "budget": dict(
                    zip(["requests", "cells", "active"], after, strict=True)
                ),
            }
            with (
                self.subTest(op=op),
                fixture.session([handshake(), row]) as (owner, _p, _f, session),
                contextlib.redirect_stdout(io.StringIO()),
            ):
                session.start()
                owner.budget.requests, owner.budget.cells, owner.budget.active = before
                with (
                    patch.object(owner, "client_event") as emitted,
                    self.assertRaises(RuntimeError),
                ):
                    session.call(op)
                self.assertEqual(
                    (owner.budget.requests, owner.budget.cells, owner.budget.active),
                    after,
                )
                emitted.assert_called_once()
                self.assertTrue(session.failed)
