"""Connection: framed TCP transport for one client.

The framing is Minecraft's standard VarInt-length-prefixed packets, no
compression and no encryption (offline-mode server, compression left
disabled for simplicity). Reads are buffered: a small recv() loop
accumulates bytes until a full packet is available.
"""

import threading

from .codec import Reader, write_varint


class Connection:
    def __init__(self, sock, addr):
        self.sock = sock
        self.addr = addr
        self.buf = b""
        self.lock = threading.Lock()
        self.alive = True

    def _recv_some(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ConnectionError("client closed connection")
        self.buf += chunk

    def _read_raw_varint_from_buf(self):
        value = 0
        shift = 0
        i = 0
        while True:
            if i >= len(self.buf):
                return None, 0
            b = self.buf[i]
            i += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return value, i

    def read_packet(self):
        """Blocking read of one full packet, returns (packet_id, Reader)."""
        while True:
            length, consumed = self._read_raw_varint_from_buf()
            if length is not None and len(self.buf) >= consumed + length:
                self.buf = self.buf[consumed:]
                payload = self.buf[:length]
                self.buf = self.buf[length:]
                r = Reader(payload)
                pid = r.read_varint()
                return pid, r
            self._recv_some()

    def send_packet(self, packet_id, payload=b""):
        body = write_varint(packet_id) + payload
        frame = write_varint(len(body)) + body
        with self.lock:
            self.sock.sendall(frame)


def log(addr, msg):
    print(f"[{addr}] {msg}")
