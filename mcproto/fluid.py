"""Fluid flow computation: when a water or lava source is placed, compute
the flowing fluid blocks that should exist around it; when a source is
removed, find the connected flowing blocks to clear.

Vanilla rules (decompiled from FlowingFluid.spread / spreadToSides):

  - Source (amount 8) spreads horizontally with dropoff:
      water dropoff = 1  -> levels 1, 2, 3, 4, 5, 6, 7  (7-block spread)
      lava  dropoff = 2  -> amounts 6, 4, 2              (3-block spread,
                                rendered as levels 2, 4, 6)
  - A cell with air below spreads DOWNWARD (falling water, amount 8,
    renders as a full column — level 8 in the block-state table).
  - A falling cell does NOT spread horizontally in mid-air; it only
    spreads horizontally once it lands (cell below becomes solid).
  - The source itself always spreads horizontally (even on a cliff edge,
    so a source on a cliff makes both a waterfall and a clifftop pool).

Level property (0-15 in block_states.json):
  0      = source
  1-7    = flowing (level 1 = nearly full, level 7 = shallow)
  8-15   = falling (renders as full column; vanilla tracks the original
           amount in the lower 3 bits, but the render is identical, so
           we just use level 8 for all falling water)

Removal: BFS from the removed source, following same-fluid flowing
blocks (NOT through other sources — they keep their own flow). This
clears the waterfall and pool belonging to one source without touching
a neighbour source's pool.

Known limits (basic scope):
  - No infinite-water-source rule (2 adjacent sources creating a new
    source). Sky farms and 2x2 pool generators won't work.
  - No fluid merging: if two sources' flows overlap, the later placement
    overwrites — flows aren't recomputed when a new source is added
    nearby.
  - No tick-based animation: the whole flow appears at once when the
    source is placed. Vanilla spreads one block per tick (50ms).
  - No lava→stone interaction with water, no obsidian/cobblestone
    generation.
"""

from .data import BLOCK_STATES, state_for
from .world import MIN_Y, WORLD_HEIGHT

# Dropoff per horizontal spread step (FlowingFluid.getDropOff).
_DROPOFF = {"water": 1, "lava": 2}

# 4 horizontal directions: (dx, dz)
_DIRS = [(0, -1), (0, 1), (-1, 0), (1, 0)]

# 6 directions for removal BFS (includes vertical).
_DIRS_6 = [(0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1), (-1, 0, 0), (1, 0, 0)]

# Safety cap: a single source can't generate more than this many flowing
# blocks. A flat 7-block diamond is 112 cells; a tall waterfall plus pool
# is maybe 500. 10000 is generous but prevents runaway computation if the
# world has no floor below a source placed in mid-air at the chunk edge.
_MAX_FLOW_CELLS = 10000


def _fluid_state_range(fluid_name):
    """(base_state_id, count) for this fluid's block-state range.
    All states for water are [89, 104], for lava [105, 120]. A cell with
    a state in this range is a water (or lava) block of some level."""
    base, count, _ = BLOCK_STATES[fluid_name]
    return base, count


def _is_fluid_of_type(state, fluid_name):
    """True if `state` is a water/lava block (source or flowing or falling)."""
    if fluid_name not in BLOCK_STATES:
        return False
    base, count = _fluid_state_range(fluid_name)
    return base <= state < base + count


def _is_source_of_type(state, fluid_name):
    """True if `state` is a source block (level 0) of this fluid."""
    return state == BLOCK_STATES[fluid_name][2]


def compute_flow(sx, sy, sz, fluid_name, get_block_fn, air_state=0):
    """Compute all flowing fluid blocks to place when a source of
    `fluid_name` exists at (sx, sy, sz).

    Returns a list of (x, y, z, state_id) tuples for flowing/falling
    blocks. Does NOT include the source itself.

    The caller is responsible for actually placing these blocks (via
    set_block) and sending BLOCK_UPDATE packets.
    """
    if fluid_name not in _DROPOFF:
        return []
    dropoff = _DROPOFF[fluid_name]

    # (x, y, z) -> amount (8 = source/falling, 7..1 = flowing)
    amounts = {(sx, sy, sz): 8}
    # Queue: (x, y, z, amount, is_source)
    queue = [(sx, sy, sz, 8, True)]

    while queue:
        x, y, z, amount, is_source = queue.pop(0)

        # Hard y boundary: don't spread outside the world vertically.
        # Prevents infinite fall when a source is placed at a chunk edge
        # with no floor below.
        if y < MIN_Y or y > MIN_Y + WORLD_HEIGHT:
            continue

        below_is_air = get_block_fn(x, y - 1, z) == air_state

        # 1. Spread downward: place falling water (amount 8) below
        if below_is_air and y - 1 >= MIN_Y:
            below = (x, y - 1, z)
            if amounts.get(below, 0) < 8:
                amounts[below] = 8
                queue.append((below[0], below[1], below[2], 8, False))

        # 2. Spread horizontally:
        #    - Source: always (cliff-edge source still spreads on top)
        #    - Non-source, solid below: yes (landed cell or horizontal flow)
        #    - Non-source, air below: no (falling cell — only spread down)
        if is_source or not below_is_air:
            new_amount = amount - dropoff
            if new_amount >= 1:
                for dx, dz in _DIRS:
                    nx, nz = x + dx, z + dz
                    cell = (nx, y, nz)
                    existing = amounts.get(cell, 0)
                    if new_amount > existing:
                        if get_block_fn(nx, y, nz) == air_state:
                            amounts[cell] = new_amount
                            queue.append((nx, y, nz, new_amount, False))

        # Safety cap
        if len(amounts) > _MAX_FLOW_CELLS:
            break

    # Convert amounts to state ids (skip the source itself)
    result = []
    for (x, y, z), amount in amounts.items():
        if (x, y, z) == (sx, sy, sz):
            continue
        # amount 8 -> level 8 (falling), amount 7..1 -> level 1..7
        level = 8 if amount >= 8 else 8 - amount
        state = state_for(fluid_name, {"level": str(level)})
        result.append((x, y, z, state))
    return result


def compute_removal(sx, sy, sz, fluid_name, get_block_fn):
    """Find all flowing fluid blocks to remove when the source at
    (sx, sy, sz) is removed.

    BFS from the source, following same-fluid flowing blocks (not through
    other sources — those keep their own flow). Returns a list of
    (x, y, z) tuples to clear (set to air).

    Call this BEFORE removing the source, so the BFS can start from a
    cell that still holds the source state.
    """
    if fluid_name not in BLOCK_STATES:
        return []

    to_remove = []
    visited = {(sx, sy, sz)}
    queue = [(sx, sy, sz)]

    while queue:
        x, y, z = queue.pop(0)
        for dx, dy, dz in _DIRS_6:
            nx, ny, nz = x + dx, y + dy, z + dz
            cell = (nx, ny, nz)
            if cell in visited:
                continue
            state = get_block_fn(nx, ny, nz)
            if not _is_fluid_of_type(state, fluid_name):
                continue
            if _is_source_of_type(state, fluid_name):
                # Another source — don't BFS through it, don't remove it
                continue
            visited.add(cell)
            to_remove.append(cell)
            queue.append(cell)
        # Safety cap
        if len(to_remove) > _MAX_FLOW_CELLS:
            break

    return to_remove


def fluid_name_for_state(state):
    """Return 'water' or 'lava' if this state is a source block of that
    fluid, else None. Used by the bucket-pickup path to decide which
    fluid's connected flow to clear."""
    for name in ("water", "lava"):
        if name in BLOCK_STATES and _is_source_of_type(state, name):
            return name
    return None
