"""Dante wire formats: request/response packets (ARC/CMC/flow control) and
info multicasts. Ported from Inferno's protocol/req_resp.rs, mcast.rs, proto_arc.rs."""
from __future__ import annotations

import struct

HEADER = 10           # start_code, total_length, seqnum, opcode1, opcode2 (all u16)
MCAST_HEADER = 32
CODE_OK = 1
MORE_PAGES = 0x8112
PAGE_SOFT_LIMIT = 800

ARC_PORT, CMC_PORT, FLOWS_CONTROL_PORT, INFO_REQUEST_PORT = 4440, 8800, 4455, 8700
DBCP1 = 0x1102


def make_packet(start_code: int, seq: int, op1: int, op2: int, content: bytes = b"") -> bytes:
    return struct.pack("!HHHHH", start_code, HEADER + len(content), seq & 0xFFFF, op1, op2) + content


def parse_packet(pkt: bytes):
    """-> (start_code, seq, opcode1, opcode2, content) or None."""
    if len(pkt) < HEADER:
        return None
    start, _length, seq, op1, op2 = struct.unpack_from("!HHHHH", pkt)
    return start, seq, op1, op2, pkt[HEADER:]


def make_mcast(start_code: int, seq: int, process: int, device_id: bytes, vendor: bytes,
               opcode: bytes, content: bytes) -> bytes:
    return (struct.pack("!HHHH", start_code, MCAST_HEADER + len(content), seq & 0xFFFF, process)
            + device_id + vendor + opcode + content)


def read_cstr(buf: bytes, offset: int) -> str | None:
    if offset >= len(buf):
        return None
    end = buf.find(b"\0", offset)
    return buf[offset:end if end >= 0 else len(buf)].decode("utf-8", "replace")


def fixed_str(s: str, n: int) -> bytes:
    """String truncated/zero-padded to n bytes (no terminator needed)."""
    return s.encode()[:n].ljust(n, b"\0")


class Buf:
    """Growable big-endian buffer with a write position (like Rust's ByteBuffer).
    Offsets inside Dante packets are absolute from packet start, so callers
    reserve the 10-byte header at the beginning."""

    def __init__(self, initial: bytes = b""):
        self.b = bytearray(initial)
        self.wpos = len(self.b)

    def write(self, data: bytes):
        end = self.wpos + len(data)
        if end > len(self.b):
            self.b.extend(b"\0" * (end - len(self.b)))
        self.b[self.wpos:end] = data
        self.wpos = end

    def u8(self, v):
        self.write(struct.pack("!B", v))

    def u16(self, v):
        self.write(struct.pack("!H", v & 0xFFFF))

    def u32(self, v):
        self.write(struct.pack("!I", v & 0xFFFFFFFF))

    def i32(self, v):
        self.write(struct.pack("!i", v))

    def cstr(self, s: str) -> int:
        off = self.wpos
        self.write(s.encode() + b"\0")
        return off

    def cstr_or_0(self, s: str | None) -> int:
        return self.cstr(s) if s is not None else 0

    def align(self, n: int):
        while self.wpos % n:
            self.u8(0)

    def bytes(self) -> bytes:
        return bytes(self.b)


def serialize_items(space_items: int, item_size: int, source, transform) -> tuple[bool, bytes]:
    """Paginated list response: [space, count, item * space, ...data]. `transform(item, buf)` writes
    any variable data into buf and returns the item's fixed-size bytes (or None to skip)."""
    buf = Buf(b"\0" * HEADER)
    buf.u8(space_items)
    buf.u8(0)
    if space_items == 0:
        return False, buf.bytes()[HEADER:]
    buf.write(b"\0" * (space_items * item_size))
    item_pos, count, more = HEADER + 2, 0, False
    for item in source:
        if count >= space_items:
            more = True
            break
        out = transform(item, buf)
        if out is None:
            continue
        assert len(out) == item_size
        buf.b[item_pos:item_pos + item_size] = out
        item_pos += item_size
        count += 1
        if buf.wpos >= PAGE_SOFT_LIMIT:
            more = True
            break
    buf.b[HEADER + 1] = count
    return more, buf.bytes()[HEADER:]


def start_index(payload: bytes) -> int | None:
    """Paginated requests carry a 1-based start index at payload[2:4]."""
    if len(payload) < 4 or (payload[2] | payload[3]) == 0:
        return None
    return struct.unpack_from("!H", payload, 2)[0] - 1


def deserialize_items(payload: bytes, item_size: int) -> list[bytes]:
    n = payload[1] if len(payload) > 1 else 0
    n = min(n, (len(payload) - 2) // item_size)
    return [payload[2 + i * item_size:2 + (i + 1) * item_size] for i in range(n)]
