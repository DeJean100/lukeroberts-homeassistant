"""Scene records: Sync, backups, rename and restore."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

from homeassistant.components.light import ATTR_EFFECT_LIST
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.luke_roberts import protocol
from custom_components.luke_roberts.ble import LukeRobertsBle
from custom_components.luke_roberts.const import (
    DOMAIN,
    MODE_LOCAL_ONLY,
    SERVICE_BACKUP_SCENES,
    SERVICE_RENAME_SCENE,
    SERVICE_RESTORE_SCENE,
)
from custom_components.luke_roberts.lamp import LukeRobertsLamp
from custom_components.luke_roberts.scene_backup import SceneBackupStore

from .common import FakeLampClient, service_info, static_record
from .test_init import LIGHT, _setup_ble

FIXTURE = Path(__file__).parent / "fixtures" / "scene_sync_model_f.hex"


@pytest.fixture(autouse=True)
def _bluetooth(mock_bluetooth):
    """Keep the real Bluetooth stack from starting."""


# --- protocol ------------------------------------------------------------------


def test_parse_real_sync_capture() -> None:
    """The 1810 bytes a Model F sent after Sync from revision 0."""
    stream = bytes.fromhex(FIXTURE.read_text().replace("\n", ""))
    records = protocol.parse_scene_records(stream)
    assert len(records) == 20
    by_id = {record.scene_id: record for record in records}
    assert by_id[0].scene_type == protocol.SceneType.NAME_ONLY
    assert by_id[0].name == "Off"
    assert by_id[2].name == "Candle Light"
    assert by_id[2].scene_type == protocol.SceneType.STATIC
    assert by_id[8].scene_type == protocol.SceneType.DYNAMIC
    assert by_id[8].name == "Demo"
    assert by_id[6].is_empty
    assert by_id[6].name == ""
    # Downlight kelvin is little endian: 0x0A8C = 2700 K.
    assert int.from_bytes(by_id[4].raw[8:10], "little") == 2700


def test_parse_scene_records_rejects_garbage() -> None:
    """Unknown types and cut-off records raise."""
    with pytest.raises(protocol.SceneRecordError):
        protocol.parse_scene_records(bytes([0x01, 0x00, 0x00, 0x09]) + bytes(4))
    with pytest.raises(protocol.SceneRecordError):
        protocol.parse_scene_records(static_record(1, "x")[:100])
    with pytest.raises(protocol.SceneRecordError):
        protocol.parse_scene_records(bytes(8))


def test_sync_frame() -> None:
    """Same layout as the app; revision little endian."""
    assert protocol.sync() == bytes.fromhex("07 00000000 00000000 0001")
    assert protocol.sync(0x025E03, bytes.fromhex("d2b221d0")) == bytes.fromhex(
        "07 035e0200 d2b221d0 0001"
    )


def test_parse_answers() -> None:
    """Sync done and scene write answers, as captured."""
    done = protocol.parse_sync_done(
        bytes.fromhex("00 085e0200 01 14 04 9c00 d2b221d0 0000")
    )
    assert done == protocol.SyncDone(0x025E08, 20, 4, bytes.fromhex("d2b221d0"))
    write = protocol.parse_scene_write(bytes.fromhex("00 065e0200 06 4b"))
    assert write == protocol.SceneWrite(0x025E06, 6, 0x4B)
    with pytest.raises(protocol.LampError):
        protocol.parse_scene_write(bytes.fromhex("85"))


def test_rename_keeps_everything_but_the_name() -> None:
    """Only the last 20 bytes change; the app does the same."""
    record = protocol.SceneRecord(static_record(6, "New", 0x4B))
    renamed = record.with_name("ClaudeTest")
    assert renamed.raw[:-20] == record.raw[:-20]
    assert renamed.name == "ClaudeTest"
    assert renamed.raw[-20:] == b"ClaudeTest".ljust(20, b"\x00")


def test_scene_names() -> None:
    """UTF-8, at most 20 bytes, not empty; only static scenes."""
    assert protocol.encode_scene_name(" Gemütlich ") == "Gemütlich".encode().ljust(
        20, b"\x00"
    )
    protocol.encode_scene_name("x" * 20)
    with pytest.raises(protocol.SceneRecordError):
        protocol.encode_scene_name("x" * 21)
    with pytest.raises(protocol.SceneRecordError):
        protocol.encode_scene_name("ü" * 11)  # 22 bytes
    with pytest.raises(protocol.SceneRecordError):
        protocol.encode_scene_name("  ")
    off = protocol.SceneRecord(bytes([1, 0, 0, 3]) + bytes(4) + b"Off".ljust(20, b"\0"))
    with pytest.raises(protocol.SceneRecordError):
        off.with_name("Aus")


# --- Bluetooth ------------------------------------------------------------------


async def test_ble_sync_and_write() -> None:
    """Sync collects the chunked records; writes go to the record characteristic."""
    client = FakeLampClient()
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        ble = LukeRobertsBle(service_info().device, "Lamp", linger=0)
        done, records = await ble.sync_scenes()
        assert client.written[-1] == protocol.sync()
        assert done.records == 5
        assert [record.scene_id for record in records] == [0, 1, 2, 3, 4]
        assert records[2].name == "Gemütlich"
        assert not client.notify_callbacks  # unsubscribed again

        answer = await ble.write_scene_record(static_record(1, "Lesen 2", 10))
        assert answer.scene_id == 1
        assert answer.revision == client.revision
        await ble.disconnect()


async def test_ble_sync_detects_missing_records() -> None:
    """A Sync that announces more records than arrived raises."""
    client = FakeLampClient()
    client.sync_count_offset = 1
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        ble = LukeRobertsBle(service_info().device, "Lamp", linger=0)
        with pytest.raises(protocol.SceneRecordError, match="announced 6"):
            await ble.sync_scenes()
        await ble.disconnect()


# --- Lamp ---------------------------------------------------------------------


async def _lamp(hass: HomeAssistant, client: FakeLampClient) -> LukeRobertsLamp:
    ble = LukeRobertsBle(service_info().device, "Lamp", linger=0)
    lamp = LukeRobertsLamp("Lamp", MODE_LOCAL_ONLY, ble, None, None)
    lamp.scene_backups = SceneBackupStore(hass, "test")
    return lamp


async def test_rename_makes_a_backup_first(hass: HomeAssistant) -> None:
    """Backup, write the same record with a new name, read back, refresh names."""
    client = FakeLampClient()
    before = dict(client.records)
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        await lamp.rename_scene(1, "Abendessen")
        assert len(client.record_writes) == 1
        written = client.record_writes[0]
        assert written[:-20] == before[1][:-20]
        assert protocol.SceneRecord(written).name == "Abendessen"
        assert lamp.state.scenes[1] == "Abendessen"

        backup = await lamp.scene_backups.latest()
        assert backup is not None
        assert backup.reason == "before renaming scene 1"
        assert backup.records[1].raw == before[1]

        # Same name again: nothing is written.
        await lamp.rename_scene(1, "Abendessen")
        assert len(client.record_writes) == 1
        await lamp.ble.disconnect()


async def test_rename_refuses_other_scene_types(hass: HomeAssistant) -> None:
    """Off, dynamic scenes, free slots and bad names are not written."""
    client = FakeLampClient()
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        for scene_id in (0, 3, 4, 9):
            with pytest.raises(ServiceValidationError):
                await lamp.rename_scene(scene_id, "Neu")
        with pytest.raises(ServiceValidationError):
            await lamp.rename_scene(1, "x" * 21)
        assert client.record_writes == []
        await lamp.ble.disconnect()


async def test_rename_detects_a_different_stored_record(hass: HomeAssistant) -> None:
    """If the lamp stores something else, the action fails loudly."""
    client = FakeLampClient()
    client.mangle_record = lambda record: record[:-20] + b"Other".ljust(20, b"\0")
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        with pytest.raises(HomeAssistantError, match="did not store"):
            await lamp.rename_scene(1, "Abendessen")
        await lamp.ble.disconnect()


async def test_restore_writes_the_backup_back(hass: HomeAssistant) -> None:
    """Restore takes the newest differing backup and keeps the sort index."""
    client = FakeLampClient()
    original = client.records[2]
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        await lamp.rename_scene(2, "Kerzen")
        # The app moved the scene in the list meanwhile.
        client.records[2] = client.records[2][:2] + b"\x33" + client.records[2][3:]

        source = await lamp.restore_scene(2)
        assert source.reason == "before renaming scene 2"
        restored = client.records[2]
        assert restored[2] == 0x33
        assert restored[3:] == original[3:]
        assert lamp.state.scenes[2] == "Gemütlich"

        # Restoring again finds nothing newer that differs (the backup made
        # by the restore itself is skipped).
        with pytest.raises(ServiceValidationError):
            await lamp.restore_scene(2)
        with pytest.raises(ServiceValidationError):
            await lamp.restore_scene(3)
        await lamp.ble.disconnect()


async def test_backups_are_kept_and_limited(hass: HomeAssistant) -> None:
    """Backups survive a reload of the store; only the last 20 are kept."""
    client = FakeLampClient()
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        for _ in range(22):
            await lamp.backup_scenes()
        await lamp.ble.disconnect()
    store = SceneBackupStore(hass, "test")
    backups = await store.backups()
    assert len(backups) == 20
    assert backups[-1].records[2].raw == client.records[2]


async def test_scene_writes_need_bluetooth(hass: HomeAssistant) -> None:
    """Without Bluetooth there is nothing to read or write."""
    lamp = LukeRobertsLamp("Lamp", MODE_LOCAL_ONLY, None, None, None)
    lamp.scene_backups = SceneBackupStore(hass, "test")
    with pytest.raises(HomeAssistantError, match="Bluetooth"):
        await lamp.backup_scenes()


# --- Actions --------------------------------------------------------------------


async def test_scene_actions(hass: HomeAssistant) -> None:
    """backup_scenes returns the scenes; rename_scene by name updates the list."""
    client = FakeLampClient()
    await _setup_ble(hass, client)
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_BACKUP_SCENES,
            {ATTR_ENTITY_ID: LIGHT},
            blocking=True,
            return_response=True,
        )
        scenes = response[LIGHT]["scenes"]
        assert {"id": 3, "name": "Demo"} in scenes
        assert all(scene["id"] != 4 for scene in scenes)  # free slot

        await hass.services.async_call(
            DOMAIN,
            SERVICE_RENAME_SCENE,
            {ATTR_ENTITY_ID: LIGHT, "scene": "Lesen", "name": "Lesen hell"},
            blocking=True,
        )
        await hass.async_block_till_done()
        state = hass.states.get(LIGHT)
        assert "Lesen hell" in state.attributes[ATTR_EFFECT_LIST]

        with pytest.raises(ServiceValidationError, match="Unknown scene"):
            await hass.services.async_call(
                DOMAIN,
                SERVICE_RENAME_SCENE,
                {ATTR_ENTITY_ID: LIGHT, "scene": "Nope", "name": "x"},
                blocking=True,
            )

        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_RESTORE_SCENE,
            {ATTR_ENTITY_ID: LIGHT, "scene": 1},
            blocking=True,
            return_response=True,
        )
        assert response[LIGHT]["reason"] == "before renaming scene 1"
        await hass.async_block_till_done()
        assert "Lesen" in hass.states.get(LIGHT).attributes[ATTR_EFFECT_LIST]
