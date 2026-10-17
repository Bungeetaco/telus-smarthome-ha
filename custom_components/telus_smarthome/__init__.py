"""The TELUS SmartHome+ integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelusSmartHomeApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_DOMAIN,
    CONF_BRIDGE_URL,
    CONF_HOUSEHOLD_ID,
    CONF_PASSWORD,
    CONF_PROFILE_ID,
    CONF_REFRESH_TOKEN,
    CONF_USERNAME,
    DEFAULT_AUTH_DOMAIN,
    DOMAIN,
)
from .coordinator import TelusCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.SWITCH,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up TELUS SmartHome+ from a config entry."""
    session = async_get_clientsession(hass)
    api = TelusSmartHomeApi(
        session,
        username=entry.data.get(CONF_USERNAME),
        password=entry.data.get(CONF_PASSWORD),
        auth_domain=entry.data.get(CONF_AUTH_DOMAIN, DEFAULT_AUTH_DOMAIN),
        access_token=entry.data.get(CONF_ACCESS_TOKEN),
        refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
        profile_id=entry.data.get(CONF_PROFILE_ID),
        household_id=entry.data.get(CONF_HOUSEHOLD_ID),
    )

    # Ensure a live token before the first poll (refresh, else full re-login).
    try:
        await api.refresh_access_token()
    except Exception as err:  # noqa: BLE001 - surfaced as reauth below
        _LOGGER.warning("Token refresh/login failed at setup: %s", err)

    coordinator = TelusCoordinator(hass, api)
    await coordinator.async_config_entry_first_refresh()

    # One-time diagnostic: dump the raw device-list JSON so the shape can be
    # confirmed. Safe to leave on; it just overwrites a file in /config.
    await _dump_devices(hass, api)
    # One-time: dump a camera's live-stream config (KVS channel + temp creds)
    # so the bridge can be finalized. Runs once (guarded by file existence).
    await _dump_livestream(hass, api, coordinator)

    # Persist any refreshed tokens back to the entry.
    _persist_tokens(hass, entry, api)

    bridge_url = entry.options.get(
        CONF_BRIDGE_URL, entry.data.get(CONF_BRIDGE_URL)
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "bridge_url": bridge_url,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _dump_devices(hass: HomeAssistant, api: TelusSmartHomeApi) -> None:
    """Write the raw device-list JSON to /config/telus_smarthome_dump.json."""
    import json

    raw = getattr(api, "last_devices_raw", None)
    if raw is None:
        return
    path = hass.config.path("telus_smarthome_dump.json")

    def _write() -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(raw, fh, indent=2, default=str)
        except OSError as err:  # noqa: BLE001
            _LOGGER.warning("Could not write device dump: %s", err)

    await hass.async_add_executor_job(_write)
    _LOGGER.warning(
        "TELUS device dump written to %s. Upload this file to finish camera setup.",
        path,
    )


async def _dump_livestream(hass: HomeAssistant, api, coordinator) -> None:
    """One-time: request a camera's live-stream config and dump it to /config.

    Guarded by file existence so it only opens one VIEWER session, not one per
    restart. Delete the file to re-run.
    """
    import json
    import os

    from .entity import device_type_of

    path = hass.config.path("telus_smarthome_livestream.json")
    if await hass.async_add_executor_job(os.path.exists, path):
        return

    camera_id = next(
        (
            dev_id
            for dev_id, dev in coordinator.data.items()
            if "camera" in device_type_of(dev) or "doorbell" in device_type_of(dev)
        ),
        None,
    )
    if not camera_id:
        return

    try:
        cfg = await api.async_get_live_config(camera_id)
    except Exception as err:  # noqa: BLE001
        cfg = {"_error": str(err)}
        _LOGGER.warning("Live-config dump failed for %s: %s", camera_id, err)

    def _write() -> None:
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump({"deviceId": camera_id, "liveConfig": cfg}, fh,
                          indent=2, default=str)
        except OSError as err:  # noqa: BLE001
            _LOGGER.warning("Could not write live-stream dump: %s", err)

    await hass.async_add_executor_job(_write)
    _LOGGER.warning(
        "TELUS live-stream config written to %s. Upload it to finish live video.",
        path,
    )


def _persist_tokens(
    hass: HomeAssistant, entry: ConfigEntry, api: TelusSmartHomeApi
) -> None:
    tokens = api.tokens
    new_data = {
        **entry.data,
        CONF_ACCESS_TOKEN: tokens["access_token"],
        CONF_REFRESH_TOKEN: tokens["refresh_token"],
        CONF_PROFILE_ID: api.profile_id,
        CONF_HOUSEHOLD_ID: api.household_id,
    }
    if new_data != dict(entry.data):
        hass.config_entries.async_update_entry(entry, data=new_data)
