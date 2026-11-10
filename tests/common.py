"""Shared helpers for Luke Roberts tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

from custom_components.luke_roberts.const import SCENE_CHAR_UUID, SERVICE_UUID

ADDRESS = "AA:BB:CC:DD:EE:FF"


def service_info(
    address: str = ADDRESS, name: str = "Luke Roberts Model F"
) -> BluetoothServiceInfoBleak:
    """Build an advertisement as seen through an adapter or proxy."""
    device = BLEDevice(address, name, {})
    adv = AdvertisementData(
        local_name=name,
        manufacturer_data={},
        service_data={},
        service_uuids=[SERVICE_UUID],
        tx_power=-127,
        rssi=-60,
        platform_data=(),
    )
    return BluetoothServiceInfoBleak(
        name=name,
        address=address,
        rssi=-60,
        manufacturer_data={},
        service_data={},
        service_uuids=[SERVICE_UUID],
        source="esphome-proxy",
        device=device,
        advertisement=adv,
        connectable=True,
        time=time.monotonic(),
        tx_power=-127,
    )


class FakeLampClient:
    """Minimal BleakClient stand-in that answers like a lamp."""

    SCENES = {0: (1, "Off"), 1: (2, "Lesen"), 2: (0xFF, "Gemütlich")}

    def __init__(self) -> None:
        self.is_connected = True
        self.written: list[bytes] = []
        self.scenes: dict[int, str] = {
            scene_id: name for scene_id, (_next, name) in self.SCENES.items()
        }
        self.software = b"1.3.2\x00"
        # Light state as reported by the undocumented state query ``09``.
        self.is_on = True
        self.brightness = 49
        self.kelvin = 3999

        # Active scene id; None mimics firmware without the scene characteristic.
        self.active_scene: int | None = 1
        self._callback: Callable[[object, bytearray], None] | None = None
        self.scene_callback: Callable[[object, bytearray], None] | None = None

    async def start_notify(self, uuid: str, callback) -> None:
        if uuid == SCENE_CHAR_UUID:
            if self.active_scene is None:
                raise BleakError("Characteristic not found")
            self.scene_callback = callback
            return
        self._callback = callback

    def push_scene(self, scene_id: int) -> None:
        """Simulate the lamp switching scene, e.g. from the app."""
        self.active_scene = scene_id
        assert self.scene_callback is not None
        self.scene_callback(None, bytearray([scene_id]))

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        self.written.append(bytes(data))
        reply = self.respond(bytes(data))
        assert self._callback is not None
        asyncio.get_running_loop().call_soon(self._callback, None, bytearray(reply))

    def respond(self, frame: bytes) -> bytes:
        if frame == b"\x09":
            return bytes([0x00, 0x88, 0xF4, 0x18, 0x71]) + bytes(
                [
                    self.kelvin & 0xFF,
                    self.kelvin >> 8,
                    self.brightness if self.is_on else 0,
                    1 if self.is_on else 0,
                ]
            )
        opcode = frame[2]
        if opcode == 0x03:
            self.brightness = frame[3]
        elif opcode == 0x04:
            self.kelvin = int.from_bytes(frame[3:5], "big")
        elif opcode == 0x05:
            self.is_on = frame[3] != 0x00
            if self.is_on:
                self.brightness = 42  # a scene brings its own brightness
        elif opcode == 0x08:
            delta = int.from_bytes(frame[3:4], "big", signed=True)
            self.brightness = max(0, min(100, self.brightness + delta))
        if opcode == 0x00:
            return bytes([0x00, 0x02])
        if opcode == 0x01:
            ids = sorted(self.scenes)
            index = ids.index(frame[3])
            next_id = ids[index + 1] if index + 1 < len(ids) else 0xFF
            return (
                bytes([0x00, 0x01, next_id]) + self.scenes[frame[3]].encode() + b"\x00"
            )
        return b"\x00"

    async def read_gatt_char(self, uuid: str) -> bytes:
        if uuid == SCENE_CHAR_UUID:
            if self.active_scene is None:
                raise BleakError("Characteristic not found")
            return bytes([self.active_scene])
        return {
            "00002a24-0000-1000-8000-00805f9b34fb": b"Model F\x00",
            "00002a26-0000-1000-8000-00805f9b34fb": b"21.3",
            "00002a28-0000-1000-8000-00805f9b34fb": self.software,
        }.get(uuid, b"")

    async def disconnect(self) -> None:
        self.is_connected = False
