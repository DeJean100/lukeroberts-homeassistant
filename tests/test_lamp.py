"""Routing and assumed state of LukeRobertsLamp."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from bleak.exc import BleakError
from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.luke_roberts import protocol
from custom_components.luke_roberts.const import (
    MODE_CLOUD_FIRST,
    MODE_CLOUD_ONLY,
    MODE_LOCAL_FIRST,
    MODE_LOCAL_ONLY,
)
from custom_components.luke_roberts.lamp import (
    ROUTE_BLUETOOTH,
    ROUTE_CLOUD,
    LukeRobertsLamp,
)

OK = b"\x00"


def _ble(responses: list[bytes] | Exception | None = None) -> MagicMock:
    ble = MagicMock()
    if isinstance(responses, Exception):
        ble.request = AsyncMock(side_effect=responses)
        ble.request_many = AsyncMock(side_effect=responses)
    else:
        ble.request = AsyncMock(side_effect=responses or [OK] * 10)
        ble.request_many = AsyncMock(side_effect=lambda frames: [OK] * len(frames))
    return ble


def _lamp(mode: str, ble: MagicMock | None, cloud: MagicMock | None) -> LukeRobertsLamp:
    return LukeRobertsLamp(
        name="Test", mode=mode, ble=ble, cloud=cloud, cloud_lamp_id=7 if cloud else None
    )


async def test_turn_on_local_selects_default_then_adjusts() -> None:
    """From off: default scene, brightness, colour temperature in one go."""
    ble = _ble()
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    await lamp.turn_on(brightness=40, kelvin=3000)
    ble.request_many.assert_awaited_once_with(
        [
            protocol.select_scene(0xFF),
            protocol.brightness(40),
            protocol.color_temperature(3000),
        ]
    )
    assert lamp.state.is_on
    assert lamp.state.brightness == 40
    assert lamp.state.kelvin == 3000
    assert lamp.state.last_route == ROUTE_BLUETOOTH


async def test_turn_on_while_on_only_adjusts() -> None:
    """When already on, only the changed attribute is sent."""
    ble = _ble()
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    lamp.state.is_on = True
    await lamp.turn_on(brightness=70)
    ble.request_many.assert_awaited_once_with([protocol.brightness(70)])


async def test_fallback_to_cloud_when_ble_fails() -> None:
    """Local first: a Bluetooth error falls back to the cloud."""
    ble = _ble(BleakError("out of range"))
    cloud = MagicMock(power=AsyncMock(), brightness=AsyncMock(), scene=AsyncMock())
    lamp = _lamp(MODE_LOCAL_FIRST, ble, cloud)
    await lamp.turn_on(brightness=55)
    cloud.power.assert_awaited_once_with(7, True)
    cloud.brightness.assert_awaited_once_with(7, 55)
    assert lamp.state.last_route == ROUTE_CLOUD


async def test_forbidden_falls_back_to_cloud() -> None:
    """A lamp with a security code still works through the cloud."""
    ble = MagicMock(request=AsyncMock(return_value=b"\xfc"))
    cloud = MagicMock(power=AsyncMock())
    lamp = _lamp(MODE_LOCAL_FIRST, ble, cloud)
    await lamp.turn_off()
    cloud.power.assert_awaited_once_with(7, False)
    assert lamp.state.is_on is False


async def test_local_only_does_not_use_cloud() -> None:
    """Local only never touches the cloud and surfaces the error."""
    ble = _ble(BleakError("gone"))
    cloud = MagicMock(power=AsyncMock())
    lamp = _lamp(MODE_LOCAL_ONLY, ble, cloud)
    with pytest.raises(HomeAssistantError, match="bluetooth"):
        await lamp.turn_off()
    cloud.power.assert_not_awaited()


async def test_cloud_first() -> None:
    """Cloud first uses the cloud even when BLE would work."""
    ble = _ble()
    cloud = MagicMock(scene=AsyncMock())
    lamp = _lamp(MODE_CLOUD_FIRST, ble, cloud)
    await lamp.select_scene(4)
    cloud.scene.assert_awaited_once_with(7, 4)
    ble.request_many.assert_not_awaited()
    assert lamp.state.scene_id == 4


async def test_ble_only_action_without_ble() -> None:
    """Next scene needs Bluetooth."""
    lamp = _lamp(MODE_LOCAL_FIRST, None, MagicMock())
    with pytest.raises(HomeAssistantError, match="needs a Bluetooth"):
        await lamp.next_scene(1)


async def test_lamp_error_is_raised() -> None:
    """Invalid id is reported, not silently retried via cloud."""
    ble = MagicMock(request=AsyncMock(return_value=b"\x84"))
    cloud = MagicMock(scene=AsyncMock())
    lamp = _lamp(MODE_LOCAL_FIRST, ble, cloud)
    with pytest.raises(HomeAssistantError, match="INVALID_ID"):
        await lamp.next_scene(1)
    assert lamp.state.last_status == "INVALID_ID"


async def test_refresh_scenes_walks_list() -> None:
    """Query Scene is repeated with the next id until 0xFF."""

    def scene(next_id: int, name: str) -> bytes:
        return bytes([0, 1, next_id]) + name.encode() + b"\x00"

    ble = MagicMock(
        request=AsyncMock(
            side_effect=[scene(3, "Off"), scene(1, "Lesen"), scene(0xFF, "Kino")]
        )
    )
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    scenes = await lamp.refresh_scenes()
    assert scenes == {0: "Off", 3: "Lesen", 1: "Kino"}
    assert [c.args[0] for c in ble.request.await_args_list] == [
        protocol.query_scene(0),
        protocol.query_scene(3),
        protocol.query_scene(1),
    ]
    assert lamp.scene_options() == {"Default": 0xFF, "Off": 0, "Lesen": 3, "Kino": 1}


def test_scene_options_duplicates() -> None:
    """Duplicate names get their id appended so they stay addressable."""
    lamp = _lamp(MODE_LOCAL_FIRST, _ble(), None)
    lamp.state.scenes = {0: "Off", 1: "Hell", 2: "Hell"}
    assert lamp.scene_options() == {
        "Default": 0xFF,
        "Off": 0,
        "Hell (1)": 1,
        "Hell (2)": 2,
    }


def test_scene_options_cloud_only() -> None:
    """Without names, cloud only offers ids 1..31."""
    lamp = _lamp("cloud_only", None, MagicMock())
    options = lamp.scene_options()
    assert options["Scene 31"] == 31
    assert options["Off"] == 0


async def test_ping_falls_back_to_v1() -> None:
    """Old firmware rejects Ping@V=02 with Invalid Version."""
    ble = MagicMock(request=AsyncMock(side_effect=[b"\x87", b"\x00"]))
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    assert await lamp.ping() == 1


async def test_brightness_step() -> None:
    """Steps send a signed change and add it to the assumed state."""
    ble = _ble()
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    lamp.state_readable = False  # firmware without the state query
    lamp.state.brightness = 30
    await lamp.brightness_step(-1)
    assert ble.request.await_args.args[0] == protocol.relative_brightness(-20)
    assert lamp.state.brightness == 10
    await lamp.brightness_step(-1)
    assert lamp.state.brightness == 0
    await lamp.brightness_step(1)
    assert ble.request.await_args.args[0] == protocol.relative_brightness(20)
    assert lamp.state.brightness == 20


async def test_brightness_step_needs_ble() -> None:
    """Relative steps have no cloud equivalent."""
    lamp = _lamp(MODE_LOCAL_FIRST, None, MagicMock())
    with pytest.raises(HomeAssistantError):
        await lamp.brightness_step(1)


def _state(kelvin: int, brightness: int, on: bool) -> bytes:
    return bytes([0x00, 0x88, 0xF4, 0x18, 0x71]) + bytes(
        [kelvin & 0xFF, kelvin >> 8, brightness, 1 if on else 0]
    )


async def test_command_takes_over_reported_state() -> None:
    """After a BLE command the lamp's own state replaces the assumed one."""
    ble = _ble([_state(2700, 42, True)])
    ble.request_many = AsyncMock(side_effect=lambda frames: [OK] * len(frames))
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    lamp.state.is_on = True
    await lamp.turn_on(scene_id=2)
    ble.request.assert_awaited_once_with(protocol.query_state())
    # The scene's own brightness, not the assumed 100 %.
    assert lamp.state.brightness == 42
    assert lamp.state.kelvin == 2700
    assert lamp.state.scene_id == 2
    assert lamp.state_readable is True


async def test_relative_brightness_uses_reported_value() -> None:
    """Brightness steps show what the lamp made of them, even from unknown."""
    ble = _ble([OK, _state(3000, 55, True)])
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    await lamp.brightness_step(-1)
    assert ble.request.await_args_list[0].args[0] == protocol.relative_brightness(-20)
    assert ble.request.await_args_list[1].args[0] == protocol.query_state()
    assert lamp.state.brightness == 55
    assert lamp.state.is_on is True


async def test_reported_off_keeps_brightness() -> None:
    """Off reads brightness 0 and the Off scene's Kelvin; the last are kept."""
    ble = _ble([OK, _state(2700, 0, False)])
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    lamp.state.is_on = True
    lamp.state.brightness = 72
    lamp.state.kelvin = 3500
    lamp.state.scene_id = 4
    await lamp.turn_off()
    assert lamp.state.is_on is False
    assert lamp.state.brightness == 72
    assert lamp.state.kelvin == 3500
    assert lamp.state.scene_id == 0


async def test_reported_on_after_off_forgets_off_scene() -> None:
    """Switched on elsewhere: on, and the Off scene is no longer claimed."""
    ble = _ble([_state(4000, 30, True)])
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    lamp.state.is_on = False
    lamp.state.scene_id = 0
    assert await lamp.read_state()
    assert lamp.state.is_on is True
    assert lamp.state.brightness == 30
    assert lamp.state.scene_id is None


async def test_unexpected_state_answer_keeps_assumed_state() -> None:
    """Firmware that answers differently is not asked again."""
    ble = _ble([OK, b"\xbc", OK])
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    lamp.state.brightness = 30
    await lamp.brightness_step(1)
    assert lamp.state.brightness == 50
    assert lamp.state_readable is False
    await lamp.brightness_step(1)
    assert ble.request.await_count == 3  # no second state query
    assert lamp.state.brightness == 70


async def test_state_query_timeout() -> None:
    """No answer at first: not asked again. After a good answer: asked again."""
    ble = _ble([OK, TimeoutError()])
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    await lamp.brightness_step(1)
    assert lamp.state_readable is False

    ble = _ble([_state(4000, 30, True), TimeoutError(), _state(4000, 40, True)])
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    assert await lamp.read_state()
    assert not await lamp.read_state()
    assert lamp.state_readable is True
    assert await lamp.read_state()
    assert lamp.state.brightness == 40


async def test_state_query_ble_error_and_failed_command() -> None:
    """A dropped link skips the query; a failed command does not query."""
    ble = _ble([OK, BleakError("gone")])
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    await lamp.brightness_step(1)
    assert lamp.state_readable is None

    ble = _ble(BleakError("gone"))
    lamp = _lamp(MODE_LOCAL_ONLY, ble, None)
    with pytest.raises(HomeAssistantError):
        await lamp.brightness_step(1)
    assert ble.request.await_count == 1


async def test_cloud_command_does_not_query_state() -> None:
    """The state query needs Bluetooth; cloud commands keep assumed values."""
    cloud = MagicMock()
    cloud.scene = AsyncMock()
    lamp = _lamp(MODE_CLOUD_FIRST, _ble(), cloud)
    lamp.state.is_on = True
    await lamp.turn_on(scene_id=2)
    assert lamp.state.last_route == ROUTE_CLOUD
    lamp.ble.request.assert_not_awaited()
    assert lamp.state.brightness == 100


def test_scene_reported_updates_assumed_state() -> None:
    """A scene reported by the lamp replaces the assumed scene and on/off."""
    lamp = _lamp(MODE_LOCAL_FIRST, _ble(), None)
    lamp.state.scenes = {0: "Off", 1: "Indirect", 2: "Evening"}
    lamp.state.brightness = 40
    listener = MagicMock()
    lamp.add_listener(listener)

    lamp.scene_reported(2)
    assert lamp.state.scene_id == 2
    assert lamp.state.is_on
    assert lamp.scene_name(lamp.state.scene_id) == "Evening"
    assert lamp.state.brightness == 40
    listener.assert_called_once()

    lamp.scene_reported(2)
    listener.assert_called_once()

    lamp.scene_reported(0)
    assert lamp.state.is_on is False
    assert listener.call_count == 2


async def test_scene_report_during_command_wins() -> None:
    """The lamp notifies the new scene before it answers the command."""
    ble = _ble()
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    lamp.state.is_on = True

    async def next_scene_then_answer(_frame: bytes) -> bytes:
        lamp.scene_reported(4)
        return OK

    ble.request = AsyncMock(side_effect=next_scene_then_answer)
    await lamp.next_scene(1)
    assert lamp.state.scene_id == 4

    async def default_then_answer(frames: list[bytes]) -> list[bytes]:
        lamp.scene_reported(1)
        return [OK] * len(frames)

    lamp.state.is_on = False
    ble.request_many = AsyncMock(side_effect=default_then_answer)
    await lamp.turn_on()
    assert lamp.state.scene_id == 1
    assert lamp.state.brightness == 100


async def test_next_scene_without_report_is_unknown() -> None:
    """Without a scene report the scene after a step stays unknown."""
    lamp = _lamp(MODE_LOCAL_FIRST, _ble(), None)
    lamp.state.scene_id = 2
    await lamp.next_scene(1)
    assert lamp.state.scene_id is None


class FakePower:
    """Power switch stand-in that records calls."""

    def __init__(self, on: bool | None) -> None:
        self.is_on = on
        self.calls: list[str] = []

    async def turn_on(self) -> None:
        self.calls.append("on")
        self.is_on = True

    async def turn_off(self) -> None:
        self.calls.append("off")
        self.is_on = False


def _powered_lamp(ble: MagicMock, on: bool | None) -> tuple[LukeRobertsLamp, FakePower]:
    lamp = _lamp(MODE_LOCAL_FIRST, ble, None)
    power = FakePower(on)
    lamp.power = power
    return lamp, power


async def test_power_turn_off_cuts_power() -> None:
    """With a power switch, off cuts the power; no Bluetooth needed."""
    ble = _ble()
    lamp, power = _powered_lamp(ble, True)
    lamp.state.is_on = True
    lamp.state.scene_id = 4
    await lamp.turn_off()
    assert power.calls == ["off"]
    ble.request.assert_not_awaited()
    assert lamp.is_on is False
    assert lamp.state.scene_id == 4  # left as is; read again at power-up


async def test_power_turn_on_plain() -> None:
    """Plain on only switches the power; the lamp starts its power-up scene."""
    ble = _ble()
    lamp, power = _powered_lamp(ble, False)
    lamp.state.is_on = True
    await lamp.turn_on()
    assert power.calls == ["on"]
    ble.request.assert_not_awaited()
    ble.request_many.assert_not_awaited()
    assert lamp.is_on is True


async def test_power_turn_on_with_brightness() -> None:
    """From no power: power on, wait for the lamp, then adjust over BLE."""
    ble = _ble([b"\x00\x08", _state(2700, 72, True), _state(2700, 40, True)])
    lamp, power = _powered_lamp(ble, False)
    await lamp.turn_on(brightness=40)
    assert power.calls == ["on"]
    assert ble.request.await_args_list[0].args[0] == protocol.ping()
    assert ble.request.await_args_list[1].args[0] == protocol.query_state()
    # The lamp reported on, so no Default scene is selected over its own.
    ble.request_many.assert_awaited_once_with([protocol.brightness(40)])
    assert lamp.state.brightness == 40
    assert lamp.is_on is True


async def test_power_turn_on_lamp_soft_off() -> None:
    """Powered but in its Off scene: Default is selected as without a switch."""
    ble = _ble([b"\x00\x08", _state(2700, 0, False), _state(2700, 50, True)])
    lamp, _power = _powered_lamp(ble, False)
    await lamp.turn_on(brightness=50)
    ble.request_many.assert_awaited_once_with(
        [protocol.select_scene(0xFF), protocol.brightness(50)]
    )


async def test_power_turn_on_lamp_does_not_come_back() -> None:
    """A lamp that stays unreachable after power-up gives a clear error."""
    ble = _ble(BleakError("not found"))
    lamp, _power = _powered_lamp(ble, False)
    lamp.ble_present = False
    with pytest.raises(HomeAssistantError) as err:
        await lamp.wait_until_reachable(timeout=0.01)
    assert err.value.translation_key == "no_answer_after_power_on"


async def test_power_state_and_availability() -> None:
    """On/off follows the power switch first; no power is not unavailable."""
    lamp, power = _powered_lamp(_ble(), False)
    lamp.ble_present = False
    lamp.state.is_on = True
    assert lamp.is_on is False
    assert lamp.available
    power.is_on = True
    assert lamp.is_on is True
    lamp.state.is_on = False  # Off scene chosen in the app
    assert lamp.is_on is False
    power.is_on = None  # power entity unavailable
    assert lamp.is_on is False
    assert not lamp.available


async def test_power_cut_since_start() -> None:
    """Not seen since Home Assistant started (power cut): still available.

    Otherwise the light could not be switched on from Home Assistant.
    """
    lamp, power = _powered_lamp(_ble(), False)
    lamp.ble = None
    lamp.ble_present = False
    assert not lamp.has_local
    assert lamp.available
    await lamp.turn_on()
    assert power.calls == ["on"]
    lamp.mode = MODE_CLOUD_ONLY
    assert not lamp.available


async def test_power_off_blocks_bluetooth_only_actions() -> None:
    """Brighter/dimmer without power fails fast instead of timing out."""
    ble = _ble()
    lamp, _power = _powered_lamp(ble, False)
    with pytest.raises(HomeAssistantError) as err:
        await lamp.brightness_step(1)
    assert err.value.translation_key == "no_power"
    ble.request.assert_not_awaited()


async def test_power_restored_reads_state() -> None:
    """After the wall switch powers the lamp, its state is read again."""
    ble = _ble([b"\x00\x08", _state(3999, 49, True)])
    lamp, _power = _powered_lamp(ble, True)
    listener = MagicMock()
    lamp.add_listener(listener)
    await lamp.power_restored()
    assert lamp.state.brightness == 49
    listener.assert_called()


async def test_power_restored_off_scene_selects_default() -> None:
    """Turned on by HA but the lamp stays in Off: Default is selected."""
    ble = _ble([b"\x00\x08", _state(2700, 0, False), b"\x00", _state(2700, 72, True)])
    lamp, _power = _powered_lamp(ble, False)
    await lamp.turn_on()
    await lamp.power_restored()
    assert ble.request.await_args_list[2].args[0] == protocol.select_scene(0xFF)
    assert lamp.state.is_on is True
    assert lamp.state.brightness == 72


async def test_power_restored_lamp_unreachable() -> None:
    """No Bluetooth after power-up: nothing breaks, the state stays."""
    ble = _ble(BleakError("not found"))
    lamp, _power = _powered_lamp(ble, True)
    lamp.ble_present = False
    with patch("custom_components.luke_roberts.lamp.POWER_UP_TIMEOUT", 0.01):
        await lamp.power_restored()
    assert lamp.state.is_on is None


async def test_power_restored_by_wall_switch_keeps_off_scene() -> None:
    """Powered by someone else: the lamp's own choice is shown, not changed."""
    ble = _ble([b"\x00\x08", _state(2700, 0, False)])
    lamp, _power = _powered_lamp(ble, True)
    await lamp.power_restored()
    assert ble.request.await_count == 2  # ping and state query, no scene
    assert lamp.is_on is False


async def test_wait_until_reachable_retries_ping() -> None:
    """The lamp is still booting at first; the next ping gets through."""
    ble = _ble([BleakError("booting"), b"\x00\x08"])
    lamp, _power = _powered_lamp(ble, True)
    await lamp.wait_until_reachable(timeout=5)
    assert ble.request.await_count == 2


async def test_wait_until_reachable_without_bluetooth() -> None:
    """Cloud-only lamps have nothing to wait for."""
    lamp = _lamp(MODE_CLOUD_FIRST, None, MagicMock())
    await lamp.wait_until_reachable(timeout=0.01)
