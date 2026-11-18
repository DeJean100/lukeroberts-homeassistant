"""Scene preview image for Luke Roberts."""

from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import LukeRobertsConfigEntry
from .const import SCENE_OFF
from .entity import LukeRobertsEntity
from .preview import message_svg, scene_svg


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LukeRobertsConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the scene preview."""
    async_add_entities([LukeRobertsScenePreview(hass, entry)])


class LukeRobertsScenePreview(LukeRobertsEntity, ImageEntity):
    """Picture of the active scene, drawn from its scene record."""

    _attr_translation_key = "scene_preview"
    _attr_content_type = "image/svg+xml"

    def __init__(self, hass: HomeAssistant, entry: LukeRobertsConfigEntry) -> None:
        LukeRobertsEntity.__init__(self, entry, "scene_preview")
        ImageEntity.__init__(self, hass)
        self._image = self._render()
        self._attr_image_last_updated = dt_util.utcnow()

    def _render(self) -> bytes:
        lamp = self._lamp
        scene_id = lamp.state.scene_id
        if scene_id is None:
            return message_svg("Scene unknown")
        if scene_id == SCENE_OFF:
            return message_svg("Off")
        record = lamp.scene_records.get(scene_id)
        if record is None:
            name = lamp.scene_name(scene_id) or f"Scene {scene_id}"
            if not lamp.scene_records:
                return message_svg(f"{name}\nrun 'Back up scenes' once")
            return message_svg(f"{name}\nno details yet")
        return scene_svg(record)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """The scene shown."""
        scene_id = self._lamp.state.scene_id
        return {
            "scene_id": scene_id,
            "scene": self._lamp.scene_name(scene_id),
        }

    async def async_added_to_hass(self) -> None:
        """Subscribe to lamp updates (redraw when the scene changes)."""
        await ImageEntity.async_added_to_hass(self)
        self.async_on_remove(self._lamp.add_listener(self._lamp_updated))

    @callback
    def _lamp_updated(self) -> None:
        image = self._render()
        if image != self._image:
            self._image = image
            self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        """The current picture."""
        return self._image
