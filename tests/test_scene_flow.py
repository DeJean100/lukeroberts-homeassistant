"""'Manage scenes' in the options flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest

from custom_components.luke_roberts import protocol

from .common import FakeLampClient
from .test_init import _setup_ble


@pytest.fixture(autouse=True)
def _bluetooth(mock_bluetooth):
    """Keep the real Bluetooth stack from starting."""


@pytest.fixture
def client() -> FakeLampClient:
    return FakeLampClient()


@pytest.fixture(autouse=True)
def _connect(client: FakeLampClient):
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        yield


async def _open(hass: HomeAssistant, entry_id: str, scene: str = "1") -> dict:
    """Options -> Manage scenes -> (read) -> pick scene -> action menu."""
    flow = hass.config_entries.options
    result = await flow.async_init(entry_id)
    assert result["type"] is FlowResultType.MENU
    assert result["menu_options"] == ["connection", "scenes"]
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "scenes"})
    if result["step_id"] == "scenes":  # first time: read the scenes
        result = await flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "scene_pick"
    values = [
        o["value"] for o in result["data_schema"].schema["scene"].config["options"]
    ]
    assert values == ["1", "2"]  # static scenes only, by sort index
    result = await flow.async_configure(result["flow_id"], {"scene": scene})
    assert result["type"] is FlowResultType.MENU
    return result


async def test_menu_and_connection(hass: HomeAssistant, client) -> None:
    """The connection form is still there, behind the menu."""
    entry = await _setup_ble(hass, client)
    flow = hass.config_entries.options
    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"], {"next_step_id": "connection"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "connection"


async def test_rename_through_the_flow(hass: HomeAssistant, client) -> None:
    """Read the scenes once, pick, rename; a bad name is refused in the form."""
    entry = await _setup_ble(hass, client)
    result = await _open(hass, entry.entry_id)
    assert "scene_delete" not in result["menu_options"]  # made in the app
    flow = hass.config_entries.options
    result = await flow.async_configure(
        result["flow_id"], {"next_step_id": "scene_rename"}
    )
    result = await flow.async_configure(result["flow_id"], {"name": "x" * 21})
    assert result["errors"] == {"name": "invalid_scene_name"}
    result = await flow.async_configure(result["flow_id"], {"name": "Lesen hell"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "scene_saved"
    assert protocol.SceneRecord(client.records[1]).name == "Lesen hell"
    assert entry.options.get("connection_mode") == "local_first"  # untouched


async def test_edit_through_the_flow(hass: HomeAssistant, client) -> None:
    """Only changed fields are applied; nothing changed writes nothing."""
    entry = await _setup_ble(hass, client)
    original = protocol.StaticScene.from_record(protocol.SceneRecord(client.records[1]))
    flow = hass.config_entries.options

    result = await _open(hass, entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"], {"next_step_id": "scene_edit"}
    )
    defaults = {key.schema: key.default() for key in result["data_schema"].schema}
    result = await flow.async_configure(result["flow_id"], defaults)
    assert result["reason"] == "scene_unchanged"
    assert client.record_writes == []

    result = await _open(hass, entry.entry_id)
    result = await flow.async_configure(
        result["flow_id"], {"next_step_id": "scene_edit"}
    )
    changed = {**defaults, "uplight_color": [0, 0, 255], "downlight_kelvin": 3300}
    result = await flow.async_configure(result["flow_id"], changed)
    assert result["reason"] == "scene_saved"
    light = protocol.StaticScene.from_record(protocol.SceneRecord(client.records[1]))
    assert light.kelvin == 3300
    assert {segment[:2] for segment in light.uplight} == {(255, 43690)}  # blue
    assert [s[2] for s in light.uplight] == [s[2] for s in original.uplight]
    assert light.spots == original.spots


async def test_duplicate_then_delete(hass: HomeAssistant, client) -> None:
    """A copy made in the flow can be deleted in the flow."""
    entry = await _setup_ble(hass, client)
    flow = hass.config_entries.options
    result = await _open(hass, entry.entry_id, "2")
    result = await flow.async_configure(
        result["flow_id"], {"next_step_id": "scene_duplicate"}
    )
    result = await flow.async_configure(result["flow_id"], {"name": "Kopie"})
    assert result["reason"] == "scene_created"
    assert protocol.SceneRecord(client.records[4]).name == "Kopie"

    result = await flow.async_init(entry.entry_id)
    result = await flow.async_configure(result["flow_id"], {"next_step_id": "scenes"})
    result = await flow.async_configure(result["flow_id"], {"scene": "4"})
    assert "scene_delete" in result["menu_options"]
    result = await flow.async_configure(
        result["flow_id"], {"next_step_id": "scene_delete"}
    )
    result = await flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "scene_deleted"
    assert protocol.SceneRecord(client.records[4]).is_empty


async def test_failure_is_reported(hass: HomeAssistant, client) -> None:
    """A lamp error ends the flow with its message; nothing is written."""
    entry = await _setup_ble(hass, client)
    result = await _open(hass, entry.entry_id)
    client.drop_done = 100
    client.change_record(client.records[1])  # forces a change read that fails
    flow = hass.config_entries.options
    with (
        patch("custom_components.luke_roberts.lamp.SYNC_CHANGES_TIMEOUT", 0.01),
        patch("custom_components.luke_roberts.lamp.SYNC_CHANGES_ATTEMPTS", 2),
    ):
        result = await flow.async_configure(
            result["flow_id"], {"next_step_id": "scene_rename"}
        )
        result = await flow.async_configure(result["flow_id"], {"name": "Neu"})
    assert result["reason"] == "scene_failed"
    assert "did not arrive complete" in result["description_placeholders"]["error"]
    assert client.record_writes == []
