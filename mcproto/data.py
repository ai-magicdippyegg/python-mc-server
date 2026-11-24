"""JSON data tables and derived block/item lookups.

Loaded once at import time from the data files next to ``server.py`` (or
from ``$MC_DATA_DIR`` if set). The tables are extracted from
``server-26_3.jar`` — see the project README for how each one was
produced.

Tables
------
``REGISTRIES``           synced-registry name -> list of entry names
                         (order == network id)
``TAGS``                  registry name -> {tag name -> [ids]}
``ITEM_NAMES``           list of item names, index == network item id
``ITEM_ID_BY_NAME``      inverse of ITEM_NAMES
``BLOCK_STATES``         block name -> [first_state_id, state_count,
                                          default_state_id]
``BLOCK_PROPS``          block name -> [[prop_name, [values...]], ...]
                         (sorted by name = state-index order, last prop
                         varies fastest)

Derived lookups
---------------
``ITEM_BLOCK_ALIASES``   item name -> block name to place, when the
                         names don't match (redstone -> redstone_wire,
                         wheat_seeds -> wheat, ...)
``ITEM_TO_BLOCK_STATE``  item name -> default block state id to place
``ITEM_TO_BLOCK_NAME``   item name -> block name to place
``_STATE_RANGES``        sorted [(first_state_id, block_name), ...] used
                         by ``item_for_state`` and lighting lookups
``_STATE_STARTS``        just the first_state_ids, for bisect
``_BLOCK_TO_ITEM``       reverse of ITEM_BLOCK_ALIASES
``BIOME_LIST``           list of biome names (from REGISTRIES)
``PLAINS_BIOME_ID``      network id of "plains"
``DIMENSION_TYPE_LIST``  list of dimension type names
``OVERWORLD_DIM_TYPE_ID`` network id of "overworld"
``TOTAL_BLOCK_STATES``   total block state count (35,711 in 26.3)
"""

import bisect
import json
import os

# Data files live next to server.py (the project root). ``mcproto/data.py``
# is one level down inside the ``mcproto`` package, so the project root is
# the parent of this file's directory. ``MC_DATA_DIR`` lets a deployment
# move them elsewhere.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("MC_DATA_DIR", _PROJECT_ROOT)


def _load(name):
    with open(os.path.join(DATA_DIR, name)) as f:
        return json.load(f)


REGISTRIES = _load("registries.json")
TAGS = _load("tags.json")

# Item registry: the network id is the index of the item name in the
# jar's Items registration order (extracted via javap).
ITEM_NAMES = [n.lower() for n in _load("item_ids.json")]
ITEM_ID_BY_NAME = {n: i for i, n in enumerate(ITEM_NAMES)}

# Block state table. Each entry is [first_state_id, state_count,
# default_state_id]. default_state_id is the state with every property at
# its natural default (not waterlogged, facing north, bottom half, log
# axis Y, ...) — what vanilla's getStateForPlacement returns when no
# placement context is given.
BLOCK_STATES = _load("block_states.json")  # name -> [base, count, default]

# Per-block property lists, in state-index order (last property varies
# fastest). Extracted from the jar's BlockBehaviour subclasses.
BLOCK_PROPS = _load("block_props.json")  # name -> [[prop, [values...]], ...]

# Items whose placed-block name differs from the item name.
ITEM_BLOCK_ALIASES = {
    "redstone": "redstone_wire", "wheat_seeds": "wheat", "carrot": "carrots", "potato": "potatoes",
    "beetroot_seeds": "beetroots", "melon_seeds": "melon_stem", "pumpkin_seeds": "pumpkin_stem",
    "cocoa_beans": "cocoa", "glow_berries": "cave_vines", "sweet_berries": "sweet_berry_bush",
    "string": "tripwire", "torchflower_seeds": "torchflower_crop", "pitcher_pod": "pitcher_crop",
}

# Build item -> {default block state, block name} for every item whose
# name (after alias resolution) is a known block.
ITEM_TO_BLOCK_STATE = {}
ITEM_TO_BLOCK_NAME = {}
for _item_name in ITEM_NAMES:
    _block_name = _item_name if _item_name in BLOCK_STATES else ITEM_BLOCK_ALIASES.get(_item_name)
    if _block_name in BLOCK_STATES:
        ITEM_TO_BLOCK_STATE[_item_name] = BLOCK_STATES[_block_name][2]  # default state id
        ITEM_TO_BLOCK_NAME[_item_name] = _block_name

# Inverse mappings used by lighting and pick-block.
_STATE_RANGES = sorted((v[0], k) for k, v in BLOCK_STATES.items())
_STATE_STARTS = [x[0] for x in _STATE_RANGES]
_BLOCK_TO_ITEM = {b: i for i, b in ITEM_BLOCK_ALIASES.items()}


def item_for_state(state_id):
    """Item name that picks/represents this block state, or None."""
    i = bisect.bisect_right(_STATE_STARTS, state_id) - 1
    if i < 0:
        return None
    block = _STATE_RANGES[i][1]
    if block in ITEM_ID_BY_NAME:
        return block
    return _BLOCK_TO_ITEM.get(block)


# Registry-derived constants used elsewhere.
TOTAL_BLOCK_STATES = 35711
BIOME_LIST = REGISTRIES["worldgen/biome"]
PLAINS_BIOME_ID = BIOME_LIST.index("plains")
DIMENSION_TYPE_LIST = REGISTRIES["dimension_type"]
OVERWORLD_DIM_TYPE_ID = DIMENSION_TYPE_LIST.index("overworld")


# ---------------------------------------------------------------------------
# Block state <-> {property: value} translation. Lives here (rather than
# in placement.py) so the connection / lighting / play_phase modules can
# all import it without pulling placement's player-context helpers.
# ---------------------------------------------------------------------------

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
