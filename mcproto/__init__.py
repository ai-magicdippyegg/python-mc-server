"""
python-mc-server — a pure-Python Minecraft Java Edition server for game
version 26.3 / protocol 777.

The package is split into focused modules so individual concerns (codec,
world state, lighting, packet handling) can be edited in isolation:

  protocol       protocol version + packet ID classes
  codec          VarInt / string / NBT read & write helpers, Reader
  data           JSON data tables (registries, tags, items, block states,
                 block properties) and derived lookups
  placement      block orientation rules for right-click placement
  world          placed-block state, get/set_block, world save, chunk
                 section/column encoding
  lighting       sky/block light flood-fill + light payload encoding
  player         per-player state, position/inventory save & restore
  connection     framed Connection class + log()
  config_phase   handle_configuration + registry/tags senders
  play_phase     handle_play + chunk/teleport/join/abilities senders
"""

import os

# Re-exported so `from mcproto import DEBUG` works the same way the
# original monolithic server.py allowed.
DEBUG = os.environ.get("MC_DEBUG", "1") == "1"

PROTOCOL_VERSION = 777
GAME_VERSION_NAME = "26.3"
