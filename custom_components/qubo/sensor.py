"""Battery, doorbell event and diagnostic sensors for Qubo devices."""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from qubopy.devices import QuboCamera, QuboLock

from . import QuboApi, QuboRuntime, qubo_device_info
from .const import DOMAIN, UNIQUE_ID_PREFIX
from .shadow import parse_percent

_LOGGER = logging.getLogger(__name__)

BATTERY_SENSOR = "battery"
BATTERY_STATUS = "batteryStatus"
LEVEL = "level"
LAST_EVENT_SENSOR = "last_doorbell_event"
WIFI_SSID_SENSOR = "wifi_ssid"
TIME_ZONE_SENSOR = "time_zone"

# Read-only settings the app surfaces on its device-info screen. They are not
# actionable from Home Assistant, so they exist to answer "is the doorbell
# configured the way I think it is" without opening the app.
CAMERA_DIAGNOSTICS = (
    (WIFI_SSID_SENSOR, "wifiSettings", "SSIDName"),
    (TIME_ZONE_SENSOR, "timeZoneSetting", "offSet"),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up Qubo sensors."""
    runtime: QuboRuntime = hass.data[DOMAIN][entry.entry_id]
    locks = [d for d in runtime.devices.values() if isinstance(d, QuboLock)]
    cameras = [d for d in runtime.devices.values() if isinstance(d, QuboCamera)]
    async_add_entities(
        [QuboLockBatterySensor(runtime, lock) for lock in locks]
        + [QuboDoorbellEventSensor(runtime, cam) for cam in cameras]
        + [
            QuboDiagnosticSensor(runtime, cam, key, service, attribute)
            for cam in cameras
            for key, service, attribute in CAMERA_DIAGNOSTICS
        ]
    )


class QuboLockBatterySensor(SensorEntity):
    """Battery level reported by a Qubo smart door lock."""

    _attr_has_entity_name = True
    _attr_translation_key = BATTERY_SENSOR
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = True

    def __init__(self, runtime: QuboRuntime, device: QuboLock) -> None:
        self._runtime = runtime
        self._device = device
        self._api: QuboApi = runtime.api
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{device.device_id}_{BATTERY_SENSOR}"
        self._attr_device_info = qubo_device_info(device)
        self._attr_native_value: int | None = None
        self._reading = None

    async def async_update(self) -> None:
        """Poll the lock's battery level."""
        reading = await self._api.read_attribute(
            self._device.device_id, BATTERY_STATUS, LEVEL
        )
        if reading is None:
            self._attr_native_value = None
            return
        self._reading = reading
        self._attr_native_value = parse_percent(reading.value)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose when the lock last reported its battery level."""
        if self._reading is None:
            return {}
        return {
            "device_reported_at": self._reading.timestamp,
            "reading_age_seconds": round(self._reading.age),
        }


class QuboDoorbellEventSensor(SensorEntity):
    """When a Qubo video doorbell last rang.

    Qubo exposes no motion or person state for this hardware, so this reports
    doorbell activity only. The cloud keeps a rolling window of events, so a new
    transaction appearing here means someone rang.
    """

    _attr_has_entity_name = True
    _attr_translation_key = LAST_EVENT_SENSOR
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_should_poll = True

    def __init__(self, runtime: QuboRuntime, device: QuboCamera) -> None:
        self._runtime = runtime
        self._device = device
        self._api: QuboApi = runtime.api
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{device.device_id}_{LAST_EVENT_SENSOR}"
        self._attr_device_info = qubo_device_info(device)
        self._attr_native_value: datetime | None = None
        self._events: list = []

    async def async_update(self) -> None:
        """Poll the cloud video service for doorbell events."""
        self._events = await self._api.fetch_doorbell_events(
            self._device.unit_id, self._device.device_id
        )
        self._attr_native_value = (
            datetime.fromtimestamp(self._events[0].created / 1000, tz=UTC)
            if self._events
            else None
        )

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Expose how far back the retained event window reaches."""
        if not self._events:
            return {"event_count": 0}
        newest = self._events[0]
        return {
            "event_count": len(self._events),
            "last_transaction_uuid": newest.transaction_uuid,
            "last_call_status": newest.call_status or None,
            "last_event_recorded": newest.is_recorded,
            "last_event_has_snapshot": newest.has_snapshot,
            "oldest_event_age_seconds": round(
                max(0.0, (datetime.now(tz=UTC).timestamp() * 1000 - self._events[-1].created) / 1000)
            ),
        }


class QuboDiagnosticSensor(SensorEntity):
    """A read-only shadow attribute reported by the device.

    These are plain strings the device publishes, so there is no device class
    and no unit - HA would otherwise reject the value.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = True

    def __init__(
        self,
        runtime: QuboRuntime,
        device: QuboCamera,
        key: str,
        service: str,
        attribute: str,
    ) -> None:
        self._device = device
        self._api: QuboApi = runtime.api
        self._service = service
        self._attribute = attribute
        self._attr_unique_id = f"{UNIQUE_ID_PREFIX}_{device.device_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = qubo_device_info(device)
        self._attr_native_value: str | None = None

    async def async_update(self) -> None:
        """Read the attribute the device last reported."""
        reading = await self._api.read_attribute(
            self._device.device_id, self._service, self._attribute
        )
        self._attr_native_value = reading.value if reading else None