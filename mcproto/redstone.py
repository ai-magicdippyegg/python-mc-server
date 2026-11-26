"""Basic redstone power propagation.

Implements a simplified power grid: power sources emit 15, redstone wire
carries the signal with -1 per block decay, redstone lamps turn on when
any neighbor has power. Recomputed on every lever toggle / redstone block
placement / break.

Supported power sources:
  lever            — 15 when powered=true (toggled by right-click)
  redstone_torch   — 15 when lit=true (basic: always lit, no inverter)
  redstone_wall_torch — same
  redstone_block   — always 15

Supported power consumers:
  redstone_lamp    — lit=true when any neighbor has power

Power carrier:
  redstone_wire    — power property 0-15, carries signal with -1 decay

Simplifications (basic scope):
  - No indirect power (power through solid blocks). Only direct adjacency.
  - No tick delay. Updates are instant.
  - No wire connection visual recomputation (wires render in default
    connection state; power still flows through adjacent wires).
  - No repeaters/comparators/pistons/buttons/pressure plates.
  - Redstone torches don't invert (always lit, always emit 15).
"""

from .data import BLOCK_STATES, decode_state, state_for
from .world import AIR_STATE, block_name_for_state

# Power sources: block_name -> (property_name, on_value) or None for always-on.
# Redstone torches emit power when lit=true. The inverter logic (torch turns
# off when its attached block is powered) is handled in recompute_power.
POWER_SOURCES = {
    "lever": ("powered", "true"),
    "redstone_torch": ("lit", "true"),
    "redstone_wall_torch": ("lit", "true"),
    "redstone_block": None,  # always 15
}

# Redstone torches: the block they're attached to is determined by their
# facing property. For wall torches, it's the block in the opposite direction
# of facing. For standing torches (redstone_torch), it's the block below.
# When the attached block is powered, the torch turns OFF (inverter).
TORCH_ATTACHED = {
    "redstone_torch": "down",  # standing torch is on top of the block below
    "redstone_wall_torch": "facing",  # wall torch is on the opposite side of facing
}

# Power consumers: block_name -> (property_name, on_value)
POWER_CONSUMERS = {
    "redstone_lamp": ("lit", "true"),
}

# 6 directions for neighbor checks
_DIRS_6 = [(0, -1, 0), (0, 1, 0), (0, 0, -1), (0, 0, 1), (-1, 0, 0), (1, 0, 0)]

# Max signal travel distance (power starts at 15, decays by 1 per wire block)
_MAX_RANGE = 16


def is_redstone_block(block_name):
    """True if this block is part of the redstone power grid."""
    return (block_name in POWER_SOURCES
            or block_name in POWER_CONSUMERS
            or block_name == "redstone_wire")


def is_lever(block_name):
    return block_name == "lever"


def get_source_power(block_name, state):
    """Returns 0-15 for the power this block emits as a source.
    For redstone torches: emits 15 when lit=true, 0 when lit=false.
    The lit state is set by the inverter logic in recompute_power."""
    if block_name not in POWER_SOURCES:
        return 0
    prop = POWER_SOURCES[block_name]
    if prop is None:
        return 15  # always on (redstone_block)
    prop_name, on_value = prop
    props = decode_state(block_name, state)
    if props.get(prop_name) == on_value:
        return 15
    return 0


def _torch_attached_pos(block_name, state, x, y, z):
    """Returns the (x, y, z) of the block a redstone torch is attached to.
    For standing torches: the block below.
    For wall torches: the block in the opposite direction of facing."""
    if block_name not in TORCH_ATTACHED:
        return None
    props = decode_state(block_name, state)
    if block_name == "redstone_torch":
        return (x, y - 1, z)  # standing torch sits on the block below
    elif block_name == "redstone_wall_torch":
        facing = props.get("facing", "north")
        opposites = {"north": (0, 0, 1), "south": (0, 0, -1),
                     "east": (-1, 0, 0), "west": (1, 0, 0)}
        dx, dy, dz = opposites.get(facing, (0, 0, 1))
        return (x + dx, y + dy, z + dz)
    return None


def toggle_lever(state):
    """Toggle a lever's powered property. Returns the new state id."""
    props = decode_state("lever", state)
    props["powered"] = "true" if props.get("powered") == "false" else "false"
    return state_for("lever", props)


def recompute_power(sx, sy, sz, get_block_fn, air_state=AIR_STATE):
    """Recompute the redstone power grid around (sx, sy, sz).

    Returns a list of (x, y, z, new_state_id) tuples for blocks whose
    state changed (wire power levels, lamp lit states). The caller sends
    BLOCK_UPDATE for each and updates the world state.

    Algorithm:
      1. BFS from (sx, sy, sz) to find all redstone blocks within range.
      2. Collect power sources and their emitted power.
      3. Propagate power through wire: each wire cell gets max power from
         adjacent sources or adjacent wire (minus 1 decay).
      4. Update wire `power` property.
      5. Update lamp `lit` property based on adjacent power.
    """
    # Step 1: BFS to find all redstone blocks within range
    # We explore from the change point and chain through adjacent redstone
    # blocks. The starting point is always expanded (even if it's now air,
    # e.g. after a block was broken) so we can find redstone blocks around it.
    redstone_cells = {}  # (x,y,z) -> (block_name, state)
    visited = set()
    queue = [(sx, sy, sz, 0, True)]  # (x, y, z, dist, is_start)

    while queue:
        x, y, z, dist, is_start = queue.pop(0)
        if (x, y, z) in visited:
            continue
        if dist > _MAX_RANGE:
            continue
        visited.add((x, y, z))

        state = get_block_fn(x, y, z)
        is_rs = False
        if state != air_state:
            name = block_name_for_state(state)
            if is_redstone_block(name):
                redstone_cells[(x, y, z)] = (name, state)
                is_rs = True

        # Expand neighbors from the starting point (to find nearby redstone
        # even if the start cell is now air) and through redstone blocks
        # (to chain). Non-redstone non-start cells are NOT expanded (no
        # indirect power through solid blocks).
        if is_start or is_rs:
            for dx, dy, dz in _DIRS_6:
                nx, ny, nz = x + dx, y + dy, z + dz
                if (nx, ny, nz) not in visited:
                    queue.append((nx, ny, nz, dist + 1, False))

    if not redstone_cells:
        return []

    # Step 2: collect power sources
    source_powers = {}  # (x,y,z) -> 15
    wire_cells = set()
    lamp_cells = set()
    for cell, (name, state) in redstone_cells.items():
        power = get_source_power(name, state)
        if power > 0:
            source_powers[cell] = power
        if name == "redstone_wire":
            wire_cells.add(cell)
        if name in POWER_CONSUMERS:
            lamp_cells.add(cell)

    # Step 3: propagate power through wire
    # Each wire cell gets max power from:
    #   - adjacent power sources (15)
    #   - adjacent wire with higher power (wire_power - 1)
    # Use iterative relaxation until stable
    wire_power = {}  # (x,y,z) -> power level 0-15

    # Initialize: wire cells adjacent to sources get power 15
    for cell in wire_cells:
        x, y, z = cell
        max_power = 0
        for dx, dy, dz in _DIRS_6:
            neighbor = (x + dx, y + dy, z + dz)
            if neighbor in source_powers:
                max_power = max(max_power, source_powers[neighbor])
        if max_power > 0:
            wire_power[cell] = max_power

    # Propagate through wire (BFS-like, process highest power first)
    changed = True
    while changed:
        changed = False
        for cell in list(wire_cells):
            x, y, z = cell
            current = wire_power.get(cell, 0)
            best = current
            for dx, dy, dz in _DIRS_6:
                neighbor = (x + dx, y + dy, z + dz)
                if neighbor in wire_power:
                    neighbor_power = wire_power[neighbor]
                    if neighbor_power > 1:
                        best = max(best, neighbor_power - 1)
            if best > current:
                wire_power[cell] = best
                changed = True

    # Step 4: compute block updates
    updates = []

    # Update wire power levels
    for cell in wire_cells:
        x, y, z = cell
        name, state = redstone_cells[cell]
        new_power = wire_power.get(cell, 0)
        props = decode_state("redstone_wire", state)
        if int(props["power"]) != new_power:
            props["power"] = str(new_power)
            new_state = state_for("redstone_wire", props)
            updates.append((x, y, z, new_state))

    # Update lamp lit states
    for cell in lamp_cells:
        x, y, z = cell
        name, state = redstone_cells[cell]
        # Check if any neighbor has power
        powered = False
        for dx, dy, dz in _DIRS_6:
            neighbor = (x + dx, y + dy, z + dz)
            # Adjacent power source
            if neighbor in source_powers:
                powered = True
                break
            # Adjacent powered wire
            if neighbor in wire_power and wire_power[neighbor] > 0:
                powered = True
                break
        prop_name, on_value = POWER_CONSUMERS[name]
        props = decode_state(name, state)
        should_be = on_value if powered else ("false" if on_value == "true" else "true")
        if props.get(prop_name) != should_be:
            props[prop_name] = should_be
            new_state = state_for(name, props)
            updates.append((x, y, z, new_state))

    # Update redstone torch lit states (inverter logic)
    # A torch turns OFF when the block it's attached to is powered.
    # This is the classic redstone inverter: power the block → torch off.
    for cell, (name, state) in redstone_cells.items():
        if name not in TORCH_ATTACHED:
            continue
        x, y, z = cell
        attached = _torch_attached_pos(name, state, x, y, z)
        if attached is None:
            continue
        ax, ay, az = attached
        # Is the attached block powered? Check if any neighbor of the attached
        # block (excluding the torch itself) is a power source or powered wire.
        attached_powered = False
        for dx, dy, dz in _DIRS_6:
            nx, ny, nz = ax + dx, ay + dy, az + dz
            if (nx, ny, nz) == cell:
                continue  # don't count the torch itself
            if (nx, ny, nz) in source_powers:
                attached_powered = True
                break
            if (nx, ny, nz) in wire_power and wire_power[(nx, ny, nz)] > 0:
                attached_powered = True
                break
        # Also check if the attached block itself is a solid powered block
        # (a lever on the attached block would power it). For basic, we check
        # if the attached block IS a power source (like a lever).
        if not attached_powered:
            astate = get_block_fn(ax, ay, az)
            if astate != air_state:
                aname = block_name_for_state(astate)
                if aname in POWER_SOURCES and aname not in TORCH_ATTACHED:
                    if get_source_power(aname, astate) > 0:
                        attached_powered = True

        props = decode_state(name, state)
        should_be_lit = "false" if attached_powered else "true"
        if props.get("lit") != should_be_lit:
            props["lit"] = should_be_lit
            new_state = state_for(name, props)
            updates.append((x, y, z, new_state))

    return updates
