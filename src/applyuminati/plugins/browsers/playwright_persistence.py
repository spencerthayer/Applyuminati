"""Generation-aware Playwright ``storage_state`` persistence.

A mutable cookie-jar file plus a mutable sidecar cannot hold a stale-writer
guarantee: if the state replace lands and the metadata write dies, the next
session looks current when it is not. Publication is therefore a single
atomic step: immutable ``state-N.json`` files behind an atomically replaced
``current.json`` manifest.

``StorageStateStore.status`` never raises. It is the only implementation
that may interpret persisted Playwright files. ``load`` is the operation
that fails closed on corruption.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import tempfile
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, assert_never

from applyuminati.core.clock import utcnow
from applyuminati.core.errors import ConfigurationError
from applyuminati.core.ids import new_ulid
from applyuminati.core.logging import get_logger

try:
    import fcntl
except ModuleNotFoundError:  # pragma: no cover - cross-process locking is POSIX-only
    fcntl = None  # type: ignore[assignment]

log = get_logger(__name__)

MANIFEST_VERSION = 1
MANIFEST_NAME = "current.json"
STATE_FILENAME_TEMPLATE = "state-{generation:08d}.json"
LEGACY_WRITER = "legacy-import"
TEMP_PREFIX = ".playwright-state-"
TEMP_SUFFIX = ".tmp"

__all__ = [
    "MANIFEST_VERSION",
    "ManifestAbsent",
    "ManifestProblem",
    "ManifestValid",
    "StorageSnapshot",
    "StorageStateStatus",
    "StorageStateStore",
]


class StorageStateWriter(Protocol):
    """The Playwright ``BrowserContext`` surface this store needs."""

    async def storage_state(self, *, path: str) -> object: ...


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    """What a session loaded: a Playwright-readable path, or none on first run."""

    path: Path | None
    generation: int


@dataclass(frozen=True, slots=True)
class StorageStateStatus:
    """Health-facing view of persisted state. Never raised as an error."""

    state_exists: bool
    readable: bool
    generation: int


@dataclass(frozen=True, slots=True)
class ManifestAbsent:
    pass


@dataclass(frozen=True, slots=True)
class ManifestValid:
    generation: int
    state_file: str
    writer_session_id: str
    state_path: Path


@dataclass(frozen=True, slots=True)
class ManifestProblem:
    message: str
    code: str
    details: dict[str, object]


def _state_filename(generation: int) -> str:
    return STATE_FILENAME_TEMPLATE.format(generation=generation)


def _restrict_file_mode(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    path.chmod(mode)


def _fsync_directory(directory: Path) -> None:
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        return
    finally:
        os.close(dir_fd)


def _new_temp_path(directory: Path) -> Path:
    handle, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=TEMP_SUFFIX, dir=directory)
    os.close(handle)
    return Path(name)


def _publish_temp_file(temp: Path, target: Path) -> None:
    """Fsync and chmod an already-written sibling temp, then atomically replace."""
    if temp.parent != target.parent:
        msg = f"temp {temp.name} and target {target.name} must share a directory"
        raise ValueError(msg)
    unpublished: Path | None = temp
    try:
        with temp.open("rb") as handle:
            os.fsync(handle.fileno())
        _restrict_file_mode(temp, 0o600)
        temp.replace(target)
        unpublished = None
        _fsync_directory(target.parent)
    finally:
        if unpublished is not None:
            with contextlib.suppress(OSError):
                unpublished.unlink()


def _atomic_replace_bytes(target: Path, payload: bytes) -> None:
    """Write ``payload`` to a unique sibling temp and atomically replace ``target``."""
    directory = target.parent
    directory.mkdir(parents=True, exist_ok=True)
    temp = _new_temp_path(directory)
    unpublished: Path | None = temp
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        _restrict_file_mode(temp, 0o600)
        temp.replace(target)
        unpublished = None
        _fsync_directory(directory)
    finally:
        if unpublished is not None:
            with contextlib.suppress(OSError):
                unpublished.unlink()


def _atomic_replace_json(target: Path, payload: Mapping[str, object]) -> None:
    _atomic_replace_bytes(target, json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"))


def _validate_storage_state(value: object) -> None:
    """Conservative Playwright-state check. Deeper details belong to Playwright."""
    if not isinstance(value, dict):
        msg = "storage state must be a JSON object"
        raise ValueError(msg)
    if not isinstance(value.get("cookies"), list):
        msg = "storage state cookies must be a list"
        raise ValueError(msg)
    if not isinstance(value.get("origins"), list):
        msg = "storage state origins must be a list"
        raise ValueError(msg)


def _storage_state_invalid(filename: str, reason: str) -> ConfigurationError:
    return ConfigurationError(
        f"playwright storage state {filename!r} is not usable: {reason}",
        code="browser.storage_state_invalid",
        details={"filename": filename, "reason": reason},
    )


def _manifest_invalid(filename: str, reason: str) -> ManifestProblem:
    return ManifestProblem(
        message=f"playwright storage-state manifest {filename!r} is not usable: {reason}",
        code="browser.storage_state_manifest_invalid",
        details={"filename": filename, "reason": reason},
    )


class StorageStateStore:
    """One configured path's generation store. Shared by every session on a backend.

    Load, migration, and commit serialise across every store object for the
    same configured path, including across host processes, so two backends
    cannot each publish the next generation.
    """

    def __init__(self, configured: Path) -> None:
        self._configured = configured
        self.store_dir = configured.with_name(configured.name + ".d")
        self._manifest_path = self.store_dir / MANIFEST_NAME
        self._lock = asyncio.Lock()
        self._lock_path = configured.with_name(configured.name + ".lock")

    def status(self) -> StorageStateStatus:
        """Never raises. Converts every persistence problem into a status value."""
        parsed = self._read_manifest()
        match parsed:
            case ManifestAbsent():
                return self._legacy_status()
            case ManifestProblem():
                return StorageStateStatus(state_exists=False, readable=False, generation=0)
            case ManifestValid() as manifest:
                try:
                    self._read_state_bytes(manifest.state_path)
                except ConfigurationError:
                    return StorageStateStatus(
                        state_exists=False,
                        readable=False,
                        generation=manifest.generation,
                    )
                return StorageStateStatus(
                    state_exists=True,
                    readable=True,
                    generation=manifest.generation,
                )
            case _ as unreachable:
                assert_never(unreachable)

    async def load(self) -> StorageSnapshot:
        async with self._lock, self._path_lock():
            return self._load_locked()

    async def commit(
        self,
        context: StorageStateWriter,
        *,
        loaded_generation: int,
        session_id: str,
    ) -> bool:
        async with self._lock, self._path_lock():
            return await self._commit_locked(
                context,
                loaded_generation=loaded_generation,
                session_id=session_id,
            )

    @contextlib.asynccontextmanager
    async def _path_lock(self) -> AsyncIterator[None]:
        """Serialise the read-generation, write-state, publish-manifest transaction.

        The per-instance asyncio.Lock cannot see a second StorageStateStore
        built for the same configured path, so two backend instances (or two
        host processes) could each read generation N and each publish
        generation N+1. A flock on a sibling ``<configured>.lock`` file scopes
        the critical section to the persistence path instead of the Python
        object. Acquire is non-blocking and retried on the event loop, so a
        store waiting on another process never blocks the loop. Non-POSIX
        hosts keep the in-process-only serialisation.
        """
        if fcntl is None:
            yield
            return
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(0.01)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _load_locked(self) -> StorageSnapshot:
        parsed = self._read_manifest()
        match parsed:
            case ManifestValid() as manifest:
                if not manifest.state_path.is_file():
                    raise _storage_state_invalid(
                        manifest.state_file,
                        "referenced state file is missing",
                    )
                self._read_state_bytes(manifest.state_path)
                return StorageSnapshot(path=manifest.state_path, generation=manifest.generation)
            case ManifestProblem() as problem:
                raise ConfigurationError(
                    problem.message,
                    code=problem.code,
                    details=problem.details,
                )
            case ManifestAbsent():
                return self._load_absent()
            case _ as unreachable:
                assert_never(unreachable)

    def _load_absent(self) -> StorageSnapshot:
        if self._configured.is_file():
            return self._migrate_legacy_locked()
        return StorageSnapshot(path=None, generation=0)

    def _legacy_status(self) -> StorageStateStatus:
        if not self._configured.is_file():
            return StorageStateStatus(state_exists=False, readable=True, generation=0)
        try:
            self._read_state_bytes(self._configured)
        except ConfigurationError:
            return StorageStateStatus(state_exists=False, readable=False, generation=0)
        return StorageStateStatus(state_exists=True, readable=True, generation=0)

    def _migrate_legacy_locked(self) -> StorageSnapshot:
        payload = self._read_state_bytes(self._configured)
        self._ensure_store_dir()
        generation = 1
        target = self._state_path(generation)
        _atomic_replace_bytes(target, payload)
        _atomic_replace_json(
            self._manifest_path,
            {
                "version": MANIFEST_VERSION,
                "generation": generation,
                "state_file": _state_filename(generation),
                "updated_at": utcnow().isoformat(),
                "writer_session_id": LEGACY_WRITER,
            },
        )
        self._archive_legacy()
        return StorageSnapshot(path=target, generation=generation)

    async def _commit_locked(
        self,
        context: StorageStateWriter,
        *,
        loaded_generation: int,
        session_id: str,
    ) -> bool:
        parsed = self._read_manifest()
        match parsed:
            case ManifestProblem():
                log.warning(
                    "playwright.storage_state_commit_skipped_unreadable_manifest",
                    session_id=session_id,
                    loaded_generation=loaded_generation,
                )
                return False
            case ManifestAbsent():
                current_generation = 0
            case ManifestValid() as manifest:
                current_generation = manifest.generation
            case _ as unreachable:
                assert_never(unreachable)

        if current_generation != loaded_generation:
            log.warning(
                "playwright.storage_state_stale_write_skipped",
                session_id=session_id,
                loaded_generation=loaded_generation,
                current_generation=current_generation,
            )
            return False

        new_generation = current_generation + 1
        self._ensure_store_dir()
        target = self._state_path(new_generation)
        temp = _new_temp_path(self.store_dir)
        try:
            await context.storage_state(path=str(temp))
        except Exception:
            with contextlib.suppress(OSError):
                temp.unlink()
            raise
        _publish_temp_file(temp, target)
        _atomic_replace_json(
            self._manifest_path,
            {
                "version": MANIFEST_VERSION,
                "generation": new_generation,
                "state_file": _state_filename(new_generation),
                "updated_at": utcnow().isoformat(),
                "writer_session_id": session_id,
            },
        )
        self._prune(new_generation)
        return True

    def _read_manifest(self) -> ManifestAbsent | ManifestValid | ManifestProblem:
        path = self._manifest_path
        if not path.exists():
            return ManifestAbsent()
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            return _manifest_invalid(path.name, str(exc))
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            return _manifest_invalid(path.name, f"invalid JSON: {exc}")
        return self._parse_manifest(value)

    def _parse_manifest(self, value: object) -> ManifestValid | ManifestProblem:  # noqa: PLR0911
        filename = self._manifest_path.name
        if not isinstance(value, dict):
            return _manifest_invalid(filename, "manifest must be a JSON object")
        version = value.get("version")
        if version != MANIFEST_VERSION:
            return _manifest_invalid(filename, f"unsupported manifest version {version!r}")
        generation = value.get("generation")
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
            return _manifest_invalid(filename, "generation must be an integer >= 1")
        state_file = value.get("state_file")
        if not isinstance(state_file, str) or not state_file:
            return _manifest_invalid(filename, "state_file must be a basename")
        if Path(state_file).name != state_file:
            return _manifest_invalid(filename, "state_file must be a basename")
        expected = _state_filename(generation)
        if state_file != expected:
            return _manifest_invalid(
                filename,
                f"state_file {state_file!r} does not match generation {generation}",
            )
        writer = value.get("writer_session_id")
        if not isinstance(writer, str):
            return _manifest_invalid(filename, "writer_session_id must be a string")
        state_path = self._contained_state_path(state_file)
        if state_path is None:
            return _manifest_invalid(filename, "state_file is outside the store directory")
        return ManifestValid(
            generation=generation,
            state_file=state_file,
            writer_session_id=writer,
            state_path=state_path,
        )

    def _contained_state_path(self, state_file: str) -> Path | None:
        candidate = (self.store_dir / state_file).resolve()
        try:
            candidate.relative_to(self.store_dir.resolve())
        except ValueError:
            return None
        return candidate

    def _read_state_bytes(self, path: Path) -> bytes:
        try:
            payload = path.read_bytes()
        except OSError as exc:
            raise _storage_state_invalid(path.name, str(exc)) from exc
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise _storage_state_invalid(path.name, f"invalid JSON: {exc}") from exc
        try:
            _validate_storage_state(value)
        except ValueError as exc:
            raise _storage_state_invalid(path.name, str(exc)) from exc
        return payload

    def _ensure_store_dir(self) -> Path:
        created = not self.store_dir.exists()
        self.store_dir.mkdir(parents=True, exist_ok=True)
        if created:
            _restrict_file_mode(self.store_dir, 0o700)
        return self.store_dir

    def _state_path(self, generation: int) -> Path:
        return self.store_dir / _state_filename(generation)

    def _archive_legacy(self) -> None:
        source = self._configured
        if not source.exists():
            return
        dest = source.with_name(source.name + ".imported")
        if dest.exists():
            dest = source.with_name(f"{source.name}.imported.{new_ulid()}")
        try:
            source.rename(dest)
        except OSError:
            log.warning(
                "playwright.storage_state_legacy_cleanup_skipped",
                filename=source.name,
            )
            return
        log.info(
            "playwright.storage_state_legacy_imported",
            filename=source.name,
            archived_as=dest.name,
        )

    def _prune(self, new_generation: int) -> None:
        keep = {_state_filename(new_generation)}
        if new_generation > 1:
            keep.add(_state_filename(new_generation - 1))
        try:
            candidates = list(self.store_dir.glob("state-*.json"))
        except OSError:
            return
        for path in candidates:
            if path.name in keep:
                continue
            with contextlib.suppress(OSError):
                path.unlink()
