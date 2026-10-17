"""Switches for the Qubo settings that the app exposes as toggles.

Each of these is a single writable shadow attribute. The doorbell reports them
as the strings ``true``/``false`` except Privacy Mode, which the app models as
``enable``/``disable``, so the value pair is carried per switch rather than
assumed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from qubopy.devices import QuboCamera, QuboDevice, QuboLock

from . import QuboRuntime, qubo_device_info
from .const import DOMAIN, UNIQUE_ID_PREFIX


@dataclass(frozen=True)
class Toggle:
    """One writable shadow attribute exposed as a switch."""

    key: str
    service: str
    attribute: str
    on_value: str = "true"
    off_value: str = "false"


DOORBELL_TOGGLES = (
    Toggle("dnd", "doorbell", "dndEnabled"),
    Toggle("chime_bell", "doorbell", "dndConfigChimeBellEnabled"),
    Toggle("privacy_mode", "cameraFunction", "state", "enable", "disable"),
    Toggle("tamper_alerts", "tamperMonitoring", "enabled"),
    Toggle("timelapse_generator", "timelapseGenerator", "enabled"),
)

LOCK_TOGGLES = (Toggle("silent_mode", "silentMode", "enabled"),)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Qubo switches."""
    runtime: QuboRuntime = hass.data[DOMAIN][entry.entry_id]
    locks = [d for d in runtime.devices.values() if isinstance(d, QuboLock)]
    cameras = [d for d in runtime.devices.values() if isinstance(d, QuboCamera)]
    async_add_entities(
        [
            QuboAttributeSwitch(runtime, cam, toggle)
            for cam in cameras
            for toggle in DOORBELL_TOGGLES
        ]
        + [
            QuboAttributeSwitch(runtime, lock, toggle)
            for lock in locks
            for toggle in LOCK_TOGGLES
        ]
    )


class QuboAttributeSwitch(SwitchEntity):
    """A Qubo setting backed by one writable shadow attribute."""

    _attr_has_entity_name = True
    _attr_should_poll = True

    def __init__(
        self, runtime: QuboRuntime, device: QuboDevice, toggle: Toggle
    ) -> None:
        self._device = device
        self._toggle = toggle
        self._api = runtime.api
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{device.device_id}_{toggle.key}"
        self._attr_translation_key = toggle.key
        self._attr_device_info = qubo_device_info(device)
        self._is_on: bool | None = None

    async def async_update(self) -> None:
        """Read the attribute the device last reported."""
        reading = await self._api.read_attribute(
            self._device.device_id, self._toggle.service, self._toggle.attribute
        )
        if reading is None:
            self._is_on = None
            return
        self._is_on = reading.value == self._toggle.on_value

    @property
    def is_on(self) -> bool | None:
        """Return whether the device's last reported value is the on value."""
        return self._is_on

    async def _async_write(self, on: bool) -> None:
        """Send the new value and show it until the next poll confirms it."""
        value = self._toggle.on_value if on else self._toggle.off_value
        await self._api.write_attribute(
            self._device, self._toggle.service, self._toggle.attribute, value
        )
        self._is_on = on
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the setting on."""
        await self._async_write(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the setting off."""
        await self._async_write(False)