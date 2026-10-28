"""Switch platform (binary switch / smart plug)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAP_BINARY_SWITCH, DOMAIN
from .entity import TelusEntity, capability_ids, get_capability


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for dev_id, dev in coordinator.data.items():
        caps = capability_ids(dev)
        # A device with brightness/color is a light, not a plain switch.
        if "binaryswitch" in caps and not (caps & {"brightness", "dimming"}):
            entities.append(TelusSwitch(coordinator, dev_id))
    async_add_entities(entities)


class TelusSwitch(TelusEntity, SwitchEntity):
    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_switch"

    @property
    def name(self) -> None:
        return None

    @property
    def is_on(self) -> bool | None:
        cap = get_capability(self.device, "binarySwitch")
        if not cap:
            return None
        val = cap.get("value") or cap.get("state") or cap.get("status")
        return str(val).lower() in ("on", "true", "1", "active")

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send(CAP_BINARY_SWITCH, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(CAP_BINARY_SWITCH, "off")
