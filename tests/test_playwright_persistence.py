"""Generation-aware Playwright storage-state persistence.

These tests never launch Chromium. They drive :class:`StorageStateStore`
through a fake context whose ``storage_state`` writes JSON the store already
knows how to validate.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from applyuminati.core.errors import ConfigurationError
from applyuminati.plugins.browsers.playwright_persistence import (
    StorageStateStore,
    _atomic_replace_json,
    _new_temp_path,
)

EMPTY_STATE = {"cookies": [], "origins": []}


def _store(tmp_path: Path) -> StorageStateStore:
    return StorageStateStore(tmp_path / "playwright-state.json")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


class FakeContext:
    def __init__(self, payload: dict[str, Any], *, delay: float = 0.0) -> None:
        self.payload = payload
        self.delay = delay
        self.temp_paths: list[str] = []

    async def storage_state(self, *, path: str) -> None:
        self.temp_paths.append(path)
        if self.delay:
            await asyncio.sleep(self.delay)
        Path(path).write_text(json.dumps(self.payload))  # noqa: ASYNC240


class RaisingContext:
    async def storage_state(self, *, path: str) -> None:
        raise RuntimeError("playwright refused to dump storage state")


def _state_payload(marker: str) -> dict[str, Any]:
    return {"cookies": [{"name": marker, "value": "1"}], "origins": []}


def _published_cookies(store: StorageStateStore) -> list[dict[str, object]]:
    current = json.loads((store.store_dir / "current.json").read_text())
    state = json.loads((store.store_dir / current["state_file"]).read_text())
    cookies = state["cookies"]
    assert isinstance(cookies, list)
    return cookies


# ---------------------------------------------------------------------------
# First run, sequential generations, stale writers
# ---------------------------------------------------------------------------


async def test_first_run_creates_generation_one(tmp_path: Path) -> None:
    store = _store(tmp_path)
    snapshot = await store.load()
    assert snapshot.path is None
    assert snapshot.generation == 0

    committed = await store.commit(
        FakeContext(_state_payload("a")),
        loaded_generation=0,
        session_id="s-a",
    )
    assert committed is True
    status = store.status()
    assert status == store.status()
    assert status.state_exists is True
    assert status.readable is True
    assert status.generation == 1
    assert _published_cookies(store) == [{"name": "a", "value": "1"}]


async def test_sequential_sessions_advance_the_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert await store.commit(
        FakeContext(_state_payload("one")), loaded_generation=0, session_id="s1"
    )
    first = await store.load()
    assert first.generation == 1
    assert await store.commit(
        FakeContext(_state_payload("two")), loaded_generation=1, session_id="s2"
    )
    second = await store.load()
    assert second.generation == 2
    assert await store.commit(
        FakeContext(_state_payload("three")), loaded_generation=2, session_id="s3"
    )
    third = await store.load()
    assert third.generation == 3
    assert _published_cookies(store) == [{"name": "three", "value": "1"}]


async def test_a_stale_writer_does_not_overwrite_the_newer_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.commit(FakeContext(_state_payload("seed")), loaded_generation=0, session_id="seed")
    snapshot_a = await store.load()
    snapshot_b = await store.load()
    assert snapshot_a.generation == snapshot_b.generation == 1

    assert await store.commit(
        FakeContext(_state_payload("a")),
        loaded_generation=snapshot_a.generation,
        session_id="a",
    )
    skipped = await store.commit(
        FakeContext(_state_payload("b")),
        loaded_generation=snapshot_b.generation,
        session_id="b",
    )
    assert skipped is False
    assert store.status().generation == 2
    assert _published_cookies(store) == [{"name": "a", "value": "1"}]


async def test_the_other_stale_writer_order_also_keeps_the_first_commit(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.commit(FakeContext(_state_payload("seed")), loaded_generation=0, session_id="seed")
    snapshot_a = await store.load()
    snapshot_b = await store.load()

    assert await store.commit(
        FakeContext(_state_payload("b")),
        loaded_generation=snapshot_b.generation,
        session_id="b",
    )
    skipped = await store.commit(
        FakeContext(_state_payload("a")),
        loaded_generation=snapshot_a.generation,
        session_id="a",
    )
    assert skipped is False
    assert _published_cookies(store) == [{"name": "b", "value": "1"}]


async def test_the_store_lock_lets_exactly_one_of_two_concurrent_commits_win(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    await store.commit(FakeContext(_state_payload("seed")), loaded_generation=0, session_id="seed")
    context_a = FakeContext(_state_payload("a"), delay=0.05)
    context_b = FakeContext(_state_payload("b"), delay=0.05)
    results = await asyncio.gather(
        store.commit(context_a, loaded_generation=1, session_id="a"),
        store.commit(context_b, loaded_generation=1, session_id="b"),
    )
    assert results.count(True) == 1
    assert results.count(False) == 1
    assert store.status().generation == 2
    winner = _published_cookies(store)[0]["name"]
    assert winner in {"a", "b"}


async def test_candidate_temp_paths_are_unique_and_not_generation_files(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.store_dir.mkdir()
    first = _new_temp_path(store.store_dir)
    second = _new_temp_path(store.store_dir)
    assert first != second
    for path in (first, second):
        assert path.parent == store.store_dir
        assert path.name.startswith(".playwright-state-")
        assert path.name.endswith(".tmp")
        assert not path.name.startswith("state-")
        assert path.name != "current.json"


async def test_a_storage_state_failure_leaves_no_tmp_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(RuntimeError, match="refused"):
        await store.commit(RaisingContext(), loaded_generation=0, session_id="s")
    if store.store_dir.exists():
        leftovers = list(store.store_dir.glob("*.tmp"))
        assert leftovers == []


# ---------------------------------------------------------------------------
# Legacy migration
# ---------------------------------------------------------------------------


async def test_a_legacy_file_is_imported_as_generation_one(tmp_path: Path) -> None:
    configured = tmp_path / "playwright-state.json"
    _write_json(configured, _state_payload("legacy"))
    store = StorageStateStore(configured)

    before = store.status()
    assert before.state_exists is True
    assert before.readable is True
    assert before.generation == 0

    snapshot = await store.load()
    assert snapshot.generation == 1
    assert snapshot.path is not None
    assert snapshot.path.name == "state-00000001.json"
    assert json.loads(snapshot.path.read_text())["cookies"][0]["name"] == "legacy"
    assert list(configured.parent.glob("playwright-state.json.imported*"))
    assert not configured.exists()


async def test_status_reports_a_corrupt_legacy_file_before_migration(tmp_path: Path) -> None:
    configured = tmp_path / "playwright-state.json"
    configured.write_text("not json")
    store = StorageStateStore(configured)
    status = store.status()
    assert status.state_exists is False
    assert status.readable is False
    assert status.generation == 0
    with pytest.raises(ConfigurationError) as raised:
        await store.load()
    assert raised.value.code == "browser.storage_state_invalid"


async def test_interrupted_mid_migration_retries_and_overwrites_the_orphan(
    tmp_path: Path,
) -> None:
    configured = tmp_path / "playwright-state.json"
    _write_json(configured, _state_payload("legacy"))
    store = StorageStateStore(configured)
    store.store_dir.mkdir()
    orphan = store.store_dir / "state-00000001.json"
    orphan.write_text("orphaned-partial")

    snapshot = await store.load()
    assert snapshot.generation == 1
    assert snapshot.path is not None
    assert json.loads(snapshot.path.read_text())["cookies"][0]["name"] == "legacy"


async def test_manifest_wins_when_the_legacy_file_was_not_renamed(tmp_path: Path) -> None:
    configured = tmp_path / "playwright-state.json"
    _write_json(configured, _state_payload("legacy"))
    store = StorageStateStore(configured)
    await store.load()
    leftover = configured
    leftover.write_text(json.dumps(_state_payload("should-not-import")))

    snapshot = await store.load()
    assert snapshot.generation == 1
    assert snapshot.path is not None
    assert json.loads(snapshot.path.read_text())["cookies"][0]["name"] == "legacy"


async def test_an_existing_imported_backup_is_not_overwritten(tmp_path: Path) -> None:
    configured = tmp_path / "playwright-state.json"
    _write_json(configured, _state_payload("legacy"))
    backup = configured.with_name(configured.name + ".imported")
    backup.write_text("keep-me")
    store = StorageStateStore(configured)

    await store.load()
    assert backup.read_text() == "keep-me"
    extras = [
        path
        for path in configured.parent.iterdir()
        if path.name.startswith("playwright-state.json.imported")
    ]
    assert len(extras) >= 1


# ---------------------------------------------------------------------------
# Interrupted commit, orphan pruning
# ---------------------------------------------------------------------------


async def test_a_failed_manifest_replace_leaves_the_previous_generation_authoritative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    await store.commit(FakeContext(_state_payload("one")), loaded_generation=0, session_id="s1")
    original = _atomic_replace_json

    def boom(target: Path, payload: object) -> None:
        if target.name == "current.json":
            raise OSError("disk full")
        original(target, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "applyuminati.plugins.browsers.playwright_persistence._atomic_replace_json",
        boom,
    )
    with pytest.raises(OSError, match="disk full"):
        await store.commit(FakeContext(_state_payload("two")), loaded_generation=1, session_id="s2")
    monkeypatch.undo()

    snapshot = await store.load()
    assert snapshot.generation == 1
    assert _published_cookies(store) == [{"name": "one", "value": "1"}]
    orphan = store.store_dir / "state-00000002.json"
    assert orphan.is_file()

    assert await store.commit(
        FakeContext(_state_payload("retry")), loaded_generation=1, session_id="s3"
    )
    assert store.status().generation == 2
    assert _published_cookies(store) == [{"name": "retry", "value": "1"}]


async def test_orphan_pruning_keeps_only_the_current_and_previous_generation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    for generation, marker in enumerate(("a", "b", "c", "d"), start=0):
        assert await store.commit(
            FakeContext(_state_payload(marker)),
            loaded_generation=generation,
            session_id=f"s{generation}",
        )
    names = sorted(path.name for path in store.store_dir.glob("state-*.json"))
    assert names == ["state-00000003.json", "state-00000004.json"]


async def test_nothing_is_pruned_when_the_manifest_publish_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    await store.commit(FakeContext(_state_payload("one")), loaded_generation=0, session_id="s1")
    await store.commit(FakeContext(_state_payload("two")), loaded_generation=1, session_id="s2")
    original = _atomic_replace_json

    def boom(target: Path, payload: object) -> None:
        if target.name == "current.json":
            raise OSError("nope")
        original(target, payload)  # type: ignore[arg-type]

    monkeypatch.setattr(
        "applyuminati.plugins.browsers.playwright_persistence._atomic_replace_json",
        boom,
    )
    with pytest.raises(OSError, match="nope"):
        await store.commit(
            FakeContext(_state_payload("three")), loaded_generation=2, session_id="s3"
        )
    names = sorted(path.name for path in store.store_dir.glob("state-*.json"))
    assert "state-00000001.json" in names
    assert "state-00000002.json" in names
    assert "state-00000003.json" in names


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _publish(store: StorageStateStore, *, generation: int, state: object | None = None) -> None:
    store.store_dir.mkdir(parents=True, exist_ok=True)
    filename = f"state-{generation:08d}.json"
    if state is None:
        state = EMPTY_STATE
    (store.store_dir / filename).write_text(json.dumps(state))
    (store.store_dir / "current.json").write_text(
        json.dumps(
            {
                "version": 1,
                "generation": generation,
                "state_file": filename,
                "updated_at": "2026-01-01T00:00:00+00:00",
                "writer_session_id": "seed",
            }
        )
    )


async def test_a_corrupt_manifest_fails_closed_and_is_not_treated_as_absent(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.store_dir.mkdir()
    (store.store_dir / "current.json").write_text("{not json")
    status = store.status()
    assert status.state_exists is False
    assert status.readable is False
    assert status.generation == 0
    with pytest.raises(ConfigurationError) as raised:
        await store.load()
    assert raised.value.code == "browser.storage_state_manifest_invalid"
    skipped = await store.commit(
        FakeContext(_state_payload("x")), loaded_generation=0, session_id="fresh"
    )
    assert skipped is False
    assert (store.store_dir / "current.json").read_text() == "{not json"


async def test_a_missing_referenced_state_is_unreadable_but_keeps_the_generation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _publish(store, generation=4)
    (store.store_dir / "state-00000004.json").unlink()
    status = store.status()
    assert status.state_exists is False
    assert status.readable is False
    assert status.generation == 4
    with pytest.raises(ConfigurationError) as raised:
        await store.load()
    assert raised.value.code == "browser.storage_state_invalid"


async def test_a_corrupt_referenced_state_is_unreadable_but_keeps_the_generation(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    _publish(store, generation=3, state={"cookies": "nope"})
    status = store.status()
    assert status.state_exists is False
    assert status.readable is False
    assert status.generation == 3
    with pytest.raises(ConfigurationError) as raised:
        await store.load()
    assert raised.value.code == "browser.storage_state_invalid"


@pytest.mark.parametrize(
    "manifest",
    [
        {
            "version": 1,
            "generation": 1,
            "state_file": "../../secret.json",
            "writer_session_id": "x",
        },
        {
            "version": 1,
            "generation": 1,
            "state_file": "state-00000002.json",
            "writer_session_id": "x",
        },
        {
            "version": 99,
            "generation": 1,
            "state_file": "state-00000001.json",
            "writer_session_id": "x",
        },
        {
            "version": 1,
            "generation": 1,
            "state_file": "subdir/state-00000001.json",
            "writer_session_id": "x",
        },
    ],
)
async def test_a_hostile_or_unknown_manifest_is_rejected(
    tmp_path: Path, manifest: dict[str, object]
) -> None:
    store = _store(tmp_path)
    store.store_dir.mkdir()
    (store.store_dir / "current.json").write_text(json.dumps(manifest))
    status = store.status()
    assert status.readable is False
    assert status.state_exists is False
    assert status.generation == 0
    with pytest.raises(ConfigurationError) as raised:
        await store.load()
    assert raised.value.code == "browser.storage_state_manifest_invalid"


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
async def test_store_files_are_owner_readable_only(tmp_path: Path) -> None:
    store = _store(tmp_path)
    await store.commit(FakeContext(EMPTY_STATE), loaded_generation=0, session_id="s")
    assert (store.store_dir.stat().st_mode & 0o777) == 0o700
    state = store.store_dir / "state-00000001.json"
    manifest = store.store_dir / "current.json"
    assert (state.stat().st_mode & 0o777) == 0o600
    assert (manifest.stat().st_mode & 0o777) == 0o600
