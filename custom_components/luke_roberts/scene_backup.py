"""Backups of the scene records stored in a lamp.

Every scene write is preceded by a backup of all scene records, kept in Home
Assistant's storage (``.storage/luke_roberts.scene_backups.<id>``), so a scene
can be written back byte for byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .protocol import SceneRecord

STORAGE_VERSION = 1
MAX_BACKUPS = 20


@dataclass(frozen=True, slots=True)
class SceneBackup:
    """All scene records of a lamp at one moment."""

    time: str  # ISO 8601
    reason: str
    revision: int
    records: dict[int, SceneRecord]

    def as_dict(self) -> dict[str, Any]:
        """Storage form."""
        return {
            "time": self.time,
            "reason": self.reason,
            "revision": self.revision,
            "records": [record.raw.hex() for record in self.records.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SceneBackup:
        """Read the storage form."""
        records = [SceneRecord(bytes.fromhex(raw)) for raw in data["records"]]
        return cls(
            time=data["time"],
            reason=data["reason"],
            revision=data["revision"],
            records={record.scene_id: record for record in records},
        )

    def summary(self) -> dict[str, Any]:
        """Short form for action responses: scene ids and names."""
        return {
            "time": self.time,
            "reason": self.reason,
            "revision": self.revision,
            "scenes": [
                {"id": scene_id, "name": record.name}
                for scene_id, record in self.records.items()
                if not record.is_empty
            ],
        }


class SceneBackupStore:
    """The last ``MAX_BACKUPS`` backups of one lamp, newest last."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.scene_backups.{key}"
        )
        self._backups: list[SceneBackup] | None = None

    async def backups(self) -> list[SceneBackup]:
        """All backups, oldest first."""
        if self._backups is None:
            data = await self._store.async_load() or {}
            self._backups = [
                SceneBackup.from_dict(item) for item in data.get("backups", [])
            ]
        return self._backups

    async def latest(self) -> SceneBackup | None:
        """The newest backup, if any."""
        backups = await self.backups()
        return backups[-1] if backups else None

    async def add(
        self, records: dict[int, SceneRecord], revision: int, reason: str
    ) -> SceneBackup:
        """Store a new backup and drop the oldest ones."""
        backup = SceneBackup(
            time=dt_util.utcnow().isoformat(timespec="seconds"),
            reason=reason,
            revision=revision,
            records=dict(records),
        )
        backups = await self.backups()
        backups.append(backup)
        del backups[:-MAX_BACKUPS]
        await self._store.async_save({"backups": [item.as_dict() for item in backups]})
        return backup
