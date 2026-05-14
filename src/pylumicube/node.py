"""High-level orchestration: open the link, run the handshake, allocate
node IDs, and surface a `Display` (and future) module accessor.

This is the entry point most users want:

    with LumiCube('/dev/ttyAMA0') as cube:
        cube.display.set_leds({0: 0xFF0000})
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

from . import uavcan as u
from .allocator import Allocator
from .constants import (
    BAUD,
    DAEMON_NODE_ID,
    SERIAL_DEVICE,
    TYPE_ALLOCATION,
    TYPE_GET_NODE_INFO,
    TYPE_GET_PREFERRED_NAME,
)
from .display import Display
from .link import SerialLink
from .transport import Transport

log = logging.getLogger(__name__)


class LumiCube:
    """Owns the serial link, transport, and allocator. Exposes module helpers."""

    def __init__(self, port: str = SERIAL_DEVICE, *, baudrate: int = BAUD,
                 source_id: int = DAEMON_NODE_ID,
                 handshake_timeout: float = 5.0,
                 discovery_timeout: float = 3.0) -> None:
        self._port = port
        self._baudrate = baudrate
        self._source_id = source_id
        self._handshake_timeout = handshake_timeout
        self._discovery_timeout = discovery_timeout

        self._link: SerialLink | None = None
        self._transport: Transport | None = None
        self._allocator: Allocator | None = None
        self._discovered_event = threading.Event()
        self._allocated: dict[int, uuid.UUID | None] = {}  # node_id -> uuid (None = passively observed)
        self._names: dict[int, str] = {}              # node_id -> preferred name
        self._display_node_id: int | None = None

    # ---------------- lifecycle ----------------

    def __enter__(self) -> "LumiCube":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()

    def start(self) -> None:
        log.info("opening %s at %d baud", self._port, self._baudrate)
        self._link = SerialLink(self._port, self._baudrate)
        self._link.start(handshake_timeout=self._handshake_timeout)
        self._transport = Transport(self._link, source_id=self._source_id)
        self._allocator = Allocator(self._transport, on_allocated=self._on_allocated)
        # The daemon claims its own ID (matches Java behaviour).
        self._allocator.reserve(self._source_id)
        # Also passively learn IDs from any non-anonymous broadcast traffic
        # — useful when the cube boards retained their IDs from a prior daemon
        # session and aren't sending fresh allocation requests.
        self._transport.add_broadcast_handler(self._on_broadcast)
        self._wait_for_nodes()
        self._discover_names()
        self._display_node_id = self._pick_display_node()

    def stop(self) -> None:
        if self._link is not None:
            self._link.stop()
            self._link = None
        self._transport = None
        self._allocator = None

    # ---------------- public accessors ----------------

    @property
    def link(self) -> SerialLink:
        if self._link is None:
            raise RuntimeError("link not started")
        return self._link

    @property
    def transport(self) -> Transport:
        if self._transport is None:
            raise RuntimeError("transport not started")
        return self._transport

    @property
    def allocator(self) -> Allocator:
        if self._allocator is None:
            raise RuntimeError("allocator not started")
        return self._allocator

    @property
    def display(self) -> Display:
        if self._display_node_id is None:
            raise RuntimeError(
                "display module not located; "
                f"allocated nodes: {self._allocated}, names: {self._names}"
            )
        assert self._transport is not None
        return Display(self._transport, self._display_node_id)

    # ---------------- discovery ----------------

    def _on_allocated(self, node_id: int, identity: uuid.UUID) -> None:
        self._allocated[node_id] = identity
        self._discovered_event.set()

    def _on_broadcast(self, transfer: u.Broadcast) -> None:
        """Record any non-anonymous source ID we see — these are nodes that
        already have IDs (e.g. allocated by a previous daemon session)."""
        if transfer.type_id == TYPE_ALLOCATION:
            return  # handled by Allocator
        if transfer.source_id == 0 or transfer.source_id == self._source_id:
            return
        if transfer.source_id not in self._allocated:
            log.info("passively discovered node %d (typeId=%d)",
                     transfer.source_id, transfer.type_id)
            self._allocated[transfer.source_id] = None
            self._discovered_event.set()

    def _wait_for_nodes(self) -> None:
        """Wait until at least one node is discovered (via allocation OR
        passive broadcast observation), then settle for any stragglers."""
        first = self._discovered_event.wait(timeout=self._discovery_timeout)
        if not first and not self._allocated:
            log.warning("no nodes discovered; nothing to talk to")
            return
        time.sleep(0.75)  # grace period for additional discoveries
        log.info("discovered %d node(s): %s", len(self._allocated), self._allocated)

    def _discover_names(self) -> None:
        assert self._transport is not None
        for node_id in list(self._allocated):
            try:
                future = self._transport.send_request(
                    dest_id=node_id,
                    type_id=TYPE_GET_PREFERRED_NAME,
                    payload=b"",
                    timeout=2.0,
                )
                response = future.result(timeout=2.5)
                name = response.decode("utf-8", errors="replace").strip("\x00").strip()
                self._names[node_id] = name
                log.info("node %d preferred_name=%r", node_id, name)
            except Exception as exc:
                log.warning("could not get preferred name for node %d: %s", node_id, exc)

    def _pick_display_node(self) -> int | None:
        # The cube base board's preferred name is `cube` (or
        # `com.abstractfoundry.cube`); it owns the `display` module along with
        # `buttons`, `screen`, `speaker`. The sibling sensor board's name is
        # `button_and_light_sensor` and owns no LEDs.
        priority = ("display", "cube")
        for needle in priority:
            for node_id, name in self._names.items():
                lname = name.lower()
                if needle in lname and "sensor" not in lname:
                    return node_id
        # Last-resort fallback: any node we have a name for, otherwise first allocated.
        if self._names:
            return next(iter(self._names))
        if self._allocated:
            return next(iter(self._allocated))
        return None
