"""Websocket commands of the scene editor card."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
import pytest
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.luke_roberts import protocol
from custom_components.luke_roberts.card import CARD_FILE

from .common import FakeLampClient
from .test_init import LIGHT, _setup_ble


def _light(record: bytes) -> dict:
    light = protocol.StaticScene.from_record(protocol.SceneRecord(record))
    return {
        "kelvin": light.kelvin,
        "uplight": [list(s) for s in light.uplight],
        "spots": light.spots,
    }


@pytest.fixture
def connect():
    client = FakeLampClient()
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        yield client


async def test_scenes_preview_save_create(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, connect: FakeLampClient
) -> None:
    """The card reads scenes, previews without saving, saves and creates."""
    client = connect
    await _setup_ble(hass, client)
    ws = await hass_ws_client(hass)

    await ws.send_json({"id": 1, "type": "luke_roberts/scenes", "entity_id": LIGHT})
    result = (await ws.receive_json())["result"]
    assert [s["name"] for s in result["scenes"]] == ["Lesen", "Gemütlich"]  # sort
    assert result["free_slots"] == 1
    assert result["stale"] is None
    first = result["scenes"][0]
    assert first["id"] == 1
    assert {k: first[k] for k in ("kelvin", "uplight", "spots")} == _light(
        client.records[1]
    )

    # Preview: one live model write, nothing stored.
    light = _light(client.records[1])
    light["kelvin"] = 3500
    light["uplight"][0] = [0, 3000, 255]
    light["spots"][5] = 200
    records = dict(client.records)
    await ws.send_json(
        {"id": 2, "type": "luke_roberts/preview", "entity_id": LIGHT, "light": light}
    )
    assert (await ws.receive_json())["success"]
    expected = protocol.StaticScene(
        3500, [tuple(s) for s in light["uplight"]], light["spots"]
    )
    assert client.live_model == [expected.live_model()]
    assert client.records == records

    # Save over scene 1: backup first, header and name kept.
    await ws.send_json(
        {
            "id": 3,
            "type": "luke_roberts/save_scene",
            "entity_id": LIGHT,
            "scene_id": 1,
            "light": light,
        }
    )
    assert (await ws.receive_json())["result"] == {"scene_id": 1}
    saved = protocol.SceneRecord(client.records[1])
    assert saved.raw == expected.apply(protocol.SceneRecord(records[1])).raw
    assert saved.name == "Lesen"

    # Create a new scene in the free slot.
    await ws.send_json(
        {
            "id": 4,
            "type": "luke_roberts/save_scene",
            "entity_id": LIGHT,
            "name": "Abend",
            "light": light,
        }
    )
    new_id = (await ws.receive_json())["result"]["scene_id"]
    created = protocol.SceneRecord(client.records[new_id])
    assert created.name == "Abend"
    assert protocol.StaticScene.from_record(created).kelvin == 3500

    await ws.send_json({"id": 5, "type": "luke_roberts/scenes", "entity_id": LIGHT})
    result = (await ws.receive_json())["result"]
    assert result["free_slots"] == 0
    assert [s["created"] for s in result["scenes"] if s["id"] == new_id] == [True]

    # End preview: the selected scene is selected again.
    client.written.clear()
    await ws.send_json(
        {"id": 6, "type": "luke_roberts/end_preview", "entity_id": LIGHT}
    )
    assert (await ws.receive_json())["success"]
    assert client.written


async def test_bad_requests(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, connect: FakeLampClient
) -> None:
    """Wrong lights, scenes and entities are refused with a message."""
    client = connect
    await _setup_ble(hass, client)
    ws = await hass_ws_client(hass)
    light = _light(client.records[1])

    short = {**light, "uplight": light["uplight"][:15]}
    await ws.send_json(
        {"id": 1, "type": "luke_roberts/preview", "entity_id": LIGHT, "light": short}
    )
    assert (await ws.receive_json())["error"]["code"] == "invalid_format"

    await ws.send_json(
        {
            "id": 2,
            "type": "luke_roberts/save_scene",
            "entity_id": LIGHT,
            "scene_id": 3,  # Demo, dynamic
            "light": light,
        }
    )
    error = (await ws.receive_json())["error"]
    assert error["code"] == "lamp_error"
    assert "not a static scene" in error["message"]

    await ws.send_json(
        {"id": 3, "type": "luke_roberts/save_scene", "entity_id": LIGHT, "light": light}
    )
    assert (await ws.receive_json())["error"]["code"] == "lamp_error"

    await ws.send_json(
        {"id": 4, "type": "luke_roberts/scenes", "entity_id": "light.kitchen"}
    )
    assert "not a Luke Roberts lamp" in (await ws.receive_json())["error"]["message"]
    assert client.record_writes == []


async def test_stored_copy_when_the_lamp_cannot_be_read(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, connect: FakeLampClient
) -> None:
    """The editor still gets the scenes, marked as stale."""
    client = connect
    entry = await _setup_ble(hass, client)
    ws = await hass_ws_client(hass)
    await ws.send_json({"id": 1, "type": "luke_roberts/scenes", "entity_id": LIGHT})
    assert (await ws.receive_json())["success"]

    lamp = entry.runtime_data
    with patch.object(
        type(lamp), "read_scene_records", AsyncMock(side_effect=HomeAssistantError("x"))
    ):
        await ws.send_json({"id": 2, "type": "luke_roberts/scenes", "entity_id": LIGHT})
        result = (await ws.receive_json())["result"]
    assert result["stale"] == "x"
    assert [s["name"] for s in result["scenes"]] == ["Lesen", "Gemütlich"]


def test_new_record_and_card_file() -> None:
    """A new record has the create header; the card file ships along."""
    light = protocol.StaticScene(2700, [(0, 2700, 255)] * 16, [0] * 84)
    record = light.new_record("Neu")
    assert record.raw[:8] == bytes([1, protocol.SCENE_NEW, 0, 1, 0, 0, 0, 0])
    assert record.name == "Neu"
    assert len(record.raw) == protocol.SCENE_RECORD_LENGTH[protocol.SceneType.STATIC]
    with pytest.raises(protocol.SceneRecordError):
        protocol.StaticScene(2700, [(0, 2700, 255)] * 15, [0] * 84).light_bytes()
    assert "luke-roberts-scene-card" in CARD_FILE.read_text()
