"""Reliable link layer for the LumiCube serial protocol.

Implements the bidirectional handshake (PING/INITIALISE) and the 16-slot
sliding window with 8-bit sequence numbers from PROTOCOL.md §2.3-§2.4.

Threading model: a single I/O worker thread polls the serial port for
incoming bytes (parsing them via FrameStream), services its egress duties
(handshake state machine, retransmits, queued responses), and dispatches
received MESSAGE bodies to a user callback. App threads call `submit()`
to enqueue an outgoing MESSAGE; it returns a `Future` that resolves when
the frame is ACKed (or rejected, e.g. on shutdown).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass

import serial  # pyserial

from . import framing
from .constants import (
    BAUD,
    CMD_ACKNOWLEDGE,
    CMD_INITIALISE,
    CMD_INITIALISED,
    CMD_MESSAGE,
    CMD_PING,
    CMD_PONG,
    CMD_UNINITIALISED,
    FLUSH_COUNT,
    PROTOCOL_VERSION,
    WINDOW_SIZE,
)

log = logging.getLogger(__name__)


MessageHandler = Callable[[bytes], None]


@dataclass
class _Slot:
    payload: bytes = b""              # raw uavcan body (without seq)
    encoded: bytes = b""              # full COBS-framed bytes (including seq+CRC+delimiter)
    last_sent_ns: int = 0
    future: Future | None = None


class HandshakeError(RuntimeError):
    pass


class SerialLink:
    """Reliable link layer over a serial port. PROTOCOL §2."""

    # Period at which the worker loops while egress is active (handshake or window non-empty).
    _ACTIVE_PERIOD_S = 0.0025
    # Idle period when egress window is empty after handshake.
    _IDLE_PERIOD_S = 0.05
    # Retransmit a slot if it has not been ACKed within this many seconds.
    _RETRANSMIT_AFTER_S = 0.05

    def __init__(self, port: str, baudrate: int = BAUD,
                 message_handler: MessageHandler | None = None) -> None:
        self._port_path = port
        self._baudrate = baudrate
        self._message_handler = message_handler

        self._serial: serial.Serial | None = None
        self._stream = framing.FrameStream()
        self._thread: threading.Thread | None = None
        self._running = False
        self._wire_lock = threading.Lock()  # serialise writes (worker + responder paths)

        # Egress state (mirrors EgressThread.java)
        self._head = 0  # oldest unacked sequence
        self._tail = 0  # next sequence to allocate
        self._egress_initialised = False
        self._countdown = FLUSH_COUNT
        self._slots: list[_Slot] = [_Slot() for _ in range(256)]
        self._pending: list[tuple[bytes, Future]] = []
        self._pending_lock = threading.Lock()

        # Ingress state (mirrors IngressThread.java)
        self._ingress_initialised = False
        self._accept = 0

        # Handshake bookkeeping
        self._handshake_event = threading.Event()  # set when both directions are initialised

    # ------------------------------------------------------------------ public

    def start(self, *, handshake_timeout: float = 5.0) -> None:
        if self._thread is not None:
            return
        self._serial = serial.Serial(
            self._port_path,
            baudrate=self._baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self._ACTIVE_PERIOD_S,
            write_timeout=1.0,
        )
        # Drain any stale bytes that arrived before we opened.
        self._serial.reset_input_buffer()
        self._serial.reset_output_buffer()

        self._running = True
        self._thread = threading.Thread(target=self._run, name="lumicube-serial", daemon=True)
        self._thread.start()

        if not self._handshake_event.wait(handshake_timeout):
            self.stop()
            raise HandshakeError(
                f"handshake did not complete within {handshake_timeout}s "
                f"(egress_init={self._egress_initialised}, ingress_init={self._ingress_initialised})"
            )

    def stop(self) -> None:
        self._running = False
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
        self._thread = None
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass
            self._serial = None
        # Cancel any unresolved futures
        with self._pending_lock:
            for _, fut in self._pending:
                if not fut.done():
                    fut.set_exception(RuntimeError("link stopped"))
            self._pending.clear()
        for slot in self._slots:
            if slot.future is not None and not slot.future.done():
                slot.future.set_exception(RuntimeError("link stopped"))
            slot.payload = b""
            slot.encoded = b""
            slot.last_sent_ns = 0
            slot.future = None

    def submit(self, uavcan_payload: bytes) -> Future:
        """Enqueue a MESSAGE for transmission. Returns a Future that resolves
        when the MCU ACKs the frame."""
        if not self._running:
            raise RuntimeError("link not started")
        fut: Future = Future()
        with self._pending_lock:
            self._pending.append((uavcan_payload, fut))
        return fut

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._message_handler = handler

    # ------------------------------------------------------------------ worker

    def _run(self) -> None:
        try:
            while self._running:
                self._read_available()
                self._service_egress()
                # short sleep when window is empty and initialised
                if self._egress_initialised and self._head == self._tail and not self._has_pending():
                    time.sleep(self._IDLE_PERIOD_S)
                else:
                    # serial.read already blocks up to ACTIVE_PERIOD_S waiting for data
                    pass
        except Exception:
            log.exception("serial worker crashed")
            self._running = False

    def _read_available(self) -> None:
        assert self._serial is not None
        # serial.read with timeout returns up to N bytes within the timeout.
        # Read whatever is in the OS buffer, plus block briefly for new data.
        try:
            chunk = self._serial.read(self._serial.in_waiting or 1)
        except serial.SerialException:
            log.exception("read failure")
            self._running = False
            return
        if chunk:
            self._stream.feed(chunk)
        while self._stream.has_frame():
            frame = self._stream.pop()
            if frame is None:
                break
            self._dispatch(frame)

    # ---------- ingress dispatch ----------

    def _dispatch(self, frame: framing.DecodedFrame) -> None:
        cmd = frame.command
        if cmd == CMD_PING:
            self._send_raw(framing.build_pong())
        elif cmd == CMD_PONG:
            if len(frame.body) < 1 or frame.body[0] < 1:
                log.warning("PONG with bad version: %r", frame.body)
                return
            if self._countdown > 0:
                self._countdown -= 1
        elif cmd == CMD_INITIALISE:
            if len(frame.body) >= 2 and frame.body[0] == PROTOCOL_VERSION:
                self._accept = frame.body[1]
                self._ingress_initialised = True
                self._send_raw(framing.build_initialised())
                self._maybe_signal_handshake()
            else:
                self._send_raw(framing.build_uninitialised())
        elif cmd == CMD_INITIALISED:
            self._egress_initialised = True
            self._maybe_signal_handshake()
        elif cmd == CMD_UNINITIALISED:
            log.info("counterparty uninitialised; resetting handshake")
            self._egress_initialised = False
            self._countdown = FLUSH_COUNT
        elif cmd == CMD_ACKNOWLEDGE:
            if len(frame.body) >= 1:
                self._handle_ack(frame.body[0])
        elif cmd == CMD_MESSAGE:
            self._handle_message(frame.body)
        else:
            log.warning("unsupported command 0x%02x", cmd)

    def _maybe_signal_handshake(self) -> None:
        if self._egress_initialised and self._ingress_initialised:
            self._handshake_event.set()

    def _handle_ack(self, ack_seq: int) -> None:
        # Walk head over any acknowledged slots in window. See EgressThread.handleFeedback().
        if _in_window(ack_seq, self._head, self._tail):
            new_head = (ack_seq + 1) & 0xFF
            # Fail-fast resolve futures for slots [old_head .. ack_seq]
            cursor = self._head
            while cursor != new_head:
                slot = self._slots[cursor]
                if slot.future is not None and not slot.future.done():
                    slot.future.set_result(None)
                slot.payload = b""
                slot.encoded = b""
                slot.last_sent_ns = 0
                slot.future = None
                cursor = (cursor + 1) & 0xFF
            self._head = new_head

    def _handle_message(self, body: bytes) -> None:
        # body = [uavcan ...][seq]; we already stripped command and CRC.
        if len(body) < 1:
            return
        seq = body[-1]
        uavcan_payload = body[:-1]
        if not self._ingress_initialised:
            self._send_raw(framing.build_uninitialised())
            return
        if seq == self._accept:
            self._accept = (self._accept + 1) & 0xFF
            self._send_raw(framing.build_acknowledge(seq))
            handler = self._message_handler
            if handler is not None:
                try:
                    handler(uavcan_payload)
                except Exception:
                    log.exception("message handler raised")
        else:
            # Already have this one (or it's out of order). Tell sender what we still need.
            self._send_raw(framing.build_acknowledge((self._accept - 1) & 0xFF))

    # ---------- egress driver ----------

    def _service_egress(self) -> None:
        if not self._egress_initialised:
            self._service_handshake()
            return

        # Pull pending submits into the window
        with self._pending_lock:
            while self._pending and ((self._tail - self._head) & 0xFF) < WINDOW_SIZE:
                payload, fut = self._pending.pop(0)
                seq = self._tail
                slot = self._slots[seq]
                slot.payload = payload
                slot.encoded = framing.build_message(payload, seq)
                slot.last_sent_ns = 0
                slot.future = fut
                self._tail = (self._tail + 1) & 0xFF

        # Transmit slots that have never been sent or whose retry timer expired.
        now = time.monotonic_ns()
        retry_threshold = self._RETRANSMIT_AFTER_S * 1e9
        cursor = self._head
        while cursor != self._tail:
            slot = self._slots[cursor]
            if slot.encoded and (slot.last_sent_ns == 0 or now - slot.last_sent_ns >= retry_threshold):
                self._send_raw(slot.encoded)
                slot.last_sent_ns = now
            cursor = (cursor + 1) & 0xFF

    def _service_handshake(self) -> None:
        if self._countdown > 0:
            self._send_raw(framing.build_ping())
        else:
            self._send_raw(framing.build_initialise(self._head))

    # ---------- writing ----------

    def _send_raw(self, frame: bytes) -> None:
        ser = self._serial
        if ser is None:
            return
        with self._wire_lock:
            try:
                ser.write(frame)
            except serial.SerialException:
                log.exception("write failure")
                self._running = False

    def _has_pending(self) -> bool:
        with self._pending_lock:
            return bool(self._pending)


def _in_window(seq: int, head: int, tail: int) -> bool:
    """True iff seq is within the half-open window [head, tail) modulo 256."""
    return ((seq - head) & 0xFF) < ((tail - head) & 0xFF)
