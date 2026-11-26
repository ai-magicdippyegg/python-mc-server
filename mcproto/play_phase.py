"""Play-phase protocol handler.

``handle_play`` is the main per-player state machine after the join
sequence. It receives play-state packets and dispatches them: keep-alive,
teleport acks, use-item-on (block place), player-action (block break +
drop/swap), movement (chunk streaming), chat commands, container clicks
(inventory tracking), set-carried-item / set-creative-slot (creative
inventory tracking), pick-block.

The module also contains all the play-state send_* helpers used by the
handler: ``send_join_game``, ``send_player_abilities``, ``send_chunk``,
``send_chunks_batch``, ``unload_chunks``, ``send_teleport``,
``send_commands`` and ``keepalive_thread``.
"""

import random
import threading
import time

from . import DEBUG
from .bucket import BUCKETS, handle_bucket
from .codec import (
    decode_block_pos,
    encode_block_pos,
    encode_chunk_pos,
    read_hashed_stack,
    read_item_stack,
    write_bool,
    write_byte,
    write_double,
    write_float,
    write_int,
    write_item_stack,
    write_long,
    write_short,
    write_string,
    write_ubyte,
    write_varint,
)
from .connection import log
from .data import BLOCK_STATES, ITEM_ID_BY_NAME, OVERWORLD_DIM_TYPE_ID, item_for_state
from .entities import (
    ENTITY_TYPES,
    Entity,
    send_add_entity,
    send_chat,
    send_health,
    send_item_entity_data,
    send_remove_entities,
    send_set_time,
    send_teleport_entity,
)
from .fluid import check_infinite_water, compute_flow, compute_removal, fluid_name_for_state
from .lighting import build_light_payload
from .placement import build_placement, decode_state
from .player import (
    active_sessions,
    get_saved_inventory,
    get_saved_position,
    save_players,
    update_player,
)
from .protocol import Play
from .redstone import is_lever, is_redstone_block, recompute_power, toggle_lever
from .world import (
    AIR_STATE,
    block_name_for_state,
    build_chunk_column,
    get_block,
    set_block,
)

VIEW_RADIUS = 4
SPAWN_X, SPAWN_Y, SPAWN_Z = 8, 1, 8

# Chat broadcast: list of (conn, username) for all connected players.
# Used by /say, /me, join/leave notifications.
_chat_listeners = []
_chat_listeners_lock = threading.Lock()

def broadcast_chat(text):
    """Send a chat message to all connected players."""
    with _chat_listeners_lock:
        listeners = list(_chat_listeners)
    for conn, name in listeners:
        try:
            send_chat(conn, text)
        except (ConnectionError, OSError):
            pass

# mutable teleport id counter, boxed in a list so handle_play can rewrite it
# from a nested scope without `nonlocal`
_teleport_id_counter = [1]


def _add_to_inventory(conn, inventory, item_name, count, addr):
    """Add an item to the player's inventory (stack with existing or find
    empty slot). Sends CONTAINER_SET_SLOT for the updated slot."""
    # Try to stack with an existing item
    for slot in range(9, 45):
        cur = inventory.get(slot)
        if cur and cur[0] == item_name and cur[1] < 64:
            new_count = cur[1] + count
            inventory[slot] = (item_name, new_count)
            conn.send_packet(Play.CONTAINER_SET_SLOT,
                              write_varint(0) + write_varint(0) + write_short(slot) +
                              write_item_stack(item_name, new_count))
            return True
    # Find an empty slot
    for slot in range(9, 45):
        if slot not in inventory:
            inventory[slot] = (item_name, count)
            conn.send_packet(Play.CONTAINER_SET_SLOT,
                              write_varint(0) + write_varint(0) + write_short(slot) +
                              write_item_stack(item_name, count))
            return True
    # Inventory full — item is lost (basic: no item entity drop)
    if DEBUG:
        log(addr, f"inventory full, lost {count}x {item_name}")
    return False


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
    """ClientboundCommandsPacket — declares all commands so the client shows
    them in the '/' suggestion list and offers tab-completion.

    Command tree layout:
      Root (0)
      ├── spawn (1)          literal, executable
      ├── help (2)           literal, executable
      ├── gamemode (3)       literal, NOT executable (needs sub-command)
      │   ├── survival (4)   literal, executable
      │   ├── creative (5)   literal, executable
      │   ├── s (6)          literal, executable
      │   ├── c (7)          literal, executable
      │   ├── 0 (8)          literal, executable
      │   └── 1 (9)          literal, executable
      ├── time (10)          literal, NOT executable (needs sub-command)
      │   ├── day (11)       literal, executable
      │   ├── night (12)     literal, executable
      │   └── noon (13)      literal, executable
      ├── say (14)           literal, executable
      └── me (15)            literal, executable

    Entry format:
      UByte flags (bits 0-1 = type [0=root,1=literal,2=arg], bit 2 = executable,
                   bit 3 = has redirect, bit 4 = has custom suggestions)
      VarInt children_count, VarInt[children_count] child indices
      [If literal: String name]
      [If argument: String name, VarInt parser_id, parser-specific data]
    """
    entries = []

    def add_literal(name, executable=True, children=None):
        """Returns the index of the new entry."""
        idx = len(entries)
        flags = 0x01  # literal type
        if executable:
            flags |= 0x04  # executable bit
        entry = bytearray()
        entry += write_ubyte(flags)
        kids = children or []
        entry += write_varint(len(kids))
        for k in kids:
            entry += write_varint(k)
        entry += write_string(name)
        entries.append(entry)
        return idx

    def add_root(children):
        idx = len(entries)
        entry = bytearray()
        entry += write_ubyte(0x00)  # root type, not executable
        entry += write_varint(len(children))
        for k in children:
            entry += write_varint(k)
        entries.append(entry)
        return idx

    # Build the tree bottom-up so we can reference child indices.
    # /gamemode sub-commands
    i_survival = add_literal("survival")
    i_creative = add_literal("creative")
    i_s = add_literal("s")
    i_c = add_literal("c")
    i_0 = add_literal("0")
    i_1 = add_literal("1")
    i_gamemode = add_literal("gamemode", executable=False,
                             children=[i_survival, i_creative, i_s, i_c, i_0, i_1])

    # /time sub-commands
    i_day = add_literal("day")
    i_night = add_literal("night")
    i_noon = add_literal("noon")
    i_time = add_literal("time", executable=False, children=[i_day, i_night, i_noon])

    # Standalone commands
    i_say = add_literal("say")
    i_me = add_literal("me")
    i_help = add_literal("help")
    i_spawn = add_literal("spawn")

    # Root
    i_root = add_root([i_spawn, i_help, i_gamemode, i_time, i_say, i_me])

    # Build the packet
    payload = bytearray()
    payload += write_varint(len(entries))
    for entry in entries:
        payload += entry
    payload += write_varint(i_root)
    conn.send_packet(Play.COMMANDS, bytes(payload))


def send_join_game(conn, entity_id, gamemode=1):
    """gamemode: 0=survival, 1=creative"""
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
    payload += write_varint(gamemode)  # game type: 0=survival, 1=creative
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


def send_player_abilities(conn, gamemode=1):
    """gamemode: 0=survival (no flying), 1=creative (fly + invulnerable + instant-break)"""
    if gamemode == 1:  # creative
        flags = 0x01 | 0x04 | 0x08  # invulnerable, allow flying, creative/instant-break
    else:  # survival
        flags = 0x00  # nothing
    payload = write_byte(flags) + write_float(0.05) + write_float(0.1)
    conn.send_packet(Play.PLAYER_ABILITIES, payload)


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
    # Load gamemode from saved player data (default creative for existing players)
    from .player import players, players_lock
    with players_lock:
        p = players.get(uuid_key, {})
        gamemode = p.get("gamemode", 1)  # default creative

    send_join_game(conn, entity_id, gamemode)
    send_player_abilities(conn, gamemode)

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
    breaking_pos = [None]  # (bx,by,bz) of the block being broken in survival
    active_sessions[uuid_key] = (username, pos_state, inventory, held_slot_box)

    log(addr, f"player entered the world at {px:.1f},{py:.1f},{pz:.1f}"
                + (" (restored)" if saved else " (spawn)")
                + (f", {len(inventory)} inventory slots restored" if inventory else ""))

    # ---- Spawn a few animals near the player for basic entity support.
    # Each player gets their own set of entities (despawned on disconnect).
    entities = []
    import math
    for _ in range(random.randint(3, 6)):
        # Pick a random animal type and position near the player
        animal = random.choice(["chicken", "cow", "pig", "sheep"])
        angle = random.uniform(0, 2 * math.pi)
        dist = random.uniform(3, 12)
        ex = px + math.cos(angle) * dist
        ez = pz + math.sin(angle) * dist
        ey = 1.0  # spawn at y=1 (on top of the stone floor at y=-1)
        ent = Entity(ENTITY_TYPES[animal], ex, ey, ez,
                     yaw=random.uniform(0, 360))
        entities.append(ent)
        send_add_entity(conn, ent)
    if DEBUG and entities:
        log(addr, f"spawned {len(entities)} animals near player")

    # ---- Entity AI tick loop: moves entities, sends teleport packets, and
    # picks up nearby item entities (adds to inventory, despawns).
    # Runs at 20 ticks/sec (50ms) — client interpolates at this rate.
    entity_state = {"alive": True}
    def entity_tick_loop():
        last_tick = time.time()
        while entity_state["alive"]:
            time.sleep(0.05)  # 20 ticks/sec
            now = time.time()
            dt = min(now - last_tick, 0.1)
            last_tick = now
            try:
                for ent in entities:
                    if ent.alive and ent.tick(dt, lambda x, y, z: get_block(x, y, z)):
                        send_teleport_entity(conn, ent)
                # Item pickup: check if any item entity is near the player
                px2, py2, pz2 = pos_state[0], pos_state[1], pos_state[2]
                for ent in list(entities):
                    if not ent.alive or ent.type != ENTITY_TYPES.get("item"):
                        continue
                    dx = ent.x - px2
                    dy = ent.y - py2
                    dz = ent.z - pz2
                    if dx*dx + dy*dy + dz*dz < 2.25:  # within 1.5 blocks
                        item_name = getattr(ent, '_item_name', None)
                        item_count = getattr(ent, '_item_count', 1)
                        if item_name and item_name in ITEM_ID_BY_NAME:
                            placed = _add_to_inventory(conn, inventory, item_name, item_count, addr)
                            if placed:
                                ent.alive = False
                                send_remove_entities(conn, [ent.id])
                                if DEBUG:
                                    log(addr, f"picked up {item_name} x{item_count}")
            except (ConnectionError, OSError):
                entity_state["alive"] = False
                return
    ent_thread = threading.Thread(target=entity_tick_loop, daemon=True)
    ent_thread.start()

    # ---- Day/night cycle: send SetTimePacket every second so the world
    # clock advances. Time wraps at 24000 (one Minecraft day = 20 minutes
    # real time at 20 ticks/sec).
    time_state = {"alive": True, "game_time": 0, "time_of_day": 0}  # start at dawn
    def time_thread():
        while time_state["alive"]:
            time.sleep(1.0)  # update every second
            time_state["game_time"] += 20  # 20 ticks per second
            time_state["time_of_day"] = (time_state["time_of_day"] + 20) % 24000
            try:
                send_set_time(conn, time_state["game_time"], time_state["time_of_day"])
            except (ConnectionError, OSError):
                time_state["alive"] = False
                return
    time_thread_obj = threading.Thread(target=time_thread, daemon=True)
    time_thread_obj.start()

    # Send a welcome chat message
    send_chat(conn, f"Welcome, {username}!")
    # Send health/food HUD (full in creative, full in survival on join)
    send_health(conn, health=20.0, food=20, saturation=5.0)
    # Register as a chat listener (for /say, /me, join/leave broadcasts)
    with _chat_listeners_lock:
        _chat_listeners.append((conn, username))
    broadcast_chat(f"{username} joined the game")

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

                from .data import ITEM_TO_BLOCK_NAME
                held = inventory.get(held_slot_box[0] + 36)

                # ---- Lever toggle: right-click on a placed lever toggles it.
                # Checked before bucket/placement so the lever activates even
                # when the player is holding an item. (Vanilla uses sneak to
                # place instead of toggle; we skip that for basic.)
                clicked_state = get_block(bx, by, bz)
                clicked_name = block_name_for_state(clicked_state)
                if is_lever(clicked_name):
                    new_lever_state = toggle_lever(clicked_state)
                    set_block(bx, by, bz, new_lever_state)
                    conn.send_packet(Play.BLOCK_UPDATE,
                                      encode_block_pos(bx, by, bz) + write_varint(new_lever_state))
                    if DEBUG:
                        props = decode_state("lever", new_lever_state)
                        log(addr, f"toggled lever at {bx},{by},{bz} (powered={props.get('powered')})")
                    # Recompute redstone power grid around the lever
                    updates = recompute_power(bx, by, bz,
                                              lambda x, y, z: get_block(x, y, z))
                    for (rx, ry, rz, rstate) in updates:
                        set_block(rx, ry, rz, rstate)
                        conn.send_packet(Play.BLOCK_UPDATE,
                                          encode_block_pos(rx, ry, rz) + write_varint(rstate))
                    if DEBUG and updates:
                        log(addr, f"  redstone: {len(updates)} block(s) updated")
                    conn.send_packet(Play.BLOCK_CHANGED_ACK, write_varint(sequence))
                    continue  # skip placement

                # ---- Buckets: place fluid from a filled bucket, or pick up a
                # fluid source with an empty bucket. Handled before regular
                # block placement because buckets aren't blocks (they're not
                # in ITEM_TO_BLOCK_NAME) so the regular path would do nothing.
                if held and held[0] in BUCKETS:
                    action = handle_bucket(
                        held[0], bx, by, bz, px, py, pz,
                        lambda x, y, z: get_block(x, y, z),
                        air_state=AIR_STATE,
                    )
                    if action is not None:
                        kind = action[0]
                        if kind == "place":
                            _, fx, fy, fz, fluid_state = action
                            set_block(fx, fy, fz, fluid_state)
                            conn.send_packet(Play.BLOCK_UPDATE,
                                              encode_block_pos(fx, fy, fz) + write_varint(fluid_state))
                            # Spread the flow: compute all flowing/falling blocks
                            # that should exist around this source and place them.
                            fname = {89: "water", 105: "lava", 30519: None}.get(fluid_state)
                            if fname:
                                flow = compute_flow(fx, fy, fz, fname,
                                                    lambda x, y, z: get_block(x, y, z),
                                                    air_state=AIR_STATE)
                                for (fx2, fy2, fz2, fstate) in flow:
                                    set_block(fx2, fy2, fz2, fstate)
                                    conn.send_packet(Play.BLOCK_UPDATE,
                                                      encode_block_pos(fx2, fy2, fz2) + write_varint(fstate))
                                if DEBUG and flow:
                                    log(addr, f"fluid {fname} spread: placed {len(flow)} flowing blocks from ({fx},{fy},{fz})")
                                # Check for infinite water source formation:
                                # if 2+ adjacent water sources surround a
                                # flowing water cell with solid below, it
                                # becomes a source (classic 2x2 pool mechanic).
                                if fname == "water":
                                    iw = check_infinite_water(fx, fy, fz,
                                                              lambda x, y, z: get_block(x, y, z),
                                                              air_state=AIR_STATE)
                                    for (iwx, iwy, iwz, iwstate) in iw:
                                        set_block(iwx, iwy, iwz, iwstate)
                                        conn.send_packet(Play.BLOCK_UPDATE,
                                                          encode_block_pos(iwx, iwy, iwz) + write_varint(iwstate))
                                    if DEBUG and iw:
                                        log(addr, f"infinite water: {len(iw)} source(s) formed")
                            if DEBUG:
                                log(addr, f"placed fluid state {fluid_state} at {fx},{fy},{fz} (held={held[0]})")
                        elif kind == "pickup":
                            _, fx, fy, fz, new_item = action
                            # Before removing the source, find all connected
                            # flowing blocks so we can clear them too.
                            fname = fluid_name_for_state(get_block(fx, fy, fz))
                            to_clear = []
                            if fname:
                                to_clear = compute_removal(fx, fy, fz, fname,
                                                           lambda x, y, z: get_block(x, y, z))
                            # Remove the source
                            set_block(fx, fy, fz, AIR_STATE)
                            conn.send_packet(Play.BLOCK_UPDATE,
                                              encode_block_pos(fx, fy, fz) + write_varint(AIR_STATE))
                            # Remove all connected flowing blocks
                            for (cx, cy, cz) in to_clear:
                                set_block(cx, cy, cz, AIR_STATE)
                                conn.send_packet(Play.BLOCK_UPDATE,
                                                  encode_block_pos(cx, cy, cz) + write_varint(AIR_STATE))
                            # swap held bucket for the filled variant
                            hot = held_slot_box[0] + 36
                            inventory[hot] = (new_item, 1)
                            conn.send_packet(Play.CONTAINER_SET_SLOT,
                                              write_varint(0) + write_varint(0) + write_short(hot) +
                                              write_item_stack(new_item, 1))
                            if DEBUG:
                                log(addr, f"picked up fluid at {fx},{fy},{fz}, swapped to {new_item}"
                                           + (f", cleared {len(to_clear)} flowing blocks" if to_clear else ""))
                        conn.send_packet(Play.BLOCK_CHANGED_ACK, write_varint(sequence))
                        continue  # skip regular block placement

                # ---- Regular block placement ----
                # only place when the held item is one that has a block form
                # (see ITEM_TO_BLOCK_STATE); an empty hand or a tool/food/etc
                # does nothing. The ack below is still sent so the client
                # doesn't wait on a prediction.
                plan = None
                if held and held[0] in ITEM_TO_BLOCK_NAME:
                    plan = build_placement(held[0], bx, by, bz, face, cursor_y,
                                           pos_state[3], pos_state[4],
                                           get_block, AIR_STATE)
                # The plan may include neighbour-connection updates (for
                # fences/walls/panes) or a clicked-block update (for double
                # slabs) that don't require the primary adjacent cell to be
                # air. For a normal single-block place, the first entry IS
                # the adjacent cell, and we place it if it's currently air
                # OR a fluid block (placing a block in water replaces the
                # water — vanilla calls this "waterlogged" but for basic
                # we just overwrite and clear the source's connected flow).
                if plan:
                    primary_x, primary_y, primary_z, primary_state, primary_blk = plan[0]
                    is_double_slab = (primary_x == bx and primary_y == by and primary_z == bz)
                    target_state = get_block(primary_x, primary_y, primary_z)
                    target_is_air = (target_state == AIR_STATE)
                    # Check if the target is a fluid block (source or flowing)
                    target_fluid = fluid_name_for_state(target_state)
                    if target_fluid is None and target_state != AIR_STATE:
                        for fn in ("water", "lava"):
                            base, count, _ = BLOCK_STATES.get(fn, (0, 0, 0))
                            if base <= target_state < base + count:
                                target_fluid = fn
                                break
                    if is_double_slab or target_is_air or target_fluid:
                        # If overwriting a fluid SOURCE, clear its connected
                        # flow first (same as bucket pickup).
                        if target_fluid and target_state == BLOCK_STATES.get(target_fluid, (0, 0, 0))[2]:
                            to_clear = compute_removal(primary_x, primary_y, primary_z,
                                                       target_fluid,
                                                       lambda x, y, z: get_block(x, y, z))
                            for (cx, cy, cz) in to_clear:
                                set_block(cx, cy, cz, AIR_STATE)
                                conn.send_packet(Play.BLOCK_UPDATE,
                                                  encode_block_pos(cx, cy, cz) + write_varint(AIR_STATE))
                        for (x, y, z, st, blk) in plan:
                            set_block(x, y, z, st)
                            conn.send_packet(Play.BLOCK_UPDATE, encode_block_pos(x, y, z) + write_varint(st))
                        if DEBUG:
                            x, y, z, st, blk = plan[0]
                            extra = ""
                            if len(plan) > 1:
                                extra = f" + {len(plan)-1} neighbour update(s)"
                            if target_fluid:
                                extra += f" (replaced {target_fluid})"
                            log(addr, f"placed {blk} at {x},{y},{z} {decode_state(blk, st) or ''} (held={held[0]}){extra}")
                        # If a redstone block was placed, recompute the power grid
                        if is_redstone_block(primary_blk):
                            rx, ry, rz = primary_x, primary_y, primary_z
                            updates = recompute_power(rx, ry, rz,
                                                      lambda x, y, z: get_block(x, y, z))
                            for (rx2, ry2, rz2, rstate) in updates:
                                set_block(rx2, ry2, rz2, rstate)
                                conn.send_packet(Play.BLOCK_UPDATE,
                                                  encode_block_pos(rx2, ry2, rz2) + write_varint(rstate))
                            if DEBUG and updates:
                                log(addr, f"  redstone: {len(updates)} block(s) updated after placement")
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
                # 0 = START_DESTROY_BLOCK
                # 1 = ABORT_DESTROY_BLOCK (player looked away / stopped)
                # 2 = STOP_DESTROY_BLOCK (client says "I'm done breaking")
                #
                # Creative: action 0 = instant break (client expects this)
                # Survival: action 0 = start breaking (just ACK), action 2 = break
                #           action 1 = abort (cancel break)
                if action == 1:  # ABORT_DESTROY_BLOCK
                    breaking_pos[0] = None
                    conn.send_packet(Play.BLOCK_CHANGED_ACK, write_varint(sequence))
                    continue

                if action == 2 and gamemode == 0:  # STOP_DESTROY_BLOCK (survival)
                    # Client says it's done breaking — break the block now.
                    bx, by, bz = decode_block_pos(block_val)
                    breaking_pos[0] = None
                    # Fall through to the block-break logic below (same as action 0)
                    action = 0  # reuse the break code

                if action == 0:
                    # ---- Entity damage: check if an entity is at/near the
                    # clicked position. If yes, damage it (not instant kill —
                    # entities have health now). Check within ~2 blocks.
                    hit_entity = None
                    for ent in entities:
                        if not ent.alive or ent.type == ENTITY_TYPES.get("item"):
                            continue
                        edx = ent.x - (bx + 0.5)
                        edy = ent.y - (by + 0.5)
                        edz = ent.z - (bz + 0.5)
                        if edx*edx + edy*edy + edz*edz < 4.0:
                            hit_entity = ent
                            break
                    if hit_entity:
                        # Damage the entity (4 HP per hit = 2 hearts)
                        hit_entity.health -= 4
                        if DEBUG:
                            log(addr, f"hit entity {hit_entity.id} (HP={hit_entity.health})")
                        if hit_entity.health <= 0:
                            hit_entity.alive = False
                            send_remove_entities(conn, [hit_entity.id])
                            if DEBUG:
                                log(addr, f"killed entity {hit_entity.id}")
                            if gamemode == 0:  # survival: drop loot
                                ent_loot = {ENTITY_TYPES["chicken"]: "chicken",
                                            ENTITY_TYPES["cow"]: "beef",
                                            ENTITY_TYPES["pig"]: "porkchop",
                                            ENTITY_TYPES["sheep"]: "mutton"}.get(hit_entity.type)
                                if ent_loot and ent_loot in ITEM_ID_BY_NAME:
                                    item_ent = Entity(ENTITY_TYPES["item"],
                                                      hit_entity.x, hit_entity.y, hit_entity.z)
                                    item_ent._item_name = ent_loot
                                    item_ent._item_count = 1
                                    entities.append(item_ent)
                                    send_add_entity(conn, item_ent)
                                    send_item_entity_data(conn, item_ent.id, ent_loot, 1)
                        conn.send_packet(Play.BLOCK_CHANGED_ACK, write_varint(sequence))
                        continue  # don't break the block behind the entity

                    # ---- Survival: don't break instantly on START_DESTROY_BLOCK.
                    # The client sends STOP_DESTROY_BLOCK (action 2) when it's
                    # done. Just record the position and send ACK.
                    if gamemode == 0:  # survival
                        breaking_pos[0] = (bx, by, bz)
                        conn.send_packet(Play.BLOCK_CHANGED_ACK, write_varint(sequence))
                        continue

                    # ---- Creative: break instantly (action 0).

                    # ---- Block break: capture the block state before breaking
                    # (needed for item drops + fluid cleanup + redstone recompute).
                    broken_state = get_block(bx, by, bz)
                    broken_name = block_name_for_state(broken_state)
                    fname = fluid_name_for_state(broken_state)
                    to_clear = []
                    if fname:
                        to_clear = compute_removal(bx, by, bz, fname,
                                                   lambda x, y, z: get_block(x, y, z))
                    set_block(bx, by, bz, AIR_STATE)
                    conn.send_packet(Play.BLOCK_UPDATE,
                                      encode_block_pos(bx, by, bz) + write_varint(AIR_STATE))
                    for (cx, cy, cz) in to_clear:
                        set_block(cx, cy, cz, AIR_STATE)
                        conn.send_packet(Play.BLOCK_UPDATE,
                                          encode_block_pos(cx, cy, cz) + write_varint(AIR_STATE))
                    if DEBUG:
                        extra = f", cleared {len(to_clear)} connected flow blocks" if to_clear else ""
                        log(addr, f"broke block at {bx},{by},{bz}{extra}")
                    # If a redstone block was broken (or adjacent to redstone),
                    # recompute the power grid around the break point.
                    if is_redstone_block(broken_name):
                        updates = recompute_power(bx, by, bz,
                                                  lambda x, y, z: get_block(x, y, z))
                        for (rx, ry, rz, rstate) in updates:
                            set_block(rx, ry, rz, rstate)
                            conn.send_packet(Play.BLOCK_UPDATE,
                                              encode_block_pos(rx, ry, rz) + write_varint(rstate))
                        if DEBUG and updates:
                            log(addr, f"  redstone: {len(updates)} block(s) updated after break")

                    # ---- Item drop: in survival, spawn an item entity on the
                    # ground (walk over to pick up). In creative, no drops.
                    # The SetEntityDataPacket now uses the correct OPTIONAL_STREAM_CODEC
                    # format (Boolean present flag + ItemStack data), confirmed
                    # by decompiling EntityDataSerializers$1.codec().
                    if gamemode == 0:  # survival
                        drop_item = None
                        if broken_name not in ("air", "water", "lava", "powder_snow",
                                                "bubble_column", "cave_air", "void_air"):
                            drop_item = item_for_state(broken_state)
                        if drop_item and drop_item in ITEM_ID_BY_NAME:
                            item_ent = Entity(ENTITY_TYPES["item"],
                                              bx + 0.5, by + 0.5, bz + 0.5)
                            item_ent._item_name = drop_item
                            item_ent._item_count = 1
                            entities.append(item_ent)
                            send_add_entity(conn, item_ent)
                            send_item_entity_data(conn, item_ent.id, drop_item, 1)

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
                    send_chat(conn, "Teleported to spawn.")
                elif command == "help":
                    send_chat(conn, "Commands: /spawn /gamemode <survival|creative> /time <day|night|noon> /say <msg> /me <action> /help")
                elif command.startswith("say "):
                    msg = command[4:]
                    broadcast_chat(f"[{username}] {msg}")
                elif command.startswith("me "):
                    msg = command[3:]
                    broadcast_chat(f"* {username} {msg}")
                elif command == "time day":
                    time_state["time_of_day"] = 1000
                    send_set_time(conn, time_state["game_time"], 1000)
                    send_chat(conn, "Time set to day.")
                elif command == "time night":
                    time_state["time_of_day"] = 13000
                    send_set_time(conn, time_state["game_time"], 13000)
                    send_chat(conn, "Time set to night.")
                elif command == "time noon":
                    time_state["time_of_day"] = 6000
                    send_set_time(conn, time_state["game_time"], 6000)
                    send_chat(conn, "Time set to noon.")
                elif command.startswith("gamemode "):
                    mode = command[9:].strip().lower()
                    if mode in ("0", "survival", "s"):
                        gamemode = 0
                        send_player_abilities(conn, 0)
                        conn.send_packet(Play.GAME_EVENT, write_ubyte(2) + write_float(0.0))
                        send_chat(conn, "Game mode set to SURVIVAL.")
                    elif mode in ("1", "creative", "c"):
                        gamemode = 1
                        send_player_abilities(conn, 1)
                        conn.send_packet(Play.GAME_EVENT, write_ubyte(2) + write_float(1.0))
                        send_chat(conn, "Game mode set to CREATIVE.")
                    else:
                        send_chat(conn, "Usage: /gamemode <survival|creative>")
                    # Save gamemode to player data
                    from .player import players as _players, players_lock as _plock
                    with _plock:
                        if uuid_key in _players:
                            _players[uuid_key]["gamemode"] = gamemode
                        else:
                            _players[uuid_key] = {"gamemode": gamemode}
                else:
                    send_chat(conn, f"Unknown command: /{command}. Type /help for available commands.")

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
        entity_state["alive"] = False
        time_state["alive"] = False
        active_sessions.pop(uuid_key, None)
        # Unregister chat listener + broadcast leave
        # Use slice assignment ([:]) to mutate the existing list rather than
        # rebinding _chat_listeners — a plain = would make Python treat it
        # as a local variable throughout handle_play, causing UnboundLocalError
        # at the .append() call earlier in the function.
        with _chat_listeners_lock:
            _chat_listeners[:] = [(c, n) for c, n in _chat_listeners if c is not conn]
        broadcast_chat(f"{username} left the game")
        # Despawn all entities for this player
        try:
            send_remove_entities(conn, [e.id for e in entities if e.alive])
        except (ConnectionError, OSError):
            pass
        update_player(uuid_key, username, *pos_state,
                      inventory=inventory, held_slot=held_slot_box[0])
        try:
            save_players()
        except Exception as e:
            log(addr, f"failed to save player data: {e}")
        log(addr, f"player disconnected (saved at {pos_state[0]:.1f},{pos_state[1]:.1f},{pos_state[2]:.1f})")
