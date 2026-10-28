"""Switch platform (binary switch / smart plug)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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

    # AI recording/notification toggles (video-event-settings): one group per
    # detection type, each with a Detect and a Notify setting.
    for dev_id, groups in (coordinator.video_settings or {}).items():
        for group in groups:
            gtype = group.get("type")
            for setting in group.get("settings", []):
                stype = setting.get("type")
                if gtype and stype:
                    entities.append(
                        TelusVideoEventToggle(coordinator, dev_id, gtype, stype)
                    )

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


class TelusVideoEventToggle(TelusEntity, SwitchEntity):
    """One AI recording/notification toggle (e.g. 'Person Detect').

    State comes from coordinator.video_settings; a change does a read-modify-write
    of the whole settingsGroups array (the API replaces the lot on PUT).
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device_id: str, group_type: str, setting_type: str) -> None:
        super().__init__(coordinator, device_id)
        self._group_type = group_type        # "Person", "Vehicle", ...
        self._setting_type = setting_type     # "Detect" or "Notify"
        self._attr_unique_id = (
            f"{device_id}_video_event_{group_type}_{setting_type}".lower()
        )
        self._attr_name = f"{group_type} {setting_type}"
        self._attr_icon = "mdi:cctv" if setting_type == "Detect" else "mdi:bell"

    def _groups(self) -> list:
        return self.coordinator.video_settings.get(self._device_id, [])

    def _setting(self) -> dict | None:
        for g in self._groups():
            if g.get("type") == self._group_type:
                for s in g.get("settings", []):
                    if s.get("type") == self._setting_type:
                        return s
        return None

    @property
    def available(self) -> bool:
        return super().available and self._setting() is not None

    @property
    def is_on(self) -> bool | None:
        s = self._setting()
        return bool(s.get("value")) if s is not None else None

    async def _set(self, value: bool) -> None:
        import copy

        groups = copy.deepcopy(self._groups())
        for g in groups:
            if g.get("type") == self._group_type:
                for s in g.get("settings", []):
                    if s.get("type") == self._setting_type:
                        s["value"] = value
        await self.coordinator.api.async_set_video_event_settings(
            self._device_id, groups
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)
