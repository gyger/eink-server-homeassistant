"""Frame preparation and changed-region tests."""

import io

from PIL import Image

from custom_components.eink_server.frame import (
    Fit,
    Frame,
    Refresh,
    changed_region,
    render,
    restore,
)


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()


def _render(
    image: Image.Image, width: int, height: int, fit: Fit = Fit.CONTAIN
) -> Frame:
    return render(_png(image), width, height, fit, 0, Refresh.PARTIAL)


def test_pack_order_and_rotation() -> None:
    """Upright 4×1 [0,17,34,51] becomes wire [51,34,17,0], low nibble first."""
    image = Image.new("L", (4, 1))
    image.putdata([0, 17, 34, 51])

    frame = _render(image, 4, 1)

    assert frame.packed == bytes([3 | 2 << 4, 1 | 0 << 4])


def test_dashboard_levels_pass_through() -> None:
    """All 16 n×17 levels survive unchanged."""
    image = Image.new("L", (16, 2))
    image.putdata([n * 17 for n in range(16)] * 2)

    frame = _render(image, 16, 2)

    with Image.open(io.BytesIO(frame.preview)) as preview:
        assert list(preview.tobytes()) == [n * 17 for n in range(16)] * 2


def test_quantize_and_transparency() -> None:
    """Off-level grays round; transparent pixels become white."""
    image = Image.new("LA", (2, 1))
    image.putdata([(20, 255), (0, 0)])

    frame = _render(image, 2, 1)

    with Image.open(io.BytesIO(frame.preview)) as preview:
        assert list(preview.tobytes()) == [17, 255]


def test_fit_contain_pads_white() -> None:
    """A square source on a wide panel is letterboxed in white."""
    frame = _render(Image.new("L", (10, 10), 0), 20, 10)

    with Image.open(io.BytesIO(frame.preview)) as preview:
        assert preview.size == (20, 10)
        assert preview.getpixel((0, 5)) == 255
        assert preview.getpixel((10, 5)) == 0


def test_rotation_clockwise() -> None:
    """rotation=90 turns a portrait source to landscape."""
    image = Image.new("L", (2, 4), 255)
    image.putpixel((0, 0), 0)

    frame = render(_png(image), 4, 2, Fit.CONTAIN, 90, Refresh.PARTIAL)

    with Image.open(io.BytesIO(frame.preview)) as preview:
        assert preview.getpixel((3, 0)) == 0


def test_restore_keeps_id_and_pixels() -> None:
    """A persisted preview rebuilds the identical packed frame."""
    frame = _render(Image.effect_noise((8, 4), 64).convert("L"), 8, 4)

    restored = restore(frame.id, frame.preview, Refresh.FULL)

    assert restored.id == frame.id
    assert restored.packed == frame.packed
    assert restored.refresh == Refresh.FULL


def test_changed_region() -> None:
    """Only the changed rectangle is sent, with even X and width."""
    blank = _render(Image.new("L", (16, 8), 255), 16, 8)
    dot = Image.new("L", (16, 8), 255)
    dot.putpixel((5, 2), 0)
    changed = _render(dot, 16, 8)

    region = changed_region(changed, blank.packed)

    # Upright (5,2) is wire (10,5); byte 5 covers pixels 10-11.
    assert (region.x, region.y, region.width, region.height) == (10, 5, 2, 1)
    assert region.pixels == bytes([0xF0])


def test_changed_region_falls_back_to_full() -> None:
    """No base or no change sends the whole frame."""
    frame = _render(Image.new("L", (4, 2), 255), 4, 2)

    for base in (None, frame.packed, b"short"):
        region = changed_region(frame, base)
        assert (region.x, region.y, region.width, region.height) == (0, 0, 4, 2)
