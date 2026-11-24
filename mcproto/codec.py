"""Low-level network codec: VarInt/VarLong/string/primitive read & write,
the ``Reader`` class, and ItemStack (de)serialization.

The item-stack helpers depend on the loaded item table from
:mod:`mcproto.data` (``ITEM_NAMES`` / ``ITEM_ID_BY_NAME``). They live
here rather than in ``data.py`` because they're codec-shaped: they read
from / write to a ``Reader`` or byte buffer.
"""

import struct

from . import data as _data
from .data import ITEM_NAMES, ITEM_ID_BY_NAME


# ---------------------------------------------------------------------------
# Write helpers — each returns bytes
# ---------------------------------------------------------------------------

def write_varint(value):
    value &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def write_varlong(value):
    value &= 0xFFFFFFFFFFFFFFFF
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        if value:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def write_string(s):
    data = s.encode("utf-8")
    return write_varint(len(data)) + data


def write_bool(b):
    return b"\x01" if b else b"\x00"


def write_ushort(v):
    return struct.pack(">H", v)


def write_short(v):
    return struct.pack(">h", v)


def write_int(v):
    return struct.pack(">i", v)


def write_long(v):
    return struct.pack(">q", v)


def write_float(v):
    return struct.pack(">f", v)


def write_double(v):
    return struct.pack(">d", v)


def write_ubyte(v):
    return struct.pack(">B", v & 0xFF)


def write_byte(v):
    return struct.pack(">b", v)


def write_uuid(msb, lsb):
    return struct.pack(">QQ", msb & 0xFFFFFFFFFFFFFFFF, lsb & 0xFFFFFFFFFFFFFFFF)


def encode_block_pos(x, y, z):
    val = ((x & 0x3FFFFFF) << 38) | ((z & 0x3FFFFFF) << 12) | (y & 0xFFF)
    return write_long(val if val < 2**63 else val - 2**64)


def encode_chunk_pos(cx, cz):
    # ChunkPos.pack (decompiled): (x & 0xFFFFFFFF) | ((z & 0xFFFFFFFF) << 32),
    # written as a plain 8-byte long (not varlong)
    val = (cx & 0xFFFFFFFF) | ((cz & 0xFFFFFFFF) << 32)
    return write_long(val if val < 2**63 else val - 2**64)


def decode_block_pos(val):
    if val < 0:
        val += 2**64
    x = val >> 38
    if x >= 2**25:
        x -= 2**26
    y = val & 0xFFF
    if y >= 2**11:
        y -= 2**12
    z = (val >> 12) & 0x3FFFFFF
    if z >= 2**25:
        z -= 2**26
    return x, y, z


# empty NBT compound in "network NBT" form (no name field on root):
# tag type 0x0A (compound) immediately followed by 0x00 (end)
EMPTY_NBT_COMPOUND = b"\x0A\x00"


# ---------------------------------------------------------------------------
# ItemStack read / write
# ---------------------------------------------------------------------------

def read_item_stack(r):
    """ItemStack OPTIONAL_(UNTRUSTED_)STREAM_CODEC (decompiled): VarInt count
    (0 => empty stack, nothing else follows); else Holder<Item> (plain VarInt
    global item id) then a DataComponentPatch (VarInt added-count, VarInt
    removed-count, then component data). We only need the item and count to
    track the inventory and decide what to place, and the stack is always the
    last field of the packets we read, so any component data is simply left
    unread (it is lost if the item is saved and restored). Returns
    (name, count) or None for an empty slot."""
    count = r.read_varint()
    if count <= 0:
        return None
    item_id = r.read_varint()
    name = ITEM_NAMES[item_id] if 0 <= item_id < len(ITEM_NAMES) else None
    if name is None:
        return None
    return (name, count)


def read_hashed_stack(r):
    """HashedStack (decompiled, used by container clicks): Boolean present;
    if present: VarInt item id, VarInt count, then a HashedPatchMap =
    VarInt n + n x (VarInt component type id, Int hash), then VarInt m + m x
    VarInt removed component type id. The hashes can't be turned back into
    components, but item and count are all we track. None = empty."""
    if not r.read_bool():
        return None
    item_id = r.read_varint()
    count = r.read_varint()
    for _ in range(r.read_varint()):
        r.read_varint()
        r.read_int()
    for _ in range(r.read_varint()):
        r.read_varint()
    name = ITEM_NAMES[item_id] if 0 <= item_id < len(ITEM_NAMES) else None
    if name is None or count <= 0:
        return None
    return (name, count)


def write_item_stack(name, count):
    """Writes a plain stack (no components) with the given item name and
    count, or an empty slot if name is None/count<=0."""
    if not name or count <= 0:
        return write_varint(0)
    if name not in ITEM_ID_BY_NAME:
        return write_varint(0)
    out = write_varint(count)
    out += write_varint(ITEM_ID_BY_NAME[name])
    out += write_varint(0)  # added components
    out += write_varint(0)  # removed components
    return out


# ---------------------------------------------------------------------------
# Reader — pulls fields out of a raw packet payload (bytes)
# ---------------------------------------------------------------------------

class Reader:
    """Reads fields out of a raw packet payload (bytes)."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def remaining(self):
        return len(self.data) - self.pos

    def read_bytes(self, n):
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def read_varint(self):
        value = 0
        shift = 0
        while True:
            b = self.data[self.pos]
            self.pos += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        if value & 0x80000000:
            value -= 0x100000000
        return value

    def read_varlong(self):
        value = 0
        shift = 0
        while True:
            b = self.data[self.pos]
            self.pos += 1
            value |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        return value

    def read_string(self):
        n = self.read_varint()
        s = self.data[self.pos:self.pos + n].decode("utf-8", errors="replace")
        self.pos += n
        return s

    def read_bool(self):
        v = self.data[self.pos]
        self.pos += 1
        return v != 0

    def read_ubyte(self):
        v = self.data[self.pos]
        self.pos += 1
        return v

    def read_byte(self):
        v = self.data[self.pos]
        self.pos += 1
        return v - 256 if v > 127 else v

    def read_short(self):
        v = struct.unpack(">h", self.data[self.pos:self.pos + 2])[0]
        self.pos += 2
        return v

    def read_ushort(self):
        v = struct.unpack(">H", self.data[self.pos:self.pos + 2])[0]
        self.pos += 2
        return v

    def read_int(self):
        v = struct.unpack(">i", self.data[self.pos:self.pos + 4])[0]
        self.pos += 4
        return v

    def read_long(self):
        v = struct.unpack(">q", self.data[self.pos:self.pos + 8])[0]
        self.pos += 8
        return v

    def read_float(self):
        v = struct.unpack(">f", self.data[self.pos:self.pos + 4])[0]
        self.pos += 4
        return v

    def read_double(self):
        v = struct.unpack(">d", self.data[self.pos:self.pos + 8])[0]
        self.pos += 8
        return v

    def read_uuid(self):
        msb, lsb = struct.unpack(">QQ", self.data[self.pos:self.pos + 16])
        self.pos += 16
        return msb, lsb
