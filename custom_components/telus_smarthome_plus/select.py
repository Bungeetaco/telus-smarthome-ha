"""Select platform: TELUS camera auto-light resources (spotlight, night vision).

Each aws.r.autoLight resource (/spotlight, /ir_sensor) takes a mode. A set is a
send-commands WRITE to the resource href with operation {"op":"w","cn":{"modes":
"<mode>"}}. Mode strings are not advertised by the resource, so a sensible
off/on/auto set is offered; adjust if a device reports other modes.
"""
from __future__ import annotations

import json

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TelusEntity, get_resource, resources_of

# href -> (friendly name, icon, options)
AUTOLIGHT: dict[str, tuple[str, str, list[str]]] = {
    "/spotlight": ("Spotlight", "mdi:spotlight", ["off", "on", "auto"]),
    "/ir_sensor": ("Night vision", "mdi:weather-night", ["off", "on", "auto"]),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    entities: list[SelectEntity] = []
    for dev_id, dev in coordinator.data.items():
        hrefs = {r.get("href") for r in resources_of(dev)}
        for href, (name, icon, opts) in AUTOLIGHT.items():
            if href in hrefs:
                entities.append(
                    TelusAutoLightSelect(coordinator, dev_id, href, name, icon, opts)
                )
    async_add_entities(entities)


class TelusAutoLightSelect(TelusEntity, SelectEntity):
    """A camera aws.r.autoLight resource exposed as off/on/auto."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, device_id, href, name, icon, options) -> None:
        super().__init__(coordinator, device_id)
        self._href = href
        self._attr_name = name
        self._attr_icon = icon
        self._attr_options = options
        self._attr_unique_id = f"{device_id}_resource{href.replace('/', '_')}"

    @property
    def current_option(self) -> str | None:
        res = get_resource(self.device, self._href)
        mode = res.get("mode") if res else None
        return mode if mode in self._attr_options else None

    async def async_select_option(self, option: str) -> None:
        await self._send(self._href, json.dumps({"op": "w", "cn": {"modes": option}}))
