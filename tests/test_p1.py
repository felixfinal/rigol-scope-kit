from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zlib

from rigol_scope.artifacts import read_artifact, save_screenshot, validate_png
from rigol_scope.config import Device, Settings
from rigol_scope.errors import BusyError, IdentityError, ProtocolError, ScopeError
from rigol_scope.locking import device_lock
from rigol_scope.mcp_server import create_server
from rigol_scope.mho900 import Identity, MHO900, OPTIONS, boolean, number
from rigol_scope.service import ScopeService
from rigol_scope.transport import TCPTransport


def png():
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xffffffff)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00")) + chunk(b"IEND", b"")


class Stream:
    def __init__(self, data, fragment=1, delay=0):
        self.data = data
        self.fragment = fragment
        self.delay = delay
        self.closed = False
        self.writes = []
    def settimeout(self, seconds):
        self.timeout = seconds
    def sendall(self, data):
        self.writes.append(data)
    def recv(self, size):
        if self.delay:
            time.sleep(self.delay)
        chunk, self.data = self.data[:min(size, self.fragment)], self.data[min(size, self.fragment):]
        return chunk
    def close(self):
        self.closed = True


def transport(data, *, fragment=1, limit=1000, deadline=None, delay=0):
    t = TCPTransport(Device("test", "127.0.0.1", 1, max_binary_bytes=limit), deadline or time.monotonic() + 2)
    stream = Stream(data, fragment, delay)
    t.sock = stream
    return t, stream


class FramingTests(unittest.TestCase):
    def test_fragmented_line(self):
        t, s = transport(b"READY\r\n")
        self.assertEqual(t.query("*IDN?"), "READY")
        self.assertEqual(s.writes, [b"*IDN?\n"])

    def test_fragmented_binary_preserves_embedded_newline(self):
        t, _ = transport(b"#15A\nB\rC\r\n")
        self.assertEqual(t.query_binary(":DISPlay:DATA? PNG"), b"A\nB\rC")

    def test_optional_extra_newlines(self):
        t, _ = transport(b"\r\n#13ABC\n\n", fragment=50)
        self.assertEqual(t.query_binary(":DISPlay:DATA? PNG"), b"ABC")

    def test_invalid_frames_close_connection(self):
        for data in [b"ERROR\n", b"#0ABC\n", b"#xABC\n", b"#1xABC\n", b"#13AB", b"#13ABCX", b"#13ABC\rX", b"#13ABC\nOTHER", b"#10\n"]:
            with self.subTest(data=data):
                t, s = transport(data, fragment=100)
                with self.assertRaises(ScopeError):
                    t.query_binary(":DISPlay:DATA? PNG")
                self.assertTrue(s.closed)
                self.assertIsNone(t.sock)

    def test_oversize_header_rejected_before_payload(self):
        t, s = transport(b"#9100000000", limit=10)
        with self.assertRaises(ProtocolError):
            t.query_binary(":DISPlay:DATA? PNG")
        self.assertTrue(s.closed)

    def test_text_truncation_is_failure_not_success(self):
        t, s = transport(b"ABCDE\nNEXT\n")
        with self.assertRaises(ProtocolError):
            t.query("*IDN?", max_bytes=3)
        self.assertTrue(s.closed)
        with self.assertRaises(ScopeError):
            t.query("*IDN?")

    def test_unterminated_and_nonascii_lines_fail(self):
        for value in (b"abc", b"\xff\n", b"abc\nother"):
            with self.subTest(value=value):
                t, _ = transport(value, fragment=50)
                with self.assertRaises(ScopeError):
                    t.query("*IDN?")

    def test_absolute_deadline_stops_slow_stream(self):
        t, s = transport(b"#210" + b"a" * 10 + b"\n", delay=.015, deadline=time.monotonic() + .03)
        with self.assertRaises(ScopeError):
            t.query_binary(":DISPlay:DATA? PNG")
        self.assertTrue(s.closed)

    def test_write_and_compound_commands_rejected(self):
        for value in (":RUN", "*IDN?;:RUN", "*IDN?\n:RUN"):
            t, s = transport(b"ok\n")
            with self.assertRaises(ProtocolError):
                t.query(value)
            self.assertEqual(s.writes, [])

    def test_real_loopback(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen()
            port = server.getsockname()[1]
            def worker():
                with server.accept()[0] as conn:
                    self.assertEqual(conn.recv(100), b"*IDN?\n")
                    conn.sendall(b"RIGOL TECHNOLOGIES,MHO954,SN,FW\n")
            thread = threading.Thread(target=worker)
            thread.start()
            d = Device("test", "127.0.0.1", port, serial="SN")
            with TCPTransport(d, time.monotonic() + 2) as t:
                self.assertEqual(Identity.read(t, d).serial, "SN")
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())


class DriverTests(unittest.TestCase):
    def test_identity_mismatch_stops(self):
        for raw in (b"OTHER,MHO954,SN,FW\n", b"RIGOL,MHO984,SN,FW\n", b"RIGOL,MHO954,WRONG,FW\n", b"bad\n"):
            t, _ = transport(raw)
            with self.assertRaises(IdentityError):
                Identity.read(t, Device("test", "127.0.0.1", 1, serial="SN"))

    def test_nonfinite_numbers_not_reported(self):
        for value in ("nan", "inf", "9.9e37", "ERROR"):
            with self.assertRaises(ProtocolError):
                number(value)
        self.assertEqual(number("-1.2e-3"), -.0012)
        with self.assertRaises(ProtocolError):
            boolean("ERROR")

    def test_options_are_queries_only(self):
        commands = []
        class Fake:
            def query(self, command):
                commands.append(command)
                return "1"
        self.assertTrue(all(MHO900(Fake()).options().values()))
        self.assertEqual(commands, [f":SYSTem:OPTion:STATus? {name}" for name in OPTIONS])

    def test_missing_afg_does_not_query_source(self):
        class Fake:
            def query(self, command):
                raise AssertionError(command)
        self.assertFalse(MHO900(Fake()).generator_status({name: False for name in OPTIONS})["available"])


class ConfigLockArtifactTests(unittest.TestCase):
    def test_config_rejects_invalid_targets_and_budgets(self):
        for values in ({"port": 0}, {"model": "DS1054Z"}, {"host": "bad\nhost"}, {"timeout_s": float("nan")}, {"operation_timeout_s": 1000}, {"max_binary_bytes": -1}):
            with self.subTest(values=values), self.assertRaises(ScopeError):
                Device(**({"alias": "test", "host": "127.0.0.1", "port": 5555} | values))

    def test_relative_config_state_path(self):
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp) / "devices.toml"
            p.write_text('schema_version=1\nstate_dir="state"\n[devices.test]\nhost="127.0.0.1"\nport=5555\n')
            self.assertEqual(Settings.load(p).state_dir, Path(temp) / "state")

    def test_unpinned_nonidentity_denied_before_connect(self):
        with tempfile.TemporaryDirectory() as temp, patch("rigol_scope.service.TCPTransport") as factory:
            service = ScopeService(Settings({"test": Device("test", "127.0.0.1", 1)}, Path(temp)))
            with self.assertRaises(IdentityError):
                service.status("test")
            factory.assert_not_called()

    def test_lock_contends_across_process_and_releases(self):
        with tempfile.TemporaryDirectory() as temp:
            script = "import sys,time;from pathlib import Path;from rigol_scope.locking import device_lock\nwith device_lock(Path(sys.argv[1]),'same-device',time.monotonic()+.1): print('acquired')"
            with device_lock(Path(temp), "same-device", time.monotonic() + 1):
                blocked = subprocess.run([sys.executable, "-c", script, temp], capture_output=True, text=True, timeout=3)
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("BusyError", blocked.stderr)
            free = subprocess.run([sys.executable, "-c", script, temp], capture_output=True, text=True, timeout=3)
            self.assertEqual(free.returncode, 0, free.stderr)

    def test_png_integrity_and_artifact_roundtrip(self):
        self.assertEqual(validate_png(png()), (1, 1))
        for value in (b"ERROR\n", png()[:-1], png() + b"X", png()[:40] + b"X" + png()[41:]):
            with self.assertRaises(ProtocolError):
                validate_png(value)
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp)
            result = save_screenshot(state, png(), {"serial": "SN"}, "test")
            self.assertEqual(read_artifact(state, result["capture_id"], "screen.png"), png())
            metadata = json.loads(read_artifact(state, result["capture_id"], "metadata.json"))
            self.assertEqual(metadata["status"], "complete")
            with self.assertRaises(ScopeError):
                read_artifact(state, "../etc", "passwd")


class MCPTests(unittest.TestCase):
    def test_schema_is_query_only_and_initialization_has_no_io(self):
        async def run():
            with patch("rigol_scope.service.TCPTransport") as factory:
                tools = await create_server().list_tools()
                factory.assert_not_called()
            base = {"scope_identify", "scope_status", "scope_capabilities", "scope_generator_status", "scope_screenshot", "scope_doctor"}
            self.assertTrue(base <= {t.name for t in tools})
            self.assertTrue(all(t.annotations.readOnlyHint for t in tools if t.name in base))
        asyncio.run(run())

    def test_tool_error_is_structured(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                server = create_server(Settings({}, Path(temp)))
                result = await server.call_tool("scope_identify", {"device": "missing"})
                self.assertTrue(result.isError)
                self.assertEqual(result.structuredContent["error_type"], "ScopeError")
        asyncio.run(run())

    def test_screenshot_returns_image_not_just_remote_path(self):
        async def run():
            with tempfile.TemporaryDirectory() as temp:
                state = Path(temp)
                result = save_screenshot(state, png(), {}, "test")
                server = create_server(Settings({}, state))
                with patch.object(ScopeService, "screenshot", return_value=result):
                    reply = await server.call_tool("scope_screenshot", {"device": "test"})
                self.assertFalse(reply.isError)
                self.assertEqual(reply.content[1].type, "image")
                self.assertEqual(reply.structuredContent["capture_id"], result["capture_id"])
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
