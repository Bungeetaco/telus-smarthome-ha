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

from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .bridge_health import BridgeHealthCoordinator
from .const import DOMAIN, MOTION_AUTO_OFF_SECONDS
from .entity import TelusEntity, device_type_of, get_resource, is_online, resources_of


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator = store["coordinator"]
    bridge_health: BridgeHealthCoordinator | None = store.get("bridge_health")
    entities: list[BinarySensorEntity] = []
    for dev_id, dev in coordinator.data.items():
        hrefs = {r.get("href") for r in resources_of(dev)}
        if hrefs & {"/pirSensor", "/aiSensor"}:
            entities.append(TelusMotion(coordinator, dev_id))
        # Every TELUS device reports connectivity.
        entities.append(TelusConnectivity(coordinator, dev_id))
        if bridge_health and (
            "camera" in device_type_of(dev) or "doorbell" in device_type_of(dev)
        ):
            entities.append(TelusBridgeStream(bridge_health, dev_id))
    async_add_entities(entities)


class TelusBridgeStream(CoordinatorEntity[BridgeHealthCoordinator], BinarySensorEntity):
    """ON while the camera bridge is reachable and this camera's live session
    has video flowing. Attributes carry the bridge's per-session counters
    (reconnects, drops, NACK negotiated, audio fallback) and its process-level
    leak indicators, so none of it needs docker logs any more."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "bridge_stream"
    _attr_has_entity_name = True

    def __init__(self, coordinator: BridgeHealthCoordinator, device_id: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_bridge_stream"

    @property
    def device_info(self) -> DeviceInfo:
        # Identifiers only: attaches to the device the camera entity created.
        return DeviceInfo(identifiers={(DOMAIN, self._device_id)})

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None  # down bridge = OFF, not unavailable

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        if not data.get("_reachable"):
            return False
        sess = self.coordinator.session(self._device_id)
        return bool(sess and sess.get("healthy"))

    @property
    def extra_state_attributes(self):
        return self.coordinator.attributes(self._device_id)


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
