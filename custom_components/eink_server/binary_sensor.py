"""Tablet connectivity."""

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EinkServerConfigEntry
from .entity import EinkServerEntity, async_track_tablets

PARALLEL_UPDATES = 0

CONNECTIVITY = BinarySensorEntityDescription(
    key="connected",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinkServerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up connectivity for every tablet."""
    hub = entry.runtime_data
    entry.async_on_unload(
        async_track_tablets(
            hass,
            hub,
            lambda uuid: [EinkConnected(hub, uuid, CONNECTIVITY)],
            async_add_entities,
        )
    )


class EinkConnected(EinkServerEntity, BinarySensorEntity):
    """Whether the tablet has an open TCP session."""

    @property
    def is_on(self) -> bool:
        """Return True while connected."""
        return self.tablet.connected
