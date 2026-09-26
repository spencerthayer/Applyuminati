---
name: Driver browser requirements
overview: Drivers declare the browser contract they need (DriverMetadata.requirements, mandatory), a PUBLIC_FORM_APPLICATION requirement set lets public Greenhouse/Lever flows run on Playwright without pretending Playwright supports human handoff, and the attempt worker consults select_browser() for unbound attempts and persists the execution decision. Local session acquisition itself lands in PR #10.
todos:
  - id: constant
    content: Add PUBLIC_FORM_APPLICATION (required navigate, semantic_snapshot, file_upload; preferred human_handoff, screenshot) beside the existing sets
    status: completed
  - id: metadata
    content: Add required DriverMetadata.requirements with no default; update Greenhouse, Lever, and every in-tree constructor
    status: completed
  - id: wiring
    content: In attempt_tasks._run, unbound attempts with no session resolve the apply-URL driver and run select_browser(); bound attempts resume untouched
    status: completed
  - id: persistence
    content: Persist the chosen backend slug and a requirements snapshot on the attempt (existing browser_backend column plus payload JSON, no migration) and record a browser_selected event
    status: completed
  - id: diagnostics
    content: Replace the generic "Connect the Mac host" session failure with requirement-aware interventions; handoff flag only when HUMAN_HANDOFF is required
    status: completed
  - id: tests
    content: Constant shape, selection admission for non-handoff backends, metadata contract, worker selection/pause/rejection paths, payload round-trip
    status: completed
  - id: docs
    content: Document the driver requirements contract and the PR #10 seam in execution-architecture.md
    status: completed
isProject: false
---

# PR #9: Driver-declared browser requirements and selection wiring

## Decisions (user-confirmed)

1. `PUBLIC_FORM_APPLICATION` is a NEW set, not a rename. `APPLICATION_SUBMISSION`
   (handoff mandatory) and `AUTHENTICATED_APPLICATION` stay as the stronger
   contracts. Public form flows drop the handoff requirement so Playwright is a
   valid backend; handoff moves to `preferred`, and the auth-flavoured
   preferences (`PERSISTENT_SESSION`, `AUTHENTICATED_USER_PROFILE`) are absent
   because a public form has no account.
2. Incremental wiring only. Browser Host registrations are NOT select_browser()
   candidates (that is a later refactor). Bound attempts resume their exact
   session and are never reselected. Local session lifecycle is PR #10; PR #9
   selects and persists the decision only. `host/client.py::open_local_session()`
   is untouched.
3. `DriverMetadata.requirements` is mandatory, no default. Silent defaults
   undermine capability-driven selection; out-of-tree drivers fail loudly at
   construction instead of mis-selecting.
4. Selection comes from the apply URL's driver, never the discovery source.
5. Inability to satisfy `PUBLIC_FORM_APPLICATION` must not auto-produce a
   handoff intervention. `requires_browser_handoff=True` only when
   `HUMAN_HANDOFF` is in the requirements' required set.

## Flow

```
attempt has a bound session / factory
        |-> yes -> bind and run (unchanged; run_step still resolves the driver)
        |-> no
              resolve driver from the apply URL (detect_driver)
              no match   -> non-handoff intervention naming that
              match      -> select_browser(settings, driver.metadata.requirements)
                            backend found  -> persist browser_backend +
                                             requirements snapshot + event;
                                             task returns "pending";
                                             PR #10 owns local acquisition
                            nothing found  -> intervention carrying the exact
                                             per-backend rejections; handoff
                                             flag only when handoff is required
```

## Persistence

- `attempt.browser_backend` (existing `String(64)` column) takes the slug.
- The requirements snapshot lives in the existing `payload` JSON column via
  `browser_requirements: {"required": [...], "preferred": [...]}` on the model;
  no Alembic migration.
- New `AttemptEventKind.BROWSER_SELECTED` records the decision with data.

## Non-goals

Local Playwright session acquisition, Browser Host tab/download dispatch, the
unified host-as-candidate selection refactor, cookie merging, Workday/generic
fallback drivers.
