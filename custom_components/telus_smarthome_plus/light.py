"""Light platform (dimmable / colour bulbs)."""
from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CAP_BINARY_SWITCH, CAP_BRIGHTNESS, DOMAIN
from .entity import TelusEntity, capability_ids, get_capability


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities = []
    for dev_id, dev in coordinator.data.items():
        caps = capability_ids(dev)
        if caps & {"brightness", "dimming", "colorhuesaturation", "colortemperature"}:
            entities.append(TelusLight(coordinator, dev_id))
    async_add_entities(entities)


class TelusLight(TelusEntity, LightEntity):
    def __init__(self, coordinator, device_id: str) -> None:
        super().__init__(coordinator, device_id)
        self._attr_unique_id = f"{device_id}_light"
        caps = capability_ids(self.device)
        modes: set[ColorMode] = set()
        if caps & {"colorhuesaturation"}:
            modes.add(ColorMode.HS)
        if caps & {"colortemperature"}:
            modes.add(ColorMode.COLOR_TEMP)
        if not modes and caps & {"brightness", "dimming"}:
            modes.add(ColorMode.BRIGHTNESS)
        if not modes:
            modes.add(ColorMode.ONOFF)
        self._attr_supported_color_modes = modes
        self._attr_color_mode = next(iter(modes))

    @property
    def name(self) -> None:
        return None

    @property
    def is_on(self) -> bool | None:
        cap = get_capability(self.device, "binarySwitch")
        if cap:
            val = cap.get("value") or cap.get("state")
            return str(val).lower() in ("on", "true", "1", "active")
        bri = self.brightness
        return bool(bri) if bri is not None else None

    @property
    def brightness(self) -> int | None:
        cap = get_capability(self.device, "brightness", "dimming")
        if not cap:
            return None
        val = cap.get("value") or cap.get("level")
        if val is None:
            return None
        # Platform value is assumed 0-100; HA wants 0-255.
        try:
            return round(float(val) / 100 * 255)
        except (TypeError, ValueError):
            return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        if ATTR_BRIGHTNESS in kwargs:
            pct = round(kwargs[ATTR_BRIGHTNESS] / 255 * 100)
            await self._send(CAP_BRIGHTNESS, f"setLevel:{pct}")
        else:
            await self._send(CAP_BINARY_SWITCH, "on")

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(CAP_BINARY_SWITCH, "off")
