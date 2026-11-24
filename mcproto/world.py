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
GROUND_TOP_Y = -1  # topmost solid (stone) layer; y >= 0 is air

# ---------------------------------------------------------------------------
# Mutable world state — placed-block overlay
# ---------------------------------------------------------------------------

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
SAVE_VERSION = 2


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


def block_name_for_state(state):
    """Block name for a state id (used by lighting + pick block)."""
    import bisect
    i = bisect.bisect_right(_STATE_STARTS, state) - 1
    return _STATE_RANGES[i][1] if i >= 0 else "air"
