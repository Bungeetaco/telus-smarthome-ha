"""Camera platform for TELUS SmartHome+.

Live view uses the KVS-WebRTC -> RTSP bridge (see kvs_bridge/). The camera
entity asks the bridge to start a viewer session for this device and hands HA
the returned RTSP URL; HA's `stream` component takes it from there (and the
HomeKit Bridge can then expose it to Apple Home).

If no bridge is configured, the entity still appears and serves the latest clip
thumbnail as a still image, but live streaming is disabled.
"""
from __future__ import annotations

import logging
from typing import Any

import aiohttp
import async_timeout

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import TelusEntity, device_type_of, is_online

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    store = hass.data[DOMAIN][entry.entry_id]
    coordinator = store["coordinator"]
    bridge_url = store.get("bridge_url")

    entities = []
    for dev_id, dev in coordinator.data.items():
        if "camera" in device_type_of(dev) or "doorbell" in device_type_of(dev):
            entities.append(TelusCamera(coordinator, dev_id, bridge_url))
    _LOGGER.debug("Set up %d TELUS camera(s)", len(entities))
    async_add_entities(entities)


class TelusCamera(TelusEntity, Camera):
    """A TELUS / linked camera exposed via the KVS bridge."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator, device_id: str, bridge_url: str | None) -> None:
        TelusEntity.__init__(self, coordinator, device_id)
        Camera.__init__(self)
        self._bridge_url = bridge_url
        self._attr_unique_id = f"{device_id}_camera"
        self._rtsp_url: str | None = None

    @property
    def name(self) -> str | None:
        return None  # use device name (has_entity_name)

    async def stream_source(self) -> str | None:
        """Fetch a fresh live-stream config from TELUS and hand it to the bridge.

        The bridge does the AWS KVS WebRTC viewer handshake and returns an RTSP
        URL that HA's stream component plays. The integration owns auth, so the
        bridge needs no TELUS credentials — just this per-session config (whose
        AWS creds expire in minutes).
        """
        if not self._bridge_url:
            _LOGGER.debug("No bridge configured; live stream unavailable")
            return None

        api = self.coordinator.api
        try:
            live_config = await api.async_get_live_config(self._device_id)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Could not get live config: %s", err)
            return None

        session = async_get_clientsession(self.hass)
        url = f"{self._bridge_url.rstrip('/')}/session/{self._device_id}/start"
        try:
            async with async_timeout.timeout(45):
                async with session.post(
                    url,
                    json={"deviceId": self._device_id, "liveConfig": live_config},
                ) as resp:
                    if resp.status != 200:
                        _LOGGER.error(
                            "Bridge start failed %s: %s",
                            resp.status,
                            await resp.text(),
                        )
                        return None
                    data = await resp.json()
                    self._rtsp_url = data.get("rtsp_url")
                    return self._rtsp_url
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("Bridge unreachable: %s", err)
            return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Still image: fall back to the device's thumbnail URL if present."""
        dev = self.device
        thumb = (
            dev.get("thumbnailUrl")
            or dev.get("snapshotUrl")
            or (dev.get("camera") or {}).get("thumbnailUrl")
        )
        if not thumb:
            return None
        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(15):
                async with session.get(thumb) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            pass
        return None
