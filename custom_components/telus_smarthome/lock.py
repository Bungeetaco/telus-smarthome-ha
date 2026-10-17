"""Lock platform (door locks via DoorStateCapability)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAP_DOOR_STATE, DOMAIN
from .entity import TelusEntity, capability_ids, get_capability


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = [
        TelusLock(coordinator, dev_id)
        for dev_id, dev in coordinator.data.items()
        if capability_ids(dev) & {"doorstate", "lock"}
    ]
    async_add_entities(entities)


class TelusLock(TelusEntity, LockEntity):
    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_lock"

    @property
    def name(self) -> None:
        return None

    @property
    def is_locked(self) -> bool | None:
        cap = get_capability(self.device, "doorState", "lock")
        if not cap:
            return None
        val = cap.get("value") or cap.get("state")
        return str(val).lower() in ("locked", "lock", "closed", "true")

    async def async_lock(self, **kwargs: Any) -> None:
        await self._send(CAP_DOOR_STATE, "lock")

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._send(CAP_DOOR_STATE, "unlock")
