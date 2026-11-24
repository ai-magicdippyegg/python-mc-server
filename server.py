#!/usr/bin/env python3
"""
A basic, from-scratch Minecraft Java Edition server, written in pure Python
(no external MC libraries) for game version 26.3 / protocol 777.

What it does:
  - Flat superflat world (stone from y=-64 to y=-1, air above)
  - Creative mode
  - Player can place blocks (always places stone, regardless of held item -
    see README for why)
  - Multiple players can connect at once

HOW THE PACKET IDS WERE OBTAINED (important):
Minecraft's network protocol changes with almost every version, and version
26.3 (protocol 777) is brand new - there's no public protocol documentation
for it yet. Instead of guessing, the packet IDs below were extracted
DIRECTLY from your uploaded server-26_3.jar using javap (Java's bytecode
disassembler), by reading the exact order that
net.minecraft.network.protocol.game.GameProtocols (and the login/
configuration/status equivalents) register packets in. Packet IDs are
assigned sequentially in registration order, so this gives exact,
non-guessed IDs for this exact version.

What is still "best effort":
  - The internal FIELD LAYOUT of each packet (what order ints/strings/etc.
    appear in) was NOT extracted from the jar (that requires decompiling
    every single packet class's StreamCodec, which is a much bigger job).
    Field layouts are filled in from what's been true across many recent
    versions. These are usually stable release-to-release, but if
    something is wrong, the client will disconnect and print/log a decode
    error - run with DEBUG=1 and tell me what happens and we can fix it.
  - Global block state IDs (air=0, stone=1) are assumed from historical
    ordering (new blocks are always appended, never inserted), not
    extracted from the jar.

See README.md for setup instructions and known limitations.
"""

import json
import math
import os
import socket
import struct
import threading
import time
import random
import zlib
import signal

DEBUG = os.environ.get("MC_DEBUG", "1") == "1"

HOST = "0.0.0.0"
PORT = int(os.environ.get("MC_PORT", "25565"))

PROTOCOL_VERSION = 777
GAME_VERSION_NAME = "26.3"
KNOWN_PACK_VERSION = "26.3"  # best guess: vanilla servers send the game version string here

# ---------------------------------------------------------------------------
# Packet IDs - extracted from server-26_3.jar via javap (see module docstring)
# ---------------------------------------------------------------------------

class Login:
    # clientbound
    DISCONNECT = 0x00
    HELLO = 0x01              # encryption request (unused - offline mode)
    LOGIN_FINISHED = 0x02     # login success
    COMPRESSION = 0x03
    CUSTOM_QUERY = 0x04
    # serverbound
    S_HELLO = 0x00            # login start
    S_KEY = 0x01
    S_CUSTOM_QUERY_ANSWER = 0x02
    S_LOGIN_ACKNOWLEDGED = 0x03


class Config:
    # clientbound
    COOKIE_REQUEST = 0x00
    CUSTOM_PAYLOAD = 0x01
    DISCONNECT = 0x02
    FINISH_CONFIGURATION = 0x03
    KEEP_ALIVE = 0x04
    PING = 0x05
    RESET_CHAT = 0x06
    REGISTRY_DATA = 0x07
    RESOURCE_PACK_POP = 0x08
    RESOURCE_PACK_PUSH = 0x09
    STORE_COOKIE = 0x0B
    TRANSFER = 0x0C
    UPDATE_ENABLED_FEATURES = 0x0D
    UPDATE_TAGS = 0x0E
    SELECT_KNOWN_PACKS = 0x0F
    # serverbound
    S_CLIENT_INFORMATION = 0x00
    S_COOKIE_RESPONSE = 0x01
    S_CUSTOM_PAYLOAD = 0x02
    S_FINISH_CONFIGURATION = 0x03
    S_KEEP_ALIVE = 0x04
    S_PONG = 0x05
    S_RESOURCE_PACK = 0x06
    S_SELECT_KNOWN_PACKS = 0x07
    S_CUSTOM_CLICK_ACTION = 0x08
    S_ACCEPT_CODE_OF_CONDUCT = 0x09


class Play:
    # clientbound (only the ones we use)
    BLOCK_CHANGED_ACK = 0x04
    BLOCK_UPDATE = 0x08
    GAME_EVENT = 0x27
    KEEP_ALIVE = 0x2D
    LEVEL_CHUNK_WITH_LIGHT = 0x2E
    LOGIN = 0x32
    CHUNK_BATCH_FINISHED = 0x0B
    CHUNK_BATCH_START = 0x0C
    FORGET_LEVEL_CHUNK = 0x26
    COMMANDS = 0x10
    CONTAINER_SET_SLOT = 0x14
    SET_HELD_SLOT = 0x6B
    PLAYER_ABILITIES = 0x41
    PLAYER_POSITION = 0x49       # synchronize player position (teleport)
    SET_CHUNK_CACHE_CENTER = 0x60
    SET_DEFAULT_SPAWN_POSITION = 0x63
    # serverbound (only the ones we use)
    S_ACCEPT_TELEPORTATION = 0x00
    S_CHAT_COMMAND = 0x07
    S_CHUNK_BATCH_RECEIVED = 0x0B
    S_CLIENT_INFORMATION = 0x0E
    S_CLIENT_TICK_END = 0x0D
    S_CONTAINER_CLICK = 0x12
    S_CONTAINER_CLOSE = 0x13
    S_CUSTOM_PAYLOAD = 0x16
    S_KEEP_ALIVE = 0x1C
    S_MOVE_PLAYER_POS = 0x1E
    S_MOVE_PLAYER_POS_ROT = 0x1F
    S_MOVE_PLAYER_ROT = 0x20
    S_MOVE_PLAYER_STATUS_ONLY = 0x21
    S_PICK_ITEM_FROM_BLOCK = 0x24
    S_PLAYER_ABILITIES = 0x28
    S_PLAYER_ACTION = 0x29
    S_PLAYER_COMMAND = 0x2A
    S_PLAYER_INPUT = 0x2B
    S_PLAYER_LOADED = 0x2C
    S_PUNCH = 0x2E
    S_SET_CARRIED_ITEM = 0x36
    S_SET_CREATIVE_MODE_SLOT = 0x39
    S_USE_ITEM_ON = 0x42
    S_USE_ITEM = 0x43


# ---------------------------------------------------------------------------
# Low level encode/decode helpers
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


# ---------------------------------------------------------------------------
# Connection: handles framing (length prefix, no compression/encryption -
# offline mode server, compression left disabled for simplicity)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Registries (loaded from registries.json, extracted from the jar's bundled
# datapack file listing - see registries.json / build script)
# ---------------------------------------------------------------------------

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(SCRIPT_DIR, "registries.json")) as f:
    REGISTRIES = json.load(f)
with open(os.path.join(SCRIPT_DIR, "tags.json")) as f:
    TAGS = json.load(f)

with open(os.path.join(SCRIPT_DIR, "item_ids.json")) as f:
    ITEM_NAMES = [n.lower() for n in json.load(f)]  # global item registry order == network item id
ITEM_ID_BY_NAME = {n: i for i, n in enumerate(ITEM_NAMES)}

with open(os.path.join(SCRIPT_DIR, "block_states.json")) as f:
    BLOCK_STATES = json.load(f)  # block name -> [base_state_id, num_states]

# item -> block-to-place lookup: for an item usable to place a block, the
# item name matches the block name by Minecraft convention (confirmed by
# cross-checking against the jar's own registration order - see README).
# We place the block's BASE (first-enumerated) state; for multi-property
# blocks (stairs, logs, etc.) that may not be the "natural" default
# orientation, but it is always the correct block type.
# block_states.json entries are [first_state_id, state_count, default_state_id]
# (default = every property at its natural default: not waterlogged, facing
# north, bottom half, log axis Y, ...)
ITEM_BLOCK_ALIASES = {
    "redstone": "redstone_wire", "wheat_seeds": "wheat", "carrot": "carrots", "potato": "potatoes",
    "beetroot_seeds": "beetroots", "melon_seeds": "melon_stem", "pumpkin_seeds": "pumpkin_stem",
    "cocoa_beans": "cocoa", "glow_berries": "cave_vines", "sweet_berries": "sweet_berry_bush",
    "string": "tripwire", "torchflower_seeds": "torchflower_crop", "pitcher_pod": "pitcher_crop",
}
ITEM_TO_BLOCK_STATE = {}
ITEM_TO_BLOCK_NAME = {}
for item_name in ITEM_NAMES:
    block_name = item_name if item_name in BLOCK_STATES else ITEM_BLOCK_ALIASES.get(item_name)
    if block_name in BLOCK_STATES:
        ITEM_TO_BLOCK_STATE[item_name] = BLOCK_STATES[block_name][2]  # default state id
        ITEM_TO_BLOCK_NAME[item_name] = block_name
# state id -> block name (for pick block), and block -> item name
_STATE_RANGES = sorted((v[0], k) for k, v in BLOCK_STATES.items())
_STATE_STARTS = [x[0] for x in _STATE_RANGES]
_BLOCK_TO_ITEM = {b: i for i, b in ITEM_BLOCK_ALIASES.items()}


def item_for_state(state_id):
    """Item name that picks/represents this block state, or None."""
    import bisect
    i = bisect.bisect_right(_STATE_STARTS, state_id) - 1
    if i < 0:
        return None
    block = _STATE_RANGES[i][1]
    if block in ITEM_ID_BY_NAME:
        return block
    return _BLOCK_TO_ITEM.get(block)


# ---------------------------------------------------------------------------
# Block orientation on placement. block_props.json holds each block's
# properties (name + value list, sorted by name = state-index order, last
# property varying fastest), extracted from the jar. Rules below follow
# vanilla's getStateForPlacement for the common cases.
# ---------------------------------------------------------------------------

with open(os.path.join(SCRIPT_DIR, "block_props.json")) as f:
    BLOCK_PROPS = json.load(f)   # block name -> [[prop, [values...]], ...]

FACE_NAMES = ["down", "up", "north", "south", "west", "east"]   # Use Item On face index
_OPPOSITE = {"north": "south", "south": "north", "east": "west", "west": "east", "up": "down", "down": "up"}
_CLOCKWISE = {"north": "east", "east": "south", "south": "west", "west": "north"}
_FACE_STEP = {"down": (0, -1, 0), "up": (0, 1, 0), "north": (0, 0, -1), "south": (0, 0, 1), "west": (-1, 0, 0), "east": (1, 0, 0)}
_AXIS_OF = {"up": "y", "down": "y", "north": "z", "south": "z", "east": "x", "west": "x"}
_WALL_VARIANT_SUFFIXES = (("_hanging_sign", "_wall_hanging_sign"), ("_sign", "_wall_sign"), ("_banner", "_wall_banner"),
                          ("_head", "_wall_head"), ("_skull", "_wall_skull"))


def horizontal_direction(yaw):
    """Direction the player faces (yaw 0 = south, 90 = west, 180 = north, -90 = east)."""
    return ("south", "west", "north", "east")[int(math.floor(yaw / 90.0 + 0.5)) & 3]


def nearest_looking_direction(yaw, pitch):
    """Axis-aligned direction closest to the player's look vector (negative pitch = looking up)."""
    yr, pr = math.radians(yaw), math.radians(pitch)
    v = {"x": -math.sin(yr) * math.cos(pr), "y": -math.sin(pr), "z": math.cos(yr) * math.cos(pr)}
    axis = max(v, key=lambda a: abs(v[a]))
    if axis == "x": return "east" if v["x"] > 0 else "west"
    if axis == "y": return "up" if v["y"] > 0 else "down"
    return "south" if v["z"] > 0 else "north"


def state_for(block_name, wanted):
    """State id for block_name with the given {property: value-string} overrides
    applied on top of the block's default state."""
    base, count, default = BLOCK_STATES[block_name]
    props = BLOCK_PROPS.get(block_name)
    if not props:
        return default
    off = default - base
    stride = count
    result = base
    for name, values in props:
        stride //= len(values)
        idx = (off // stride) % len(values)
        if name in wanted and wanted[name] in values:
            idx = values.index(wanted[name])
        result += idx * stride
    return result


def decode_state(block_name, state):
    """Inverse of state_for: {property: value} for a state id (used for tests/logging)."""
    base, count, _ = BLOCK_STATES[block_name]
    off, stride, out = state - base, count, {}
    for name, values in BLOCK_PROPS.get(block_name, []):
        stride //= len(values)
        out[name] = values[(off // stride) % len(values)]
    return out


def build_placement(item_name, bx, by, bz, face, cursor_y, yaw, pitch, air_at):
    """Work out what placing item_name against block (bx,by,bz) on `face` creates.
    Returns [(x, y, z, state, block_name), ...] (several for doors/beds), or None if
    it can't be placed that way. air_at(x,y,z) says whether a cell is empty."""
    block = ITEM_TO_BLOCK_NAME.get(item_name)
    if block is None:
        return None
    face_name = FACE_NAMES[face]
    horizontal_face = face_name in ("north", "south", "east", "west")
    dx, dy, dz = _FACE_STEP[face_name]
    px, py, pz = bx + dx, by + dy, bz + dz

    # items that become a different block when attached to a wall
    if "torch" in block and not block.endswith("wall_torch") and "torchflower" not in block:
        if face_name == "down":
            return None
        if horizontal_face:
            block = block.replace("torch", "wall_torch") if block == "torch" else (
                "soul_wall_torch" if block == "soul_torch" else "redstone_wall_torch")
    else:
        for suffix, wall in _WALL_VARIANT_SUFFIXES:
            if block.endswith(suffix) and "wall" not in block:
                if face_name == "up" and suffix == "_hanging_sign":
                    return None
                if horizontal_face and block[:-len(suffix)] + wall in BLOCK_STATES:
                    block = block[:-len(suffix)] + wall
                break
    if block == "ladder" and not horizontal_face:
        return None
    if block not in BLOCK_STATES:
        return None

    props = {n: v for n, v in BLOCK_PROPS.get(block, [])}
    hd = horizontal_direction(yaw)
    want = {}

    if "axis" in props:
        want["axis"] = _AXIS_OF[face_name]
    if "hanging" in props and face_name == "down":
        want["hanging"] = "true"
    if "rotation" in props:
        offset = 180.0 if ("sign" in block or "banner" in block) else 0.0
        want["rotation"] = str(int(math.floor((yaw + offset) * 16.0 / 360.0 + 0.5)) & 15)

    top_bottom = "top" if (face_name == "down" or (face_name != "up" and cursor_y > 0.5)) else "bottom"
    if "trapdoor" in block:
        if horizontal_face:
            want["facing"], want["half"] = face_name, ("top" if cursor_y > 0.5 else "bottom")
        else:
            want["facing"], want["half"] = _OPPOSITE[hd], ("bottom" if face_name == "up" else "top")
    else:
        if "half" in props:
            hv = props["half"]
            want["half"] = "lower" if "lower" in hv else top_bottom
        if "type" in props and "top" in props["type"]:
            want["type"] = top_bottom
        if "facing" in props:
            fv = props["facing"]
            if "face" in props:                       # buttons, levers, grindstones
                attach = "ceiling" if face_name == "down" else ("floor" if face_name == "up" else "wall")
                want["face"] = attach
                want["facing"] = face_name if attach == "wall" else hd
            elif block == "hopper":
                want["facing"] = "down" if face_name == "up" else _OPPOSITE[face_name]
            elif block in ("ladder", "end_rod", "lightning_rod") or "wall_" in block or "shulker_box" in block \
                    or block.endswith("lightning_rod"):
                want["facing"] = face_name
            elif block.endswith("_stairs") or "fence_gate" in block or (block.endswith("_door") and "trap" not in block) \
                    or block.endswith("_bed"):
                want["facing"] = hd
            elif block.endswith("anvil"):
                want["facing"] = _CLOCKWISE[hd]
            elif "up" in fv and "down" in fv:         # pistons, dispensers, observers, barrels...
                want["facing"] = _OPPOSITE[nearest_looking_direction(yaw, pitch)]
            else:                                     # furnaces, chests, repeaters, carved pumpkins...
                want["facing"] = _OPPOSITE[hd]
            if want.get("facing") not in fv:
                if block in ("ladder",) or "wall_" in block:
                    return None
                want.pop("facing", None)

    placed = []
    if "half" in props and "lower" in props["half"] and "upper" in props["half"]:      # doors, tall plants
        if not air_at(px, py + 1, pz) or py + 1 >= MIN_Y + WORLD_HEIGHT:
            return None
        placed.append((px, py, pz, state_for(block, want), block))
        up = dict(want); up["half"] = "upper"
        placed.append((px, py + 1, pz, state_for(block, up), block))
    elif block.endswith("_bed") and "part" in props:
        ox, oy, oz = _FACE_STEP[hd]
        if not air_at(px + ox, py, pz + oz):
            return None
        want["facing"] = hd
        placed.append((px, py, pz, state_for(block, dict(want, part="foot")), block))
        placed.append((px + ox, py, pz + oz, state_for(block, dict(want, part="head")), block))
    else:
        placed.append((px, py, pz, state_for(block, want), block))
    return placed



TOTAL_BLOCK_STATES = 35711

BIOME_LIST = REGISTRIES["worldgen/biome"]
PLAINS_BIOME_ID = BIOME_LIST.index("plains")
DIMENSION_TYPE_LIST = REGISTRIES["dimension_type"]
OVERWORLD_DIM_TYPE_ID = DIMENSION_TYPE_LIST.index("overworld")

# ---------------------------------------------------------------------------
# World: flat superflat world. Stone (assumed global block state id 1) from
# y=-64 to y=-1, air (id 0) above. Placed blocks are tracked separately and
# override the flat terrain.
# ---------------------------------------------------------------------------

AIR_STATE = 0
STONE_STATE = 1  # best-guess global block state id, see module docstring

MIN_Y = -64
WORLD_HEIGHT = 384
SECTION_COUNT = WORLD_HEIGHT // 16
GROUND_TOP_Y = -1  # topmost solid (stone) layer; y >= 0 is air

placed_blocks = {}  # (x, y, z) -> state id
world_lock = threading.Lock()


def get_block(x, y, z):
    with world_lock:
        if (x, y, z) in placed_blocks:
            return placed_blocks[(x, y, z)]
    if MIN_Y <= y <= GROUND_TOP_Y:
        return STONE_STATE
    return AIR_STATE


def set_block(x, y, z, state):
    with world_lock:
        placed_blocks[(x, y, z)] = state
    invalidate_light(x, z)


# ---------------------------------------------------------------------------
# World persistence: simple JSON save file, autosaved periodically and on
# shutdown, loaded on startup. Only stores the diff from the flat terrain
# (placed/broken blocks), same idea as a real server's region files just
# much simpler.
# ---------------------------------------------------------------------------

SAVE_FILE = os.environ.get("MC_SAVE_FILE", os.path.join(SCRIPT_DIR, "world_save.json"))
SAVE_INTERVAL = int(os.environ.get("MC_SAVE_INTERVAL", "30"))


def load_world():
    if not os.path.exists(SAVE_FILE):
        return
    try:
        with open(SAVE_FILE) as f:
            data = json.load(f)
        # saves record which block-state table they were written with; a
        # save from a different table would show the wrong blocks, so it is
        # set aside rather than loaded
        if data.get("__states__") != TOTAL_BLOCK_STATES:
            stale = SAVE_FILE + ".old"
            os.replace(SAVE_FILE, stale)
            print(f"[world] save was written with a different block table; moved to {stale}, starting a fresh world")
            return
        data = data["blocks"]
        with world_lock:
            for key, state in data.items():
                x, y, z = (int(v) for v in key.split(","))
                placed_blocks[(x, y, z)] = state
        print(f"[world] loaded {len(data)} changed blocks from {SAVE_FILE}")
    except Exception as e:
        print(f"[world] failed to load {SAVE_FILE}: {e}")


def save_world():
    with world_lock:
        data = {"__states__": TOTAL_BLOCK_STATES,
                "blocks": {f"{x},{y},{z}": state for (x, y, z), state in placed_blocks.items()}}
    # write to a temp file then rename, so a crash mid-save can't corrupt
    # the save file (same trick real servers use for region files)
    tmp = SAVE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SAVE_FILE)


# ---------------------------------------------------------------------------
# Player persistence: last known position/rotation per player UUID, in
# players.json next to the world save. Keyed by UUID (not name) like a real
# server's playerdata folder. In offline mode the client picks its own UUID
# from the account name, so the same account gets the same entry each time.
# ---------------------------------------------------------------------------

PLAYER_FILE = os.environ.get("MC_PLAYER_FILE", os.path.join(SCRIPT_DIR, "players.json"))
players = {}  # uuid string -> {"name", "x", "y", "z", "yaw", "pitch"}
players_lock = threading.Lock()


def load_players():
    if not os.path.exists(PLAYER_FILE):
        return
    try:
        with open(PLAYER_FILE) as f:
            data = json.load(f)
        with players_lock:
            players.update(data)
        print(f"[players] loaded {len(data)} saved players from {PLAYER_FILE}")
    except Exception as e:
        print(f"[players] failed to load {PLAYER_FILE}: {e}")


def save_players():
    with players_lock:
        data = json.dumps(players)
    tmp = PLAYER_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, PLAYER_FILE)


def update_player(uuid_key, name, x, y, z, yaw, pitch, inventory=None, held_slot=None):
    with players_lock:
        entry = players.get(uuid_key, {})
        entry.update({"name": name, "x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch})
        if inventory is not None:
            entry["inventory"] = {str(k): v for k, v in inventory.items()}
        if held_slot is not None:
            entry["held_slot"] = held_slot
        entry["items_version"] = len(ITEM_NAMES)
        players[uuid_key] = entry


def get_saved_inventory(uuid_key):
    """Returns (inventory_dict{slot:(name,count)}, held_slot) using only
    entries that reference a name we can actually place/represent."""
    with players_lock:
        p = players.get(uuid_key)
    if not p:
        return {}, 0
    if p.get("inventory") and p.get("items_version") != len(ITEM_NAMES):
        # saved with an older item table: the names would map to the wrong items
        print(f"[players] discarding inventory saved with an older item table for {p.get('name')}")
        return {}, 0
    raw = p.get("inventory", {})
    inv = {}
    for k, v in raw.items():
        try:
            slot = int(k)
            name, count = v[0], int(v[1])
        except (ValueError, TypeError, IndexError):
            continue
        if name not in ITEM_ID_BY_NAME or count <= 0:
            continue
        inv[slot] = (name, count)
    held = p.get("held_slot", 0)
    if not isinstance(held, int) or not (0 <= held <= 8):
        held = 0
    return inv, held


def get_saved_position(uuid_key):
    """Returns (x, y, z, yaw, pitch) or None. Rejects garbage values (NaN,
    inf, or absurdly far/deep) so a corrupt file can't strand a player."""
    with players_lock:
        p = players.get(uuid_key)
    if not p:
        return None
    try:
        x, y, z = float(p["x"]), float(p["y"]), float(p["z"])
        yaw, pitch = float(p.get("yaw", 0.0)), float(p.get("pitch", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    vals = (x, y, z, yaw, pitch)
    if any(v != v or abs(v) > 3e7 for v in vals):  # NaN / huge
        return None
    if y < MIN_Y - 64 or y > MIN_Y + WORLD_HEIGHT + 64:
        return None
    return x, y, z, yaw, pitch


active_sessions = {}  # uuid_key -> (username, pos_state, inventory, held_slot_box)


def autosave_loop():
    while True:
        time.sleep(SAVE_INTERVAL)
        # write each connected player's live state first, so a crash or kill
        # loses at most one interval of inventory/position changes
        for key, (uname, pos, inv, hbox) in list(active_sessions.items()):
            update_player(key, uname, *pos, inventory=inv, held_slot=hbox[0])
        try:
            save_players()
        except Exception as e:
            print(f"[players] autosave failed: {e}")
        try:
            save_world()
            if DEBUG:
                print(f"[world] autosaved ({len(placed_blocks)} changed blocks)")
        except Exception as e:
            print(f"[world] autosave failed: {e}")


def build_section_bytes(block_state_id, biome_id, block_count):
    # LevelChunkSection.write (decompiled): shorts nonEmptyBlockCount,
    # fluidCount, then states.write / biomes.write (PalettedContainer):
    # byte bitsPerEntry, palette.write, then the packed data as a *fixed
    # size* long array with NO length prefix (verified via
    # FriendlyByteBuf.writeFixedSizeLongArray - just writes N longs raw).
    # For bitsPerEntry=0 (SingleValuePalette) that array is zero-length,
    # so nothing follows the palette VarInt at all.
    out = bytearray()
    out += write_short(block_count)
    out += write_short(0)  # fluid count (we have no fluids)
    # block states paletted container: single-valued (bits_per_entry = 0)
    out += write_ubyte(0)
    out += write_varint(block_state_id)
    # biomes paletted container: single-valued
    out += write_ubyte(0)
    out += write_varint(biome_id)
    return bytes(out)


def build_chunk_column(chunk_x, chunk_z):
    """Builds the 'data' byte array for Level Chunk with Light: one section
    per 16 vertical blocks, bottom (y=-64) to top."""
    sections = bytearray()

    # figure out which world-y range each section covers, relative to MIN_Y
    for section_index in range(SECTION_COUNT):
        section_min_y = MIN_Y + section_index * 16
        section_max_y = section_min_y + 15

        overrides = {}
        if placed_blocks:
            with world_lock:
                for (px, py, pz), state in placed_blocks.items():
                    if (px // 16 == chunk_x and pz // 16 == chunk_z
                            and section_min_y <= py <= section_max_y):
                        overrides[(px, py, pz)] = state

        if not overrides:
            # whole section is uniform: either all stone or all air
            if section_max_y <= GROUND_TOP_Y:
                sections += build_section_bytes(STONE_STATE, PLAINS_BIOME_ID, 4096)
            else:
                sections += build_section_bytes(AIR_STATE, PLAINS_BIOME_ID, 0)
        else:
            # A section with player-placed overrides. IMPORTANT: this must
            # NOT use a "direct/global" container (bits > 8) with an
            # arbitrary bit width like we first tried - decompiling
            # PalettedContainer.createOrReuseData showed that for Global
            # configs the reader ignores the transmitted bit count and
            # instead re-sizes storage to Strategy.globalPaletteBitsInMemory
            # (== ceil(log2(total block STATE count)) for this exact jar,
            # a number we can't reliably reproduce without running the
            # game) - any mismatch between that and what we actually wrote
            # corrupts every section after it. A LINEAR palette (bits 1-4)
            # has no such ambiguity: Configuration.Simple.bitsInMemory()
            # always equals exactly the bits we send, confirmed via
            # decompile. Our overrides are always just STONE/AIR anyway, so
            # a small palette is a natural fit, not a workaround.
            entries = []
            count = 0
            for ly in range(16):
                wy = section_min_y + ly
                for lz in range(16):
                    wz = chunk_z * 16 + lz
                    for lx in range(16):
                        wx = chunk_x * 16 + lx
                        state = overrides.get((wx, wy, wz))
                        if state is None:
                            state = STONE_STATE if wy <= GROUND_TOP_Y else AIR_STATE
                        if state != AIR_STATE:
                            count += 1
                        entries.append(state)

            palette = []
            palette_index = {}
            for state in entries:
                if state not in palette_index:
                    palette_index[state] = len(palette)
                    palette.append(state)
            # Palette choice (Strategy.getConfigurationForBitCount, decompiled):
            #   1-4 bits -> linear palette (always 4 bits in memory), up to 16 blocks
            #   5-8 bits -> hashmap palette, bits in memory == bits sent, up to 256 blocks
            #   9+  bits -> global ids, in-memory width = ceillog2(total block states)
            # linear/hashmap use a palette table + packed indices; the global
            # case writes no table and packs the raw state ids themselves.
            n_distinct = len(palette)
            if n_distinct <= 16:
                bits_per_entry = 4
                use_palette = True
            elif n_distinct <= 256:
                bits_per_entry = max(5, (n_distinct - 1).bit_length())
                use_palette = True
            else:
                bits_per_entry = (TOTAL_BLOCK_STATES - 1).bit_length()  # ceillog2
                use_palette = False

            sec = bytearray()
            sec += write_short(count)
            sec += write_short(0)  # fluid count
            sec += write_ubyte(bits_per_entry)
            if use_palette:
                sec += write_varint(len(palette))
                for state in palette:
                    sec += write_varint(state)
            values_per_long = 64 // bits_per_entry
            longs = []
            cur = 0
            cur_count = 0
            for state in entries:
                idx = palette_index[state] if use_palette else state
                cur |= idx << (cur_count * bits_per_entry)
                cur_count += 1
                if cur_count == values_per_long:
                    longs.append(cur)
                    cur = 0
                    cur_count = 0
            if cur_count:
                longs.append(cur)
            for lv in longs:
                sec += write_long(lv if lv < 2**63 else lv - 2**64)
            # biomes: uniform plains (single-valued palette, no trailing data)
            sec += write_ubyte(0)
            sec += write_varint(PLAINS_BIOME_ID)
            sections += sec

    return bytes(sections)


# ---------------------------------------------------------------------------
# Lighting. The client runs its own light updates when a block changes
# (LevelChunk.setBlockState -> LevelLightEngine.checkBlock), but it needs the
# right light to start from; with none, every edited chunk goes dark. So each
# chunk packet carries sky light and block light computed here.
#
# Light sections: 24 world sections plus one below and one above the world
# (index 0 = y -80..-65, index 1 = y -64..-49, ..., index 25 = above the top).
# Flat-world baseline: stone up to y=-1 (no light), open sky above (15). Chunks
# whose neighbourhood has no edits use that baseline directly; otherwise light
# is flooded over the chunk plus 16 blocks each side, using a simple opacity and
# emission model keyed on block names (not exact vanilla shapes).
# ---------------------------------------------------------------------------

LIGHT_SECTIONS = SECTION_COUNT + 2
_FULL = b"\xff" * 2048

_OPACITY_1 = {"water", "lava", "bubble_column", "ice", "frosted_ice", "cobweb", "slime_block", "honey_block"}
_TRANSPARENT_WORDS = (
    "glass", "torch", "lantern", "flower", "sapling", "propagule", "short_grass", "tall_grass", "fern", "seagrass",
    "kelp", "vine", "rail", "button", "pressure_plate", "sign", "banner", "carpet", "fence", "bars", "chain", "door",
    "pane", "candle", "bell", "coral", "sprout", "roots", "azalea", "bush", "wheat", "carrots", "potatoes",
    "beetroots", "melon_stem", "pumpkin_stem", "leaf_litter", "petals", "cake", "head", "skull", "scaffolding",
    "rod", "dripleaf", "lily_pad", "sugar_cane", "bamboo", "cactus", "fire", "potted", "tripwire", "lever",
    "redstone_wire", "ladder", "end_rod", "nether_wart", "mushroom", "wildflowers", "bud", "cluster", "pot",
)
_OPAQUE_EXCEPTIONS = ("command_block", "tinted_glass", "sea_lantern", "coral_block", "mushroom_stem",
                      "mushroom_block", "block_of", "lightning_rod_block")
_EMISSION = {
    "torch": 14, "wall_torch": 14, "soul_torch": 10, "soul_wall_torch": 10, "lantern": 15, "soul_lantern": 10,
    "glowstone": 15, "sea_lantern": 15, "shroomlight": 15, "jack_o_lantern": 15, "lava": 15, "fire": 15,
    "soul_fire": 10, "beacon": 15, "end_rod": 14, "conduit": 15, "ochre_froglight": 15, "verdant_froglight": 15,
    "pearlescent_froglight": 15, "magma_block": 3, "crying_obsidian": 10, "enchanting_table": 7,
    "ender_chest": 7, "nether_portal": 11, "end_portal": 15, "end_gateway": 15, "glow_lichen": 7,
    "sculk_catalyst": 6, "amethyst_cluster": 5, "large_amethyst_bud": 4, "medium_amethyst_bud": 2,
    "small_amethyst_bud": 1,
}
_light_props_cache = {}


def block_name_for_state(state):
    import bisect
    i = bisect.bisect_right(_STATE_STARTS, state) - 1
    return _STATE_RANGES[i][1] if i >= 0 else "air"


def state_light_props(state):
    """(opacity 0-15, emission 0-15) for a block state, from its block name."""
    r = _light_props_cache.get(state)
    if r is not None:
        return r
    name = block_name_for_state(state)
    if name in ("air", "cave_air", "void_air", "barrier", "light", "structure_void"):
        op = 0
    elif any(w in name for w in _OPAQUE_EXCEPTIONS):
        op = 15
    elif name in _OPACITY_1 or "leaves" in name:
        op = 1
    elif name.endswith("_wall") or name == "snow" or any(w in name for w in _TRANSPARENT_WORDS):
        op = 0
    else:
        op = 15
    em = _EMISSION.get(name)
    if em is None:
        em = 15 if "copper_lantern" in name else 0
    r = (op, em)
    _light_props_cache[state] = r
    return r


def _baseline_sky(li):
    """Baseline sky light for light section li: 0 inside the stone floor, 15 above."""
    section_top_y = MIN_Y + (li - 1) * 16 + 15
    return b"" if section_top_y <= GROUND_TOP_Y else _FULL


_light_cache = {}   # (cx, cz) -> packet bytes for the light part of the chunk packet


def invalidate_light(x, z):
    cx, cz = x >> 4, z >> 4
    for dx in (-1, 0, 1):
        for dz in (-1, 0, 1):
            _light_cache.pop((cx + dx, cz + dz), None)


def _bitset_bytes(bits):
    return bits.to_bytes((bits.bit_length() + 7) // 8, "little") if bits else b""


def _light_payload(sky, block):
    """sky/block: dict light_index -> 2048-byte nibble array, or b'' for all-zero sections.
    Wire order (ClientboundLightUpdatePacketData, decompiled): skyYMask, blockYMask,
    emptySkyYMask, emptyBlockYMask (each a BitSet = byte array), then the sky arrays
    and the block arrays (VarInt count, each VarInt length + 2048 bytes)."""
    out = bytearray()
    sky_mask = blk_mask = sky_empty = blk_empty = 0
    sky_arr, blk_arr = [], []
    for li in range(LIGHT_SECTIONS):
        sd = sky[li]
        if sd and any(sd):
            sky_mask |= 1 << li; sky_arr.append(sd)
        else:
            sky_empty |= 1 << li
        bd = block[li]
        if bd and any(bd):
            blk_mask |= 1 << li; blk_arr.append(bd)
        else:
            blk_empty |= 1 << li
    for bits in (sky_mask, blk_mask, sky_empty, blk_empty):
        b = _bitset_bytes(bits)
        out += write_varint(len(b)) + b
    for arrs in (sky_arr, blk_arr):
        out += write_varint(len(arrs))
        for a in arrs:
            out += write_varint(len(a)) + a
    return bytes(out)


_BASELINE_LIGHT = None


def compute_light_cells(cx, cz):
    """Flood sky and block light for the chunk's neighbourhood. Returns
    (get_sky, get_block, lo_li, hi_li, sky_array, block_array, row_index) or None if no edits nearby."""
    x0, z0 = cx * 16 - 16, cz * 16 - 16
    W = 48
    with world_lock:
        edits = [(x, y, z, st) for (x, y, z), st in placed_blocks.items()
                 if x0 <= x < x0 + W and z0 <= z < z0 + W]
    if not edits:
        return None
    # vertical extent: from the lowest edit (or the floor top) up to just above
    # the highest edit; emitters also light up to 15 blocks above themselves
    min_y = min(min(e[1] for e in edits), GROUND_TOP_Y + 1)
    emit_tops = [e[1] + 15 for e in edits if state_light_props(e[3])[1] > 0]
    max_y = max(max(e[1] for e in edits) + 1, max(emit_tops) if emit_tops else 0)
    lo_li = max(0, (min_y - MIN_Y) // 16 + 1)
    hi_li = min(LIGHT_SECTIONS - 1, (max_y - MIN_Y) // 16 + 1)
    ylo = MIN_Y + (lo_li - 1) * 16
    H = (hi_li - lo_li + 1) * 16
    yhi = ylo + H - 1

    N = W * W * H
    op = bytearray(N)         # opacity per cell
    em = bytearray(N)         # emission per cell
    layer = W * W
    for yi in range(H):       # baseline: stone (opaque) up to the floor, air above
        if ylo + yi <= GROUND_TOP_Y:
            op[yi * layer:(yi + 1) * layer] = b"\x0f" * layer

    def idx(x, y, z):
        return ((y - ylo) * W + (z - z0)) * W + (x - x0)

    for x, y, z, st in edits:
        if ylo <= y <= yhi:
            o, e = state_light_props(st)
            i = idx(x, y, z)
            op[i] = o
            em[i] = e

    sky = bytearray(N)
    for zz in range(W):       # sky: straight down from the top, reduced by filters, 0 under opaque
        for xx in range(W):
            level = 15
            base = zz * W + xx
            for yi in range(H - 1, -1, -1):
                i = yi * layer + base
                o = op[i]
                if o >= 15:
                    level = 0
                elif o:
                    level = max(0, level - o)
                sky[i] = level
    blk = bytearray(N)
    for i in range(N):
        if em[i]:
            blk[i] = em[i]

    offs = (1, -1, W, -W, layer, -layer)

    edit_cols = {(x - x0) + (z - z0) * W for x, y, z, st in edits}

    def sky_seeds():
        """Cells that could raise a neighbour: lit neighbours of dark, non-opaque
        cells. Dark non-opaque cells only occur in columns that contain an edit."""
        seeds = set()
        for col in edit_cols:
            for yi in range(H):
                i = yi * layer + col
                if op[i] < 15 and sky[i] < 15:
                    seeds.add(i)
                    xi, zi = col % W, col // W
                    for k, d in enumerate(offs):
                        if (k == 0 and xi == W - 1) or (k == 1 and xi == 0) or (k == 2 and zi == W - 1) \
                                or (k == 3 and zi == 0) or (k == 4 and yi == H - 1) or (k == 5 and yi == 0):
                            continue
                        if sky[i + d] > 1:
                            seeds.add(i + d)
        return list(seeds)

    def flood(light, is_sky):
        queue = sky_seeds() if is_sky else [i for i in range(N) if light[i] > 1]
        qi = 0
        while qi < len(queue):
            i = queue[qi]
            qi += 1
            L = light[i]
            if L <= 1:
                continue
            yi, rem = divmod(i, layer)
            zi, xi = divmod(rem, W)
            for k, d in enumerate(offs):
                if k == 0 and xi == W - 1: continue
                if k == 1 and xi == 0: continue
                if k == 2 and zi == W - 1: continue
                if k == 3 and zi == 0: continue
                if k == 4 and yi == H - 1: continue
                if k == 5 and yi == 0: continue
                j = i + d
                o = op[j]
                if o >= 15:
                    continue
                if is_sky and k == 5 and L == 15 and o == 0:
                    nl = 15                    # sky light goes straight down without loss
                else:
                    nl = L - (o if o > 1 else 1)
                if nl > light[j]:
                    light[j] = nl
                    queue.append(j)

    flood(sky, True)
    flood(blk, False)

    def get_sky(x, y, z):
        if y > yhi: return 15
        if y < ylo: return 0
        return sky[idx(x, y, z)]

    def get_block(x, y, z):
        if y > yhi or y < ylo: return 0
        return blk[idx(x, y, z)]

    def row_index(y, z, x):
        return ((y - ylo) * W + (z - z0)) * W + (x - x0)

    return get_sky, get_block, lo_li, hi_li, sky, blk, row_index


def build_light_payload(cx, cz):
    global _BASELINE_LIGHT
    cached = _light_cache.get((cx, cz))
    if cached is not None:
        return cached
    cells = compute_light_cells(cx, cz)
    if cells is None:
        if _BASELINE_LIGHT is None:
            sky = {li: _baseline_sky(li) for li in range(LIGHT_SECTIONS)}
            block = {li: b"" for li in range(LIGHT_SECTIONS)}
            _BASELINE_LIGHT = _light_payload(sky, block)
        payload = _BASELINE_LIGHT
    else:
        lo_li, hi_li, row_index = cells[2], cells[3], cells[6]
        sky, block = {}, {}
        for li in range(LIGHT_SECTIONS):
            if li < lo_li or li > hi_li:
                sky[li] = _baseline_sky(li) if li < lo_li else _FULL
                block[li] = b""
                continue
            sy0 = MIN_Y + (li - 1) * 16
            sa, ba = bytearray(2048), bytearray(2048)
            sky_arr, blk_arr = cells[4], cells[5]
            for ly in range(16):
                for lz in range(16):
                    base = row_index(sy0 + ly, cz * 16 + lz, cx * 16)
                    srow, brow = sky_arr[base:base + 16], blk_arr[base:base + 16]
                    o = (ly * 16 + lz) * 8
                    for k in range(8):
                        sa[o + k] = srow[2 * k] | (srow[2 * k + 1] << 4)
                        ba[o + k] = brow[2 * k] | (brow[2 * k + 1] << 4)
            sky[li], block[li] = bytes(sa), bytes(ba)
        payload = _light_payload(sky, block)
    _light_cache[(cx, cz)] = payload
    return payload


def send_chunk(conn, chunk_x, chunk_z):
    data = build_chunk_column(chunk_x, chunk_z)
    payload = bytearray()
    payload += write_int(chunk_x)
    payload += write_int(chunk_z)
    payload += write_varint(0)  # heightmaps: empty Map<Heightmap.Types,long[]> (VarInt count, not NBT)
    payload += write_varint(len(data))
    payload += data
    payload += write_varint(0)  # block entity count
    payload += build_light_payload(chunk_x, chunk_z)  # sky + block light, see the lighting section
    conn.send_packet(Play.LEVEL_CHUNK_WITH_LIGHT, bytes(payload))


# ---------------------------------------------------------------------------
# Per-player handling
# ---------------------------------------------------------------------------

def log(addr, msg):
    print(f"[{addr}] {msg}")


def send_registry_data(conn, registry_name, entries):
    payload = bytearray()
    payload += write_string("minecraft:" + registry_name)
    payload += write_varint(len(entries))
    for entry_id in entries:
        payload += write_string("minecraft:" + entry_id)
        payload += write_bool(False)  # no data: client uses its own local copy
    conn.send_packet(Config.REGISTRY_DATA, bytes(payload))


def send_update_tags(conn):
    """ClientboundUpdateTagsPacket (decompiled): Map<registry, NetworkPayload>,
    NetworkPayload = Map<tag name, IntList>. Without real data here, the
    client can't resolve tag references (e.g. #minecraft:infiniburn_overworld)
    inside the vanilla registry data it loads locally, and registry loading
    fails outright - this is NOT optional the way it first looked. IDs were
    computed from the jar's actual block/item/entity_type registration order
    (extracted via javap) and, for our own synced registries, their position
    in registries.json. See tags.json."""
    payload = bytearray()
    payload += write_varint(len(TAGS))
    for registry_key, tag_map in TAGS.items():
        payload += write_string(registry_key)
        payload += write_varint(len(tag_map))
        for tag_name, ids in tag_map.items():
            payload += write_string("minecraft:" + tag_name)
            payload += write_varint(len(ids))
            for i in ids:
                payload += write_varint(i)
    conn.send_packet(Config.UPDATE_TAGS, bytes(payload))


def handle_configuration(conn, addr):
    # optional brand plugin message from server (harmless if ignored)
    conn.send_packet(Config.CUSTOM_PAYLOAD,
                      write_string("minecraft:brand") + write_string("pybasic"))

    # feature flags
    conn.send_packet(Config.UPDATE_ENABLED_FEATURES,
                      write_varint(1) + write_string("minecraft:vanilla"))

    # known packs: declare we know the client's own core pack so entries in
    # Registry Data below can be sent with has_data=False
    payload = write_varint(1) + write_string("minecraft") + write_string("core") + write_string(KNOWN_PACK_VERSION)
    conn.send_packet(Config.SELECT_KNOWN_PACKS, payload)

    # wait for client's known packs response before sending registries
    while True:
        pid, r = conn.read_packet()
        if pid == Config.S_SELECT_KNOWN_PACKS:
            break
        elif pid == Config.S_CLIENT_INFORMATION:
            continue
        elif pid == Config.S_CUSTOM_PAYLOAD:
            continue
        else:
            if DEBUG:
                log(addr, f"config: ignoring packet 0x{pid:02X} while waiting for known packs")

    for registry_name, entries in REGISTRIES.items():
        send_registry_data(conn, registry_name, entries)

    send_update_tags(conn)

    conn.send_packet(Config.FINISH_CONFIGURATION, b"")

    # wait for acknowledge finish configuration
    while True:
        pid, r = conn.read_packet()
        if pid == Config.S_FINISH_CONFIGURATION:
            return
        elif DEBUG:
            log(addr, f"config: ignoring packet 0x{pid:02X} while waiting for finish ack")


VIEW_RADIUS = 4


def chunks_in_radius(cx, cz, radius):
    for dx in range(-radius, radius + 1):
        for dz in range(-radius, radius + 1):
            yield (cx + dx, cz + dz)


def send_chunks_batch(conn, coords):
    """Wraps chunk sends in the required Chunk Batch Start/Finished pair
    (see the module docstring's note on ClientboundChunkBatchStartPacket) -
    used both for the initial join and for streaming new chunks in as the
    player walks."""
    coords = list(coords)
    if not coords:
        return
    conn.send_packet(Play.CHUNK_BATCH_START, b"")
    for (cx, cz) in coords:
        send_chunk(conn, cx, cz)
    conn.send_packet(Play.CHUNK_BATCH_FINISHED, write_varint(len(coords)))


def unload_chunks(conn, coords):
    # ClientboundForgetLevelChunkPacket (decompiled): a single ChunkPos,
    # packed as (x & 0xFFFFFFFF) | ((z & 0xFFFFFFFF) << 32) in one long
    for (cx, cz) in coords:
        conn.send_packet(Play.FORGET_LEVEL_CHUNK, encode_chunk_pos(cx, cz))


_teleport_id_counter = [1]


def send_teleport(conn, x, y, z, yaw=0.0, pitch=0.0):
    """ClientboundPlayerPositionPacket (decompiled, see send_join/handle_play
    for field order notes): id:VarInt, PositionMoveRotation, relatives:Int.
    Used both for the initial spawn placement and for /spawn."""
    _teleport_id_counter[0] += 1
    teleport_id = _teleport_id_counter[0]
    payload = bytearray()
    payload += write_varint(teleport_id)
    payload += write_double(x)
    payload += write_double(y)
    payload += write_double(z)
    payload += write_double(0.0)
    payload += write_double(0.0)
    payload += write_double(0.0)
    payload += write_float(yaw)
    payload += write_float(pitch)
    payload += write_int(0)
    conn.send_packet(Play.PLAYER_POSITION, bytes(payload))
    return teleport_id



def send_commands(conn):
    """ClientboundCommandsPacket (decompiled): VarInt entry count, entries,
    VarInt root index. Entry = byte flags, VarInt-array children, [VarInt
    redirect if flags&8], [stub: literal = string name]. Declares one
    executable literal command, /spawn, so the client recognizes it (and
    offers tab-completion) instead of treating it as unknown."""
    payload = bytearray()
    payload += write_varint(2)
    # entry 0: root node, one child (entry 1)
    payload += write_ubyte(0) + write_varint(1) + write_varint(1)
    # entry 1: literal "spawn", executable (0x04) | literal type (0x01), no children
    payload += write_ubyte(0x05) + write_varint(0) + write_string("spawn")
    payload += write_varint(0)  # root index
    conn.send_packet(Play.COMMANDS, bytes(payload))


def send_join_game(conn, entity_id):
    payload = bytearray()
    payload += write_int(entity_id)
    payload += write_bool(False)  # is hardcore
    payload += write_varint(1)
    payload += write_string("minecraft:overworld")  # dimension names
    payload += write_varint(20)  # max players (legacy, unused by client)
    payload += write_varint(8)   # view distance
    payload += write_varint(8)   # simulation distance
    payload += write_bool(False)  # reduced debug info
    payload += write_bool(True)   # enable respawn screen
    payload += write_bool(False)  # do limited crafting
    # CommonPlayerSpawnInfo (decompiled from the jar):
    payload += write_varint(OVERWORLD_DIM_TYPE_ID)  # dimension type: Holder<T> registry ref = VarInt index
    payload += write_string("minecraft:overworld")  # dimension name (ResourceKey = string)
    payload += write_long(0)      # hashed seed
    payload += write_varint(1)    # game type: VarInt (GameType.STREAM_CODEC is idMapper/VarInt) - 1=creative
    payload += write_varint(0)    # previous game type: Optional<GameType> as VarInt, 0 = absent
    payload += write_bool(False)  # is debug
    payload += write_bool(True)   # is flat
    payload += write_bool(False)  # has death location
    payload += write_varint(0)    # portal cooldown
    payload += write_varint(63)   # sea level
    # end CommonPlayerSpawnInfo
    payload += write_bool(False)  # online mode (new field in 26.3)
    payload += write_bool(False)  # enforces secure chat
    conn.send_packet(Play.LOGIN, bytes(payload))


def send_player_abilities(conn):
    flags = 0x01 | 0x04 | 0x08  # invulnerable, allow flying, creative/instant-break
    payload = write_byte(flags) + write_float(0.05) + write_float(0.1)
    conn.send_packet(Play.PLAYER_ABILITIES, payload)


SPAWN_X, SPAWN_Y, SPAWN_Z = 8, 1, 8


def keepalive_thread(conn, addr, state):
    while state["alive"]:
        time.sleep(10)
        try:
            kid = random.randint(0, 2**63 - 1)
            conn.send_packet(Play.KEEP_ALIVE, write_long(kid))
        except Exception:
            state["alive"] = False
            return


def handle_play(conn, addr, entity_id, uuid_key, username):
    send_join_game(conn, entity_id)
    send_player_abilities(conn)

    # Game Event 13 = start waiting for level chunks (required for client to spawn)
    conn.send_packet(Play.GAME_EVENT, write_ubyte(13) + write_float(0.0))

    # RespawnData (decompiled): GlobalPos{dimension string, BlockPos} + yaw float + pitch float
    conn.send_packet(Play.SET_DEFAULT_SPAWN_POSITION,
                      write_string("minecraft:overworld") +
                      encode_block_pos(SPAWN_X, SPAWN_Y, SPAWN_Z) +
                      write_float(0.0) + write_float(0.0))

    # where to put the player: their saved position if they've been here
    # before, otherwise the world spawn
    saved = get_saved_position(uuid_key)
    if saved:
        px, py, pz, pyaw, ppitch = saved
    else:
        px, py, pz, pyaw, ppitch = SPAWN_X + 0.5, SPAWN_Y, SPAWN_Z + 0.5, 0.0, 0.0
    start_cx, start_cz = int(px // 16), int(pz // 16)
    pos_state = [px, py, pz, pyaw, ppitch]  # updated from movement packets

    conn.send_packet(Play.SET_CHUNK_CACHE_CENTER,
                      write_varint(start_cx) + write_varint(start_cz))

    # send a small area of chunks around spawn
    # ClientboundChunkBatchStartPacket/Finished (decompiled): without
    # these, the client's chunk-batch state machine never considers the
    # chunks "committed" and only meshes/renders whatever batch it was
    # already expecting (in practice: just the first chunk or two) -
    # everything else stays present as world data (you can still interact
    # with it) but never gets a mesh built, so it doesn't render.
    sent_chunks = set(chunks_in_radius(start_cx, start_cz, VIEW_RADIUS))
    send_chunks_batch(conn, sent_chunks)

    send_commands(conn)

    # put the player at their saved position (or spawn)
    # Until the client confirms this teleport (ServerboundAcceptTeleportation),
    # any movement packets it sends are for its OLD position (it starts at
    # 0,0,0) and must be ignored - otherwise we'd overwrite the restored
    # position and stream the wrong chunks. Real servers do the same.
    pending_teleport = [send_teleport(conn, px, py, pz, pyaw, ppitch)]

    state = {"alive": True}
    t = threading.Thread(target=keepalive_thread, args=(conn, addr, state), daemon=True)
    t.start()

    last_chunk = [(start_cx, start_cz)]  # mutable box so the packet-handling loop can update it

    # restore inventory: container slots 36-44 are the hotbar, 9-35 the
    # main inventory (standard player-inventory numbering, used by both the
    # inventory screen and creative-mode slot clicks)
    inventory, held_slot = get_saved_inventory(uuid_key)
    for slot, (item_name, count) in inventory.items():
        conn.send_packet(Play.CONTAINER_SET_SLOT,
                          write_varint(0) + write_varint(0) + write_short(slot) +
                          write_item_stack(item_name, count))
    if held_slot:
        conn.send_packet(Play.SET_HELD_SLOT, write_varint(held_slot))
    held_slot_box = [held_slot]
    active_sessions[uuid_key] = (username, pos_state, inventory, held_slot_box)

    log(addr, f"player entered the world at {px:.1f},{py:.1f},{pz:.1f}"
                + (" (restored)" if saved else " (spawn)")
                + (f", {len(inventory)} inventory slots restored" if inventory else ""))

    try:
        while True:
            pid, r = conn.read_packet()

            if pid == Play.S_KEEP_ALIVE:
                pass

            elif pid == Play.S_ACCEPT_TELEPORTATION:
                try:
                    if r.read_varint() == pending_teleport[0]:
                        pending_teleport[0] = None
                except Exception:
                    pass

            elif pid == Play.S_USE_ITEM_ON:
                try:
                    hand = r.read_varint()
                    block_val = r.read_long()
                    face = r.read_varint()
                    cursor_x = r.read_float()
                    cursor_y = r.read_float()
                    cursor_z = r.read_float()
                    inside_block = r.read_bool()
                    world_border_hit = r.read_bool()
                    sequence = r.read_varint() if r.remaining() > 0 else 0
                except Exception as e:
                    if DEBUG:
                        log(addr, f"failed to parse Use Item On: {e}")
                    continue

                bx, by, bz = decode_block_pos(block_val)
                dx, dy, dz = {
                    0: (0, -1, 0), 1: (0, 1, 0), 2: (0, 0, -1),
                    3: (0, 0, 1), 4: (-1, 0, 0), 5: (1, 0, 0),
                }.get(face, (0, 1, 0))
                px, py, pz = bx + dx, by + dy, bz + dz

                # only place when the held item is one that has a block form
                # (see ITEM_TO_BLOCK_STATE); an empty hand or a tool/food/etc
                # does nothing. The ack below is still sent so the client
                # doesn't wait on a prediction.
                held = inventory.get(held_slot_box[0] + 36)
                plan = None
                if held and held[0] in ITEM_TO_BLOCK_NAME:
                    plan = build_placement(held[0], bx, by, bz, face, cursor_y,
                                           pos_state[3], pos_state[4],
                                           lambda x, y, z: get_block(x, y, z) == AIR_STATE)
                if plan and get_block(px, py, pz) == AIR_STATE:
                    for (x, y, z, st, blk) in plan:
                        set_block(x, y, z, st)
                        conn.send_packet(Play.BLOCK_UPDATE, encode_block_pos(x, y, z) + write_varint(st))
                    if DEBUG:
                        x, y, z, st, blk = plan[0]
                        log(addr, f"placed {blk} at {x},{y},{z} {decode_state(blk, st) or ''} (held={held[0]})")
                elif DEBUG and not plan:
                    log(addr, f"right-click with {held[0] if held else 'empty hand'}: nothing placed")

                conn.send_packet(Play.BLOCK_CHANGED_ACK, write_varint(sequence))

            elif pid == Play.S_PLAYER_ACTION:
                # ServerboundPlayerActionPacket (decompiled): action:VarInt
                # (enum ordinal), pos:BlockPos, direction:UByte, sequence:VarInt
                try:
                    action = r.read_varint()
                    block_val = r.read_long()
                    direction = r.read_ubyte()
                    sequence = r.read_varint()
                except Exception as e:
                    if DEBUG:
                        log(addr, f"failed to parse Player Action: {e}")
                    continue

                bx, by, bz = decode_block_pos(block_val)
                # inventory-changing actions (ServerboundPlayerActionPacket.Action):
                # 4 = DROP_ALL_ITEMS, 5 = DROP_ITEM, 7 = SWAP_ITEM_WITH_OFFHAND
                hot = 36 + held_slot_box[0]
                if action == 5:
                    cur = inventory.get(hot)
                    if cur:
                        if cur[1] > 1:
                            inventory[hot] = (cur[0], cur[1] - 1)
                        else:
                            inventory.pop(hot, None)
                        if DEBUG:
                            log(addr, f"dropped 1 {cur[0]} (hotbar slot {held_slot_box[0]})")
                    continue
                if action == 4:
                    cur = inventory.pop(hot, None)
                    if DEBUG and cur:
                        log(addr, f"dropped stack of {cur[1]} {cur[0]} (hotbar slot {held_slot_box[0]})")
                    continue
                if action == 7:
                    main, off = inventory.get(hot), inventory.get(45)
                    for slot, val in ((hot, off), (45, main)):
                        if val: inventory[slot] = val
                        else: inventory.pop(slot, None)
                    continue
                # 0 = START_DESTROY_BLOCK: creative mode breaks instantly
                if action == 0:
                    set_block(bx, by, bz, AIR_STATE)
                    conn.send_packet(Play.BLOCK_UPDATE,
                                      encode_block_pos(bx, by, bz) + write_varint(AIR_STATE))
                    if DEBUG:
                        log(addr, f"broke block at {bx},{by},{bz}")

                conn.send_packet(Play.BLOCK_CHANGED_ACK, write_varint(sequence))

            elif pid in (Play.S_MOVE_PLAYER_POS, Play.S_MOVE_PLAYER_POS_ROT):
                # ServerboundMovePlayerPacket.Pos/PosRot (decompiled): both
                # start with 3 doubles (x, y, z); PosRot has 2 extra floats
                # after that we don't need. Used only to stream in new
                # chunks as the player walks past the initially-sent area -
                # we don't track position for anything else.
                if pending_teleport[0] is not None:
                    continue  # stale packet from before the teleport was confirmed
                try:
                    x = r.read_double()
                    y = r.read_double()
                    z = r.read_double()
                    if pid == Play.S_MOVE_PLAYER_POS_ROT:
                        pos_state[3] = r.read_float()  # yRot (yaw)
                        pos_state[4] = r.read_float()  # xRot (pitch)
                except Exception:
                    continue
                pos_state[0], pos_state[1], pos_state[2] = x, y, z
                cx, cz = int(x // 16), int(z // 16)
                if (cx, cz) != last_chunk[0]:
                    last_chunk[0] = (cx, cz)
                    conn.send_packet(Play.SET_CHUNK_CACHE_CENTER,
                                      write_varint(cx) + write_varint(cz))
                    keep = set(chunks_in_radius(cx, cz, VIEW_RADIUS))
                    needed = [c for c in keep if c not in sent_chunks]
                    stale = [c for c in sent_chunks if c not in keep]
                    if needed:
                        send_chunks_batch(conn, needed)
                        sent_chunks.update(needed)
                    if stale:
                        unload_chunks(conn, stale)
                        sent_chunks.difference_update(stale)
                    if DEBUG and (needed or stale):
                        log(addr, f"streamed {len(needed)} new / unloaded {len(stale)} "
                                   f"chunks (now centered on {cx},{cz})")

            elif pid == Play.S_CHAT_COMMAND:
                try:
                    command = r.read_string()
                except Exception:
                    continue
                if DEBUG:
                    log(addr, f"chat command: /{command}")
                if command == "spawn" or command.startswith("spawn "):
                    pending_teleport[0] = send_teleport(conn, SPAWN_X + 0.5, SPAWN_Y, SPAWN_Z + 0.5)
                    pos_state[:] = [SPAWN_X + 0.5, SPAWN_Y, SPAWN_Z + 0.5, 0.0, 0.0]
                    if DEBUG:
                        log(addr, "teleported to spawn via /spawn")

            elif pid == Play.S_MOVE_PLAYER_ROT:
                if pending_teleport[0] is not None:
                    continue
                try:
                    pos_state[3] = r.read_float()
                    pos_state[4] = r.read_float()
                except Exception:
                    pass

            elif pid == Play.S_SET_CARRIED_ITEM:
                try:
                    slot = r.read_short()
                    if 0 <= slot <= 8:
                        held_slot_box[0] = slot
                except Exception:
                    pass

            elif pid == Play.S_CONTAINER_CLICK:
                # ServerboundContainerClickPacket (decompiled): containerId,
                # VarInt stateId, short slot, byte button, VarInt click type,
                # map<short, HashedStack> changedSlots (count + entries),
                # HashedStack carried. changedSlots is the client's resulting
                # contents of every slot the click touched, so we simply adopt
                # it (this is how moves/swaps/shift-clicks in the inventory
                # screen reach the server).
                try:
                    container_id = r.read_varint()
                    r.read_varint(); r.read_short(); r.read_byte(); r.read_varint()
                    changed = []
                    for _ in range(r.read_varint()):
                        sl = r.read_short()
                        changed.append((sl, read_hashed_stack(r)))
                except Exception as e:
                    if DEBUG:
                        log(addr, f"failed to parse Container Click: {e}")
                    continue
                if container_id == 0:
                    for sl, stack in changed:
                        if not (0 <= sl <= 45):
                            continue
                        if stack is None:
                            inventory.pop(sl, None)
                        else:
                            inventory[sl] = stack

            elif pid == Play.S_PICK_ITEM_FROM_BLOCK:
                # middle-click: ServerboundPickItemFromBlockPacket = BlockPos
                # + includeData bool. Creative: put the block's item in the
                # hotbar (select it if already there, else use the selected
                # slot if empty / first empty slot / replace the selected one).
                try:
                    px_, py_, pz_ = decode_block_pos(r.read_long())
                except Exception:
                    continue
                item_name = item_for_state(get_block(px_, py_, pz_))
                if not item_name or item_name == "air":
                    continue
                hot_slots = list(range(36, 45))
                existing = next((sl for sl in hot_slots if inventory.get(sl, (None,))[0] == item_name), None)
                if existing is not None:
                    target = existing
                else:
                    cur = 36 + held_slot_box[0]
                    target = cur if cur not in inventory else next((sl for sl in hot_slots if sl not in inventory), cur)
                    inventory[target] = (item_name, 1)
                    conn.send_packet(Play.CONTAINER_SET_SLOT,
                                      write_varint(0) + write_varint(0) + write_short(target) +
                                      write_item_stack(item_name, 1))
                if target - 36 != held_slot_box[0]:
                    held_slot_box[0] = target - 36
                    conn.send_packet(Play.SET_HELD_SLOT, write_varint(held_slot_box[0]))
                if DEBUG:
                    log(addr, f"picked {item_name} into hotbar slot {target - 36}")

            elif pid == Play.S_SET_CREATIVE_MODE_SLOT:
                # ServerboundSetCreativeModeSlotPacket (decompiled): SHORT
                # slotNum, then an ItemStack. We track all slots the client
                # sends (standard player-inventory numbering: 9-35 main,
                # 36-44 hotbar) so inventory persists across sessions.
                try:
                    slot_num = r.read_short()
                    stack = read_item_stack(r)
                except Exception:
                    stack = 'skip'  # has components we don't parse - leave slot untouched
                if stack != 'skip' and 0 <= slot_num <= 45:
                    # (slot -1 is the creative screen's "drop this item" - it
                    # never lands in the inventory, so there is nothing to track)
                    if stack is None:
                        inventory.pop(slot_num, None)
                    else:
                        inventory[slot_num] = stack

            elif pid in (Play.S_MOVE_PLAYER_STATUS_ONLY,
                         Play.S_PLAYER_LOADED, Play.S_CLIENT_TICK_END,
                         Play.S_CHUNK_BATCH_RECEIVED,
                         Play.S_USE_ITEM, Play.S_CUSTOM_PAYLOAD,
                         Play.S_CLIENT_INFORMATION, Play.S_PLAYER_INPUT,
                         Play.S_PUNCH,
                         Play.S_CONTAINER_CLOSE, Play.S_PLAYER_ABILITIES,
                         Play.S_PLAYER_COMMAND):
                # all harmless/expected packets we don't need to act on:
                # rotation-only movement, held-item/inventory clicks,
                # brand/client-info, movement-input flags, arm-swing
                # "punch", pick-block, closing the inventory screen,
                # flight/sprint/sneak toggles
                pass

            else:
                if DEBUG:
                    log(addr, f"play: ignoring packet 0x{pid:02X} ({r.remaining()} bytes)")

    except (ConnectionError, OSError):
        pass
    finally:
        state["alive"] = False
        active_sessions.pop(uuid_key, None)
        update_player(uuid_key, username, *pos_state,
                      inventory=inventory, held_slot=held_slot_box[0])
        try:
            save_players()
        except Exception as e:
            log(addr, f"failed to save player data: {e}")
        log(addr, f"player disconnected (saved at {pos_state[0]:.1f},{pos_state[1]:.1f},{pos_state[2]:.1f})")


# ---------------------------------------------------------------------------
# Status (server list ping) and Login
# ---------------------------------------------------------------------------

_entity_id_counter = 1000
_entity_id_lock = threading.Lock()


def next_entity_id():
    global _entity_id_counter
    with _entity_id_lock:
        _entity_id_counter += 1
        return _entity_id_counter


def handle_client(sock, addr):
    conn = Connection(sock, addr)
    try:
        pid, r = conn.read_packet()  # handshake
        if pid != 0x00:
            return
        protocol = r.read_varint()
        server_address = r.read_string()
        server_port = r.read_ushort()
        next_state = r.read_varint()

        if DEBUG:
            log(addr, f"handshake: protocol={protocol} next_state={next_state}")

        if next_state == 1:
            # status
            pid, r = conn.read_packet()  # status request
            status = {
                "version": {"name": GAME_VERSION_NAME, "protocol": PROTOCOL_VERSION},
                "players": {"max": 20, "online": 0, "sample": []},
                "description": {"text": "Basic Python MC Server (flat/creative)"},
            }
            conn.send_packet(0x00, write_string(json.dumps(status)))
            pid, r = conn.read_packet()  # ping request
            payload = r.read_bytes(8)
            conn.send_packet(0x01, payload)
            return

        if next_state != 2:
            return

        # ---- LOGIN ----
        pid, r = conn.read_packet()
        if pid != Login.S_HELLO:
            return
        username = r.read_string()
        uuid_msb, uuid_lsb = r.read_uuid()

        if DEBUG:
            log(addr, f"login start: {username}")

        # offline mode: no encryption, no compression - straight to success
        # NOTE: 26.3 added a `sessionId` UUID field after the GameProfile
        # (confirmed by decompiling ClientboundLoginFinishedPacket in the jar)
        session_id = random.getrandbits(64), random.getrandbits(64)
        conn.send_packet(Login.LOGIN_FINISHED,
                          write_uuid(uuid_msb, uuid_lsb) + write_string(username) + write_varint(0) +
                          write_uuid(session_id[0], session_id[1]))

        pid, r = conn.read_packet()
        if pid != Login.S_LOGIN_ACKNOWLEDGED:
            if DEBUG:
                log(addr, f"expected Login Acknowledged, got 0x{pid:02X}")
            return

        # ---- CONFIGURATION ----
        handle_configuration(conn, addr)

        # ---- PLAY ----
        entity_id = next_entity_id()
        uuid_key = f"{uuid_msb & 0xFFFFFFFFFFFFFFFF:016x}{uuid_lsb & 0xFFFFFFFFFFFFFFFF:016x}"
        handle_play(conn, addr, entity_id, uuid_key, username)

    except (ConnectionError, OSError) as e:
        if DEBUG:
            log(addr, f"connection error: {e}")
    except Exception as e:
        if DEBUG:
            import traceback
            log(addr, f"unexpected error: {e}")
            traceback.print_exc()
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main():
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    load_world()
    load_players()
    t = threading.Thread(target=autosave_loop, daemon=True)
    t.start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(20)
    print(f"Basic MC 26.3 (protocol {PROTOCOL_VERSION}) server listening on {HOST}:{PORT}")
    print("DEBUG =", DEBUG, "(set MC_DEBUG=0 to quiet it down)")
    print(f"world save file: {SAVE_FILE} (autosave every {SAVE_INTERVAL}s)")
    try:
        while True:
            sock, addr = srv.accept()
            t = threading.Thread(target=handle_client, args=(sock, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("shutting down")
    finally:
        print("saving world...")
        try:
            save_players()
        except Exception as e:
            print(f"failed to save players on shutdown: {e}")
        try:
            save_world()
            print(f"saved {len(placed_blocks)} changed blocks to {SAVE_FILE}")
        except Exception as e:
            print(f"failed to save on shutdown: {e}")
        srv.close()


if __name__ == "__main__":
    main()
