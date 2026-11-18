"""Scene preview: SVG drawing and the image entity."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch
import xml.etree.ElementTree as ET

from homeassistant.components.image import async_get_image
from homeassistant.components.light import ATTR_EFFECT, DOMAIN as LIGHT_DOMAIN
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import HomeAssistant
import pytest

from custom_components.luke_roberts import protocol
from custom_components.luke_roberts.const import DOMAIN, SERVICE_BACKUP_SCENES
from custom_components.luke_roberts.preview import kelvin_to_rgb, scene_svg

from .common import FakeLampClient
from .test_init import LIGHT, _setup_ble

FIXTURE = Path(__file__).parent / "fixtures" / "scene_sync_model_f.hex"
IMAGE = "image.esstisch_scene_preview"
SVG = "{http://www.w3.org/2000/svg}"


@pytest.fixture(autouse=True)
def _bluetooth(mock_bluetooth):
    """Keep the real Bluetooth stack from starting."""


def _records() -> dict[int, protocol.SceneRecord]:
    stream = bytes.fromhex(FIXTURE.read_text().replace("\n", ""))
    return {r.scene_id: r for r in protocol.parse_scene_records(stream)}


def _group(svg: ET.Element, name: str) -> ET.Element:
    return next(g for g in svg.iter(f"{SVG}g") if g.get("filter") == f"url(#{name})")


def test_scene_svg_draws_uplight_and_spots() -> None:
    """16 uplight blobs, one dot per lit downlight spot, round transparent tile."""
    records = _records()
    bright = ET.fromstring(scene_svg(records[7]))  # every spot at full
    assert len(_group(bright, "aura").findall(f"{SVG}circle")) == 16
    assert len(_group(bright, "glow").findall(f"{SVG}circle")) == 84
    assert bright.find(f"{SVG}rect") is None  # corners stay transparent
    assert bright.find(f".//{SVG}clipPath") is not None
    indirect = ET.fromstring(scene_svg(records[1]))  # uplight only
    assert _group(indirect, "glow").findall(f"{SVG}circle") == []


def test_scene_svg_other_types() -> None:
    """Off and dynamic scenes get a text tile."""
    records = _records()
    assert b"Off" in scene_svg(records[0])
    demo = scene_svg(records[8])
    assert b"Demo" in demo
    assert b"animated" in demo
    ET.fromstring(demo)


def test_kelvin_colours() -> None:
    """Warm white is orange, 4000 K is lighter."""
    warm = kelvin_to_rgb(2700)
    neutral = kelvin_to_rgb(4000)
    assert warm[0] == 255
    assert warm[2] < neutral[2]


async def test_preview_follows_the_scene(hass: HomeAssistant) -> None:
    """A hint before the first backup, then the picture of the active scene."""
    client = FakeLampClient()
    await _setup_ble(hass, client)
    state = hass.states.get(IMAGE)
    assert state is not None
    image = await async_get_image(hass, IMAGE)
    assert image.content_type == "image/svg+xml"
    assert b"Back up scenes" in image.content

    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP_SCENES,
            {ATTR_ENTITY_ID: LIGHT},
            blocking=True,
            return_response=True,
        )
        await hass.async_block_till_done()
        image = await async_get_image(hass, IMAGE)
        assert image.content == scene_svg(
            protocol.SceneRecord(client.records[1])
        )  # Lesen is active

        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: LIGHT, ATTR_EFFECT: "Gemütlich"},
            blocking=True,
        )
        await hass.async_block_till_done()
    image = await async_get_image(hass, IMAGE)
    assert image.content == scene_svg(protocol.SceneRecord(client.records[2]))
    state = hass.states.get(IMAGE)
    assert state.attributes["scene"] == "Gemütlich"


async def test_entity_picture_changes_with_the_scene(
    hass: HomeAssistant, hass_client, freezer
) -> None:
    """The picture URL carries a version, so thumbnails do not stay cached."""
    client = FakeLampClient()
    await _setup_ble(hass, client)
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP_SCENES,
            {ATTR_ENTITY_ID: LIGHT},
            blocking=True,
            return_response=True,
        )
        await hass.async_block_till_done()
        first = hass.states.get(IMAGE).attributes["entity_picture"]
        assert "&v=" in first

        client.records[2] = (
            client.records[2][:120] + b"\xff" * 38 + client.records[2][158:]
        )
        client.change_record(client.records[2])
        freezer.tick(5)
        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON,
            {ATTR_ENTITY_ID: LIGHT, ATTR_EFFECT: "Gemütlich"},
            blocking=True,
        )
        await hass.services.async_call(
            "button",
            "press",
            {ATTR_ENTITY_ID: "button.esstisch_refresh_scenes"},
            blocking=True,
        )
        await hass.async_block_till_done()
    second = hass.states.get(IMAGE).attributes["entity_picture"]
    assert second != first

    http = await hass_client()
    response = await http.get(second)
    assert response.status == 200
    assert response.headers["Content-Type"] == "image/svg+xml"
    assert await response.read() == scene_svg(protocol.SceneRecord(client.records[2]))
