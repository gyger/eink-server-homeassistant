"""Bridges the PV3 gateway to Home Assistant state and persistence.

platforms / actions
      │ dispatcher signals, publish()
     Hub ── Store (.storage/eink_server): tablets + desired frame
      │ Listener callbacks, Gateway.publish()
   Gateway ── TCP :11113 ── tablets
"""

import base64
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from . import pv3
from .const import (
    DOMAIN,
    SIGNAL_NEW_TABLET,
    SIGNAL_TOUCH,
    SIGNAL_UPDATE,
    STORAGE_SAVE_DELAY,
    STORAGE_VERSION,
)
from .frame import Fit, Frame, Refresh, render, restore
from .gateway import Delivery, Gateway

_LOGGER = logging.getLogger(__name__)


@dataclass
class Tablet:
    """Last known state of one tablet."""

    uuid: str
    width: int = 0
    height: int = 0
    firmware: str = ""
    battery: int | None = None
    temperature: int | None = None
    humidity: int | None = None
    last_seen: datetime | None = None
    connected: bool = False
    frame: Frame | None = field(default=None, repr=False)
    delivery: Delivery | None = None


class UnknownTabletError(Exception):
    """The tablet has never connected, so its panel size is unknown."""


class Hub:
    """Owns the gateway and the tablet registry for one config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Create a stopped hub."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}"
        )
        self.tablets: dict[str, Tablet] = {}
        self._gateway = Gateway(self)

    async def async_start(self, port: int) -> None:
        """Load persisted tablets, restore desired frames, then listen."""
        for uuid, raw in ((await self._store.async_load()) or {}).items():
            self.tablets[uuid] = await self._hass.async_add_executor_job(
                _load, uuid, raw
            )

        for tablet in self.tablets.values():
            if tablet.frame:
                self._gateway.publish(tablet.uuid, tablet.frame)

        await self._gateway.start(None, port)

    async def async_stop(self) -> None:
        """Stop listening and flush state."""
        await self._gateway.stop()
        await self._store.async_save(self._dump())

    async def async_publish(
        self, uuid: str, image: bytes, fit: Fit, rotation: int, refresh: Refresh
    ) -> None:
        """Render an image for the tablet and make it the desired frame."""
        tablet = self.tablets.get(uuid)
        if tablet is None or not tablet.width:
            raise UnknownTabletError(uuid)

        frame = await self._hass.async_add_executor_job(
            render, image, tablet.width, tablet.height, fit, rotation, refresh
        )

        # A re-render with unchanged pixels would redraw the whole panel,
        # because an empty changed region falls back to full size.
        if _unchanged(tablet.frame, frame):
            return

        tablet.frame = frame
        self._gateway.publish(uuid, frame)
        self._save()

    @callback
    def on_status(self, uuid: str, status: pv3.Status) -> None:
        """Record telemetry; a first status enrolls the tablet."""
        tablet = self.tablets.get(uuid)
        new = tablet is None
        if tablet is None:
            tablet = self.tablets[uuid] = Tablet(uuid)

        if status.width and status.height:
            tablet.width, tablet.height = status.width, status.height
            tablet.firmware = status.firmware
        tablet.battery = status.battery
        tablet.temperature = status.temperature
        tablet.humidity = status.humidity
        tablet.last_seen = dt_util.utcnow()
        tablet.connected = True
        self._save()

        if new:
            async_dispatcher_send(self._hass, SIGNAL_NEW_TABLET, uuid)
            return

        self._notify(uuid)

    @callback
    def on_connection(self, uuid: str, connected: bool) -> None:
        """Track the open session; enrollment waits for the status."""
        tablet = self.tablets.get(uuid)
        if tablet is None:
            return

        tablet.connected = connected
        self._notify(uuid)

    @callback
    def on_delivery(self, uuid: str, frame_id: int, state: Delivery) -> None:
        """Track the desired frame's delivery."""
        tablet = self.tablets.get(uuid)
        if tablet is None or tablet.frame is None or tablet.frame.id != frame_id:
            return

        tablet.delivery = state
        self._notify(uuid)

    @callback
    def on_touch(self, uuid: str, frame_id: int, x: int, y: int) -> None:
        """Forward a tap to the tablet's event entity."""
        async_dispatcher_send(
            self._hass,
            f"{SIGNAL_TOUCH}_{uuid}",
            {"x": x, "y": y, "frame_id": frame_id},
        )

    @callback
    def _notify(self, uuid: str) -> None:
        async_dispatcher_send(self._hass, f"{SIGNAL_UPDATE}_{uuid}")

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._dump, STORAGE_SAVE_DELAY)

    def _dump(self) -> dict[str, Any]:
        """Serialize tablets; frames persist as their preview PNG."""
        return {uuid: _serialize(tablet) for uuid, tablet in self.tablets.items()}


def _unchanged(current: Frame | None, new: Frame) -> bool:
    """True for a partial frame showing the same pixels; full forces a redraw."""
    if current is None or new.refresh == Refresh.FULL:
        return False

    return current.packed == new.packed


def _serialize(tablet: Tablet) -> dict[str, Any]:
    """Serialize one tablet; session state is not persisted."""
    raw: dict[str, Any] = {
        "width": tablet.width,
        "height": tablet.height,
        "firmware": tablet.firmware,
        "battery": tablet.battery,
        "temperature": tablet.temperature,
        "humidity": tablet.humidity,
        "last_seen": tablet.last_seen.isoformat() if tablet.last_seen else None,
        "frame": None,
    }
    if tablet.frame:
        raw["frame"] = {
            "id": tablet.frame.id,
            "refresh": tablet.frame.refresh,
            "preview": base64.b64encode(tablet.frame.preview).decode(),
        }

    return raw


def _load(uuid: str, raw: dict[str, Any]) -> Tablet:
    """Rebuild a tablet; CPU-bound frame repacking runs in an executor."""
    tablet = Tablet(
        uuid=uuid,
        width=raw.get("width", 0),
        height=raw.get("height", 0),
        firmware=raw.get("firmware", ""),
        battery=raw.get("battery"),
        temperature=raw.get("temperature"),
        humidity=raw.get("humidity"),
        last_seen=dt_util.parse_datetime(raw["last_seen"])
        if raw.get("last_seen")
        else None,
    )

    if stored := raw.get("frame"):
        preview = base64.b64decode(stored["preview"])
        tablet.frame = restore(stored["id"], preview, Refresh(stored["refresh"]))

    return tablet
