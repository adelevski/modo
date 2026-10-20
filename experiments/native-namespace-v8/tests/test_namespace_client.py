"""Real HTTP parser/in-memory transports and local pipes only; no Docker/network."""

import io
import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import test_native_review as legacy

import namespace_client as client

REAL_HTTP = client.http.client.HTTPConnection


def memory_http(body=b"{}", status=200):
    wire = (
        b"HTTP/1.1 "
        + str(status).encode()
        + b" Result\r\nConnection: close\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\n\r\n"
        + body
    )
    with patch.object(client.http.client, "HTTPConnection", REAL_HTTP):
        active, transport = legacy.SafetyTests().memory_http_client(wire, send=False)
    made = [False]
    original_makefile, original_close = transport.makefile, transport.close

    def makefile(mode):
        made[0] = True
        return original_makefile(mode)

    def close():
        original_close()
        if not made[0]:
            transport.buffer.close()

    transport.makefile, transport.close = makefile, close
    active.connect = Mock(side_effect=lambda: setattr(active, "sock", transport))
    transport.shutdown = Mock()
    return active, transport


class ClientTests(unittest.TestCase):
    def startup(self, ready_at=0, refusal=False, failure=None, ipv6_only=False):
        clock = [0.0]
        times = []
        contexts = []

        def families():
            ready = clock[0] >= ready_at
            return {
                "tcp4": ready and not ipv6_only,
                "tcp6": ready and ipv6_only,
                "wildcard4": ready and not ipv6_only,
                "wildcard6": ready and ipv6_only,
            }

        def constructor(host, port, timeout):
            self.assertEqual((host, port), ("127.0.0.1", 8002))
            self.assertLessEqual(timeout, 5)
            number = len(times)
            times.append(clock[0])
            body = (
                b"{"
                if failure == "json"
                else json.dumps(
                    {"version": "wrong" if failure == "version" else "3.9.1"}
                ).encode()
            )
            response, transport = memory_http(body, 503 if failure == "status" else 200)

            def connect():
                if failure == "timeout":
                    raise TimeoutError("bounded")
                if failure == "reset":
                    raise ConnectionResetError("reset")
                if ipv6_only or (refusal and number == 0):
                    response.sock = None
                    raise ConnectionRefusedError("refused")
                response.sock = transport

            response.connect = Mock(side_effect=connect)
            contexts.append((response, transport))
            return response

        with (
            patch.object(client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(
                client.time,
                "sleep",
                side_effect=lambda n: clock.__setitem__(0, clock[0] + n),
            ),
            patch.object(client, "listener_families", side_effect=families),
            patch.object(client.http.client, "HTTPConnection", side_effect=constructor),
            patch.object(client, "Timer", return_value=Mock()),
            patch.object(
                client.socket,
                "create_connection",
                side_effect=AssertionError("network forbidden"),
            ),
        ):
            instance = client.Client()
            try:
                result = instance.dispatch({"seq": 1, "op": "startup"})
                error = None
            except Exception as problem:  # noqa: BLE001 - terminal outcomes are asserted
                result = None
                error = problem
        return instance, result, error, times, clock[0]

    def test_delayed_listener_startup_spends_window_without_early_requests(self):
        instance, result, error, times, elapsed = self.startup(ready_at=2)
        self.assertIsNone(error)
        self.assertTrue(result["ipv4_http_ready"])
        self.assertEqual(times, [2])
        self.assertEqual(elapsed, 2)
        self.assertEqual(instance.counters(), {"requests": 1, "cells": 0, "active": 0})

    def test_refusal_then_late_second_status_preserves_two_attempt_cap(self):
        instance, result, error, times, _elapsed = self.startup(refusal=True)
        self.assertIsNone(error)
        self.assertEqual(times, [0, 4])
        self.assertEqual(result["status_attempts"], 2)
        self.assertEqual(instance.budget.active, 0)

    def test_ipv6_only_listener_cannot_establish_ipv4_application_readiness(self):
        instance, result, error, times, _elapsed = self.startup(ipv6_only=True)
        self.assertIsNone(result)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(times, [0, 4])
        self.assertFalse(instance.ready)
        self.assertEqual(instance.budget.requests, 2)
        self.assertTrue(
            all(
                o["wildcard6"] and not o["wildcard4"]
                for o in instance.startup_observations
            )
        )

    def test_absent_listener_expires_without_any_request_or_expansion(self):
        instance, result, error, times, elapsed = self.startup(ready_at=100)
        self.assertIsNone(result)
        self.assertIsInstance(error, TimeoutError)
        self.assertEqual(times, [])
        self.assertLessEqual(elapsed, 5)
        self.assertLessEqual(len(instance.startup_observations), 6)
        self.assertEqual(instance.budget.requests, 0)

    def test_timeout_reset_and_protocol_failures_are_terminal_on_first_attempt(self):
        for failure in ["timeout", "reset", "status", "version", "json"]:
            with self.subTest(failure=failure):
                instance, _result, error, times, _elapsed = self.startup(
                    failure=failure
                )
                self.assertIsNotNone(error)
                self.assertEqual(times, [0])
                self.assertEqual(instance.budget.active, 0)
                self.assertFalse(instance.ready)

    def test_exact_slot_close_leaves_other_socket_and_budget_intact(self):
        instance = client.Client()
        instance.ready = True
        first, a = memory_http()
        second, b = memory_http()
        with (
            patch.object(
                client.http.client, "HTTPConnection", side_effect=[first, second]
            ),
            patch.object(client, "Timer", return_value=Mock()),
        ):
            one = instance.dispatch({"seq": 1, "op": "open", "costing": "auto"})["slot"]
            two = instance.dispatch({"seq": 2, "op": "open", "costing": "pedestrian"})[
                "slot"
            ]
            instance.dispatch({"seq": 3, "op": "close", "slot": two})
            self.assertFalse(a.buffer.closed)
            self.assertTrue(b.buffer.closed)
            self.assertEqual(
                instance.counters(), {"requests": 2, "cells": 8, "active": 1}
            )
            self.assertEqual(set(instance.slots), {one})
            instance.close_all()
            self.assertTrue(a.buffer.closed)
            self.assertEqual(instance.budget.active, 0)

    def test_read_uses_real_parser_and_returns_only_four_normalized_cells(self):
        instance = client.Client()
        instance.ready = True
        body = json.dumps(
            {
                "sources_to_targets": [
                    [{"time": 1, "unused": "x" * 10000}, {"time": 2}],
                    [{"time": 3}, {"time": 4}],
                ]
            }
        ).encode()
        active, transport = memory_http(body)
        with (
            patch.object(client.http.client, "HTTPConnection", return_value=active),
            patch.object(client, "Timer", return_value=Mock()),
        ):
            number = instance.dispatch({"seq": 1, "op": "open", "costing": "auto"})[
                "slot"
            ]
            result = instance.dispatch({"seq": 2, "op": "read", "slot": number})
        self.assertEqual(
            result["body"], {"sources_to_targets": [{"time": n} for n in [1, 2, 3, 4]]}
        )
        self.assertTrue(transport.buffer.closed)
        self.assertEqual(instance.budget.active, 0)

    def test_bad_matrix_or_expired_response_closes_owned_slot(self):
        for failure in ["shape", "deadline"]:
            with self.subTest(failure=failure):
                instance = client.Client()
                instance.ready = True
                active, transport = memory_http(b"{}")
                with (
                    patch.object(
                        client.http.client, "HTTPConnection", return_value=active
                    ),
                    patch.object(client, "Timer", return_value=Mock()),
                ):
                    number = instance.open_slot("auto")["slot"]
                    if failure == "deadline":
                        active._native_http_expired.set()
                    with self.assertRaises((ValueError, TimeoutError)):
                        instance.read_slot(number)
                    self.assertFalse(instance.slots)
                    self.assertEqual(instance.budget.active, 0)
                    self.assertTrue(transport.buffer.closed)

    def test_constructor_close_and_timer_cancel_errors_release_active_slot(self):
        instance = client.Client()
        instance.ready = True
        with (
            patch.object(
                client.http.client,
                "HTTPConnection",
                side_effect=OSError("construction"),
            ),
            self.assertRaises(OSError),
        ):
            instance.open_slot("auto")
        self.assertEqual(instance.budget.active, 0)
        for fault in ["close", "cancel"]:
            active, _transport = memory_http()
            timer = Mock()
            if fault == "close":
                active.close = Mock(side_effect=OSError("close"))
            else:
                timer.cancel.side_effect = OSError("cancel")
            with (
                patch.object(client.http.client, "HTTPConnection", return_value=active),
                patch.object(client, "Timer", return_value=timer),
            ):
                number = instance.open_slot("auto")["slot"]
                with self.assertRaises(OSError):
                    instance.close_slot(number)
            self.assertEqual(instance.budget.active, 0)
            self.assertFalse(instance.slots)

    def test_global_budget_cannot_reset_via_close_or_repeat_startup(self):
        instance = client.Client()
        instance.ready = True
        instance.startup_done = True
        with (
            patch.object(
                client.http.client,
                "HTTPConnection",
                side_effect=lambda *a, **k: memory_http()[0],
            ),
            patch.object(client, "Timer", return_value=Mock()),
        ):
            for _ in range(12):
                number = instance.open_slot("auto")["slot"]
                instance.close_slot(number)
            with self.assertRaises(RuntimeError):
                instance.open_slot("auto")
            with self.assertRaises(ValueError):
                instance.startup()
        self.assertEqual(
            instance.counters(), {"requests": 12, "cells": 48, "active": 0}
        )

    def test_bad_commands_and_finish_with_held_socket_are_terminal(self):
        for row in [
            {"seq": True, "op": "finish"},
            {"seq": 1, "op": "open", "costing": "truck"},
            {"seq": 1, "op": "open", "costing": "auto", "url": "http://outside/"},
            {"seq": 1, "op": "close", "slot": 9},
            {"seq": 2, "op": "finish"},
        ]:
            instance = client.Client()
            instance.ready = True
            with self.subTest(row=row), self.assertRaises((ValueError, TypeError)):
                instance.dispatch(row)
        instance = client.Client()
        instance.ready = True
        active, _transport = memory_http()
        with (
            patch.object(client.http.client, "HTTPConnection", return_value=active),
            patch.object(client, "Timer", return_value=Mock()),
        ):
            instance.open_slot("auto")
            with self.assertRaises(ValueError):
                instance.dispatch({"seq": 1, "op": "finish"})
            instance.close_all()

    def test_decode_rejects_duplicate_fields_partial_and_large_records(self):
        for raw in [b'{"seq":1,"seq":2}\n', b"{}", b"x" * 4097 + b"\n"]:
            with self.subTest(raw=raw[:20]), self.assertRaises(ValueError):
                client.decode_record(raw)

    def test_per_family_listener_reader_does_not_merge_ipv6_into_ipv4(self):
        files = {
            "/proc/net/tcp": "header\n",
            "/proc/net/tcp6": "header\n0: 00000000000000000000000000000000:1F42 ignored 0A rest\n",
        }
        with patch("builtins.open", side_effect=lambda path: io.StringIO(files[path])):
            self.assertEqual(
                client.listener_families(),
                {"tcp4": False, "tcp6": True, "wildcard4": False, "wildcard6": True},
            )

    def test_control_io_partial_line_eof_and_deadline_are_not_success(self):
        for fault in ["partial", "eof", "timeout"]:
            with self.subTest(fault=fault):
                reader, writer = os.pipe()
                output, output_writer = os.pipe()
                try:
                    instance = client.Client()
                    control = client.ControlIO(instance, reader, output_writer)
                    if fault == "partial":
                        os.write(writer, b"{")
                        os.close(writer)
                        writer = None
                    elif fault == "eof":
                        os.close(writer)
                        writer = None
                    if fault == "timeout":
                        with (
                            patch.object(
                                client.select, "select", return_value=([], [], [])
                            ),
                            self.assertRaises(TimeoutError),
                        ):
                            control.read()
                    else:
                        with self.assertRaises(EOFError):
                            control.read()
                finally:
                    for fd in [reader, writer, output, output_writer]:
                        if fd is not None:
                            os.close(fd)

    def test_control_output_size_total_and_event_caps(self):
        for fault in ["record", "total", "events"]:
            reader, writer = os.pipe()
            output, output_writer = os.pipe()
            try:
                control = client.ControlIO(client.Client(), reader, output_writer)
                if fault == "total":
                    control.output_bytes = 65536
                elif fault == "events":
                    control.output_records = 64
                with self.assertRaises(ValueError):
                    control.write({"x": "x" * 4096} if fault == "record" else {})
            finally:
                for fd in [reader, writer, output, output_writer]:
                    os.close(fd)

    def test_isolation_requires_combined_client_server_cgroup_and_nonroot(self):
        status = "CapEff:\t0000000000000000\nNoNewPrivs:\t1\nSeccomp:\t2\n"
        files = {
            "/proc/self/status": status,
            "/sys/fs/cgroup/memory.max": str(512 * 1024**2),
            "/sys/fs/cgroup/cpu.max": "100000 100000",
            "/sys/fs/cgroup/pids.max": "64",
        }

        def opened(path, *args, **kwargs):
            if path == "/native-review-write-denied":
                raise OSError(30, "read only")
            return io.StringIO(files[path])

        with (
            patch("builtins.open", side_effect=opened),
            patch.object(client.os, "getuid", return_value=65532),
            patch.object(client.os, "getgid", return_value=65532),
        ):
            self.assertEqual(client.isolation()["pids.max"], "64")
            files["/sys/fs/cgroup/memory.max"] = "max"
            with self.assertRaises(RuntimeError):
                client.isolation()

    def test_run_eof_interrupt_and_output_failure_close_both_socket_slots(self):
        for failure in [EOFError, InterruptedError, BrokenPipeError]:
            instance = client.Client()
            instance.ready = True
            one, a = memory_http()
            two, b = memory_http()
            with (
                patch.object(
                    client.http.client, "HTTPConnection", side_effect=[one, two]
                ),
                patch.object(client, "Timer", return_value=Mock()),
            ):
                instance.open_slot("auto")
                instance.open_slot("auto")
                control = Mock()
                control.read.side_effect = failure("failure")
                with (
                    patch.object(client, "Client", return_value=instance),
                    patch.object(client, "ControlIO", return_value=control),
                    patch.object(client, "isolation", return_value={}),
                    patch.object(client.signal, "signal"),
                    patch.object(client.signal, "setitimer"),
                    self.assertRaises(failure),
                ):
                    client.run()
            self.assertTrue(a.buffer.closed)
            self.assertTrue(b.buffer.closed)
            self.assertEqual(instance.budget.active, 0)


class DiagnosticTimingTests(unittest.TestCase):
    def test_no_native_log_ids_with_immediate_real_responses_finishes_all_finite_cases(
        self,
    ):
        import native_review as native

        clock = [0.0]
        events = []
        calls = []
        transports = []
        with (
            patch.object(client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(
                client.time,
                "sleep",
                side_effect=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            ),
        ):
            instance = client.Client()
            owner = native.NativeReview(Path("/mock"))
            owner.budget = instance.budget

            class LocalSession:
                def __init__(self):
                    self.slots = set()
                    self.ends = {}
                    self.seq = 0
                    self.failed = False
                    self.finished = False

                def call(self, op, **fields):
                    self.seq += 1
                    calls.append((clock[0], op, fields.copy()))
                    return instance.dispatch({"seq": self.seq, "op": op, **fields})

                def startup(self):
                    return self.call("startup")

                def open(self, costing):
                    before = clock[0]
                    number = self.call("open", costing=costing)["slot"]
                    self.slots.add(number)
                    self.ends[number] = before + 5
                    return number

                def slot_deadline(self, number):
                    return self.ends[number]

                def close_slot(self, number):
                    self.call("close", slot=number)
                    self.slots.remove(number)
                    self.ends.pop(number)

                def read_slot(self, number):
                    result = self.call("read", slot=number)
                    self.slots.remove(number)
                    self.ends.pop(number)
                    return result["status"], result["body"]

                def pending(self, number):
                    return self.call("pending", slot=number)["pending"]

                def finish(self):
                    self.call("finish")
                    self.finished = True

            session = LocalSession()
            owner.session = session

            def construct(*args, **kwargs):
                body = (
                    b'{"version":"3.9.1"}'
                    if instance.budget.cells == 0
                    else json.dumps(
                        {"sources_to_targets": [{"time": n} for n in [1, 2, 3, 4]]}
                    ).encode()
                )
                active, transport = memory_http(body)
                transports.append(transport)
                return active

            def begin():
                session.startup()

            def logs(**kwargs):
                return ""

            with (
                patch.object(owner, "await_startup", side_effect=begin),
                patch.object(owner, "logs", side_effect=logs),
                patch.object(
                    client,
                    "listener_families",
                    return_value={
                        "tcp4": True,
                        "tcp6": False,
                        "wildcard4": True,
                        "wildcard6": False,
                    },
                ),
                patch.object(
                    client.http.client, "HTTPConnection", side_effect=construct
                ),
                patch.object(client, "Timer", return_value=Mock()),
                patch.object(
                    client.socket,
                    "create_connection",
                    side_effect=AssertionError("network forbidden"),
                ),
                patch.object(
                    native,
                    "emit",
                    side_effect=lambda event, **fields: events.append(
                        {"event": event, **fields}
                    ),
                ),
            ):
                owner.scenarios()
        self.assertTrue(session.finished)
        self.assertEqual(
            instance.counters(), {"requests": 10, "cells": 36, "active": 0}
        )
        self.assertTrue(all(transport.buffer.closed for transport in transports))
        for name in ["disconnect", "queued-disconnect", "server-timeout"]:
            row = next(e for e in events if e["event"] == name)
            self.assertEqual(row["outcome"], "inconclusive")
            self.assertTrue(row["recovery_matrix_succeeded"])
        queued = next(e for e in events if e["event"] == "queued-disconnect")
        self.assertTrue(queued["shared_observation_window_exhausted"])
        self.assertFalse(queued["before_close_baseline_available"])
        self.assertFalse(queued["thor_queue_admission_proven"])
        opens = [(at, data) for at, op, data in calls if op == "open"]
        first_open = opens[4][0]
        first_read = next(
            at for at, op, data in calls if op == "read" and data["slot"] == 6
        )
        second_close = next(
            at for at, op, data in calls if op == "close" and data["slot"] == 7
        )
        self.assertLess(first_read, first_open + 5)
        self.assertLess(second_close, min(first_open + 5, opens[5][0] + 5))
        self.assertLessEqual(second_close - first_open, 3.01)
        self.assertLessEqual(clock[0], 9.01)
        self.assertEqual(events[-1]["event"], "native-smoke-finished")

    def test_log_timeout_is_clipped_to_shared_observation_deadline(self):
        import native_review as native

        owner = native.NativeReview(Path("/mock"))
        owner.container_id = "a" * 64
        clock = [10.0]
        with (
            patch.object(native.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(
                owner,
                "docker",
                return_value=subprocess.CompletedProcess([], 0, "", ""),
            ) as docker,
        ):
            owner.logs(deadline=10.25)
            self.assertEqual(docker.call_args.kwargs["timeout"], 0.25)
            clock[0] = 10.25
            self.assertIsNone(owner.diagnostic_logs(10.25))
            self.assertEqual(docker.call_count, 1)
