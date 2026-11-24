"""Basic entity support: spawn animals near the player, random-walk AI,
and despawn on disconnect.

Entity types are sent as VarInt registry IDs. The IDs were extracted from
the jar's ``EntityTypes`` class registration order (alphabetical by field
name). Common ones used here:

  CHICKEN = 26     COW = 30      PIG = 101
  SHEEP = 114      CAT = 21      HORSE = 67

Packet formats (decompiled from the jar):

  ClientboundAddEntityPacket (0x01):
    VarInt entity_id, UUID uuid, VarInt entity_type, Double x/y/z,
    Byte[1] movement (LpVec3: 0x00 = Vec3.ZERO),
    Byte xRot, Byte yRot, Byte yHeadRot, VarInt data

  ClientboundTeleportEntityPacket (0x80):
    VarInt entity_id,
    PositionMoveRotation: Double x/y/z + Double dx/dy/dz + Float yaw + Float pitch,
    VarInt relatives (0 = all absolute),
    Boolean onGround

  ClientboundRemoveEntitiesPacket (0x4E):
    VarInt count, VarInt[count] entity_ids

AI: every 2-4 seconds, each entity picks a random yaw and walks 1-3
blocks in that direction. No gravity, no collision, no pathfinding —
entities can walk through blocks. The movement is sent via TeleportEntity
(absolute position, simpler than delta-based MoveEntity).

Known limits (basic scope):
  - No entity metadata (ClientboundSetEntityData). The client renders
    with default metadata — some entities might look odd (e.g., sheep
    always white, no name tags).
  - No collision detection — entities walk through blocks and each other.
  - No gravity — entities float in the air (don't fall to the ground).
  - No damage / health — entities can't be killed (no hit packets).
  - No persistence — entities despawn when the player disconnects and
    respawn (at new random positions) on rejoin.
  - No entity-entity interaction (breeding, following, fleeing).
"""

import math
import random
import struct
import threading
import time
import uuid as _uuid

from .codec import write_bool, write_byte, write_double, write_float, write_int, write_long, write_ubyte, write_varint, write_uuid
from .protocol import Play

# Entity type registry IDs (from EntityTypes class, alphabetical registration order)
ENTITY_TYPES = {
    "chicken": 26,
    "cow": 30,
    "pig": 101,
    "sheep": 114,
    "cat": 21,
    "horse": 67,
    "bat": 10,
    "bee": 11,
    "creeper": 32,
}

# Entity id allocator
_entity_id_counter = 10000
_entity_id_lock = threading.Lock()


def next_entity_id():
    global _entity_id_counter
    with _entity_id_lock:
        _entity_id_counter += 1
        return _entity_id_counter


# Gravity in blocks/sec². Vanilla uses -0.08 blocks/tick² at 20 ticks/sec,
# which is -0.08 * 20 * 20 = -32 blocks/sec² in per-second units.
_GRAVITY = -32.0

# Tick rate for the entity AI loop (seconds per tick). 0.05 = 20 ticks/sec,
# matching vanilla. The client interpolates position updates at this rate
# so movement looks smooth.
_TICK_DT = 0.05


class Entity:
    """A simple entity with position, rotation, gravity, and random-walk AI."""

    def __init__(self, entity_type, x, y, z, yaw=0.0, pitch=0.0):
        self.id = next_entity_id()
        self.uuid = (random.getrandbits(64), random.getrandbits(64))
        self.type = entity_type  # registry id (int)
        self.x = x
        self.y = y
        self.z = z
        self.yaw = yaw
        self.pitch = pitch
        self.alive = True
        # Physics state
        self._vel_y = 0.0  # vertical velocity (blocks/sec)
        self._on_ground = False
        # AI state
        self._next_move_time = time.time() + random.uniform(1, 4)
        self._target_x = x
        self._target_z = z
        self._move_speed = 0.0  # blocks per second

    def _block_below_is_solid(self, get_block_fn, air_state):
        """True if the block just below the entity's feet is solid."""
        below_y = math.floor(self.y - 0.01)
        state = get_block_fn(math.floor(self.x), below_y, math.floor(self.z))
        return state != air_state

    def tick(self, dt, get_block_fn, air_state=0):
        """Advance the entity's physics + AI by dt seconds. Returns True if
        the entity moved (and the caller should send a teleport packet)."""
        if not self.alive:
            return False

        moved = False
        now = time.time()

        # ---- Gravity: fall until standing on a solid block ----
        if self._on_ground:
            # Check if we're still on solid ground (block could have been removed)
            if not self._block_below_is_solid(get_block_fn, air_state):
                self._on_ground = False
                self._vel_y = 0.0

        if not self._on_ground:
            self._vel_y += _GRAVITY * dt
            new_y = self.y + self._vel_y * dt
            # Check if we'd land on a block during this step
            old_below = math.floor(self.y - 0.01)
            new_below = math.floor(new_y - 0.01)
            if new_below < old_below:
                # We crossed a block boundary — check if the new block below is solid
                state = get_block_fn(math.floor(self.x), new_below, math.floor(self.z))
                if state != air_state:
                    # Landed — rest on top of this block
                    self.y = float(new_below + 1)
                    self._vel_y = 0.0
                    self._on_ground = True
                    moved = True
                else:
                    self.y = new_y
                    moved = True
            else:
                self.y = new_y
                moved = True

        # ---- Random-walk AI ----
        if now >= self._next_move_time:
            # Pick a new random target within 2-5 blocks
            angle = random.uniform(0, 2 * math.pi)
            dist = random.uniform(2, 5)
            self._target_x = self.x + math.cos(angle) * dist
            self._target_z = self.z + math.sin(angle) * dist
            # Minecraft yaw: 0=south (+Z), 90=west (-X), 180=north (-Z), 270=east (+X)
            self.yaw = (-math.degrees(angle) - 90) % 360
            self._move_speed = random.uniform(0.5, 1.5)  # blocks per second
            self._next_move_time = now + random.uniform(3, 7)

        # Move toward target (only if on ground — can't walk in mid-air)
        if self._on_ground and self._move_speed > 0:
            dx = self._target_x - self.x
            dz = self._target_z - self.z
            dist = (dx * dx + dz * dz) ** 0.5
            if dist > 0.05:
                speed = self._move_speed * dt
                if dist <= speed:
                    self.x = self._target_x
                    self.z = self._target_z
                    self._move_speed = 0.0
                    self._next_move_time = now + random.uniform(1, 3)
                else:
                    self.x += (dx / dist) * speed
                    self.z += (dz / dist) * speed
                moved = True

        return moved


def _pack_degrees(degrees):
    """Pack a 0-360 degree angle as a single byte (0-255), as Minecraft's
    protocol expects. Uses unsigned byte packing since packed degrees are
    0-255 (the client reads them as signed -128..127, which is the same
    bits — the protocol is the same either way)."""
    return write_ubyte(int(degrees * 256 / 360) & 0xFF)


def send_add_entity(conn, entity):
    """Send ClientboundAddEntityPacket to spawn an entity on the client."""
    payload = bytearray()
    payload += write_varint(entity.id)
    payload += write_uuid(entity.uuid[0], entity.uuid[1])
    payload += write_varint(entity.type)
    payload += write_double(entity.x)
    payload += write_double(entity.y)
    payload += write_double(entity.z)
    payload += b"\x00"  # LpVec3: byte 0 = Vec3.ZERO (no initial movement)
    payload += _pack_degrees(entity.pitch)
    payload += _pack_degrees(entity.yaw)
    payload += _pack_degrees(entity.yaw)  # yHeadRot = yaw
    payload += write_varint(0)  # data
    conn.send_packet(Play.ADD_ENTITY, bytes(payload))


def send_teleport_entity(conn, entity):
    """Send ClientboundTeleportEntityPacket to update an entity's position.

    Wire format (decompiled from ClientboundTeleportEntityPacket.STREAM_CODEC):
      VarInt entity_id
      PositionMoveRotation:
        Double x, Double y, Double z        (position — Vec3.STREAM_CODEC)
        Double dx, Double dy, Double dz     (delta movement — Vec3.STREAM_CODEC)
        Float yaw, Float pitch              (ByteBufCodecs.FLOAT)
      Int relatives                          (4-byte big-endian bitmask, NOT VarInt —
                                             Relative.SET_STREAM_CODEC uses ByteBufCodecs.INT
                                             which is readInt/writeInt, not VarInt)
      Bool onGround                          (1 byte: 0 or 1)
    """
    payload = bytearray()
    payload += write_varint(entity.id)
    # PositionMoveRotation: 3 doubles (pos) + 3 doubles (delta) + 2 floats (yaw, pitch)
    payload += write_double(entity.x)
    payload += write_double(entity.y)
    payload += write_double(entity.z)
    payload += write_double(0.0)  # delta x
    payload += write_double(0.0)  # delta y
    payload += write_double(0.0)  # delta z
    payload += write_float(entity.yaw)
    payload += write_float(entity.pitch)
    payload += write_int(0)  # relatives: 4-byte int bitmask (0 = all absolute)
    payload += write_bool(True)  # onGround
    conn.send_packet(Play.TELEPORT_ENTITY, bytes(payload))


def send_remove_entities(conn, entity_ids):
    """Send ClientboundRemoveEntitiesPacket to despawn entities."""
    if not entity_ids:
        return
    payload = bytearray()
    payload += write_varint(len(entity_ids))
    for eid in entity_ids:
        payload += write_varint(eid)
    conn.send_packet(Play.REMOVE_ENTITIES, bytes(payload))
