"""Camera platform for TELUS SmartHome+.

Live view uses the KVS-WebRTC -> RTSP bridge (see gst_bridge/). The camera
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
    bridge_token = store.get("bridge_token") or ""

    entities = []
    for dev_id, dev in coordinator.data.items():
        if "camera" in device_type_of(dev) or "doorbell" in device_type_of(dev):
            entities.append(TelusCamera(coordinator, dev_id, bridge_url, bridge_token))
    _LOGGER.debug("Set up %d TELUS camera(s)", len(entities))
    async_add_entities(entities)


class TelusCamera(TelusEntity, Camera):
    """A TELUS / linked camera exposed via the KVS bridge."""

    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, coordinator, device_id: str, bridge_url: str | None,
                 bridge_token: str = "") -> None:
        TelusEntity.__init__(self, coordinator, device_id)
        Camera.__init__(self)
        self._bridge_url = bridge_url
        self._bridge_token = bridge_token
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
            _LOGGER.error("%s: could not get live config from TELUS: %s",
                          self._device_id, err)
            return None

        session = async_get_clientsession(self.hass)
        url = f"{self._bridge_url.rstrip('/')}/session/{self._device_id}/start"
        try:
            # The bridge itself waits up to 12 s for the video pad; 45 s covers a
            # slow KVS handshake plus the request round trip.
            async with async_timeout.timeout(45):
                async with session.post(
                    url,
                    json={"deviceId": self._device_id, "liveConfig": live_config},
                    headers={"X-Bridge-Token": self._bridge_token} if self._bridge_token else None,
                ) as resp:
                    if resp.status == 401:
                        _LOGGER.error(
                            "%s: bridge %s rejected the token (HTTP 401): set the same "
                            "value in the integration's 'bridge token' option and the "
                            "bridge's BRIDGE_TOKEN",
                            self._device_id, self._bridge_url,
                        )
                        return None
                    if resp.status != 200:
                        _LOGGER.error(
                            "%s: bridge %s refused the session: HTTP %s %s",
                            self._device_id, self._bridge_url, resp.status,
                            (await resp.text())[:200],
                        )
                        return None
                    data = await resp.json()
                    if data.get("started") is False:
                        # The bridge answered with the URL anyway so HA can keep
                        # retrying the RTSP source; nothing is published yet.
                        _LOGGER.warning(
                            "%s: bridge %s accepted the session but video has not "
                            "started yet (reused=%s); the stream may be dark for a "
                            "few seconds",
                            self._device_id, self._bridge_url, data.get("reused"),
                        )
                    self._rtsp_url = data.get("rtsp_url")
                    return self._rtsp_url
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.error("%s: bridge %s unreachable: %s",
                          self._device_id, self._bridge_url, err)
            return None

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Still image: the device's thumbnail URL when the API offers one, else a
        frame from Home Assistant's bundled go2rtc, which already relays this
        camera's stream. Returning None here made camera_proxy answer 500 on every
        dashboard load (card poster, more-info preview, notification images)."""
        dev = self.device
        thumb = (
            dev.get("thumbnailUrl")
            or dev.get("snapshotUrl")
            or (dev.get("camera") or {}).get("thumbnailUrl")
        )
        session = async_get_clientsession(self.hass)
        if thumb:
            try:
                async with async_timeout.timeout(15):
                    async with session.get(thumb) as resp:
                        if resp.status == 200:
                            return await resp.read()
            except (aiohttp.ClientError, TimeoutError):
                pass
        return await self._go2rtc_frame(session)

    async def _go2rtc_frame(self, session: aiohttp.ClientSession) -> bytes | None:
        """One JPEG frame of this entity's stream from HA's go2rtc (loopback, the
        default bind of the core go2rtc integration), or None if it cannot."""
        if not self.entity_id:
            return None
        url = f"http://127.0.0.1:1984/api/frame.jpeg?src={self.entity_id}"
        try:
            async with async_timeout.timeout(8):
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except (aiohttp.ClientError, TimeoutError):
            pass
        return None
