"""A small WebSocket client (RFC 6455) on the standard library, for the exchanges' public streams.

Text frames in and out, fragmented messages reassembled, pings answered, close honoured. Client
frames are masked as the protocol requires. No compression extensions are offered, so servers send
plain frames.
"""
from __future__ import annotations

import base64
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketClosed(ConnectionError):
    pass


def encode_frame(opcode: int, payload: bytes, mask: bytes | None = None) -> bytes:
    """One final frame, masked (client to server)."""
    mask = mask if mask is not None else os.urandom(4)
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class WebSocket:
    def __init__(self, url: str, timeout: float = 30.0, user_agent: str = "massive-collate/1"):
        u = urlparse(url)
        if u.scheme not in ("ws", "wss"):
            raise ValueError(f"not a WebSocket URL: {url}")
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) \
            if u.scheme == "wss" else raw
        self.sock.settimeout(timeout)
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        request = (f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {user_agent}\r\n\r\n")
        self.sock.sendall(request.encode())
        head = self._read_until(b"\r\n\r\n").decode("latin-1")
        status = head.split("\r\n", 1)[0]
        if " 101 " not in status + " ":
            self.sock.close()
            raise ConnectionError(f"WebSocket upgrade refused: {status}")

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketClosed("connection closed during the handshake")
            self._buf += chunk
        head, _, self._buf = self._buf.partition(marker)
        return head

    def _read(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WebSocketClosed("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def send(self, text: str) -> None:
        self.sock.sendall(encode_frame(OP_TEXT, text.encode()))

    def _frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read(2)
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", self._read(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", self._read(8))[0]
        mask = self._read(4) if b1 & 0x80 else None
        payload = self._read(n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self) -> str:
        """The next text (or binary, decoded) message; pings are answered on the way."""
        parts, kind = [], None
        while True:
            fin, opcode, payload = self._frame()
            if opcode == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                try:
                    self.sock.sendall(encode_frame(OP_CLOSE, payload[:2]))
                finally:
                    self.sock.close()
                raise WebSocketClosed(f"closed by the server ({struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else 'no code'})")
            if opcode in (OP_TEXT, OP_BINARY):
                kind, parts = opcode, [payload]
            elif opcode == OP_CONT:
                parts.append(payload)
            if fin and kind is not None:
                return b"".join(parts).decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, struct.pack("!H", 1000)))
        except OSError:
            pass
        self.sock.close()
