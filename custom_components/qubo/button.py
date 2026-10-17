"""Restart button for the Qubo video doorbell."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from qubopy.devices import QuboCamera

from . import QuboRuntime, qubo_device_info
from .const import DOMAIN, UNIQUE_ID_PREFIX

REBOOT_SERVICE = "deviceReboot"
REBOOT_COMMAND = "reboot"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Qubo buttons."""
    runtime: QuboRuntime = hass.data[DOMAIN][entry.entry_id]
    cameras = [d for d in runtime.devices.values() if isinstance(d, QuboCamera)]
    async_add_entities(QuboRestartButton(runtime, cam) for cam in cameras)


class QuboRestartButton(ButtonEntity):
    """Restart a Qubo video doorbell.

    The app sends this as a command rather than an attribute write, and the
    cloud only acks the request - the doorbell drops off the network and comes
    back on its own after a minute or two.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "restart"

    def __init__(self, runtime: QuboRuntime, device: QuboCamera) -> None:
        self._device = device
        self._api = runtime.api
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{device.device_id}_restart"
        self._attr_device_info = qubo_device_info(device)

    async def async_press(self) -> None:
        """Ask the doorbell to reboot."""
        await self._api.send_command(self._device, REBOOT_SERVICE, REBOOT_COMMAND)