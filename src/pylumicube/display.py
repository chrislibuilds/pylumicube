"""Display module helper.

Wraps SET_FIELDS service requests to the LED matrix. PROTOCOL §4.4 + §4.7
+ §4.8.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from . import flat_dictionary
from .constants import MAX_UAVCAN_PAYLOAD, TYPE_SET_FIELDS
from .metadata import display_specs
from .transport import Transport

log = logging.getLogger(__name__)


# Field key offsets on the `cube` node (verified via ENUMERATE_FIELDS).
# See metadata.py for the full layout.
LED_KEY_OFFSET = 266
NUM_LEDS = 192
SHOW_KEY = 1266


class Display:
    """High-level write API for the display module."""

    def __init__(self, transport: Transport, node_id: int) -> None:
        self._transport = transport
        self._node_id = node_id
        self._specs = display_specs()

    @property
    def node_id(self) -> int:
        return self._node_id

    # ------------------------------------------------------------------
    # LED writes
    # ------------------------------------------------------------------

    def set_leds(self, leds: Mapping[int, int], show: bool = True,
                 timeout: float = 2.0) -> None:
        """Set LED colours by 0-based index. `leds` maps index -> 0xRRGGBB."""
        values: dict[int, int] = {}
        for idx, colour in leds.items():
            if not 0 <= idx < NUM_LEDS:
                raise ValueError(f"led index {idx} out of range")
            values[LED_KEY_OFFSET + idx] = colour & 0xFFFFFF
        if show:
            values[SHOW_KEY] = 1
        self._send_set_fields(values, timeout=timeout)

    def fill(self, colour: int, show: bool = True, timeout: float = 2.0) -> None:
        leds = {i: colour for i in range(NUM_LEDS)}
        self.set_leds(leds, show=show, timeout=timeout)

    def show(self, timeout: float = 2.0) -> None:
        self._send_set_fields({SHOW_KEY: 1}, timeout=timeout)

    def set_brightness(self, brightness: int, timeout: float = 2.0) -> None:
        if not 0 <= brightness <= 255:
            raise ValueError("brightness must be 0..255")
        self._send_set_fields({256: brightness}, timeout=timeout)

    # ------------------------------------------------------------------

    def _send_set_fields(self, values: Mapping[int, int], timeout: float) -> None:
        # Split into batches that fit within one MESSAGE frame.
        batches = list(_batch_for_frame(values, self._specs))
        log.debug("set_fields: %d values across %d batch(es)", len(values), len(batches))
        for batch in batches:
            payload = flat_dictionary.serialise(batch, self._specs)
            future = self._transport.send_request(
                dest_id=self._node_id,
                type_id=TYPE_SET_FIELDS,
                payload=payload,
                timeout=timeout,
            )
            # Java returns empty body on SET_FIELDS success; we just block for ACK.
            future.result(timeout=timeout)


def _batch_for_frame(values: Mapping[int, int],
                     specs: Mapping[int, "object"]) -> list[dict[int, int]]:
    """Split a {key: value} map into chunks whose serialised FlatDictionary
    fits into one UAVCAN payload (<=245 bytes). Greedy: append keys in
    sorted order until the encoded size would exceed the budget."""
    sorted_keys = sorted(values)
    if not sorted_keys:
        return []
    batches: list[dict[int, int]] = []
    current: dict[int, int] = {}
    for key in sorted_keys:
        candidate = dict(current)
        candidate[key] = values[key]
        try:
            encoded = flat_dictionary.serialise(candidate, specs)  # type: ignore[arg-type]
        except Exception:
            # Fall back: flush and start a new batch
            if current:
                batches.append(current)
            current = {key: values[key]}
            continue
        if len(encoded) > MAX_UAVCAN_PAYLOAD:
            if current:
                batches.append(current)
            current = {key: values[key]}
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches
