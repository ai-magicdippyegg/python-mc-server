"""Connection-aware block state computation for fences, walls, panes,
and double-slab placement.

Vanilla's connection rules (decompiled from FenceBlock / WallBlock):

  FenceBlock.connectsTo(state, faceSolid, dir):
      same_fence(state) || (!isExceptionForConnection(state) && faceSolid)
                  || (block is FenceGate && connects in this dir)
  FenceBlock.isSameFence(state):
      state is in FENCES tag AND
      state is in WOODEN_FENCES tag == self is in WOODEN_FENCES tag

  WallBlock.connectsTo(state, faceSolid, dir):
      state is in WALLS tag || (!isExceptionForConnection(state) && faceSolid)
                  || block is IronBarsBlock
                  || (block is FenceGate && connects in this dir)

We approximate the vanilla rules:
  - "same fence": two fences are the same category if both are wooden
    (any name ending in ``_fence`` except ``nether_brick_fence``) or
    both are ``nether_brick_fence``.
  - "face sturdy": a block is treated as a sturdy neighbour if its
    lighting opacity is 15 (full cube). This is the same heuristic the
    lighting module already computes; it has the same edge cases (glass
    is opacity 0 so doesn't connect, leaves are opacity 1 so don't
    connect, partial blocks like slabs/stairs don't connect).
  - "walls": any block name ending in ``_wall``.
  - "iron bars": ``iron_bars`` itself.
  - Fence gates are skipped for this basic version.

Wall connection values (``low``/``tall``) are simplified: any
connection becomes ``low``. The ``tall`` variant depends on whether the
neighbour is taller than this wall (e.g. a wall with a block above),
which needs shape comparison we don't model.
"""

from .data import BLOCK_PROPS, BLOCK_STATES, decode_state, state_for

# ---------------------------------------------------------------------------
# Block category detection — by name suffix, since we don't have a tag
# table mapping every block to its tag membership.
# ---------------------------------------------------------------------------

def is_fence(name):
    """Any fence (wooden or nether brick)."""
    return name.endswith("_fence")

def is_wooden_fence(name):
    """Fences that connect to each other (wooden fence category). Excludes
    nether_brick_fence which is its own category."""
    return name.endswith("_fence") and name != "nether_brick_fence"

def is_wall(name):
    return name.endswith("_wall") and name not in ("wall_torch", "soul_wall_torch",
                                                     "redstone_wall_torch")

def is_pane(name):
    """Glass panes and iron bars — they share the same connection model."""
    return name.endswith("_pane") or name == "iron_bars"

def is_slab(name):
    return name.endswith("_slab")


# ---------------------------------------------------------------------------
# "Sturdy neighbour" check — using lighting opacity as a proxy for
# isFaceSturdy. Imported lazily to avoid pulling the lighting module (and
# its world dependency) at placement-module import time.
# ---------------------------------------------------------------------------

def _is_sturdy(state):
    """True if a block with this state id is a sturdy (full-cube) neighbour
    that fences/walls/panes should connect to. Uses lighting opacity 15 as
    the proxy."""
    from .lighting import state_light_props
    if state == 0:  # air
        return False
    op, _ = state_light_props(state)
    return op >= 15


# ---------------------------------------------------------------------------
# Connection computation — given a freshly-placed fence/wall/pane at
# (x, y, z), compute its state id with the four directional connections
# set based on the current neighbours.
# ---------------------------------------------------------------------------

# Direction -> (dx, dz, facing-property-name-on-THIS-block, opposite-property-name-on-NEIGHBOUR)
# Vanilla's FenceBlock.PROPERTY_BY_DIRECTION maps:
#   NORTH -> NORTH, SOUTH -> SOUTH, EAST -> EAST, WEST -> WEST
# When a fence is placed at (x,y,z) and a neighbour is to its NORTH
# (i.e. at (x, y, z-1)), the fence's NORTH flag is set if the neighbour
# connects. The neighbour's SOUTH flag is the one pointing back toward us.
_DIRS = [
    ("north",  0, -1, "north", "south"),
    ("south",  0,  1, "south", "north"),
    ("east",   1,  0, "east",  "west"),
    ("west",  -1,  0, "west",  "east"),
]


def _fence_connects(placed_name, neighbor_state, neighbor_name):
    """Should fence `placed_name` connect to a neighbour with this state/name?"""
    if neighbor_state == 0:  # air
        return False
    if is_fence(neighbor_name):
        # Same-category check: wooden↔wooden, or nether_brick↔nether_brick
        if is_wooden_fence(placed_name) == is_wooden_fence(neighbor_name):
            return True
        return False
    # Sturdy full-cube neighbour
    return _is_sturdy(neighbor_state)


def _wall_connects(placed_name, neighbor_state, neighbor_name):
    """Should wall `placed_name` connect to a neighbour with this state/name?"""
    if neighbor_state == 0:
        return False
    if is_wall(neighbor_name):
        return True
    if neighbor_name == "iron_bars":
        return True
    return _is_sturdy(neighbor_state)


def _pane_connects(placed_name, neighbor_state, neighbor_name):
    """Panes connect to: same-pane-type blocks and sturdy neighbours.
    Vanilla's IronBarsBlock connects to anything with a sturdy face or
    another IronBarsBlock; stained-glass-panes connect to same-type
    panes via the same code path. We approximate: any pane/iron_bars
    connects to any other pane/iron_bars."""
    if neighbor_state == 0:
        return False
    if is_pane(neighbor_name):
        return True
    return _is_sturdy(neighbor_state)


_CONNECTS_FNS = {
    "fence": _fence_connects,
    "wall":  _wall_connects,
    "pane":  _pane_connects,
}


def _block_category(name):
    if is_fence(name): return "fence"
    if is_wall(name):  return "wall"
    if is_pane(name):  return "pane"
    return None


def compute_connection_state(block_name, x, y, z, get_block_fn, default_state):
    """State id for a freshly-placed fence/wall/pane at (x, y, z), with the
    four directional connection properties set based on current neighbours.

    For walls, connections are ``low`` (simplified from vanilla's low/tall
    distinction, which depends on neighbour height we don't model).

    Returns ``default_state`` unchanged if the block has no connection
    properties (not a fence/wall/pane)."""
    cat = _block_category(block_name)
    if cat is None:
        return default_state

    connects_fn = _CONNECTS_FNS[cat]
    want = {}
    for dir_name, dx, dz, prop_on_this, _ in _DIRS:
        nx, nz = x + dx, z + dz
        nstate = get_block_fn(nx, y, nz)
        # need the neighbour's block name
        from .world import block_name_for_state
        nname = block_name_for_state(nstate)
        if connects_fn(block_name, nstate, nname):
            if cat == "wall":
                want[prop_on_this] = "low"
            else:
                want[prop_on_this] = "true"
    if not want:
        return default_state
    return state_for(block_name, want)


def collect_neighbor_updates(block_name, x, y, z, get_block_fn):
    """After placing a fence/wall/pane at (x, y, z), each horizontal
    neighbour that's ALSO a fence/wall/pane needs its connection-back
    property updated to point at the new block.

    Returns a list of ``(nx, ny, nz, new_state, neighbor_name)`` tuples
    for neighbours whose state changed. Caller sends BLOCK_UPDATE for each.
    """
    cat = _block_category(block_name)
    if cat is None:
        return []

    updates = []
    for dir_name, dx, dz, prop_on_this, prop_on_neighbour in _DIRS:
        # The neighbour in this direction:
        nx, nz = x + dx, z + dz
        nstate = get_block_fn(nx, y, nz)
        if nstate == 0:
            continue
        from .world import block_name_for_state
        nname = block_name_for_state(nstate)

        connects_fn = _CONNECTS_FNS[cat]
        # Should the NEIGHBOUR connect to us (the newly-placed block)?
        # The neighbour might be a fence, wall, or pane — use its own
        # category's rule, but only if it IS one of these.
        n_cat = _block_category(nname)
        if n_cat is None:
            # neighbour is a non-connection block (e.g. sturdy stone) — it
            # doesn't need updating (its state has no connection props)
            continue
        # Use the neighbour's own connection function (e.g. a fence next to
        # a wall: the wall connects to fences, the fence connects to walls?).
        # Actually fences don't connect to walls visually, only to fences/
        # gates/solid. So use the neighbour's category's function and check
        # if IT would connect to US.
        n_connects_fn = _CONNECTS_FNS[n_cat]
        # The neighbour considers "us" (block_name) as the candidate.
        if not n_connects_fn(nname, nstate, block_name):
            continue

        # Decode neighbour's current props, set the prop pointing back at us
        current = decode_state(nname, nstate)
        if n_cat == "wall":
            new_val = "low"
        else:
            new_val = "true"
        if current.get(prop_on_neighbour) == new_val:
            continue  # already connected
        current[prop_on_neighbour] = new_val
        new_state = state_for(nname, current)
        if new_state != nstate:
            updates.append((nx, y, nz, new_state, nname))
    return updates


# ---------------------------------------------------------------------------
# Double-slab placement: clicking an existing slab of the same type with
# the matching slab item converts it to type=double instead of placing a
# new slab.
# ---------------------------------------------------------------------------

def maybe_double_slab(item_name, bx, by, bz, get_block_fn, default_state):
    """If the clicked block is the same slab type as ``item_name``, return
    the state id for ``type=double`` (so the caller can update the clicked
    block in place). Otherwise return None — caller places a new slab at
    the adjacent position as usual."""
    if not is_slab(item_name):
        return None
    if item_name not in BLOCK_STATES:
        return None
    clicked_state = get_block_fn(bx, by, bz)
    if clicked_state == 0:
        return None
    from .world import block_name_for_state
    clicked_name = block_name_for_state(clicked_state)
    if clicked_name != item_name:
        return None
    # Decode the clicked slab's current props, set type=double
    current = decode_state(clicked_name, clicked_state)
    if current.get("type") == "double":
        return None  # already double
    current["type"] = "double"
    return state_for(clicked_name, current)
