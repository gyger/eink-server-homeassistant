"""Base entity and dynamic tablet discovery."""

from collections.abc import Callable, Iterable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity, EntityDescription

from .const import DOMAIN, MANUFACTURER, SIGNAL_NEW_TABLET, SIGNAL_UPDATE
from .hub import Hub, Tablet


class EinkServerEntity(Entity):
    """One entity of one tablet, pushed by the hub."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: Hub, uuid: str, description: EntityDescription) -> None:
        """Initialize the entity."""
        self.entity_description = description
        self._hub = hub
        self._uuid = uuid
        self._attr_unique_id = f"{uuid}-{description.key}"

        tablet = self.tablet
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, uuid)},
            manufacturer=MANUFACTURER,
            name=f"Tablet {uuid[:8]}",
            sw_version=tablet.firmware or None,
        )

    @property
    def tablet(self) -> Tablet:
        """Return the tablet's latest state."""
        return self._hub.tablets[self._uuid]

    async def async_added_to_hass(self) -> None:
        """Refresh on hub updates for this tablet."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, f"{SIGNAL_UPDATE}_{self._uuid}", self._handle_update
            )
        )

    @callback
    def _handle_update(self) -> None:
        self.async_write_ha_state()


@callback
def async_track_tablets(
    hass: HomeAssistant,
    hub: Hub,
    factory: Callable[[str], Iterable[Entity]],
    add: Callable[[list[Entity]], None],
) -> Callable[[], None]:
    """Add entities for known tablets and for tablets enrolling later."""
    add([entity for uuid in hub.tablets for entity in factory(uuid)])

    @callback
    def _new(uuid: str) -> None:
        add(list(factory(uuid)))

    return async_dispatcher_connect(hass, SIGNAL_NEW_TABLET, _new)
