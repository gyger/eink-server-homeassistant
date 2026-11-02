"""PV3 tablet gateway: TCP sessions and frame delivery. No Home Assistant.

    tablet ──status──> gateway ──status reply──> tablet
                          │  display_state != desired frame?
                          └──image (full or changed rect)──> tablet
    tablet ──ack(seq)──> base confirmed, no resend
    tablet ──status(display_state == frame)──> delivered
    tablet ──touch──> listener

Only the newest desired frame per tablet matters. Partial updates are sent
only against a frame the tablet confirmed on this connection.
"""

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import socket
import time
from typing import Any, Protocol

from . import pv3
from .frame import Frame, Refresh, changed_region

_LOGGER = logging.getLogger(__name__)

# Tablets heartbeat every few minutes; a silent socket is dead.
READ_TIMEOUT = 20 * 60
WRITE_TIMEOUT = 10

# Go's defaults for accepted TCP connections, which the Go server relies
# on: probe after 15 s idle, every 15 s, give up after 9 probes.
KEEPALIVE_IDLE = 15
KEEPALIVE_INTERVAL = 15
KEEPALIVE_COUNT = 9

# One frame in flight: the next waits for its ack, at most this long.
# Like Go's deliveryRetryInterval: an unacked frame is resent after 15 s,
# at most MAX_ATTEMPTS times per connection, then the connection is
# closed so the tablet reconnects fresh. Acks normally arrive in 2-9 s.
ACK_TIMEOUT = 15
MAX_ATTEMPTS = 3

UINT32 = 1 << 32

# A connection starts with a kind-1 status, so its first image uses
# sequence 2. That record is encoded in advance: pure-Python LZ4 takes
# ~1.5 s on HA hardware, and a Joan 6 woken from sleep stops listening
# before that (Go answers within milliseconds).
FIRST_SEQ = 2


class Delivery(StrEnum):
    """Desired-frame delivery state."""

    PENDING = "pending"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    DELIVERED = "delivered"
    FAILED = "failed"


class Listener(Protocol):
    """Callbacks from the gateway, all on the event loop."""

    def on_status(self, uuid: str, status: pv3.Status) -> None:
        """A tablet reported telemetry."""

    def on_connection(self, uuid: str, connected: bool) -> None:
        """A tablet session opened or closed."""

    def on_delivery(self, uuid: str, frame_id: int, state: Delivery) -> None:
        """The desired frame changed delivery state."""

    def on_touch(self, uuid: str, frame_id: int, x: int, y: int) -> None:
        """A tap in upright panel coordinates."""


@dataclass
class _Session:
    """One open tablet connection."""

    uuid: str
    status: pv3.Status
    writer: asyncio.StreamWriter
    next_seq: int

    # Last non-zero panel size; not every status carries it.
    width: int = 0
    height: int = 0
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    deliver_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    closed: bool = False

    # Last image written on this connection.
    sent_id: int = 0
    sent_seq: int = 0
    sent_at: float = 0.0
    sent_packed: bytes | None = None
    acked_seq: int = 0
    attempts: int = 0

    def sequence(self) -> int:
        """Return the next non-zero message sequence."""
        seq = self.next_seq or 1
        self.next_seq = (seq + 1) % UINT32 or 1
        return seq

    def observe(self, kind: int) -> None:
        """Keep outgoing sequences above the tablet's status kind."""
        if self.next_seq <= kind:
            self.next_seq = (kind + 1) % UINT32 or 1

    def acknowledged(self) -> bool:
        """Whether the tablet acknowledged the last image."""
        return self.sent_seq != 0 and self.acked_seq == self.sent_seq

    def in_flight(self) -> bool:
        """Whether the last image still awaits its ack."""
        waiting = self.sent_seq != 0 and not self.acknowledged()
        return waiting and time.monotonic() - self.sent_at < ACK_TIMEOUT

    async def write(self, data: bytes) -> None:
        """Write one whole record; a failure closes the connection."""
        async with self.write_lock:
            try:
                self.writer.write(data)
                await asyncio.wait_for(self.writer.drain(), WRITE_TIMEOUT)
            except OSError, TimeoutError:
                self.close()
                raise

    def close(self) -> None:
        """Mark dead and close; the reader loop then exits."""
        self.closed = True
        self.writer.close()


class Gateway:
    """Accepts tablet connections and keeps them on their desired frame."""

    def __init__(self, listener: Listener) -> None:
        """Create a stopped gateway."""
        self._listener = listener
        self._desired: dict[str, Frame] = {}
        self._prepared: dict[str, tuple[int, bytes]] = {}
        self._sessions: dict[str, _Session] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Bound TCP port, useful when started on port 0."""
        assert self._server is not None
        return self._server.sockets[0].getsockname()[1]

    async def start(self, host: str | None, port: int) -> None:
        """Listen for tablets."""
        self._server = await asyncio.start_server(self._handle, host, port)

    async def stop(self) -> None:
        """Close the listener, all sessions and pending deliveries."""
        if self._server is not None:
            self._server.close()

        for session in list(self._sessions.values()):
            session.close()

        for task in list(self._tasks):
            task.cancel()

        if self._server is not None:
            await self._server.wait_closed()

    def connected(self, uuid: str) -> bool:
        """Whether the tablet has an open session."""
        return uuid in self._sessions

    def publish(self, uuid: str, frame: Frame) -> None:
        """Make frame the tablet's desired frame and send it if connected."""
        self._desired[uuid] = frame
        self._listener.on_delivery(uuid, frame.id, Delivery.PENDING)
        self._spawn(self._prepare(uuid, frame))

        session = self._sessions.get(uuid)
        _LOGGER.debug(
            "%s: frame %08x desired, connected=%s", uuid, frame.id, bool(session)
        )
        if session:
            self._spawn(self._deliver(session))

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Serve one tablet connection until it closes."""
        peer = writer.get_extra_info("peername")
        _tune(writer.get_extra_info("socket"))
        session: _Session | None = None
        try:
            while True:
                record = await asyncio.wait_for(pv3.read_record(reader), READ_TIMEOUT)
                if record.type == pv3.Packet.BOOTLOADER:
                    _LOGGER.info("Ignoring bootloader connection from %s", peer)
                    return

                session = await self._dispatch(record, session, writer)
        except (asyncio.IncompleteReadError, OSError, TimeoutError) as err:
            _LOGGER.debug("Connection %s ended: %r", peer, err)
        except pv3.ProtocolError as err:
            _LOGGER.warning("Closing %s: %s", peer, err)
        finally:
            writer.close()
            self._drop(session)

    async def _dispatch(
        self,
        record: pv3.Record,
        session: _Session | None,
        writer: asyncio.StreamWriter,
    ) -> _Session | None:
        """Route one record; returns the (possibly new) session."""
        try:
            mtype = pv3.message_type(record)
        except pv3.ProtocolError as err:
            _LOGGER.debug("Ignoring record: %s", err)
            return session

        if mtype == pv3.Message.STATUS:
            try:
                status = pv3.parse_status(record)
            except pv3.ProtocolError as err:
                _LOGGER.warning("Rejected status: %s", err)
                return session
            return await self._on_status(status, session, writer)

        # Acks and touches are only valid inside an identified session.
        if session is None:
            _LOGGER.debug("Ignoring message %d before status", mtype)
            return session

        try:
            if mtype == pv3.Message.ACK:
                self._on_ack(session, *pv3.parse_ack(record))
            elif mtype == pv3.Message.TOUCH:
                self._on_touch(session, pv3.parse_touch(record))
            else:
                _LOGGER.debug("Ignoring unsupported message %d", mtype)
        except pv3.ProtocolError as err:
            _LOGGER.warning("%s: rejected message %d: %s", session.uuid, mtype, err)

        return session

    async def _on_status(
        self,
        status: pv3.Status,
        session: _Session | None,
        writer: asyncio.StreamWriter,
    ) -> _Session:
        """Reply to a status, report telemetry, then deliver if needed."""
        uuid = pv3.format_uuid(status.uuid)
        if session is not None and session.uuid != uuid:
            raise pv3.ProtocolError("status UUID changed")

        if session is None:
            session = _Session(uuid, status, writer, next_seq=status.kind + 1)
            if old := self._sessions.get(uuid):
                old.close()
            self._sessions[uuid] = session
            self._listener.on_connection(uuid, True)

        session.status = status
        session.observe(status.kind)

        # Showing the frame sent on this connection confirms it like an ack.
        if session.sent_id and status.display_state == session.sent_id:
            session.acked_seq = session.sent_seq
        if status.width and status.height:
            session.width, session.height = status.width, status.height
        _LOGGER.debug(
            "%s: status kind=%d display_state=%08x fields=%s",
            uuid,
            status.kind,
            status.display_state,
            dict(sorted(status.fields.items())),
        )

        # Like Go: the kind-1 (session start) reply is chunked, others raw.
        chunked = status.kind == 1
        compression = pv3.Compression.CHUNKED if chunked else pv3.Compression.RAW
        try:
            await session.write(pv3.status_response(status, compression))
        except OSError, TimeoutError:
            self._drop(session)
            raise

        self._listener.on_status(uuid, status)

        desired = self._desired.get(uuid)
        if desired and status.display_state == desired.id:
            self._listener.on_delivery(uuid, desired.id, Delivery.DELIVERED)
            return session

        self._spawn(self._deliver(session))
        return session

    def _on_ack(self, session: _Session, uuid: bytes, sequence: int) -> None:
        """Record that the tablet received the last image."""
        if pv3.format_uuid(uuid) != session.uuid or sequence != session.sent_seq:
            _LOGGER.debug("%s: unmatched ack %d", session.uuid, sequence)
            return

        _LOGGER.debug("%s: ack %d", session.uuid, sequence)
        session.acked_seq = sequence
        self._listener.on_delivery(session.uuid, session.sent_id, Delivery.ACKNOWLEDGED)

        # A newer frame may have been waiting for this ack.
        self._spawn(self._deliver(session))

    def _on_touch(self, session: _Session, touch: pv3.Touch) -> None:
        """Convert to upright coordinates and report."""
        if pv3.format_uuid(touch.uuid) != session.uuid:
            raise pv3.ProtocolError("touch UUID mismatch")

        try:
            x, y = touch.physical(session.width, session.height)
        except pv3.ProtocolError as err:
            raise pv3.ProtocolError(
                f"{err}: raw ({touch.raw_x},{touch.raw_y}),"
                f" panel {session.width}x{session.height}"
            ) from err

        self._listener.on_touch(session.uuid, touch.frame_id, x, y)

    async def _deliver(self, session: _Session) -> None:
        """Send the desired frame unless the tablet has or just got it."""
        async with session.deliver_lock:
            frame = self._desired.get(session.uuid)
            if session.closed or frame is None:
                return
            if session.status.display_state == frame.id:
                return

            # The tablet redraws frames one by one; queueing more only
            # builds a backlog. The ack or its timeout sends the newest.
            if session.in_flight():
                return

            if session.sent_id == frame.id:
                if session.acknowledged():
                    return
                if session.attempts >= MAX_ATTEMPTS:
                    self._listener.on_delivery(session.uuid, frame.id, Delivery.FAILED)
                    session.close()
                    return
            else:
                session.attempts = 0

            base = self._base(session) if frame.refresh == Refresh.PARTIAL else None
            seq = session.sequence()
            prepared = self._prepared.get(session.uuid)
            if (
                base is None
                and seq == FIRST_SEQ
                and prepared
                and prepared[0] == frame.id
            ):
                wire = prepared[1]
            else:
                wire = await asyncio.get_running_loop().run_in_executor(
                    None, _encode, session.status.uuid, seq, frame, base
                )
            _LOGGER.debug(
                "%s: sending frame %08x seq=%d partial=%s bytes=%d",
                session.uuid,
                frame.id,
                seq,
                base is not None,
                len(wire),
            )

            try:
                await session.write(wire)
            except (OSError, TimeoutError) as err:
                _LOGGER.warning("%s: frame write failed: %r", session.uuid, err)
                self._listener.on_delivery(session.uuid, frame.id, Delivery.FAILED)
                return

            _LOGGER.debug("%s: frame %08x written", session.uuid, frame.id)
            self._spawn(self._expire(session, seq))

            session.sent_id, session.sent_seq = frame.id, seq
            session.sent_packed = frame.packed
            session.sent_at = time.monotonic()
            session.attempts += 1
            self._listener.on_delivery(session.uuid, frame.id, Delivery.SENT)

    async def _prepare(self, uuid: str, frame: Frame) -> None:
        """Encode the full frame for the next connection's first image."""
        raw = bytes.fromhex(uuid.replace("-", ""))
        wire = await asyncio.get_running_loop().run_in_executor(
            None, _encode, raw, FIRST_SEQ, frame, None
        )
        if self._desired.get(uuid) is frame:
            self._prepared[uuid] = (frame.id, wire)

    def _base(self, session: _Session) -> bytes | None:
        """Return the frame sent on this connection once confirmed.

        Like Go, the base is connection-local: after a reconnect the first
        frame is full even if the tablet reports a known frame. A Joan 6
        drops partial updates sent as a connection's first image.
        """
        return session.sent_packed if session.acknowledged() else None

    async def _expire(self, session: _Session, seq: int) -> None:
        """Retry delivery once an unacked frame times out."""
        await asyncio.sleep(ACK_TIMEOUT)
        if session.sent_seq == seq and not session.acknowledged():
            _LOGGER.debug("%s: no ack for seq %d", session.uuid, seq)
            await self._deliver(session)

    def _drop(self, session: _Session | None) -> None:
        """Forget a closed session unless a newer one replaced it."""
        if session is None:
            return

        session.closed = True
        if self._sessions.get(session.uuid) is not session:
            return

        del self._sessions[session.uuid]
        self._listener.on_connection(session.uuid, False)

    def _spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a delivery in the background, tracked for shutdown."""
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_failure)


def _encode(uuid: bytes, seq: int, frame: Frame, base: bytes | None) -> bytes:
    """Build the image record; CPU-bound, runs in an executor."""
    return pv3.build_image(uuid, seq, frame.id, [changed_region(frame, base)])


def _log_failure(task: asyncio.Task[None]) -> None:
    """Surface crashes of background deliveries, which are otherwise silent."""
    if task.cancelled() or task.exception() is None:
        return

    _LOGGER.error("Frame delivery crashed", exc_info=task.exception())


def _tune(sock: socket.socket | None) -> None:
    """Apply Go's TCP defaults: no delay and 15 s keepalive probes.

    Keepalive detects tablets that vanished without closing the
    connection, instead of writing frames into a dead socket.
    """
    if sock is None:
        return

    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
    for option, value in (
        ("TCP_KEEPIDLE", KEEPALIVE_IDLE),
        ("TCP_KEEPINTVL", KEEPALIVE_INTERVAL),
        ("TCP_KEEPCNT", KEEPALIVE_COUNT),
    ):
        if hasattr(socket, option):
            sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)
