"""Lock entity for Qubo smart door locks (HLM/HLW/HLR series).

This reports exactly what the device last told the cloud, and nothing else.

An earlier version set the state optimistically the moment an unlock command
was accepted, on the theory that the next reading would confirm it. In
practice the lock often reports nothing at all, so the entity sat claiming the
door was open while the last thing the device actually said was that it was
shut. Since the Qubo cloud has no remote locking, a wrong "unlocked" is the one
state the user cannot correct from Home Assistant, so nothing is invented here.

The lock does report reasonably promptly in practice (tens of seconds when the
doorbell gateway has recent contact with it), so after an unlock this polls
quickly for the state change rather than guessing at it.
"""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from qubopy.devices import QuboLock
from qubopy.exceptions import QuboCommandError

from . import QuboApi, QuboRuntime, qubo_device_info
from .const import DOMAIN, UNIQUE_ID_PREFIX
from .shadow import parse_locked

_LOGGER = logging.getLogger(__name__)

LOCK_CONTROL = "lockControl"
LOCK_STATE = "lockState"

# How often to poll normally, and the tighter loop after sending a command.
POLL_SECONDS = 60
POST_COMMAND_ATTEMPTS = 9
POST_COMMAND_INTERVAL = 20


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Qubo lock entities."""
    runtime: QuboRuntime = hass.data[DOMAIN][entry.entry_id]
    locks = [d for d in runtime.devices.values() if isinstance(d, QuboLock)]
    async_add_entities(QuboLockEntity(runtime, lock) for lock in locks)


class QuboLockEntity(LockEntity):
    """A Qubo smart door lock, actuated through its paired in-home gateway."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = 0
    _attr_should_poll = True
    _attr_available = False

    def __init__(self, runtime: QuboRuntime, device: QuboLock) -> None:
        self._runtime = runtime
        self._device = device
        self._api: QuboApi = runtime.api
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{device.device_id}_lock"
        self._attr_device_info = qubo_device_info(device)
        self._attr_is_locked: bool | None = None
        self._reading = None

    @property
    def available(self) -> bool:
        """Available once a state has been read and a gateway exists to relay."""
        return self._reading is not None and self._device.is_gateway_accessible

    @property
    def is_locked(self) -> bool | None:
        """Return the last state the device reported, or None if never heard from."""
        return self._attr_is_locked

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the gateway and how stale the displayed state is."""
        attributes: dict[str, object] = {"gateway_id": self._device.gateway_id}
        if self._reading is not None:
            attributes["device_reported_at"] = self._reading.timestamp
            attributes["reading_age_seconds"] = round(self._reading.age)
        return attributes

    async def async_update(self) -> None:
        """Read the lock's last reported state.

        A failed read is logged and ignored rather than raised: the coordinator
        handles re-authentication, and a blip here should not blank a door
        state that is still the best information available.
        """
        try:
            reading = await self._async_read()
        except Exception as err:  # noqa: BLE001 - keep the last known state
            _LOGGER.debug("Qubo %s: state read failed: %s", self._device.name, err)
            return
        self._async_apply(reading)

    async def _async_read(self):
        """Read lockControl.lockState through the v2 per-attribute shadow."""
        return await self._api.read_attribute(
            self._device.device_id, LOCK_CONTROL, LOCK_STATE
        )

    def _async_apply(self, reading) -> None:
        """Adopt a reading verbatim, ignoring only unreadable ones."""
        if reading is None:
            return
        self._reading = reading
        self._attr_is_locked = parse_locked(reading.value)

    async def async_unlock(self, code: str | None = None) -> None:
        """Send an unlock command via the paired gateway, then wait for the report."""
        _LOGGER.info(
            "Qubo unlocking %s via gateway %s",
            self._device.name,
            self._device.gateway_id,
        )
        try:
            # qubopy's QuboLock.unlock() already uses the correct PUT verb and
            # payload, so it is used as-is.
            await self._device.unlock()
        except QuboCommandError as err:
            raise HomeAssistantError(f"Qubo unlock failed: {err}") from err

        # The cloud accepted the command. Rather than assume the bolt moved,
        # watch for the lock to actually report, which it does once the
        # gateway has BLE contact with it.
        before = self._reading
        for _ in range(POST_COMMAND_ATTEMPTS):
            await asyncio.sleep(POST_COMMAND_INTERVAL)
            try:
                reading = await self._async_read()
            except Exception as err:  # noqa: BLE001 - try again on the next tick
                _LOGGER.debug(
                    "Qubo %s: post-unlock read failed: %s", self._device.name, err
                )
                continue
            if reading is None:
                continue
            if before is not None and reading.timestamp <= before.timestamp:
                continue  # nothing new from the device yet
            self._async_apply(reading)
            self.async_write_ha_state()
            return
        _LOGGER.info(
            "Qubo %s reported nothing after unlock; showing last known state",
            self._device.name,
        )

    async def async_lock(self, code: str | None = None) -> None:
        """Refuse remote locking, which the Qubo cloud does not support."""
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="lock_not_supported"
        )