# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Performance
- `Display.set_leds` / `fill` / `show` / `set_brightness` are now async
  by default (new `await_ack: bool = False` keyword). The wire I/O runs
  on a single background worker thread, so a frame loop can compute
  frame N+1 while frame N is still being pushed. At most one frame is
  in flight at a time (firmware constraint); subsequent async calls
  block on the previous one for natural backpressure. Sync semantics
  remain available via `await_ack=True`. New `Display.flush(timeout)`
  drains the queue on demand; `LumiCube.stop()` drains it before
  closing the wire. Mirrors how the Java daemon's
  `_Display.set_leds → run_async(self.set, ...)` decouples script
  compute from wire transmission.
- `LumiCube.display` is now cached for the LumiCube's lifetime (it used
  to return a fresh `Display` per access). Required so the async
  worker thread and pending-write state survive across calls.
- `flat_dictionary.SizeTracker` — incremental encoded-size calculator
  used by `display._batch_for_frame`. Replaces the previous O(n²) split
  algorithm (which re-serialised the candidate batch on every key) with
  an O(n) one. ~22× faster on a 192-LED frame in microbench; reduces
  per-frame Python CPU dramatically on the Pi.

### Wire-protocol findings (vs. the Java daemon)
- The cube firmware does **not** service concurrent SET_FIELDS service
  requests — submitting two or three in flight at once causes all but
  the first to be dropped (no ServiceResponse, so the caller times out).
  An attempted pipelining of `_send_set_fields` was reverted; the Java
  daemon also issues SET_FIELDS strictly sequentially. Sequential
  semantics are now pinned by a unit test.

### Added
- `scripts/lava_lamp.py` — native-API rewrite of the upstream community
  script. Precomputes the 192 surface coordinates and their LED indices
  at startup, replaces the per-pixel `colorsys.hsv_to_rgb` with a
  vectorised numpy packer, and benefits from the new async display so
  noise computation overlaps the previous frame's wire push.
- `scripts/digital_clock.py` — native-API example script (does not use
  the compat shim). Renders hours, minutes, and a filling-dot seconds
  animation, with an optional OpenWeatherMap temperature overlay.
  Configurable via `scripts/digital_clock_config.py`; a template is
  checked in as `scripts/digital_clock_config.py.example`.
- `[extras]` optional-dependencies group in `pyproject.toml`:
  `requests` for the digital-clock weather fetch. Install via
  `pip install -e '.[extras]'` or `uv sync --extra extras`.
- `.gitignore` rule for `scripts/digital_clock_config.py` so per-user
  API keys never reach version control.
- `pylumicube.compat.get_hosted_cube()` and `open_or_use_hosted(port)`
  helpers — let native-API scripts run both standalone (`python my.py`)
  *and* under `lumicube-run my.py`. The runner registers its open cube
  before exec'ing the script; `open_or_use_hosted` yields that cube
  when registered, or opens a fresh `LumiCube(port)` otherwise.
  `scripts/digital_clock.py` uses this pattern.

## [0.1.3] - 2026-05-28

### Added
- `pylumicube.compat` — compatibility shim mirroring the
  `foundry_api/standard_library.py` namespace that the upstream Java
  `foundry-daemon` injects into community scripts. Provides:
  - `LumiCubeCompat` facade with `display`, plus warn+no-op stubs for
    the not-yet-implemented modules (`microphone`, `speaker`, `screen`,
    `buttons`, `light_sensor`, `imu`, `env_sensor`, `pi`).
  - `DisplayShim` with `set_all`, `set_led`, `set_leds` (accepting int,
    `(x, y)`, or `(x, y, z)` keys), `set_panel`, `set_3d`, `brightness`
    property (clamped 0..100, mapped to the wire's 0..255), and a
    built-in `scroll_text` (5x7 ASCII font, no PIL).
  - Colour constants (`black`, `red`, etc.), `hsv_colour`,
    `random_colour`, `noise_2d/3d/4d` (via `opensimplex`), `run_async`,
    and the speaker waveform sentinels.
  - `build_globals(cube)` for embedding the namespace into a custom
    runner; `run_script(path)` for end-to-end execution.
- `lumicube-run <script.py>` CLI — opens the cube, builds the compat
  namespace, exec's the script, blanks the matrix on Ctrl-C.
- Tests pinning the (x, y) → LED-index and (x, y, z) → LED-index
  mappings to the upstream daemon's formulae.

### Changed
- Runtime dependency added: `opensimplex>=0.4` (used by the noise
  helpers; the rest of pylumicube still has no extra deps).
- `utilities/` directory (formerly `scripts/`) holds the reverse-
  engineering helpers; `scripts/` now holds upstream LumiCube
  community/user scripts that run via `lumicube-run`.

## [0.1.1] - 2026-05-14

### Added
- Initial public release of `pylumicube`, ported from the
  internal `lumicube-python` working tree.
- `LumiCube` high-level API with passive node discovery, preferred-name
  resolution, and a `Display` helper for the LED matrix.
- 3-stage dynamic node-ID allocator for cold-boot scenarios.
- `lumicube-leds` CLI (`all`, `single`, `off`) for the 24x8 LED panel.
- `scripts/snapshot_hardware.py` — `ENUMERATE_FIELDS` walker that
  decodes the cube's in-line sub-dictionaries, resolves block floors
  via forward-probe disambiguation + backwards binary search, and
  optionally probes well-known display keys to surface fields the
  walk can't reach.
- `scripts/check_pi_uart.sh` — fresh-Pi-image verification (boot
  config, no serial console, no `foundry-daemon` install).
- Protocol reference documented in `PROTOCOL.md`.

### Wire-protocol findings (vs. the Java daemon)
- Cube firmware silently drops `MESSAGE` payloads ≥ 245 bytes
  (between-delimiter count = 255 trips the parser); the safe ceiling
  is 244 bytes. `MAX_UAVCAN_PAYLOAD = 244`.
- Cube emits each metadata field as its own `01 01 <value>` run-1
  rather than coalescing into run-N. Receivers must handle both.
- Variable-length string prefix is **1 byte** (max 255) on the cube
  firmware vs LEB128 on the daemon. Identical for lengths < 128.
- `ENUMERATE_FIELDS` response leading-skip is contextual (echo when
  the query lands inside a block, relative-skip-to-next-floor in a
  gap) — see `PROTOCOL.md` §4.5.1 for the disambiguation algorithm.
- The `cube` base board reports overlapping field blocks
  (speaker.data 241..352, display.led_colour 266..1264,
  display.brightness 256). SET_FIELDS routes correctly per peripheral
  regardless. Open question — see `PROTOCOL.md` §7 item 5.

[0.1.3]: https://github.com/chrislibuilds/pylumicube/releases/tag/v0.1.3
[0.1.1]: https://github.com/chrislibuilds/pylumicube/releases/tag/v0.1.1
