"""Actions, mirroring opendisplay.upload_image so dashboards plug in alike.

Example (hass-eink-dashboard as the source):
    action: eink_server.upload_image
    data:
      device_id: 0123456789abcdef0123456789abcdef
      image:
        media_content_id: media-source://image/image.kitchen_dashboard
        media_content_type: image/png
"""

from datetime import timedelta
import importlib

import aiohttp
from homeassistant.components.http.auth import async_sign_path
from homeassistant.components.media_source import async_resolve_media
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.network import get_url
from homeassistant.helpers.selector import MediaSelector, MediaSelectorConfig
from PIL import UnidentifiedImageError
import voluptuous as vol

from .const import DOMAIN
from .frame import Fit, Refresh
from .hub import Hub, UnknownTabletError

SERVICE_UPLOAD_IMAGE = "upload_image"

ATTR_IMAGE = "image"
ATTR_FIT = "fit"
ATTR_ROTATION = "rotation"
ATTR_REFRESH = "refresh"

DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=30)
SIGNED_PATH_TTL = timedelta(minutes=5)

# Media sources that resolve to an endless MJPEG stream; take one snapshot.
SNAPSHOT_DOMAINS = ("camera", "image")

SCHEMA_UPLOAD_IMAGE = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
        vol.Required(ATTR_IMAGE): vol.Any(
            cv.url, MediaSelector(MediaSelectorConfig(accept=["image/*"]))
        ),
        vol.Optional(ATTR_FIT, default=Fit.CONTAIN): vol.Coerce(Fit),
        vol.Optional(ATTR_ROTATION, default=0): vol.All(
            vol.Coerce(int), vol.In([0, 90, 180, 270])
        ),
        vol.Optional(ATTR_REFRESH, default=Refresh.PARTIAL): vol.Coerce(Refresh),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register actions."""
    hass.services.async_register(
        DOMAIN, SERVICE_UPLOAD_IMAGE, _async_upload_image, schema=SCHEMA_UPLOAD_IMAGE
    )


async def _async_upload_image(call: ServiceCall) -> None:
    """Fetch the image and make it the tablet's desired frame."""
    hass = call.hass
    hub, uuid = _resolve_device(call)
    source = call.data[ATTR_IMAGE]

    # Plain URLs must be allowlisted; media-source items are trusted.
    if isinstance(source, str) and not hass.config.is_allowed_external_url(source):
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="url_not_allowed",
            translation_placeholders={"url": source},
        )

    if isinstance(source, str):
        image = await _async_download(hass, source)
    else:
        image = await _async_media(hass, source["media_content_id"])

    try:
        await hub.async_publish(
            uuid,
            image,
            call.data[ATTR_FIT],
            call.data[ATTR_ROTATION],
            call.data[ATTR_REFRESH],
        )
    except UnknownTabletError as err:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="tablet_never_connected"
        ) from err
    except (UnidentifiedImageError, OSError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="unsupported_image"
        ) from err


def _resolve_device(call: ServiceCall) -> tuple[Hub, str]:
    """Map an HA device ID to its hub and tablet UUID."""
    device_id: str = call.data[ATTR_DEVICE_ID]
    invalid = ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="invalid_device_id",
        translation_placeholders={"device_id": device_id},
    )

    device = dr.async_get(call.hass).async_get(device_id)
    if device is None:
        raise invalid

    uuid = next(
        (ident for domain, ident in device.identifiers if domain == DOMAIN), None
    )
    if uuid is None:
        raise invalid

    entry = call.hass.config_entries.async_get_entry(device.config_entry_id)
    if entry is None or entry.state is not ConfigEntryState.LOADED:
        raise invalid

    return entry.runtime_data, uuid


async def _async_media(hass: HomeAssistant, media_content_id: str) -> bytes:
    """Return the image bytes behind a media-source item."""
    domain, _, entity_id = media_content_id.removeprefix("media-source://").partition(
        "/"
    )

    # Camera/image entities: one snapshot instead of their MJPEG stream.
    if domain in SNAPSHOT_DOMAINS:
        component = await hass.async_add_import_executor_job(
            importlib.import_module, f"homeassistant.components.{domain}"
        )
        snapshot = await component.async_get_image(hass, entity_id)
        return snapshot.content

    media = await async_resolve_media(hass, media_content_id, None)
    if media.path is not None:
        return await hass.async_add_executor_job(media.path.read_bytes)

    return await _async_download(hass, media.url)


async def _async_download(hass: HomeAssistant, url: str) -> bytes:
    """Download an image; relative HA paths are signed first."""
    if not url.startswith(("http://", "https://")):
        url = get_url(hass) + async_sign_path(
            hass, url, SIGNED_PATH_TTL, use_content_user=True
        )

    session = async_get_clientsession(hass)
    try:
        async with session.get(url, timeout=DOWNLOAD_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.read()
    except (aiohttp.ClientError, TimeoutError) as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="media_download_error",
            translation_placeholders={"error": str(err)},
        ) from err
