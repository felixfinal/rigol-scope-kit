"""Strict bounded TCP SCPI transport.

Derived from the interface and definite-block read approach in scopebench-mcp
0.3.1, transports/{base,tcp}.py (Apache-2.0). Framing, limits and lifecycle
have been rewritten; no permissive non-block or truncated-success fallback.
"""
from __future__ import annotations

import socket
import time

from .config import Device
from .errors import ProtocolError, ScopeError


class TCPTransport:
    def __init__(self, device: Device, deadline: float):
        self.device = device
        self.deadline = deadline
        self.sock: socket.socket | None = None
        self.buffer = bytearray()
        self.commands: list[str] = []

    def remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ScopeError("Operation deadline exceeded")
        return min(self.device.timeout_s, remaining)

    def __enter__(self):
        try:
            self.sock = socket.create_connection((self.device.host, self.device.port), self.remaining())
            self.sock.settimeout(self.remaining())
            return self
        except (OSError, ScopeError) as exc:
            self.close()
            raise ScopeError(f"SCPI connection failed: {exc}") from exc

    def __exit__(self, *_):
        self.close()

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None
        self.buffer.clear()

    def _recv(self):
        if self.sock is None:
            raise ScopeError("Connection is closed")
        self.sock.settimeout(self.remaining())
        chunk = self.sock.recv(65536)
        if not chunk:
            raise ProtocolError("Connection closed before a complete SCPI response")
        self.buffer.extend(chunk)

    def _exact(self, count: int) -> bytes:
        while len(self.buffer) < count:
            self._recv()
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def _send_query(self, command: str):
        if "?" not in command or any(c in command for c in "\r\n;"):
            raise ProtocolError("P1 transport only accepts one query command")
        if self.buffer.strip(b"\r\n"):
            raise ProtocolError("Unexpected trailing data; connection is out of sync")
        self.buffer.clear()
        if self.sock is None:
            raise ScopeError("Connection is closed")
        self.sock.settimeout(self.remaining())
        self.sock.sendall(command.encode("ascii") + b"\n")
        self.commands.append(command)

    def write(self, command: str):
        """One driver-generated setting command. No raw-write tool is exposed."""
        try:
            if not command or any(c in command for c in "\r\n;?"):
                raise ProtocolError("Expected one non-query SCPI command")
            if self.buffer.strip(b"\r\n"):
                raise ProtocolError("Unexpected pending response before write")
            self.buffer.clear()
            if self.sock is None:
                raise ScopeError("Connection is closed")
            self.sock.settimeout(self.remaining())
            self.sock.sendall(command.encode("ascii") + b"\n")
            self.commands.append(command)
        except (OSError, ValueError, ScopeError) as exc:
            self.close()
            if isinstance(exc, ScopeError):
                raise
            raise ScopeError(f"SCPI write failed; state may have changed: {exc}") from exc

    def _first(self) -> bytes:
        for _ in range(5):
            first = self._exact(1)
            if first not in (b"\r", b"\n"):
                return first
        raise ProtocolError("Too many leading response terminators")

    def query(self, command: str, max_bytes: int = 4096) -> str:
        try:
            self._send_query(command)
            result = bytearray(self._first())
            while len(result) <= max_bytes:
                byte = self._exact(1)
                if byte == b"\n":
                    if self.buffer.strip(b"\r\n"):
                        raise ProtocolError("Unexpected data after text response")
                    return result.rstrip(b"\r").decode("ascii")
                result.extend(byte)
            raise ProtocolError("Text response exceeds limit; connection discarded")
        except (OSError, ValueError, ScopeError) as exc:
            self.close()
            if isinstance(exc, ScopeError):
                raise
            raise ScopeError(f"SCPI query failed: {exc}") from exc

    def query_binary(self, command: str) -> bytes:
        try:
            self._send_query(command)
            if self._first() != b"#":
                raise ProtocolError("Expected IEEE 488.2 definite-length binary block")
            digits = self._exact(1)
            if digits not in b"123456789":
                raise ProtocolError("Invalid/indefinite binary block length header")
            length_text = self._exact(int(digits))
            if not length_text.isdigit():
                raise ProtocolError("Non-numeric binary payload length")
            length = int(length_text)
            if not 0 < length <= self.device.max_binary_bytes:
                raise ProtocolError("Binary payload exceeds configured limit or is empty")
            payload = self._exact(length)
            tail = self._exact(1)
            if tail == b"\r":
                tail = self._exact(1)
            if tail != b"\n" or self.buffer.strip(b"\r\n"):
                raise ProtocolError("Invalid binary terminator or extra payload")
            return payload
        except (OSError, ValueError, ScopeError) as exc:
            self.close()
            if isinstance(exc, ScopeError):
                raise
            raise ScopeError(f"SCPI binary query failed: {exc}") from exc
