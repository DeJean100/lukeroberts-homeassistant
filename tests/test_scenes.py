"""Scene records: Sync, backups, rename and restore."""

from __future__ import annotations

import asyncio
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
    """Sync returns the answer and the raw stream; writes go to the record char."""
    client = FakeLampClient()
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        ble = LukeRobertsBle(service_info().device, "Lamp", linger=0)
        done, stream = await ble.sync_scenes(0, bytes(4))
        assert client.written[-1] == protocol.sync()
        assert done is not None
        assert done.records == 5
        assert done.database == client.database
        records = protocol.parse_scene_records(stream)
        assert [record.scene_id for record in records] == [0, 1, 2, 3, 4]
        assert records[2].name == "Gemütlich"
        assert not client.notify_callbacks  # unsubscribed again

        answer = await ble.write_scene_record(static_record(1, "Lesen 2", 10))
        assert answer.scene_id == 1
        assert answer.revision == client.revision

        # Only the changed record since the previous revision.
        done, stream = await ble.sync_scenes(answer.revision - 1, client.database)
        assert done is not None
        assert done.records == 1
        assert protocol.parse_scene_records(stream)[0].name == "Lesen 2"
        await ble.disconnect()


async def test_ble_sync_without_answer() -> None:
    """A lost final answer gives None, not an exception."""
    client = FakeLampClient()
    client.drop_done = 1
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        ble = LukeRobertsBle(service_info().device, "Lamp", linger=0)
        done, stream = await ble.sync_scenes(0, bytes(4), timeout=0.05)
        assert done is None
        assert stream
        await ble.disconnect()


def test_recover_scene_records_from_damaged_streams() -> None:
    """Records with a gap inside are dropped; the others are kept, never wrong."""
    stream = bytes.fromhex(FIXTURE.read_text().replace("\n", ""))
    reference = {r.scene_id: r for r in protocol.parse_scene_records(stream)}
    assert protocol.recover_scene_records(stream, 20) == reference
    chunks = [stream[i : i + 2] for i in range(0, len(stream), 2)]
    for start in range(0, len(chunks), 7):
        damaged = b"".join(chunks[:start] + chunks[start + 3 :])
        recovered = protocol.recover_scene_records(damaged, 20)
        assert 0 < len(recovered) < 20
        for scene_id, record in recovered.items():
            assert record == reference[scene_id]


# --- Lamp ---------------------------------------------------------------------


async def _lamp(hass: HomeAssistant, client: FakeLampClient) -> LukeRobertsLamp:
    ble = LukeRobertsBle(service_info().device, "Lamp", linger=0)
    lamp = LukeRobertsLamp("Lamp", MODE_LOCAL_ONLY, ble, None, None)
    lamp.scene_backups = SceneBackupStore(hass, "test")
    ble.add_scene_listener(lamp.scene_reported)
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
        assert await lamp.scene_backups.backups() == []
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
        client.change_record(client.records[2][:2] + b"\x33" + client.records[2][3:])

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


async def test_cache_reads_only_changes(hass: HomeAssistant) -> None:
    """After the first full read only changed scenes are asked for."""
    client = FakeLampClient()
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        await lamp.read_scene_records()
        assert client.syncs == [(0, 5)]
        await lamp.read_scene_records()
        assert client.syncs[-1] == (client.revision, 0)

        client.change_record(static_record(2, "Kerzenlicht", 20))
        revision, records = await lamp.read_scene_records()
        assert client.syncs[-1] == (client.revision - 1, 1)
        assert records[2].name == "Kerzenlicht"
        assert revision == client.revision

        # A reset lamp (other database id) is read in full again.
        client.database = bytes.fromhex("01020304")
        await lamp.read_scene_records()
        assert client.syncs[-1] == (0, 5)
        cache = await lamp.scene_backups.cache()
        assert cache.database == client.database
        await lamp.ble.disconnect()


async def test_full_read_collects_over_attempts(hass: HomeAssistant) -> None:
    """A proxy that loses chunks: intact records are collected per attempt."""
    client = FakeLampClient()
    client.chunk = 2
    attempt = {"n": 0}

    def drop(n: int) -> bool:
        if n == 0:
            attempt["n"] += 1
        # First attempt loses a chunk in scene 1, the second one in scene 2.
        return (attempt["n"] == 1 and n == 30) or (attempt["n"] == 2 and n == 120)

    client.drop_chunk = drop
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        _, records = await lamp.read_scene_records()
        assert attempt["n"] == 2
        assert {k: v.raw for k, v in records.items()} == client.records
        await lamp.ble.disconnect()


def test_merge_damaged_records() -> None:
    """Two copies with gaps in different places give the record back."""
    stream = bytes.fromhex(FIXTURE.read_text().replace("\n", ""))
    reference = {r.scene_id: r for r in protocol.parse_scene_records(stream)}
    for scene_id in (2, 4, 13, 18):
        raw = reference[scene_id].raw
        chunks = [raw[i : i + 2] for i in range(0, len(raw), 2)]
        first = b"".join(chunks[:12] + chunks[15:])
        second = b"".join(chunks[:60] + chunks[62:])
        merged = protocol.merge_damaged_records(scene_id, [first, second])
        assert merged == reference[scene_id]
        # The same gap twice cannot be filled.
        assert protocol.merge_damaged_records(scene_id, [first, first]) is None


def test_merge_refuses_ambiguous_copies() -> None:
    """Repeating data with gaps that fit several ways is not guessed."""
    raw = static_record(1, "Lesen", 10)  # uplight and downlight repeat
    chunks = [raw[i : i + 2] for i in range(0, len(raw), 2)]
    first = b"".join(chunks[:20] + chunks[21:])
    second = b"".join(chunks[:30] + chunks[31:])
    merged = protocol.merge_damaged_records(1, [first, second])
    assert merged is None or merged.raw == raw


async def test_full_read_repairs_damaged_scenes(hass: HomeAssistant) -> None:
    """A scene damaged in every attempt is rebuilt from two copies."""
    client = FakeLampClient()
    client.records[2] = bytes.fromhex(FIXTURE.read_text().replace("\n", ""))[
        28 + 178 : 28 + 2 * 178
    ]
    client.records[2] = client.records[2][:1] + b"\x02" + client.records[2][2:]
    client.chunk = 2
    attempt = {"n": 0}

    def drop(n: int) -> bool:
        if n == 0:
            attempt["n"] += 1
        # Scene 2 starts at chunk 14 + 89 = 103; lose a different chunk of it.
        return n == (110 if attempt["n"] == 1 else 160)

    client.drop_chunk = drop
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        _, records = await lamp.read_scene_records()
        assert attempt["n"] == 2
        assert records[2].raw == client.records[2]
        await lamp.ble.disconnect()


async def test_lost_answer_ends_a_sync_early() -> None:
    """Without the final answer the Sync ends shortly after the last record."""
    client = FakeLampClient()
    client.drop_done = 1
    with (
        patch(
            "custom_components.luke_roberts.ble.establish_connection",
            AsyncMock(return_value=client),
        ),
        patch("custom_components.luke_roberts.ble.SYNC_IDLE_TIMEOUT", 0.1),
    ):
        ble = LukeRobertsBle(service_info().device, "Lamp", linger=0)
        loop = asyncio.get_running_loop()
        start = loop.time()
        done, stream = await ble.sync_scenes(0, bytes(4), timeout=10)
        assert done is None
        assert stream
        assert loop.time() - start < 2
        await ble.disconnect()


async def test_changes_are_retried_and_never_half_taken(hass: HomeAssistant) -> None:
    """Damaged or unanswered change reads are retried; the cache stays intact."""
    client = FakeLampClient()
    with (
        patch(
            "custom_components.luke_roberts.ble.establish_connection",
            AsyncMock(return_value=client),
        ),
        patch("custom_components.luke_roberts.lamp.SYNC_CHANGES_TIMEOUT", 0.05),
    ):
        lamp = await _lamp(hass, client)
        await lamp.read_scene_records()
        client.change_record(static_record(1, "Lesen neu", 10))
        client.drop_done = 1
        tries = {"n": 0}

        def drop(n: int) -> bool:
            if n == 0:
                tries["n"] += 1
            return tries["n"] == 1 and n == 3

        client.drop_chunk = drop
        _, records = await lamp.read_scene_records()
        assert records[1].name == "Lesen neu"

        # Always damaged: an error, and the cache keeps the last good state.
        client.change_record(static_record(1, "Kaputt", 10))
        client.drop_chunk = lambda n: n == 2
        with pytest.raises(HomeAssistantError, match="did not arrive complete"):
            await lamp.read_scene_records()
        cache = await lamp.scene_backups.cache()
        assert cache.records[1].name == "Lesen neu"

        # A rename is refused before anything is written.
        with pytest.raises(HomeAssistantError, match="did not arrive complete"):
            await lamp.rename_scene(2, "Neu")
        assert client.record_writes == []
        await lamp.ble.disconnect()


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


async def test_duplicate_scene(hass: HomeAssistant) -> None:
    """A copy goes to a free slot; the lamp picks id and sort index."""
    client = FakeLampClient()
    source = client.records[2]
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        new_id = await lamp.duplicate_scene(2, "Gemütlich 2")
        assert new_id == 4  # the free slot of the fake lamp
        written = client.record_writes[-1]
        assert written[:4] == bytes([0x01, 0xFE, 0x00, 0x01])
        assert written[8:-20] == source[8:-20]
        stored = protocol.SceneRecord(client.records[4])
        assert stored.name == "Gemütlich 2"
        assert stored.sort_index == 0x4B
        assert lamp.state.scenes[4] == "Gemütlich 2"
        assert client.records[2] == source
        backup = await lamp.scene_backups.latest()
        assert backup.reason == "before duplicating scene 2"

        # No free slot left, Off and dynamic scenes: refused, nothing written.
        writes = len(client.record_writes)
        for scene_id, name in ((2, "Noch eine"), (0, "Aus 2"), (3, "Demo 2")):
            with pytest.raises(ServiceValidationError):
                await lamp.duplicate_scene(scene_id, name)
        assert len(client.record_writes) == writes
        await lamp.ble.disconnect()


async def test_duplicate_memory_full(hass: HomeAssistant) -> None:
    """The lamp's 'memory full' answer becomes a clear error."""
    client = FakeLampClient()

    def full(record: bytes) -> bytes:
        client.record_writes.append(record)
        return b"\x85"

    client._write_record = full  # type: ignore[method-assign]
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        with pytest.raises(HomeAssistantError, match="no room"):
            await lamp.duplicate_scene(1, "Lesen 2")
        await lamp.ble.disconnect()


async def test_duplicate_action(hass: HomeAssistant) -> None:
    """duplicate_scene returns the new id; the copy appears in the list."""
    client = FakeLampClient()
    await _setup_ble(hass, client)
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        response = await hass.services.async_call(
            DOMAIN,
            "duplicate_scene",
            {ATTR_ENTITY_ID: LIGHT, "scene": "Lesen", "name": "Lesen warm"},
            blocking=True,
            return_response=True,
        )
        await hass.async_block_till_done()
    assert response[LIGHT] == {"scene_id": 4, "name": "Lesen warm"}
    assert "Lesen warm" in hass.states.get(LIGHT).attributes[ATTR_EFFECT_LIST]


async def test_delete_only_own_scenes(hass: HomeAssistant) -> None:
    """Only scenes made by duplicate_scene can be deleted; Default first."""
    client = FakeLampClient()
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        with pytest.raises(ServiceValidationError, match="not created"):
            await lamp.delete_scene(1)
        new_id = await lamp.duplicate_scene(1, "Lesen 2")
        await hass.async_block_till_done()
        assert lamp.state.scene_id == new_id  # the lamp switched to the copy

        client.written.clear()
        await lamp.delete_scene(new_id)
        assert protocol.select_scene(0xFF) in client.written
        assert client.record_writes[-1] == bytes([0x01, new_id, 0x4B, 0, 0, 0, 0, 0])
        assert protocol.SceneRecord(client.records[new_id]).is_empty
        assert new_id not in lamp.state.scenes
        assert await lamp.scene_backups.created() == set()
        assert (await lamp.scene_backups.latest()).reason == (
            f"before deleting scene {new_id}"
        )
        with pytest.raises(ServiceValidationError):
            await lamp.delete_scene(new_id)
        await lamp.ble.disconnect()


def test_static_scene_round_trip() -> None:
    """Decoding and encoding a real record gives the same bytes."""
    stream = bytes.fromhex(FIXTURE.read_text().replace("\n", ""))
    for record in protocol.parse_scene_records(stream):
        if record.scene_type == protocol.SceneType.STATIC:
            light = protocol.StaticScene.from_record(record)
            assert light.apply(record) == record
            assert len(light.uplight) == 16
            assert len(light.spots) == 84


def test_scale_levels_keeps_the_pattern() -> None:
    """The brightest gets the percentage, the rest follows."""
    assert protocol.scale_levels([0, 51, 102], 100) == [0, 128, 255]
    assert protocol.scale_levels([0, 0], 50) == [128, 128]
    assert protocol.scale_levels([10, 20], 0) == [0, 0]


async def test_edit_scene(hass: HomeAssistant) -> None:
    """Only the asked values change; backup first; name and header kept."""
    client = FakeLampClient()
    source = client.records[1]
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        lamp = await _lamp(hass, client)
        await lamp.edit_scene(
            1,
            kelvin=3500,
            uplight_color=protocol.uplight_hs(240, 100),
            downlight_brightness=50,
        )
        edited = protocol.SceneRecord(client.records[1])
        light = protocol.StaticScene.from_record(edited)
        assert light.kelvin == 3500
        assert {segment[:2] for segment in light.uplight} == {(255, 43690)}
        before = protocol.StaticScene.from_record(protocol.SceneRecord(source))
        assert [s[2] for s in light.uplight] == [s[2] for s in before.uplight]
        assert max(light.spots) == 128
        assert edited.raw[:8] == source[:8]
        assert edited.name == "Lesen"
        assert (await lamp.scene_backups.latest()).reason == "before editing scene 1"

        # Nothing changes: nothing is written.
        writes = len(client.record_writes)
        await lamp.edit_scene(1, kelvin=3500)
        assert len(client.record_writes) == writes
        with pytest.raises(ServiceValidationError):
            await lamp.edit_scene(3, kelvin=3000)  # dynamic
        await lamp.ble.disconnect()


async def test_edit_action(hass: HomeAssistant) -> None:
    """edit_scene action with a white uplight; empty calls are refused."""
    client = FakeLampClient()
    await _setup_ble(hass, client)
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        await hass.services.async_call(
            DOMAIN,
            "edit_scene",
            {
                ATTR_ENTITY_ID: LIGHT,
                "scene": "Gemütlich",
                "uplight_kelvin": 3000,
                "uplight_brightness": 20,
            },
            blocking=True,
        )
        light = protocol.StaticScene.from_record(
            protocol.SceneRecord(client.records[2])
        )
        assert {segment[:2] for segment in light.uplight} == {(0, 3000)}
        assert max(segment[2] for segment in light.uplight) == 51
        for data in ({}, {"uplight_kelvin": 3000, "uplight_hs_color": [0, 50]}):
            with pytest.raises(ServiceValidationError):
                await hass.services.async_call(
                    DOMAIN,
                    "edit_scene",
                    {ATTR_ENTITY_ID: LIGHT, "scene": "Lesen", **data},
                    blocking=True,
                )


# Captured from app 2.4.0: the scene record it saved, and the live model it
# wrote to preview that scene afterwards.
CAPTURED_RECORD = bytes.fromhex(
    "01fe0001000000008c0aa2701f92a2701f94a2701fa2a2701f82a2701f93a2701fa9a2701f8fa2701fd5a2701fffa2701fffa2701fffa2701f7da2701f82a2701f9ca2701fa1a2701f8e000000000000000000000000000029be000000000000001fffffffff0000000000000000d3ffffffffffb3000000000000000032ffffffac400021a100000000000000000000005fbc00000000000000000000004e65770000000000000000000000000000000000"
)
CAPTURED_LIVE_MODEL = bytes.fromhex(
    "648c0aa2701f92a2701f94a2701fa2a2701f82a2701f93a2701fa9a2701f8fa2701fd5a2701fffa2701fffa2701fffa2701f7da2701f82a2701f9ca2701fa1a2701f8e000000000000000000000000000029be000000000000001fffffffff0000000000000000d3ffffffffffb3000000000000000032ffffffac400021a100000000000000000000005fbc0000000000000000000000"
)


def test_live_model_matches_the_app() -> None:
    """The preview frame is byte-identical to what the app writes."""
    record = protocol.SceneRecord(CAPTURED_RECORD)
    light = protocol.StaticScene.from_record(record)
    assert light.live_model(record) == CAPTURED_LIVE_MODEL


async def test_preview_scene(hass: HomeAssistant) -> None:
    """preview_scene writes a live model with the changes and saves nothing."""
    client = FakeLampClient()
    await _setup_ble(hass, client)
    records = dict(client.records)
    with patch(
        "custom_components.luke_roberts.ble.establish_connection",
        AsyncMock(return_value=client),
    ):
        await hass.services.async_call(
            DOMAIN,
            "preview_scene",
            {ATTR_ENTITY_ID: LIGHT, "scene": "Lesen", "downlight_kelvin": 3500},
            blocking=True,
        )
        record = protocol.SceneRecord(records[1])
        light = protocol.StaticScene.from_record(record)
        light.kelvin = 3500
        assert client.live_model == [light.live_model(record)]
        assert client.record_writes == []
        assert client.records == records

        # Without changes it shows the scene as stored.
        await hass.services.async_call(
            DOMAIN,
            "preview_scene",
            {ATTR_ENTITY_ID: LIGHT, "scene": "Lesen"},
            blocking=True,
        )
        assert client.live_model[-1][1:] == records[1][8:158]
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                DOMAIN,
                "preview_scene",
                {ATTR_ENTITY_ID: LIGHT, "scene": 3, "downlight_kelvin": 3000},
                blocking=True,
            )
