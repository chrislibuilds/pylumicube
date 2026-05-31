"""SET_FIELDS batching + sequential-send tests.

The batcher used to call `flat_dictionary.serialise(candidate, specs)`
once per LED key (O(n²)) to decide where to split frames. It now uses an
incremental `SizeTracker`. These tests pin the tracker's reported size
to `len(serialise(...))` exactly across the cases the display module
actually exercises, and verify the batches it emits all serialise within
the wire budget.

A short-lived pipelined implementation of `_send_set_fields` was reverted
after on-hardware testing: the cube firmware will not respond to
concurrent SET_FIELDS service requests (the second/third one times out).
The last test in this file pins the post-revert sequential semantics so
we don't reintroduce the regression.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from pylumicube import flat_dictionary
from pylumicube.constants import MAX_UAVCAN_PAYLOAD
from pylumicube.display import (
    LED_KEY_OFFSET,
    NUM_LEDS,
    SHOW_KEY,
    Display,
    _batch_for_frame,
)
from pylumicube.metadata import display_specs


# ----- SizeTracker correctness ------------------------------------------


def _tracker_size(keys: list[int], specs) -> int:
    """Drive the tracker over `keys` and return the final size."""
    lookup = flat_dictionary._FloorIndex(specs)
    tracker = flat_dictionary.SizeTracker()
    for key in keys:
        tracker.commit(key, lookup.lookup(key))
    return tracker.size


def test_tracker_matches_serialise_single_key() -> None:
    specs = display_specs()
    values = {266: 0xFF0000}
    expected = len(flat_dictionary.serialise(values, specs))
    assert _tracker_size(sorted(values), specs) == expected


def test_tracker_matches_serialise_two_contiguous_keys() -> None:
    specs = display_specs()
    values = {266: 0x111111, 267: 0x222222}
    expected = len(flat_dictionary.serialise(values, specs))
    assert _tracker_size(sorted(values), specs) == expected


def test_tracker_matches_serialise_full_led_frame_plus_show() -> None:
    specs = display_specs()
    values = {LED_KEY_OFFSET + i: 0x654321 for i in range(NUM_LEDS)}
    values[SHOW_KEY] = 1
    expected = len(flat_dictionary.serialise(values, specs))
    assert _tracker_size(sorted(values), specs) == expected


def test_tracker_matches_serialise_sparse_keys() -> None:
    specs = display_specs()
    values = {266: 1, 300: 2, 400: 3, SHOW_KEY: 1}
    expected = len(flat_dictionary.serialise(values, specs))
    assert _tracker_size(sorted(values), specs) == expected


def test_tracker_matches_brightness_only() -> None:
    """Brightness lives at key 256 (size=1 spec), different from LEDs."""
    specs = display_specs()
    values = {256: 0x40}
    expected = len(flat_dictionary.serialise(values, specs))
    assert _tracker_size(sorted(values), specs) == expected


def test_cost_delta_does_not_commit_state() -> None:
    """Calling cost_delta without commit must leave the tracker unchanged."""
    specs = display_specs()
    lookup = flat_dictionary._FloorIndex(specs)
    tracker = flat_dictionary.SizeTracker()
    spec_266 = lookup.lookup(266)
    cost = tracker.cost_delta(266, spec_266)
    assert tracker.size == 0
    # Calling it again must return the same cost.
    assert tracker.cost_delta(266, spec_266) == cost


# ----- _batch_for_frame invariants --------------------------------------


def test_each_batch_serialises_within_budget() -> None:
    """Every batch the splitter returns must fit one UAVCAN frame."""
    specs = display_specs()
    values = {LED_KEY_OFFSET + i: 0x123456 for i in range(NUM_LEDS)}
    values[SHOW_KEY] = 1
    batches = _batch_for_frame(values, specs)
    assert batches  # not empty
    for batch in batches:
        encoded = flat_dictionary.serialise(batch, specs)
        assert len(encoded) <= MAX_UAVCAN_PAYLOAD, (
            f"batch of {len(batch)} keys encoded to {len(encoded)} bytes "
            f"(> {MAX_UAVCAN_PAYLOAD})"
        )


def test_batches_are_a_partition_of_the_input() -> None:
    """Concatenating all batch dicts must reproduce the full input."""
    specs = display_specs()
    values = {LED_KEY_OFFSET + i: 0x0F0F0F for i in range(NUM_LEDS)}
    values[SHOW_KEY] = 1
    batches = _batch_for_frame(values, specs)
    seen: dict[int, int] = {}
    for batch in batches:
        # No overlap between batches.
        assert not (seen.keys() & batch.keys())
        seen.update(batch)
    assert seen == values


def test_batches_preserve_key_ordering() -> None:
    """Within each batch keys must be sorted (consumed by serialise() in
    sorted order anyway, but the batcher emits them sorted for clarity)."""
    specs = display_specs()
    values = {LED_KEY_OFFSET + i: i for i in range(NUM_LEDS)}
    values[SHOW_KEY] = 1
    batches = _batch_for_frame(values, specs)
    flat = [k for batch in batches for k in batch]
    assert flat == sorted(flat)


def test_show_key_lands_in_last_batch() -> None:
    """SHOW (highest key) must be the last write — otherwise the firmware
    flips the panel before the LEDs are loaded."""
    specs = display_specs()
    values = {LED_KEY_OFFSET + i: 0x123456 for i in range(NUM_LEDS)}
    values[SHOW_KEY] = 1
    batches = _batch_for_frame(values, specs)
    assert SHOW_KEY in batches[-1]


def test_empty_values_yields_empty_batches() -> None:
    assert _batch_for_frame({}, display_specs()) == []


def test_full_frame_uses_at_most_three_batches() -> None:
    """A 192-LED + SHOW frame encodes to ~587 bytes; with budget=244 this
    must fit in <=3 batches. Tighter than necessary — pin to catch regression."""
    specs = display_specs()
    values = {LED_KEY_OFFSET + i: 0x010203 for i in range(NUM_LEDS)}
    values[SHOW_KEY] = 1
    batches = _batch_for_frame(values, specs)
    assert len(batches) <= 3


# ----- _send_set_fields is strictly sequential --------------------------


def test_send_set_fields_awaits_each_batch_before_submitting_the_next() -> None:
    """Each batch's ACK must be awaited before the next SET_FIELDS goes out.

    On real hardware the cube firmware drops concurrent SET_FIELDS
    service requests (later ones never get a response, so they time out).
    We previously pipelined the batches and it caused exactly that
    failure mode on a Pi running a `fill` loop. Pin the sequential
    behaviour to avoid the regression.
    """
    fake_transport = MagicMock()
    events: list[str] = []

    def submit_request(*args, **kwargs):
        events.append("submit")
        fut = MagicMock()
        fut.result.side_effect = lambda *a, **k: events.append("await") or b""
        return fut

    fake_transport.send_request.side_effect = submit_request

    display = Display(fake_transport, node_id=124)
    display.fill(0xFF0000, await_ack=True)  # multi-batch payload (192 LEDs + SHOW)

    assert events, "no submits/awaits observed"
    # The sequence must strictly alternate submit, await, submit, await, …
    expected = ["submit", "await"] * (len(events) // 2)
    assert events == expected, (
        f"expected strict submit/await alternation, got {events}"
    )


def test_send_set_fields_awaits_every_future() -> None:
    """We must call .result() on every dispatched future so caller sees errors."""
    fake_transport = MagicMock()
    futures: list = []

    def submit_request(*args, **kwargs):
        fut = MagicMock()
        fut.result.return_value = b""
        futures.append(fut)
        return fut

    fake_transport.send_request.side_effect = submit_request

    display = Display(fake_transport, node_id=124)
    display.fill(0x00FF00, await_ack=True)
    assert futures
    for fut in futures:
        fut.result.assert_called()
