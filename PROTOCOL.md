# LumiCube Wire Protocol

Reverse-engineering notes for the protocol spoken by the Java `foundry-daemon`
to the LumiCube hardware over `/dev/ttyAMA0`. The goal is a Python
reimplementation that can replace the Java daemon for direct hardware control.

All facts here are derived from the open-source Java daemon in
[https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/).
Pointers to the canonical Java source are given throughout for cross-checking.

## Stack overview

```
+----------------------------------------------------------------+
| Application: nodes, modules, fields                            |
|   - Dynamic node-ID allocation (3-stage, UUID-keyed)           |
|   - GET_NODE_INFO / GET_PREFERRED_NAME                         |
|   - ENUMERATE_FIELDS (discover schema)                         |
|   - SET_FIELDS (write), PUBLISHED_FIELDS (read), SUBSCRIBE_*   |
|   - FlatDictionary (writes/telemetry) and                      |
|     RecursiveDictionary (metadata) wire encodings              |
+----------------------------------------------------------------+
| UAVCAN-derived transport (DroneCAN-style frames)               |
|   - 32-bit messageId discriminating broadcast/request/response |
|   - 5-bit transferId, 5-bit priority, 7-bit nodeIds            |
+----------------------------------------------------------------+
| Reliable link layer                                            |
|   - COBS + CRC16/CCITT-FALSE, 0x00 delimiter, ≤256 byte frames |
|   - 8-bit sequence, 16-slot sliding window, INIT/PING/ACK      |
+----------------------------------------------------------------+
| Physical: UART 3 Mbaud, 8N1, no parity, no flow control        |
+----------------------------------------------------------------+
```

The daemon is the *master* (and the dynamic-ID allocator); cube boards are
nodes that sit on the same serial line behind a microcontroller-side bridge.

---

## 1. Physical layer

- Device: `/dev/ttyAMA0` (RPi UART)
- Baud: **3,000,000**
- 8 data bits, **1 stop bit**, **no parity**, no flow control
- Read mode: blocking until ≥1 byte, then drained via `available()`

Source: [SerialDriver.java:59](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/SerialDriver.java#L59)

---

## 2. Link layer

### 2.1 Framing

Frames are delimited by the byte `0x00`. The bytes between two delimiters are
the **encoded frame**. Within an encoded frame:

```
|------------------- COBS-encoded region --------------------|
[ COBS overhead ][ command ][ ... body ... ][ CRC16 hi ][ CRC16 lo ]   0x00
^ index 0        ^ index 1                                              ^ delimiter
```

After **COBS-decoding** the region (which replaces the COBS overhead bytes with
the original `0x00`s), the trailing two bytes are a **CRC-16/CCITT-FALSE** of
the post-COBS bytes from index 1 (command byte) through the end including the
CRC itself; the receiver validates by recomputing the CRC over `[command +
body + crc]` and checking the result is 0.

Practical encoding recipe (transmitter):
1. Place a placeholder `0x00` at index 0 (reserved for the COBS overhead byte).
2. Write `command` at index 1, body from index 2.
3. Compute CRC16 over `[1 .. end_of_body]` and append it big-endian.
4. COBS-encode the region `[1 .. end_of_body+2]` writing the overhead byte
   at index 0 (this is the `COBS.encode(buffer, pointer=0, offset=1, length=…)`
   form used in the Java code).
5. Append `0x00` delimiter.

Maximum total framed size is **256 bytes** including the leading COBS overhead
byte and trailing `0x00`. This caps the *decoded* command+body+CRC at 254
bytes.

Source:
- [COBS.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/common/COBS.java)
- [CRC16.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/common/CRC16.java)
- [IngressThread.java:96](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/IngressThread.java#L96)
- [EgressThread.java:96](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/EgressThread.java#L96)

#### COBS variant note

The Java COBS is the standard byte-stuffing variant: at most 254 non-zero
bytes per code group; the overhead byte preceding each group equals the
distance-to-next-zero; runs of 255 are not optimised (no “zero pair elision”).
Buffers are constrained to ≤256 bytes total.

#### CRC-16/CCITT-FALSE

- Polynomial: `0x1021`
- Initial value: `0xFFFF`
- No reflection on input or output
- No final XOR
- Transmitted big-endian (high byte first)

A 256-entry lookup table is in [CRC16.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/common/CRC16.java).

### 2.2 Command codes

The byte at decoded index 1 is the link-layer command. Direction and meaning:

| Code | Name           | Direction       | Body                          |
|------|----------------|-----------------|-------------------------------|
| 0x00 | PING           | daemon → cube   | (none)                        |
| 0x1E | INITIALISE     | daemon → cube   | `version (1 B)`, `seq (1 B)`  |
| 0x2D | MESSAGE        | both ways       | UAVCAN payload + 1-B sequence |
| 0xAA | ACKNOWLEDGE    | both ways       | `seq (1 B)`                   |
| 0xB4 | INITIALISED    | cube → daemon   | (none)                        |
| 0xCC | UNINITIALISED  | cube → daemon   | (none)                        |
| 0xFF | PONG           | cube → daemon   | `version (1 B)` (must be ≥1)  |

Codes `≥ 0xA0` are *response* codes, meaning the receiver feeds them back to
its egress thread (used for ACK/initialised/uninitialised/pong feedback).
Codes `< 0xA0` are commands the receiver acts on. Code `0x2D` is the only
real data carrier; everything else is link-layer signaling.

Source: [IngressThread.java:115](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/IngressThread.java#L115),
[EgressThread.java:228](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/EgressThread.java#L228)

### 2.3 Reliable transfer (8-bit sequence + 16-slot window)

Only `MESSAGE` (0x2D) carries a sequence number; the other commands are
stateless control.

- A `MESSAGE` frame's **last decoded byte before the CRC** is the 8-bit
  sequence number (modulo 256).
- The receiver maintains a single `accept` counter (next expected seq).
  - If `seq == accept`, deliver the payload up the stack and reply
    `ACKNOWLEDGE seq`; advance `accept = (accept + 1) mod 256`.
  - Otherwise reply `ACKNOWLEDGE (accept - 1) mod 256` and drop the frame.
    (i.e. "still waiting for seq = accept".)
- The sender keeps a window of up to **16 outstanding frames**
  (`WINDOW_SIZE = 16`) in `slots[seq]`. On `ACKNOWLEDGE n`, it advances
  `head = (n + 1) mod 256` over any in-window slots, freeing them.
- Unacknowledged slots are retransmitted by walking a `cursor` over the
  window each active period (2.5 ms). Retransmission is therefore
  bandwidth-throttled and naturally slows under congestion.
- Bandwidth budget: nominally 100% of the 3 Mbaud line (the Java daemon
  exposes a `UTILISATION` constant set to `1.0`). Active-period spin =
  2.5 ms; idle-period spin (window empty, initialised) = 1 s.

Source: [EgressThread.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/EgressThread.java),
[IngressThread.handleMessage](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/IngressThread.java#L150)

### 2.4 Initialisation handshake

The daemon side starts uninitialised. Until initialised, the daemon will not
deliver any received `MESSAGE` upward (it returns `UNINITIALISED`). The
handshake is symmetric — *each side* maintains its own ingress/egress
initialised state; both must be initialised before `MESSAGE` flows in either
direction.

Egress sequence used by the daemon at startup ([EgressThread.publishBatch](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/EgressThread.java#L167)):

1. While not initialised, send `PING` once per active period for up to
   `FLUSH_COUNT = 256` consecutive PONG responses (ensuring the receiver's
   stream is drained before INITIALISE). Each `PONG` decrements the countdown.
2. When countdown reaches 0, send `INITIALISE version=1 seq=<head>`.
3. The cube replies `INITIALISED`. From that point the cube treats `seq` as
   the next acceptable sequence number, and the egress thread starts shipping
   `MESSAGE` frames from `head`.

Ingress side ([IngressThread.handleInitialise](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/IngressThread.java#L134)):

- On receiving `INITIALISE version seq` (frame total length 6 bytes,
  `version == 1`), set `accept = seq`, mark the channel initialised, reply
  `INITIALISED`.
- On receiving any other `MESSAGE` while not yet initialised, reply
  `UNINITIALISED`.

`INITIALISE` is currently always sent with `version = 1`. Counterparties that
don't speak version ≥ 1 are rejected in `feedback()`.

### 2.5 Frame size limits

- A `MESSAGE` frame must contain at least 4 bytes after COBS decoding
  (1 COBS overhead, 1 command, 2 CRC) to be considered.
- The Java daemon caps UAVCAN payload at **245 bytes** (= 256 frame
  budget − 1 COBS overhead − 1 command (0x2D) − 1 transferId − 4
  messageId − 1 sequence − 2 CRC − 1 delimiter). Enforced at
  [SerialConnector.checkPackable](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/SerialConnector.java#L141).
- **The cube firmware effectively caps payload at 244 bytes.** Its frame
  parser ([spReadIncomingForNewFrame](https://github.com/abstractfoundry/lumicube-boards/blob/main/Firmware/Libraries/AFlibrary/Src/serialProtocol.c#L730))
  silently discards frames whose between-delimiter length reaches
  `SP_MAX_PACKET_SIZE = 255`. A 245-byte payload yields a 255-byte
  between-delimiter frame and trips that check. Verified empirically on
  2026-05-10: 244-byte payloads are ACKed, 245-byte payloads disappear.
  The Python daemon uses 244 (`MAX_UAVCAN_PAYLOAD = 244`); whether the
  Java daemon ever actually emits a 245-byte payload in practice is
  unverified (it may be that COBS overhead nudges its 245-byte payloads
  below 255 between delimiters, but treat 244 as the safe ceiling).

---

## 3. UAVCAN-derived transport

The body of a `MESSAGE` frame (after the link-layer command byte and before
the trailing sequence + CRC) is:

```
[ transferId (1 B) ][ messageId (4 B, little-endian) ][ payload (0..245 B) ]
```

This is a **single-frame** UAVCAN-style transfer. The system never fragments
across link-layer frames; payloads that don't fit in a single frame are
instead split at the application layer (e.g. `SET_FIELDS` issues multiple
requests with consecutive `transferId`s — see §4.4).

Source: [SerialConnector.writer](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/SerialConnector.java#L160),
[SerialConnector.receiveFrame](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/SerialConnector.java#L87)

### 3.1 Field layouts

`transferId` is in the low 5 bits (bits 0–4 of the byte); upper bits are
ignored on receive but typically zero on transmit.

The 32-bit `messageId` (little-endian on the wire) packs three different
formats discriminated by the **request bit (bit 7)** and, for broadcasts,
**sourceId == 0**.

#### 3.1.1 Broadcast (named, sourceId ≠ 0)

```
bit  31           24 23                            8 7 6                0
    | priority(5) |  | typeId16 (16 bits)          | |0| sourceId(7)    |
                                                       ^^ request bit = 0
```

Encoding: `messageId = (sourceId & 0x7F) | ((typeId & 0xFFFF) << 8) | ((priority & 0x1F) << 24)`

#### 3.1.2 Anonymous broadcast (sourceId == 0, typeId 2 bits)

```
bit  31           24 23     10 9 8 7 6                0
    | priority(5) |          | typ2| |0| sourceId == 0|
```

Encoding: `messageId = ((typeId & 0x3) << 8) | ((priority & 0x1F) << 24)`

This restricted form is used in the early stages of dynamic node-ID
allocation, before a node has been given a real ID.

#### 3.1.3 Service request

```
bit  31           24 23           16 15  14         8 7 6                0
    | priority(5) |  | typeId8 (8) | |1| destId(7)   |1| sourceId(7)     |
                                     ^^ bit 15 = 1   ^^ request bit = 1
```

Encoding: `messageId = 0x8080 | (sourceId & 0x7F) | ((destId & 0x7F) << 8) | ((typeId & 0xFF) << 16) | ((priority & 0x1F) << 24)`

#### 3.1.4 Service response

```
bit  31           24 23           16 15  14         8 7 6                0
    | priority(5) |  | typeId8 (8) | |0| destId(7)   |1| sourceId(7)     |
                                     ^^ bit 15 = 0   ^^ request bit = 1
```

Encoding: `messageId = 0x80 | (sourceId & 0x7F) | ((destId & 0x7F) << 8) | ((typeId & 0xFF) << 16) | ((priority & 0x1F) << 24)`

#### Decoding (received frame)

```python
mid = msg_id_bytes_le
src = byte0 & 0x7F
prio = byte3 & 0x1F
if (byte0 & 0x80) == 0:  # broadcast
    if src == 0:
        type_id = byte1 & 0x03
        anonymous_broadcast(...)
    else:
        type_id = byte1 | (byte2 << 8)
        broadcast(src, type_id, ...)
else:  # service
    dest = byte1 & 0x7F
    type_id = byte2
    if (byte1 & 0x80) == 0:
        response(src, dest, type_id, ...)
    else:
        request(src, dest, type_id, ...)
```

Note: the byte at offset `byte3` (priority byte) **also has bit 7 set** for
service-only on top of priority? — no. In the daemon code, the priority byte
only carries 5 bits of priority; bits 5–7 are unused. (Some UAVCAN dialects
use higher bits for service flags; here those flags live in `byte0` and
`byte1`.)

### 3.2 Transfer IDs

- Broadcasts: per-(source, typeId) **monotonic 5-bit counter** (`BroadcastTable`,
  16-bit-keyed; transfer ID wraps at 32). Used by the receiver only for
  duplicate suppression / ordering — not strictly required for correctness on
  this transport because the link layer is reliable.
- Services: **claimed/released** in a 32-bit-wide slot per `(destId, typeId)`,
  expiring after 5 s. Allows up to 32 concurrent in-flight requests per
  `(destId, typeId)` pair. The receiver returns the same transferId in the
  response so the requester can match it to a continuation.

Source: [Node.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/Node.java),
[BroadcastTable.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/BroadcastTable.java),
[ServiceTable.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/ServiceTable.java)

### 3.3 Priority

5-bit unsigned number. Lower numbers are higher priority in classic UAVCAN.
The Java daemon uses `20` for almost all application traffic and (also) `20`
for allocation broadcasts. The cube does not currently use priority for
arbitration in any observed way — it is set and forwarded but otherwise inert.

### 3.4 Daemon node ID

The daemon must have a non-anonymous ID in `[1..127]` because it is the
allocator. The convention used elsewhere in the source: the daemon allocates
**125** as its own ID. Cube boards get 1, 2, 3, … as they appear (the
allocator counts down from 125 looking for the first free slot — see
[Allocator.allocateId](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/Allocator.java#L135)).

---

## 4. Application layer

### 4.1 Type IDs

From [TypeId.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/TypeId.java):

| Kind      | Name                       | TypeId     | Notes |
|-----------|----------------------------|-----------:|-------|
| Broadcast | `ALLOCATION`               |          1 | Dynamic ID allocation (anonymous and named) |
| Broadcast | `NODE_STATUS`              |        341 | Periodic per-node status (DroneCAN-style) |
| Broadcast | `PUBLISHED_FIELDS`         |     20 000 | Telemetry: a subset of node's fields |
| Service   | `GET_NODE_INFO`            |          1 | UUID, name, uptime, health, mode |
| Service   | `SUBSCRIBE_DEFAULT_FIELDS` |        200 | Renew telemetry subscription |
| Service   | `GET_PREFERRED_NAME`       |        202 | Module's preferred name (UTF-8) |
| Service   | `ENUMERATE_FIELDS`         |        204 | Discover field schema |
| Service   | `SET_FIELDS`               | 216..231 (incl.) | Write fields. The 16 IDs are used round-robin per request batch (a multi-frame `SET_FIELDS` uses consecutive type IDs). |

### 4.2 Dynamic node-ID allocation (3-stage)

Adapted from DroneCAN/UAVCAN dynamic node-ID allocation, but only the
*allocator* side is in the daemon. Anonymous broadcasts to typeId
`ALLOCATION` (= 1) carry a UUID exchange:

- **Stage 1 (length 7):** allocatee sends `[ flags, uuid[0..6] ]` with the
  low bit of `flags` = 1 ("new query"). Allocator replies the same way with
  `flags = 0` and echoing the same 6 bytes of UUID.
- **Stage 2 (length 7):** allocatee sends `[ 0, uuid[6..12] ]`. Allocator
  echoes `[ 0, uuid[6..12] ]`.
- **Stage 3 (length 5):** allocatee sends `[ flags, uuid[12..16] ]` where
  `flags >> 1` is the requested ID (or 0 = "any"). Allocator replies
  `[ allocatedId<<1, uuid[12..16] ]` — now 17 bytes total payload `[hdr,
  uuid_full]`.

UUID byte order in the exchange is the same as the byte order returned by
`GET_NODE_INFO` (UUIDs as observed in Redis: e.g.
`35004000-0f51-3033-3839-353700000000`, which is the STM32 UID rendered in
the conventional 8-4-4-4-12 hex form; the bytes go on the wire in the
natural order shown in that string).

The allocator must serialise stages: it tracks a `cursor` and rejects
out-of-order continuations. A 500 ms timeout resets the cursor.

Source: [Allocator.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/Allocator.java)

### 4.3 GET_NODE_INFO response

DroneCAN `uavcan.protocol.GetNodeInfo` subset; daemon ignores most of it.
Layout (from [NodeInfo.deserialise](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/NodeInfo.java#L48)):

| Offset | Size | Field |
|-------:|-----:|-------|
| 0      |    4 | uptime seconds (LE) |
| 4      |    1 | `health<<6 | mode<<3 | sub_mode` (sub_mode unused) |
| 5..23  |   19 | (ignored — vendor-specific status, software/hardware version) |
| 24     |   16 | UUID (raw bytes) |
| 40     |    1 | certificate length `L` |
| 41     |    L | (ignored) |
| 41+L   |  ≤80 | name (US-ASCII; e.g. `com.abstractfoundry.cube`) |

Request payload is empty.

### 4.4 SET_FIELDS

Service request; payload is a **FlatDictionary**-encoded set of `(key,
value)` pairs targeting the destination node. The destination is the node
that owns the module; the `module` concept is purely a daemon-side grouping
of (node, field-key-range).

Big writes are split into multiple consecutive requests using consecutive
`typeId`s in the range 216..231 (16 IDs cycled). The Java daemon uses one
batch of up to 32 fields per request and ships up to 10 requests per call
(`MAX_KEYS / BATCH_CAPACITY = 320 / 32`).

Response payload: empty (success/timeout signaled at the link/UAVCAN level).

Source: [SetFieldsMethod.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/server/method/node/SetFieldsMethod.java)

### 4.5 ENUMERATE_FIELDS

Service request; payload is a **dictionary skip query** that walks the
node's metadata namespace one entry at a time. The query format used by the
daemon (`QueryMetadataTask.issueQuery`):

```
0x12  k0_lo  k0_hi    # skip-to keyAccumulator = k0   (skip command, 2-byte param)
0x01  0x01             # 1-element run starts here
0x12  k1_lo  k1_hi    # within sub-dictionary, skip-to k1
```

The cube also accepts the simpler form `[skip k0][run-1][no inner skip]`
(omitting the trailing `0x12 0x00 0x00`), and the minimum widths
`0x11 k0` for `k0 ≤ 255` are fine too — `utilities/query_key.py` and
`utilities/snapshot_hardware.py::build_query` both use the
narrowest-width form.

The response is a **RecursiveDictionary**: a single field at top-level key
`k0` whose value is a sub-dictionary (the metadata for a single field of the
node), starting at sub-dictionary key `k1`. The daemon iterates over the
node's whole field set by chaining queries: it uses the last returned `k0`
as the next start, and `subdictionary.lastKey() + 1` as the next sub-key,
repeating until the node returns nothing (`NoSuchElementException`).

#### 4.5.1 Response leading-skip semantics (cube firmware)

The cube's response always replays the query's outer structure
(`skip k0, run-1, value`) with the value filled in. The **leading
skip in the response is contextual** — not a direct report of the
returned block's floor:

| Response leading skip       | Meaning |
|-----------------------------|---------|
| `0`                         | queried at exactly the block's floor |
| equals queried `k0`         | **echo** — `k0` is inside a block; floor is somewhere ≤ `k0`, unspecified |
| differs from queried `k0`   | **gap** — no block at `k0`; the next block's floor is `k0 + skip` |

The unfortunate corollary: when `skip == k0`, the wire bytes are
ambiguous between an echo (in-block at unknown floor) and a gap whose
next floor happens to equal `2 × k0`. Disambiguate by probing one key
forward: under gap interpretation the response at `k0+1` still points
at the same `next_floor` so its rel decreases by 1, under echo
interpretation rel tracks the query and equals `k0+1`. Concrete
algorithm in `utilities/snapshot_hardware.py::_classify` /
`_resolve_floor`.

For walking the schema this means **you cannot rely on the leading
skip alone to learn a block's floor**. After landing inside an echo
case, binary-search backwards in `[max(0, k0 - span + 1), k0]`,
classifying each probe the same way; gaps that point at the target
block reveal the floor directly (`mid + rel`).

The sub-dictionary itself is emitted **in-line, with no length
prefix** — it consumes everything after the outer `01 01`. This
diverges from the LEB128-prefixed RecursiveDictionary the Java daemon
emits.

Each metadata sub-dictionary is keyed by the **bootstrap field IDs** in
[BootstrapMetadata.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/BootstrapMetadata.java):

| Key | Name        | Type           | Size | Meaning |
|----:|-------------|----------------|-----:|---------|
| 0   | `name`      | UTF8_STRING    | var  | field name within module (e.g. `red`, `pixel_r_3`) |
| 1   | `type`      | UINT           | 1    | FieldType enum — see §4.7 |
| 2   | `size`      | UINT           | 1    | bytes per scalar value (0 = variable) |
| 3   | `span`      | UINT           | 4    | how many consecutive keys this field occupies (≥1) |
| 4   | `gettable`  | BOOLEAN        | 1    | readable from node |
| 5   | `settable`  | BOOLEAN        | 1    | writable to node |
| 6   | `idempotent`| BOOLEAN        | 1    | safe to retry |
| 7   | `min_value` | UINT           | var  | |
| 8   | `max_value` | UINT           | var  | |
| 9   | `units`     | UTF8_STRING    | var  | |
| 10  | `debug`     | BOOLEAN        | 1    | debug-only field |
| 11  | `system`    | BOOLEAN        | 1    | system field (hidden from user namespace) |
| 12  | `module`    | UTF8_STRING    | var  | module name override (else node's preferred name) |

A field with `span > 1` declares a **block** of identically-typed keys
starting at the floor key — e.g. a 64-pixel display might declare a single
metadata entry at floor key 1024 with `span = 192` (3 colour channels × 64).

### 4.6 SUBSCRIBE_DEFAULT_FIELDS / PUBLISHED_FIELDS

To get telemetry, the daemon issues a `SUBSCRIBE_DEFAULT_FIELDS` request to
each node with payload `[ duration_seconds, bandwidth_bytes_per_sec ]` (each
1 byte). The node then **broadcasts** `PUBLISHED_FIELDS` (typeId 20 000)
periodically with a FlatDictionary-encoded snapshot of its publishable
fields, until the subscription expires. The daemon renews subscriptions on
its heartbeat.

Source: [SubscribeDefaultFieldsTask.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/heartbeat/SubscribeDefaultFieldsTask.java)

### 4.7 FieldType enum

From [FieldType.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/FieldType.java):

| Code | Name          | Encoding |
|-----:|---------------|----------|
| 0    | `NULL`        | sized null block (size bytes per element) |
| 1    | `RAW`         | unsigned integer, fixed `size` bytes LE |
| 2    | `ENUM`        | signed integer, fixed `size` bytes LE |
| 3    | `BOOLEAN`     | signed integer, fixed `size` bytes LE; 0 = false |
| 4    | `UINT`        | unsigned integer, fixed `size` bytes LE |
| 5    | `INT`         | signed integer, fixed `size` bytes LE |
| 6    | `FLOAT`       | IEEE-754, currently `size = 4` only |
| 7    | `TIME`        | signed integer (treated like INT) |
| 8    | `UTF8_CHAR`   | one byte per char (used inside fixed-size string blocks) |
| 9    | `UTF8_STRING` | length-prefixed string (size = 0); length is varint, see §4.8 |
| 10   | `DICTIONARY`  | nested RecursiveDictionary (size = 0) |

### 4.8 FlatDictionary wire encoding

A flat dictionary is a stream of TLV-style commands that walk a virtual
key cursor (`keyAccumulator` starting at 0) and emit values along the way.

Each command starts with a byte:

```
   bit 7 6 5 4 3 2 1 0
       d d d d w w w w
   d = discriminator (4 bits)
   w = parameter width in bytes (4 bits, must be 0..4)
```

The next `w` bytes are a little-endian unsigned integer `parameter`.

Discriminators:
- `0` — **Run**: emit `parameter` consecutive values starting at the current
  `keyAccumulator`. Values are encoded according to the metadata for the
  current key (see §4.9 below). Within a run, the encoding may switch
  between sub-blocks if the run crosses metadata `floor` boundaries.
- `1` — **Skip**: advance `keyAccumulator` by `parameter`. Used to jump to
  a key that's not `keyAccumulator` (the only legal "0 skip" is at the very
  start of the dictionary).
- `2` — **End-of-dictionary** (cube firmware only). Defined as
  `AF_COMMAND_DISCRIMINATOR_END_DICTIONARY` in `serialProtocol.c`; the
  Java daemon's deserialiser throws on any discriminator other than 0
  or 1, so it is presumably never emitted on the wire by either side.
  Implementations targeting both should not emit it and should reject
  it on receive.

**Daemon vs cube emission style.** The Java daemon coalesces adjacent
values into a single run-N command and then emits N values back-to-back.
The cube firmware (`afFieldProtocol.c::createMetaDataPacket`) emits each
metadata field separately as `01 01 <value>` (a fresh run-1 per field).
Both are legal under this format — receivers must decode either way.
When parsing cube responses, do not greedily interpret `01 04` as run-4
if context says you've already started a run; it may be a run-1 followed
by a 1-byte value `0x04`.

A typical write of two non-adjacent fields (key 5 and key 7) looks like:

```
0x11 0x05    # skip 5 (width 1)
0x01 0x01    # run length 1 (width 1)
<value bytes for key 5>
0x11 0x01    # skip 1
0x01 0x01    # run length 1
<value bytes for key 7>
```

The end of the dictionary is the end of the buffer; there is no terminator.

#### Variable-length size prefix (used for strings and `size = 0` integrals)

The Java daemon uses an **LEB128-style varint**, base-128, little-endian:
each byte carries 7 bits of size; the high bit set on a byte means "more
follows". See [RecursiveDictionary.parseSize](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/RecursiveDictionary.java#L150).

The cube firmware writes a **single byte length prefix** (max 255) — see
[afFieldProtocol.c::addVariableLengthFieldDataToPacket](https://github.com/abstractfoundry/lumicube-boards/blob/main/Firmware/Libraries/AFlibrary/Src/afFieldProtocol.c#L296):

```c
addBytesToPacket(packet, &fieldDataSize, 1);   // 1-byte length, NOT a varint
```

The two encodings are byte-identical for lengths < 128 (the high bit
isn't set so the LEB128 varint is also 1 byte). For names/units/module
strings of length ≥ 128 the encodings diverge — currently untested in
either direction. Receivers should accept whichever the counterparty
emits.

### 4.9 Encoding values within a run

For each key in a run, the encoder consults the metadata at that key to
determine the value layout:

- `metadata.floor(key)` returns the floor key of the homogeneous block that
  contains this key
- `metadata.span(floor)` returns how many keys share that block
- `metadata.size(floor)` returns the bytes-per-value (0 = variable)
- `metadata.type(floor)` returns the FieldType code

Inside one run, the encoder emits up to `floor + span - currentKey` values
of the same type before re-consulting metadata for the next block. Concrete
encodings:

- Fixed-size integral types (`RAW`, `ENUM`, `BOOLEAN`, `UINT`, `INT`):
  `size` bytes LE, sign-extended on decode for the signed types.
- Fixed-size float (`FLOAT`, size = 4): IEEE-754 single, LE.
- `UTF8_CHAR` with size = 1: one byte per char (used inside string blocks
  declared as a `span` of UTF8_CHARs).
- `UTF8_STRING` with size = 0: `<varint size><utf8 bytes>` per value.
- `NULL` with size > 0: skip `size` bytes per value (placeholder padding).
- `DICTIONARY` with size = 0: a recursively-nested RecursiveDictionary.

### 4.10 RecursiveDictionary (metadata replies)

Same wire format as FlatDictionary, but values may be sub-dictionaries. On
decode, the daemon builds a `TreeMap<Integer, Object>` whose values are
either primitives or nested `RecursiveDictionary`s.

Source: [RecursiveDictionary.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/RecursiveDictionary.java)

---

## 5. Module / namespace model (daemon-side construct)

The daemon turns the per-node `(metadata, preferred_name)` pairs into a
flat **namespace** of module-name → (nodeId, fields) for the user-facing
API. This is purely a daemon construct — the wire protocol only talks
about nodes and 16-bit field keys.

Algorithm (from [Namespace.build](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/Namespace.java#L81)):

1. For each (nodeId, preferredName, metadata):
   - For each field key, take its `module` metadata if set, else default to
     the node's preferredName. Special-case: if the node's
     `GET_NODE_INFO.name` is `com.abstractfoundry.cube` and the module name
     is `buttons`, rewrite to `system_button` (TODO in source — disambiguates
     the cube-base-board buttons from optional add-on button modules).
   - Group fields under their module name.
2. If two different nodeIds claim the same module name, the namespace is
   discarded (returned empty). TODO note in source: append an FNV-1a hash
   suffix to disambiguate.

On the LumiCube hardware as observed (May 2026), the modules exposed by the
running daemon are: **`buttons`, `display`, `env_sensor`, `imu`, `light_sensor`,
`microphone`, `screen`, `speaker`** (plus a `pi` pseudo-module that's
daemon-local). Two physical UAVCAN nodes are present (UUIDs ending `…3837` and
`…3920` in the running device's Redis state).

### 5.1 Wire field-key layout on the live cube

The two UAVCAN nodes report their `GET_PREFERRED_NAME` as:

- **`cube`** (allocated ID 124 in our captures) — the base board. Owns
  `display`, `buttons`, `screen`, `speaker`, plus a `microphone` field
  set. This is the node you address for LED writes.
- **`button_and_light_sensor`** (allocated ID 125) — sensor board. Owns
  `env_sensor`, `imu`, `light_sensor`, `microphone` controls, etc. No LEDs.

On the `cube` node, what `utilities/snapshot_hardware.py` auto-walks via
`ENUMERATE_FIELDS` (2026-05-14 capture, in
`ref_data/snapshot_hardware-output.txt`):

| Floor | Span | Field                      | Type | Size | Module       |
|------:|-----:|----------------------------|------|------|--------------|
|     0 |  112 | `data`                     | INT  | 2    | microphone   |
|   112 |    1 | `enable`                   | BOOL | 1    | microphone   |
|   241 |  112 | `data`                     | INT  | 2    | speaker      |
|   266 |  999 | `led_colour`               | UINT | 3    | display      |
|  1265 |    1 | `spread_spectrum_period`   | UINT | 2    | display (sys)|
|  1266 |    1 | `show`                     | BOOL | 1    | display      |
|  3315 |    1 | `rectangle_x`              | UINT | 2    | screen       |
|  7412 |    1 | `button_pressed`           | BOOL | 1    | buttons      |
| 67413 |    1 | `rdp_level`                | UINT | 4    | (system)     |

Direct probes by key (via `utilities/query_key.py`) also surface fields
the walk steps over: `display.brightness` at **256** (UINT 1B, max
100), and per earlier captures the other display fields
(`panel_*` @ 257..261, `gamma_correction_*` @ 262..265). These exist on
the wire — `query_key.py 256` returns their full metadata — but
`ENUMERATE_FIELDS`'s "next field at floor ≥ k" walk does not visit
them, because after `speaker.data` the cube's next-pointer jumps
straight to the led_colour block (whose reported floor 266 is below
the cursor 353 the walker is at — see §4.5.1; this is the *echo*
case landing inside led_colour and binary-searching back to 266).

**The reported field blocks overlap.** speaker.data (241..352) and
display.led_colour (266..1264) cover the same keys 266..352;
display.brightness (256..256) sits inside speaker.data's range. The
overlap is consistent across queries (`query_key.py 266` returns
led_colour; `query_key.py 256` returns brightness; the snapshot at
`abs_key=353` returns led_colour). SET_FIELDS to key 266 verifiably
sets LEDs (not speaker samples), so the firmware routes writes by some
mechanism other than the published spans — possibly module-context in
the request, possibly priority by metadata order. This is currently an
open question (see §7).

`led_colour` declares span 999 (= `MAX_LEDS-1` in the firmware) so the
cube has room for up to 1000-pixel chains. On a 24×8 LumiCube only the
first 192 LED keys (266..457) are physical; writes to 458..1264 are
accepted but discarded.

**The per-module zero-based keys exposed by the Java daemon's REST API
(`display.brightness` = 0) are a daemon-side abstraction.** The wire
keys are absolute and shared with every other module on the same node,
so when re-implementing the protocol you must either query
`ENUMERATE_FIELDS` to discover them or hardcode the values above.

On the `button_and_light_sensor` node (allocated ID 125 in our captures)
the auto-walk yields 15 fields packed at keys 0..14:

| Floor | Field                          | Module       |
|------:|--------------------------------|--------------|
|     0 | `top_pressed`                  | buttons      |
|     1 | `top_pressed_count`            | buttons      |
|     2 | `middle_pressed`               | buttons      |
|     3 | `middle_pressed_count`         | buttons      |
|     4 | `bottom_pressed`               | buttons      |
|     5 | `bottom_pressed_count`         | buttons      |
|     6 | `ambient_light`                | light_sensor |
|     7 | `red`                          | light_sensor |
|     8 | `green`                        | light_sensor |
|     9 | `blue`                         | light_sensor |
|    10 | `last_gesture` (ENUM)          | light_sensor |
|    11 | `num_gestures`                 | light_sensor |
|    12 | `within_proximity`             | light_sensor |
|    13 | `num_times_within_proximity`   | light_sensor |
|    14 | `rdp_level`                    | (system)     |

---

## 6. Bring-up sequence (daemon perspective)

A minimal Python daemon needs to do this on startup:

1. Open `/dev/ttyAMA0` at 3 Mbaud 8N1, drain any in-flight bytes.
2. Run the link-layer state machine: send `PING` until 256 `PONG`s have come
   back, then send `INITIALISE version=1 seq=<head>`. Wait for `INITIALISED`.
   The cube side is doing the same in the other direction — respond to its
   `PING`s with `PONG`s and to its `INITIALISE` with `INITIALISED`. Both
   directions must complete before `MESSAGE` traffic flows in either.
3. Reserve a node ID for the daemon itself (the Java daemon picks 1; the
   convention scans down from 125 for nodes).
4. **Discover existing nodes.** Two cases:
   - **Cold boot:** the cube boards are anonymous and broadcast typeId
     `ALLOCATION` (= 1) requests. Run the 3-stage allocator and assign
     IDs counting down from 125 over free slots.
   - **Warm restart (daemon restarted, hardware untouched):** the boards
     still have the IDs they were given by the previous daemon and won't
     re-allocate. Harvest their IDs from the source IDs of any non-
     anonymous broadcast they emit (`NODE_STATUS` typeId 341 is sent
     periodically by every initialised node and is sufficient).
   Either way, the same set of IDs eventually appears.
5. For each connected node, issue services in this order on a heartbeat:
   - `GET_NODE_INFO` (typeId 1) → record UUID + name
   - `GET_PREFERRED_NAME` (typeId 202) → record module's preferred name
   - `ENUMERATE_FIELDS` (typeId 204) chained from `(0, 0)` until exhausted →
     build the field schema
   - `SUBSCRIBE_DEFAULT_FIELDS` (typeId 200) → request telemetry
6. From then on:
   - Receive `PUBLISHED_FIELDS` broadcasts (typeId 20000) and decode them as
     FlatDictionaries against the per-node metadata to update a local
     telemetry cache.
   - Send `SET_FIELDS` (typeId 216 + cyclic offset) to drive outputs.
7. Maintain heartbeats: re-run subscription renewal on a timer, expire
   in-flight service requests after 5 s.

The **"it works" milestone** for the Python implementation is: light the
display from the `cube` node by writing its `led_colour` and `show`
fields, and observe button presses via published-fields telemetry. The
LED half is **verified working** as of 2026-05-10 (`utilities/probe_set_fields.py`,
the `lumicube-leds` CLI, and `LumiCube.display.set_leds()` all produce
real photons on the panel).

---

## 7. Open questions / TODOs for live verification

1. **NODE_STATUS (typeId 341) layout.** Still un-decoded. We harvest
   source IDs from these broadcasts for passive discovery but don't
   parse the payload (likely the standard DroneCAN
   `uavcan.protocol.NodeStatus` — uptime + health/mode/vendor_specific).
2. **Allocator stage-3 byte order.** The header byte semantics
   (`requestedId = (header & 0xFE) >> 1`, "alloc complete" = `(header & 1)`)
   match DroneCAN. Not yet exercised on the live cube — boards retain
   their previously-allocated IDs across daemon restarts, so passive
   discovery has bypassed the allocator path so far. Cold-boot test
   pending.
3. **Field metadata `min_value` / `max_value` width for FLOAT and
   variable-size fields.** Bootstrap declares them as `UINT, size =
   var`. `utilities/snapshot_hardware.py` parses them at the parent
   field's `size` bytes if seen, defaulting to 1. Verified for
   fixed-size integral fields (e.g. `display.brightness` max_value=100,
   1-byte); behaviour for FLOAT and variable-size fields untested.
4. **PUBLISHED_FIELDS framing for fields wider than one frame.**
5. **Overlapping field blocks on the cube node.** speaker.data
   (floor 241, span 112), display.led_colour (floor 266, span 999),
   and display.brightness (floor 256, span 1) all claim overlapping
   key ranges in their published metadata, yet SET_FIELDS works as
   expected at each (e.g. key 266 sets an LED, not a speaker sample).
   Hypothesis: the firmware routes by something other than the
   published spans — perhaps module-context inside SET_FIELDS, or
   metadata-order priority. Worth confirming by sending SET_FIELDS at
   a contested key (e.g. 280) and observing which peripheral
   actually changes state.
6. **SET_FIELDS concurrency limit.** The cube firmware appears to
   service exactly one SET_FIELDS request at a time. Issuing two or
   three concurrently (distinct transferIds, all in flight) leaves the
   second/third without a ServiceResponse — the requester times out.
   Pipelining `_send_set_fields` therefore failed on hardware and was
   reverted to strict per-batch round-trips. The Java daemon also
   serialises SET_FIELDS. Open question: does this hold for *other*
   service types, and is the limit firmware-wide (one outstanding
   per-peripheral?) or per-type? Worth probing with a mixed
   SET_FIELDS + GET_FIELDS burst.

A realistic capture path: stop the Java daemon (`systemctl --user stop
foundry-daemon.service`; if the user systemd manager isn't reachable in
your shell, `export XDG_RUNTIME_DIR=/run/user/$(id -u)` first), then run
the Python `utilities/probe_set_fields.py` which logs every TX/RX frame at
the link layer.

---

## 8. Reference: file pointers

Canonical Java sources for each layer:

- Link layer:
  [SerialDriver.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/SerialDriver.java),
  [IngressThread.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/IngressThread.java),
  [EgressThread.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/serial/EgressThread.java),
  [COBS.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/common/COBS.java),
  [CRC16.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/common/CRC16.java)
- UAVCAN layer:
  [SerialConnector.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/SerialConnector.java),
  [Node.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/Node.java),
  [Allocator.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/Allocator.java),
  [TypeId.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/TypeId.java),
  [NodeInfo.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/uavcan/NodeInfo.java)
- Application / bus layer:
  [BootstrapMetadata.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/BootstrapMetadata.java),
  [FieldType.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/FieldType.java),
  [FlatDictionary.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/FlatDictionary.java),
  [RecursiveDictionary.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/RecursiveDictionary.java),
  [Namespace.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/Namespace.java),
  [QueriedMetadata.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/bus/QueriedMetadata.java)
- Heartbeat tasks (the bring-up choreography):
  [QueryNodeInfoTask.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/heartbeat/QueryNodeInfoTask.java),
  [QueryPreferredNamesTask.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/heartbeat/QueryPreferredNamesTask.java),
  [QueryMetadataTask.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/heartbeat/QueryMetadataTask.java),
  [SubscribeDefaultFieldsTask.java](https://github.com/abstractfoundry/lumicube-daemon/blob/main/src/main/java/com/abstractfoundry/daemon/heartbeat/SubscribeDefaultFieldsTask.java)
