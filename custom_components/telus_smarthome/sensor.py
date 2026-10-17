"""Sensors for TELUS cameras (Wi-Fi signal, temperature, AI detection).

Read from the OCF resource model: /maintenance/info carries rssi, ssid,
internalTemperature, ip; /aiSensor carries the latest AI detection (the object
class the camera recognised: Person/Vehicle/Animal/Package).
"""
from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TelusEntity, get_resource

# The camera labels detections with these classes. "Body" is a person; the rest
# pass through title-cased. Anything unseen is shown raw.
AI_EVENT_LABELS = {
    "body": "Person",
    "person": "Person",
    "vehicle": "Vehicle",
    "animal": "Animal",
    "package": "Package",
    "face": "Face",
    "motion": "Motion",
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SensorEntity] = []
    for dev_id, dev in coordinator.data.items():
        if get_resource(dev, "/maintenance/info"):
            entities.append(TelusRssi(coordinator, dev_id))
            entities.append(TelusTemperature(coordinator, dev_id))
        if get_resource(dev, "/aiSensor"):
            entities.append(TelusAiSensor(coordinator, dev_id))
    async_add_entities(entities)


class TelusAiSensor(TelusEntity, SensorEntity):
    """Latest AI detection: what the camera's on-board AI last recognised.

    State is the detected object class (Person / Vehicle / Animal / Package /
    Motion). Attributes carry whether it's detecting right now, the confidence,
    when it happened, and the bounding box, so automations can react to e.g.
    "state == Person".
    """

    _attr_translation_key = "ai_sensor"
    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_ai_sensor"

    def _ai(self) -> dict:
        return get_resource(self.device, "/aiSensor") or {}

    @property
    def native_value(self):
        res = self._ai()
        name = res.get("eventName") or (res.get("detectedObject") or {}).get(
            "module_name"
        )
        if not name:
            return None
        return AI_EVENT_LABELS.get(str(name).lower(), str(name).title())

    @property
    def extra_state_attributes(self):
        res = self._ai()
        obj = res.get("detectedObject") or {}
        attrs: dict = {
            "detecting": bool(res.get("value")),
            "raw_event": res.get("eventName"),
            "module": obj.get("module_name"),
            "confidence": obj.get("confidence"),
            "event_id": (res.get("ve") or {}).get("vei"),
        }
        ts = res.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            # timestamp is epoch milliseconds
            attrs["last_detected"] = datetime.fromtimestamp(
                ts / 1000, tz=timezone.utc
            ).isoformat()
        box = obj.get("boundingBox") or {}
        if any(box.values()):
            attrs["bounding_box"] = box
            attrs["canvas_size"] = obj.get("canvasSize")
        return {k: v for k, v in attrs.items() if v is not None}


class TelusRssi(TelusEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "wifi_signal"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_rssi"

    @property
    def native_value(self):
        res = get_resource(self.device, "/maintenance/info") or {}
        return res.get("rssi")

    @property
    def extra_state_attributes(self):
        res = get_resource(self.device, "/maintenance/info") or {}
        return {
            k: res.get(k)
            for k in ("ssid", "ip", "mac", "band", "channel", "tx_rate")
            if res.get(k) is not None
        }


class TelusTemperature(TelusEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "device_temperature"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_temperature"

    @property
    def native_value(self):
        res = get_resource(self.device, "/maintenance/info") or {}
        temp = res.get("internalTemperature")
        if isinstance(temp, list) and temp:
            return temp[0]
        return temp
