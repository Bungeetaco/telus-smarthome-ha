"""Binary sensors for TELUS cameras (motion + connectivity).

Derived from the OCF resource model: /pirSensor and /aiSensor carry motion
events (value true/false); connectivityStatus carries online state.
"""
from __future__ import annotations

import time

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, MOTION_AUTO_OFF_SECONDS
from .entity import TelusEntity, get_resource, is_online, resources_of


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[BinarySensorEntity] = []
    for dev_id, dev in coordinator.data.items():
        hrefs = {r.get("href") for r in resources_of(dev)}
        if hrefs & {"/pirSensor", "/aiSensor"}:
            entities.append(TelusMotion(coordinator, dev_id))
        # Every TELUS device reports connectivity.
        entities.append(TelusConnectivity(coordinator, dev_id))
    async_add_entities(entities)


class TelusMotion(TelusEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_translation_key = "motion"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_motion"

    @property
    def is_on(self) -> bool | None:
        now_ms = time.time() * 1000
        for href in ("/pirSensor", "/aiSensor"):
            res = get_resource(self.device, href)
            if not res or res.get("value") is not True:
                continue
            # The backend cache holds the last event's value:true until the next
            # refresh, so a value:true with a stale timestamp must not latch
            # motion ON forever. Honour it only while the event is recent.
            ts = res.get("timestamp")
            if isinstance(ts, (int, float)) and ts > 0:
                if (now_ms - ts) > MOTION_AUTO_OFF_SECONDS * 1000:
                    continue
            return True
        return False


class TelusConnectivity(TelusEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "connectivity"
    _attr_entity_category = None

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_connectivity"

    @property
    def is_on(self) -> bool | None:
        return is_online(self.device)
