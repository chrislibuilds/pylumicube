# pylumicube

Pure-Python driver for the [Abstract Foundry LumiCube](https://github.com/abstractfoundry).
Speaks the reverse-engineered wire protocol directly over `/dev/ttyAMA0`,
so you don't need the original Java `foundry-daemon` AppImage to drive
the panel. Protocol details are in [`PROTOCOL.md`](./PROTOCOL.md).

## Status

The reverse-engineered wire protocol is implemented end to end: the LED
matrix is driven directly from Python on real hardware, and upstream
LumiCube community scripts run unchanged via a compatibility shim
(verified on a LumiCube Advanced Kit, firmware shipped with AppImage
2.0.1). The roadmap below tracks progress towards full parity with the
Java `foundry-daemon`.

**Wire & transport**

- [x] **Link layer** — bidirectional PING/PONG handshake honouring the firmware's 256-PONG drain, `INITIALISE`/`INITIALISED`, 16-slot sliding window with retransmits.
- [x] **Node discovery** — passive harvest from `NODE_STATUS` broadcasts, plus a 3-stage dynamic node-ID allocator for cold-boot scenarios.
- [x] **Module discovery** — `GET_PREFERRED_NAME` to pick the `cube` base board over the `button_and_light_sensor` board.
- [x] **Schema discovery** — `ENUMERATE_FIELDS` walker (`utilities/snapshot_hardware.py`) that decodes in-line sub-dicts and resolves block floors via probing + binary search. See [`PROTOCOL.md`](./PROTOCOL.md) §4.5.1.

**Hardware modules**

- [x] **LED matrix** — `SET_FIELDS` writes covering all 192 LEDs (3 frames). Exposed as the `lumicube-leds` CLI and `LumiCube.display`.
- [x] **Upstream-script compat shim** — `pylumicube.compat` recreates the foundry-daemon globals (`cube`, `display`, `hsv_colour`, `noise_*`, colour constants, etc.). `lumicube-run script.py` execs a community script in that namespace; display-only scripts (rainbow, rain, binary_clock, conways_game_of_life, autumn_scene, land_grab, lava_lamp, ripples, scrolling_clock) run unchanged. Sensor / audio / screen modules are warn-and-no-op stubs until they land.
- [ ] **Microphone input** — `SUBSCRIBE_DEFAULT_FIELDS` + `PUBLISHED_FIELDS` telemetry plumbing, then expose `microphone.data`.
- [ ] **Light sensor** — colour, proximity, and gesture readings from the `button_and_light_sensor` board (telemetry-driven, builds on microphone).
- [ ] **Secondary LCD screen** — drive the `screen` module on the cube node. Forces the move from a hardcoded display schema to runtime `ENUMERATE_FIELDS` + direct-probe discovery (see [`PROTOCOL.md`](./PROTOCOL.md) §5.1).

**Tooling**

- [ ] **FastAPI daemon** — replace the Java `foundry-daemon` with a Python REST API (Swagger-documented), shipped as a systemd unit.
- [ ] **Web frontend** for the daemon.

Protocol-side open questions are tracked in [`PROTOCOL.md`](./PROTOCOL.md) §7.

## Install

Requirements: Python **3.11+** and access to a serial device at
3 Mbaud (`/dev/ttyAMA0` on a Pi). Runtime dependencies are `pyserial`
and `opensimplex` (the latter only used by `compat.noise_2d/3d/4d`).

### Option A — `uv` (recommended for development)

[`uv`](https://github.com/astral-sh/uv) manages the virtualenv and
lockfile for you. From a fresh clone:

```bash
git clone https://github.com/chrislibuilds/pylumicube.git
cd pylumicube
uv sync                 # creates .venv and installs runtime deps
uv sync --extra dev     # adds pytest for the test suite
uv sync --extra extras  # adds requests for scripts/digital_clock.py (optional)
```

Run commands with `uv run …` so they pick up the project venv without
you activating it:

```bash
uv run pytest
uv run lumicube-leds all FF0000
uv run lumicube-run scripts/rainbow.py
```

### Option B — classic `venv` + `pip`

Works in any 3.11+ Python install. From a fresh clone:

```bash
git clone https://github.com/chrislibuilds/pylumicube.git
cd pylumicube
python3 -m venv .venv
source .venv/bin/activate            # PowerShell: .venv\Scripts\Activate.ps1
pip install --upgrade pip
pip install -e '.[dev]'              # editable install + dev extras (pytest)
pip install -e '.[extras]'           # optional: deps for scripts/digital_clock.py
# Or combine the groups:
# pip install -e '.[dev,extras]'
```

After `activate`, the `lumicube-leds`, `lumicube-run`, and `pytest`
commands are all on your `PATH`:

```bash
pytest
lumicube-leds all FF0000
lumicube-run scripts/rainbow.py
```

### Option C — from PyPI (once released)

```bash
pip install pylumicube
```

### Pre-flight on the Raspberry Pi (optional)

A vanilla Raspberry Pi OS 13 (Bookworm/Trixie) install — including the
Lite / headless image without X or Wayland — has nothing using
`/dev/ttyAMA0`, so you can skip this section and go straight to the CLI.
The optional bits are:

- **Have you ever installed the Abstract Foundry `foundry-daemon`
  AppImage?** It holds `/dev/ttyAMA0` exclusively, so pylumicube will
  fail with `Resource busy` until you stop it:

  ```bash
  # Preferred: the user-scope service the AppImage installs.
  export XDG_RUNTIME_DIR=/run/user/$(id -u)
  systemctl --user stop foundry-daemon.service

  # Fallback if the user systemd isn't reachable (non-login SSH):
  pkill -f foundry-daemon
  ```

- **Bringing up a brand-new Pi image?** `utilities/check_pi_uart.sh`
  audits the boot config (UART enabled, serial console disabled, no
  daemon installed). Run it once to confirm the OS is ready to talk to
  the cube.

## CLI

Two console scripts ship with the package: `lumicube-leds` for direct
LED control and `lumicube-run` for executing upstream community scripts.
Prefix with `uv run` if you're using uv; activate the venv first if
you're using classic pip.

### `lumicube-leds` — direct LED control

One-shot LED writes that exit when done. Useful for diagnostics or for
piping from shell scripts.

```bash
# Solid colours
lumicube-leds all FF0000              # whole matrix red
lumicube-leds all 0000FF              # whole matrix blue
lumicube-leds off                     # turn every LED off

# Single LED by 0..191 index
lumicube-leds single 42 00FF00        # LED 42 = green

# Other options
lumicube-leds --port /dev/ttyAMA0 --debug all 0000FF
lumicube-leds --help                  # full flag list
```

Exit codes: `0` on success, `1` on any error (no cube found, bad
handshake, port busy, …). Pass `--debug` to see the link-layer logs.

### `lumicube-run` — run a community script

Recreates the foundry-daemon's pre-populated globals (`cube`, `display`,
`hsv_colour`, colour constants, `noise_2d/3d/4d`, `time`/`math`/`random`,
etc.) and `exec`s the given script in that namespace, so upstream
[community scripts](https://github.com/abstractfoundry/lumicube/tree/main/community-scripts)
work unchanged.

```bash
# Display-only scripts — fully working today.
lumicube-run scripts/rainbow.py
lumicube-run scripts/binary_clock.py
lumicube-run scripts/lava_lamp.py
lumicube-run scripts/scrolling_clock.py    # uses the built-in font

# Stop the script with Ctrl-C — the matrix is blanked on exit unless
# you pass --no-clear.
lumicube-run --no-clear scripts/rainbow.py
```

Sensor / audio / screen modules (`microphone`, `speaker`, `screen`,
`buttons`, `light_sensor`, `imu`, `env_sensor`, `pi`) are warn-and-no-op
stubs for now — scripts that only poke the LED matrix run end to end;
scripts that read sensors or play sounds will print a one-time
`RuntimeWarning` per attribute and silently skip those calls.

### Native-API scripts (`scripts/digital_clock.py`)

`scripts/digital_clock.py` is a "from scratch" example of a clock built
against the native `pylumicube.LumiCube` API (no compat shim). It shows
how to compute (x, y) → LED-index yourself and push frames via
`Display.set_leds`. Run it as a plain Python script:

```bash
uv run python scripts/digital_clock.py
# or, in an activated venv:
python scripts/digital_clock.py
```

It also works under `lumicube-run` — the runner registers its open cube
via `pylumicube.compat.get_hosted_cube()`, and the script picks that up
through `open_or_use_hosted(port)` instead of opening a second serial
connection:

```bash
uv run lumicube-run scripts/digital_clock.py
```

To make your own native-API script dual-mode-compatible, replace the
bare `with LumiCube(port) as cube:` with the helper:

```python
from pylumicube.compat import open_or_use_hosted

with open_or_use_hosted('/dev/ttyAMA0') as cube:
    cube.display.fill(0x00FF00)
```

Standalone, the helper opens and tears down a fresh `LumiCube(port)`.
Hosted by `lumicube-run`, it yields the runner's cube and leaves
teardown to the runner.

The optional weather feature uses `requests`, which is part of the
`extras` install group (`pip install -e '.[extras]'` or
`uv sync --extra extras`). To enable it, copy the example config and
edit your OpenWeatherMap API key + city ID:

```bash
cp scripts/digital_clock_config.py.example scripts/digital_clock_config.py
$EDITOR scripts/digital_clock_config.py    # set OPENWEATHERMAP_API_KEY
```

`scripts/digital_clock_config.py` is listed in `.gitignore` so your key
never lands in version control. Without the config file the clock still
runs — it just skips the weather overlay.

## Library

`pylumicube` is usable as a library two ways: the native API for direct
field-level control, and the compat shim for upstream-style scripting.

### Native API

```python
from pylumicube import LumiCube

# The context manager opens the serial port, runs the handshake,
# discovers nodes, and closes everything cleanly on exit.
with LumiCube('/dev/ttyAMA0') as cube:
    cube.display.fill(0x00FF00)                          # whole matrix green
    cube.display.set_leds({0: 0xFF0000, 1: 0xFFFFFF})    # by 0..191 index
    cube.display.set_brightness(128)                     # 0..255
```

### Compat-shim API (upstream-style)

```python
from pylumicube.compat import run_script

# One-shot: open the cube and run a community script in the
# foundry-daemon-compatible namespace.
run_script('scripts/binary_clock.py')
```

Or drive things yourself with the upstream helpers:

```python
from pylumicube import LumiCube
from pylumicube.compat import build_globals

with LumiCube() as cube:
    ns = build_globals(cube)
    display = ns['display']
    display.set_led(0, 0, ns['red'])                # (x, y) coords
    display.set_3d({(0, 0, 8): ns['cyan']}, show=True)  # 3D face coords
    display.scroll_text('Hello', ns['cyan'])        # uses built-in font
```

The compat namespace mirrors the upstream daemon's
[`api.txt`](https://github.com/abstractfoundry/lumicube/blob/main/official-documentation/api.txt):
`cube`, `display`, `buttons`, …, plus `hsv_colour`, `random_colour`,
`noise_2d/3d/4d`, `run_async`, and the colour constants.

## Testing

```bash
uv run pytest          # with uv
# or
pytest                 # with an activated venv
```

The suite is fully offline — no hardware required. It covers COBS,
CRC, framing, FlatDictionary, UAVCAN `messageId` encoding, an end-to-end
handshake against a fake serial emulator, and the compat shim's
coordinate mappings (pinned to the upstream daemon's formulae so the
shim can't silently drift).

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
scripts/                 # upstream community scripts (run via lumicube-run)
                         #   + native-API examples like digital_clock.py
utilities/               # on-device debug + bring-up helpers
PROTOCOL.md              # canonical protocol spec
CHANGELOG.md             # versioned change history
LICENSE                  # GPL-3.0
```

## Scripts and utilities

`scripts/` contains a mix of:

- **Upstream LumiCube community / user scripts** (e.g. `rainbow.py`,
  `binary_clock.py`, `lava_lamp.py`) — written against the
  foundry-daemon globals. Run with `lumicube-run <script.py>`.
- **Native-API examples** like `digital_clock.py` — use
  `pylumicube.LumiCube` directly and are launched as plain Python
  scripts (`python scripts/digital_clock.py`). They depend on the
  `[extras]` install group; see the CLI section above.

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