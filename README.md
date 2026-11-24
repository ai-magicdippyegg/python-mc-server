# Basic Minecraft 26.3 server, from scratch in Python

Pure Python, zero dependencies. Flat superflat world, creative mode, you can
place blocks. No encryption/auth (offline mode only).

## Running it

1. Put `server.py`, `registries.json`, and `tags.json` in the same folder.
2. `python3 server.py` (needs only Python 3.8+, nothing to `pip install`).
3. It listens on `0.0.0.0:25565`. In your Minecraft client, set your account
   to **offline/no Microsoft login isn't required for offline servers, but
   the server itself must be run with `online-mode` off** — since this
   server never does the Mojang session-server handshake, it only works
   with a client connecting in **offline mode**, and the client needs
   protocol 777 / version 26.3 exactly (it's the version your uploaded jar
   is).
4. This sandbox can't be reached from the internet or from your machine, so
   you need to run `server.py` yourself — either on the same machine as your
   Minecraft client (`localhost:25565`), or on a machine on your LAN.
5. Debug logging is on by default (`MC_DEBUG=1`). Set `MC_DEBUG=0` to quiet
   it. Set `MC_PORT=25566` etc. to change the port.

## How this was built (so you know what to trust)

Minecraft's wire protocol changes almost every version, and 26.3 (protocol
777) is too new to have public documentation. Instead of guessing packet
IDs, I extracted them **directly from your uploaded `server-26_3.jar`**
using `javap` (Java's bytecode disassembler) on
`net.minecraft.network.protocol.game.GameProtocols` (and the login/
configuration equivalents) — Mojang ships these deobfuscated, and packets
are registered in a fixed, sequential order, so reading that order gives
exact IDs, not guesses. I also read the actual biome/dimension/registry
folder names bundled in the jar's datapack (`registries.json`) so the
"known registries" the server declares match your exact version.

**Update:** your first run caught a real bug (client disconnected with a
"Failed to decode packet 'login_finished'" error) — turns out 26.3 added a
new `sessionId` field to that packet that didn't exist in older protocol
versions. Rather than guess again, I decompiled the actual packet classes
from your jar (`ClientboundLoginFinishedPacket`, `ClientboundLoginPacket`,
`ClientboundPlayerPositionPacket`, `ClientboundSetDefaultSpawnPositionPacket`,
the chunk/section/palette classes, `ServerboundUseItemOnPacket`, etc.) to
read their exact field order and wire types straight from Mojang's own
(deobfuscated) code, instead of relying on memory of older versions. That
turned up and fixed several more real mismatches: a new `onlineMode` field
in the join packet, the dimension-type field being a numeric registry
reference instead of a string, the teleport packet's field order being
wrong, the spawn position packet missing a dimension field, and — the
biggest one — the chunk section encoding had two extra stray bytes per
section (a missing "fluid count" field, and a palette length prefix that
modern Minecraft doesn't actually send). All of this was checked against
the actual bytecode, not guessed, and re-verified against a local test
client afterward.

**What's still best-effort** (couldn't be extracted from the jar, or
wasn't worth the time to chase down further):
- A handful of smaller packets/fields I didn't individually decompile.
  If something else disconnects you, the same approach (find the packet
  class in the jar, `javap -p -c` it, read the `STREAM_CODEC` construction)
  will nail it down exactly rather than guessing again.
- The global block state ID for stone (guessed as `1`, following the
  historical block-registration order — air is always `0`, and stone has
  been immediately after it since the 1.13 "flattening"). If blocks render
  wrong or don't render, this is the next thing to check.

I ran a full protocol simulation locally (a fake client walking through
handshake → login → configuration → play → placing a block) after each
round of fixes, so the packet framing, chunk encoding, and block-placement
logic are confirmed to work end-to-end without crashing. What I *can't*
fully verify here is real client-side rendering — that needs you, but we
now have a solid loop: you run it, tell me what breaks, I decompile that
exact packet from the jar and fix it precisely.

**Update 2:** progress, but a new failure — the game log showed the
client's own registry loading crashing with things like `Missing tag:
'minecraft:infiniburn_overworld' in 'minecraft:block'`. Turns out sending
an *empty* tags packet (which I'd treated as a "nice to have, skip it")
isn't actually optional: the client resolves `has_data=false` registry
entries from its own local vanilla data, and that local data references
things like `#minecraft:infiniburn_overworld` — the client needs the real
tag → block/item/entity list mapping from the server to resolve those
references, or the whole registry fails to load and the connection drops.

Fixing this properly meant computing real tag data: I decompiled
`Blocks`, `Items`, and `EntityTypes` from your jar the same way (reading
their registration order via javap) to get the actual global numeric IDs
for blocks/items/entity types, then read every relevant tag's JSON file
from the jar's bundled datapack and resolved them (including tags that
reference other tags) into the id lists the network format wants. That's
now baked into `tags.json` (also extracted from your exact jar, ~750 tags
across blocks/items/entity types and our synced registries) and sent for
real during configuration.

One known gap: a handful of blocks/items in this version are registered
through a grouped "family" helper (e.g. the 8 weathered-copper lightning
rod variants) rather than as plain individual fields, which my extraction
approach doesn't see through — those specific entries couldn't be resolved
to exact IDs. I left their tag declared but empty rather than omitted, so
it won't crash the connection; it just means content strictly gated on
that one tag (e.g. the `channeling` enchantment's lightning behavior)
won't recognize the affected blocks. Not worth chasing further for a
"basic" server, but flagging it so it's not a mystery if you notice it.

**Update 3:** you were right that it was smaller — down to 2 registries
this time (`timeline`, `world_clock`), both `IllegalStateException:
Unbound tags/values`. Same root cause as update 2 (registries I hadn't
declared at all), but this time, instead of guessing which registries
matter and patching them one at a time forever, I found the actual
authoritative list: I decompiled
`net.minecraft.resources.RegistryDataLoader` and read its
`SYNCHRONIZED_REGISTRIES` field directly — that's the literal list Mojang's
own client/server code uses to decide what gets synced over the network.
Turns out there are 32 synced registries in 26.3, and I'd only declared
19 of them (my original guesses from skimming the jar's datapack folder
names, which was never going to be reliable). All 32 are now in
`registries.json` and `tags.json`, so this class of error — "some registry
I forgot to declare" — should be closed out for good rather than
whack-a-mole.

**Update 4:** you're in! Joining, walking, and placing blocks all work.
Two things from your last message:

- The `0x2E`/`0x2B`/etc "ignoring packet" spam while walking was just
  noise, not a bug — those are movement-input flags, the left-click
  "arm swing" packet, and similar harmless stuff the client sends
  constantly. They're now quietly ignored instead of logged.
- Block **breaking** wasn't implemented at all yet (only placing) — that's
  why it looked broken client-side and reverted: the client was predicting
  the break optimistically but the server never confirmed it, so it
  snapped back. I decompiled `ServerboundPlayerActionPacket` for its exact
  field layout and wired up real breaking (instant, matching creative mode).
- Added world persistence: autosaves every 30s and on shutdown to
  `world_save.json`, reloaded on startup. Tested a full break → autosave →
  restart → reload round trip locally before sending this over.

**Update 5:** the "only my spawn chunk renders, but I can still break/place
blocks in the unrendered area" symptom was a real, specific bug — I'd
completely missed a whole sub-protocol. Since 1.19.4, chunk sending is
wrapped in a "batch": the server is supposed to send
`ClientboundChunkBatchStart`, then the chunk packets, then
`ClientboundChunkBatchFinished` (with a count). Without that wrapper, the
client's chunk-loading state machine never considers the chunks
"committed" — the block data is there (which is why interacting with it
worked), but it never builds a mesh for it, so it doesn't render. That's
also almost certainly why the block-outline-that-doesn't-render thing
happened — same missing signal. Added the batch start/finish around the
chunk send, confirmed locally that all 81 chunks now land between a
matching start/finish pair before the teleport packet.

Also quieted two more harmless packets from the debug log (`0x28`
flight/ability toggle, `0x2A` sprint/sneak toggle) since your log showed
them spamming too.

**Update 6:** the corrupted/garbled spawn-chunk screenshot was a real,
fairly deep bug in the chunk section encoding — specifically the path
used once a chunk has any placed/broken blocks in it (which is why it
only showed up in your spawn chunk, and only after you'd built things
there and reloaded from the save file). I'd encoded those sections as a
"direct/global" palette using 15 bits per block, which seemed reasonable,
but decompiling `PalettedContainer`'s actual read path turned up something
non-obvious: for that mode, the client doesn't use the bit-width you send
at all — it recalculates its own fixed bit-width from
`ceil(log2(total block state count))` for whatever this exact version's
block state count is, and if that number doesn't happen to match the 15 I
guessed, every subsequent block in the section gets read at the wrong bit
offset, corrupting into essentially random block types (matching your
screenshot exactly - a scrambled mix of unrelated blocks).

Rather than try to pin down that exact number (which depends on the exact
count of block states in 26.3, not something I can get without running
the game), I switched those sections to a "linear palette" instead — a
small lookup table of just the distinct blocks actually in that section
(for us, always just stone/air) plus small packed indices into it. That
mode's bit-width is never recalculated by the client, so there's no
version-specific number to get wrong. Confirmed by writing a decoder that
walks the exact bytes our server produces and checks every block lands
where it should, and by testing a second player joining a world that
already has broken/placed blocks in it (the only way to actually exercise
this path) - both come back clean now.

**Update 7:** the triangular void in your screenshots wasn't a bug — that
was just the edge of the fixed 9x9 chunk area we were sending, which the
README already flagged as a known limitation, but seeing it makes it look
more like a glitch than a deliberate limit. Rather than leave it, I built
actual chunk streaming: I decompiled `ServerboundMovePlayerPacket` for its
position fields, and now the server tracks which chunk you're in and
streams in a new batch (with the same batch start/finish wrapping as the
initial join) whenever you cross into an unloaded chunk, moving the
"loaded center" with you instead of it being fixed at spawn forever.
Tested by simulating a player walking far outside the original area and
confirming new chunks and a center update actually arrive.

(Chunk unloading was added in Update 8, below.)

**Update 8:** chunk unloading and `/spawn`.
- Whenever you cross into a new chunk, the server now also tells the client
  to forget chunks outside the 9x9 window (`ClientboundForgetLevelChunkPacket`,
  a single packed chunk position, decompiled from the jar), so client memory
  stays flat no matter how far you explore.
- `/spawn` teleports you back to the spawn point. The chat-command packet is
  just one string (no leading `/`). I also send a tiny command tree
  (`ClientboundCommandsPacket`) declaring `spawn`, so the client recognizes
  it and tab-completes it instead of treating it as unknown.
- No chat confirmation message is shown after `/spawn` — text components use
  a much more involved NBT encoding, so I left that out for "basic".

**Update 9:** player data now saves. Each player's last position and
look direction are stored in `players.json` (next to `server.py`), keyed by
UUID like a real server's playerdata. It's written when you disconnect, on
the same autosave timer as the world, and on shutdown. Rejoin and you
appear where you left off, facing the same way, and the right chunks load
around you. `/spawn` still goes to the world spawn, and leaving right after
saves you there. Delete `players.json` to reset everyone to spawn.
Override the path with `MC_PLAYER_FILE=<path>`.

One detail worth knowing: after the server teleports you on join, the
client briefly sends movement packets from its old position (0,0,0). The
server now ignores movement until the client confirms the teleport, the
same way a real server does, so those stale packets can't overwrite your
saved position or stream the wrong chunks.

**Update 10:** inventory and placing any block.
- Your inventory is tracked (the client tells the server about every creative-menu pick and
  hotbar change), saved to `players.json` per player, and sent back when you rejoin, including
  which hotbar slot you had selected.
- Right-click now places the block for the item in your hand instead of always stone. Items
  with no block form (tools, food) or an empty hand still place stone.
- This needed two new data files, `item_ids.json` and `block_states.json` (keep them next to
  `server.py`). I generated them by reading the jar's own bytecode: the item order gives each
  item's network ID, and every block's property list gives its exact range of block state IDs
  (29,231 states in total).
- Known limits: blocks with orientation (stairs, logs, slabs, doors) place in their first
  state, so they may face a default direction rather than where you aim. The dyed blocks and
  copper blocks come from a small table I wrote by hand, so a few of those could be off.
  Items carrying extra data (enchantments, custom names) aren't tracked.
- The inventory code passed my local fake-client tests, but I haven't run it in the real
  client yet.

**Update 11:** wrong blocks fixed. Placing dirt gave grass and TNT gave stone because my block
state counts ignored properties a block inherits from a parent class (grass gets `snowy` from
`SnowyBlock`, for example), so every block after it was shifted. I rebuilt the table so it
follows each block's whole parent chain, covers blocks in subpackages (pistons and others),
and handles properties added in loops (brewing stand, glow lichen, chiseled bookshelf). I
checked about 100 blocks against state counts I know (stairs 80, door 64, redstone wire 1296,
and so on). The total is now 35,711 states (it was 29,231).
- Blocks now place their natural default state: not waterlogged, facing north, bottom half,
  logs vertical. Before, the first-enumerated state had `waterlogged=true`.
- `redstone` places redstone wire, and seeds and crops place their plants.
- Your old `world_save.json` held IDs from the wrong table, so the server moves it aside as
  `world_save.json.old` and starts a fresh world. This only happens once.

**Update 12:** the real cause of the wrong blocks. Your `/calibrate` screenshots showed all 36 test
blocks were exactly right, so the block table was never the problem. The problem was the **item**
list: `Items` has the same 35 grouped collections (colour and copper items) that I had missed
for blocks, so every item ID after the first group was shifted. The client sent oak slab's real
item ID and the server read it as `warped_fence`. The item list is now 1,658 items (was 1,218).
- I rebuilt `tags.json` from the corrected block and item orders. 483 tag lists changed, and
  lists that were empty (like `lightning_rods`) now resolve.
- Copper block names now match vanilla (`exposed_copper`, not `exposed_copper_block`).
- Inventories saved with the old item list are discarded once on join, because their item
  names were decoded wrongly. Re-pick your items from the creative menu.
- `/calibrate` is removed again.

**Update 13:** joining crashed once a chunk held more than 16 different blocks (the server hit an
assertion while building the chunk). Now that you can place any block, that happens quickly. Chunk
sections now pick the right palette: a small table for up to 16 blocks, a bigger hashmap table for
17-256, and raw block IDs beyond that. I checked all three decode back correctly.

**Update 14:** right-clicking with an empty hand, or with an item that isn't a block (tools, food and
so on), now places nothing. Before, anything that wasn't a block fell back to stone.

**Update 15:** the server now follows your inventory when items leave it or move around.
- Q (drop one), Ctrl+Q (drop stack) and F (swap with offhand) update the server's copy.
- Moving, swapping and shift-clicking in the inventory screen are now tracked (the client reports
  the final slot contents, and the server adopts them).
- Middle-click pick block now works: it puts the block's item in your hotbar, or selects it if it's
  already there.
- The creative screen's "drop item" (slot -1) is ignored instead of creating a bogus slot.
- Items with extra data (enchantments, names) are now tracked by item and count instead of being
  skipped; the extra data is lost if the item is saved and restored.
- Autosave now writes connected players' live inventory and position too, not just on disconnect.

**Update 16:** basic lighting. Chunks used to be sent with no light data, so as soon as you edited one
the client recomputed light from nothing and the whole chunk went dark. The server now computes sky
light and block light and sends them with every chunk.
- Sky light is 15 in the open, drops by 1 per block spreading sideways into shade, and a covered
  pit stays dark. Glass lets it straight through, and leaves and water dim it a little.
- Torches, lanterns, glowstone, sea lanterns, lava and other emitters give block light (torch 14,
  fading 1 per block) that also spreads across chunk borders.
- The client updates light itself when you place or break a block, so no extra packets are needed
  for edits; it just needed the right starting light.
- Which blocks block or emit light is decided from the block's name, not its real shape. Stairs,
  slabs and doors are treated simply, and blocks whose glow depends on state (lit furnace,
  campfire, redstone lamp) don't glow yet.
- Chunks far from any edit use a precomputed baseline, so joining stays fast (about 1.6 s with
  150 edits spread over a 100x100 area).

**Update 17:** blocks now face the way you do when placed. This needed a new data file,
`block_props.json` (keep it next to `server.py`): every block's properties and value lists, read from
the jar's bytecode. I regenerated the block table with the same script and it matched the old one
exactly (0 differences out of 1,286 blocks).
- Stairs, fence gates and doors face the way you look. Furnaces, chests, repeaters, carved pumpkins
  and glazed terracotta face you. Anvils turn a quarter turn.
- Logs, pillars and chains follow the face you click (axis x/y/z). Slabs, stairs and trapdoors go top
  or bottom by where you click (the underside always gives top).
- Pistons, dispensers, droppers, observers and barrels follow where you look, including up and down.
  Hoppers, end rods, lightning rods and shulker boxes follow the clicked face.
- Torches become wall torches when clicked on a side (and refuse ceilings). Signs, banners and heads
  do the same on walls, and standing ones rotate to your yaw. Buttons and levers pick floor, wall or
  ceiling. Lanterns hang when placed under a block. Ladders only place on side faces.
- Doors and tall plants now place both halves, and beds place foot and head.
- Not done yet: stairs corner shapes, fence/wall/pane connections, rail curves, double slabs, and
  door hinge side.

## What it does

- Superflat world: stone from y=-64 to y=-1, air above. A 9x9 chunk area
  (~144 blocks square) loads around you at all times and streams in as you
  walk — go far enough in one direction and there's a brief pause while the
  next batch loads, same idea as a real server's view distance
- Creative mode, flying enabled
- Right-click a block face to place a block — **it always places stone**,
  regardless of what item/block you're holding (see "What's still
  best-effort" below for why)
- **Break blocks** (left-click, instant since it's creative)
- **World persistence**: changes (placed/broken blocks) autosave every 30s
  and on shutdown (Ctrl+C or a normal `kill`), to `world_save.json` next to
  `server.py`. Delete that file to reset the world. Only the diff from the
  flat terrain is stored, so it stays tiny. Change the interval with
  `MC_SAVE_INTERVAL=<seconds>`, or the save path with `MC_SAVE_FILE=<path>`.
- No lighting data is sent (client will likely show it dark/flat-lit until
  it computes its own, or you may see rendering quirks)
- Player position/rotation saved between sessions (see Update 9)
- No inventory, no other players visible to each other

## If the client doesn't connect

Run with `MC_DEBUG=1` (default) and watch the terminal — it prints every
packet ID it doesn't recognize, and any Python traceback if something
crashes. Send me:
- the last few lines it printed before disconnect, and
- whatever disconnect message/error Minecraft itself shows

and I can adjust the relevant packet's field layout from there.
