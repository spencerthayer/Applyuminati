---
name: Playwright persistence config
overview: Make Playwright's storage-state persistence crash-safe and deterministic under concurrent sessions (immutable generation files behind an atomically replaced manifest), add proxy and browser-binary launch configuration, and keep configured paths and proxy credentials out of the public settings API.
todos:
  - id: downloads-root
    content: Add optional top-level download-root override while preserving Settings.downloads_dir as the resolved Path interface; expanduser and resolve relative overrides against data_dir
    status: completed
  - id: persistence-module
    content: Add playwright_persistence.py with StorageSnapshot and StorageStateStore using immutable generation files plus an atomically replaced current manifest
    status: completed
  - id: migration-recovery
    content: Make legacy state migration idempotent across interruption points; manifest wins once published; legacy backup cleanup is non-authoritative
    status: completed
  - id: wire-store
    content: Own one StorageStateStore in PlaywrightBackend; snapshot generation at open_session and commit generation-aware state on close
    status: completed
  - id: stale-tests
    content: Test concurrent commits, stale writers, sequential generations, first run, malformed manifest/state, interrupted commits, permissions, and orphan recovery
    status: completed
  - id: launch-settings
    content: Add secret-safe PlaywrightProxySettings plus playwright_channel and playwright_executable_path with conflict validation
    status: completed
  - id: launch-builder
    content: Centralize Playwright launch kwargs in _launch_options and unit-test without starting Chromium
    status: completed
  - id: public-settings
    content: Rebuild public browser settings explicitly so proxy credentials, storage paths, and executable paths never leak through public_dict()
    status: completed
  - id: capability-tests
    content: Test configured-first-run PERSISTENT_LOGIN semantics and launch/persistence health booleans
    status: completed
  - id: docs
    content: Document persistence commit semantics, proxy/binary selection, generic download root, secrets, and Docker path interpretation
    status: completed
isProject: false
---

# PR #8: Playwright persistence and browser launch configuration

## Preconditions

PR #7 merged at `1a7fba4`, which is where `origin/main` sits. The local branch `Playwrightpersistenceconfig` is already based on that commit and carries the two plan commits, so no rebase is needed, only a rename:

```bash
git branch -m playwright-persistence-config
```

Re-read every file before editing. Line numbers cited below come from the PR #7 branch and may have shifted in the squash.

## What already holds (do not redo)

- The static descriptor already omits `PERSISTENT_LOGIN` ([playwright_backend.py:1396](src/applyuminati/plugins/browsers/playwright_backend.py)); only runtime `_metadata(settings)` adds it. `select_browser()` and host `advertise_backends()` both read runtime `.metadata`, so a config-dependent capability is not statically advertised.
- The current rule is "path configured", not "file exists", which is the interpretation this PR keeps.
- Checkpoints already omit the storage-state path. Preserve.
- `tests/test_docs_consistency.py` asserts maturity and registry facts only. New settings will not break it; do not change maturity.

## 1. Configurable download root (generic, not Playwright)

`Settings.downloads_dir` stays a property. Add an override field beside it, so the derived-path API does not become mutable configuration state:

```python
downloads_path: Path | None = None

@property
def downloads_dir(self) -> Path:
    if self.downloads_path is None:
        return self.data_dir / "downloads"
    path = self.downloads_path.expanduser()
    return path if path.is_absolute() else self.data_dir / path
```

Relative overrides resolve against `data_dir`, never CWD, which is not stable in a worker. `ensure_directories()` and `PlaywrightSession.download` are unchanged; the return type stays `Path`.

Env name follows the normal prefix rule: `APPLYUMINATI_DOWNLOADS_PATH`. No `validation_alias` and no second `DOWNLOADS_DIR` spelling. There is no public configuration contract to preserve yet, so maintaining two names buys nothing.

No `BrowserSettings` download field. Backends consume `settings.downloads_dir` only.

## 2. Storage state: immutable generations behind an atomic manifest

A mutable state file plus a mutable metadata sidecar cannot hold the guarantee. If the state replace lands and the sidecar write dies, the published state has advanced while the recorded generation has not, and the next writer looks current when it is stale. The fix is to make publication itself the atomic step.

New `src/applyuminati/plugins/browsers/playwright_persistence.py` (plugins layer; import-linter permits `plugins -> browser -> core`, and these are Playwright semantics).

### Resolving the configured path

Resolve `browser.playwright_storage_state` deterministically before constructing `StorageStateStore`. Apply `expanduser()`. If the configured path is relative, resolve it against `data_dir`, not the current working directory. Store and session code consume only this resolved path. Do not change the external setting name.

Prefer a root-settings helper over teaching the Playwright plugin about `data_dir` path policy:

```python
@property
def playwright_storage_state_path(self) -> Path | None:
    configured = self.browser.playwright_storage_state
    if configured is None:
        return None
    path = configured.expanduser()
    return path if path.is_absolute() else self.data_dir / path
```

Then `StorageStateStore(settings.playwright_storage_state_path)` rather than `settings.browser.playwright_storage_state` directly. This matters more now that the path controls an on-disk persistence store: a relative path must not depend on worker CWD, and it makes the Docker path documentation coherent, since both the custom executable and the persistence path then have deterministic process-local meanings.

```mermaid
flowchart TD
    configured["configured storage path"] --> store[StorageStateStore]
    store --> manifest["current.json<br/>authoritative generation"]
    store --> states["immutable state-N.json"]
    store --> lock["generation lock"]
    manifest --> states
    lock --> skip["stale writer skips"]
```

### Layout

`store_dir = configured.with_name(configured.name + ".d")`, created `0o700` when Applyuminati creates it:

```
playwright-state.json.d/
    current.json            # the only mutable file
    state-00000003.json     # immutable, 0600
    state-00000004.json
```

```json
{ "version": 1, "generation": 4, "state_file": "state-00000004.json",
  "updated_at": "...", "writer_session_id": "..." }
```

`version` costs nothing now and gives a later persistence migration an explicit hook instead of shape-sniffing. An unknown future version is a named error, not a guess.

### API

```python
@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    path: Path | None      # handed to new_context(storage_state=...); None on first run
    generation: int

@dataclass(frozen=True, slots=True)
class StorageStateStatus:
    state_exists: bool
    readable: bool
    generation: int

class StorageStateStore:
    def __init__(self, configured: Path) -> None: ...
    def status(self) -> StorageStateStatus: ...          # never raises
    async def load(self) -> StorageSnapshot: ...         # raises on corruption
    async def commit(
        self,
        context,
        *,
        loaded_generation: int,
        session_id: str,
    ) -> bool: ...
```

There is no `configured` field. A store only exists when `playwright_storage_state` is set, so from inside one the answer is always `True`, and when persistence is off there is no store to ask.

`load()` is async because legacy migration takes the store lock.

`status()` must never raise. It backs Playwright `health()` only, and a corrupt manifest must not turn a health probe into an exception. `load()` remains the single operation that raises the named configuration error, at the point where a session actually needs the state.

Runtime `_metadata()` determines `PERSISTENT_LOGIN` solely from whether `playwright_storage_state` is configured, using the existing rule. `_metadata()` must not read the persistence manifest, inspect the legacy file, or call `StorageStateStore.status()`. A corrupt persistence store does not remove the backend's declared ability to persist login state; it causes `health()` to degrade and `open_session()` to fail closed until repaired. Routing capability computation through the filesystem would also invite someone later to make `PERSISTENT_LOGIN` contingent on `state_exists` or `readable`.

`readable` covers the whole persistence chain, not just the manifest. A valid manifest pointing at a corrupt state file is unhealthy persistence and must not report otherwise. `status()` runs the same conservative state validation as `load()`, inspects the legacy configured path the same way `load()` would, and converts every problem into a value:

- Persistence unconfigured: no store exists, so there is nothing to call
- No manifest, no legacy state: `state_exists=False, readable=True, generation=0`
- No manifest, valid legacy state: `state_exists=True, readable=True, generation=0`
- No manifest, invalid legacy state: `state_exists=False, readable=False, generation=0`
- Valid manifest, valid referenced state: `state_exists=True, readable=True, generation=N`
- Invalid or unreadable manifest: `state_exists=False, readable=False, generation=0`
- Valid manifest, referenced state missing or invalid: `state_exists=False, readable=False, generation=N`

The legacy rows cover the upgrade window after PR #7 and before the first `load()` migrates. `generation=0` alongside `state_exists=True` reads as usable legacy state not yet imported into the generation system, which keeps health truthful before any Playwright session has run.

The last row keeps the generation because it is known and useful for diagnosis even though the state behind it is unusable.

`StorageStateStore.status()` is the only implementation that may interpret persisted Playwright manifest and state files. Runtime metadata and `Settings.public_dict()` may determine only whether persistence is *configured*, from the setting itself. Playwright health is the only reporting surface that inspects persistence runtime state, and it does so through `status()`. Do not duplicate manifest parsing or state-file checks in settings, metadata, or health code, and in particular do not stat the configured path from those callers, because after migration it no longer exists. `status()` itself is of course allowed to inspect both the manifest and the legacy configured path; the prohibition is on callers doing it independently.

Callers split the two questions:

```python
persistence_configured = self._storage_store is not None
status = self._storage_store.status() if self._storage_store is not None else None
```

`public_dict()` is the exception, and it resolves by scope rather than by an entry point. `core` is forbidden from importing `plugins` by the "Core domain is vendor-neutral" contract in [pyproject.toml](pyproject.toml), so `core/settings.py` cannot reach the store at all, and must not grow its own manifest reader to compensate. `public_dict()` therefore reports configuration only, via `persistent_login_configured`. Whether valid state exists is a runtime fact and belongs to backend health, which lives in the plugins layer and can call `status()` directly.

### One manifest reader

`load()`, `status()`, and the `commit()` generation check all need to parse `current.json` and validate its referenced path. Three independent readings will drift, most likely leaving `status()` more permissive than `load()`. Put the parse plus validation in a single private helper returning a discriminated result, and let each caller decide what to do with a failure:

```python
def _read_manifest(self) -> ManifestAbsent | ManifestValid | ManifestProblem: ...
```

Three result states, not two. The helper must distinguish an absent manifest from a broken one, because they lead to opposite decisions:

- `ManifestAbsent`: `load()` takes the legacy-migration or first-run branch, `status()` reports `readable=True`, `commit()` treats it as generation 0.
- `ManifestValid`: normal path.
- `ManifestProblem`: `load()` raises the named error, `status()` reports `readable=False`, `commit()` skips the write, logs, and returns `False`.

`commit()` may treat only `ManifestAbsent` as generation 0. It must never treat `ManifestProblem` as absent. A first-run session holds `loaded_generation=0`, so collapsing the two would let it publish generation 1 straight over a manifest that a person still needs to inspect.

### Atomic write helpers

State publication and manifest publication need identical durability, but they differ in who writes the bytes: Applyuminati serializes the manifest, while Playwright writes the state file itself through `context.storage_state(path=...)`. Two primitives cover both without either path getting weaker semantics:

```python
def _atomic_replace_bytes(target: Path, payload: bytes) -> None:
    """Create a uniquely named sibling temp file in target.parent, write and
    fsync it, chmod 0600 on POSIX, atomically replace target, fsync
    target.parent, and remove the unpublished temp on failure where possible."""

def _publish_temp_file(temp: Path, target: Path) -> None:
    """Require temp.parent == target.parent. Fsync and chmod the already-written
    temp, atomically replace target, fsync the parent directory, and remove the
    unpublished temp on failure where possible."""

def _atomic_replace_json(target: Path, payload: Mapping[str, object]) -> None:
    """Serialize and delegate to _atomic_replace_bytes()."""
```

Usage is fixed:

- Manifest: `_atomic_replace_json(current_json, manifest)`.
- Playwright state: `await context.storage_state(path=temp)`, then `_publish_temp_file(temp, generation_target)`.

Do not read the Playwright-generated state file back into memory just to push it through `_atomic_replace_bytes()`.

Temp and target always share a directory, so `os.replace` cannot cross a filesystem boundary. `_atomic_replace_bytes()` guarantees that itself by creating the temp in `target.parent`; only `_publish_temp_file()` requires it of its caller, since the caller handed Playwright the temp path.

Both helpers must also:

1. keep the temp name unique, so two concurrent writers cannot collide on it;
2. flush where the helper itself owns the open file;
3. `os.fsync()` the file before replacement;
4. apply `0o600` on POSIX before replacement;
5. use `os.replace()`;
6. fsync the containing directory after replacement;
7. clean up an unpublished temp file on failure where possible.

### load()

Authority rule, applied in this order:

- **Manifest exists and is valid: the manifest wins.** Return its generation and referenced path. Never re-import a legacy file just because one is still lying around.
- Manifest exists but is unreadable, non-JSON, or fails validation: raise `ConfigurationError(code="browser.storage_state_manifest_invalid")`. Do not guess a generation. Silently downgrading damaged concurrency metadata discards the exact safety property this PR adds; a repair CLI can come later.
- Manifest references a state file that is missing or invalid: `browser.storage_state_invalid`.
- **No manifest, but the configured path exists as a regular file** (state written by PR #7 or earlier): migrate it, see below.
- Nothing exists: `StorageSnapshot(path=None, generation=0)`. First run opens clean, as PR #7 established; the first successful commit becomes generation 1.

Errors carry the filename and the parse error, never file contents.

### Legacy migration, idempotent across interruption

Migration runs under the store lock and must survive being killed at any point:

1. Validate the legacy file. Invalid means `browser.storage_state_invalid`, not a silent clean start.
2. **Copy** it to `state-00000001.json` with `_atomic_replace_bytes()`. Copy, never move: the original must stay readable until `current.json` commits, so a crash mid-migration leaves the legacy file as the only authoritative state and the retry can start over from it. **Overwriting an existing `state-00000001.json` is expected and correct** on a retry after a crash: no manifest published it, so nothing references it. Do not fail because the file already exists.
3. Publish `current.json` at generation 1. This is the commit point.
4. Cleanup only: rename the legacy file to `<configured>.imported` so nobody keeps editing a file that is no longer read, and log `playwright.storage_state_legacy_imported`.

Step 4 is not part of the commit. If it fails or the process dies first, the manifest is already authoritative and the next `load()` takes the manifest branch and ignores the leftover legacy file.

`.imported` collisions must not overwrite an earlier backup. Suffix with a timestamp or ULID, and if the rename still cannot be performed, leave the legacy file in place and log that cleanup was skipped. No correctness depends on moving it.

### Manifest validation

State validation stays conservative because Playwright owns that schema; being stricter would reject states Playwright accepts:

```python
def _validate_storage_state(value: object) -> None:
    # top-level object, cookies list, origins list. Nothing deeper.
```

Manifest validation is stricter because Applyuminati owns that schema:

- `version` is a known schema version; an unrecognized one is a named error, not a best-effort read
- `generation` is an `int` and `>= 1`
- `state_file` is a bare basename equal to `f"state-{generation:08d}.json"`
- `writer_session_id` is a string
- the resolved state path is inside `store_dir`

The last two checks matter: corruption or hand-editing must not turn `{"state_file": "../../something"}` into a path traversal primitive, even though the manifest is local rather than remote-controlled.

### commit()

Under `asyncio.Lock`:

1. Read `current_generation` from the manifest (0 when `ManifestAbsent`), and let `new_generation = current_generation + 1`.
2. If `current_generation` differs from `loaded_generation`: skip, `log.warning("playwright.storage_state_stale_write_skipped", session_id=..., loaded_generation=..., current_generation=...)`, return `False`. No cookie merging. A skipped write is a persistence conflict, not a browser failure.
3. Otherwise write the candidate state and publish it to `state-{new_generation:08d}.json` with `_publish_temp_file()`. **That filename may already exist as an orphan from an interrupted prior commit, and replacing it is correct.** Authority comes from the manifest, never from generation-file existence, so an unreferenced next-generation file is neither a committed generation nor a stale-write signal. This matches the legacy-migration retry rule.

   Before calling `context.storage_state()`, create a unique sibling pathname in `store_dir` using `tempfile.mkstemp()` or an equivalent collision-safe primitive, and close the returned file descriptor before passing the pathname to Playwright. The temp name must not match `state-*.json` or `current.json`, so orphan pruning can never mistake an unpublished temp for a generation; something like `.playwright-state-<random>.tmp` works. `_publish_temp_file()` owns cleanup after Playwright has written it. If `context.storage_state()` itself raises, the caller removes the temp path before propagating the error.
4. Publish the manifest onto `current.json`. **This is the commit point.**
5. Only now prune. Never before step 4 lands. Retain exactly the files referenced by `new_generation` and, when it exists, `new_generation - 1`. Best-effort delete all other unreferenced `state-*.json` files, suppressed on failure. Never infer authority from filenames; `current.json` remains authoritative.

A crash before step 4 leaves an unreferenced generation file and the previous generation authoritative, so nothing was published and a later writer at the same generation is correctly not stale. A crash after it leaves a manifest whose referenced file was already renamed into place. The state write no longer publishes anything on its own, which is what closes the window the sidecar design left open.

Cross-process coordination is out of scope: one backend owns the store, and the lock is in-process.

## 3. Wire the store through the backend

`PlaywrightBackend.__init__` builds one `StorageStateStore | None` from `settings.playwright_storage_state_path`, never from the raw `settings.browser.playwright_storage_state`, shared by every session so the lock means something. `open_session` awaits `store.load()`, which is where manifest and state errors surface before a context exists, replacing `_storage_state_to_load()` ([playwright_backend.py:1295](src/applyuminati/plugins/browsers/playwright_backend.py)).

`PlaywrightSession` gains `loaded_storage_generation` and a store reference. `_save_storage_state` ([playwright_backend.py:1134](src/applyuminati/plugins/browsers/playwright_backend.py)) delegates to `store.commit(...)` and loses its last-writer-wins docstring. The session still closes normally when a commit is skipped.

## 4. Launch configuration

New on `BrowserSettings` ([settings.py:102](src/applyuminati/core/settings.py)):

```python
class PlaywrightProxySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    server: str
    username: SecretStr | None = None   # account identifiers are sensitive too
    password: SecretStr | None = None
    bypass: str | None = None

playwright_proxy: PlaywrightProxySettings | None = None
playwright_channel: str | None = None
playwright_executable_path: Path | None = None
```

### Settings-time validation

- channel and executable_path both set: "Configure either playwright_channel or playwright_executable_path, not both."
- empty or whitespace channel rejected.
- `server` must parse as `scheme://host[:port]`. Accept `http`, `https`, and `socks5`; Playwright documents HTTP(S) and SOCKSv5 proxy support. The value **must carry no URI userinfo**. Reject a parsed URL with a `username` or `password` component, so `http://user:pass@proxy.example.com:8080` cannot smuggle credentials past the `SecretStr` fields and into serialization, validation error messages, or logs. Credentials come only from the dedicated fields.
- No connectivity check during settings validation.
- `playwright_executable_path` is normalized, not merely checked: call `expanduser()`, reject the result if it is not absolute, and store the expanded absolute `Path` back into `BrowserSettings`. All later code, including `_launch_options()`, consumes only that normalized value. Do not resolve symlinks and do not require the file to exist during settings validation. A CWD-relative binary path means something different in a worker, a container, a test, and a service process.

### Launch-time validation

Do not stat the executable during `Settings()` construction. A machine may legitimately carry Playwright configuration while running only Ego Lite, and `applyuminati capabilities` should stay meaningful when the configured binary is absent.

Check existence and regular-file-ness in `_launch_options()` or immediately before launch, raising `BackendUnavailableError(code="browser.playwright_binary_missing")` rather than surfacing a raw Playwright traceback.

### Options builder

Centralize kwargs in a module-level `_launch_options(settings) -> dict[str, object]` producing `headless`, `proxy`, `channel`, `executable_path`, consumed by `_ensure_browser` ([playwright_backend.py:1327](src/applyuminati/plugins/browsers/playwright_backend.py)). Proxy is a launch option, not per-context. Both `SecretStr` values are unwrapped only inside this helper. No stealth, fingerprint, or user-agent flags.

Omit keys whose value is `None` rather than passing explicit nulls. Assertions become exact key-set comparisons instead of "present but `None`" checks, and nothing depends on Playwright treating `channel=None` the same as an absent `channel`. The same applies inside the proxy dict for `username`, `password`, and `bypass`.

Nested env names use the repository's `__` delimiter: `APPLYUMINATI_BROWSER__PLAYWRIGHT_CHANNEL`, `APPLYUMINATI_BROWSER__PLAYWRIGHT_EXECUTABLE_PATH`, `APPLYUMINATI_BROWSER__PLAYWRIGHT_PROXY__SERVER`.

## 5. Keep secrets and paths out of the public settings API

`public_dict()` ([settings.py:391](src/applyuminati/core/settings.py)) currently dumps the whole `browser` subtree. Once proxy credentials and configured paths live there, `SecretStr` serialization alone is not an adequate contract. Rebuild the section explicitly, the way `security` already is:

```python
payload = self.model_dump(
    mode="json",
    exclude={"llm", "email", "security", "browser", "downloads_path"},
)
payload["browser"] = {
    "preferred": ..., "headless": ..., "navigation_timeout_seconds": ...,
    "capture_artifacts": ...,
    "persistent_login_configured": ...,
    "proxy_configured": ..., "channel": ...,
    "custom_executable_configured": ..., "ego_lite_binary_configured": ...,
}
```

Every value here is derived from settings alone. `persistence_state_exists` is deliberately absent: it needs the manifest, `core` cannot import `plugins`, and it belongs to backend health rather than to a configuration dump.

`downloads_path` is a new top-level field, so it must be excluded explicitly or the rebuilt browser subtree would not save it. Do not add the resolved download root elsewhere in the public payload. If the UI later needs to know whether it is customized, expose only `custom_downloads_path_configured: bool`, and only if there is a real consumer. Otherwise expose nothing.

No proxy username or password, no storage-state path, no executable path, no ego workspace path.

This drops absolute paths the API returns today, so check the web UI and tests for consumers of `public_dict()["browser"]` before landing it.

Lock the contract with an exact key-set assertion so a future `playwright_api_token` cannot become public by accident:

```python
assert set(public["browser"]) == {
    "preferred", "headless", "navigation_timeout_seconds", "capture_artifacts",
    "persistent_login_configured", "proxy_configured",
    "channel", "custom_executable_configured", "ego_lite_binary_configured",
}
```

## 6. Capability semantics and health

State the definition in code and docs so the first-run case is not relitigated:

> `PERSISTENT_LOGIN` means the backend is configured to preserve and restore authentication state between contexts and runs. It does not mean valid authenticated state already exists.

The runtime rule is unchanged. Health details carry exactly these keys:

```
persistence_configured
persistence_state_exists
persistence_readable
persistence_generation
proxy_configured
channel_configured
custom_executable_configured
```

`persistence_configured` is `self._storage_store is not None`. The three other persistence values come from `status()`, which health can call because it lives in the plugins layer. The four states are:

- Not configured: `configured=False, state_exists=False, readable=True, generation=0`
- Configured, first run: `configured=True, state_exists=False, readable=True, generation=0`
- Configured, healthy state: `configured=True, state_exists=True, readable=True, generation=N`
- Configured, corrupt persistence: `configured=True, state_exists=False, readable=False, generation=<known generation or 0>`

`readable=True` when unconfigured is deliberate: nothing is broken, there is simply nothing to read, and conflating that with corruption would make an ordinary Ego Lite machine look damaged.

If `HealthReport` supports a degraded status, `persistence_readable=False` makes Playwright health degraded rather than backend-unavailable, because the Playwright runtime and browser binary may still be fine. It does not soften session behavior. While `playwright_storage_state` is configured and unreadable, every `open_session()` must fail through `StorageStateStore.load()` rather than quietly opening a clean context. The operator repairs the persistence store or explicitly unsets the setting. Do not bypass corrupt configured state on the theory that a particular workflow looks like it does not need login persistence: `open_session()` has no per-session opt-out, and guessing would silently run an unauthenticated application attempt.

Never the proxy password or any absolute path. Maturity unchanged.

## 7. Documentation

- [docs/execution-architecture.md](docs/execution-architecture.md) section 7: replace the "last writer wins" paragraph from PR #7 with the manifest commit model, the stale-writer skip, and the legacy migration.
- [ARCHITECTURE.md](ARCHITECTURE.md): browser paragraph.
- [.env.example](.env.example): the browser block plus the new download-root entry, with the `__` nesting spelled correctly.
- State that the state files hold live session cookies, that channel and executable_path select a binary and confer no user profile, and that Playwright still cannot hand a browser to a person.
- State that `playwright_executable_path` is interpreted inside the process or container running Playwright, so a macOS host path configured for a Linux container will not resolve. Browser Host Playwright will carry its own settings environment later.
- State that proxy credentials belong in the dedicated fields, not in the server URL.

## Test matrix

Offline, in a new `tests/test_playwright_persistence.py` plus `tests/test_browser_capabilities.py` and the settings tests:

**Concurrency and generations**

- Against the mechanism rather than a simulation: `await asyncio.gather(store.commit(a, loaded_generation=1, ...), store.commit(b, loaded_generation=1, ...))` returns exactly one `True`.
- Stale writer both ways: A and B load generation 1; whoever commits first wins and the other is skipped, with the surviving state matching the winner.
- Sequential: generations 1 to 2 to 3, every commit succeeds.
- First run: nothing on disk, clean open, close creates generation 1.

**Migration and recovery**

- Legacy import: a plain PR #7 state file becomes generation 1 and is renamed to `.imported`.
- Before migration, `status()` on a valid legacy file reports `state_exists=True, readable=True, generation=0`, and on a corrupt legacy file reports `state_exists=False, readable=False, generation=0`.
- Interrupted mid-migration: `state-00000001.json` present, no manifest, legacy file still present. The retry succeeds and overwrites the orphan rather than failing.
- Interrupted after publication: manifest present, legacy file un-renamed. The manifest wins; no second import.
- `.imported` collision: an existing backup is not overwritten, and failure to rename does not fail the load.
- Interrupted commit: manifest replace raises; the previous generation stays authoritative, the orphan file is unreferenced, and the next commit succeeds by replacing that orphan rather than tripping over it.
- Orphan pruning: after several commits only the current and previous `state-*.json` remain, and nothing is pruned when the manifest publish fails.
- Two concurrent candidate-state writes use distinct temporary paths, and a failure from `context.storage_state()` leaves no `.tmp` file behind.

**Validation and safety**

- A corrupt manifest, a manifest whose referenced state is missing, and a manifest whose referenced state is corrupt each raise the appropriate named error from `load()`.
- All three leave `status()` returning `readable=False, state_exists=False` without raising, with `generation` retained in the two cases where the manifest itself parsed.
- A corrupt manifest also makes `commit()` skip rather than publish over it, including from a `loaded_generation=0` session.
- The documented health states each produce their exact `persistence_*` values, including `persistence_readable=True` when persistence is unconfigured.
- Fail-closed: with corrupt configured persistence, `open_session()` raises rather than opening a clean context, even though `health()` reports degraded rather than unavailable.
- Manifest with a non-basename `state_file`, a mismatched generation, a traversing path, or an unknown `version` is rejected.
- Permissions: state files `0o600` and the store directory `0o700` on POSIX, skipped on Windows.
- Disabled: no path configured, open and close write nothing, no `PERSISTENT_LOGIN`.

**Configuration**

- `_launch_options`: default, channel only, executable only, proxy server only, proxy with credentials, each asserted as an exact key set so unset options are absent rather than `None`. No Chrome or Edge install needed.
- Settings validation: channel plus executable_path, blank channel, blank/schemeless proxy server, proxy server carrying userinfo, relative executable path.
- `playwright_storage_state_path` resolution: `~/state.json` is expanded, `state.json` becomes `<data_dir>/state.json`, and `/absolute/state.json` is unchanged.
- `playwright_executable_path` normalization asserts the stored value, not just that construction succeeded: `BrowserSettings(playwright_executable_path="~/browser").playwright_executable_path == Path("~/browser").expanduser()`.
- Missing executable file raises `browser.playwright_binary_missing` at launch, not at `Settings()` construction.
- `public_dict()` does not contain `downloads_path`, the resolved `downloads_dir`, the Playwright storage path, executable path, Ego paths, proxy username, or proxy password.
- `public_dict()["browser"]` matches the exact permitted key set and carries no credentials or paths. Before locking that set, grep the web UI for the browser fields it actually reads and confirm each survives; the key-set assertion is a contract, so anything omitted here breaks the UI silently at runtime rather than loudly in CI.
- `PERSISTENT_LOGIN` present when the path is configured and the file is absent; absent when unconfigured.

Browser-marked, in `tests/test_playwright_sessions.py`: two real sessions closing concurrently against Chromium with one commit winning; a custom download root honored with `relative_path` still relative and traversal protection intact. Do not weaken PR #7's download security tests.

## Out of scope

`BrowserRequirements`, `PUBLIC_FORM_APPLICATION`, wiring `select_browser()` into attempt execution, Docker-local execution, Browser Host tab and download dispatch, ATS plugins, maturity promotion, cookie merging, cross-process locking, a repair CLI, and anything resembling stealth or anti-blocking.

## Commits

1. download-root override preserving the `downloads_dir` property
2. `playwright_persistence.py`: manifest, immutable generations, atomic helpers, commit
3. idempotent legacy migration and recovery tests
4. wire the store into backend and session, remove last-writer-wins
5. concurrency, stale-writer, interrupted-commit and orphan tests
6. proxy, channel, executable_path settings with validation
7. `_launch_options` helper and unit tests
8. explicit public browser settings with the key-set contract test, plus permissions and capability tests
9. docs and `.env.example`

## Validation

`uv run ruff format`, `ruff check`, `pyright`, `lint-imports`, full `pytest`, and the browser-marked suite. The pre-existing local `test_ego_lite.py::test_a_backend_without_the_helper_is_not_selectable` failure only happens on a machine with `ego-browser` installed and passes in CI.
