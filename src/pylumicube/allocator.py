"""3-stage dynamic node-ID allocator.

Direct port of ref_data/lumicube-daemon/.../uavcan/Allocator.java.
The cube boards boot anonymous and broadcast typeId=ALLOCATION (1) requests
that the daemon answers with the same typeId. PROTOCOL.md §4.2.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable

from . import uavcan as u
from .constants import (
    ALLOCATOR_FIRST_CANDIDATE,
    DEFAULT_PRIORITY,
    TYPE_ALLOCATION,
)
from .transport import Transport

log = logging.getLogger(__name__)


_QUERY_TIMEOUT_S = 0.5


class Allocator:
    """Tracks UUID -> nodeId allocations and runs the 3-stage exchange."""

    def __init__(self, transport: Transport,
                 on_allocated: Callable[[int, uuid.UUID], None] | None = None) -> None:
        self._transport = transport
        self._on_allocated = on_allocated
        self._lock = threading.Lock()
        self._table: dict[int, uuid.UUID] = {}     # nodeId -> UUID
        self._exchange = bytearray(17)             # 1-byte header + 16-byte UUID
        self._cursor = 0
        self._timestamp = 0.0
        transport.add_broadcast_handler(self._on_broadcast)

    @property
    def table(self) -> dict[int, uuid.UUID]:
        with self._lock:
            return dict(self._table)

    def reserve(self, node_id: int, identity: uuid.UUID | None = None) -> None:
        """Pre-claim an ID (e.g. for the daemon itself)."""
        if not 1 <= node_id <= 127:
            raise ValueError("invalid node id")
        with self._lock:
            if node_id in self._table:
                raise ValueError(f"node {node_id} already allocated")
            self._table[node_id] = identity  # may be None

    # ---------------- ingress ----------------

    def _on_broadcast(self, transfer: u.Broadcast) -> None:
        if transfer.type_id != TYPE_ALLOCATION:
            return
        if transfer.source_id != 0:
            log.warning("non-anonymous allocation broadcast (source=%d) — another allocator on bus?",
                        transfer.source_id)
            return

        now = time.monotonic()
        if self._cursor > 0 and now - self._timestamp > _QUERY_TIMEOUT_S:
            log.info("allocation timeout, resetting cursor")
            self._cursor = 0

        body = transfer.payload
        new_query = bool(body[0] & 0x01) if body else False
        length = len(body)

        if new_query and length == 7:
            self._stage1(body)
        elif self._cursor == 7 and length == 7:
            self._stage2(body)
        elif self._cursor == 13 and length == 5:
            self._stage3(body)
        else:
            log.warning("invalid allocation query: cursor=%d, length=%d, new=%s",
                        self._cursor, length, new_query)

    def _stage1(self, body: bytes) -> None:
        self._timestamp = time.monotonic()
        self._exchange[0] = 0
        self._exchange[1:7] = body[1:7]
        self._cursor = 7
        self._broadcast(self._exchange[:7])
        log.debug("allocation stage 1 complete")

    def _stage2(self, body: bytes) -> None:
        self._timestamp = time.monotonic()
        self._exchange[7:13] = body[1:7]
        self._cursor = 13
        self._broadcast(self._exchange[:13])
        log.debug("allocation stage 2 complete")

    def _stage3(self, body: bytes) -> None:
        self._timestamp = time.monotonic()
        self._exchange[13:17] = body[1:5]
        identity = uuid.UUID(bytes=bytes(self._exchange[1:17]))
        requested_id = (body[0] & 0xFE) >> 1
        log.info("allocatee uuid=%s requested_id=%d", identity, requested_id)
        allocated_id = self._allocate_id(requested_id, identity)
        if allocated_id == 0:
            log.error("node ID exhaustion")
            self._cursor = 0
            return
        self._exchange[0] = (allocated_id << 1) & 0xFE
        self._broadcast(self._exchange[:17])
        log.info("assigned node_id=%d to uuid=%s", allocated_id, identity)
        self._cursor = 0
        if self._on_allocated is not None:
            try:
                self._on_allocated(allocated_id, identity)
            except Exception:
                log.exception("on_allocated callback raised")

    def _allocate_id(self, requested_id: int, identity: uuid.UUID) -> int:
        with self._lock:
            for nid, existing in self._table.items():
                if existing == identity:
                    return nid
            if requested_id != 0:
                # Java raises here; we'd rather fall back to the count-down path.
                log.info("allocatee requested specific id %d; ignoring and assigning anyway", requested_id)
            for candidate in range(ALLOCATOR_FIRST_CANDIDATE, 0, -1):
                if candidate not in self._table:
                    self._table[candidate] = identity
                    return candidate
        return 0

    def _broadcast(self, body: bytes) -> None:
        # Allocator broadcasts go out as anonymous broadcasts (source_id 0) per Allocator.java
        # which uses Node.broadcast() (the daemon's own source_id). Either works as long as
        # the recipient parses payload contents. The Java daemon uses its own non-anonymous
        # source_id; mirror that here.
        self._transport.send_broadcast(
            type_id=TYPE_ALLOCATION,
            payload=bytes(body),
            priority=DEFAULT_PRIORITY,
        )
