"""Config flow for TELUS SmartHome+ (headless email/password login)."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import TelusApiError, TelusAuthError, TelusSmartHomeApi
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

_LOGGER = logging.getLogger(__name__)


class TelusConfigFlow(ConfigFlow, domain=DOMAIN):
    """Email/password login, like a typical cloud integration."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return TelusOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            session = async_get_clientsession(self.hass)
            api = TelusSmartHomeApi(
                session,
                username=user_input[CONF_USERNAME],
                password=user_input[CONF_PASSWORD],
                auth_domain=user_input.get(CONF_AUTH_DOMAIN, DEFAULT_AUTH_DOMAIN),
            )
            try:
                await api.async_login()
                await api.async_bootstrap()
            except TelusAuthError as err:
                _LOGGER.warning("Login failed: %s", err)
                errors["base"] = "invalid_auth"
            except TelusApiError as err:
                _LOGGER.warning("API error during login: %s", err)
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(api.household_id or user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                tokens = api.tokens
                return self.async_create_entry(
                    title=f"TELUS SmartHome+ ({user_input[CONF_USERNAME]})",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_AUTH_DOMAIN: user_input.get(
                            CONF_AUTH_DOMAIN, DEFAULT_AUTH_DOMAIN
                        ),
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                        CONF_PROFILE_ID: api.profile_id,
                        CONF_HOUSEHOLD_ID: api.household_id,
                        CONF_BRIDGE_URL: user_input.get(CONF_BRIDGE_URL),
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_AUTH_DOMAIN, default=DEFAULT_AUTH_DOMAIN): str,
                    vol.Optional(CONF_BRIDGE_URL): str,
                }
            ),
            errors=errors,
        )


class TelusOptionsFlow(OptionsFlow):
    """Let the user set/change the KVS bridge URL after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(
            CONF_BRIDGE_URL, self.config_entry.data.get(CONF_BRIDGE_URL, "")
        )
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Optional(CONF_BRIDGE_URL, default=current or ""): str}
            ),
        )
