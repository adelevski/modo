"""Fixed finite native client. Executed only by the reviewed owned-container adapter."""

import contextlib
import errno
import http.client
import itertools
import json
import math
import os
import select
import signal
import socket
import sys
import time
from threading import Event, Timer

PORT = 8002
STARTUP_SECONDS = 5
STARTUP_POLLS = 6
CLIENT_SECONDS = 60
MAX_RECORD = 4096
MAX_COMMANDS = 32
MAX_OUTPUT = 65536
POINTS = [{"lat": 41.901, "lon": -87.669}, {"lat": 41.961, "lon": -87.609}]
TARGETS = [{"lat": 41.901, "lon": -87.609}, {"lat": 41.961, "lon": -87.669}]


class SmokeHTTPResponse(http.client.HTTPResponse):
    """Stdlib parser with strict chunk terminators/trailers for smoke evidence."""

    def _get_chunk_left(self):
        if self.chunk_left == 0:
            if self._safe_read(2) != b"\r\n":
                raise http.client.IncompleteRead(b"")
            self.chunk_left = None  # The superclass must not consume CRLF twice.
        return super()._get_chunk_left()

    def _read_and_discard_trailer(self):
        # Stdlib otherwise tolerates EOF instead of the terminating blank line.
        for _ in range(32):
            line = self.fp.readline(8193)
            if line == b"\r\n":
                return
            if not line or not line.endswith(b"\r\n"):
                raise http.client.IncompleteRead(b"")
            if len(line) > 8192:
                raise http.client.HTTPException("smoke trailer line exceeds bound")
        raise http.client.HTTPException("smoke trailer count exceeds bound")


class Budget:
    def __init__(self):
        self.requests = self.cells = self.active = 0

    def begin(self, cells):
        if type(cells) is not int or cells not in {0, 4}:
            raise ValueError("only status or fixed 2x2 matrices are in scope")
        if self.requests >= 12 or self.cells + cells > 48 or self.active >= 2:
            raise RuntimeError("finite HTTP/cell/outstanding budget exhausted")
        self.requests += 1  # Failed requests consume budget too.
        self.cells += cells
        self.active += 1

    def end(self):
        self.active -= 1


class HTTPClient:
    def __init__(self):
        self.deadline = time.monotonic() + CLIENT_SECONDS
        self.budget = Budget()

    def time_left(self, limit):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("native client deadline")
        return min(limit, remaining)

    @contextlib.contextmanager
    def connection(self, costing=None, deadline=None):
        timeout = self.time_left(5)  # Refuse all HTTP work after the stage deadline.
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
            if timeout <= 0:
                raise TimeoutError("HTTP startup deadline")
        end = time.monotonic() + timeout
        self.budget.begin(4 if costing else 0)
        client, timer = None, None
        try:
            client = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
            client._native_http_deadline = end
            client._native_http_expired = Event()
            client.auto_open = 0
            client.connect()
            remaining = min(self.time_left(5), end - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("HTTP absolute deadline")
            sock = client.sock

            def expire():
                client._native_http_expired.set()  # EOF caused by shutdown is not completion.
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = Timer(remaining, expire)
            timer.daemon = True
            timer.start()
            if costing:
                body = json.dumps(
                    {
                        "sources": POINTS,
                        "targets": TARGETS,
                        "costing": costing,
                        "id": f"native-{self.budget.requests}",
                    }
                ).encode()
                client.request(
                    "POST",
                    "/sources_to_targets",
                    body=body,
                    headers={"Content-Type": "application/json", "Connection": "close"},
                )
            else:
                client.request("GET", "/status", headers={"Connection": "close"})
            yield client
        finally:
            try:
                if timer is not None:
                    timer.cancel()
            finally:
                try:
                    if client is not None:
                        client.close()
                finally:
                    self.budget.end()

    def http_time_left(self, client):
        remaining = self.time_left(5)
        expired = getattr(client, "_native_http_expired", None)
        deadline = getattr(client, "_native_http_deadline", None)
        if expired is not None and expired.is_set():
            raise TimeoutError("HTTP absolute timer expired")
        if deadline is not None:
            remaining = min(remaining, deadline - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("HTTP absolute deadline")
        return remaining

    def response(self, client):
        # HTTPConnection.getresponse() clears client.sock for will-close replies.
        # Its makefile still owns the transport while HTTPResponse reads the body.
        transport = client.sock
        if transport is None:
            raise RuntimeError("HTTP transport unavailable before response parsing")
        transport.settimeout(self.http_time_left(client))
        client.response_class = SmokeHTTPResponse
        result = client.getresponse()
        raw = bytearray()
        try:
            for _ in range(32):
                if result.isclosed():
                    break  # The parser may have released the final socket reference.
                transport.settimeout(self.http_time_left(client))
                chunk = result.read1(min(65536, 1048577 - len(raw)))
                # Timer shutdown may manufacture close-delimited EOF.
                self.http_time_left(client)
                if not chunk:
                    break
                raw.extend(chunk)
                if len(raw) > 1048576:
                    raise RuntimeError("response exceeds 1 MiB")
            else:
                raise RuntimeError("HTTP response read budget exceeded")
            self.http_time_left(client)
            if result.length is not None and result.length != 0:
                raise RuntimeError("incomplete Content-Length response")
            if result.chunked and not result.isclosed():
                raise RuntimeError("incomplete chunked response")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                if result.status == 200:
                    raise RuntimeError(
                        "successful native response was not JSON"
                    ) from None
                body = {"non_json_error": True}  # prime_server 504 has a text body.
            # Reject expiry during framing/JSON processing too.
            self.http_time_left(client)
            return result.status, body
        finally:
            # A detached response is no longer retained by HTTPConnection.close().
            result.close()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate protocol field")
        result[key] = value
    return result


def decode_record(raw):
    if len(raw) > MAX_RECORD or not raw.endswith(b"\n"):
        raise ValueError("control record bound/framing")
    row = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(row, dict):
        raise TypeError("protocol object required")
    return row


def isolation():
    with open("/proc/self/status") as stream:
        status = dict(line.split(":", 1) for line in stream if ":" in line)
    result = {
        "uid": os.getuid(),
        "gid": os.getgid(),
        "cap_eff": status["CapEff"].strip(),
        "no_new_privs": status["NoNewPrivs"].strip(),
        "seccomp": status["Seccomp"].strip(),
    }
    for name in ["memory.max", "cpu.max", "pids.max"]:
        with open("/sys/fs/cgroup/" + name) as stream:
            result[name] = stream.read(128).strip()
    if result != {
        "uid": 65532,
        "gid": 65532,
        "cap_eff": "0000000000000000",
        "no_new_privs": "1",
        "seccomp": "2",
        "memory.max": str(512 * 1024**2),
        "cpu.max": "100000 100000",
        "pids.max": "64",
    }:
        raise RuntimeError("client does not share reviewed isolation/cgroup")
    try:
        with open("/native-review-write-denied", "w"):
            pass
    except OSError as error:
        if error.errno != errno.EROFS:
            raise
    else:
        raise RuntimeError("client root filesystem writable")
    return result


def listener_families():
    result = {"tcp4": False, "tcp6": False, "wildcard4": False, "wildcard6": False}
    for family, path in [(4, "/proc/net/tcp"), (6, "/proc/net/tcp6")]:
        try:
            with open(path) as stream:
                next(stream, None)
                for line in itertools.islice(stream, 512):
                    fields = line.split()
                    if len(fields) > 3 and fields[3] == "0A":
                        address, port = fields[1].rsplit(":", 1)
                        if int(port, 16) == PORT:
                            result["tcp" + str(family)] = True
                            result["wildcard" + str(family)] |= int(address, 16) == 0
        except FileNotFoundError:
            continue
    return result


class ControlIO:
    """Only this client's finite stdin/stdout records, never a general transport."""

    def __init__(self, client, input_fd=0, output_fd=1):
        self.client, self.input_fd, self.output_fd = client, input_fd, output_fd
        self.buffer = bytearray()
        self.output_bytes = self.output_records = 0
        os.set_blocking(input_fd, False)
        os.set_blocking(output_fd, False)

    def read(self):
        end = time.monotonic() + self.client.time_left(5)
        for _ in range(64):
            if b"\n" in self.buffer:
                raw, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                return decode_record(bytes(raw) + b"\n")
            if len(self.buffer) > MAX_RECORD:
                raise ValueError("control input overflow")
            remaining = min(self.client.time_left(5), end - time.monotonic())
            if (
                remaining <= 0
                or not select.select([self.input_fd], [], [], remaining)[0]
            ):
                raise TimeoutError("control input deadline")
            chunk = os.read(self.input_fd, min(4096, MAX_RECORD + 1 - len(self.buffer)))
            if not chunk:
                raise EOFError("control EOF before finish")
            self.buffer.extend(chunk)
        raise RuntimeError("control read budget")

    def write(self, row):
        raw = (json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if (
            len(raw) > MAX_RECORD
            or self.output_bytes + len(raw) > MAX_OUTPUT
            or self.output_records >= 64
        ):
            raise ValueError("client output overflow")
        self.output_bytes += len(raw)
        self.output_records += 1
        offset = 0
        end = time.monotonic() + self.client.time_left(5)
        for _ in range(64):
            if offset == len(raw):
                return
            remaining = min(self.client.time_left(5), end - time.monotonic())
            if (
                remaining <= 0
                or not select.select([], [self.output_fd], [], remaining)[1]
            ):
                raise TimeoutError("control output deadline")
            written = os.write(self.output_fd, raw[offset:])
            if written <= 0:
                raise BrokenPipeError("control output lost")
            offset += written
        raise RuntimeError("control write budget")


class Client(HTTPClient):
    def __init__(self):
        super().__init__()
        self.slots = {}
        self.commands = 0
        self.startup_done = False
        self.ready = False
        self.startup_observations = []

    def counters(self):
        return {
            "requests": self.budget.requests,
            "cells": self.budget.cells,
            "active": self.budget.active,
        }

    def startup(self):
        if self.startup_done or self.budget.requests:
            raise ValueError("startup cannot repeat")
        self.startup_done = True  # Even failure must not reset the global budget.
        end = time.monotonic() + self.time_left(STARTUP_SECONDS)
        attempts, next_http = 0, time.monotonic()
        for poll in range(STARTUP_POLLS):
            remaining = min(self.time_left(5), end - time.monotonic())
            if remaining <= 0:
                raise TimeoutError("namespace startup deadline")
            observed = listener_families()
            self.startup_observations.append({"poll": poll + 1, **observed})
            if (
                observed["wildcard4"] or observed["wildcard6"]
            ) and time.monotonic() >= next_http:
                attempts += 1
                try:
                    with self.connection(deadline=end) as client:
                        status, body = self.response(client)
                except ConnectionRefusedError:
                    self.startup_observations[-1]["status_outcome"] = (
                        "connection-refused"
                    )
                else:
                    self.startup_observations[-1]["status_outcome"] = "http-response"
                    if (
                        status != 200
                        or not isinstance(body, dict)
                        or body.get("version") != "3.9.1"
                    ):
                        raise RuntimeError("namespace startup status/version")
                    self.ready = True
                    return {
                        "status_attempts": attempts,
                        "ipv4_http_ready": True,
                        "observations": self.startup_observations,
                    }
                if attempts >= 2:
                    raise RuntimeError("namespace status-probe cap exhausted")
                next_http = max(time.monotonic() + 0.2, end - 1)
            if poll + 1 < STARTUP_POLLS:
                remaining = min(self.time_left(5), end - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError("namespace startup deadline")
                time.sleep(min(1, remaining))
        raise TimeoutError("namespace startup poll cap")

    def open_slot(self, costing):
        context = self.connection(costing)
        client = context.__enter__()
        number = self.budget.requests
        self.slots[number] = (context, client)
        return {"slot": number}

    def close_slot(self, number):
        context, _client = self.slots.pop(number)
        context.__exit__(None, None, None)
        return {"closed": number}

    def read_slot(self, number):
        try:
            status, body = self.response(self.slots[number][1])
            normalized = {
                "non_json_error": bool(
                    isinstance(body, dict) and body.get("non_json_error")
                )
            }
            if status == 200:
                if not isinstance(body, dict):
                    raise ValueError("matrix object required")
                rows = body.get("sources_to_targets")
                if not isinstance(rows, list) or len(rows) not in {2, 4}:
                    raise ValueError("matrix shape")
                cells = (
                    [cell for row in rows for cell in row] if len(rows) == 2 else rows
                )
                if len(cells) != 4 or any(
                    not isinstance(cell, dict)
                    or type(cell.get("time")) not in {int, float}
                    or not math.isfinite(cell["time"])
                    or cell["time"] < 0
                    for cell in cells
                ):
                    raise ValueError("finite four-cell matrix required")
                normalized = {
                    "sources_to_targets": [{"time": cell["time"]} for cell in cells]
                }
            return {"status": status, "body": normalized}
        finally:
            self.close_slot(number)

    def dispatch(self, row):
        self.time_left(5)
        self.commands += 1
        if (
            self.commands > MAX_COMMANDS
            or type(row.get("seq")) is not int
            or row["seq"] != self.commands
        ):
            raise ValueError("control sequence/budget")
        op = row.get("op")
        expected = {
            "startup": {"seq", "op"},
            "open": {"seq", "op", "costing"},
            "pending": {"seq", "op", "slot"},
            "read": {"seq", "op", "slot"},
            "close": {"seq", "op", "slot"},
            "finish": {"seq", "op"},
        }
        if op not in expected or set(row) != expected[op]:
            raise ValueError("fixed command fields")
        if op == "startup":
            return self.startup()
        if not self.ready:
            raise ValueError("startup first")
        if op == "open":
            if row["costing"] not in {"auto", "pedestrian"}:
                raise ValueError("fixed costing required")
            return self.open_slot(row["costing"])
        if op == "finish":
            if self.slots or self.budget.active:
                raise ValueError("finish with outstanding sockets")
            return {"finished": True}
        number = row["slot"]
        if type(number) is not int or number not in self.slots:
            raise ValueError("unknown client slot")
        if op == "close":
            return self.close_slot(number)
        if op == "read":
            return self.read_slot(number)
        client = self.slots[number][1]
        self.http_time_left(client)
        return {
            "slot": number,
            "pending": not bool(select.select([client.sock], [], [], 0)[0]),
        }

    def close_all(self):
        failures = []
        for number in list(self.slots):
            try:
                self.close_slot(number)
            except Exception as error:  # noqa: BLE001 - close every owned slot
                failures.append(type(error).__name__)
        if failures:
            raise RuntimeError("client socket cleanup failed")


def run():
    client = Client()
    control = ControlIO(client)

    def stop(signum, frame):
        raise InterruptedError("namespace client interrupted")

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, CLIENT_SECONDS)
    row = {"seq": 0, "op": "handshake"}
    try:
        control.write(
            {
                "seq": 0,
                "op": "handshake",
                "result": {"schema": "modo-native-client-v1", "isolation": isolation()},
                "budget": client.counters(),
            }
        )
        for _ in range(MAX_COMMANDS):
            row = control.read()
            result = client.dispatch(row)
            control.write(
                {
                    "seq": row["seq"],
                    "op": row["op"],
                    "result": result,
                    "budget": client.counters(),
                }
            )
            if row["op"] == "finish":
                return
        raise RuntimeError("control command cap before finish")
    except Exception as error:
        try:
            error_op = row.get("op")
            if not isinstance(error_op, str) or error_op not in {
                "handshake",
                "startup",
                "open",
                "pending",
                "read",
                "close",
                "finish",
            }:
                error_op = "invalid"
            control.write(
                {
                    "seq": client.commands,
                    "op": error_op,
                    "error_type": type(error).__name__,
                    "observations": client.startup_observations,
                    "budget": client.counters(),
                }
            )
        except Exception:  # noqa: BLE001, S110 - preserve original failure if output lost
            pass
        raise
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        client.close_all()


if __name__ == "__main__":
    try:
        run()
    except Exception:  # noqa: BLE001 - fixed client reports bounded typed errors only
        sys.exit(1)  # Only typed bounded diagnostics; no source/argv traceback.
