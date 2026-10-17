"""DataUpdateCoordinator for TELUS SmartHome+."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import TelusApiError, TelusAuthError, TelusSmartHomeApi
from .const import DOMAIN, UPDATE_INTERVAL

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

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
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
