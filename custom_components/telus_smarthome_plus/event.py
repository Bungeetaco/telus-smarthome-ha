"""Doorbell button-press event for TELUS cameras.

The /chime OCF resource (rt ``aws.r.kvs.video_event``, ``ve.vet`` == "chime") is
the camera's button-press channel: each press carries a new ``ve.vei`` event id.
We fire an HA event whenever a new vei appears, so automations can react to a
real ring -- distinct from /pirSensor (motion) and /aiSensor (AI classification),
all three of which share ``ve.vii`` (the video-interval id) so they correlate to
the same clip.

Note: /chime is polled via the coordinator (not pushed), so the ring lands within
one poll interval of the physical press -- the authoritative "they rang" signal,
not the fastest one (local Frigate motion is sub-second).
"""
from __future__ import annotations

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TelusEntity, get_resource, resources_of


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[EventEntity] = []
    for dev_id, dev in coordinator.data.items():
        if "/chime" in {r.get("href") for r in resources_of(dev)}:
            entities.append(TelusDoorbellPress(coordinator, dev_id))
    async_add_entities(entities)


class TelusDoorbellPress(TelusEntity, EventEntity):
    """Fires 'ring' when the /chime resource reports a new press event id."""

    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = ["ring"]
    _attr_translation_key = "doorbell_press"

    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_doorbell_press"
        self._last_vei: str | None = None

    def _chime_vei(self) -> str | None:
        res = get_resource(self.device, "/chime") or {}
        return (res.get("ve") or {}).get("vei")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # Baseline the current event id so a restored/last press on startup does
        # not fire a spurious ring.
        self._last_vei = self._chime_vei()

    @callback
    def _handle_coordinator_update(self) -> None:
        res = get_resource(self.device, "/chime") or {}
        vei = (res.get("ve") or {}).get("vei")
        if vei and vei != self._last_vei:
            # A new ve.vei is a fresh chime event; value:true is the active press.
            if res.get("value") is True:
                self._trigger_event(
                    "ring",
                    {
                        "event_id": vei,
                        "video_interval_id": (res.get("ve") or {}).get("vii"),
                    },
                )
            # Advance the baseline either way so we never fire late on a stale id.
            self._last_vei = vei
        super()._handle_coordinator_update()
