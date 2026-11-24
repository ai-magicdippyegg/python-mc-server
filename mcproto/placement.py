"""Block placement rules: facing-aware orientation when right-clicking a
block face with an item.

``state_for`` and ``decode_state`` (imported from :mod:`mcproto.data`)
translate between block state ids and ``{property: value}`` dicts.
``build_placement`` implements vanilla's ``getStateForPlacement`` for the
common cases — stairs/doors facing the player, logs following the clicked
face axis, slabs/stairs going top or bottom by cursor Y, wall variants of
torches/signs/etc., doors and tall plants placing both halves, beds
placing foot and head, and fence/wall/pane connections to neighbours
(delegated to :mod:`mcproto.connections`).

Known remaining limits: stairs corner shapes, rail curves, door hinge side.
"""

import math

from .connections import (
    collect_neighbor_updates,
    compute_connection_state,
    maybe_double_slab,
)
from .data import (
    BLOCK_PROPS,
    BLOCK_STATES,
    ITEM_TO_BLOCK_NAME,
    decode_state,
    state_for,
)

# Use Item On face index (0=down, 1=up, 2=north, 3=south, 4=west, 5=east)
FACE_NAMES = ["down", "up", "north", "south", "west", "east"]
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


def build_placement(item_name, bx, by, bz, face, cursor_y, yaw, pitch,
                    get_block_fn, air_state=0,
                    min_y=None, world_height=None):
    """Work out what placing item_name against block (bx,by,bz) on `face` creates.
    Returns [(x, y, z, state, block_name), ...] (several for doors/beds, plus
    neighbour-connection updates for fences/walls/panes), or None if it
    can't be placed that way.

    ``get_block_fn(x, y, z)`` returns the block state id at a position;
    ``air_state`` is the id that represents an empty cell.

    For fence/wall/pane blocks, the returned list includes the placed block
    (with connections to its current neighbours) PLUS an update entry for
    each horizontal neighbour that needs its connection-back property set
    (so a fence placed next to an existing fence updates both sides).

    For slabs, if the clicked block is the same slab type, the list contains
    a single update that converts the clicked block to ``type=double`` (no
    new block is placed at the adjacent position).

    ``min_y`` and ``world_height`` are needed to bound-check door/tall-plant
    upper halves; if not supplied they're read from :mod:`mcproto.world`.
    """
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

    # air_at() helper derived from get_block_fn for the door/bed checks below
    def air_at(x, y, z):
        return get_block_fn(x, y, z) == air_state

    # ---- Double-slab shortcut: clicking an existing slab of the same type
    # with the matching slab item converts it to type=double (no new block
    # placed at the adjacent position).
    if block.endswith("_slab"):
        double_state = maybe_double_slab(item_name, bx, by, bz, get_block_fn, state_for(block, want))
        if double_state is not None:
            return [(bx, by, bz, double_state, block)]

    placed = []
    if "half" in props and "lower" in props["half"] and "upper" in props["half"]:      # doors, tall plants
        if min_y is None or world_height is None:
            # Late import to avoid a circular import at module load time.
            from . import world as _world
            min_y = _world.MIN_Y
            world_height = _world.WORLD_HEIGHT
        if not air_at(px, py + 1, pz) or py + 1 >= min_y + world_height:
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

    # ---- Fence / wall / pane connections: recompute the placed block's
    # state with directional connections based on current neighbours,
    # and add neighbour updates so they connect back to the new block.
    if placed:
        _, _, _, placed_state, placed_name = placed[0]
        new_state = compute_connection_state(placed_name, px, py, pz, get_block_fn, placed_state)
        if new_state != placed_state:
            placed[0] = (px, py, pz, new_state, placed_name)
        # Door / bed second-half blocks don't get connection logic (they're
        # not fences/walls/panes); only fence/wall/pane blocks produce
        # neighbour updates here, so it's safe to call for all placed blocks.
        if len(placed) == 1:
            placed.extend(
                collect_neighbor_updates(placed_name, px, py, pz, get_block_fn)
            )

    return placed
