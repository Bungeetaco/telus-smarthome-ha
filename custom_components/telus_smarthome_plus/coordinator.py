"""DataUpdateCoordinator for TELUS SmartHome+."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TelusApiError, TelusAuthError, TelusSmartHomeApi
from .const import DOMAIN, REFRESH_STATUS_EVERY, UPDATE_INTERVAL

_LOGGER = logging.getLogger(__name__)


class TelusCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the household device list and indexes it by device id."""

    def __init__(self, hass: HomeAssistant, api: TelusSmartHomeApi) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.api = api
        self._poll_count = 0
        self.video_settings: dict[str, list[Any]] = {}

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        # Ask the backend to refresh camera telemetry. GET /devices returns
        # backend-CACHED resource values; POST refreshDeviceStatus requests a
        # fresh pull. It wakes/pokes the physical device, so run it only every
        # REFRESH_STATUS_EVERY polls and only for camera devices (those exposing
        # an /aiSensor or /pirSensor resource), never every battery device.
        # NOTE: a battery camera (like the doorbell) only refreshes wifi/temp on
        # its next wake, so this will not always un-freeze them immediately; the
        # event resources (/aiSensor, /pirSensor) stay fresh regardless.
        if self.data and self._poll_count % REFRESH_STATUS_EVERY == 0:
            # Deferred import: entity.py imports this module, so importing it at
            # module top would be a circular import.
            from .entity import resources_of

            for dev_id, dev in self.data.items():
                hrefs = {r.get("href") for r in resources_of(dev)}
                if hrefs & {"/aiSensor", "/pirSensor"}:
                    try:
                        await self.api.async_refresh_status(dev_id)
                    except (TelusApiError, TelusAuthError) as err:
                        _LOGGER.debug(
                            "refreshDeviceStatus(%s) failed (non-fatal): %s",
                            dev_id, err,
                        )
        self._poll_count += 1

        try:
            devices = await self.api.async_get_devices()
        except TelusAuthError as err:
            # Surface as reauth to HA.
            from homeassistant.exceptions import ConfigEntryAuthFailed

            raise ConfigEntryAuthFailed(str(err)) from err
        except TelusApiError as err:
            raise UpdateFailed(str(err)) from err

        indexed: dict[str, dict[str, Any]] = {}
        for dev in devices:
            dev_id = dev.get("deviceId") or dev.get("id") or dev.get("assetId")
            if dev_id:
                indexed[str(dev_id)] = dev

        # Per-type AI recording/notification toggles (video-event-settings) for
        # camera devices, so the switch entities have state. Read-only GET, so
        # fetching it every poll is fine. Keep the last value on a transient error.
        from .entity import resources_of

        video_settings: dict[str, list[Any]] = {}
        for dev_id, dev in indexed.items():
            if "/aiSensor" in {r.get("href") for r in resources_of(dev)}:
                try:
                    video_settings[dev_id] = await self.api.async_get_video_event_settings(
                        dev_id
                    )
                except (TelusApiError, TelusAuthError) as err:
                    _LOGGER.debug("video-event-settings(%s) failed: %s", dev_id, err)
                    video_settings[dev_id] = self.video_settings.get(dev_id, [])
        self.video_settings = video_settings

        _LOGGER.debug("Fetched %d devices", len(indexed))
        return indexed
