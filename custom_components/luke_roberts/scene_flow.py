"""'Manage scenes' steps of the options flow.

Pick a static scene stored in the lamp, then rename, edit, duplicate,
restore or (for scenes Home Assistant created) delete it. Every change goes
through the lamp methods, so it gets the same backup and read-back check as
the actions. The flow ends with a short message instead of changing the
entry's options.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.config_entries import ConfigFlowResult, OptionsFlow
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    BooleanSelector,
    ColorRGBSelector,
    ColorTempSelector,
    ColorTempSelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.util.color import color_RGB_to_hs
import voluptuous as vol

from . import protocol
from .preview import uplight_rgb

if TYPE_CHECKING:
    from .lamp import LukeRobertsLamp

SCENE = "scene"
NAME = "name"
DOWNLIGHT_KELVIN = "downlight_kelvin"
DOWNLIGHT_BRIGHTNESS = "downlight_brightness"
UPLIGHT_COLOR = "uplight_color"
UPLIGHT_WHITE = "uplight_white"
UPLIGHT_KELVIN = "uplight_kelvin"
UPLIGHT_BRIGHTNESS = "uplight_brightness"

PERCENT = NumberSelector(
    NumberSelectorConfig(
        min=0, max=100, step=1, unit_of_measurement="%", mode=NumberSelectorMode.SLIDER
    )
)
KELVIN = ColorTempSelector(
    ColorTempSelectorConfig(
        unit="kelvin",
        min=protocol.MIN_SCENE_KELVIN,
        max=protocol.MAX_SCENE_KELVIN,
    )
)


def _percent(level: int) -> int:
    return round(level * 100 / 255)


def _dominant(light: protocol.StaticScene) -> tuple[int, int, int]:
    """Saturation, value, brightness of the brightest uplight segment."""
    return max(light.uplight, key=lambda segment: segment[2])


class SceneOptionsMixin(OptionsFlow):
    """Options flow steps for the scenes stored in the lamp."""

    _scene_id: int | None = None
    _edit_defaults: dict[str, Any] | None = None

    @property
    def _scene_lamp(self) -> LukeRobertsLamp | None:
        lamp = getattr(self.config_entry, "runtime_data", None)
        if lamp is None or not lamp.has_local or lamp.scene_backups is None:
            return None
        return lamp

    def _scene_done(self, reason: str, **placeholders: str) -> ConfigFlowResult:
        return self.async_abort(reason=reason, description_placeholders=placeholders)

    def _scene_failed(self, err: HomeAssistantError) -> ConfigFlowResult:
        return self.async_abort(
            reason="scene_failed", description_placeholders={"error": str(err)}
        )

    def _record(self) -> protocol.SceneRecord:
        assert self._scene_lamp is not None and self._scene_id is not None
        return self._scene_lamp.scene_records[self._scene_id]

    async def async_step_scenes(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Read the scenes once if needed, then pick one."""
        lamp = self._scene_lamp
        if lamp is None:
            return self.async_abort(reason="scenes_need_bluetooth")
        if lamp.scene_records:
            return await self.async_step_scene_pick()
        if user_input is None:
            return self.async_show_form(step_id="scenes", data_schema=vol.Schema({}))
        try:
            await lamp.backup_scenes("first read")
        except HomeAssistantError as err:
            return self._scene_failed(err)
        return await self.async_step_scene_pick()

    async def async_step_scene_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose a static scene."""
        lamp = self._scene_lamp
        assert lamp is not None
        if user_input is not None:
            self._scene_id = int(user_input[SCENE])
            return await self.async_step_scene_action()
        options = [
            SelectOptionDict(value=str(scene_id), label=record.name or str(scene_id))
            for scene_id, record in sorted(
                lamp.scene_records.items(), key=lambda item: item[1].sort_index
            )
            if record.scene_type == protocol.SceneType.STATIC
        ]
        if not options:
            return self.async_abort(reason="no_static_scenes")
        default = str(lamp.state.scene_id)
        key = (
            vol.Required(SCENE, default=default)
            if any(option["value"] == default for option in options)
            else vol.Required(SCENE)
        )
        return self.async_show_form(
            step_id="scene_pick",
            data_schema=vol.Schema(
                {
                    key: SelectSelector(
                        SelectSelectorConfig(
                            options=options, mode=SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    async def async_step_scene_action(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """What to do with the scene."""
        lamp = self._scene_lamp
        assert lamp is not None and lamp.scene_backups is not None
        menu = ["scene_edit", "scene_rename", "scene_duplicate", "scene_restore"]
        if self._scene_id in await lamp.scene_backups.created():
            menu.append("scene_delete")
        return self.async_show_menu(
            step_id="scene_action",
            menu_options=menu,
            description_placeholders={"scene": self._record().name},
        )

    async def async_step_scene_rename(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """New name for the scene."""
        lamp = self._scene_lamp
        assert lamp is not None and self._scene_id is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[NAME].strip()
            try:
                protocol.encode_scene_name(name)
            except protocol.SceneRecordError:
                errors[NAME] = "invalid_scene_name"
            else:
                try:
                    await lamp.rename_scene(self._scene_id, name)
                except HomeAssistantError as err:
                    return self._scene_failed(err)
                return self._scene_done("scene_saved", scene=name)
        return self.async_show_form(
            step_id="scene_rename",
            data_schema=vol.Schema(
                {vol.Required(NAME, default=self._record().name): TextSelector()}
            ),
            errors=errors,
        )

    async def async_step_scene_duplicate(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Name of the copy."""
        lamp = self._scene_lamp
        assert lamp is not None and self._scene_id is not None
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input[NAME].strip()
            try:
                protocol.encode_scene_name(name)
            except protocol.SceneRecordError:
                errors[NAME] = "invalid_scene_name"
            else:
                try:
                    await lamp.duplicate_scene(self._scene_id, name)
                except HomeAssistantError as err:
                    return self._scene_failed(err)
                return self._scene_done("scene_created", scene=name)
        suggestion = f"{self._record().name} 2"
        if len(suggestion.encode()) > protocol.SCENE_NAME_LENGTH:
            suggestion = self._record().name
        return self.async_show_form(
            step_id="scene_duplicate",
            data_schema=vol.Schema(
                {vol.Required(NAME, default=suggestion): TextSelector()}
            ),
            errors=errors,
        )

    async def async_step_scene_edit(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Colours and brightness; only fields that were changed are applied."""
        lamp = self._scene_lamp
        assert lamp is not None and self._scene_id is not None
        light = protocol.StaticScene.from_record(self._record())
        saturation, value, brightness = _dominant(light)
        white = saturation == 0
        defaults: dict[str, Any] = {
            DOWNLIGHT_KELVIN: light.kelvin,
            DOWNLIGHT_BRIGHTNESS: _percent(max(light.spots)),
            UPLIGHT_WHITE: white,
            UPLIGHT_COLOR: list(
                uplight_rgb(1 if white else saturation, 0 if white else value)
            ),
            UPLIGHT_KELVIN: value if white else protocol.MIN_SCENE_KELVIN,
            UPLIGHT_BRIGHTNESS: _percent(brightness),
        }
        if user_input is not None:
            changes: dict[str, Any] = {}
            if user_input[DOWNLIGHT_KELVIN] != defaults[DOWNLIGHT_KELVIN]:
                changes["kelvin"] = int(user_input[DOWNLIGHT_KELVIN])
            if user_input[DOWNLIGHT_BRIGHTNESS] != defaults[DOWNLIGHT_BRIGHTNESS]:
                changes["downlight_brightness"] = int(user_input[DOWNLIGHT_BRIGHTNESS])
            if user_input[UPLIGHT_BRIGHTNESS] != defaults[UPLIGHT_BRIGHTNESS]:
                changes["uplight_brightness"] = int(user_input[UPLIGHT_BRIGHTNESS])
            if user_input[UPLIGHT_WHITE]:
                if not white or user_input[UPLIGHT_KELVIN] != defaults[UPLIGHT_KELVIN]:
                    changes["uplight_color"] = protocol.uplight_white(
                        int(user_input[UPLIGHT_KELVIN])
                    )
            elif white or list(user_input[UPLIGHT_COLOR]) != defaults[UPLIGHT_COLOR]:
                hue, sat = color_RGB_to_hs(*user_input[UPLIGHT_COLOR])
                changes["uplight_color"] = protocol.uplight_hs(hue, sat)
            if not changes:
                return self._scene_done("scene_unchanged", scene=self._record().name)
            try:
                await lamp.edit_scene(self._scene_id, **changes)
            except HomeAssistantError as err:
                return self._scene_failed(err)
            return self._scene_done("scene_saved", scene=self._record().name)
        return self.async_show_form(
            step_id="scene_edit",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        UPLIGHT_WHITE, default=defaults[UPLIGHT_WHITE]
                    ): BooleanSelector(),
                    vol.Required(
                        UPLIGHT_COLOR, default=defaults[UPLIGHT_COLOR]
                    ): ColorRGBSelector(),
                    vol.Required(
                        UPLIGHT_KELVIN, default=defaults[UPLIGHT_KELVIN]
                    ): KELVIN,
                    vol.Required(
                        UPLIGHT_BRIGHTNESS, default=defaults[UPLIGHT_BRIGHTNESS]
                    ): PERCENT,
                    vol.Required(
                        DOWNLIGHT_KELVIN, default=defaults[DOWNLIGHT_KELVIN]
                    ): KELVIN,
                    vol.Required(
                        DOWNLIGHT_BRIGHTNESS, default=defaults[DOWNLIGHT_BRIGHTNESS]
                    ): PERCENT,
                }
            ),
            description_placeholders={"scene": self._record().name},
        )

    async def async_step_scene_restore(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm writing the newest differing backup back."""
        lamp = self._scene_lamp
        assert lamp is not None and self._scene_id is not None
        if user_input is not None:
            try:
                backup = await lamp.restore_scene(self._scene_id)
            except HomeAssistantError as err:
                return self._scene_failed(err)
            return self._scene_done(
                "scene_restored", scene=self._record().name, time=backup.time
            )
        return self.async_show_form(
            step_id="scene_restore",
            data_schema=vol.Schema({}),
            description_placeholders={"scene": self._record().name},
        )

    async def async_step_scene_delete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm deleting a scene Home Assistant created."""
        lamp = self._scene_lamp
        assert lamp is not None and self._scene_id is not None
        name = self._record().name
        if user_input is not None:
            try:
                await lamp.delete_scene(self._scene_id)
            except HomeAssistantError as err:
                return self._scene_failed(err)
            return self._scene_done("scene_deleted", scene=name)
        return self.async_show_form(
            step_id="scene_delete",
            data_schema=vol.Schema({}),
            description_placeholders={"scene": name},
        )
