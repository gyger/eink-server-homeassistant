"""Tests for the eink-server integration."""

import asyncio
import struct
import zlib

from custom_components.eink_server import pv3

# UUID of the captured Joan 6 tablet.
UUID_TEXT = "30004f00-0650-4858-5239-312000000000"
UUID = bytes.fromhex(UUID_TEXT.replace("-", ""))
WIDTH, HEIGHT = 1024, 758
TIMEOUT = 5


def tablet_record(payload: bytes, rtype: int = pv3.Packet.APPLICATION) -> bytes:
    """Encode a tablet-direction record (CRC over header)."""
    head = struct.pack("<4I", rtype, 0, pv3.Direction.TABLET, len(payload))
    return head + struct.pack("<I", zlib.crc32(head)) + payload


def status_payload(
    kind: int = 1, display_state: int = 0, uuid: bytes = UUID, size: bool = True
) -> bytes:
    """Build a status payload like the captured 552-byte one."""
    fields = {
        pv3.Field.DISPLAY_STATE: display_state,
        pv3.Field.BATTERY: 100,
        pv3.Field.TEMPERATURE: (-3) % (1 << 32),
        pv3.Field.HUMIDITY: 28,
        pv3.Field.FIRMWARE_MAJOR: 7,
        pv3.Field.FIRMWARE_MINOR: 4,
        pv3.Field.FIRMWARE_REVISION: 4407,
        pv3.Field.WIDTH: WIDTH,
        pv3.Field.HEIGHT: HEIGHT,
    }
    if not size:
        del fields[pv3.Field.WIDTH], fields[pv3.Field.HEIGHT]
    table = b"".join(struct.pack("<2I", v, k) for k, v in fields.items())
    head = struct.pack("<I16s3I", 0, uuid, pv3.Message.STATUS, kind, len(table))
    return head + table + b"\0\0\0\0"


def ack_payload(sequence: int, uuid: bytes = UUID) -> bytes:
    """Build a post-image acknowledgement."""
    return struct.pack("<I16s6I", 0, uuid, pv3.Message.ACK, sequence, 8, 0, 1, 0)


def touch_payload(raw_x: int, raw_y: int, frame_id: int = 0) -> bytes:
    """Build a touch message."""
    return struct.pack(
        "<I16s9I",
        0,
        UUID,
        pv3.Message.TOUCH,
        pv3.TOUCH_SENTINEL,
        pv3.TOUCH_BODY_SIZE,
        0,
        frame_id,
        0,
        0,
        raw_x,
        raw_y,
    )


def lz4_decode(block: bytes, size: int) -> bytes:
    """Reference LZ4 block decoder (spec-level, byte-wise copies)."""
    out = bytearray()
    pos = 0
    while True:
        token = block[pos]
        pos += 1
        length, pos = _lz4_length(block, pos, token >> 4)
        out += block[pos : pos + length]
        pos += length
        if pos == len(block):
            break

        offset = int.from_bytes(block[pos : pos + 2], "little")
        pos += 2
        assert 0 < offset <= len(out)
        match, pos = _lz4_length(block, pos, token & 15)
        for _ in range(match + 4):
            out.append(out[-offset])

    assert len(out) == size
    return bytes(out)


def _lz4_length(block: bytes, pos: int, length: int) -> tuple[int, int]:
    if length == 15:
        while block[pos] == 255:
            length += 255
            pos += 1
        length += block[pos]
        pos += 1
    return length, pos


def unchunk(payload: bytes) -> bytes:
    """Decode server LZ4 chunks back to the logical message."""
    out = bytearray()
    offset = 0
    while offset < len(payload):
        _, _, size, raw, _, _ = pv3.CHUNK_HEADER.unpack_from(payload, offset)
        offset += pv3.CHUNK_HEADER.size
        out += lz4_decode(payload[offset : offset + size], raw)
        offset += size

    return bytes(out)


class Tablet:
    """Minimal PV3 tablet client."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader, self.writer = reader, writer

    async def send(self, payload: bytes) -> None:
        self.writer.write(tablet_record(payload))
        await self.writer.drain()

    async def receive(self) -> bytes:
        """Return the next server record's logical payload."""
        record = await asyncio.wait_for(pv3.read_record(self.reader), TIMEOUT)
        assert record.direction == pv3.Direction.SERVER
        return record.payload

    async def status(self, kind: int, display_state: int = 0) -> bytes:
        """Send a status and return the reply payload."""
        await self.send(status_payload(kind, display_state))
        return await self.receive()

    async def image(self) -> tuple[int, int, pv3.Primitive]:
        """Receive an image; return (sequence, frame ID, primitive)."""
        logical = unchunk(await self.receive())
        words = struct.unpack_from("<I16s9I", logical)
        assert words[2] == pv3.Message.IMAGE
        _, origin, w, h, _, _, size = pv3.PRIMITIVE_HEADER.unpack_from(logical, 56)
        pixels = logical[80 : 80 + size]
        return (
            words[3],
            words[6],
            pv3.Primitive(origin & 0xFFFF, origin >> 16, w, h, pixels),
        )

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()


async def settle(predicate, timeout: float = TIMEOUT) -> None:
    """Wait until predicate() holds."""
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)
