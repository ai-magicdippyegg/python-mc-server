"""Per-player state: position, rotation, inventory; save/load to
``players.json``; live session registry and autosave loop.

Player entries are keyed by UUID string (offline-mode UUID derived from
the account name, so the same account gets the same entry each time),
like a real server's ``playerdata/`` folder but in one JSON file.
"""

import json
import os
import threading
import time

from .data import ITEM_ID_BY_NAME, ITEM_NAMES
from .world import MIN_Y, WORLD_HEIGHT, placed_blocks, world_lock

# ---------------------------------------------------------------------------
# Persistence — players.json next to server.py (or $MC_PLAYER_FILE)
# ---------------------------------------------------------------------------

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAYER_FILE = os.environ.get("MC_PLAYER_FILE", os.path.join(_PROJECT_ROOT, "players.json"))
players = {}  # uuid string -> {"name", "x", "y", "z", "yaw", "pitch", "inventory", ...}
players_lock = threading.Lock()


def load_players():
    if not os.path.exists(PLAYER_FILE):
        return
    try:
        with open(PLAYER_FILE) as f:
            data = json.load(f)
        with players_lock:
            players.update(data)
        print(f"[players] loaded {len(data)} saved players from {PLAYER_FILE}")
    except Exception as e:
        print(f"[players] failed to load {PLAYER_FILE}: {e}")


def save_players():
    with players_lock:
        data = json.dumps(players)
    tmp = PLAYER_FILE + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, PLAYER_FILE)


def update_player(uuid_key, name, x, y, z, yaw, pitch, inventory=None, held_slot=None):
    with players_lock:
        entry = players.get(uuid_key, {})
        entry.update({"name": name, "x": x, "y": y, "z": z, "yaw": yaw, "pitch": pitch})
        if inventory is not None:
            entry["inventory"] = {str(k): v for k, v in inventory.items()}
        if held_slot is not None:
            entry["held_slot"] = held_slot
        entry["items_version"] = len(ITEM_NAMES)
        players[uuid_key] = entry


def get_saved_inventory(uuid_key):
    """Returns (inventory_dict{slot:(name,count)}, held_slot) using only
    entries that reference a name we can actually place/represent."""
    with players_lock:
        p = players.get(uuid_key)
    if not p:
        return {}, 0
    if p.get("inventory") and p.get("items_version") != len(ITEM_NAMES):
        # saved with an older item table: the names would map to the wrong items
        print(f"[players] discarding inventory saved with an older item table for {p.get('name')}")
        return {}, 0
    raw = p.get("inventory", {})
    inv = {}
    for k, v in raw.items():
        try:
            slot = int(k)
            name, count = v[0], int(v[1])
        except (ValueError, TypeError, IndexError):
            continue
        if name not in ITEM_ID_BY_NAME or count <= 0:
            continue
        inv[slot] = (name, count)
    held = p.get("held_slot", 0)
    if not isinstance(held, int) or not (0 <= held <= 8):
        held = 0
    return inv, held


def get_saved_position(uuid_key):
    """Returns (x, y, z, yaw, pitch) or None. Rejects garbage values (NaN,
    inf, or absurdly far/deep) so a corrupt file can't strand a player."""
    with players_lock:
        p = players.get(uuid_key)
    if not p:
        return None
    try:
        x, y, z = float(p["x"]), float(p["y"]), float(p["z"])
        yaw, pitch = float(p.get("yaw", 0.0)), float(p.get("pitch", 0.0))
    except (KeyError, TypeError, ValueError):
        return None
    vals = (x, y, z, yaw, pitch)
    if any(v != v or abs(v) > 3e7 for v in vals):  # NaN / huge
        return None
    if y < MIN_Y - 64 or y > MIN_Y + WORLD_HEIGHT + 64:
        return None
    return x, y, z, yaw, pitch


# ---------------------------------------------------------------------------
# Live sessions — connected players' current state, used by autosave so a
# crash or kill loses at most one interval of inventory/position changes.
# ---------------------------------------------------------------------------

active_sessions = {}  # uuid_key -> (username, pos_state, inventory, held_slot_box)


def autosave_loop(save_interval):
    """Periodically persist world + every connected player's live state.

    Takes ``save_interval`` explicitly (rather than reading the env var
    itself) so the entry point controls the cadence and the loop is
    testable with a tiny interval.
    """
    from .world import save_world
    while True:
        time.sleep(save_interval)
        for key, (uname, pos, inv, hbox) in list(active_sessions.items()):
            update_player(key, uname, *pos, inventory=inv, held_slot=hbox[0])
        try:
            save_players()
        except Exception as e:
            print(f"[players] autosave failed: {e}")
        try:
            save_world()
            from . import DEBUG
            if DEBUG:
                print(f"[world] autosaved ({len(placed_blocks)} changed blocks)")
        except Exception as e:
            print(f"[world] autosave failed: {e}")
