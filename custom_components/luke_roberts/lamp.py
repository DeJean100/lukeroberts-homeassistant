"""One Luke Roberts lamp, reachable locally (BLE) and/or through the cloud.

This class behaves like the Luke Roberts app: it talks to the lamp directly
over Bluetooth when it is in range and can fall back to the cloud. The state
kept here is what was last sent (assumed state). After every Bluetooth command
it is corrected with the brightness, colour temperature and on/off the lamp
reports (undocumented command ``09``), and it can be refreshed from the cloud.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
import logging
import time
from typing import Any, Protocol

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from . import protocol
from .ble import BLE_ERRORS, LukeRobertsBle
from .cloud import CloudAuthError, CloudError, CloudState, LukeRobertsCloud
from .const import (
    CLOUD_MAX_SCENE_ID,
    DOMAIN,
    MODE_CLOUD_FIRST,
    MODE_CLOUD_ONLY,
    MODE_LOCAL_ONLY,
    POWER_UP_TIMEOUT,
    RELATIVE_BRIGHTNESS_STEP,
    SCENE_DEFAULT,
    SCENE_OFF,
)
from .scene_backup import SceneBackup, SceneBackupStore, SceneCache

_LOGGER = logging.getLogger(__name__)

ROUTE_BLUETOOTH = "bluetooth"
ROUTE_CLOUD = "cloud"
RESTORE_REASON = "before restoring scene"
# Reading scene records through a Bluetooth proxy that drops notifications.
SYNC_CHANGES_ATTEMPTS = 10
SYNC_CHANGES_TIMEOUT = 5.0
SYNC_ALL_ATTEMPTS = 12


class PowerSwitch(Protocol):
    """Something that switches the lamp's mains power (KNX, relay, plug)."""

    @property
    def is_on(self) -> bool | None:
        """True while powered, False while cut, None if unknown."""

    async def turn_on(self) -> None:
        """Switch the power on."""

    async def turn_off(self) -> None:
        """Switch the power off."""


@dataclass
class LampState:
    """Assumed lamp state."""

    is_on: bool | None = None
    brightness: int | None = None  # percent 0..100
    kelvin: int | None = None
    scene_id: int | None = None
    scenes: dict[int, str] = field(default_factory=dict)
    api_version: int | None = None
    device_info: dict[str, str] = field(default_factory=dict)
    last_route: str | None = None
    last_status: str | None = None


class LukeRobertsLamp:
    """Routes commands to BLE or cloud and keeps the assumed state."""

    def __init__(
        self,
        name: str,
        mode: str,
        ble: LukeRobertsBle | None,
        cloud: LukeRobertsCloud | None,
        cloud_lamp_id: int | None,
    ) -> None:
        self.name = name
        self.mode = mode
        self.ble = ble
        self.cloud = cloud
        self.cloud_lamp_id = cloud_lamp_id
        self.state = LampState()
        self.ble_present = ble is not None
        # Set up with a Bluetooth address. ``ble`` stays None until the lamp
        # is first seen, e.g. after a Home Assistant start with its power cut.
        self.address_configured = ble is not None
        # Device information read over Bluetooth (monotonic time), None = never.
        self.details_read_at: float | None = None
        self.details_reading = False
        # Whether the lamp answers the state query (``09``) as expected:
        # None = not asked yet, False = it does not, so it is not asked again.
        self.state_readable: bool | None = None
        # Optional power switch in front of the lamp, used for on/off.
        self.power: PowerSwitch | None = None
        # When this integration switched the power on to turn the light on
        # (monotonic time), so power_restored knows "on" was asked for.
        self._power_on_requested: float | None = None
        # Backups of the scene records, kept before every scene write.
        self.scene_backups: SceneBackupStore | None = None
        # One scene read or write at a time.
        self._scene_lock = asyncio.Lock()
        # Scene records as last read (from the cache), for previews.
        self.scene_records: dict[int, protocol.SceneRecord] = {}
        self._listeners: list[Callable[[], None]] = []
        # Counts scene reports from the lamp, to tell whether one arrived while
        # a command ran (the lamp notifies before it answers the command).
        self._scene_reports = 0

    # --- Listener plumbing ----------------------------------------------------

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register an entity update callback."""
        self._listeners.append(listener)
        return lambda: self._listeners.remove(listener)

    def notify(self) -> None:
        """Tell all entities to write their state."""
        for listener in list(self._listeners):
            listener()

    # --- Capabilities ---------------------------------------------------------

    @property
    def has_local(self) -> bool:
        """BLE is configured and allowed by the connection mode."""
        return self.ble is not None and self.mode != MODE_CLOUD_ONLY

    @property
    def local_configured(self) -> bool:
        """Bluetooth is set up and allowed, even if the lamp was not seen yet."""
        return self.address_configured and self.mode != MODE_CLOUD_ONLY

    @property
    def has_cloud(self) -> bool:
        """Cloud is configured and allowed by the connection mode."""
        return (
            self.cloud is not None
            and self.cloud_lamp_id is not None
            and self.mode != MODE_LOCAL_ONLY
        )

    @property
    def available(self) -> bool:
        """At least one route can be tried.

        With a power switch the lamp is also available while its power is
        cut and it does not advertise, so it can be switched on.
        """
        if (
            self.power is not None
            and self.local_configured
            and self.power.is_on is not None
        ):
            return True
        return (self.has_local and self.ble_present) or self.has_cloud

    @property
    def powered(self) -> bool:
        """False only when a power switch is known to be off."""
        return self.power is None or self.power.is_on is not False

    @property
    def is_on(self) -> bool | None:
        """On/off as shown: the power switch first, then the lamp itself."""
        if self.power is not None:
            power = self.power.is_on
            if power is False:
                return False
            if power is True:
                # Powered: on unless the lamp itself is in its Off scene.
                return self.state.is_on is not False
        return self.state.is_on

    def _require_power(self) -> None:
        if not self.powered:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_power",
                translation_placeholders={"name": self.name},
            )

    def scene_options(self) -> dict[str, int]:
        """Map display names to scene ids for selects and light effects."""
        options: dict[str, int] = {"Default": SCENE_DEFAULT}
        if self.state.scenes:
            names = list(self.state.scenes.values())
            for scene_id, name in self.state.scenes.items():
                label = name or f"Scene {scene_id}"
                if names.count(name) > 1 or label in options:
                    label = f"{label} ({scene_id})"
                options[label] = scene_id
        else:
            options["Off"] = SCENE_OFF
            upper = CLOUD_MAX_SCENE_ID if not self.has_local else 15
            for scene_id in range(1, upper + 1):
                options[f"Scene {scene_id}"] = scene_id
        return options

    def scene_name(self, scene_id: int | None) -> str | None:
        """Return the display name of a scene id."""
        if scene_id is None:
            return None
        for label, sid in self.scene_options().items():
            if sid == scene_id:
                return label
        return None

    # --- Routing ----------------------------------------------------------------

    async def _run(
        self,
        local: Callable[[LukeRobertsBle], Awaitable[Any]] | None,
        cloud: Callable[[LukeRobertsCloud, int], Awaitable[Any]] | None,
    ) -> Any:
        """Execute via the preferred route, falling back if allowed."""
        routes: list[str] = []
        if self.mode in (MODE_CLOUD_FIRST, MODE_CLOUD_ONLY):
            routes = [ROUTE_CLOUD, ROUTE_BLUETOOTH]
        else:
            routes = [ROUTE_BLUETOOTH, ROUTE_CLOUD]

        errors: list[str] = []
        for route in routes:
            if route == ROUTE_BLUETOOTH:
                if local is None or not self.has_local:
                    continue
                assert self.ble is not None
                try:
                    result = await local(self.ble)
                except protocol.LampForbiddenError as err:
                    errors.append(
                        "the lamp has a security code set in the Luke Roberts "
                        "app and rejects local commands"
                    )
                    self.state.last_status = protocol.status_name(err.status)
                    continue
                except protocol.LampError as err:
                    self.state.last_status = protocol.status_name(err.status)
                    raise HomeAssistantError(
                        f"{self.name}: lamp rejected command ({err})"
                    ) from err
                except BLE_ERRORS as err:
                    errors.append(f"bluetooth: {err or type(err).__name__}")
                    _LOGGER.debug("%s: BLE failed: %r", self.name, err)
                    continue
                self.state.last_route = ROUTE_BLUETOOTH
                self.state.last_status = "OK"
                return result
            if cloud is None or not self.has_cloud:
                continue
            assert self.cloud is not None and self.cloud_lamp_id is not None
            try:
                result = await cloud(self.cloud, self.cloud_lamp_id)
            except CloudAuthError as err:
                errors.append(f"cloud: API key rejected ({err})")
                continue
            except CloudError as err:
                errors.append(f"cloud: {err}")
                continue
            self.state.last_route = ROUTE_CLOUD
            self.state.last_status = "Queued"
            return result

        if not errors:
            raise HomeAssistantError(
                f"{self.name}: this action needs a Bluetooth connection to the lamp"
            )
        raise HomeAssistantError(f"{self.name}: " + "; ".join(errors))

    # --- Commands -----------------------------------------------------------------

    async def turn_on(
        self,
        brightness: int | None = None,
        kelvin: int | None = None,
        scene_id: int | None = None,
    ) -> None:
        """Switch on, optionally selecting a scene and adjusting it."""
        if self.power is not None and self.power.is_on is not True:
            self._power_on_requested = time.monotonic()
            await self.power.turn_on()
            if scene_id is None and brightness is None and kelvin is None:
                # The lamp starts its power-up scene by itself; the state is
                # read once it is back (power_restored).
                self.notify()
                return
            await self.wait_until_reachable()
            await self.read_state()
        select: int | None = scene_id
        if select is None and not self.state.is_on:
            select = SCENE_DEFAULT

        frames: list[bytes] = []
        if select is not None:
            frames.append(protocol.select_scene(select))
        if brightness is not None:
            frames.append(protocol.brightness(brightness))
        if kelvin is not None:
            frames.append(protocol.color_temperature(kelvin))
        if not frames:
            return

        async def local(ble: LukeRobertsBle) -> None:
            for response in await ble.request_many(frames):
                protocol.check_status(response)

        async def cloud(api: LukeRobertsCloud, lamp_id: int) -> None:
            if select == SCENE_DEFAULT:
                await api.power(lamp_id, True)
            elif select is not None:
                await api.scene(lamp_id, select)
            if brightness is not None:
                await api.brightness(lamp_id, brightness)
            if kelvin is not None:
                await api.kelvin(lamp_id, kelvin)

        reports = self._scene_reports
        await self._run(local, cloud)
        if select is not None:
            # A scene the lamp reported meanwhile is more precise (e.g. the
            # real id behind "Default"), so it is kept.
            if self._scene_reports == reports:
                self.state.scene_id = select
            # A newly selected scene brings its own brightness and colour.
            self.state.brightness = 100
            self.state.kelvin = None
        self.state.is_on = True
        if brightness is not None:
            self.state.brightness = brightness
        if kelvin is not None:
            self.state.kelvin = kelvin
        await self._read_state_after_command()
        self.notify()

    async def turn_off(self) -> None:
        """Cut the power if a power switch is set, else select the Off scene."""
        if self.power is not None:
            await self.power.turn_off()
            self.notify()
            return

        async def local(ble: LukeRobertsBle) -> None:
            protocol.check_status(await ble.request(protocol.select_scene(SCENE_OFF)))

        async def cloud(api: LukeRobertsCloud, lamp_id: int) -> None:
            await api.power(lamp_id, False)

        await self._run(local, cloud)
        self.state.is_on = False
        self.state.scene_id = SCENE_OFF
        await self._read_state_after_command()
        self.notify()

    async def select_scene(self, scene_id: int) -> None:
        """Select a scene by id (0 = off, 255 = default)."""
        if scene_id == SCENE_OFF:
            await self.turn_off()
        else:
            await self.turn_on(scene_id=scene_id)

    async def next_scene(self, direction: int) -> None:
        """Step to the next brighter (+1) or dimmer (-1) scene. BLE only."""
        self._require_power()

        async def local(ble: LukeRobertsBle) -> None:
            protocol.check_status(
                await ble.request(protocol.next_scene_by_brightness(direction))
            )

        reports = self._scene_reports
        await self._run(local, None)
        self.state.is_on = True
        if self._scene_reports == reports:
            self.state.scene_id = None
        self.state.brightness = 100
        await self._read_state_after_command()
        self.notify()

    async def relative_brightness(self, delta: int) -> None:
        """Change the current brightness by delta percentage points. BLE only."""
        self._require_power()

        async def local(ble: LukeRobertsBle) -> None:
            protocol.check_status(
                await ble.request(protocol.relative_brightness(delta))
            )

        await self._run(local, None)
        if self.state.brightness is not None:
            self.state.brightness = max(0, min(100, self.state.brightness + delta))
        await self._read_state_after_command()
        self.notify()

    async def brightness_step(self, direction: int) -> None:
        """One step brighter (+1) or dimmer (-1)."""
        step = RELATIVE_BRIGHTNESS_STEP
        await self.relative_brightness(step if direction >= 0 else -step)

    async def immediate_light(
        self,
        uplight: protocol.Uplight | None,
        downlight: protocol.Downlight | None,
        duration_ms: int,
    ) -> None:
        """Send a transient Immediate Light command. BLE only."""
        self._require_power()
        frame = protocol.immediate_light(uplight, downlight, duration_ms)

        async def local(ble: LukeRobertsBle) -> None:
            protocol.check_status(await ble.request(frame))

        await self._run(local, None)
        if duration_ms == 0:
            self.state.is_on = True
            if downlight is not None:
                self.state.kelvin = downlight.kelvin
        self.notify()

    async def wait_until_reachable(self, timeout: float | None = None) -> None:
        """Wait after power-up until the lamp answers over Bluetooth.

        The lamp may not have been seen before (power cut since Home
        Assistant started): then its first advertisement creates ``ble``.
        """
        if not self.local_configured:
            return
        if timeout is None:
            timeout = POWER_UP_TIMEOUT
        deadline = time.monotonic() + timeout
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self.ble is not None and self.ble_present:
                try:
                    await self.ping()
                except (*BLE_ERRORS, protocol.LampError, HomeAssistantError) as err:
                    last = err
                else:
                    return
            await asyncio.sleep(1)
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="no_answer_after_power_on",
            translation_placeholders={
                "name": self.name,
                "seconds": f"{timeout:.0f}",
                "error": str(last),
            },
        )

    async def power_restored(self) -> None:
        """Catch up with the lamp after its power went from off to on.

        Which scene the lamp starts with depends on its settings in the Luke
        Roberts app (power-on scene, click detection on the wall switch), so
        it is read, not assumed. Only when this integration switched the
        power on to turn the light on and the lamp still reports its Off
        scene (e.g. after a cut of under a second), Default is selected.
        """
        requested = self._power_on_requested
        self._power_on_requested = None
        asked_for_on = (
            requested is not None and time.monotonic() - requested < POWER_UP_TIMEOUT
        )
        try:
            await self.wait_until_reachable()
            if await self.read_state() and self.state.is_on is False and asked_for_on:
                _LOGGER.debug(
                    "%s: Off scene after power-up, selecting Default", self.name
                )
                assert self.ble is not None
                protocol.check_status(
                    await self.ble.request(protocol.select_scene(SCENE_DEFAULT))
                )
                await self.read_state()
        except (HomeAssistantError, protocol.LampError, *BLE_ERRORS) as err:
            _LOGGER.debug("%s: after power-up: %s", self.name, err)
        self.notify()

    async def _read_state_after_command(self) -> None:
        """Replace the assumed state by the lamp's own after a BLE command."""
        if self.state.last_route == ROUTE_BLUETOOTH:
            await self.read_state()

    async def read_state(self) -> bool:
        """Read brightness, colour temperature and on/off from the lamp.

        Uses the undocumented command ``09``. Returns True if the lamp
        answered and the state was taken over. Never raises: if the lamp
        cannot be asked, the assumed state stays. A lamp that answers
        differently, or does not answer before it ever answered correctly,
        is not asked again until Home Assistant restarts.
        """
        if not self.has_local or not self.ble_present or self.state_readable is False:
            return False
        assert self.ble is not None
        try:
            response = await self.ble.request(protocol.query_state())
        except TimeoutError:
            if self.state_readable is None:
                _LOGGER.debug("%s: no answer to the state query", self.name)
                self.state_readable = False
            return False
        except BLE_ERRORS as err:
            _LOGGER.debug("%s: state not read: %r", self.name, err)
            return False
        light = protocol.parse_state(response)
        if light is None:
            _LOGGER.debug(
                "%s: unexpected answer to the state query: %s; keeping the "
                "assumed state",
                self.name,
                response.hex(" "),
            )
            self.state_readable = False
            return False
        self.state_readable = True
        _LOGGER.debug("%s: lamp reports %s", self.name, light)
        self.state.is_on = light.is_on
        if light.is_on:
            self.state.brightness = light.brightness
            self.state.kelvin = light.kelvin
            if self.state.scene_id == SCENE_OFF:
                self.state.scene_id = None  # switched on elsewhere, scene unknown
        else:
            # While off the lamp reports brightness 0 and the Off scene's
            # colour temperature; the last values are kept for turning on.
            self.state.scene_id = SCENE_OFF
        return True

    def scene_reported(self, scene_id: int) -> None:
        """Take over the active scene the lamp reported over Bluetooth.

        Only the scene and on/off follow; the lamp reports the scene before a
        brightness command in the same batch is applied, so brightness and
        colour temperature come from the state query after the command.
        """
        self._scene_reports += 1
        is_on = scene_id != SCENE_OFF
        if self.state.scene_id == scene_id and self.state.is_on == is_on:
            return
        self.state.scene_id = scene_id
        self.state.is_on = is_on
        self.notify()

    async def ping(self) -> int:
        """Ping the lamp and return its highest supported API version."""
        if self.ble is None:
            raise HomeAssistantError(f"{self.name}: no Bluetooth device configured")
        try:
            version = protocol.parse_ping(await self.ble.request(protocol.ping()))
        except protocol.LampError:
            # Firmware older than 1.0.7 only knows Ping@V=01.
            protocol.check_status(await self.ble.request(protocol.ping_v1()))
            version = 1
        self.state.api_version = version
        return version

    async def refresh_scenes(self) -> dict[int, str]:
        """Walk the scene list with Query Scene. BLE only."""
        if self.ble is None:
            raise HomeAssistantError(
                f"{self.name}: scene names can only be read over Bluetooth"
            )
        scenes: dict[int, str] = {}
        scene_id = 0
        try:
            while scene_id != 0xFF and scene_id not in scenes:
                response = await self.ble.request(protocol.query_scene(scene_id))
                info = protocol.parse_query_scene(scene_id, response)
                scenes[scene_id] = info.name
                if info.next_id == scene_id:
                    break
                scene_id = info.next_id
        except protocol.LampForbiddenError as err:
            raise HomeAssistantError(
                f"{self.name}: the lamp has a security code set and rejects "
                "local commands"
            ) from err
        except protocol.LampError as err:
            if not scenes:
                raise HomeAssistantError(
                    f"{self.name}: reading scenes failed ({err})"
                ) from err
        except BLE_ERRORS as err:
            raise HomeAssistantError(
                f"{self.name}: lamp not reachable over Bluetooth ({err})"
            ) from err
        self.state.scenes = scenes
        self.state.last_route = ROUTE_BLUETOOTH
        self.notify()
        return scenes

    # --- Scene records (not in the Lamp Control API) ---------------------------

    def _scene_ble(self) -> LukeRobertsBle:
        self._require_power()
        if self.mode == MODE_CLOUD_ONLY:
            raise HomeAssistantError(
                f"{self.name}: scenes can only be read and changed over Bluetooth"
            )
        if self.ble is None:
            raise HomeAssistantError(
                f"{self.name}: the lamp is not reachable over Bluetooth right now "
                "(no power, out of range, or connected to the app)"
            )
        return self.ble

    async def read_scene_records(
        self, expected: protocol.SceneRecord | None = None
    ) -> tuple[int, dict[int, protocol.SceneRecord]]:
        """Bring the scene cache up to date and return revision and records.

        Like the Luke Roberts app, only the scenes changed since the cached
        revision are asked for (a check with nothing changed takes about
        0.2 s). Without a usable cache all slots are read; through a
        Bluetooth proxy that drops notifications this takes several attempts,
        and every attempt adds the records that arrived intact. A damaged
        stream is never taken over. BLE only.
        """
        ble = self._scene_ble()
        if self.scene_backups is None:
            raise HomeAssistantError(f"{self.name}: no storage for scene backups")
        try:
            cache = await self.scene_backups.cache()
            if cache is not None:
                updated = await self._sync_changes(ble, cache, expected)
                if updated is not None:
                    cache = updated
                else:
                    cache = await self._sync_all(ble)
            else:
                cache = await self._sync_all(ble)
        except protocol.LampForbiddenError as err:
            raise HomeAssistantError(
                f"{self.name}: the lamp has a security code set and rejects "
                "local commands"
            ) from err
        except protocol.LampError as err:
            raise HomeAssistantError(
                f"{self.name}: reading the scene records failed ({err})"
            ) from err
        except BLE_ERRORS as err:
            raise HomeAssistantError(
                f"{self.name}: lamp not reachable over Bluetooth ({err})"
            ) from err
        await self.scene_backups.set_cache(cache)
        self.state.last_route = ROUTE_BLUETOOTH
        if cache.records != self.scene_records:
            self.scene_records = dict(cache.records)
            self.notify()
        return cache.revision, dict(cache.records)

    async def load_scene_records(self) -> None:
        """Take the scene records from the stored cache (no Bluetooth)."""
        if self.scene_backups is not None and (
            cache := await self.scene_backups.cache()
        ):
            self.scene_records = dict(cache.records)

    async def scene_records_for_editing(
        self,
    ) -> tuple[dict[int, protocol.SceneRecord], str | None]:
        """Scene records brought up to date, or the stored copy with the reason.

        An editor can still show the scenes when the lamp cannot be read
        right now; saving reads the lamp again anyway.
        """
        try:
            async with self._scene_lock:
                _revision, records = await self.read_scene_records()
        except HomeAssistantError as err:
            if not self.scene_records:
                raise
            return dict(self.scene_records), str(err)
        return records, None

    async def update_scene_records(self) -> None:
        """Catch up with scenes changed in the app, if records were read before.

        Only the changes are asked for, so this is quick; the first full
        read is left to the backup action because it can take a while
        through a Bluetooth proxy. Errors are only logged.
        """
        if self.scene_backups is None or await self.scene_backups.cache() is None:
            return
        try:
            async with self._scene_lock:
                await self.read_scene_records()
        except HomeAssistantError as err:
            _LOGGER.debug("%s: scene records not updated: %s", self.name, err)

    async def _sync_changes(
        self,
        ble: LukeRobertsBle,
        cache: SceneCache,
        expected: protocol.SceneRecord | None = None,
    ) -> SceneCache | None:
        """Apply the scenes changed since the cache; None if a full read is needed.

        Like a full read, intact records are collected over attempts and a
        record that only arrived with gaps is rebuilt from two damaged copies.
        """
        collected: dict[int, protocol.SceneRecord] = {}
        damaged: dict[int, list[bytes]] = {}
        last: protocol.SyncDone | None = None
        for attempt in range(1, SYNC_CHANGES_ATTEMPTS + 1):
            done, stream = await ble.sync_scenes(
                cache.revision, cache.database, SYNC_CHANGES_TIMEOUT
            )
            if done is None:
                continue
            if done.database != cache.database or done.revision < cache.revision:
                _LOGGER.debug("%s: scene database changed, reading all", self.name)
                return None
            if last is not None and (
                done.revision != last.revision or done.records != last.records
            ):
                collected, damaged = {}, {}  # changed meanwhile, start over
            last = done
            segments = protocol.changed_record_segments(stream)
            if expected is not None and done.records == 1 and stream:
                # Only the record just written changed: the whole stream is
                # (a damaged copy of) it, even if its header was lost.
                segments = [(expected.scene_id, stream)]
            await self._collect_records(segments, collected, damaged, expected)
            if len(collected) == done.records:
                merged = dict(cache.records)
                merged.update(collected)
                return SceneCache(done.revision, done.database, merged)
            _LOGGER.debug(
                "%s: scene changes incomplete (attempt %d, %d bytes, %d of %d)",
                self.name,
                attempt,
                len(stream),
                len(collected),
                done.records,
            )
        raise HomeAssistantError(
            f"{self.name}: the scene changes did not arrive complete after "
            f"{SYNC_CHANGES_ATTEMPTS} attempts (weak Bluetooth link?); try again"
        )

    @staticmethod
    async def _collect_records(
        segments: Iterable[tuple[int, bytes]],
        collected: dict[int, protocol.SceneRecord],
        damaged: dict[int, list[bytes]],
        expected: protocol.SceneRecord | None = None,
    ) -> None:
        """Add intact records; rebuild others from two damaged copies if unique.

        A record just written (``expected``) is also taken when its damaged
        copies together confirm it.
        """
        for scene_id, segment in segments:
            if scene_id in collected:
                continue
            if (record := protocol.intact_record(segment)) is not None:
                collected[record.scene_id] = record
                continue
            copies = damaged.setdefault(scene_id, [])
            # Only pairs with the new copy; older pairs were tried before.
            for earlier in copies:
                record = await asyncio.get_running_loop().run_in_executor(
                    None, protocol.merge_damaged_records, scene_id, [earlier, segment]
                )
                if record is not None:
                    collected[scene_id] = record
                    break
            copies.append(segment)
            if (
                scene_id not in collected
                and expected is not None
                and scene_id == expected.scene_id
                and protocol.confirms_record(expected.raw, copies)
            ):
                collected[scene_id] = expected

    async def _sync_all(self, ble: LukeRobertsBle) -> SceneCache:
        """Read all scene slots, collecting intact records over several attempts."""
        collected: dict[int, protocol.SceneRecord] = {}
        damaged: dict[int, list[bytes]] = {}
        last: protocol.SyncDone | None = None
        for attempt in range(1, SYNC_ALL_ATTEMPTS + 1):
            done, stream = await ble.sync_scenes(0, bytes(4))
            if done is None:
                continue
            if last is not None and (
                done.revision != last.revision or done.records != last.records
            ):
                collected, damaged = {}, {}  # changed meanwhile, start over
            last = done
            collected.update(protocol.recover_scene_records(stream, done.records))
            # Scenes with gaps: rebuild from two damaged copies when unique.
            await self._collect_records(
                protocol.scene_segments(stream, done.records).items(),
                collected,
                damaged,
            )
            _LOGGER.debug(
                "%s: full scene read attempt %d: %d of %d slots",
                self.name,
                attempt,
                len(collected),
                done.records,
            )
            if len(collected) == done.records:
                return SceneCache(done.revision, done.database, collected)
        have = len(collected)
        total = last.records if last is not None else "?"
        raise HomeAssistantError(
            f"{self.name}: only {have} of {total} scenes arrived complete after "
            f"{SYNC_ALL_ATTEMPTS} attempts (weak Bluetooth link?); try again"
        )

    async def test_bluetooth_link(self, attempts: int = 5) -> dict[str, Any]:
        """Read all scene slots a few times and report how much arrived.

        Read-only. The lamp sends every slot in tiny notifications; a direct
        Bluetooth link delivers all of them, a Bluetooth proxy may drop some.
        """
        ble = self._scene_ble()
        if self.scene_backups is None:
            raise HomeAssistantError(f"{self.name}: no storage for scene backups")
        cache = await self.scene_backups.cache()
        full = (
            b"".join(cache.records[i].raw for i in sorted(cache.records))
            if cache
            else b""
        )
        expected = len(full) or None
        results: list[dict[str, Any]] = []
        async with self._scene_lock:
            for _ in range(attempts):
                started = time.monotonic()
                try:
                    done, stream = await ble.sync_scenes(0, bytes(4))
                except BLE_ERRORS as err:
                    raise HomeAssistantError(
                        f"{self.name}: lamp not reachable over Bluetooth ({err})"
                    ) from err
                result: dict[str, Any] = {
                    "received_bytes": len(stream),
                    "final_answer": done is not None,
                    "seconds": round(time.monotonic() - started, 1),
                }
                if expected:
                    result["lost_percent"] = round(
                        100 * max(0, expected - len(stream)) / expected, 1
                    )
                    result["gaps"] = protocol.stream_gaps(full, stream)
                results.append(result)
        losses = [r["lost_percent"] for r in results if "lost_percent" in r]
        return {
            "expected_bytes": expected,
            "attempts": results,
            "average_lost_percent": round(sum(losses) / len(losses), 1)
            if losses
            else None,
            "final_answers_missing": sum(not r["final_answer"] for r in results),
        }

    async def backup_scenes(self, reason: str = "manual") -> SceneBackup:
        """Read all scene records and keep them as a backup."""
        async with self._scene_lock:
            return await self._backup_scenes(reason)

    async def _backup_scenes(self, reason: str) -> SceneBackup:
        if self.scene_backups is None:
            raise HomeAssistantError(f"{self.name}: no storage for scene backups")
        revision, records = await self.read_scene_records()
        backup = await self.scene_backups.add(records, revision, reason)
        _LOGGER.debug(
            "%s: scene backup at revision 0x%X (%s)", self.name, revision, reason
        )
        return backup

    async def rename_scene(self, scene_id: int, name: str) -> None:
        """Give a static scene another name, keeping everything else."""
        try:
            protocol.encode_scene_name(name)
        except protocol.SceneRecordError as err:
            raise ServiceValidationError(f"{self.name}: {err}") from err
        async with self._scene_lock:
            await self._rename_scene(scene_id, name)

    async def _rename_scene(self, scene_id: int, name: str) -> None:
        assert self.scene_backups is not None  # checked by read_scene_records
        revision, records = await self.read_scene_records()
        record = records.get(scene_id)
        if record is None or record.is_empty:
            raise ServiceValidationError(f"{self.name}: there is no scene {scene_id}")
        try:
            renamed = record.with_name(name)
        except protocol.SceneRecordError as err:
            raise ServiceValidationError(
                f"{self.name}: scene {scene_id} ({record.name}) cannot be "
                f"renamed: {err}"
            ) from err
        if renamed == record:
            return
        await self.scene_backups.add(
            records, revision, f"before renaming scene {scene_id}"
        )
        await self._write_scene(renamed)

    async def edit_scene(
        self,
        scene_id: int,
        *,
        kelvin: int | None = None,
        uplight_color: tuple[int, int] | None = None,
        uplight_brightness: int | None = None,
        downlight_brightness: int | None = None,
    ) -> None:
        """Change the light of a static scene, keeping everything else.

        ``uplight_color`` is ``(saturation, value)`` for all 16 segments, as
        in the record: saturation 0 makes the value a colour temperature.
        Brightness is in percent and scales the scene's pattern so that its
        brightest segment or spot gets that value.
        """

        def change(light: protocol.StaticScene) -> protocol.StaticScene:
            return _edit_light(
                light,
                kelvin=kelvin,
                uplight_color=uplight_color,
                uplight_brightness=uplight_brightness,
                downlight_brightness=downlight_brightness,
            )

        await self._change_light(scene_id, change)

    async def save_scene_light(
        self, scene_id: int, light: protocol.StaticScene
    ) -> None:
        """Replace the whole light of a static scene (backup first, then check)."""
        await self._change_light(scene_id, lambda _old: light)

    async def _change_light(
        self,
        scene_id: int,
        change: Callable[[protocol.StaticScene], protocol.StaticScene],
    ) -> None:
        async with self._scene_lock:
            revision, records = await self.read_scene_records()
            assert self.scene_backups is not None
            record = self._static_record(records, scene_id, "edited")
            try:
                edited = change(protocol.StaticScene.from_record(record)).apply(record)
            except protocol.SceneRecordError as err:
                raise ServiceValidationError(f"{self.name}: {err}") from err
            if edited == record:
                return
            await self.scene_backups.add(
                records, revision, f"before editing scene {scene_id}"
            )
            await self._write_scene(edited)

    async def create_scene(self, name: str, light: protocol.StaticScene) -> int:
        """Store a light as a new scene in a free slot; returns its id.

        Like creating a scene in the app, the lamp switches to it.
        """
        try:
            record = light.new_record(name)
        except protocol.SceneRecordError as err:
            raise ServiceValidationError(f"{self.name}: {err}") from err
        async with self._scene_lock:
            revision, records = await self.read_scene_records()
            assert self.scene_backups is not None
            if not any(record.is_empty for record in records.values()):
                raise ServiceValidationError(
                    f"{self.name}: the lamp has no free scene slot"
                )
            await self.scene_backups.add(records, revision, "before creating a scene")
            return await self._write_scene(record)

    async def preview_scene(
        self,
        scene_id: int,
        *,
        kelvin: int | None = None,
        uplight_color: tuple[int, int] | None = None,
        uplight_brightness: int | None = None,
        downlight_brightness: int | None = None,
    ) -> None:
        """Show a static scene, with optional changes, without saving it.

        Takes the same changes as :meth:`edit_scene`. The lamp shows the
        light until a scene is selected (or the lamp is switched).
        """
        async with self._scene_lock:
            _revision, records = await self.read_scene_records()
            record = self._static_record(records, scene_id, "previewed")
            light = _edit_light(
                protocol.StaticScene.from_record(record),
                kelvin=kelvin,
                uplight_color=uplight_color,
                uplight_brightness=uplight_brightness,
                downlight_brightness=downlight_brightness,
            )
        await self.preview_light(light)

    async def preview_light(self, light: protocol.StaticScene) -> None:
        """Show a light on the lamp without saving it; quick, nothing is read.

        Meant for live editing: the newest call wins, so a lost write is
        corrected by the next one.
        """
        try:
            frame = light.live_model()
        except protocol.SceneRecordError as err:
            raise ServiceValidationError(f"{self.name}: {err}") from err
        try:
            await self._scene_ble().write_live_model(frame)
        except BLE_ERRORS as err:
            raise HomeAssistantError(
                f"{self.name}: lamp not reachable over Bluetooth ({err})"
            ) from err

    async def end_preview(self) -> None:
        """Back to the selected scene after previews."""
        scene_id = self.state.scene_id
        if scene_id is None:
            scene_id = SCENE_DEFAULT
        await self.select_scene(scene_id)

    def _static_record(
        self, records: dict[int, protocol.SceneRecord], scene_id: int, verb: str
    ) -> protocol.SceneRecord:
        record = records.get(scene_id)
        if record is None or record.scene_type != protocol.SceneType.STATIC:
            raise ServiceValidationError(
                f"{self.name}: scene {scene_id} is not a static scene; only "
                f"static scenes can be {verb}"
            )
        return record

    async def restore_scene(self, scene_id: int) -> SceneBackup:
        """Write a scene back as it was in the newest backup that differs.

        Backups made by a restore itself are skipped, so restoring twice
        does not undo the first restore. Returns the backup used.
        """
        async with self._scene_lock:
            return await self._restore_scene(scene_id)

    async def _restore_scene(self, scene_id: int) -> SceneBackup:
        if self.scene_backups is None:
            raise HomeAssistantError(f"{self.name}: no storage for scene backups")
        revision, records = await self.read_scene_records()
        current = records.get(scene_id)
        if current is None or current.scene_type != protocol.SceneType.STATIC:
            raise ServiceValidationError(
                f"{self.name}: scene {scene_id} is not a static scene on the lamp; "
                "only existing static scenes can be restored"
            )
        source: SceneBackup | None = None
        for backup in reversed(await self.scene_backups.backups()):
            if backup.reason.startswith(RESTORE_REASON):
                continue
            saved = backup.records.get(scene_id)
            if (
                saved is not None
                and saved.scene_type == protocol.SceneType.STATIC
                and saved.raw[3:] != current.raw[3:]
            ):
                source = backup
                break
        if source is None:
            raise ServiceValidationError(
                f"{self.name}: no backup with a different version of scene {scene_id}"
            )
        await self.scene_backups.add(records, revision, f"{RESTORE_REASON} {scene_id}")
        saved = source.records[scene_id]
        # Keep the current position in the app's list.
        raw = saved.raw[:2] + bytes([current.sort_index]) + saved.raw[3:]
        await self._write_scene(protocol.SceneRecord(raw))
        return source

    async def duplicate_scene(self, scene_id: int, name: str) -> int:
        """Copy a static scene into a free slot under a new name.

        Like creating a scene in the app, the lamp switches to the new
        scene. Returns the id the lamp gave it.
        """
        try:
            protocol.encode_scene_name(name)
        except protocol.SceneRecordError as err:
            raise ServiceValidationError(f"{self.name}: {err}") from err
        async with self._scene_lock:
            revision, records = await self.read_scene_records()
            assert self.scene_backups is not None
            source = records.get(scene_id)
            if source is None or source.scene_type != protocol.SceneType.STATIC:
                raise ServiceValidationError(
                    f"{self.name}: scene {scene_id} is not a static scene; only "
                    "static scenes can be copied"
                )
            if not any(record.is_empty for record in records.values()):
                raise ServiceValidationError(
                    f"{self.name}: the lamp has no free scene slot"
                )
            raw = source.with_name(name).raw
            copy = protocol.SceneRecord(
                bytes([protocol.SCENE_RECORD_VERSION, protocol.SCENE_NEW, 0]) + raw[3:]
            )
            await self.scene_backups.add(
                records, revision, f"before duplicating scene {scene_id}"
            )
            return await self._write_scene(copy)

    async def delete_scene(self, scene_id: int) -> None:
        """Delete a scene that this integration created (with duplicate_scene).

        Scenes made in the app are never deleted from Home Assistant. If the
        scene is active, Default is selected first, as the app does.
        """
        if self.scene_backups is None:
            raise HomeAssistantError(f"{self.name}: no storage for scene backups")
        async with self._scene_lock:
            if scene_id not in await self.scene_backups.created():
                raise ServiceValidationError(
                    f"{self.name}: scene {scene_id} was not created by Home "
                    "Assistant; delete it in the Luke Roberts app"
                )
            revision, records = await self.read_scene_records()
            record = records.get(scene_id)
            if record is None or record.is_empty:
                await self.scene_backups.set_created(scene_id, False)
                raise ServiceValidationError(
                    f"{self.name}: there is no scene {scene_id} (anymore)"
                )
            await self.scene_backups.add(
                records, revision, f"before deleting scene {scene_id}"
            )
            if self.state.scene_id == scene_id:
                await self.turn_on(scene_id=SCENE_DEFAULT)
            header = bytes(
                [protocol.SCENE_RECORD_VERSION, scene_id, record.sort_index]
            ) + bytes([protocol.SceneType.EMPTY, 0, 0, 0, 0])
            await self._write_scene(protocol.SceneRecord(header))
            await self.scene_backups.set_created(scene_id, False)

    async def _write_scene(self, record: protocol.SceneRecord) -> int:
        """Write a record and check what the lamp stored; returns the scene id.

        A record with id ``SCENE_NEW`` creates a scene, the lamp picks the id.
        """
        ble = self._scene_ble()
        try:
            answer = await ble.write_scene_record(record.raw)
        except protocol.LampError as err:
            if err.status == protocol.Status.MEMORY_FULL:
                raise HomeAssistantError(
                    f"{self.name}: the lamp has no room for another scene"
                ) from err
            raise HomeAssistantError(
                f"{self.name}: the lamp rejected the scene ({err})"
            ) from err
        except BLE_ERRORS as err:
            raise HomeAssistantError(
                f"{self.name}: lamp not reachable over Bluetooth ({err})"
            ) from err
        _LOGGER.debug(
            "%s: scene %d written, revision 0x%X",
            self.name,
            answer.scene_id,
            answer.revision,
        )
        new = record.scene_id == protocol.SCENE_NEW
        expected_id = answer.scene_id if new else record.scene_id
        if new and self.scene_backups is not None:
            # Created, even if the check below fails: Home Assistant may
            # delete it again.
            await self.scene_backups.set_created(expected_id, True)
        # What the lamp should now hold: its id and sort index, our content.
        stored_as = protocol.SceneRecord(
            bytes([record.raw[0], expected_id, answer.sort_index]) + record.raw[3:]
        )
        try:
            _, records = await self.read_scene_records(stored_as)
        except HomeAssistantError as err:
            raise HomeAssistantError(
                f"{self.name}: scene {expected_id} was written (the lamp "
                f"confirmed it) but could not be read back to check it: {err}"
            ) from err
        stored = records.get(expected_id)
        if (
            answer.scene_id != expected_id
            or stored is None
            or (stored.raw[3:] != record.raw[3:])
        ):
            raise HomeAssistantError(
                f"{self.name}: the lamp did not store scene {expected_id} as "
                "sent; a backup of the previous scenes was kept"
            )
        await self.refresh_scenes()
        return expected_id

    async def read_device_info(self) -> dict[str, str]:
        """Read the Device Information Service. BLE only."""
        if self.ble is None:
            return {}
        info = await self.ble.read_device_info()
        self.state.device_info = info
        return info

    async def update_from_cloud(self) -> CloudState | None:
        """Refresh the assumed state from the cloud, if configured."""
        if not self.has_cloud:
            return None
        assert self.cloud is not None and self.cloud_lamp_id is not None
        cloud_state = await self.cloud.get_state(self.cloud_lamp_id)
        changed = False
        for attr in ("is_on", "brightness", "kelvin", "scene_id"):
            value = getattr(cloud_state, attr)
            if value is not None and getattr(self.state, attr) != value:
                setattr(self.state, attr, value)
                changed = True
        if changed:
            self.notify()
        return cloud_state


def _edit_light(
    light: protocol.StaticScene,
    *,
    kelvin: int | None,
    uplight_color: tuple[int, int] | None,
    uplight_brightness: int | None,
    downlight_brightness: int | None,
) -> protocol.StaticScene:
    """Apply the changes of edit_scene / preview_scene to a scene's light."""
    if kelvin is not None:
        light.kelvin = kelvin
    if uplight_color is not None:
        light.uplight = [
            (uplight_color[0], uplight_color[1], brightness)
            for _sat, _value, brightness in light.uplight
        ]
    if uplight_brightness is not None:
        levels = protocol.scale_levels(
            [segment[2] for segment in light.uplight], uplight_brightness
        )
        light.uplight = [
            (sat, value, level)
            for (sat, value, _old), level in zip(light.uplight, levels, strict=True)
        ]
    if downlight_brightness is not None:
        light.spots = protocol.scale_levels(light.spots, downlight_brightness)
    return light
