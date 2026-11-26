"""World state: the placed-blocks overlay on the flat superflat floor,
get/set block, world save/load, and chunk section + column encoding.

The world is conceptually infinite in X/Z; only blocks that differ from
the flat default (stone y=-64..-1, air above) are stored. Chunks are
encoded on demand from this diff plus the flat baseline.

Block-state constants
---------------------
``AIR_STATE``      = 0   (always — air is registered first)
``STONE_STATE``    = 1   (best-guess global id; stone has been
                          immediately after air since the 1.13
                          flattening — see README)
``MIN_Y``          = -64
``WORLD_HEIGHT``   = 384
``SECTION_COUNT``  = 24  (WORLD_HEIGHT // 16)
``GROUND_TOP_Y``   = -1  (topmost solid layer; y >= 0 is air)
"""

import json
import os
import threading

from .codec import write_int, write_long, write_short, write_ubyte, write_varint
from .data import (
    BLOCK_STATES,
    PLAINS_BIOME_ID,
    TOTAL_BLOCK_STATES,
    _STATE_RANGES,
    _STATE_STARTS,
)

# ---------------------------------------------------------------------------
# World shape and well-known block state ids
# ---------------------------------------------------------------------------

AIR_STATE = 0
STONE_STATE = 1  # best-guess global block state id, see module docstring

MIN_Y = -64
WORLD_HEIGHT = 384
SECTION_COUNT = WORLD_HEIGHT // 16
GROUND_TOP_Y = -1  # topmost solid layer (grass); y >= 0 is air

# Block state IDs for the world surface layers
GRASS_STATE = 9     # grass_block[snowy=false]
DIRT_STATE = 10     # dirt
BEDROCK_STATE = 88  # bedrock

# ---------------------------------------------------------------------------
# Mutable world state — placed-block overlay
# ---------------------------------------------------------------------------

placed_blocks = {}  # (x, y, z) -> state id
world_lock = threading.Lock()


def get_block(x, y, z):
    """Get the block state at (x, y, z). Checks the placed-block overlay
    first, then falls back to the baseline terrain:
      y == GROUND_TOP_Y (-1):  grass_block
      y == -2 to -3:           dirt
      y == MIN_Y (-64):        bedrock
      y < -3 (down to -63):    stone
      y >= 0:                  air
    """
    with world_lock:
        if (x, y, z) in placed_blocks:
            return placed_blocks[(x, y, z)]
    if y == GROUND_TOP_Y:
        return GRASS_STATE
    if GROUND_TOP_Y - 2 <= y < GROUND_TOP_Y:  # y == -2 or -3
        return DIRT_STATE
    if y == MIN_Y:  # bedrock layer at the very bottom
        return BEDROCK_STATE
    if MIN_Y < y < GROUND_TOP_Y - 2:  # y == -4 to -63
        return STONE_STATE
    return AIR_STATE


def set_block(x, y, z, state):
    with world_lock:
        placed_blocks[(x, y, z)] = state
    # Late import to avoid a circular import with the lighting module
    # (lighting reads placed_blocks; set_block invalidates the light cache).
    from . import lighting
    lighting.invalidate_light(x, z)


# ---------------------------------------------------------------------------
# Persistence — world_save.json next to server.py (or $MC_SAVE_FILE)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SAVE_FILE = os.environ.get("MC_SAVE_FILE", os.path.join(_PROJECT_ROOT, "world_save.json"))
SAVE_INTERVAL = int(os.environ.get("MC_SAVE_INTERVAL", "30"))

# Save format version. Bumped to 2 when the `facing` property value order in
# block_props.json was corrected to match the Direction enum declaration
# order — saves written before that fix hold state ids that no longer
# decode to the same block state, so they are set aside on load rather
# than loaded (same approach as the TOTAL_BLOCK_STATES check below).
SAVE_VERSION = 3  # v3: grass/dirt surface + bedrock layer (was all stone)


def load_world():
    if not os.path.exists(SAVE_FILE):
        return
    try:
        with open(SAVE_FILE) as f:
            data = json.load(f)
        # saves record which block-state table they were written with; a
        # save from a different table would show the wrong blocks, so it is
        # set aside rather than loaded
        if (data.get("__states__") != TOTAL_BLOCK_STATES
                or data.get("__version__", 1) != SAVE_VERSION):
            stale = SAVE_FILE + ".old"
            os.replace(SAVE_FILE, stale)
            print(f"[world] save was written with an older format; moved to {stale}, starting a fresh world")
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
                "__version__": SAVE_VERSION,
                "blocks": {f"{x},{y},{z}": state for (x, y, z), state in placed_blocks.items()}}
    # write to a temp file then rename, so a crash mid-save can't corrupt
    # the save file (same trick real servers use for region files)
    tmp = SAVE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, SAVE_FILE)


# ---------------------------------------------------------------------------
# Chunk section + column encoding
# ---------------------------------------------------------------------------

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
    per 16 vertical blocks, bottom (y=-64) to top.

    Baseline terrain (when no player-placed overrides):
      y == GROUND_TOP_Y (-1):  grass_block
      y == -2 to -3:           dirt
      y == MIN_Y (-64):        bedrock
      y == -4 to -63:          stone
      y >= 0:                  air
    Player-placed blocks override the baseline.
    """
    sections = bytearray()

    for section_index in range(SECTION_COUNT):
        section_min_y = MIN_Y + section_index * 16
        section_max_y = section_min_y + 15

        # Collect overrides for this section: player-placed blocks AND
        # baseline surface/bedrock layers.
        overrides = {}

        # Player-placed blocks
        if placed_blocks:
            with world_lock:
                for (px, py, pz), state in placed_blocks.items():
                    if (px // 16 == chunk_x and pz // 16 == chunk_z
                            and section_min_y <= py <= section_max_y):
                        overrides[(px, py, pz)] = state

        # Baseline surface layers (grass/dirt/bedrock) — only for sections
        # that contain these Y levels. Player-placed overrides take priority
        # (checked via `not in overrides`).
        if section_min_y <= GROUND_TOP_Y <= section_max_y or section_min_y <= MIN_Y <= section_max_y:
            for lx in range(16):
                for lz in range(16):
                    wx = chunk_x * 16 + lx
                    wz = chunk_z * 16 + lz
                    for ly in range(16):
                        wy = section_min_y + ly
                        key = (wx, wy, wz)
                        if key in overrides:
                            continue  # player override takes priority
                        if wy == GROUND_TOP_Y:
                            overrides[key] = GRASS_STATE
                        elif GROUND_TOP_Y - 2 <= wy < GROUND_TOP_Y:
                            overrides[key] = DIRT_STATE
                        elif wy == MIN_Y:
                            overrides[key] = BEDROCK_STATE

        if not overrides:
            # Uniform section: all stone or all air
            if section_max_y <= GROUND_TOP_Y:
                sections += build_section_bytes(STONE_STATE, PLAINS_BIOME_ID, 4096)
            else:
                sections += build_section_bytes(AIR_STATE, PLAINS_BIOME_ID, 0)
        else:
            # Non-uniform section — build with palette encoding
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
            n_distinct = len(palette)
            if n_distinct <= 16:
                bits_per_entry = 4
                use_palette = True
            elif n_distinct <= 256:
                bits_per_entry = max(5, (n_distinct - 1).bit_length())
                use_palette = True
            else:
                bits_per_entry = (TOTAL_BLOCK_STATES - 1).bit_length()
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
            sec += write_ubyte(0)
            sec += write_varint(PLAINS_BIOME_ID)
            sections += sec

    return bytes(sections)


def block_name_for_state(state):
    """Block name for a state id (used by lighting + pick block)."""
    import bisect
    i = bisect.bisect_right(_STATE_STARTS, state) - 1
    return _STATE_RANGES[i][1] if i >= 0 else "air"


# ---------------------------------------------------------------------------
# Tree generation: scatter a few oak trees around spawn so the world
# doesn't look completely barren. Trees are stored as placed_blocks
# (saved/loaded with the world). Called once on first world load.
# ---------------------------------------------------------------------------

OAK_LOG_STATE = 140    # oak_log[axis=y] (default state)
OAK_LEAVES_STATE = 268 # oak_leaves[distance=1,persistent=false,waterlogged=false] (default)

import random as _tree_random
import math as _tree_math

def _place_tree(x, ground_y, z, height=4):
    """Place an oak tree at (x, ground_y+1, z) with the given trunk height.
    ground_y is the top solid block (grass). The trunk starts at ground_y+1."""
    trunk_base = ground_y + 1
    # Trunk
    for dy in range(height):
        placed_blocks[(x, trunk_base + dy, z)] = OAK_LOG_STATE
    # Leaves: 5x5 ring at top-1, 3x3 at top, plus +1 above
    top = trunk_base + height - 1
    for dy, radius in [(0, 2), (1, 1), (2, 1)]:
        ly = top - 1 + dy
        for dx in range(-radius, radius + 1):
            for dz in range(-radius, radius + 1):
                if dx == 0 and dz == 0 and dy < 2:
                    continue  # trunk space
                if abs(dx) == radius and abs(dz) == radius:
                    continue  # corners cut (round canopy)
                lx, lz = x + dx, z + dz
                if (lx, ly, lz) not in placed_blocks:
                    placed_blocks[(lx, ly, lz)] = OAK_LEAVES_STATE


def generate_trees():
    """Generate scattered oak trees around spawn (called once on fresh world)."""
    with world_lock:
        _tree_random.seed(42)  # deterministic — same tree positions every restart
        for _ in range(12):
            angle = _tree_random.uniform(0, 2 * _tree_math.pi)
            dist = _tree_random.uniform(5, 35)
            tx = int(8 + _tree_math.cos(angle) * dist)
            tz = int(8 + _tree_math.sin(angle) * dist)
            height = _tree_random.randint(4, 6)
            _place_tree(tx, GROUND_TOP_Y, tz, height)
    print(f"[world] generated trees around spawn")
