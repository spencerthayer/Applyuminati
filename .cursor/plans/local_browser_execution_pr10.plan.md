---
name: Local browser execution
overview: Make the persisted selection from PR #9 executable: a process-owned LocalBrowserManager turns a PENDING attempt with browser_backend="playwright" into a live local session driven through run_step(), and the Browser Host gains the tab/download dispatch PR #7 modeled. Host-backed sessions remain host-owned; local Playwright sessions remain process-owned; no silent fallback when a contract exceeds what the selected backend offers.
todos:
  - id: manager
    content: Add LocalBrowserManager owning one process-wide PlaywrightBackend keyed by selected backend slug with honest restart reconstruction
    status: pending
  - id: acquire
    content: In _run, after selection persisted, acquire the local session through the manager and advance to run_step
    status: pending
  - id: ownership
    content: Keep host-backed sessions host-owned; record which ownership a session has on the attempt
    status: pending
  - id: host-dispatch
    content: Enable Browser Host tab and download commands for host-backed sessions per the PR #7 protocol model
    status: pending
  - id: no-fallback
    content: Refuse rather than substitute when requirements exceed the selected backend; requirement-aware failure only
    status: pending
  - id: tests
    content: Manager lifecycle, acquisition from persisted decision, ownership boundaries, no-fallback refusals, host dispatch
    status: pending
  - id: docs
    content: Document the ownership rule and acquisition path in execution-architecture.md
    status: pending
isProject: false
---

# PR #10: Local browser execution manager and Browser Host tab/download dispatch

## The seam PR #9 left

After PR #9 an unbound attempt with a satisfiable contract parks at `PENDING`
with `browser_backend` and the requirements snapshot persisted. This PR makes
that decision executable:

```
PENDING attempt
    + browser_backend = "playwright"
    + browser_requirements snapshot
          |
          v
   LocalBrowserManager (process-owned, one backend per slug)
          |
          v
   process-owned PlaywrightBackend session
          |
          v
   run_step() advances the attempt
```

## Rules

- **Ownership.** Host-backed sessions stay host-owned (Browser Host protocol,
  ego workspaces, resume identity). Local Playwright sessions are process-owned
  and reconstructed honestly after a restart: if the process died, the session
  is gone and the attempt says so rather than pretending to re-enter.
- **No silent fallback.** The requirements snapshot is re-checked against the
  selected backend's metadata before acquisition. A contract the backend does
  not satisfy refuses with the typed error; nothing substitutes another
  backend. Selection remains a PR #9 decision; acquisition never reselects.
- **`host/client.py::open_local_session()` stays out of the worker path.** The
  manager is the process-wide owner for the application worker; the client
  helper keeps its existing narrow meaning.
- **Host dispatch.** Tab and download commands already modeled by PR #7
  (`openOrReuseTab`, `listTabs`, `closeTab`, `download`) are dispatched for
  host-backed sessions whose backend advertises the capability, with the
  host-side stripping rules (`HOST_UNDISPATCHABLE_CAPABILITIES`) respected.

## Out of scope

Browser Host as a select_browser() candidate, ego acquisition locally, cookie
merging, Workday/generic drivers, maturity promotion (PR #11).
