"""Image → packed 4-bit panel frame, and changed-region detection.

Panel encoding: 16 gray levels, value n maps to intensity n×17, two pixels
per byte with the left pixel in the low nibble. The Joan 6 panel shows the
buffer rotated 180°, so frames are rotated before packing.

hass-eink-dashboard's 16-level output already uses n×17, so its images
pass through unchanged.
"""

from dataclasses import dataclass
from enum import StrEnum
import io
import secrets

from PIL import Image, ImageOps

from .pv3 import Primitive

LEVELS = 16
LEVEL_STEP = 255 // (LEVELS - 1)
WHITE = 255

# Intensity → level and intensity → nearest representable intensity.
TO_LEVEL = [round(v / LEVEL_STEP) for v in range(256)]
TO_GRAY = [level * LEVEL_STEP for level in TO_LEVEL]


class Fit(StrEnum):
    """How a source maps onto the panel when sizes differ."""

    CONTAIN = "contain"
    COVER = "cover"
    STRETCH = "stretch"


class Refresh(StrEnum):
    """Partial sends only the changed rectangle when the base is confirmed."""

    PARTIAL = "partial"
    FULL = "full"


@dataclass(frozen=True)
class Frame:
    """One displayable frame.

    preview: upright grayscale PNG for Home Assistant and persistence.
    packed: wire-orientation 4-bit pixels for the tablet.
    """

    id: int
    width: int
    height: int
    preview: bytes
    packed: bytes
    refresh: Refresh = Refresh.PARTIAL


def render(
    data: bytes, width: int, height: int, fit: Fit, rotation: int, refresh: Refresh
) -> Frame:
    """Decode any Pillow image and prepare it for a width×height panel."""
    with Image.open(io.BytesIO(data)) as source:
        image = _flatten(ImageOps.exif_transpose(source))

    # Clockwise, e.g. 90 for a portrait-mounted tablet.
    if rotation:
        image = image.rotate(-rotation, expand=True)

    gray = _fit(image, width, height, fit).point(TO_GRAY)
    return _frame(gray, refresh)


def restore(frame_id: int, preview: bytes, refresh: Refresh) -> Frame:
    """Rebuild a persisted frame from its preview PNG, keeping its ID."""
    with Image.open(io.BytesIO(preview)) as source:
        gray = source.convert("L")

    return _frame(gray, refresh, frame_id)


def changed_region(current: Frame, base: bytes | None) -> Primitive:
    """Return the smallest rectangle covering changed packed bytes.

    Falls back to the full frame without a comparable base or when
    nothing changed. X and width stay even because one byte holds two
    pixels.
    """
    full = Primitive(0, 0, current.width, current.height, current.packed)
    row = current.width // 2
    if base is None or len(base) != len(current.packed) or current.width % 2:
        return full

    top, bottom, left, right = None, -1, row, -1
    for y in range(current.height):
        start = y * row
        diff = _xor(current.packed[start : start + row], base[start : start + row])
        if not diff:
            continue

        # Big-endian XOR: the highest set bit is the first changed byte.
        first = row - 1 - (diff.bit_length() - 1) // 8
        last = row - 1 - ((diff & -diff).bit_length() - 1) // 8
        top = y if top is None else top
        bottom = y
        left = min(left, first)
        right = max(right, last)

    if top is None:
        return full

    span = right - left + 1
    pixels = b"".join(
        current.packed[y * row + left : y * row + left + span]
        for y in range(top, bottom + 1)
    )
    return Primitive(left * 2, top, span * 2, bottom - top + 1, pixels)


def _frame(gray: Image.Image, refresh: Refresh, frame_id: int = 0) -> Frame:
    """Encode an upright grayscale image as preview PNG and packed pixels."""
    if gray.width * gray.height % 2:
        raise ValueError("pixel count must be even")

    buf = io.BytesIO()
    gray.save(buf, format="PNG", optimize=True)

    levels = gray.rotate(180).point(TO_LEVEL).tobytes()
    packed = bytes(map(_pair, levels[0::2], levels[1::2], strict=True))

    return Frame(
        id=frame_id or _new_id(),
        width=gray.width,
        height=gray.height,
        preview=buf.getvalue(),
        packed=packed,
        refresh=refresh,
    )


def _flatten(image: Image.Image) -> Image.Image:
    """Grayscale with transparency composited onto white."""
    if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, (WHITE, WHITE, WHITE, WHITE))
        image = Image.alpha_composite(canvas, rgba)

    return image.convert("L")


def _fit(image: Image.Image, width: int, height: int, fit: Fit) -> Image.Image:
    """Scale to exactly width×height."""
    size = (width, height)
    if image.size == size:
        return image

    if fit == Fit.STRETCH:
        return image.resize(size, Image.Resampling.LANCZOS)
    if fit == Fit.COVER:
        return ImageOps.fit(image, size, Image.Resampling.LANCZOS)

    return ImageOps.pad(image, size, Image.Resampling.LANCZOS, color=WHITE)


def _pair(left: int, right: int) -> int:
    """Pack two levels; left pixel in the low nibble."""
    return left | right << 4


def _xor(a: bytes, b: bytes) -> int:
    """XOR two equal-length byte strings as big-endian integers."""
    return int.from_bytes(a, "big") ^ int.from_bytes(b, "big")


def _new_id() -> int:
    """Random non-zero 32-bit frame ID; 0 means "nothing displayed"."""
    return secrets.randbits(32) or 1
