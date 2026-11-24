#!/usr/bin/env python3
"""
Entry point for the basic Python Minecraft 26.3 server (protocol 777).

This file is intentionally thin — it only wires up the accept loop and
the shutdown/save hooks. All actual protocol handling lives in the
``mcproto`` package:

  - mcproto.protocol       packet IDs
  - mcproto.codec          VarInt / string / NBT read & write, Reader
  - mcproto.data           JSON data tables (registries, tags, items,
                            block states, block properties)
  - mcproto.placement      block orientation rules for placement
  - mcproto.world          placed-block state, get/set_block, world
                            save, chunk section/column encoding
  - mcproto.lighting       sky/block light computation
  - mcproto.player         per-player state, position/inventory save
  - mcproto.connection     framed Connection class + log()
  - mcproto.config_phase   configuration-phase handler
  - mcproto.play_phase     play-phase handler

Run with ``python3 server.py``. Env vars:

  MC_DEBUG=1|0     debug logging (default 1)
  MC_PORT=25565    listen port
  MC_HOST=0.0.0.0  listen host
  MC_SAVE_FILE, MC_SAVE_INTERVAL, MC_PLAYER_FILE, MC_DATA_DIR
                    see the relevant mcproto modules

See README.md for setup and known limits.
"""

import json
import os
import random
import signal
import socket
import threading

from mcproto import DEBUG, GAME_VERSION_NAME, PROTOCOL_VERSION
from mcproto.codec import write_string, write_uuid, write_varint
from mcproto.config_phase import handle_configuration
from mcproto.connection import Connection, log
from mcproto.player import autosave_loop, load_players, save_players
from mcproto.play_phase import handle_play
from mcproto.protocol import Login
from mcproto.world import load_world, save_world, SAVE_FILE, SAVE_INTERVAL

HOST = os.environ.get("MC_HOST", "0.0.0.0")
PORT = int(os.environ.get("MC_PORT", "25565"))


# ---------------------------------------------------------------------------
# Entity id allocator — every connected player gets a unique one for the
# join packet (ClientboundLoginPacket.entityId).
# ---------------------------------------------------------------------------

_entity_id_counter = 1000
_entity_id_lock = threading.Lock()


def next_entity_id():
    global _entity_id_counter
    with _entity_id_lock:
        _entity_id_counter += 1
        return _entity_id_counter


# ---------------------------------------------------------------------------
# Per-connection handler: handshake → status (server list ping) or
# login → configuration → play.
# ---------------------------------------------------------------------------

def handle_client(sock, addr):
    conn = Connection(sock, addr)
    try:
        pid, r = conn.read_packet()  # handshake
        if pid != 0x00:
            return
        protocol = r.read_varint()
        server_address = r.read_string()
        server_port = r.read_ushort()
        next_state = r.read_varint()

        if DEBUG:
            log(addr, f"handshake: protocol={protocol} next_state={next_state}")

        if next_state == 1:
            # status
            pid, r = conn.read_packet()  # status request
            status = {
                "version": {"name": GAME_VERSION_NAME, "protocol": PROTOCOL_VERSION},
                "players": {"max": 20, "online": 0, "sample": []},
                "description": {"text": "Basic Python MC Server (flat/creative)"},
            }
            conn.send_packet(0x00, write_string(json.dumps(status)))
            pid, r = conn.read_packet()  # ping request
            payload = r.read_bytes(8)
            conn.send_packet(0x01, payload)
            return

        if next_state != 2:
            return

        # ---- LOGIN ----
        pid, r = conn.read_packet()
        if pid != Login.S_HELLO:
            return
        username = r.read_string()
        uuid_msb, uuid_lsb = r.read_uuid()

        if DEBUG:
            log(addr, f"login start: {username}")

        # offline mode: no encryption, no compression - straight to success
        # NOTE: 26.3 added a `sessionId` UUID field after the GameProfile
        # (confirmed by decompiling ClientboundLoginFinishedPacket in the jar)
        session_id = random.getrandbits(64), random.getrandbits(64)
        conn.send_packet(Login.LOGIN_FINISHED,
                          write_uuid(uuid_msb, uuid_lsb) + write_string(username) + write_varint(0) +
                          write_uuid(session_id[0], session_id[1]))

        pid, r = conn.read_packet()
        if pid != Login.S_LOGIN_ACKNOWLEDGED:
            if DEBUG:
                log(addr, f"expected Login Acknowledged, got 0x{pid:02X}")
            return

        # ---- CONFIGURATION ----
        handle_configuration(conn, addr)

        # ---- PLAY ----
        entity_id = next_entity_id()
        uuid_key = f"{uuid_msb & 0xFFFFFFFFFFFFFFFF:016x}{uuid_lsb & 0xFFFFFFFFFFFFFFFF:016x}"
        handle_play(conn, addr, entity_id, uuid_key, username)

    except (ConnectionError, OSError) as e:
        if DEBUG:
            log(addr, f"connection error: {e}")
    except Exception as e:
        if DEBUG:
            import traceback
            log(addr, f"unexpected error: {e}")
            traceback.print_exc()
    finally:
        try:
            sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Main: load world + players, start autosave, listen forever, save on exit.
# ---------------------------------------------------------------------------

def main():
    def _handle_sigterm(signum, frame):
        raise KeyboardInterrupt()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    load_world()
    load_players()
    t = threading.Thread(target=autosave_loop, args=(SAVE_INTERVAL,), daemon=True)
    t.start()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(20)
    print(f"Basic MC 26.3 (protocol {PROTOCOL_VERSION}) server listening on {HOST}:{PORT}")
    print("DEBUG =", DEBUG, "(set MC_DEBUG=0 to quiet it down)")
    print(f"world save file: {SAVE_FILE} (autosave every {SAVE_INTERVAL}s)")
    try:
        while True:
            sock, addr = srv.accept()
            t = threading.Thread(target=handle_client, args=(sock, addr), daemon=True)
            t.start()
    except KeyboardInterrupt:
        print("shutting down")
    finally:
        print("saving world...")
        try:
            save_players()
        except Exception as e:
            print(f"failed to save players on shutdown: {e}")
        try:
            save_world()
            from mcproto.world import placed_blocks
            print(f"saved {len(placed_blocks)} changed blocks to {SAVE_FILE}")
        except Exception as e:
            print(f"failed to save on shutdown: {e}")
        srv.close()


if __name__ == "__main__":
    main()
