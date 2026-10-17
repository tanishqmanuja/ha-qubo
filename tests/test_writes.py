"""Run with `python tests/test_writes.py` or `pytest tests/`.

The two write paths are the only requests this integration sends that the app
builds with a different envelope: the command body nests ``instanceId`` *inside*
the command rather than beside ``services``, and both paths are PUT where
qubopy's own helpers use POST. Either mistake earns a silent HTTP 405 from the
cloud, so the verbatim app bodies are pinned here.

qubopy is not installed in this checkout, so it is stubbed. The path constants
in the stub are written out literally rather than imported - they are the wire
contract under test, and re-deriving them from the stub would make the
assertions circular.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "qubo"

# Verbatim from qubopy 0.1.2 qubopy/const.py.
PATH_SEND_COMMAND = (
    "/unit-entity-management/api/v1/sp/{sp}/units/{unit_uuid}"
    "/entities/{entity_uuid}/devices/{device_uuid}/command"
)
PATH_UPDATE_SHADOW_ATTRIBUTE = (
    "/unit-entity-management/api/v1/sp/{sp}/units/{unit_uuid}"
    "/entities/{entity_uuid}/devices/{device_uuid}/shadow/attribute"
)


def _install_qubopy_stub() -> None:
    """Register a minimal qubopy so api.py imports without the real package."""
    if "qubopy" in sys.modules:
        return

    class QuboCommandError(Exception):
        """Raised when a command or shadow attribute update is rejected."""

    class QuboAuthError(Exception):
        """Raised when authentication fails or credentials are invalid."""

    const = types.ModuleType("qubopy.const")
    const.PATH_SEND_COMMAND = PATH_SEND_COMMAND
    const.PATH_UPDATE_SHADOW_ATTRIBUTE = PATH_UPDATE_SHADOW_ATTRIBUTE

    root = types.ModuleType("qubopy")
    root.QuboClient = object

    devices = types.ModuleType("qubopy.devices")
    devices.QuboDevice = object

    exceptions = types.ModuleType("qubopy.exceptions")
    exceptions.QuboCommandError = QuboCommandError
    exceptions.QuboAuthError = QuboAuthError

    root.const = const
    root.devices = devices
    root.exceptions = exceptions
    for name, module in (
        ("qubopy", root),
        ("qubopy.const", const),
        ("qubopy.devices", devices),
        ("qubopy.exceptions", exceptions),
    ):
        sys.modules[name] = module


_install_qubopy_stub()

# api.py uses a relative import for .shadow, so load it as part of a package
# rather than putting the component directory itself on sys.path.
_package = types.ModuleType("qubo_component")
_package.__path__ = [str(COMPONENT_DIR)]
sys.modules.setdefault("qubo_component", _package)
api = importlib.import_module("qubo_component.api")


class FakeResponse:
    """Minimal stand-in for an aiohttp response context manager."""

    def __init__(self, status: int, text: str) -> None:
        self.status = status
        self._text = text

    async def text(self) -> str:
        """Return the body the cloud sent."""
        return self._text

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False


class FakeSession:
    """Records writes and replies the way the Qubo cloud does."""

    def __init__(self, status: int = 200, text: str = "SUCCESS") -> None:
        self.status = status
        self.text = text
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method, url, json=None, headers=None):
        """Record the call and return the configured reply."""
        self.calls.append((method, url, json))
        return FakeResponse(self.status, self.text)


class FakeAuth:
    """Stand-in for QuboAuth's header provider."""

    async def get_headers(self) -> dict[str, str]:
        """Return the headers qubopy would attach."""
        return {"App-Id": "com.hero.iot"}


class RecordingClient:
    """Stand-in for QuboClient that keeps every call it is handed."""

    sp_id = "d10e4bfb0153496e8e8bb955f7ebe413"
    base_url = "https://srvcapp.platform.quboworld.com:443"

    def __init__(self, status: int = 200, text: str = "SUCCESS") -> None:
        self.auth = FakeAuth()
        self.session = FakeSession(status, text)
        self.reads: list[tuple[str, str, object]] = []

    async def _get_session(self):
        """Return the session qubopy would use."""
        return self.session

    async def request(self, method, path, json_data=None, params=None, retry=False):
        """Record a read and behave like a successful GET."""
        self.reads.append((method, path, json_data))
        return {"attributeValue": "false", "timestamp": 0}


class FakeDevice:
    """The Main Entrance video doorbell, as qubopy exposes it."""

    device_id = "00000000-0000-4000-8000-000000000002"
    unit_id = "00000000-0000-4000-8000-000000000001"
    entity_id = "00000000-0000-4000-8000-000000000003"
    name = "Video Doorbell Pro"


def test_attribute_write_is_a_put_with_the_apps_body() -> None:
    client = RecordingClient()
    asyncio.run(
        api.QuboApi(client).write_attribute(
            FakeDevice(), "doorbell", "dndEnabled", "true"
        )
    )

    method, url, body = client.session.calls[0]
    assert method == "PUT"
    assert url == (
        "https://srvcapp.platform.quboworld.com:443"
        "/unit-entity-management/api/v1/sp/d10e4bfb0153496e8e8bb955f7ebe413"
        "/units/00000000-0000-4000-8000-000000000001"
        "/entities/00000000-0000-4000-8000-000000000003"
        "/devices/00000000-0000-4000-8000-000000000002/shadow/attribute"
    )
    assert body == {
        "instanceId": 0,
        "serviceName": "doorbell",
        "attributeName": "dndEnabled",
        "attributeValue": "true",
    }


def test_reboot_command_nests_instance_id_inside_the_command() -> None:
    client = RecordingClient()
    asyncio.run(
        api.QuboApi(client).send_command(FakeDevice(), "deviceReboot", "reboot")
    )

    method, url, body = client.session.calls[0]
    assert method == "PUT"
    assert url.endswith(
        "/units/00000000-0000-4000-8000-000000000001"
        "/entities/00000000-0000-4000-8000-000000000003"
        "/devices/00000000-0000-4000-8000-000000000002/command"
    )
    assert body == {
        "devices": {
            "handleName": "Video Doorbell Pro",
            "deviceUUID": "00000000-0000-4000-8000-000000000002",
            "services": {
                "deviceReboot": {
                    "commands": {"reboot": {"parameters": {}, "instanceId": 0}}
                }
            },
        }
    }


def test_command_parameters_default_to_an_empty_object() -> None:
    client = RecordingClient()
    asyncio.run(
        api.QuboApi(client).send_command(
            FakeDevice(), "doorbell", "flash", {"duration": 3}
        )
    )
    assert client.session.calls[0][2]["devices"]["services"]["doorbell"][
        "commands"
    ] == {"flash": {"parameters": {"duration": 3}, "instanceId": 0}}


def test_a_plain_text_success_reply_is_not_an_error() -> None:
    """The cloud answers writes with `SUCCESS`, which qubopy would try to parse."""
    client = RecordingClient(status=200, text="SUCCESS")
    asyncio.run(
        api.QuboApi(client).write_attribute(
            FakeDevice(), "doorbell", "dndEnabled", "true"
        )
    )
    assert len(client.session.calls) == 1


def test_a_rejected_write_raises_instead_of_looking_like_success() -> None:
    client = RecordingClient(status=400, text="ERR_SM_050")
    try:
        asyncio.run(
            api.QuboApi(client).write_attribute(
                FakeDevice(), "doorbell", "dndEnabled", "true"
            )
        )
    except api.QuboCommandError as err:
        assert "ERR_SM_050" in str(err)
        return
    raise AssertionError("a rejected write must raise")


def test_a_rejected_session_is_reported_as_an_auth_error() -> None:
    """Auth errors must reach the coordinator so it re-logs in."""
    client = RecordingClient(status=401, text="")
    try:
        asyncio.run(api.QuboApi(client).send_command(FakeDevice(), "deviceReboot", "reboot"))
    except api.QuboAuthError:
        return
    raise AssertionError("a 401 write must raise QuboAuthError")


def test_an_unbound_device_is_refused_rather_than_guessed() -> None:
    """qubopy substitutes "default" here; that silently addresses the wrong scope."""
    client = RecordingClient()
    unbound = FakeDevice()
    unbound.unit_id = ""
    unbound.entity_id = ""

    for call in (
        api.QuboApi(client).write_attribute(unbound, "doorbell", "dndEnabled", "true"),
        api.QuboApi(client).send_command(unbound, "deviceReboot", "reboot"),
    ):
        try:
            asyncio.run(call)
        except api.QuboCommandError:
            continue
        raise AssertionError("an unbound device must not be commanded")

    assert client.session.calls == []


if __name__ == "__main__":
    for _name, _func in sorted(globals().items()):
        if _name.startswith("test_"):
            _func()
            print(f"ok  {_name}")
    print("all passed")