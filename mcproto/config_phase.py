"""Configuration-phase protocol handler.

Drives a freshly-logged-in client through configuration: declares known
packs, sends the 32 synced registries (``registries.json``) with
``has_data=false`` so the client uses its own local data, sends the
~750 tag lists (``tags.json``) so the client can resolve
``#minecraft:...`` references in that local data, then finishes
configuration.

Two short packets ``minecraft:brand`` (``pybasic``) and
``minecraft:vanilla`` feature flag are sent first as harmless
hello/feature-marker packets.
"""

from . import DEBUG
from .connection import log
from .codec import write_bool, write_string, write_varint
from .data import REGISTRIES, TAGS
from .protocol import Config, KNOWN_PACK_VERSION


def send_registry_data(conn, registry_name, entries):
    payload = bytearray()
    payload += write_string("minecraft:" + registry_name)
    payload += write_varint(len(entries))
    for entry_id in entries:
        payload += write_string("minecraft:" + entry_id)
        payload += write_bool(False)  # no data: client uses its own local copy
    conn.send_packet(Config.REGISTRY_DATA, bytes(payload))


def send_update_tags(conn):
    """ClientboundUpdateTagsPacket (decompiled): Map<registry, NetworkPayload>,
    NetworkPayload = Map<tag name, IntList>. Without real data here, the
    client can't resolve tag references (e.g. #minecraft:infiniburn_overworld)
    inside the vanilla registry data it loads locally, and registry loading
    fails outright - this is NOT optional the way it first looked. IDs were
    computed from the jar's actual block/item/entity_type registration order
    (extracted via javap) and, for our own synced registries, their position
    in registries.json. See tags.json."""
    payload = bytearray()
    payload += write_varint(len(TAGS))
    for registry_key, tag_map in TAGS.items():
        payload += write_string(registry_key)
        payload += write_varint(len(tag_map))
        for tag_name, ids in tag_map.items():
            payload += write_string("minecraft:" + tag_name)
            payload += write_varint(len(ids))
            for i in ids:
                payload += write_varint(i)
    conn.send_packet(Config.UPDATE_TAGS, bytes(payload))


def handle_configuration(conn, addr):
    # optional brand plugin message from server (harmless if ignored)
    conn.send_packet(Config.CUSTOM_PAYLOAD,
                      write_string("minecraft:brand") + write_string("pybasic"))

    # feature flags
    conn.send_packet(Config.UPDATE_ENABLED_FEATURES,
                      write_varint(1) + write_string("minecraft:vanilla"))

    # known packs: declare we know the client's own core pack so entries in
    # Registry Data below can be sent with has_data=False
    payload = write_varint(1) + write_string("minecraft") + write_string("core") + write_string(KNOWN_PACK_VERSION)
    conn.send_packet(Config.SELECT_KNOWN_PACKS, payload)

    # wait for client's known packs response before sending registries
    while True:
        pid, r = conn.read_packet()
        if pid == Config.S_SELECT_KNOWN_PACKS:
            break
        elif pid == Config.S_CLIENT_INFORMATION:
            continue
        elif pid == Config.S_CUSTOM_PAYLOAD:
            continue
        else:
            if DEBUG:
                log(addr, f"config: ignoring packet 0x{pid:02X} while waiting for known packs")

    for registry_name, entries in REGISTRIES.items():
        send_registry_data(conn, registry_name, entries)

    send_update_tags(conn)

    conn.send_packet(Config.FINISH_CONFIGURATION, b"")

    # wait for acknowledge finish configuration
    while True:
        pid, r = conn.read_packet()
        if pid == Config.S_FINISH_CONFIGURATION:
            return
        elif DEBUG:
            log(addr, f"config: ignoring packet 0x{pid:02X} while waiting for finish ack")
