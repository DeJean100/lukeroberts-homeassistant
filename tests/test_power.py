"""Power switch entity wrapper."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.luke_roberts.power import EntityPowerSwitch


async def test_power_switch_state(hass: HomeAssistant) -> None:
    """On, off, unavailable and a missing entity."""
    power = EntityPowerSwitch(hass, "light.dining_power")
    assert power.is_on is None  # entity does not exist (yet)
    hass.states.async_set("light.dining_power", "on")
    assert power.is_on is True
    hass.states.async_set("light.dining_power", "off")
    assert power.is_on is False
    hass.states.async_set("light.dining_power", "unavailable")
    assert power.is_on is None


async def test_power_switch_calls_own_domain(hass: HomeAssistant) -> None:
    """A switch is switched with switch.turn_on/turn_off."""
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    power = EntityPowerSwitch(hass, "switch.plug")
    await power.turn_on()
    await power.turn_off()
    assert on[0].data == {"entity_id": "switch.plug"}
    assert off[0].data == {"entity_id": "switch.plug"}
