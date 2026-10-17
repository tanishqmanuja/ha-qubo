"""Shadow reads that carry the device's own timestamp, and write paths we still
format ourselves.

The cloud's v1 shadow that qubopy parses by default carries no timestamps, so a
stale reading is indistinguishable from a fresh one - which matters when deciding
whether a switch write actually reached the device. The only way to see both the
value and the moment the device last reported it is the v2 per-attribute shadow
(``/api/v2/.../devices/{id}/deviceshadow?serviceName=&attributeName=``), so reads
go through :meth:`QuboClient.get_device_shadow_v2`.

The write paths stay here for one reason only. qubopy 0.1.3 issued them with POST,
which the cloud rejects, and parsed every 2xx body as JSON although both endpoints
answer with the literal text ``SUCCESS``; 0.1.4 fixed both. What it does not do is
validate a device's binding before addressing it, so :meth:`QuboApi._device_path`
still formats the template here where a missing unit or entity uuid fails loudly
with a :class:`QuboCommandError` instead of guessing a scope.
"""

from __future__ import annotations

import logging
import time
from typing import Any, NamedTuple

from qubopy import QuboClient
from qubopy.const import PATH_SEND_COMMAND, PATH_UPDATE_SHADOW_ATTRIBUTE
from qubopy.devices import QuboDevice
from qubopy.exceptions import QuboCommandError

from .shadow import DoorbellEvent, parse_vms_events

_LOGGER = logging.getLogger(__name__)


class AttributeReading(NamedTuple):
    """One attribute value plus the moment the device last reported it."""

    value: str
    timestamp: int

    @property
    def age(self) -> float:
        """Return how many seconds ago the device reported this value."""
        return max(0.0, (int(time.time() * 1000) - self.timestamp) / 1000)


def _to_ms(value: Any) -> int:
    """Coerce a device timestamp to epoch milliseconds."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class QuboApi:
    """Per-attribute reads against the v2 shadow."""

    def __init__(self, client: QuboClient) -> None:
        self._client = client

    @property
    def sp_id(self) -> str:
        """Return the service provider id."""
        return self._client.sp_id

    async def read_attribute(
        self, device_uuid: str, service: str, attribute: str
    ) -> AttributeReading | None:
        """Read one attribute with the device's own timestamp.

        Returns ``None`` when the device or attribute does not exist, which is
        how the integration detects that this hardware cannot report it.
        """
        try:
            resp = await self._client.get_device_shadow_v2(
                device_uuid, service_name=service, attribute_name=attribute
            )
        except QuboCommandError as err:
            _LOGGER.debug("Qubo %s.%s unavailable: %s", service, attribute, err)
            return None
        return AttributeReading(
            value=str(resp.get("attributeValue", ""))[:64],
            timestamp=_to_ms(resp.get("timestamp")),
        )

    async def fetch_doorbell_events(
        self, unit_uuid: str, device_uuid: str
    ) -> list[DoorbellEvent]:
        """Return doorbell events for one video device, newest first.

        This is the cloud video service, not the device shadow. It is the only
        doorbell activity the cloud exposes to this integration, and it carries
        no motion or person classification - only that the doorbell rang.
        """
        path = f"/vms/api/v1/sp/{self.sp_id}/units/{unit_uuid}/devices/{device_uuid}"
        try:
            resp = await self._client.request("GET", path)
        except QuboCommandError as err:
            _LOGGER.debug("Qubo doorbell events unavailable: %s", err)
            return []
        return parse_vms_events(resp)

    def _device_path(self, template: str, device: QuboDevice) -> str:
        """Fill a qubopy control path template for one device.

        qubopy's own wrappers substitute ``"default"`` for a missing unit or
        entity uuid, which silently addresses the wrong scope, so format the
        template here where a missing uuid fails loudly instead.
        """
        if not device.unit_id or not device.entity_id:
            raise QuboCommandError(
                f"{device.name} has no unit/entity binding and cannot be commanded"
            )
        return template.format(
            sp=self.sp_id,
            unit_uuid=device.unit_id,
            entity_uuid=device.entity_id,
            device_uuid=device.device_id,
        )

    async def _write(self, path: str, body: dict[str, Any]) -> None:
        """PUT one write and let qubopy judge the reply.

        Since 0.1.4 ``client.request`` accepts the plain-text ``SUCCESS`` these
        endpoints answer with, raises :class:`QuboAuthError` on 401/403, retries
        once through a token refresh on 401, and raises
        :class:`QuboCommandError` with the body on anything else.
        """
        await self._client.request("PUT", path, json_data=body)

    async def write_attribute(
        self, device: QuboDevice, service: str, attribute: str, value: str
    ) -> None:
        """Write one shadow attribute.

        Mirrors the app's own request exactly, including the string-typed
        ``attributeValue``. The write is a request to the device, not a report
        of what the device has done, so callers should poll afterwards rather
        than assume the value stuck.
        """
        await self._write(
            self._device_path(PATH_UPDATE_SHADOW_ATTRIBUTE, device),
            {
                "instanceId": 0,
                "serviceName": service,
                "attributeName": attribute,
                "attributeValue": value,
            },
        )

    async def send_command(
        self,
        device: QuboDevice,
        service: str,
        command: str,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Send one command using the envelope the app uses for controls.

        ``instanceId`` is nested inside the command object, not beside
        ``services``; that differs from the attribute-write body above.
        """
        await self._write(
            self._device_path(PATH_SEND_COMMAND, device),
            {
                "devices": {
                    "handleName": device.name,
                    "deviceUUID": device.device_id,
                    "services": {
                        service: {
                            "commands": {
                                command: {
                                    "parameters": parameters or {},
                                    "instanceId": 0,
                                }
                            }
                        }
                    },
                }
            },
        )