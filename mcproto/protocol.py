"""Protocol version constants and packet ID classes.

Packet IDs were extracted from server-26_3.jar via javap, reading the
exact registration order of
net.minecraft.network.protocol.{login,configuration,game}.GameProtocols
(packet IDs are assigned sequentially in registration order). See the
project README for the full background.
"""

PROTOCOL_VERSION = 777
GAME_VERSION_NAME = "26.3"
KNOWN_PACK_VERSION = "26.3"  # vanilla servers send the game version string here


class Login:
    # clientbound
    DISCONNECT = 0x00
    HELLO = 0x01              # encryption request (unused - offline mode)
    LOGIN_FINISHED = 0x02     # login success
    COMPRESSION = 0x03
    CUSTOM_QUERY = 0x04
    # serverbound
    S_HELLO = 0x00            # login start
    S_KEY = 0x01
    S_CUSTOM_QUERY_ANSWER = 0x02
    S_LOGIN_ACKNOWLEDGED = 0x03


class Config:
    # clientbound
    COOKIE_REQUEST = 0x00
    CUSTOM_PAYLOAD = 0x01
    DISCONNECT = 0x02
    FINISH_CONFIGURATION = 0x03
    KEEP_ALIVE = 0x04
    PING = 0x05
    RESET_CHAT = 0x06
    REGISTRY_DATA = 0x07
    RESOURCE_PACK_POP = 0x08
    RESOURCE_PACK_PUSH = 0x09
    STORE_COOKIE = 0x0B
    TRANSFER = 0x0C
    UPDATE_ENABLED_FEATURES = 0x0D
    UPDATE_TAGS = 0x0E
    SELECT_KNOWN_PACKS = 0x0F
    # serverbound
    S_CLIENT_INFORMATION = 0x00
    S_COOKIE_RESPONSE = 0x01
    S_CUSTOM_PAYLOAD = 0x02
    S_FINISH_CONFIGURATION = 0x03
    S_KEEP_ALIVE = 0x04
    S_PONG = 0x05
    S_RESOURCE_PACK = 0x06
    S_SELECT_KNOWN_PACKS = 0x07
    S_CUSTOM_CLICK_ACTION = 0x08
    S_ACCEPT_CODE_OF_CONDUCT = 0x09


class Play:
    # clientbound (only the ones we use)
    BLOCK_CHANGED_ACK = 0x04
    BLOCK_UPDATE = 0x08
    GAME_EVENT = 0x27
    KEEP_ALIVE = 0x2D
    LEVEL_CHUNK_WITH_LIGHT = 0x2E
    LOGIN = 0x32
    CHUNK_BATCH_FINISHED = 0x0B
    CHUNK_BATCH_START = 0x0C
    FORGET_LEVEL_CHUNK = 0x26
    COMMANDS = 0x10
    CONTAINER_SET_SLOT = 0x14
    SET_HELD_SLOT = 0x6B
    PLAYER_ABILITIES = 0x41
    PLAYER_POSITION = 0x49       # synchronize player position (teleport)
    SET_CHUNK_CACHE_CENTER = 0x60
    SET_DEFAULT_SPAWN_POSITION = 0x63
    # serverbound (only the ones we use)
    S_ACCEPT_TELEPORTATION = 0x00
    S_CHAT_COMMAND = 0x07
    S_CHUNK_BATCH_RECEIVED = 0x0B
    S_CLIENT_INFORMATION = 0x0E
    S_CLIENT_TICK_END = 0x0D
    S_CONTAINER_CLICK = 0x12
    S_CONTAINER_CLOSE = 0x13
    S_CUSTOM_PAYLOAD = 0x16
    S_KEEP_ALIVE = 0x1C
    S_MOVE_PLAYER_POS = 0x1E
    S_MOVE_PLAYER_POS_ROT = 0x1F
    S_MOVE_PLAYER_ROT = 0x20
    S_MOVE_PLAYER_STATUS_ONLY = 0x21
    S_PICK_ITEM_FROM_BLOCK = 0x24
    S_PLAYER_ABILITIES = 0x28
    S_PLAYER_ACTION = 0x29
    S_PLAYER_COMMAND = 0x2A
    S_PLAYER_INPUT = 0x2B
    S_PLAYER_LOADED = 0x2C
    S_PUNCH = 0x2E
    S_SET_CARRIED_ITEM = 0x36
    S_SET_CREATIVE_MODE_SLOT = 0x39
    S_USE_ITEM_ON = 0x42
    S_USE_ITEM = 0x43
