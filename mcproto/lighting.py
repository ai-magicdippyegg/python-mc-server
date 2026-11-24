"""Sky and block light computation.

The client runs its own light updates when a block changes
(LevelChunk.setBlockState -> LevelLightEngine.checkBlock), but it needs
the right light to start from; with none, every edited chunk goes dark.
So each chunk packet carries sky light and block light computed here.

Light sections: SECTION_COUNT+2 long — one below and one above the world
(index 0 = y -80..-65, index 1 = y -64..-49, ..., index 25 = above the
top). Flat-world baseline: stone up to y=-1 (no light), open sky above
(15). Chunks whose neighbourhood has no edits use that baseline
directly; otherwise light is flooded over the chunk plus 16 blocks each
side, using a simple opacity and emission model keyed on block names
(not exact vanilla shapes).
"""

from .codec import write_varint
from .data import _STATE_RANGES, _STATE_STARTS
from .world import (
    GROUND_TOP_Y,
    MIN_Y,
    SECTION_COUNT,
    block_name_for_state,
)

# Re-export so callers can `from mcproto.lighting import LIGHT_SECTIONS`.
# (Defined in world.py to keep SECTION_COUNT + 2 next to SECTION_COUNT,
# but it's conceptually a lighting constant.)
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
    # Late import: lighting reads placed_blocks (world state), but world.set_block
    # calls invalidate_light here — top-level import would create a cycle.
    from . import world as _world
    with _world.world_lock:
        edits = [(x, y, z, st) for (x, y, z), st in _world.placed_blocks.items()
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
