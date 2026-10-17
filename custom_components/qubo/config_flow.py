"""Config flow for the Qubo integration."""

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
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from qubopy import QuboAuth, QuboClient
from qubopy.exceptions import QuboAuthError, QuboCommandError, QuboConnectionError

from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_NAME,
    CONF_REFRESH_TOKEN,
    CONF_USER_UUID,
    DEFAULT_DEVICE_NAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Optional(CONF_DEVICE_NAME, default=DEFAULT_DEVICE_NAME): str,
    }
)


async def _async_probe(hass: Any, user_input: dict[str, Any]) -> dict[str, Any]:
    """Log in, list the account's hardware, and return entry data."""
    auth = QuboAuth(session=async_get_clientsession(hass))
    await auth.login_with_email(
        user_input[CONF_EMAIL],
        user_input[CONF_PASSWORD],
        device_attribute=user_input.get(CONF_DEVICE_NAME) or DEFAULT_DEVICE_NAME,
    )
    client = QuboClient(auth)
    try:
        devices = await client.get_all_devices()
    finally:
        await client.close()

    hardware = [d for d in devices if d.info.device_type != "mobile"]
    if not hardware:
        raise QuboCommandError("No Qubo hardware is paired with this account yet")

    tokens = auth.tokens
    return {
        CONF_EMAIL: user_input[CONF_EMAIL],
        CONF_PASSWORD: user_input[CONF_PASSWORD],
        CONF_ACCESS_TOKEN: tokens.access_token if tokens else None,
        CONF_REFRESH_TOKEN: tokens.refresh_token if tokens else None,
        CONF_USER_UUID: tokens.user_uuid if tokens else "",
    }


class QuboConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Qubo cloud login."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect Qubo account credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                data = await _async_probe(self.hass, user_input)
            except QuboAuthError:
                errors["base"] = "invalid_auth"
            except QuboConnectionError:
                errors["base"] = "cannot_connect"
            except QuboCommandError as err:
                _LOGGER.warning("Qubo account returned no hardware: %s", err)
                errors["base"] = "no_devices"
            else:
                await self.async_set_unique_id(data[CONF_USER_UUID])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL], data=data
                )

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> QuboOptionsFlow:
        """Return the options flow handler."""
        return QuboOptionsFlow()


class QuboOptionsFlow(OptionsFlow):
    """Change the label this integration identifies itself with."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the Qubo login label."""
        current = self.config_entry.options.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME)
        if user_input is not None:
            device_name = user_input[CONF_DEVICE_NAME].strip() or DEFAULT_DEVICE_NAME
            if device_name != current:
                # The label is stored server-side by the cloud, so the stored
                # tokens must be dropped to force a fresh login under the new
                # name. Reloading the entry is what triggers that login.
                data = {
                    k: v
                    for k, v in self.config_entry.data.items()
                    if k not in (CONF_ACCESS_TOKEN, CONF_REFRESH_TOKEN)
                }
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=data
                )
            return self.async_create_entry(
                title="", data={CONF_DEVICE_NAME: device_name}
            )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {vol.Required(CONF_DEVICE_NAME, default=current): str}
            ),
        )