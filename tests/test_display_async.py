"""Async (`await_ack=False`) write-path tests for `Display`.

Mirrors how the Java daemon's `_Display.set_leds` runs the wire I/O on a
background thread so the script's frame loop can compute frame N+1
while frame N is still being pushed. The implementation here is a
ThreadPoolExecutor with ``max_workers=1`` plus block-on-previous
backpressure (at most one frame in flight on the wire, per
PROTOCOL.md §7 item 6).

These tests pin the four properties that matter to the caller:

  1. Async submit returns immediately (a Future).
  2. The next async submit blocks until the previous one finishes
     (block-on-previous backpressure → max one frame on the wire).
  3. `flush()` drains in-flight writes.
  4. `close()` is idempotent and tears down the worker thread.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from pylumicube.display import Display


def _slow_request(latency_s: float):
    """Build a `send_request` side-effect that produces futures whose
    `result()` blocks for `latency_s` to simulate firmware ACK latency."""

    def submit_request(*args, **kwargs):
        fut = MagicMock()

        def slow_result(*a, **k):
            time.sleep(latency_s)
            return b""

        fut.result.side_effect = slow_result
        return fut

    return submit_request


def test_async_submit_returns_future_quickly() -> None:
    """`await_ack=False` must return *before* the wire write completes."""
    fake_transport = MagicMock()
    fake_transport.send_request.side_effect = _slow_request(latency_s=0.15)

    display = Display(fake_transport, node_id=124)
    try:
        t0 = time.perf_counter()
        ret = display.fill(0xFF0000)  # default await_ack=False
        elapsed = time.perf_counter() - t0
        # 192 LEDs + SHOW => 3 batches => 0.45s on the worker; the call
        # itself must return much faster than that.
        assert elapsed < 0.10, (
            f"async submit took {elapsed*1000:.0f} ms; expected <100 ms"
        )
        assert isinstance(ret, Future)
        # Eventually completes successfully.
        ret.result(timeout=2.0)
    finally:
        display.close()


def test_async_submit_returns_none_when_await_ack_true() -> None:
    fake_transport = MagicMock()
    fake_transport.send_request.side_effect = _slow_request(latency_s=0.0)

    display = Display(fake_transport, node_id=124)
    try:
        assert display.fill(0x00FF00, await_ack=True) is None
    finally:
        display.close()


def test_async_at_most_one_frame_in_flight() -> None:
    """The 2nd async call must wait for the 1st to finish before starting.

    Drive that via a barrier: the 1st frame's wire-side `result()` waits
    on a `threading.Event`; until we set it, the 2nd `set_leds` call
    must block.
    """
    fake_transport = MagicMock()
    proceed = threading.Event()
    started = threading.Event()

    def submit_request(*args, **kwargs):
        fut = MagicMock()

        def gated_result(*a, **k):
            started.set()
            proceed.wait(timeout=2.0)
            return b""

        fut.result.side_effect = gated_result
        return fut

    fake_transport.send_request.side_effect = submit_request

    display = Display(fake_transport, node_id=124)
    try:
        # First async submit returns immediately, worker starts blocking.
        display.fill(0x111111)
        assert started.wait(timeout=1.0), "worker never picked up first job"

        # Second submit: must block until we let the first finish.
        second_returned = threading.Event()

        def call_second():
            display.fill(0x222222)
            second_returned.set()

        threading.Thread(target=call_second, daemon=True).start()
        # Give the second call enough time to either block or finish.
        assert not second_returned.wait(timeout=0.25), (
            "second async call returned before the first one completed"
        )

        # Release the first frame; the second one should now proceed.
        proceed.set()
        assert second_returned.wait(timeout=2.0)
    finally:
        proceed.set()  # in case the test failed mid-way
        display.close()


def test_flush_blocks_until_pending_done() -> None:
    fake_transport = MagicMock()
    fake_transport.send_request.side_effect = _slow_request(latency_s=0.15)

    display = Display(fake_transport, node_id=124)
    try:
        t0 = time.perf_counter()
        display.fill(0xABCDEF)   # async; returns ~immediately
        # `flush` must wait the full ~0.45s for the 3 sequential batches.
        display.flush(timeout=2.0)
        elapsed = time.perf_counter() - t0
        assert elapsed >= 0.30, (
            f"flush returned in {elapsed*1000:.0f} ms; expected to wait "
            f"for ~3 × 0.15s batches"
        )
    finally:
        display.close()


def test_close_drains_pending_writes() -> None:
    """`close()` must wait for the most recent async write to land."""
    fake_transport = MagicMock()
    fake_transport.send_request.side_effect = _slow_request(latency_s=0.10)

    display = Display(fake_transport, node_id=124)
    display.fill(0x123456)
    t0 = time.perf_counter()
    display.close()
    elapsed = time.perf_counter() - t0
    # 3 batches × 0.10s = ~0.30s of wire time. close() must absorb it.
    assert elapsed >= 0.20, (
        f"close returned in {elapsed*1000:.0f} ms; expected to drain the "
        f"in-flight async write"
    )


def test_close_is_idempotent() -> None:
    fake_transport = MagicMock()
    fake_transport.send_request.side_effect = _slow_request(latency_s=0.0)

    display = Display(fake_transport, node_id=124)
    display.close()
    display.close()   # must not raise


def test_no_executor_created_for_sync_only_callers() -> None:
    """A purely sync caller should not spawn the worker thread."""
    fake_transport = MagicMock()
    fake_transport.send_request.side_effect = _slow_request(latency_s=0.0)

    display = Display(fake_transport, node_id=124)
    try:
        display.fill(0x010203, await_ack=True)
        display.show(await_ack=True)
        assert display._async_executor is None
    finally:
        display.close()


def test_show_set_brightness_have_async_knob() -> None:
    """`show` and `set_brightness` accept `await_ack` and behave the same way."""
    fake_transport = MagicMock()
    fake_transport.send_request.side_effect = _slow_request(latency_s=0.0)

    display = Display(fake_transport, node_id=124)
    try:
        # Sync paths return None.
        assert display.show(await_ack=True) is None
        assert display.set_brightness(64, await_ack=True) is None
        # Async paths return Futures.
        f1 = display.show()
        f2 = display.set_brightness(128)
        assert isinstance(f1, Future)
        assert isinstance(f2, Future)
        f2.result(timeout=2.0)
    finally:
        display.close()


def test_set_brightness_validates_range() -> None:
    display = Display(MagicMock(), node_id=124)
    try:
        with pytest.raises(ValueError):
            display.set_brightness(-1)
        with pytest.raises(ValueError):
            display.set_brightness(256)
    finally:
        display.close()
