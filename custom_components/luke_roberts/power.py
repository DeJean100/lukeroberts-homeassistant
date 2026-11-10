"""Optional power switch in front of the lamp.

Some lamps hang on a switched circuit (a KNX actuator, a smart plug, a relay).
When such an entity is configured, the lamp's light switches the power for
on/off and uses Bluetooth only for brightness, colour temperature and scenes.
After a power cut the lamp starts its own power-up (Default) scene.
"""

from __future__ import annotations

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant, split_entity_id


class EntityPowerSwitch:
    """A light or switch entity that powers the lamp."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self.hass = hass
        self.entity_id = entity_id

    @property
    def is_on(self) -> bool | None:
        """True while powered, False while cut, None if unknown/unavailable."""
        state = self.hass.states.get(self.entity_id)
        if state is None:
            return None
        if state.state == STATE_ON:
            return True
        if state.state == STATE_OFF:
            return False
        return None

    async def _call(self, service: str) -> None:
        domain, _ = split_entity_id(self.entity_id)
        await self.hass.services.async_call(
            domain, service, {ATTR_ENTITY_ID: self.entity_id}, blocking=True
        )

    async def turn_on(self) -> None:
        """Switch the power on."""
        await self._call(SERVICE_TURN_ON)

    async def turn_off(self) -> None:
        """Switch the power off."""
        await self._call(SERVICE_TURN_OFF)
