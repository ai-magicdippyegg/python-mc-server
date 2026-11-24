"""Bucket item handling: place fluid from a filled bucket, pick up a
fluid source with an empty bucket.

Vanilla ``BucketItem.use()`` does its own raycast to find what the
player is looking at, then either empties the bucket (place fluid) or
picks up a fluid source block (refill bucket). Our server doesn't do
raycasting — we use the clicked block + face from
``ServerboundUseItemOnPacket`` directly, which is sufficient for the
common case of right-clicking adjacent to a block.

Behaviour (creative mode — what this server runs):
  - Place fluid (filled bucket on a non-fluid block): set the cell
    adjacent to the clicked face to the fluid source state. The bucket
    is NOT consumed (creative infinite materials).
  - Pick up fluid (empty bucket on a fluid source block): set the
    clicked cell to air and swap the held bucket for the filled variant.
  - Filled bucket on a fluid block of the same type: do nothing.
  - Empty bucket on a non-fluid block: do nothing (fall through to the
    normal placement path, which also does nothing since "bucket" isn't
    a placeable block).

Only source fluid blocks are pickable (vanilla's
``BucketPickup.pickupBlock`` checks ``getFluidState`` is a source).
Flowing fluid (level > 0) is left alone.
"""

from .data import BLOCK_STATES

# Filled bucket item name -> (fluid_block_name, fluid_source_state_id)
# State ids are read from BLOCK_STATES so they stay correct if the data
# table is ever rebuilt.
BUCKET_TO_FLUID = {
    "water_bucket":        ("water",        BLOCK_STATES["water"][2]),       # state 89  = water[level=0]
    "lava_bucket":         ("lava",         BLOCK_STATES["lava"][2]),        # state 105 = lava[level=0]
    "powder_snow_bucket":  ("powder_snow",  BLOCK_STATES["powder_snow"][2]), # state 30519
}

# All bucket item names we recognise (filled + empty + the empty bucket itself)
BUCKETS = set(BUCKET_TO_FLUID.keys()) | {"bucket"}

# Reverse: fluid source state id -> filled bucket item name (for pickup)
FLUID_SOURCE_STATES = {
    state: bucket_name for bucket_name, (_, state) in BUCKET_TO_FLUID.items()
}


def handle_bucket(item_name, bx, by, bz, px, py, pz, get_block_fn, air_state=0):
    """Decide what a bucket right-click should do.

    Args:
        item_name: held bucket item name (``'water_bucket'``, ``'bucket'``, ...)
        bx, by, bz: position of the clicked block (what the player aimed at)
        px, py, pz: position adjacent to the clicked face (where a placed
                    block would go)
        get_block_fn(x, y, z): returns the block state id at a position
        air_state: the state id that represents "empty cell" (default 0)

    Returns one of:
        ``("place", x, y, z, fluid_state)`` — place a fluid source at (x,y,z)
        ``("pickup", x, y, z, new_bucket_item)`` — pick up fluid at (x,y,z),
            swap held bucket for ``new_bucket_item``
        ``None`` — no action (water bucket on water, empty bucket on stone,
            filled bucket with no air adjacent, ...)
    """
    if item_name == "bucket":
        # Empty bucket: try to pick up the clicked block if it's a fluid source
        clicked_state = get_block_fn(bx, by, bz)
        if clicked_state in FLUID_SOURCE_STATES:
            return ("pickup", bx, by, bz, FLUID_SOURCE_STATES[clicked_state])
        return None

    if item_name in BUCKET_TO_FLUID:
        # Filled bucket: place adjacent if the adjacent cell is air
        _, fluid_state = BUCKET_TO_FLUID[item_name]
        if get_block_fn(px, py, pz) == air_state:
            return ("place", px, py, pz, fluid_state)
        return None

    return None
