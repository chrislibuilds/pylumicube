"""UAVCAN transport sitting on top of the link layer.

Provides:
  - `send_request(dest_id, type_id, payload)` -> Future[bytes] that resolves
    when the matching response is received (or times out).
  - `send_broadcast(type_id, payload, source_id=...)` for fire-and-forget.
  - subscriber callbacks for received broadcasts and requests.

The link layer guarantees frame delivery and ordering; this layer just
manages transferIds and matches requests to responses.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future

from . import uavcan as u
from .constants import DEFAULT_PRIORITY, MAX_UAVCAN_PAYLOAD
from .link import SerialLink

log = logging.getLogger(__name__)


BroadcastHandler = Callable[[u.Broadcast], None]
RequestHandler = Callable[[u.ServiceRequest], None]


class Transport:
    def __init__(self, link: SerialLink, source_id: int) -> None:
        self._link = link
        self._source_id = source_id
        self._lock = threading.Lock()
        # transferId pool keyed by (dest_id, type_id) -> next ID (0..31, monotonic mod 32)
        self._service_transfer_ids: dict[tuple[int, int], int] = {}
        # broadcast transferId per (source, typeId) — only relevant when *we* broadcast
        self._broadcast_transfer_ids: dict[tuple[int, int], int] = {}
        # outstanding requests: (source_of_response, type_id, transfer_id) -> Future
        self._inflight: dict[tuple[int, int, int], Future] = {}
        self._broadcast_handlers: list[BroadcastHandler] = []
        self._request_handlers: list[RequestHandler] = []

        link.set_message_handler(self._on_message)

    # ---------------- public API ----------------

    @property
    def source_id(self) -> int:
        return self._source_id

    def set_source_id(self, source_id: int) -> None:
        self._source_id = source_id

    def add_broadcast_handler(self, handler: BroadcastHandler) -> None:
        self._broadcast_handlers.append(handler)

    def add_request_handler(self, handler: RequestHandler) -> None:
        self._request_handlers.append(handler)

    def send_broadcast(self, type_id: int, payload: bytes,
                       source_id: int | None = None,
                       priority: int = DEFAULT_PRIORITY) -> Future:
        if len(payload) > MAX_UAVCAN_PAYLOAD:
            raise ValueError("payload too large")
        src = source_id if source_id is not None else self._source_id
        if src == 0:
            message_id = u.encode_anonymous_broadcast(type_id, priority)
        else:
            message_id = u.encode_broadcast(src, type_id, priority)
        with self._lock:
            key = (src, type_id)
            transfer_id = self._broadcast_transfer_ids.get(key, 0)
            self._broadcast_transfer_ids[key] = (transfer_id + 1) & 0x1F
        body = u.pack_message(transfer_id, message_id, payload)
        return self._link.submit(body)

    def send_request(self, dest_id: int, type_id: int, payload: bytes,
                     priority: int = DEFAULT_PRIORITY,
                     timeout: float = 5.0) -> Future:
        if len(payload) > MAX_UAVCAN_PAYLOAD:
            raise ValueError("payload too large")
        message_id = u.encode_request(self._source_id, dest_id, type_id, priority)
        with self._lock:
            key = (dest_id, type_id)
            transfer_id = self._service_transfer_ids.get(key, 0)
            self._service_transfer_ids[key] = (transfer_id + 1) & 0x1F
            response_fut: Future = Future()
            inflight_key = (dest_id, type_id, transfer_id)
            self._inflight[inflight_key] = response_fut
        body = u.pack_message(transfer_id, message_id, payload)
        link_fut = self._link.submit(body)

        def on_link_complete(f: Future) -> None:
            try:
                f.result()
            except Exception as exc:
                with self._lock:
                    self._inflight.pop(inflight_key, None)
                if not response_fut.done():
                    response_fut.set_exception(exc)
        link_fut.add_done_callback(on_link_complete)

        def expire() -> None:
            time.sleep(timeout)
            with self._lock:
                still_pending = self._inflight.pop(inflight_key, None)
            if still_pending is response_fut and not response_fut.done():
                response_fut.set_exception(TimeoutError(
                    f"no response for type_id={type_id} dest={dest_id} tid={transfer_id} after {timeout}s"))
        threading.Thread(target=expire, daemon=True).start()
        return response_fut

    def send_response(self, dest_id: int, type_id: int, transfer_id: int,
                      payload: bytes, priority: int = DEFAULT_PRIORITY) -> Future:
        message_id = u.encode_response(self._source_id, dest_id, type_id, priority)
        body = u.pack_message(transfer_id, message_id, payload)
        return self._link.submit(body)

    # ---------------- ingress ----------------

    def _on_message(self, uavcan_payload: bytes) -> None:
        transfer = u.parse_message(uavcan_payload)
        if transfer is None:
            return
        if isinstance(transfer, u.Broadcast):
            for handler in list(self._broadcast_handlers):
                try:
                    handler(transfer)
                except Exception:
                    log.exception("broadcast handler raised")
        elif isinstance(transfer, u.ServiceRequest):
            for handler in list(self._request_handlers):
                try:
                    handler(transfer)
                except Exception:
                    log.exception("request handler raised")
        elif isinstance(transfer, u.ServiceResponse):
            key = (transfer.source_id, transfer.type_id, transfer.transfer_id)
            with self._lock:
                fut = self._inflight.pop(key, None)
            if fut is not None and not fut.done():
                fut.set_result(transfer.payload)
            else:
                log.debug("unmatched response: %r", transfer)
