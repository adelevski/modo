"""One fixed native-client exec; no endpoint selection or reusable RPC service."""

import json
import math
import os
import re
import select
import subprocess
import time
from functools import wraps

import namespace_client as client


def terminal_failure(method):
    """Any uncertain fixed-client operation leaves only owned cleanup available."""

    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except BaseException as error:
            previous = self.failed
            self.failed = True
            if not previous and self.process is not None:
                try:
                    self.owner.failed_command(
                        ["exec", self.owner.container_id],
                        5,
                        stderr=bytes(self.stderr),
                        exception=error,
                        truncated=self.stderr_truncated,
                    )
                except Exception:  # noqa: BLE001, S110 - preserve original when evidence sink fails
                    pass
            raise

    return guarded


class NamespaceSession:
    def __init__(self, owner, popen=None):
        self.owner = owner
        self.popen = popen or subprocess.Popen
        self.process = None
        self.attempted = self.finished = False
        self.failed = False
        self.seq = 0
        self.deadline = None
        self.buffer = bytearray()
        self.stderr = bytearray()
        self.stderr_truncated = False
        self.stdout_bytes = self.events = 0
        self.eof = set()
        self.slots = set()
        self.slot_deadlines = {}

    def left(self, limit):
        remaining = self.owner.time_left(limit)
        if self.deadline is not None:
            remaining = min(remaining, self.deadline - time.monotonic())
        if remaining <= 0:
            raise TimeoutError("namespace exec deadline")
        return remaining

    @terminal_failure
    def start(self):
        if self.attempted:
            raise RuntimeError("namespace exec cannot be retried")
        self.attempted = True  # Unknown exec outcome never permits replacement.
        self.owner.ensure_other_stage_idle()
        state = self.owner.runtime_state("client-start", self.left(1))
        if not state["running"] or state["oom_killed"]:
            raise RuntimeError("native router exited/OOM before client")
        row = self.owner.document("container", "inspect", self.owner.container_id)
        self.owner.validate_container(row[0])
        network = self.owner.document("network", "inspect", self.owner.network_id)[0]
        self.owner.validate_network(network, {self.owner.container_id})
        if set(network["Containers"]) != {self.owner.container_id}:
            raise RuntimeError(
                "namespace client requires exact owned bridge membership"
            )
        source = self.owner.namespace_source()
        if len(source.encode()) > 65536:
            raise ValueError("fixed client source exceeds exec argument cap")
        duration = self.left(client.CLIENT_SECONDS)
        self.deadline = time.monotonic() + duration
        args = [
            "exec",
            "--interactive",
            "--user",
            "65532:65532",
            self.owner.container_id,
            "/usr/bin/timeout",
            "--signal=TERM",
            "--kill-after=2s",
            f"{duration:.3f}s",
            "python3",
            "-I",
            "-B",
            "-c",
            source,
        ]
        self.owner.command_budget(args)
        try:
            self.process = self.popen(
                self.owner.command + args,
                env=self.owner.env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for stream in [
                self.process.stdin,
                self.process.stdout,
                self.process.stderr,
            ]:
                os.set_blocking(stream.fileno(), False)
            row = self.receive(5)
            if (
                set(row) != {"seq", "op", "result", "budget"}
                or row["seq"] != 0
                or row["op"] != "handshake"
                or type(row["seq"]) is not int
            ):
                raise ValueError("client handshake framing")
            if (
                row["result"]
                != {
                    "schema": "modo-native-client-v1",
                    "isolation": {
                        "uid": 65532,
                        "gid": 65532,
                        "cap_eff": "0000000000000000",
                        "no_new_privs": "1",
                        "seccomp": "2",
                        "memory.max": str(512 * 1024**2),
                        "cpu.max": "100000 100000",
                        "pids.max": "64",
                    },
                }
                or type(row["result"]["isolation"]["uid"]) is not int
                or type(row["result"]["isolation"]["gid"]) is not int
            ):
                raise ValueError("client/server shared isolation mismatch")
            self.check_budget(row["budget"], 0, 0, 0)
        except BaseException as error:
            self.failed = True
            self.owner.failed_command(
                args,
                duration,
                stderr=bytes(self.stderr),
                exception=error,
                truncated=self.stderr_truncated,
            )
            raise

    def drain_once(self, end):
        readers = [
            getattr(self.process, name)
            for name in ["stdout", "stderr"]
            if name not in self.eof
        ]
        remaining = min(self.left(5), end - time.monotonic())
        if remaining <= 0 or not readers:
            raise (
                EOFError("namespace stream ended before expected reply")
                if not readers
                else TimeoutError("namespace reply deadline")
            )
        ready = select.select(readers, [], [], remaining)[0]
        if not ready:
            raise TimeoutError("namespace reply deadline")
        for stream in ready:
            name = "stdout" if stream is self.process.stdout else "stderr"
            raw = os.read(stream.fileno(), 4096)
            if not raw:
                self.eof.add(name)
                continue
            if name == "stdout":
                self.stdout_bytes += len(raw)
                if self.stdout_bytes > client.MAX_OUTPUT:
                    raise ValueError("namespace stdout overflow")
                self.buffer.extend(raw)
                if b"\n" not in self.buffer and len(self.buffer) > client.MAX_RECORD:
                    raise ValueError("namespace partial record overflow")
            else:
                available = max(0, 8192 - len(self.stderr))
                self.stderr.extend(raw[:available])
                self.stderr_truncated |= len(raw) > available
                if self.stderr_truncated:
                    raise ValueError("namespace stderr overflow")

    def receive(self, seconds):
        end = time.monotonic() + self.left(seconds)
        for _ in range(128):
            if b"\n" in self.buffer:
                raw, _, rest = self.buffer.partition(b"\n")
                self.buffer = bytearray(rest)
                self.events += 1
                if self.events > 64:
                    raise ValueError("namespace event cap")
                return client.decode_record(bytes(raw) + b"\n")
            if "stdout" in self.eof:
                raise EOFError("namespace stdout EOF before acknowledgement")
            self.drain_once(end)
        raise RuntimeError("namespace read operation cap")

    def send(self, row):
        raw = (json.dumps(row, allow_nan=False, separators=(",", ":")) + "\n").encode()
        if len(raw) > client.MAX_RECORD:
            raise ValueError("namespace command overflow")
        offset = 0
        end = time.monotonic() + self.left(5)
        for _ in range(64):
            if offset == len(raw):
                return
            remaining = min(self.left(5), end - time.monotonic())
            if (
                remaining <= 0
                or not select.select([], [self.process.stdin], [], remaining)[1]
            ):
                raise TimeoutError("namespace command deadline")
            size = os.write(self.process.stdin.fileno(), raw[offset:])
            if size <= 0:
                raise BrokenPipeError("namespace control pipe")
            offset += size
        raise RuntimeError("namespace write operation cap")

    def check_budget(self, budget, requests, cells, active):
        if (
            not isinstance(budget, dict)
            or set(budget) != {"requests", "cells", "active"}
            or any(type(v) is not int for v in budget.values())
        ):
            raise ValueError("namespace counter schema")
        if (
            budget != {"requests": requests, "cells": cells, "active": active}
            or not 0 <= requests <= 12
            or not 0 <= cells <= 48
            or not 0 <= active <= 2
        ):
            raise ValueError("namespace budget divergence")
        (
            self.owner.budget.requests,
            self.owner.budget.cells,
            self.owner.budget.active,
        ) = requests, cells, active

    def call(self, op, **fields):
        if (
            self.failed
            or self.finished
            or self.process is None
            or self.seq >= client.MAX_COMMANDS
        ):
            raise RuntimeError("namespace session state/command cap")
        self.left(5)
        self.seq += 1
        try:
            self.send({"seq": self.seq, "op": op, **fields})
            row = self.receive(5)
            if (
                row.get("seq") != self.seq
                or row.get("op") != op
                or type(row.get("seq")) is not int
            ):
                raise ValueError("namespace response identity")
            if "error_type" in row:
                if (
                    set(row) != {"seq", "op", "error_type", "observations", "budget"}
                    or not isinstance(row["error_type"], str)
                    or not re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_]{0,63}", row["error_type"]
                    )
                ):
                    raise ValueError("namespace error schema")
                self.observations(row["observations"], allow_empty=True)
                budget = row["budget"]
                if (
                    not isinstance(budget, dict)
                    or set(budget) != {"requests", "cells", "active"}
                    or any(type(v) is not int for v in budget.values())
                ):
                    raise ValueError("namespace error counters")
                old = self.owner.budget
                before = (old.requests, old.cells, old.active)
                allowed = {before}
                if op == "startup" and before == (0, 0, 0):
                    allowed = {(attempts, 0, 0) for attempts in range(3)}
                elif op == "open":
                    allowed.add((old.requests + 1, old.cells + 4, old.active))
                elif op in {"read", "close"} and old.active > 0:
                    allowed.add((old.requests, old.cells, old.active - 1))
                if (
                    budget["requests"],
                    budget["cells"],
                    budget["active"],
                ) not in allowed:
                    raise ValueError(
                        "impossible per-operation namespace error transition"
                    )
                self.check_budget(
                    budget, budget["requests"], budget["cells"], budget["active"]
                )
                self.owner.client_event(
                    error_type=row["error_type"],
                    observations=row["observations"],
                    budget=budget,
                )
                raise RuntimeError(
                    "fixed namespace client failed: " + row["error_type"]
                )
            if set(row) != {"seq", "op", "result", "budget"} or not isinstance(
                row["result"], dict
            ):
                raise ValueError("namespace response schema")
            return row
        except BaseException as error:
            self.failed = True
            try:
                self.owner.failed_command(
                    ["exec", self.owner.container_id],
                    5,
                    stderr=bytes(self.stderr),
                    exception=error,
                    truncated=self.stderr_truncated,
                )
            except Exception:  # noqa: BLE001, S110 - preserve original when evidence sink fails
                pass
            raise

    @staticmethod
    def observations(observations, allow_empty=False):
        if (
            not isinstance(observations, list)
            or not (0 if allow_empty else 1) <= len(observations) <= 6
        ):
            raise ValueError("namespace observation cap")
        for number, obs in enumerate(observations, 1):
            basic = {"poll", "tcp4", "tcp6", "wildcard4", "wildcard6"}
            if (
                not isinstance(obs, dict)
                or set(obs) not in [basic, basic | {"status_outcome"}]
                or type(obs["poll"]) is not int
                or obs["poll"] != number
                or any(type(obs[k]) is not bool for k in basic - {"poll"})
                or (obs["wildcard4"] and not obs["tcp4"])
                or (obs["wildcard6"] and not obs["tcp6"])
                or (
                    "status_outcome" in obs
                    and obs["status_outcome"]
                    not in {"http-response", "connection-refused"}
                )
            ):
                raise ValueError("namespace observation schema")

    @terminal_failure
    def startup(self):
        row = self.call("startup")
        result = row["result"]
        if (
            set(result) != {"status_attempts", "ipv4_http_ready", "observations"}
            or type(result["status_attempts"]) is not int
            or result["status_attempts"] not in {1, 2}
            or result["ipv4_http_ready"] is not True
        ):
            raise ValueError("namespace startup result")
        self.observations(result["observations"])
        outcomes = [
            obs["status_outcome"]
            for obs in result["observations"]
            if "status_outcome" in obs
        ]
        if (
            len(outcomes) != result["status_attempts"]
            or outcomes[-1] != "http-response"
        ):
            raise ValueError("namespace startup outcomes differ from counter")
        self.check_budget(row["budget"], result["status_attempts"], 0, 0)
        return result

    @terminal_failure
    def open(self, costing):
        old = self.owner.budget
        requests, cells, active = old.requests, old.cells, old.active
        if (
            costing not in {"auto", "pedestrian"}
            or requests >= 12
            or cells + 4 > 48
            or active >= 2
        ):
            raise RuntimeError("host finite request reservation refused")
        # Measured before the RPC, this is a conservative bound on the child's
        # later-starting <=5s HTTP attempt; no shared clock epoch is assumed.
        request_end = time.monotonic() + self.left(5)
        row = self.call("open", costing=costing)
        number = requests + 1
        if (
            row["result"] != {"slot": number}
            or type(row["result"].get("slot")) is not int
        ):
            raise ValueError("namespace slot identity")
        self.check_budget(row["budget"], number, cells + 4, active + 1)
        self.slots.add(number)
        self.slot_deadlines[number] = request_end
        return number

    def slot_deadline(self, number):
        if number not in self.slots:
            raise ValueError("host unknown slot deadline")
        return self.slot_deadlines[number]

    @terminal_failure
    def pending(self, number):
        if number not in self.slots:
            raise ValueError("host unknown slot")
        row = self.call("pending", slot=number)
        result = row["result"]
        if (
            set(result) != {"slot", "pending"}
            or result["slot"] != number
            or type(result["slot"]) is not int
            or type(result["pending"]) is not bool
        ):
            raise ValueError("namespace pending schema")
        b = self.owner.budget
        self.check_budget(row["budget"], b.requests, b.cells, b.active)
        return result["pending"]

    @terminal_failure
    def release(self, number, op):
        if number not in self.slots:
            raise ValueError("host unknown slot")
        row = self.call(op, slot=number)
        b = self.owner.budget
        self.check_budget(row["budget"], b.requests, b.cells, b.active - 1)
        self.slots.remove(number)
        self.slot_deadlines.pop(number)
        return row["result"]

    @terminal_failure
    def close_slot(self, number):
        result = self.release(number, "close")
        if result != {"closed": number} or type(result.get("closed")) is not int:
            raise ValueError("namespace close acknowledgement")

    @terminal_failure
    def read_slot(self, number):
        result = self.release(number, "read")
        if (
            set(result) != {"status", "body"}
            or type(result["status"]) is not int
            or not 100 <= result["status"] <= 599
            or not isinstance(result["body"], dict)
        ):
            raise ValueError("namespace normalized response schema")
        body = result["body"]
        if result["status"] == 200:
            if (
                set(body) != {"sources_to_targets"}
                or not isinstance(body["sources_to_targets"], list)
                or len(body["sources_to_targets"]) != 4
            ):
                raise ValueError("namespace matrix schema")
            for cell in body["sources_to_targets"]:
                if (
                    not isinstance(cell, dict)
                    or set(cell) != {"time"}
                    or type(cell["time"]) not in {int, float}
                    or not math.isfinite(cell["time"])
                    or cell["time"] < 0
                ):
                    raise ValueError("namespace finite matrix cell")
        elif (
            set(body) != {"non_json_error"} or type(body["non_json_error"]) is not bool
        ):
            raise ValueError("namespace error body schema")
        return result["status"], body

    @terminal_failure
    def finish(self):
        row = self.call("finish")
        b = self.owner.budget
        self.check_budget(row["budget"], b.requests, b.cells, 0)
        if (
            self.slots
            or row["result"] != {"finished": True}
            or row["result"].get("finished") is not True
        ):
            raise ValueError("namespace finish with unclosed slots")
        self.process.stdin.close()
        end = time.monotonic() + self.left(5)
        for _ in range(128):
            if {"stdout", "stderr"} <= self.eof:
                break
            self.drain_once(end)
        else:
            raise RuntimeError("namespace EOF operation cap")
        if self.buffer or self.process.wait(timeout=self.left(1)) != 0:
            raise RuntimeError("namespace extra output or unsuccessful exec exit")
        self.finished = True

    def abort_cli(self):
        # Terminating this CLI does NOT establish in-container exec termination.
        if self.process is None:
            return
        try:
            if not self.process.stdin.closed:
                self.process.stdin.close()
        finally:
            try:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=self.owner.time_left(1))
            finally:
                self.process.stdout.close()
                self.process.stderr.close()
