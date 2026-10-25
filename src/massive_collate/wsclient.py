"""A small WebSocket client (RFC 6455) on the standard library, for the exchanges' public streams.

Text frames in and out, fragmented messages reassembled, pings answered (and sent, with the
server's pongs counted), close honoured. A frame is taken from the buffer only once all of it has
arrived, and a message's fragments are kept on the object: a read timeout part way through either
(a quiet stream's keepalive wait) leaves the stream where it was, so the next read goes on from
there instead of taking payload bytes for a frame header. Client
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


MAX_FRAME = 64 * 1024 * 1024  # a frame claiming more is a broken stream, not a message
MAX_MESSAGE = 64 * 1024 * 1024  # a message reassembled from fragments: the same, in total


class WebSocket:
    pongs = 0  # pongs received (answers to `ping`)
    _kind: int | None = None  # the message being reassembled: its opcode ...
    _parts: list = []  # ... and its fragments so far (replaced, never changed in place)

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

    def _fill(self, n: int) -> None:
        """Make the buffer hold at least n bytes (a timeout leaves what arrived in it)."""
        while len(self._buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self._buf)))
            if not chunk:
                raise WebSocketClosed("connection closed")
            self._buf += chunk

    def send(self, text: str) -> None:
        self.sock.sendall(encode_frame(OP_TEXT, text.encode()))

    def ping(self, payload: bytes = b"") -> None:
        """A ping: the server answers with a pong (counted in `pongs`) if the connection is alive."""
        self.sock.sendall(encode_frame(OP_PING, payload))

    def _frame(self) -> tuple[bool, int, bytes]:
        """The next whole frame, taken from the buffer only once all of it is there."""
        self._fill(2)
        b0, b1 = self._buf[0], self._buf[1]
        fin, opcode = bool(b0 & 0x80), b0 & 0x0F
        n, at = b1 & 0x7F, 2
        if n == 126:
            self._fill(4)
            n, at = struct.unpack("!H", self._buf[2:4])[0], 4
        elif n == 127:
            self._fill(10)
            n, at = struct.unpack("!Q", self._buf[2:10])[0], 10
        if n > MAX_FRAME:
            raise WebSocketClosed(f"a frame of {n:,} bytes: the stream is broken")
        mask = None
        if b1 & 0x80:
            self._fill(at + 4)
            mask, at = self._buf[at:at + 4], at + 4
        self._fill(at + n)
        payload, self._buf = self._buf[at:at + n], self._buf[at + n:]
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    def recv(self) -> str:
        """The next text (or binary, decoded) message; pings are answered on the way."""
        while True:
            fin, opcode, payload = self._frame()
            if opcode == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, payload))
                continue
            if opcode == OP_PONG:
                self.pongs += 1
                continue
            if opcode == OP_CLOSE:
                try:
                    self.sock.sendall(encode_frame(OP_CLOSE, payload[:2]))
                finally:
                    self.sock.close()
                raise WebSocketClosed(f"closed by the server ({struct.unpack('!H', payload[:2])[0] if len(payload) >= 2 else 'no code'})")
            if opcode in (OP_TEXT, OP_BINARY):
                self._kind, self._parts = opcode, [payload]
            elif opcode == OP_CONT:
                if sum(map(len, self._parts)) + len(payload) > MAX_MESSAGE:
                    self.sock.close()
                    raise WebSocketClosed(f"a message of over {MAX_MESSAGE:,} bytes in fragments: the stream is broken")
                self._parts = [*self._parts, payload]
            if fin and self._kind is not None:
                message, self._kind, self._parts = b"".join(self._parts), None, []
                return message.decode("utf-8", errors="replace")

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, struct.pack("!H", 1000)))
        except OSError:
            pass
        self.sock.close()
