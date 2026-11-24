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

import random
import struct
import threading
import time
import uuid as _uuid

from .codec import write_bool, write_byte, write_double, write_float, write_int, write_long, write_varint, write_uuid
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


class Entity:
    """A simple entity with position, rotation, and random-walk AI."""

    def __init__(self, entity_type, x, y, z, yaw=0.0, pitch=0.0):
        self.id = next_entity_id()
        self.uuid = _uuid.uuid4().int >> 64, _uuid.uuid4().int >> 64  # (msb, lsb)
        # Actually, let's use a proper random UUID
        self.uuid = (random.getrandbits(64), random.getrandbits(64))
        self.type = entity_type  # registry id (int)
        self.x = x
        self.y = y
        self.z = z
        self.yaw = yaw
        self.pitch = pitch
        self.alive = True
        # AI state
        self._next_move_time = time.time() + random.uniform(1, 4)
        self._target_x = x
        self._target_z = z
        self._move_speed = 0.0  # blocks per tick

    def tick(self, dt, get_block_fn, air_state=0):
        """Advance the entity's AI by dt seconds. Returns True if the
        entity moved (and the caller should send a teleport packet)."""
        if not self.alive:
            return False

        import math
        now = time.time()
        if now >= self._next_move_time:
            # Pick a new random target within 2-5 blocks
            angle = random.uniform(0, 2 * math.pi)
            dist = random.uniform(2, 5)
            self._target_x = self.x + math.cos(angle) * dist
            self._target_z = self.z + math.sin(angle) * dist
            # Minecraft yaw: 0=south (+Z), 90=west (-X), 180=north (-Z), 270=east (+X)
            # angle is standard math (0=+X, 90=+Y). Convert: yaw = -degrees(angle) - 90
            self.yaw = (-math.degrees(angle) - 90) % 360
            self._move_speed = random.uniform(0.5, 1.5)  # blocks per second
            self._next_move_time = now + random.uniform(3, 7)

        # Move toward target
        dx = self._target_x - self.x
        dz = self._target_z - self.z
        dist = (dx * dx + dz * dz) ** 0.5
        if dist > 0.1:
            speed = self._move_speed * dt
            if dist <= speed:
                self.x = self._target_x
                self.z = self._target_z
                self._move_speed = 0.0
                self._next_move_time = now + random.uniform(1, 3)
            else:
                self.x += (dx / dist) * speed
                self.z += (dz / dist) * speed
            return True
        return False


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
    payload += write_byte(int(entity.pitch * 256 / 360))  # packed degrees
    payload += write_byte(int(entity.yaw * 256 / 360))
    payload += write_byte(int(entity.yaw * 256 / 360))  # yHeadRot = yaw
    payload += write_varint(0)  # data
    conn.send_packet(Play.ADD_ENTITY, bytes(payload))


def send_teleport_entity(conn, entity):
    """Send ClientboundTeleportEntityPacket to update an entity's position."""
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
    payload += write_varint(0)  # relatives: 0 = all absolute
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
