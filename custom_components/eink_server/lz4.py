"""LZ4 block compressor, byte-identical to the Go eink-server's.

Faithful port of pierrec/lz4 v4 CompressorHC.CompressBlock (level 0, full
64 KiB chain search), which the Go server uses for every PV3 chunk. Joan
tablets accept its output; matching it byte for byte removes the encoder
as a variable.

Sequence layout:
    token  [literal length ext]  literals  offset(2, LE)  [match length ext]
    └ high nibble: literal length, low nibble: match length - 4; 15 = "more"
"""

MIN_MATCH = 4
WIN_SIZE_LOG = 16
WIN_SIZE = 1 << WIN_SIZE_LOG
WIN_MASK = WIN_SIZE - 1
HT_SIZE = 1 << 16

# pierrec: the last match cannot start within the last 14 bytes, and
# matches stop there too (stricter than the LZ4 spec's 12/5).
MF_LIMIT = 10 + MIN_MATCH

# Knuth multiplicative hash of 4 bytes into 16 bits.
HASHER = 2654435761
U32 = 0xFFFFFFFF

# Skip faster through incompressible data: 1 + (gap >> 7) bytes.
ADAPT_SKIP_LOG = 7
RUN_MASK = 15


def compress(src: bytes) -> bytes:
    """Compress one block exactly like Go's CompressorHC."""
    hash_table = [0] * HT_SIZE
    chain_table = [0] * HT_SIZE
    out = bytearray()
    size = len(src)
    limit = size - MF_LIMIT
    anchor = pos = 0

    while pos < limit:
        word = int.from_bytes(src[pos : pos + 4], "little")
        slot = _hash(word)

        # Follow the chain within the window, keep the longest match.
        length = offset = 0
        candidate = hash_table[slot]
        while candidate > 0 and pos - candidate < WIN_SIZE:
            if src[candidate + length] == src[pos + length]:
                found = _match_length(src, candidate, pos, limit)
                if found >= MIN_MATCH and found > length:
                    length, offset = found, pos - candidate
            candidate = chain_table[candidate & WIN_MASK]

        chain_table[pos & WIN_MASK] = hash_table[slot]
        hash_table[slot] = pos

        if length == 0:
            pos += 1 + ((pos - anchor) >> ADAPT_SKIP_LOG)
            continue

        # Index every position inside the match, rolling the 4-byte word.
        start = max(pos + 1, pos + length - WIN_SIZE)
        for inner in range(start, pos + length):
            word = (word >> 8) | src[inner + 3] << 24
            slot = _hash(word)
            chain_table[inner & WIN_MASK] = hash_table[slot]
            hash_table[slot] = inner

        _sequence(out, src[anchor:pos], offset, length)
        pos += length
        anchor = pos

    _last(out, src[anchor:])
    return bytes(out)


def literal(src: bytes) -> bytes:
    """Encode src as a single literal-only sequence."""
    out = bytearray()
    _last(out, src)
    return bytes(out)


def _hash(word: int) -> int:
    """Go: x * 2654435761 >> 16 on uint32."""
    return (word * HASHER & U32) >> (32 - WIN_SIZE_LOG)


def _match_length(src: bytes, candidate: int, pos: int, limit: int) -> int:
    """Count equal bytes 8 at a time, like Go; may pass limit by up to 7."""
    length = 0
    while length < limit - pos:
        a = int.from_bytes(src[candidate + length : candidate + length + 8], "little")
        b = int.from_bytes(src[pos + length : pos + length + 8], "little")
        diff = a ^ b
        if diff == 0:
            length += 8
            continue

        # Trailing zero bits / 8 = equal leading bytes in memory order.
        return length + (((diff & -diff).bit_length() - 1) >> 3)

    return length


def _sequence(out: bytearray, literals: bytes, offset: int, length: int) -> None:
    """Append literals followed by a back-reference."""
    extra = length - MIN_MATCH
    out.append(min(len(literals), RUN_MASK) << 4 | min(extra, RUN_MASK))
    _length(out, len(literals))
    out += literals
    out += offset.to_bytes(2, "little")
    _length(out, extra)


def _last(out: bytearray, literals: bytes) -> None:
    """Append the final, literal-only sequence."""
    out.append(min(len(literals), RUN_MASK) << 4)
    _length(out, len(literals))
    out += literals


def _length(out: bytearray, value: int) -> None:
    """Append length extension bytes, e.g. 20 → 05, 270 → FF 00."""
    if value < RUN_MASK:
        return

    value -= RUN_MASK
    while value >= 255:
        out.append(255)
        value -= 255

    out.append(value)
