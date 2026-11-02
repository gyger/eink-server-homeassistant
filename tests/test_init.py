"""End-to-end tests: Home Assistant hosting a simulated tablet."""

import io

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from PIL import Image
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
)

from custom_components.eink_server.const import DOMAIN

from . import HEIGHT, UUID_TEXT, WIDTH, ack_payload, settle, touch_payload
from .conftest import Connect

IMAGE_URL = "http://dashboard.local/kitchen.png"


def _png(width: int, height: int, color: int = 0) -> bytes:
    buf = io.BytesIO()
    Image.new("L", (width, height), color).save(buf, format="PNG")
    return buf.getvalue()


def _entity(hass: HomeAssistant, domain: str, key: str) -> str:
    """Return the entity ID for a tablet entity key."""
    entity_id = er.async_get(hass).async_get_entity_id(
        domain, DOMAIN, f"{UUID_TEXT}-{key}"
    )
    assert entity_id
    return entity_id


def _state(hass: HomeAssistant, domain: str, key: str) -> str | None:
    state = hass.states.get(_entity(hass, domain, key))
    return state.state if state else None


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _device(hass: HomeAssistant, entry: MockConfigEntry) -> dr.DeviceEntry | None:
    return dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, UUID_TEXT), entry.entry_id
    )


async def _upload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device = _device(hass, entry)
    assert device
    await hass.services.async_call(
        DOMAIN,
        "upload_image",
        {"device_id": device.id, "image": IMAGE_URL},
        blocking=True,
    )


@pytest.fixture
def image_url(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """Serve a full-panel black PNG at an allowlisted URL."""
    hass.config.allowlist_external_urls = {IMAGE_URL}
    aioclient_mock.get(IMAGE_URL, content=_png(WIDTH, HEIGHT))


async def test_config_flow(hass: HomeAssistant) -> None:
    """The user step creates one listener entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PORT: 11113}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_PORT: 11113}


async def test_tablet_enrolls(
    hass: HomeAssistant, entry: MockConfigEntry, connect: Connect
) -> None:
    """A status creates the device and its telemetry entities."""
    await _setup(hass, entry)
    tablet = await connect()
    await tablet.status(kind=1)
    await settle(lambda: hass.states.async_entity_ids("sensor"))
    await hass.async_block_till_done()

    assert _device(hass, entry)
    assert _state(hass, "sensor", "battery") == "100"
    assert _state(hass, "sensor", "temperature") == "-3"
    assert _state(hass, "binary_sensor", "connected") == "on"

    await tablet.close()
    await settle(lambda: _state(hass, "binary_sensor", "connected") == "off")


@pytest.mark.usefixtures("image_url")
async def test_upload_and_deliver(
    hass: HomeAssistant, entry: MockConfigEntry, connect: Connect
) -> None:
    """upload_image renders, sends, and tracks delivery to the echo."""
    await _setup(hass, entry)
    tablet = await connect()
    await tablet.status(kind=1)
    await settle(lambda: hass.states.async_entity_ids("sensor"))

    await _upload(hass, entry)
    seq, frame_id, primitive = await tablet.image()

    assert (primitive.width, primitive.height) == (WIDTH, HEIGHT)
    assert set(primitive.pixels) == {0}

    await tablet.send(ack_payload(seq))
    await settle(lambda: _state(hass, "sensor", "delivery") == "acknowledged")

    await tablet.status(kind=3, display_state=frame_id)
    await settle(lambda: _state(hass, "sensor", "delivery") == "delivered")
    assert _state(hass, "image", "content") not in (None, "unknown")


@pytest.mark.usefixtures("image_url")
async def test_frame_survives_restart(
    hass: HomeAssistant, entry: MockConfigEntry, connect: Connect
) -> None:
    """A reloaded entry keeps the frame and does not resend it."""
    await _setup(hass, entry)
    tablet = await connect()
    await tablet.status(kind=1)
    await settle(lambda: hass.states.async_entity_ids("sensor"))
    await _upload(hass, entry)
    _, frame_id, _ = await tablet.image()
    await tablet.close()

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    tablet = await connect()
    await tablet.status(kind=1, display_state=frame_id)
    await settle(lambda: _state(hass, "sensor", "delivery") == "delivered")


async def test_touch_event(
    hass: HomeAssistant, entry: MockConfigEntry, connect: Connect
) -> None:
    """A tap updates the event entity with upright coordinates."""
    await _setup(hass, entry)
    tablet = await connect()
    await tablet.status(kind=1)
    await settle(lambda: hass.states.async_entity_ids("event"))
    await hass.async_block_till_done()

    await tablet.send(touch_payload(924, 703, frame_id=7))
    entity_id = _entity(hass, "event", "touch")
    await settle(lambda: hass.states.get(entity_id).attributes.get("x") is not None)

    attributes = hass.states.get(entity_id).attributes
    assert (attributes["event_type"], attributes["x"], attributes["y"]) == (
        "tap",
        99,
        54,
    )
    assert attributes["frame_id"] == 7


async def test_unknown_device_rejected(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A device ID of another integration is rejected."""
    await _setup(hass, entry)

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN,
            "upload_image",
            {"device_id": "missing", "image": IMAGE_URL},
            blocking=True,
        )


async def test_unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Unloading closes the listener."""
    await _setup(hass, entry)

    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED
