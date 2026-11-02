"""Gateway tests against a simulated tablet over localhost TCP."""

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
import io
import socket
import struct

from PIL import Image
import pytest

from custom_components.eink_server import gateway as gateway_module, pv3
from custom_components.eink_server.frame import Fit, Frame, Refresh, render
from custom_components.eink_server.gateway import Delivery, Gateway

from . import (
    UUID_TEXT,
    Tablet,
    ack_payload,
    settle,
    status_payload,
    touch_payload,
    unchunk,
)

W, H = 16, 8


@dataclass
class Recorder:
    """Listener collecting callbacks."""

    statuses: list[pv3.Status] = field(default_factory=list)
    connections: list[bool] = field(default_factory=list)
    deliveries: list[tuple[int, Delivery]] = field(default_factory=list)
    touches: list[tuple[int, int, int]] = field(default_factory=list)

    def on_status(self, uuid: str, status: pv3.Status) -> None:
        self.statuses.append(status)

    def on_connection(self, uuid: str, connected: bool) -> None:
        self.connections.append(connected)

    def on_delivery(self, uuid: str, frame_id: int, state: Delivery) -> None:
        self.deliveries.append((frame_id, state))

    def on_touch(self, uuid: str, frame_id: int, x: int, y: int) -> None:
        self.touches.append((frame_id, x, y))


def _frame(
    dot: tuple[int, int] | None = None, refresh: Refresh = Refresh.PARTIAL
) -> Frame:
    """White W×H frame, optionally with one black pixel."""
    image = Image.new("L", (W, H), 255)
    if dot:
        image.putpixel(dot, 0)
    return render(_png(image), W, H, Fit.CONTAIN, 0, refresh)


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
async def setup() -> AsyncIterator[tuple[Gateway, Recorder, Tablet]]:
    recorder = Recorder()
    gateway = Gateway(recorder)
    await gateway.start("127.0.0.1", 0)
    tablet = Tablet(*await asyncio.open_connection("127.0.0.1", gateway.port))
    yield gateway, recorder, tablet
    await tablet.close()
    await gateway.stop()


async def test_status_reply(setup) -> None:
    """First reply is chunked, heartbeats raw; telemetry reported."""
    gateway, recorder, tablet = setup

    first = await tablet.status(kind=1)
    second = await tablet.status(kind=2)

    assert unchunk(first)[24:28] == struct.pack("<I", 1)
    assert second[24:28] == struct.pack("<I", 2)
    assert recorder.connections == [True]
    assert [s.kind for s in recorder.statuses] == [1, 2]
    assert gateway.connected(UUID_TEXT)


async def test_full_then_partial_then_delivered(setup) -> None:
    """Full frame first, changed rect once acked, delivered on echo."""
    gateway, recorder, tablet = setup
    await tablet.status(kind=1)

    first = _frame()
    gateway.publish(UUID_TEXT, first)
    seq, frame_id, primitive = await tablet.image()
    assert frame_id == first.id
    assert (primitive.width, primitive.height) == (W, H)
    assert seq == 2

    await tablet.send(ack_payload(seq))
    await settle(lambda: (first.id, Delivery.ACKNOWLEDGED) in recorder.deliveries)

    second = _frame(dot=(5, 2))
    gateway.publish(UUID_TEXT, second)
    seq2, frame_id, primitive = await tablet.image()
    assert frame_id == second.id
    assert (primitive.x, primitive.y, primitive.width, primitive.height) == (
        10,
        5,
        2,
        1,
    )
    assert seq2 == seq + 1

    await tablet.status(kind=3, display_state=second.id)
    await settle(lambda: (second.id, Delivery.DELIVERED) in recorder.deliveries)


async def test_one_frame_in_flight(setup) -> None:
    """Frames wait for the previous ack; then only the newest is sent."""
    gateway, _, tablet = setup
    await tablet.status(kind=1)
    gateway.publish(UUID_TEXT, _frame())
    seq, _, _ = await tablet.image()

    gateway.publish(UUID_TEXT, _frame(dot=(1, 1)))
    newest = _frame(dot=(5, 2))
    gateway.publish(UUID_TEXT, newest)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(tablet.receive(), 0.3)

    await tablet.send(ack_payload(seq))
    _, frame_id, primitive = await tablet.image()

    assert frame_id == newest.id
    assert (primitive.x, primitive.y, primitive.width, primitive.height) == (
        10,
        5,
        2,
        1,
    )


async def test_unacked_frame_expires(setup, monkeypatch) -> None:
    """Without an ack in time, the next frame goes out full-screen."""
    monkeypatch.setattr(gateway_module, "ACK_TIMEOUT", 0)
    gateway, _, tablet = setup
    await tablet.status(kind=1)

    gateway.publish(UUID_TEXT, _frame())
    await tablet.image()

    gateway.publish(UUID_TEXT, _frame(dot=(1, 1)))
    _, _, primitive = await tablet.image()
    assert (primitive.width, primitive.height) == (W, H)


async def test_full_refresh_requested(setup) -> None:
    """Refresh.FULL ignores a confirmed base."""
    gateway, recorder, tablet = setup
    await tablet.status(kind=1)

    gateway.publish(UUID_TEXT, _frame())
    seq, frame_id, _ = await tablet.image()
    await tablet.send(ack_payload(seq))
    await settle(lambda: (frame_id, Delivery.ACKNOWLEDGED) in recorder.deliveries)

    gateway.publish(UUID_TEXT, _frame(dot=(1, 1), refresh=Refresh.FULL))
    _, _, primitive = await tablet.image()
    assert (primitive.width, primitive.height) == (W, H)


async def test_pending_frame_sent_on_connect(setup) -> None:
    """A frame published while offline is sent after the first status."""
    gateway, _, tablet = setup
    frame = _frame()
    gateway.publish(UUID_TEXT, frame)

    await tablet.status(kind=1)
    _, frame_id, _ = await tablet.image()

    assert frame_id == frame.id


async def test_displayed_frame_not_resent(setup) -> None:
    """A tablet already showing the frame gets no image."""
    gateway, recorder, tablet = setup
    frame = _frame()
    gateway.publish(UUID_TEXT, frame)

    await tablet.status(kind=1, display_state=frame.id)
    await tablet.status(kind=2, display_state=frame.id)

    assert (frame.id, Delivery.DELIVERED) in recorder.deliveries
    assert (frame.id, Delivery.SENT) not in recorder.deliveries


async def test_touch(setup) -> None:
    """Touch reports upright coordinates; needs a prior status."""
    _, recorder, tablet = setup

    await tablet.send(touch_payload(1, 1))
    await tablet.status(kind=1)
    await tablet.send(touch_payload(924, 703, frame_id=7))
    await settle(lambda: recorder.touches)

    assert recorder.touches == [(7, 99, 54)]


async def test_disconnect_reported(setup) -> None:
    """Closing the socket ends the session."""
    gateway, recorder, tablet = setup
    await tablet.status(kind=1)

    await tablet.close()
    await settle(lambda: recorder.connections == [True, False])

    assert not gateway.connected(UUID_TEXT)


async def test_wrong_uuid_ack_ignored(setup) -> None:
    """An ack for another UUID does not confirm the frame."""
    gateway, recorder, tablet = setup
    await tablet.status(kind=1)
    gateway.publish(UUID_TEXT, _frame())
    seq, frame_id, _ = await tablet.image()

    await tablet.send(ack_payload(seq, uuid=bytes(16)))
    await tablet.status(kind=3)

    assert (frame_id, Delivery.ACKNOWLEDGED) not in recorder.deliveries


async def test_touch_after_status_without_size(setup) -> None:
    """A status lacking panel size keeps the last known size."""
    _, recorder, tablet = setup
    await tablet.status(kind=1)
    await tablet.send(status_payload(kind=2, size=False))
    await tablet.receive()

    await tablet.send(touch_payload(924, 703))
    await settle(lambda: recorder.touches)

    assert recorder.touches == [(0, 99, 54)]


async def test_socket_options_match_go(setup) -> None:
    """Accepted sockets get Go's defaults: no delay, 15 s keepalive."""
    gateway, _, tablet = setup
    await tablet.status(kind=1)

    sock = gateway._sessions[UUID_TEXT].writer.get_extra_info("socket")

    assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY)
    assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
    if hasattr(socket, "TCP_KEEPIDLE"):
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE) == 15
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL) == 15
        assert sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT) == 9


async def test_chunked_reply_follows_kind(setup) -> None:
    """Like Go, a kind-1 status gets a chunked reply even mid-connection."""
    _, _, tablet = setup
    await tablet.status(kind=1)

    reply = await tablet.status(kind=1)

    assert unchunk(reply)[24:28] == struct.pack("<I", 1)


async def test_full_frame_after_reconnect(setup) -> None:
    """Like Go: a new connection's first frame is full, even if the tablet
    reports a known frame. A Joan 6 dropped such partials and reconnected
    in a loop."""
    gateway, _, tablet = setup
    await tablet.status(kind=1)
    first = _frame()
    gateway.publish(UUID_TEXT, first)
    seq, _, _ = await tablet.image()
    await tablet.send(ack_payload(seq))
    await tablet.close()
    await settle(lambda: not gateway.connected(UUID_TEXT))

    tablet = Tablet(*await asyncio.open_connection("127.0.0.1", gateway.port))
    try:
        await tablet.status(kind=1, display_state=first.id)
        gateway.publish(UUID_TEXT, _frame(dot=(5, 2)))
        _, _, primitive = await tablet.image()
    finally:
        await tablet.close()

    assert (primitive.width, primitive.height) == (W, H)


async def test_partial_after_echo_on_same_connection(setup) -> None:
    """A status echo of the frame sent on this connection confirms it."""
    gateway, _, tablet = setup
    await tablet.status(kind=1)
    first = _frame()
    gateway.publish(UUID_TEXT, first)
    await tablet.image()

    await tablet.status(kind=2, display_state=first.id)
    gateway.publish(UUID_TEXT, _frame(dot=(5, 2)))
    _, _, primitive = await tablet.image()

    assert (primitive.width, primitive.height) == (2, 1)


async def test_first_frame_is_preencoded(setup, monkeypatch) -> None:
    """A woken tablet gets its frame without encoding delay.

    Pure-Python LZ4 takes ~1.5 s on HA hardware; a Joan 6 woken from
    sleep stops listening before that. The frame for the next connect
    is encoded when published, not when the tablet checks in.
    """
    gateway, _, tablet = setup
    calls: list[int] = []
    real = gateway_module._encode

    def counting(*args):
        calls.append(1)
        return real(*args)

    monkeypatch.setattr(gateway_module, "_encode", counting)
    frame = _frame()
    gateway.publish(UUID_TEXT, frame)
    await settle(lambda: UUID_TEXT in gateway._prepared)

    await tablet.status(kind=1)
    _, frame_id, _ = await tablet.image()

    assert frame_id == frame.id
    assert len(calls) == 1
