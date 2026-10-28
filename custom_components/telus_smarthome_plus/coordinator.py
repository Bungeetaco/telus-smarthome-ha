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

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        # Keep live telemetry fresh. GET /devices returns backend-CACHED resource
        # values; only POST refreshDeviceStatus makes the backend pull current
        # wifi/temp/connectivity (and a cleared motion value) from the device. It
        # pokes the physical device, so run it only every REFRESH_STATUS_EVERY
        # polls, and only for camera devices (those exposing an /aiSensor or
        # /pirSensor resource), never every battery device.
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
        _LOGGER.debug("Fetched %d devices", len(indexed))
        return indexed
