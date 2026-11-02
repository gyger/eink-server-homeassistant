"""Preview of the frame a tablet should be showing."""

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import EinkServerConfigEntry
from .entity import EinkServerEntity, async_track_tablets
from .hub import Hub

PARALLEL_UPDATES = 0

CONTENT = EntityDescription(key="content", translation_key="content")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinkServerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a preview image for every tablet."""
    hub = entry.runtime_data
    entry.async_on_unload(
        async_track_tablets(
            hass,
            hub,
            lambda uuid: [EinkPreview(hass, hub, uuid)],
            async_add_entities,
        )
    )


class EinkPreview(EinkServerEntity, ImageEntity):
    """Upright grayscale preview of the desired frame."""

    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, hub: Hub, uuid: str) -> None:
        """Initialize both parents; ImageEntity needs hass for its token."""
        EinkServerEntity.__init__(self, hub, uuid, CONTENT)
        ImageEntity.__init__(self, hass)
        self._frame_id: int | None = None
        self._track_frame()

    async def async_image(self) -> bytes | None:
        """Return the preview PNG."""
        frame = self.tablet.frame
        return frame.preview if frame else None

    @callback
    def _handle_update(self) -> None:
        """Bump last-updated when a new frame is desired."""
        self._track_frame()
        super()._handle_update()

    def _track_frame(self) -> None:
        frame = self.tablet.frame
        frame_id = frame.id if frame else None
        if frame_id == self._frame_id:
            return

        self._frame_id = frame_id
        self._attr_image_last_updated = dt_util.utcnow()
