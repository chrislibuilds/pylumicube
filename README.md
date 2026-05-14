# pylumicube

Pure-Python driver for the [Abstract Foundry LumiCube](https://github.com/abstractfoundry).
Speaks the reverse-engineered wire protocol directly over `/dev/ttyAMA0`,
so you don't need the original Java `foundry-daemon` AppImage to drive
the panel. Protocol details are in [`PROTOCOL.md`](./PROTOCOL.md).

## Status

First milestone reached: **lighting up the LED matrix from Python works
end-to-end on real hardware** (verified 2026-05-10).

### Done

- **Link layer.** Bidirectional PING/PONG handshake with the firmware's
  256-PONG drain honoured, INITIALISE/INITIALISED, 16-slot sliding
  window with retransmits.
- **Node discovery.** Passive harvest from `NODE_STATUS` broadcasts;
  the 3-stage dynamic node-ID allocator is implemented and ready for
  cold-boot scenarios.
- **Module discovery.** `GET_PREFERRED_NAME` to pick the `cube` base
  board (which owns the LED matrix) over the `button_and_light_sensor`
  board.
- **LED matrix.** `SET_FIELDS` writes covering all 192 LEDs split
  across 3 frames. Exposed as the `lumicube-leds` CLI and the
  `LumiCube.display` API.
- **Schema discovery.** `ENUMERATE_FIELDS` walker
  (`scripts/snapshot_hardware.py`) that decodes the cube's in-line
  sub-dicts and resolves block floors via probing + binary search.
  Wire semantics in [`PROTOCOL.md`](./PROTOCOL.md) §4.5.1.

### Todo (roughly ascending complexity)

1. **Run upstream Python scripts** against pylumicube — e.g.
   [`digital_clock_v1.py`](https://github.com/abstractfoundry/lumicube/blob/main/community-scripts/digital_clock_v1.py).
   Provide a compat shim mirroring the abstractfoundry client API so
   existing community scripts work unchanged.
2. **Microphone input.** Implement the `SUBSCRIBE_DEFAULT_FIELDS` +
   `PUBLISHED_FIELDS` telemetry plumbing first, then expose the
   `microphone.data` stream.
3. **Light sensor.** Colour, proximity, and gesture readings from the
   `button_and_light_sensor` board (telemetry-driven, builds on item 2).
4. **Secondary LCD screen.** Drive the `screen` module on the cube
   node. Also forces the move from a hardcoded display schema to
   runtime `ENUMERATE_FIELDS` + direct-probe discovery (see
   [`PROTOCOL.md`](./PROTOCOL.md) §5.1).
5. **FastAPI daemon.** Replace the Java `foundry-daemon` with a
   Python REST API (Swagger-documented), shipped as a systemd unit.
6. **Web frontend** for the daemon.

Protocol-side open questions tracked in [`PROTOCOL.md`](./PROTOCOL.md) §7.

## Install

From PyPI (once released):

```bash
pip install pylumicube
```

From source — with [`uv`](https://github.com/astral-sh/uv):

```bash
uv sync
```

Or with pip in any 3.11+ venv:

```bash
pip install -e .
```

The only runtime dependency is `pyserial`.

## CLI

The entry point is `lumicube-leds` (equivalent to `python -m pylumicube.cli`).
The Java `foundry-daemon` must not be running — it holds `/dev/ttyAMA0`
exclusively.

```bash
# Set every LED to red
lumicube-leds all FF0000

# Set LED 42 to green
lumicube-leds single 42 00FF00

# Off
lumicube-leds off

# Custom port + verbose logging
lumicube-leds --port /dev/ttyAMA0 --debug all 0000FF
```

## Library

```python
from pylumicube import LumiCube

with LumiCube('/dev/ttyAMA0') as cube:
    cube.display.fill(0x00FF00)
    cube.display.set_leds({0: 0xFF0000, 1: 0xFFFFFF, 2: 0x000000})
```

## Testing

```bash
pytest tests/
```

Tests cover COBS, CRC, framing, FlatDictionary, UAVCAN messageId
encoding, and an end-to-end handshake against a fake serial emulator —
no hardware required.

## Project layout

```
src/pylumicube/
    constants.py         # protocol constants
    cobs.py              # in-place COBS (matches Java)
    crc.py               # CRC-16/CCITT-FALSE
    framing.py           # delimited frame builder + parser
    link.py              # SerialLink: handshake + sliding window
    uavcan.py            # messageId encoding/decoding
    transport.py         # Transport: transferIds, request/response
    flat_dictionary.py   # FlatDictionary TLV encoder/decoder
    metadata.py          # FieldSpec + hardcoded display schema
    allocator.py         # 3-stage dynamic node-ID allocator
    node.py              # LumiCube top-level API
    display.py           # Display module helpers
    cli.py               # CLI tool

tests/                   # offline pytest suite
scripts/                 # on-device debug + bring-up helpers
PROTOCOL.md              # canonical protocol spec
CHANGELOG.md             # versioned change history
LICENSE                  # GPL-3.0
```

## Scripts

Helper utilities under `scripts/` (require a connected cube):

- `snapshot_hardware.py` — walk every node's `ENUMERATE_FIELDS` schema
  and print one row per field. Useful as a reference dump.
- `query_key.py <key> [...]` — query a single field's metadata by
  absolute wire key.
- `dump_metadata.py`, `find_leds.py`, `probe_set_fields.py` — earlier
  debug helpers from the reverse-engineering work; kept for posterity.
- `check_pi_uart.sh` — verify a fresh Raspberry Pi OS image is ready to
  talk to the LumiCube via `/dev/ttyAMA0` (correct boot config, no
  serial console, no daemon installed, etc.).

## Compatibility

- Python 3.11 or newer.
- Linux (tested on Raspberry Pi OS Bookworm). Other POSIX platforms
  should work if you can open `/dev/ttyAMA0` at 3 Mbaud.
- LumiCube firmware as shipped with the AppImage 2.0.1 image. Older
  firmware revisions may use different field-key layouts.

## Contributing

Pull requests welcome. Please:
- Keep changes covered by `pytest tests/`.
- Update [`PROTOCOL.md`](./PROTOCOL.md) when you discover something new
  about the wire format.
- Note user-visible changes in [`CHANGELOG.md`](./CHANGELOG.md).

## License

GPL-3.0-or-later. See [`LICENSE`](./LICENSE).