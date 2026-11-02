"""LZ4 block compressor tests."""

import os

import pytest

from custom_components.eink_server import lz4

from . import lz4_decode

WHITE = b"\xff" * 4800


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"a",
        b"abcdabcdabcd",
        b"abcdabcdabcdabcd" * 3,
        WHITE,
        b"\xff" * 2000 + bytes(range(256)) * 4 + b"\xff" * 1776,
        os.urandom(4800),
        bytes(i % 7 for i in range(4800)),
    ],
)
def test_roundtrip(data: bytes) -> None:
    """Compressed blocks decode to the input."""
    assert lz4_decode(lz4.compress(data), len(data)) == data
    assert lz4_decode(lz4.literal(data), len(data)) == data


def test_white_compresses() -> None:
    """A white chunk shrinks to a few bytes."""
    assert len(lz4.compress(WHITE)) < 40
