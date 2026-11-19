"""Websocket commands for the scene editor card.

The card works on whole lights (16 uplight segments, 84 spots), which the
entity actions do not cover: it reads every static scene, shows edits live
on the lamp and saves or creates a scene.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er
import voluptuous as vol

from . import protocol
from .const import DOMAIN
from .lamp import LukeRobertsLamp

LIGHT_SCHEMA = vol.Schema(
    {
        vol.Required("kelvin"): vol.All(
            vol.Coerce(int),
            vol.Range(protocol.MIN_SCENE_KELVIN, protocol.MAX_SCENE_KELVIN),
        ),
        vol.Required("uplight"): vol.All(
            [
                vol.ExactSequence(
                    [
                        vol.All(vol.Coerce(int), vol.Range(0, 255)),
                        vol.All(vol.Coerce(int), vol.Range(0, 0xFFFF)),
                        vol.All(vol.Coerce(int), vol.Range(0, 255)),
                    ]
                )
            ],
            vol.Length(min=protocol.UPLIGHT_SEGMENTS, max=protocol.UPLIGHT_SEGMENTS),
        ),
        vol.Required("spots"): vol.All(
            [vol.All(vol.Coerce(int), vol.Range(0, 255))],
            vol.Length(min=protocol.DOWNLIGHT_SPOTS, max=protocol.DOWNLIGHT_SPOTS),
        ),
    }
)


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the commands (once per Home Assistant run)."""
    for command in (ws_scenes, ws_preview, ws_end_preview, ws_save):
        websocket_api.async_register_command(hass, command)


def _lamp(hass: HomeAssistant, entity_id: str) -> LukeRobertsLamp:
    entity = er.async_get(hass).async_get(entity_id)
    entry = (
        hass.config_entries.async_get_entry(entity.config_entry_id)
        if entity is not None and entity.config_entry_id
        else None
    )
    if (
        entry is None
        or entry.domain != DOMAIN
        or entry.state is not ConfigEntryState.LOADED
        or not isinstance(entry.runtime_data, LukeRobertsLamp)
    ):
        raise HomeAssistantError(f"{entity_id} is not a Luke Roberts lamp")
    return entry.runtime_data


def _light(data: dict[str, Any]) -> protocol.StaticScene:
    return protocol.StaticScene(
        data["kelvin"], [tuple(segment) for segment in data["uplight"]], data["spots"]
    )


def _scene(record: protocol.SceneRecord, created: set[int]) -> dict[str, Any]:
    light = protocol.StaticScene.from_record(record)
    return {
        "id": record.scene_id,
        "name": record.name,
        "sort": record.sort_index,
        "created": record.scene_id in created,
        "kelvin": light.kelvin,
        "uplight": [list(segment) for segment in light.uplight],
        "spots": light.spots,
    }


async def _run(
    connection: websocket_api.ActiveConnection, msg_id: int, job: Any
) -> None:
    try:
        result = await job
    except HomeAssistantError as err:
        connection.send_error(msg_id, "lamp_error", str(err))
        return
    connection.send_result(msg_id, result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "luke_roberts/scenes",
        vol.Required("entity_id"): cv.entity_id,
    }
)
@websocket_api.async_response
async def ws_scenes(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Every static scene with its light, in the app's order."""

    async def job() -> dict[str, Any]:
        lamp = _lamp(hass, msg["entity_id"])
        records, error = await lamp.scene_records_for_editing()
        assert lamp.scene_backups is not None
        created = await lamp.scene_backups.created()
        static = sorted(
            (r for r in records.values() if r.scene_type == protocol.SceneType.STATIC),
            key=lambda record: (record.sort_index, record.scene_id),
        )
        return {
            "active": lamp.state.scene_id,
            "scenes": [_scene(record, created) for record in static],
            "free_slots": sum(record.is_empty for record in records.values()),
            "stale": error,
        }

    await _run(connection, msg["id"], job())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "luke_roberts/preview",
        vol.Required("entity_id"): cv.entity_id,
        vol.Required("light"): LIGHT_SCHEMA,
    }
)
@websocket_api.async_response
async def ws_preview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Show a light on the lamp without saving it."""

    async def job() -> None:
        await _lamp(hass, msg["entity_id"]).preview_light(_light(msg["light"]))

    await _run(connection, msg["id"], job())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "luke_roberts/end_preview",
        vol.Required("entity_id"): cv.entity_id,
    }
)
@websocket_api.async_response
async def ws_end_preview(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Back to the selected scene."""

    async def job() -> None:
        await _lamp(hass, msg["entity_id"]).end_preview()

    await _run(connection, msg["id"], job())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "luke_roberts/save_scene",
        vol.Required("entity_id"): cv.entity_id,
        vol.Exclusive("scene_id", "target"): vol.All(
            vol.Coerce(int), vol.Range(0, 255)
        ),
        vol.Exclusive("name", "target"): cv.string,
        vol.Required("light"): LIGHT_SCHEMA,
    }
)
@websocket_api.async_response
async def ws_save(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Overwrite a scene's light (``scene_id``) or create a scene (``name``)."""

    async def job() -> dict[str, int]:
        lamp = _lamp(hass, msg["entity_id"])
        light = _light(msg["light"])
        if "scene_id" in msg:
            await lamp.save_scene_light(msg["scene_id"], light)
            return {"scene_id": msg["scene_id"]}
        if "name" in msg:
            return {"scene_id": await lamp.create_scene(msg["name"], light)}
        raise HomeAssistantError("Give scene_id to save or name to create a scene")

    await _run(connection, msg["id"], job())
