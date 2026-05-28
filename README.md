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
  (`utilities/snapshot_hardware.py`) that decodes the cube's in-line
  sub-dicts and resolves block floors via probing + binary search.
  Wire semantics in [`PROTOCOL.md`](./PROTOCOL.md) §4.5.1.
- **Upstream-script compat shim.** `pylumicube.compat` recreates the
  foundry-daemon globals (`cube`, `display`, `hsv_colour`,
  `noise_*`, colour constants, etc.). `lumicube-run script.py`
  exec's a community script in that namespace — display-only scripts
  (rainbow, rain, binary_clock, conways_game_of_life, autumn_scene,
  land_grab, lava_lamp, ripples, scrolling_clock) run unchanged.
  Sensor/audio/screen modules are warn+no-op stubs until they land.

### Todo (roughly ascending complexity)

1. **Microphone input.** Implement the `SUBSCRIBE_DEFAULT_FIELDS` +
   `PUBLISHED_FIELDS` telemetry plumbing first, then expose the
   `microphone.data` stream.
2. **Light sensor.** Colour, proximity, and gesture readings from the
   `button_and_light_sensor` board (telemetry-driven, builds on item 1).
3. **Secondary LCD screen.** Drive the `screen` module on the cube
   node. Also forces the move from a hardcoded display schema to
   runtime `ENUMERATE_FIELDS` + direct-probe discovery (see
   [`PROTOCOL.md`](./PROTOCOL.md) §5.1).
4. **FastAPI daemon.** Replace the Java `foundry-daemon` with a
   Python REST API (Swagger-documented), shipped as a systemd unit.
5. **Web frontend** for the daemon.

Protocol-side open questions tracked in [`PROTOCOL.md`](./PROTOCOL.md) §7.

## Install

From PyPI (once released):

```bash
pip install pylumicube
```

From source — with [`uv`](https://github.com/astral-sh/uv):

```bash
git clone https://github.com/chrislibuilds/pylumicube.git
cd pylumicube
uv sync
```

Or with pip in any 3.11+ venv:

```bash
git clone https://github.com/chrislibuilds/pylumicube.git
cd pylumicube
pip install -e .
```

Runtime dependencies: `pyserial` and `opensimplex` (the latter only used
by `compat.noise_2d/3d/4d` — opt out by stubbing if you don't need it).

## CLI

Two entry points ship with the package; the Java `foundry-daemon` must
not be running for either — it holds `/dev/ttyAMA0` exclusively.

### `lumicube-leds` — direct LED control

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

### `lumicube-run` — run a community script

Recreates the foundry-daemon's pre-populated globals (`cube`, `display`,
`hsv_colour`, colour constants, `noise_2d/3d/4d`, `time`/`math`/`random`,
etc.) and `exec`s the given script in that namespace, so upstream
[community scripts](https://github.com/abstractfoundry/lumicube/tree/main/community-scripts)
work unchanged.

```bash
# Run a display-only script (display + hsv_colour are hardware-backed).
lumicube-run scripts/rainbow.py
lumicube-run scripts/binary_clock.py
lumicube-run scripts/lava_lamp.py
```

Sensor / audio / screen modules (`microphone`, `speaker`, `screen`,
`buttons`, `light_sensor`, `imu`, `env_sensor`, `pi`) are warn-and-no-op
stubs for now — scripts that only poke the LED matrix run end to end;
scripts that read sensors or play sounds will print a one-time warning
per attribute and silently skip those calls.

## Library

```python
from pylumicube import LumiCube

with LumiCube('/dev/ttyAMA0') as cube:
    cube.display.fill(0x00FF00)
    cube.display.set_leds({0: 0xFF0000, 1: 0xFFFFFF, 2: 0x000000})
```

Or with the compat shim (matching the upstream daemon's script API):

```python
from pylumicube import LumiCube
from pylumicube.compat import run_script

# Open the cube and run a community script against the compat namespace.
run_script('scripts/binary_clock.py')

# Or drive things yourself using upstream-style helpers.
with LumiCube() as cube:
    from pylumicube.compat import build_globals
    ns = build_globals(cube)
    display = ns['display']
    display.set_led(0, 0, ns['red'])
    display.scroll_text('Hello', ns['cyan'])
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
    cli.py               # lumicube-leds CLI
    compat/
        runtime.py       # upstream-API shim: DisplayShim, stubs, build_globals, run_script
        font.py          # 5x7 ASCII bitmap font for scroll_text
        cli.py           # lumicube-run CLI

tests/                   # offline pytest suite
scripts/                 # upstream LumiCube community/user scripts (run via lumicube-run)
utilities/               # on-device debug + bring-up helpers
PROTOCOL.md              # canonical protocol spec
CHANGELOG.md             # versioned change history
LICENSE                  # GPL-3.0
```

## Scripts and utilities

`scripts/` contains upstream LumiCube community / user scripts (e.g.
`rainbow.py`, `binary_clock.py`, `lava_lamp.py`). Run them with
`lumicube-run <script.py>` — see the CLI section above.

`utilities/` contains helper tools used during bring-up and reverse-
engineering (require a connected cube):

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