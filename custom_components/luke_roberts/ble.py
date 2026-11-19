"""Bluetooth LE transport for a Luke Roberts lamp.

Connections go through Home Assistant's Bluetooth stack, so any local adapter
or Bluetooth proxy (ESPHome, Shelly, ...) that can reach the lamp is used
automatically. ``bleak_retry_connector`` picks the best path.

The lamp answers every write on the API characteristic with an indication.
Requests are serialised with a lock so each response is matched to exactly
one command.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
import time

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    establish_connection,
)

from . import protocol
from .const import (
    API_CHAR_UUID,
    DIS_FIRMWARE_UUID,
    DIS_HARDWARE_UUID,
    DIS_MANUFACTURER_UUID,
    DIS_MODEL_UUID,
    DIS_SERIAL_UUID,
    DIS_SOFTWARE_UUID,
    KEEPALIVE_INTERVAL,
    LIVE_MODEL_CHAR_UUID,
    RESPONSE_TIMEOUT,
    SCENE_CHAR_UUID,
    SCENE_RECORD_CHAR_UUID,
    SETTINGS_CHAR_UUID,
    SYNC_IDLE_TIMEOUT,
    SYNC_TIMEOUT,
)

_LOGGER = logging.getLogger(__name__)

BLE_ERRORS = (BleakError, TimeoutError, asyncio.TimeoutError, EOFError)

_DIS_FIELDS = {
    "manufacturer": DIS_MANUFACTURER_UUID,
    "model": DIS_MODEL_UUID,
    "serial_number": DIS_SERIAL_UUID,
    "firmware": DIS_FIRMWARE_UUID,
    "hardware": DIS_HARDWARE_UUID,
    "software": DIS_SOFTWARE_UUID,
}


class LukeRobertsBle:
    """Request/response client for the External API characteristic."""

    def __init__(
        self,
        ble_device: BLEDevice,
        name: str,
        linger: float,
    ) -> None:
        self._ble_device = ble_device
        self._name = name
        self._linger = linger
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._connect_lock = asyncio.Lock()
        self._response: asyncio.Future[bytes] | None = None
        self._last_command = 0.0
        self._keepalive_task: asyncio.Task[None] | None = None
        self._connection_listeners: list[Callable[[bool], None]] = []
        self._scene_listeners: list[Callable[[int], None]] = []

    @property
    def connected(self) -> bool:
        """Return True while a GATT link is up."""
        return self._client is not None and self._client.is_connected

    def set_ble_device(self, ble_device: BLEDevice) -> None:
        """Update the device handle, e.g. when a closer proxy sees the lamp."""
        self._ble_device = ble_device

    def add_connection_listener(
        self, listener: Callable[[bool], None]
    ) -> Callable[[], None]:
        """Register a callback for connect/disconnect events."""
        self._connection_listeners.append(listener)
        return lambda: self._connection_listeners.remove(listener)

    def _notify_connection(self, connected: bool) -> None:
        for listener in list(self._connection_listeners):
            listener(connected)

    def add_scene_listener(self, listener: Callable[[int], None]) -> Callable[[], None]:
        """Register a callback for the active scene id reported by the lamp."""
        self._scene_listeners.append(listener)
        return lambda: self._scene_listeners.remove(listener)

    def _report_scene(self, data: bytes | bytearray) -> None:
        if not data:
            return
        scene_id = data[0]
        _LOGGER.debug("%s: active scene %d", self._name, scene_id)
        for listener in list(self._scene_listeners):
            listener(scene_id)

    def _on_scene_notification(self, _sender: object, data: bytearray) -> None:
        self._report_scene(data)

    async def _watch_scene(self, client: BleakClientWithServiceCache) -> None:
        """Read the active scene and follow changes while the link is up.

        Firmware without the scene characteristic simply keeps the assumed
        state, so every failure here is logged and ignored.
        """
        try:
            await client.start_notify(SCENE_CHAR_UUID, self._on_scene_notification)
            self._report_scene(await client.read_gatt_char(SCENE_CHAR_UUID))
        except Exception as err:  # noqa: BLE001 - optional characteristic
            _LOGGER.debug("%s: active scene not readable: %r", self._name, err)

    # --- Connection handling --------------------------------------------------

    async def _ensure_connected(self) -> BleakClientWithServiceCache:
        if self.connected:
            assert self._client is not None
            return self._client
        async with self._connect_lock:
            if self.connected:
                assert self._client is not None
                return self._client
            _LOGGER.debug("%s: connecting via %s", self._name, self._ble_device)
            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,
                self._name,
                disconnected_callback=self._on_disconnected,
                ble_device_callback=lambda: self._ble_device,
                max_attempts=3,
            )
            try:
                await client.start_notify(API_CHAR_UUID, self._on_notification)
            except BLE_ERRORS:
                await client.disconnect()
                raise
            self._client = client
            _LOGGER.debug("%s: connected", self._name)
            await self._watch_scene(client)
            self._notify_connection(True)
            return client

    def _on_disconnected(self, client: BleakClientWithServiceCache) -> None:
        if self._client is not None and client is not self._client:
            return  # late callback from a previous link
        _LOGGER.debug("%s: disconnected", self._name)
        self._client = None
        if self._response is not None and not self._response.done():
            self._response.set_exception(BleakError("Disconnected"))
        self._notify_connection(False)

    def _on_notification(self, _sender: object, data: bytearray) -> None:
        _LOGGER.debug("%s: <- %s", self._name, bytes(data).hex(" "))
        if self._response is not None and not self._response.done():
            self._response.set_result(bytes(data))

    async def disconnect(self) -> None:
        """Drop the link and stop the keepalive."""
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        client = self._client
        self._client = None
        if client is not None and client.is_connected:
            with contextlib.suppress(*BLE_ERRORS):
                await client.disconnect()

    # --- Requests ---------------------------------------------------------------

    async def request(self, frame: bytes) -> bytes:
        """Write one command and return the lamp's response."""
        async with self._lock:
            response = await self._request_locked(frame)
        self._last_command = time.monotonic()
        self._schedule_keepalive()
        return response

    async def request_many(self, frames: list[bytes]) -> list[bytes]:
        """Send several commands back to back over one connection."""
        responses: list[bytes] = []
        async with self._lock:
            for frame in frames:
                responses.append(await self._request_locked(frame))
        self._last_command = time.monotonic()
        self._schedule_keepalive()
        return responses

    async def _request_locked(
        self,
        frame: bytes,
        char_uuid: str = API_CHAR_UUID,
        timeout: float = RESPONSE_TIMEOUT,
    ) -> bytes:
        client = await self._ensure_connected()
        loop = asyncio.get_running_loop()
        self._response = loop.create_future()
        _LOGGER.debug("%s: -> %s %s", self._name, char_uuid[4:8], frame.hex(" "))
        try:
            await client.write_gatt_char(char_uuid, frame, response=True)
            async with asyncio.timeout(timeout):
                return await self._response
        finally:
            self._response = None

    async def sync_scenes(
        self, revision: int, database: bytes, timeout: float = SYNC_TIMEOUT
    ) -> tuple[protocol.SyncDone | None, bytes]:
        """Send one Sync and return the final answer and the raw record stream.

        The lamp sends the settings record on its own characteristic, then
        the scene records changed after ``revision`` split into notifications
        on the scene record characteristic, and finally the answer on the API
        characteristic. The answer is None if it did not arrive in time. The
        stream is returned as received: a Bluetooth proxy may drop
        notifications, so the caller checks it.

        The settings notifications are always subscribed: without them the
        lamp answers a Sync with nothing to send with status 0x82.
        """
        stream = bytearray()
        last_chunk = time.monotonic()

        def on_record(_sender: object, data: bytearray) -> None:
            nonlocal last_chunk
            last_chunk = time.monotonic()
            stream.extend(data)

        def on_settings(_sender: object, data: bytearray) -> None:
            pass  # the settings record is not used (yet)

        done: protocol.SyncDone | None = None
        async with self._lock:
            client = await self._ensure_connected()
            await client.start_notify(SETTINGS_CHAR_UUID, on_settings)
            await client.start_notify(SCENE_RECORD_CHAR_UUID, on_record)
            frame = protocol.sync(revision, database)
            self._response = asyncio.get_running_loop().create_future()
            _LOGGER.debug("%s: -> 2842 %s", self._name, frame.hex(" "))
            try:
                await client.write_gatt_char(API_CHAR_UUID, frame, response=True)
                deadline = time.monotonic() + timeout
                response: bytes | None = None
                while response is None:
                    try:
                        async with asyncio.timeout(0.25):
                            response = await asyncio.shield(self._response)
                    except TimeoutError:
                        now = time.monotonic()
                        # A proxy may lose the final answer too: give up when
                        # the records stopped coming, not only at the deadline.
                        if now > deadline or (
                            stream and now - last_chunk > SYNC_IDLE_TIMEOUT
                        ):
                            _LOGGER.debug("%s: no answer to Sync", self._name)
                            break
                if response is not None:
                    done = protocol.parse_sync_done(response)
            finally:
                self._response = None
                for uuid in (SCENE_RECORD_CHAR_UUID, SETTINGS_CHAR_UUID):
                    with contextlib.suppress(Exception):
                        await client.stop_notify(uuid)
        self._last_command = time.monotonic()
        self._schedule_keepalive()
        _LOGGER.debug(
            "%s: Sync from 0x%X: %d bytes, %s",
            self._name,
            revision,
            len(stream),
            done,
        )
        return done, bytes(stream)

    async def write_scene_record(self, record: bytes) -> protocol.SceneWrite:
        """Write one scene record; the lamp answers on the API characteristic."""
        async with self._lock:
            response = await self._request_locked(record, SCENE_RECORD_CHAR_UUID)
        self._last_command = time.monotonic()
        self._schedule_keepalive()
        return protocol.parse_scene_write(response)

    async def write_live_model(self, frame: bytes) -> None:
        """Show light on the lamp without saving it (write without response)."""
        async with self._lock:
            client = await self._ensure_connected()
            _LOGGER.debug("%s: -> 2845 %s", self._name, frame.hex(" "))
            await client.write_gatt_char(LIVE_MODEL_CHAR_UUID, frame, response=False)
        self._last_command = time.monotonic()
        self._schedule_keepalive()

    async def read_device_info(self) -> dict[str, str]:
        """Read the Device Information Service, skipping missing fields."""
        info: dict[str, str] = {}
        async with self._lock:
            client = await self._ensure_connected()
            for key, uuid in _DIS_FIELDS.items():
                try:
                    raw = await client.read_gatt_char(uuid)
                except BLE_ERRORS:
                    continue
                except Exception:  # noqa: BLE001 - missing characteristic
                    continue
                value = bytes(raw).split(b"\x00", 1)[0].decode(errors="replace")
                if value.strip():
                    info[key] = value.strip()
        self._last_command = time.monotonic()
        self._schedule_keepalive()
        return info

    # --- Keepalive --------------------------------------------------------------

    def _schedule_keepalive(self) -> None:
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.get_running_loop().create_task(
                self._keepalive()
            )

    async def _keepalive(self) -> None:
        """Ping while the user is active, then free the lamp for other centrals."""
        try:
            while self.connected:
                remaining = self._linger - (time.monotonic() - self._last_command)
                if remaining <= 0:
                    break
                await asyncio.sleep(min(KEEPALIVE_INTERVAL, remaining))
                if time.monotonic() - self._last_command >= self._linger:
                    break
                if self._lock.locked() or not self.connected:
                    continue
                async with self._lock:
                    with contextlib.suppress(*BLE_ERRORS, protocol.LampError):
                        await self._request_locked(protocol.ping())
        finally:
            self._keepalive_task = None
        async with self._lock:
            if time.monotonic() - self._last_command >= self._linger:
                _LOGGER.debug("%s: idle, releasing link", self._name)
                client = self._client
                self._client = None
                if client is not None and client.is_connected:
                    with contextlib.suppress(*BLE_ERRORS):
                        await client.disconnect()
