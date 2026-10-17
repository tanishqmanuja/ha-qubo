"""Qubo integration for Home Assistant.

Built on qubopy for everything it does correctly: authentication and token
refresh, device discovery, typed device objects and shadow parsing. The few
endpoints it gets wrong live in :mod:`.api`.
"""

from __future__ import annotations

import logging
import secrets
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from qubopy import QuboAuth, QuboClient
from qubopy.devices import QuboDevice
from qubopy.exceptions import QuboAuthError, QuboConnectionError

from .api import QuboApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_DEVICE_NAME,
    CONF_LOGIN_DEVICE_ID,
    CONF_MOBILE_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_UUID,
    DEFAULT_DEVICE_NAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LOCK,
    Platform.SENSOR,
    Platform.CAMERA,
    Platform.SWITCH,
    Platform.BUTTON,
]

# Mobile entries are not controllable hardware; they exist only to supply the
# userAppID the cloud stream manager requires when minting a stream URL.
MOBILE_DEVICE_TYPE = "mobile"
MOBILE_NAME_PREFIX = "MobileApp_"


def qubo_device_info(device: QuboDevice) -> DeviceInfo:
    """Build Home Assistant device registry info for a Qubo device."""
    return DeviceInfo(
        identifiers={(DOMAIN, device.device_id)},
        name=device.name,
        manufacturer="Qubo (Hero Electronix)",
        model=device.model or None,
        sw_version=device.info.device_type or None,
    )


async def _async_login(auth: QuboAuth, entry: ConfigEntry) -> None:
    """Authenticate against the Qubo cloud using the configured login label."""
    await auth.login_with_email(
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
        device_attribute=entry.options.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
    )


def _persist_tokens(hass: HomeAssistant, entry: ConfigEntry, auth: QuboAuth) -> None:
    """Write rotated tokens back to the config entry."""
    tokens = auth.tokens
    if tokens is None:
        return
    data = {
        **entry.data,
        CONF_ACCESS_TOKEN: tokens.access_token,
        CONF_REFRESH_TOKEN: tokens.refresh_token,
        CONF_USER_UUID: tokens.user_uuid,
    }
    if data != entry.data:
        hass.config_entries.async_update_entry(entry, data=data)


def _mobile_device_id(devices: list[QuboDevice], stored: str | None) -> str | None:
    """Pick the source device id to use as userAppID when minting stream URLs.

    The stream manager only accepts a source device id already registered
    against the account, so reuse the stored one when we have it and otherwise
    read it off a paired mobile app device name.
    """
    if stored:
        return stored
    for device in devices:
        name = device.name or ""
        if name.startswith(MOBILE_NAME_PREFIX):
            return name[len(MOBILE_NAME_PREFIX) :]
    return None


class QuboCoordinator(DataUpdateCoordinator[dict[str, QuboDevice]]):
    """Poll the v1 device shadow for the camera's reported attributes."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: QuboClient,
        devices: dict[str, QuboDevice],
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self._devices = devices

    @property
    def devices(self) -> dict[str, QuboDevice]:
        """Return the discovered devices keyed by device uuid."""
        return self._devices

    async def _async_update_data(self) -> dict[str, QuboDevice]:
        try:
            return await self._async_poll()
        except QuboAuthError as err:
            _LOGGER.warning("Qubo session rejected, re-authenticating: %s", err)
            try:
                await _async_login(self.client.auth, self.config_entry)
            except (QuboAuthError, QuboConnectionError) as login_err:
                raise UpdateFailed(f"Qubo login failed: {login_err}") from login_err
            return await self._async_poll()

    async def _async_poll(self) -> dict[str, QuboDevice]:
        for device in self._devices.values():
            try:
                await device.refresh()
            except QuboAuthError:
                raise
            except Exception as err:  # noqa: BLE001 - one dead device must not stall the rest
                _LOGGER.debug("Qubo %s refresh failed: %s", device.name, err)
        _persist_tokens(self.hass, self.config_entry, self.client.auth)
        return self._devices


class QuboRuntime:
    """Everything a platform needs, resolved once during setup."""

    def __init__(
        self,
        auth: QuboAuth,
        client: QuboClient,
        coordinator: QuboCoordinator,
        stream_user_app_id: str | None,
        configured_options: dict,
    ) -> None:
        self.auth = auth
        self.client = client
        self.coordinator = coordinator
        self.api = QuboApi(client)
        self.stream_user_app_id = stream_user_app_id
        self.configured_options = dict(configured_options)

    @property
    def devices(self) -> dict[str, QuboDevice]:
        """Return the discovered devices keyed by device uuid."""
        return self.coordinator.devices

    async def async_close(self) -> None:
        """Release the client."""
        await self.client.close()


async def _async_build_runtime(
    hass: HomeAssistant, entry: ConfigEntry
) -> QuboRuntime:
    """Create an authenticated client and discover the account's devices."""
    session = async_get_clientsession(hass)

    # The login source device id and the stream userAppID are deliberately kept
    # apart. Logging in under a paired phone's id would make this integration
    # share that app session, so tapping "Logout" on it in the Qubo app would
    # break Home Assistant. Minting streams, on the other hand, only accepts a
    # userAppID the cloud already knows about.
    login_id = entry.data.get(CONF_LOGIN_DEVICE_ID)
    if not login_id:
        login_id = secrets.token_hex(8)
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_LOGIN_DEVICE_ID: login_id}
        )

    auth = QuboAuth(device_id=login_id, session=session)
    if entry.data.get(CONF_ACCESS_TOKEN):
        auth.set_tokens(
            access_token=entry.data[CONF_ACCESS_TOKEN],
            refresh_token=entry.data.get(CONF_REFRESH_TOKEN),
            user_uuid=entry.data.get(CONF_USER_UUID, ""),
        )
    if not auth.is_authenticated:
        await _async_login(auth, entry)
        _persist_tokens(hass, entry, auth)

    client = QuboClient(auth, session=session)
    discovered = await client.get_all_devices()
    hardware = {
        d.device_id: d for d in discovered if d.info.device_type != MOBILE_DEVICE_TYPE
    }

    stream_id = _mobile_device_id(discovered, entry.data.get(CONF_MOBILE_DEVICE_ID))
    if stream_id and entry.data.get(CONF_MOBILE_DEVICE_ID) != stream_id:
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_MOBILE_DEVICE_ID: stream_id}
        )

    coordinator = QuboCoordinator(hass, entry, client, hardware)
    return QuboRuntime(auth, client, coordinator, stream_id, entry.options)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Qubo from a config entry."""
    try:
        runtime = await _async_build_runtime(hass, entry)
    except (QuboAuthError, QuboConnectionError) as err:
        _LOGGER.error("Could not connect to the Qubo cloud: %s", err)
        raise ConfigEntryNotReady from err

    try:
        await runtime.coordinator.async_config_entry_first_refresh()
    except UpdateFailed:
        # The lock and battery entities read the v2 shadow themselves, so carry
        # on without the camera attribute poll rather than failing the entry.
        _LOGGER.warning("Qubo camera attribute poll failed; continuing")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the user changes an option.

    This deliberately ignores changes to the entry's *data*. Token rotation
    writes fresh credentials back to entry.data roughly once an hour, and
    update listeners fire for that too, so reloading unconditionally bounced
    every platform and made the entities briefly unavailable.
    """
    runtime: QuboRuntime = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if runtime is None or dict(entry.options) == runtime.configured_options:
        return
    runtime.configured_options = dict(entry.options)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a Qubo config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime: QuboRuntime = hass.data[DOMAIN].pop(entry.entry_id)
        await runtime.async_close()
    return unloaded