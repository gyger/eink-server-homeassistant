"""Telemetry and delivery-state sensors."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import EinkServerConfigEntry
from .entity import EinkServerEntity, async_track_tablets
from .gateway import Delivery
from .hub import Tablet

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class EinkSensorDescription(SensorEntityDescription):
    """Sensor description with a value extractor."""

    value: Callable[[Tablet], Any]


SENSORS: tuple[EinkSensorDescription, ...] = (
    EinkSensorDescription(
        key="battery",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda t: t.battery,
    ),
    EinkSensorDescription(
        key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda t: t.temperature,
    ),
    EinkSensorDescription(
        key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value=lambda t: t.humidity,
    ),
    EinkSensorDescription(
        key="last_seen",
        translation_key="last_seen",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda t: t.last_seen,
    ),
    EinkSensorDescription(
        key="delivery",
        translation_key="delivery",
        device_class=SensorDeviceClass.ENUM,
        options=[state.value for state in Delivery],
        entity_category=EntityCategory.DIAGNOSTIC,
        value=lambda t: t.delivery,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EinkServerConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up sensors for every tablet."""
    hub = entry.runtime_data
    entry.async_on_unload(
        async_track_tablets(
            hass,
            hub,
            lambda uuid: [EinkSensor(hub, uuid, d) for d in SENSORS],
            async_add_entities,
        )
    )


class EinkSensor(EinkServerEntity, SensorEntity):
    """One telemetry value of a tablet."""

    entity_description: EinkSensorDescription

    @property
    def native_value(self) -> Any:
        """Return the current value."""
        return self.entity_description.value(self.tablet)
