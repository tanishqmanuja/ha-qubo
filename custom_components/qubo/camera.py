"""Camera entity for Qubo cameras and video doorbells (HCP/HCO/HCD series)."""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from qubopy.devices import QuboCamera

from . import QuboCoordinator, QuboRuntime, qubo_device_info
from .const import DOMAIN, STREAM_QUALITY, UNIQUE_ID_PREFIX

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Qubo camera entities."""
    runtime: QuboRuntime = hass.data[DOMAIN][entry.entry_id]
    cameras = [d for d in runtime.devices.values() if isinstance(d, QuboCamera)]
    async_add_entities(QuboCameraEntity(runtime, cam) for cam in cameras)


class QuboCameraEntity(CoordinatorEntity[QuboCoordinator], Camera):
    """A Qubo camera whose live stream is relayed through the Qubo cloud.

    The cloud mints a single-consumer RTSPS URL per session, so a fresh URL is
    requested every time a viewer attaches and nothing is cached.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, runtime: QuboRuntime, device: QuboCamera) -> None:
        super().__init__(runtime.coordinator)
        # CoordinatorEntity.__init__ does not chain, but Camera.__init__ sets up
        # the state the webrtc provider bookkeeping depends on.
        Camera.__init__(self)
        self._runtime = runtime
        self._device = device
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{device.device_id}_camera"
        self._attr_device_info = qubo_device_info(device)

    async def async_stream_source(self) -> str | None:
        """Mint a fresh cloud stream URL for the new viewer.

        qubopy's QuboCamera.start_stream() falls back to the login source device
        id for userAppID, which the stream manager would reject now that this
        integration signs in under its own device.
        """
        try:
            url = await self._device.client.get_live_stream_url(
                unit_uuid=self._device.unit_id,
                device_uuid=self._device.device_id,
                device_type=self._device.info.device_type,
                quality=STREAM_QUALITY,
                user_app_id=self._runtime.stream_user_app_id,
            )
        except Exception as err:  # noqa: BLE001 - Home Assistant shows an idle frame
            _LOGGER.warning(
                "Qubo %s: could not start stream: %s", self._device.name, err
            )
            return None
        return url or None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Return the reported network and storage telemetry."""
        reported = self._device.reported_state
        return {
            "ip_address": self._device.lan_ip_address or None,
            "wifi_ssid": self._device.wifi_ssid or None,
            "wifi_signal": self._device.wifi_rssi,
            "night_mode": self._device.night_mode,
            "privacy_mode": self._device.is_privacy_mode_on,
            "sd_card_status": self._device.sd_card_status or None,
            "sd_free_mb": self._device.sd_storage_free_mb,
            "sd_total_mb": self._device.sd_storage_total_mb,
            "streaming": reported.get("streamControl_streamState"),
        }
