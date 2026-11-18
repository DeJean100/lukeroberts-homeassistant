"""Shared helpers for Luke Roberts tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import time

from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak

from custom_components.luke_roberts.const import (
    API_CHAR_UUID,
    SCENE_CHAR_UUID,
    SCENE_RECORD_CHAR_UUID,
    SERVICE_UUID,
    SETTINGS_CHAR_UUID,
)

ADDRESS = "AA:BB:CC:DD:EE:FF"


def static_record(
    scene_id: int, name: str, sort_index: int = 0, fill: int = 0x40
) -> bytes:
    """A 178-byte static scene record as the lamp stores it."""
    header = bytes([0x01, scene_id, sort_index, 0x01, 0, 0, 0, 0])
    body = (2700).to_bytes(2, "little") + bytes([0xA2, 0x70, 0x1F, fill]) * 16
    body += bytes([fill]) * 84
    return header + body + name.encode().ljust(20, b"\x00")


def default_records() -> dict[int, bytes]:
    """Scene slots of the fake lamp: Off, two static scenes, Demo, one free."""
    return {
        0: bytes([0x01, 0x00, 0x00, 0x03, 0, 0, 0, 0]) + b"Off".ljust(20, b"\x00"),
        1: static_record(1, "Lesen", 10),
        2: static_record(2, "Gemütlich", 20),
        3: bytes([0x01, 0x03, 0xFF, 0x02, 0, 0, 0, 0])
        + (2700).to_bytes(2, "little")
        + bytes(78)
        + b"Demo".ljust(20, b"\x00"),
        4: bytes([0x01, 0x04, 0x00, 0x00, 0, 0, 0, 0]),
    }


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
        # Scene records (Sync / scene writes) and the lamp's revision.
        self.records: dict[int, bytes] = default_records()
        self.revision = 0x025E00
        self.record_writes: list[bytes] = []
        # Rewrite what a scene write stores, to test the read-back check.
        self.mangle_record: Callable[[bytes], bytes] | None = None
        # Announce more records than are sent, to test the count check.
        self.sync_count_offset = 0
        self.notify_callbacks: dict[str, Callable[[object, bytearray], None]] = {}

    async def start_notify(self, uuid: str, callback) -> None:
        if uuid == SCENE_CHAR_UUID:
            if self.active_scene is None:
                raise BleakError("Characteristic not found")
            self.scene_callback = callback
            return
        if uuid in (SETTINGS_CHAR_UUID, SCENE_RECORD_CHAR_UUID):
            self.notify_callbacks[uuid] = callback
            return
        self._callback = callback

    async def stop_notify(self, uuid: str) -> None:
        self.notify_callbacks.pop(uuid, None)

    def _sync(self) -> None:
        """Answer a Sync like the lamp: settings, records in 12-byte chunks, done."""
        loop = asyncio.get_running_loop()
        settings = self.notify_callbacks.get(SETTINGS_CHAR_UUID)
        if settings is not None:
            loop.call_soon(settings, None, bytearray(b"\x02\x01\x01\x01\x01Lamp"))
        stream = b"".join(self.records[i] for i in sorted(self.records))
        records = self.notify_callbacks.get(SCENE_RECORD_CHAR_UUID)
        if records is not None:
            for i in range(0, len(stream), 12):
                loop.call_soon(records, None, bytearray(stream[i : i + 12]))
        done = (
            b"\x00"
            + self.revision.to_bytes(4, "little")
            + bytes(
                [
                    0x01,
                    len(self.records) + self.sync_count_offset,
                    self.active_scene or 0,
                    0x9C,
                    0x00,
                ]
            )
            + b"\xd2\xb2\x21\xd0\x00\x00"
        )
        assert self._callback is not None
        loop.call_soon(self._callback, None, bytearray(done))

    def _write_record(self, record: bytes) -> bytes:
        self.record_writes.append(record)
        self.revision += 1
        scene_id = record[1]
        if self.mangle_record is not None:
            record = self.mangle_record(record)
        if record[3] == 0x00:
            self.records[scene_id] = record[:8]
            self.scenes.pop(scene_id, None)
        else:
            self.records[scene_id] = record
            self.scenes[scene_id] = record[-20:].split(b"\x00", 1)[0].decode()
        return b"\x00" + self.revision.to_bytes(4, "little") + record[1:3]

    def push_scene(self, scene_id: int) -> None:
        """Simulate the lamp switching scene, e.g. from the app."""
        self.active_scene = scene_id
        assert self.scene_callback is not None
        self.scene_callback(None, bytearray([scene_id]))

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool) -> None:
        if uuid == SCENE_RECORD_CHAR_UUID:
            reply = self._write_record(bytes(data))
        else:
            assert uuid == API_CHAR_UUID
            self.written.append(bytes(data))
            if data[0] == 0x07:
                self._sync()
                return
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
