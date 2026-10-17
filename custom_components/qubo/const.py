"""Constants for the Qubo integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "qubo"

CONF_DEVICE_NAME: Final = "device_name"
CONF_MOBILE_DEVICE_ID: Final = "mobile_device_id"
CONF_LOGIN_DEVICE_ID: Final = "login_device_id"
CONF_ACCESS_TOKEN: Final = "access_token"
CONF_REFRESH_TOKEN: Final = "refresh_token"
CONF_USER_UUID: Final = "user_uuid"

# Label shown for this integration under Qubo app -> Account -> Logged In Devices.
DEFAULT_DEVICE_NAME: Final = "Home Assistant"

DEFAULT_SCAN_INTERVAL: Final = 60

# Stream profiles accepted by the cloud stream manager. "high" is 1312x2304 @10fps
# on the Video Doorbell Pro, "low" is 480x640.
STREAM_QUALITY: Final = "high"

# Entity unique-id prefix, kept identical to the previous qubo_lock integration
# so existing entity ids survive the domain rename.
UNIQUE_ID_PREFIX: Final = "qubo"