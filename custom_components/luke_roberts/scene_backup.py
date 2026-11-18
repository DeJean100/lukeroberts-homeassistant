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


@dataclass(slots=True)
class SceneCache:
    """The lamp's scene records as last read, with the Sync revision.

    ``database`` is the id the lamp reports in its Sync answer; an
    incremental Sync only works with it.
    """

    revision: int
    database: bytes
    records: dict[int, SceneRecord]

    def as_dict(self) -> dict[str, Any]:
        """Storage form."""
        return {
            "revision": self.revision,
            "database": self.database.hex(),
            "records": [record.raw.hex() for record in self.records.values()],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SceneCache:
        """Read the storage form."""
        records = [SceneRecord(bytes.fromhex(raw)) for raw in data["records"]]
        return cls(
            revision=data["revision"],
            database=bytes.fromhex(data["database"]),
            records={record.scene_id: record for record in records},
        )


class SceneBackupStore:
    """Scene cache and the last ``MAX_BACKUPS`` backups of one lamp."""

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.scene_backups.{key}"
        )
        self._loaded = False
        self._backups: list[SceneBackup] = []
        self._cache: SceneCache | None = None

    async def _load(self) -> None:
        if self._loaded:
            return
        data = await self._store.async_load() or {}
        self._backups = [
            SceneBackup.from_dict(item) for item in data.get("backups", [])
        ]
        if cache := data.get("cache"):
            self._cache = SceneCache.from_dict(cache)
        self._loaded = True

    async def _save(self) -> None:
        data: dict[str, Any] = {"backups": [item.as_dict() for item in self._backups]}
        if self._cache is not None:
            data["cache"] = self._cache.as_dict()
        await self._store.async_save(data)

    async def backups(self) -> list[SceneBackup]:
        """All backups, oldest first."""
        await self._load()
        return self._backups

    async def latest(self) -> SceneBackup | None:
        """The newest backup, if any."""
        backups = await self.backups()
        return backups[-1] if backups else None

    async def add(
        self, records: dict[int, SceneRecord], revision: int, reason: str
    ) -> SceneBackup:
        """Store a new backup and drop the oldest ones."""
        await self._load()
        backup = SceneBackup(
            time=dt_util.utcnow().isoformat(timespec="seconds"),
            reason=reason,
            revision=revision,
            records=dict(records),
        )
        self._backups.append(backup)
        del self._backups[:-MAX_BACKUPS]
        await self._save()
        return backup

    async def cache(self) -> SceneCache | None:
        """The scene records as last read, if any."""
        await self._load()
        return self._cache

    async def set_cache(self, cache: SceneCache) -> None:
        """Keep the scene records as last read."""
        await self._load()
        self._cache = cache
        await self._save()
