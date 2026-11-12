"""Polls the camera bridge's /health so its state is visible in Home Assistant.

Why: the bridge leaked sessions for a day and once went dark for 27 h with
nothing visible in HA except Frigate recordings and docker logs. The bridge's
GET /health (gst_bridge) reports per-device session health plus process-level
leak indicators; this coordinator polls it and the binary_sensor / sensor
platforms expose it per camera.

The bridge being down is a STATE (binary sensor off, attribute `reason`), not an
update failure: entities stay available so automations can trigger on it.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import aiohttp
import async_timeout

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

BRIDGE_HEALTH_INTERVAL = timedelta(seconds=30)
BRIDGE_HEALTH_TIMEOUT = 5

# Session fields copied verbatim into entity attributes when present.
SESSION_ATTRS = (
    "healthy", "started", "secs_since_frame", "reconnect_attempts",
    "reconnects_total", "gave_up", "nack_negotiated", "audio_requested",
    "audio_fallback", "audio_active", "audio_source", "video_drops", "audio_drops",
    "pre_keyframe_dropped", "pre_keyframe_audio_dropped", "last_au_age_s",
)
PROCESS_ATTRS = (
    "fds", "threads", "ffmpeg_children", "session_objects", "wedged_sessions",
    "watchdog_age_s",
)


class BridgeHealthCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """GET {bridge_url}/health every 30 s; never raises for a down bridge."""

    def __init__(self, hass: HomeAssistant, bridge_url: str, bridge_token: str = "") -> None:
        super().__init__(
            hass, _LOGGER, name=f"{DOMAIN}_bridge_health",
            update_interval=BRIDGE_HEALTH_INTERVAL,
        )
        self.bridge_url = bridge_url.rstrip("/")
        # /health is unauthenticated on the bridge; the header is sent anyway so
        # a future bridge that closes it keeps working.
        self._headers = {"X-Bridge-Token": bridge_token} if bridge_token else None

    async def _async_update_data(self) -> dict[str, Any]:
        session = async_get_clientsession(self.hass)
        try:
            async with async_timeout.timeout(BRIDGE_HEALTH_TIMEOUT):
                async with session.get(f"{self.bridge_url}/health", headers=self._headers) as resp:
                    # 503 = the bridge says it is sick; the body is still JSON.
                    body = await resp.json(content_type=None)
                    body["_reachable"] = True
                    body["_http_status"] = resp.status
                    return body
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            if self.data is None or self.data.get("_reachable"):
                _LOGGER.warning("Camera bridge %s unreachable: %s", self.bridge_url, err)
            return {"_reachable": False, "_error": str(err), "sessions": {}}

    def session(self, device_id: str) -> dict[str, Any] | None:
        """The bridge's session record for this device, or None."""
        data = self.data or {}
        return (data.get("sessions") or {}).get(device_id)

    def attributes(self, device_id: str) -> dict[str, Any]:
        data = self.data or {}
        attrs: dict[str, Any] = {
            "bridge_url": self.bridge_url,
            "bridge_reachable": bool(data.get("_reachable")),
        }
        if not data.get("_reachable"):
            attrs["reason"] = data.get("_error") or "unreachable"
            return attrs
        attrs["bridge_version"] = data.get("version")
        attrs["bridge_degraded"] = data.get("degraded")
        if data.get("reasons"):
            attrs["reason"] = "; ".join(data["reasons"])
        sess = self.session(device_id)
        if sess is None:
            attrs["reason"] = attrs.get("reason") or "no session (camera not opened yet)"
        else:
            for k in SESSION_ATTRS:
                if k in sess:
                    attrs[k] = sess[k]
        proc = data.get("process") or {}
        for k in PROCESS_ATTRS:
            if k in proc:
                attrs[f"bridge_{k}"] = proc[k]
        return attrs
