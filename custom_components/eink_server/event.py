"""Touch taps as event entities."""

from typing import Any

from homeassistant.components.event import EventEntity, EventEntityDescription
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EinkServerConfigEntry
from .const import SIGNAL_TOUCH
from .entity import EinkServerEntity, async_track_tablets

PARALLEL_UPDATES = 0

EVENT_TAP = "tap"

TOUCH = EventEntityDescription(
    key="touch", translation_key="touch", event_types=[EVENT_TAP]
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinkServerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up a touch event entity for every tablet."""
    hub = entry.runtime_data
    entry.async_on_unload(
        async_track_tablets(
            hass,
            hub,
            lambda uuid: [EinkTouch(hub, uuid, TOUCH)],
            async_add_entities,
        )
    )


class EinkTouch(EinkServerEntity, EventEntity):
    """Fires "tap" with x, y in upright panel pixels and the frame ID."""

    async def async_added_to_hass(self) -> None:
        """Subscribe to taps of this tablet."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_TOUCH}_{self._uuid}", self._handle_touch
            )
        )

    @callback
    def _handle_touch(self, data: dict[str, Any]) -> None:
        self._trigger_event(EVENT_TAP, data)
        self.async_write_ha_state()
