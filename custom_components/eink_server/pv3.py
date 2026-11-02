"""Joan/Visionect protocol-v3 codec. Pure byte handling, no I/O policy.

Port of eink-server internal/pv3; layouts in eink-server doc/protocol.md.

    record = [type][word1][direction][payload length][crc32] + payload
             └──────────── five little-endian uint32 ───────────┘
"""

import asyncio
from dataclasses import dataclass
from enum import IntEnum
import struct
import zlib

from . import lz4

HEADER = struct.Struct("<5I")
MAX_PAYLOAD = 32 << 20

# Server payloads are split into LZ4 chunks of at most this many bytes.
CHUNK_SIZE = 4800
CHUNK_HEADER = struct.Struct("<6I")

# Constant words observed in every captured message.
FIELD_SIZE_ACK = 8
TOUCH_SENTINEL = 0xFFFFFFFF
TOUCH_BODY_SIZE = 20
RECT_FLAGS = 0x102
ENCODING_4BIT = 4
IMAGE_TYPE_RECT = 1

STATUS_HEADER = 32
IMAGE_HEADER = 56
PRIMITIVE_HEADER = struct.Struct("<IIHHIII")


class Packet(IntEnum):
    """Outer record type."""

    BOOTLOADER = 2
    APPLICATION = 3


class Direction(IntEnum):
    """Outer word 2; also selects which bytes the CRC covers."""

    TABLET = 0
    SERVER = 1


class Message(IntEnum):
    """Logical message type at payload offset 20."""

    ACK = 1
    STATUS = 3
    IMAGE = 5
    TOUCH = 6


class Field(IntEnum):
    """Status table field IDs."""

    DISPLAY_STATE = 10
    BATTERY = 11
    TEMPERATURE = 13
    HUMIDITY = 15
    FIRMWARE_MAJOR = 17
    FIRMWARE_MINOR = 18
    FIRMWARE_REVISION = 19
    WIDTH = 39
    HEIGHT = 40


class Compression(IntEnum):
    """Status response encoding; the first reply of a session is chunked."""

    RAW = 0
    CHUNKED = 1


class ProtocolError(ValueError):
    """Malformed record or message."""


@dataclass(frozen=True)
class Record:
    """One outer record."""

    type: int
    direction: int
    payload: bytes
    word1: int = 0


@dataclass(frozen=True)
class Status:
    """Tablet status, sent on connect and every heartbeat."""

    uuid: bytes
    kind: int
    fields: dict[int, int]

    @property
    def display_state(self) -> int:
        """Frame ID currently on the panel."""
        return self.fields.get(Field.DISPLAY_STATE, 0)

    @property
    def battery(self) -> int:
        """Battery percent."""
        return self.fields.get(Field.BATTERY, 0)

    @property
    def temperature(self) -> int:
        """Temperature in °C, signed."""
        raw = self.fields.get(Field.TEMPERATURE, 0)
        return raw - (1 << 32) if raw & 0x80000000 else raw

    @property
    def humidity(self) -> int:
        """Relative humidity percent."""
        return self.fields.get(Field.HUMIDITY, 0)

    @property
    def width(self) -> int:
        """Native panel width."""
        return self.fields.get(Field.WIDTH, 0)

    @property
    def height(self) -> int:
        """Native panel height."""
        return self.fields.get(Field.HEIGHT, 0)

    @property
    def firmware(self) -> str:
        """Firmware version, e.g. 7.4.4407."""
        parts = (Field.FIRMWARE_MAJOR, Field.FIRMWARE_MINOR, Field.FIRMWARE_REVISION)
        return ".".join(str(self.fields.get(f, 0)) for f in parts)


@dataclass(frozen=True)
class Touch:
    """Completed contact in native (180° rotated) panel coordinates."""

    uuid: bytes
    frame_id: int
    raw_x: int
    raw_y: int

    def physical(self, width: int, height: int) -> tuple[int, int]:
        """Return top-left-origin coordinates, e.g. raw (924,703) → (99,54)."""
        if not (0 <= self.raw_x < width and 0 <= self.raw_y < height):
            raise ProtocolError("touch outside display bounds")
        return width - 1 - self.raw_x, height - 1 - self.raw_y


@dataclass(frozen=True)
class Primitive:
    """Packed 4-bit rectangle; origin and size in wire orientation."""

    x: int
    y: int
    width: int
    height: int
    pixels: bytes


def format_uuid(raw: bytes) -> str:
    """Format 16 bytes as 8-4-4-4-12 hex."""
    h = raw.hex()
    return f"{h[0:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


async def read_record(reader: asyncio.StreamReader) -> Record:
    """Read exactly one record; raises IncompleteReadError at EOF."""
    header = await reader.readexactly(HEADER.size)
    rtype, word1, direction, length, check = HEADER.unpack(header)
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"payload exceeds limit, header {header.hex()}")

    payload = await reader.readexactly(length)

    # Tablet records checksum their header, server records their payload.
    covered = header[:16] if direction == Direction.TABLET else payload
    if check != zlib.crc32(covered):
        raise ProtocolError("checksum mismatch")

    return Record(rtype, direction, payload, word1)


def marshal(record: Record) -> bytes:
    """Encode a record with its checksum."""
    if len(record.payload) > MAX_PAYLOAD:
        raise ProtocolError("payload exceeds limit")

    head = struct.pack(
        "<4I", record.type, record.word1, record.direction, len(record.payload)
    )
    covered = head if record.direction == Direction.TABLET else record.payload
    return head + struct.pack("<I", zlib.crc32(covered)) + record.payload


def message_type(record: Record) -> int:
    """Return the logical message type of a tablet application record."""
    if (
        record.type != Packet.APPLICATION
        or record.direction != Direction.TABLET
        or len(record.payload) < 24
    ):
        raise ProtocolError("not a tablet application message")

    return _u32(record.payload, 20)


def parse_status(record: Record) -> Status:
    """Decode a status: header, then [value, field ID] pairs."""
    p = record.payload
    if record.type != Packet.APPLICATION or len(p) < STATUS_HEADER:
        raise ProtocolError("not a status record")
    if _u32(p, 20) != Message.STATUS:
        raise ProtocolError("unsupported protocol version")

    size = _u32(p, 28)
    if size % 8 != 0 or STATUS_HEADER + size + 4 > len(p):
        raise ProtocolError("invalid status table length")

    pairs = struct.iter_unpack("<2I", p[STATUS_HEADER : STATUS_HEADER + size])
    fields = {key: value for value, key in pairs}
    return Status(uuid=p[4:20], kind=_u32(p, 24), fields=fields)


def status_response(status: Status, compression: Compression) -> bytes:
    """Build the 44-byte server reply to a status."""
    logical = struct.pack(
        "<I16s6I", 0, status.uuid, 1, status.kind, FIELD_SIZE_ACK, 0, 1, 0
    )
    payload = chunk(logical) if compression == Compression.CHUNKED else logical
    return marshal(Record(Packet.APPLICATION, Direction.SERVER, payload))


def parse_ack(record: Record) -> tuple[bytes, int]:
    """Decode a post-image acknowledgement into (uuid, echoed sequence)."""
    p = record.payload
    if len(p) != 44:
        raise ProtocolError("invalid acknowledgement shape")

    words = struct.unpack("<I16s6I", p)
    if words[0] != 0 or words[2] != Message.ACK or words[4:] != (8, 0, 1, 0):
        raise ProtocolError("invalid acknowledgement payload")

    return words[1], words[3]


def parse_touch(record: Record) -> Touch:
    """Decode a 56-byte touch message."""
    p = record.payload
    if len(p) != 56:
        raise ProtocolError("invalid touch shape")

    reserved, uuid, mtype, sentinel, size, zero, frame_id, w1, w2, x, y = struct.unpack(
        "<I16s9I", p
    )
    expected = (0, Message.TOUCH, TOUCH_SENTINEL, TOUCH_BODY_SIZE, 0, 0, 0)
    if (reserved, mtype, sentinel, size, zero, w1, w2) != expected:
        raise ProtocolError("invalid touch payload")

    return Touch(uuid=uuid, frame_id=frame_id, raw_x=x, raw_y=y)


def build_image(
    uuid: bytes, sequence: int, frame_id: int, primitives: list[Primitive]
) -> bytes:
    """Build an image record; later primitives draw over earlier ones."""
    if not primitives:
        raise ProtocolError("image requires a primitive")

    body = b"".join(_primitive(p) for p in primitives)
    header = struct.pack(
        "<I16s9I",
        0,
        uuid,
        Message.IMAGE,
        sequence,
        IMAGE_HEADER - 36 + len(body),
        0,
        frame_id,
        len(primitives),
        0,
        len(body),
        0,
    )
    payload = chunk(header + body)
    return marshal(Record(Packet.APPLICATION, Direction.SERVER, payload))


def chunk(data: bytes) -> bytes:
    """Split into ≤4800-byte LZ4 chunks.

    Each chunk is one LZ4 block, byte-identical to the Go server's.
    Chunk header: index, final index, block size, raw size, flags,
    reserved.
    """
    final = (len(data) - 1) // CHUNK_SIZE
    out = bytearray()
    for index, start in enumerate(range(0, len(data), CHUNK_SIZE)):
        raw = data[start : start + CHUNK_SIZE]
        block = lz4.compress(raw)
        out += CHUNK_HEADER.pack(index, final, len(block), len(raw), 0, 0)
        out += block

    return bytes(out)


def _primitive(p: Primitive) -> bytes:
    """Encode one rectangle: header then packed pixels."""
    count = p.width * p.height
    valid = (
        0 <= p.x <= 0xFFFF
        and 0 <= p.y <= 0xFFFF
        and 0 < p.width <= 0xFFFF
        and 0 < p.height <= 0xFFFF
        and count % 2 == 0
        and len(p.pixels) == count // 2
    )
    if not valid:
        raise ProtocolError("invalid packed image primitive")

    header = PRIMITIVE_HEADER.pack(
        IMAGE_TYPE_RECT,
        p.y << 16 | p.x,
        p.width,
        p.height,
        RECT_FLAGS,
        ENCODING_4BIT,
        len(p.pixels),
    )
    return header + p.pixels


def _u32(data: bytes, offset: int) -> int:
    """Read a little-endian uint32."""
    return struct.unpack_from("<I", data, offset)[0]
