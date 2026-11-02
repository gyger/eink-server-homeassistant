"""PV3 codec tests, mirroring eink-server internal/pv3 tests."""

import asyncio
import struct

import pytest

from custom_components.eink_server import pv3

from . import (
    HEIGHT,
    UUID,
    UUID_TEXT,
    WIDTH,
    ack_payload,
    status_payload,
    tablet_record,
    touch_payload,
    unchunk,
)


async def _read(data: bytes, feed: int | None = None) -> pv3.Record:
    """Decode a record, optionally fed in fragments of feed bytes."""
    reader = asyncio.StreamReader()
    step = feed or len(data)
    for i in range(0, len(data), step):
        reader.feed_data(data[i : i + step])
    reader.feed_eof()
    return await pv3.read_record(reader)


async def test_status() -> None:
    """Status fields decode like the captured Joan 6 status."""
    record = await _read(tablet_record(status_payload(display_state=7)), feed=3)
    status = pv3.parse_status(record)

    assert pv3.message_type(record) == pv3.Message.STATUS
    assert pv3.format_uuid(status.uuid) == UUID_TEXT
    assert (status.width, status.height) == (WIDTH, HEIGHT)
    assert status.battery == 100
    assert status.humidity == 28
    assert status.temperature == -3
    assert status.firmware == "7.4.4407"
    assert status.display_state == 7


async def test_checksum_mismatch() -> None:
    """A corrupted header is rejected."""
    data = bytearray(tablet_record(status_payload()))
    data[16] ^= 1

    with pytest.raises(pv3.ProtocolError):
        await _read(bytes(data))


async def test_status_response() -> None:
    """Server reply echoes kind; chunked and raw carry the same 44 bytes."""
    status = pv3.parse_status(await _read(tablet_record(status_payload(kind=5))))

    raw = await _read(pv3.status_response(status, pv3.Compression.RAW))
    chunked = await _read(pv3.status_response(status, pv3.Compression.CHUNKED))

    expected = struct.pack("<I16s6I", 0, UUID, 1, 5, 8, 0, 1, 0)
    assert raw.direction == pv3.Direction.SERVER
    assert raw.payload == expected
    assert unchunk(chunked.payload) == expected


async def test_ack() -> None:
    """Acknowledgement yields the echoed sequence; bad constants fail."""
    record = await _read(tablet_record(ack_payload(4)))
    assert pv3.parse_ack(record) == (UUID, 4)

    bad = bytearray(ack_payload(4))
    bad[28] = 9
    with pytest.raises(pv3.ProtocolError):
        pv3.parse_ack(await _read(tablet_record(bytes(bad))))


@pytest.mark.parametrize(
    ("raw", "physical"),
    [((924, 703), (99, 54)), ((491, 399), (532, 358)), ((100, 37), (923, 720))],
)
async def test_touch(raw: tuple[int, int], physical: tuple[int, int]) -> None:
    """Captured raw coordinates map to upright ones."""
    record = await _read(tablet_record(touch_payload(*raw, frame_id=0xEB3C1EA7)))
    touch = pv3.parse_touch(record)

    assert touch.frame_id == 0xEB3C1EA7
    assert touch.physical(WIDTH, HEIGHT) == physical


def test_touch_out_of_bounds() -> None:
    """Coordinates outside the panel are rejected."""
    touch = pv3.Touch(UUID, 0, WIDTH, 1)

    with pytest.raises(pv3.ProtocolError):
        touch.physical(WIDTH, HEIGHT)


async def test_build_image() -> None:
    """Image header and primitive layout match doc/protocol.md."""
    pixels = bytes(range(8))
    primitive = pv3.Primitive(x=2, y=3, width=4, height=4, pixels=pixels)
    record = await _read(pv3.build_image(UUID, 9, 0xDEADBEEF, [primitive]))
    logical = unchunk(record.payload)

    words = struct.unpack_from("<I16s9I", logical)
    assert words[1] == UUID
    assert words[2:] == (5, 9, len(logical) - 36, 0, 0xDEADBEEF, 1, 0, 32, 0)

    header = pv3.PRIMITIVE_HEADER.unpack_from(logical, 56)
    assert header == (1, 3 << 16 | 2, 4, 4, 0x102, 4, 8)
    assert logical[80:] == pixels


def test_odd_primitive_rejected() -> None:
    """Odd pixel counts cannot be packed."""
    with pytest.raises(pv3.ProtocolError):
        pv3.build_image(UUID, 1, 1, [pv3.Primitive(0, 0, 3, 1, b"\0")])


@pytest.mark.parametrize("size", [1, 14, 15, 269, 270, 4800, 4801, 20000])
def test_chunk_roundtrip(size: int) -> None:
    """Literal-only LZ4 chunks decode to the input, final index set."""
    data = bytes(i % 251 for i in range(size))
    chunked = pv3.chunk(data)

    assert unchunk(chunked) == data
    assert pv3.CHUNK_HEADER.unpack_from(chunked)[1] == (size - 1) // pv3.CHUNK_SIZE


# Go eink-server output for this reply (pv3.StatusResponse, pierrec LZ4 HC).
GO_STATUS_REPLY_KIND1 = bytes.fromhex(
    "03000000000000000100000041000000254d073d0000000000000000290000002c0000000000000000000000f3060000000036002b0001504b464d3233200000000001040010080d00b00000000100000000000000"
)


def test_status_reply_matches_go_bytes() -> None:
    """Chunked reply, LZ4 included, is byte-identical to the Go server."""
    uuid = bytes.fromhex("36002b0001504b464d32332000000000")
    status = pv3.Status(uuid=uuid, kind=1, fields={})

    reply = pv3.status_response(status, pv3.Compression.CHUNKED)

    assert reply == GO_STATUS_REPLY_KIND1
