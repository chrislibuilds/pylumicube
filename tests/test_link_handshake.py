"""Link-layer handshake test using a fake serial port (no hardware).

Simulates the cube side of the wire: respond to PINGs with PONGs, accept
INITIALISE, send our own INITIALISE, accept ACK back. Verifies that
SerialLink.start() returns successfully and that submit() resolves once
the cube ACKs.
"""

from __future__ import annotations

import threading
import time

import pytest

import pylumicube.framing as framing
import pylumicube.link as link_module
from pylumicube.constants import (
    CMD_ACKNOWLEDGE,
    CMD_INITIALISE,
    CMD_INITIALISED,
    CMD_MESSAGE,
    CMD_PING,
    CMD_PONG,
    FLUSH_COUNT,
    PROTOCOL_VERSION,
)


class FakeSerial:
    """Bare-minimum substitute for serial.Serial used by SerialLink."""

    def __init__(self) -> None:
        self._rx_buf = bytearray()              # bytes the SUT will read
        self._tx_buf = bytearray()              # bytes the SUT has written
        self._lock = threading.Lock()
        self._closed = False
        # Constants used by SerialLink:
        self.timeout = 0.0025

    # SerialLink-facing API
    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx_buf.clear()

    def reset_output_buffer(self) -> None:
        with self._lock:
            self._tx_buf.clear()

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx_buf)

    def read(self, n: int) -> bytes:
        # Mimic pyserial: block until at least one byte or timeout.
        deadline = time.monotonic() + self.timeout
        while True:
            with self._lock:
                if self._closed:
                    return b""
                if self._rx_buf:
                    take = min(n, len(self._rx_buf))
                    chunk = bytes(self._rx_buf[:take])
                    del self._rx_buf[:take]
                    return chunk
            if time.monotonic() >= deadline:
                return b""
            time.sleep(0.001)

    def write(self, data: bytes) -> int:
        with self._lock:
            self._tx_buf.extend(data)
        return len(data)

    def close(self) -> None:
        with self._lock:
            self._closed = True

    # Test helpers
    def inject(self, data: bytes) -> None:
        with self._lock:
            self._rx_buf.extend(data)

    def drain_tx(self) -> bytes:
        with self._lock:
            chunk = bytes(self._tx_buf)
            self._tx_buf.clear()
        return chunk


class CubeEmulator:
    """Reads from FakeSerial.tx (what SUT writes) and writes back to its rx
    (what SUT reads). Implements the cube-side of the handshake."""

    def __init__(self, fake: FakeSerial) -> None:
        self.fake = fake
        self.stream = framing.FrameStream()
        self.cube_initialised = False
        self.cube_egress_initialised = False
        self.received_messages: list[bytes] = []
        self.our_seq = 0   # next sequence we will send up to the SUT (cube tx)
        self.their_accept = 0  # what we believe their accept is
        self.stop_flag = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.stop_flag = True
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        # First step: drain stream of PINGs from the SUT, send back PONGs.
        # Also kick off our own egress handshake by sending PINGs ourselves.
        cube_countdown = FLUSH_COUNT
        cube_pings_sent = 0
        last_ping = 0.0
        while not self.stop_flag:
            tx = self.fake.drain_tx()
            self.stream.feed(tx)
            while self.stream.has_frame():
                frame = self.stream.pop()
                if frame is None:
                    break
                self._handle_frame(frame, cube_countdown_ref=lambda: cube_countdown)
                if frame.command == CMD_PONG and cube_countdown > 0:
                    cube_countdown -= 1

            # Cube-side egress: we need to PING the SUT until we have received
            # FLUSH_COUNT PONGs, then send INITIALISE.
            now = time.monotonic()
            if not self.cube_egress_initialised and now - last_ping > 0.001:
                if cube_countdown > 0:
                    self.fake.inject(framing.build_ping())
                    cube_pings_sent += 1
                else:
                    self.fake.inject(framing.build_initialise(self.our_seq))
                last_ping = now

            time.sleep(0.0005)

    def _handle_frame(self, frame: framing.DecodedFrame, cube_countdown_ref) -> None:
        cmd = frame.command
        if cmd == CMD_PING:
            self.fake.inject(framing.build_pong())
        elif cmd == CMD_INITIALISE:
            assert frame.body[0] == PROTOCOL_VERSION
            self.their_accept = frame.body[1]
            self.fake.inject(framing.build_initialised())
            self.cube_initialised = True
        elif cmd == CMD_INITIALISED:
            self.cube_egress_initialised = True
        elif cmd == CMD_PONG:
            pass
        elif cmd == CMD_ACKNOWLEDGE:
            pass
        elif cmd == CMD_MESSAGE:
            seq = frame.body[-1]
            payload = frame.body[:-1]
            if seq == self.their_accept:
                self.fake.inject(framing.build_acknowledge(seq))
                self.received_messages.append(payload)
                self.their_accept = (self.their_accept + 1) & 0xFF
            else:
                self.fake.inject(framing.build_acknowledge((self.their_accept - 1) & 0xFF))


@pytest.fixture
def fake_link(monkeypatch):
    fake = FakeSerial()

    class _SerialPatch:
        Serial = lambda *a, **kw: fake
        EIGHTBITS = 8
        PARITY_NONE = "N"
        STOPBITS_ONE = 1

        class SerialException(Exception):
            pass

    # Patch the symbol that link.py imports
    monkeypatch.setattr(link_module, "serial", _SerialPatch)
    return fake


def test_handshake_and_message(fake_link, monkeypatch):
    fake = fake_link
    cube = CubeEmulator(fake)
    try:
        link = link_module.SerialLink("dummy")
        link.start(handshake_timeout=10.0)
        try:
            future = link.submit(b"\x00\x01\x02\x03\x04\xaa\xbb")
            future.result(timeout=5.0)
            # Wait briefly for cube to process and append
            for _ in range(100):
                if cube.received_messages:
                    break
                time.sleep(0.01)
            assert cube.received_messages, "cube did not receive the MESSAGE"
            assert cube.received_messages[0] == b"\x00\x01\x02\x03\x04\xaa\xbb"
        finally:
            link.stop()
    finally:
        cube.stop()
