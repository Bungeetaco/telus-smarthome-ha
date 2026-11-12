"""The TELUS SmartHome+ integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelusSmartHomeApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_AUTH_DOMAIN,
    CONF_AUTO_SAVE,
    CONF_BRIDGE_URL,
    CONF_HOUSEHOLD_ID,
    CONF_KEEP_DAYS,
    CONF_PASSWORD,
    CONF_PROFILE_ID,
    CONF_REFRESH_TOKEN,
    CONF_SAVE_FOLDER,
    CONF_USERNAME,
    DEFAULT_AUTH_DOMAIN,
    DEFAULT_KEEP_DAYS,
    DOMAIN,
)
from .clips import SYNC_INTERVAL, ClipSaver, TelusClipView
from .bridge_health import BridgeHealthCoordinator
from .coordinator import TelusCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.SWITCH,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SELECT,
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

    # Persist any refreshed tokens back to the entry.
    _persist_tokens(hass, entry, api)

    bridge_url = entry.options.get(
        CONF_BRIDGE_URL, entry.data.get(CONF_BRIDGE_URL)
    )
    # Bridge health is a STATE in HA (binary_sensor/sensor per camera), not
    # something that needs docker logs. A bridge that is down must not block
    # setup, so this is a plain refresh, not first_refresh.
    bridge_health = None
    if bridge_url:
        bridge_health = BridgeHealthCoordinator(hass, bridge_url)
        await bridge_health.async_refresh()
    saver = ClipSaver(
        hass, api, coordinator, entry.options.get(CONF_SAVE_FOLDER) or None
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "bridge_url": bridge_url,
        "bridge_health": bridge_health,
        "clip_saver": saver,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _register_clip_support(hass)

    # Optional auto-save of new clips to /media/telus_clips.
    if entry.options.get(CONF_AUTO_SAVE, False):
        keep_days = int(entry.options.get(CONF_KEEP_DAYS, DEFAULT_KEEP_DAYS))

        async def _auto_sync(_now=None) -> None:
            try:
                await saver.async_sync(keep_days)
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Clip auto-save pass failed: %s", err)

        entry.async_on_unload(
            async_track_time_interval(hass, _auto_sync, SYNC_INTERVAL)
        )
        entry.async_create_background_task(
            hass, _auto_sync(), "telus_smarthome_plus_clip_autosave_first_pass"
        )
        _LOGGER.info(
            "TELUS clip auto-save is ON (keep %s days)", keep_days or "forever"
        )
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


def _register_clip_support(hass: HomeAssistant) -> None:
    """Register the MP4 download view and the download_clip service (once)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get("_clip_support"):
        return
    domain_data["_clip_support"] = True
    hass.http.register_view(TelusClipView(hass))

    async def _download_clip(call: ServiceCall) -> dict:
        savers = [
            v["clip_saver"]
            for v in hass.data.get(DOMAIN, {}).values()
            if isinstance(v, dict) and "clip_saver" in v
        ]
        if not savers:
            raise ValueError("TELUS SmartHome+ is not set up")
        saver = savers[0]
        device_id = call.data.get("device_id")
        if not device_id:
            cams = saver.cameras()
            if not cams:
                raise ValueError("no TELUS camera found")
            device_id = next(iter(cams))
        path = await saver.async_find_and_save(
            device_id, call.data.get("clip_id"), call.data.get("folder") or None
        )
        return {"path": path}

    import voluptuous as vol

    hass.services.async_register(
        DOMAIN,
        "download_clip",
        _download_clip,
        schema=vol.Schema(
            {
                vol.Optional("device_id"): str,
                vol.Optional("clip_id"): str,
                vol.Optional("folder"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _send_command(call: ServiceCall) -> dict:
        entries = [
            v
            for v in hass.data.get(DOMAIN, {}).values()
            if isinstance(v, dict) and "api" in v
        ]
        if not entries:
            raise ValueError("TELUS SmartHome+ is not set up")
        data = entries[0]
        api = data["api"]
        coordinator = data["coordinator"]
        device_id = call.data["device_id"]
        dev = coordinator.data.get(device_id, {})
        resp = await api.async_send_command(
            device_id,
            call.data["capability_identifier"],
            call.data["operation"],
            connector_id=call.data.get("connector_id") or dev.get("connectorId"),
            group_id=call.data.get("group_id") or dev.get("groupId"),
            provider=call.data.get("provider") or dev.get("provider"),
        )
        await coordinator.async_request_refresh()
        return {"response": resp}

    hass.services.async_register(
        DOMAIN,
        "send_command",
        _send_command,
        schema=vol.Schema(
            {
                vol.Required("device_id"): str,
                vol.Required("capability_identifier"): str,
                vol.Required("operation"): str,
                vol.Optional("connector_id"): str,
                vol.Optional("group_id"): str,
                vol.Optional("provider"): str,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


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
