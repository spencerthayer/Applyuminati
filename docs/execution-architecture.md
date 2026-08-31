# Execution architecture: Browser Host, human handoff, application drivers

Status: living design document for the execution milestone.

Product statement: **automate the job hunt; step in only when the web makes
you.** Applyuminati progresses applications autonomously whenever automation is
possible. When an employer requires a human (authentication, MFA, CAPTCHA,
identity verification, consent, an ambiguous consequential question, a legal
attestation), the system pauses, preserves the exact application state, hands
the interaction to the user, and resumes from that checkpoint.

Applyuminati does not promise unattended submission, and does not attempt to
defeat any access control.

## 1. Audit of the repository before this milestone

Read on `main` at commit `783a56b`. The full validation suite passed:
`ruff format --check`, `ruff check`, `pyright`, `lint-imports` (4 contracts
kept), 38 Python tests, ESLint, `tsc`, 23 Vitest tests, the web build, the
Docker image build and the compose config check.

What was already right and is preserved unchanged:

| Property | Where |
|---|---|
| Structured data owns canonical truth; `AssertionLevel` promotion is gated | `core/provenance.py` |
| Deterministic scoring authoritative, LLM bounded to plus or minus 0.2 | `scoring/engine.py`, `scoring/llm_pass.py` |
| Vendor independence through Protocol plus Registry plus entry points | `core/registry.py` and each contract package |
| Local-first SQLite, portable column types, no Redis/Celery/Kafka | `db/base.py`, `tasks/queue.py` |
| Failures as data with a recovery policy | `core/errors.py`, `tasks/recovery.py` |
| 20-state application lifecycle with an enforced transition table | `core/models/application.py` |
| Semantic browser contract with handoff and checkpoint primitives | `browser/base.py` |
| Layering enforced by import-linter rather than by convention | `pyproject.toml` |

Discrepancies found between the issue text and the repository:

1. The issue says one description claims a 23-state application machine.
   `ApplicationState` has exactly 20 members and `ARCHITECTURE.md` already says
   20. There is nothing to fix, but the count is now asserted by a test
   (`tests/test_docs_consistency.py`) so it cannot drift.
2. Several capabilities the issue asks to introduce already exist:
   `SEMANTIC_SNAPSHOT`, `PERSISTENT_LOGIN`, `HUMAN_HANDOFF`, `FILE_UPLOAD`,
   `MULTI_TAB`, `SCREENSHOT`, `JAVASCRIPT_EVAL`. Missing ones were added rather
   than a parallel enum being created: `PERSISTENT_SESSION`, `DOWNLOADS`,
   `AUTHENTICATED_USER_PROFILE`.
3. Browser selection was preference-order only. It could hand a workflow that
   needs `PERSISTENT_LOGIN` a Playwright session with none. Fixed by
   capability-driven selection.
4. ego lite metadata was stale and partly wrong. Corrections in section 6.
5. `EgoLiteSession.wait_for_control` polled `waitForAgentControl` and then
   called `takeOverTaskSpace` in the same script. The public harness documents
   that `takeOverTaskSpace` has no ownership check and must never be called on
   the agent's own initiative. See section 6.
6. `release.yml` triggered on `push` to `main` independently of `ci.yml`, so a
   commit that failed CI could still publish `latest`. Fixed by `workflow_run`
   gating.
7. The API had no authentication and Docker published the port on all
   interfaces. Fixed in phase 2.

## 2. Two state machines, deliberately separate

`ApplicationState` answers *where is this pursuit in the hiring process*.
`WorkflowState` answers *what is the executor doing right now*. They are
orthogonal and both are needed.

```
Application: SUBMITTED     Workflow: WAITING_FOR_PROVIDER   (verifying evidence)
Application: APPLYING      Workflow: WAITING_FOR_HUMAN      (Workday login)
Application: APPLYING      Workflow: RETRY_SCHEDULED        (transient 503)
```

`WorkflowState` lives in `core/models/execution.py`:

| State | Meaning | Worker held? |
|---|---|---|
| `PENDING` | Attempt created, not started | no |
| `RUNNING` | A worker is executing a step | yes |
| `WAITING_FOR_HUMAN` | A typed intervention is open | **no** |
| `WAITING_FOR_PROVIDER` | Waiting on an external system | no |
| `RETRY_SCHEDULED` | Failed, retry time recorded | no |
| `COMPLETED` | Attempt finished its goal | no |
| `FAILED` | Terminal failure, structured | no |
| `CANCELLED` | User or policy stopped it | no |

`WAITING_FOR_HUMAN` is not a failure. It never enters retry policy and never
counts against `max_attempts`.

## 3. Browser Host

ego lite is a native macOS application. Applyuminati's production image is
Linux. A Linux container cannot use a macOS application, and the fixes that
would appear to make it work (mounting host executables, exposing host shell
execution, running arbitrary `ego-browser` programs from the server) are all
worse than the problem.

The resolution is a small native companion process.

```
                    Applyuminati (Docker, native, or NAS)
              +--------------------------------------+
              | API, React UI, discovery, scoring,   |
              | research, workflow engine,           |
              | questionnaire policy, memory,        |
              | persistence                          |
              +------------------+-------------------+
                                 |
                    Browser Host Protocol (WebSocket)
                                 |
              +------------------v-------------------+
              | applyuminati-browser-host            |
              | native desktop process               |
              | ego lite adapter, Playwright adapter |
              +------------------+-------------------+
                                 |
                              ego lite
                                 |
                        persistent task space
                        /                   \
                     agent                human
```

### 3.1 Direction and transport

The host dials **out** to Applyuminati. The server never needs to discover a
desktop port, which is what makes Docker Desktop, a native install, and a NAS
deployment all work with the same code.

```
applyuminati-browser-host  --->  wss://applyuminati.local/api/v1/browser-host/ws
```

A single WebSocket carries both directions: server commands down, results and
events up. Remote hosts require TLS (`wss://`); the host refuses a plaintext
`ws://` URL unless the target is loopback or the operator passes
`--allow-insecure`.

### 3.2 Registration

On connect, the host sends one `register` frame:

```json
{
  "type": "register",
  "id": "01JB...",
  "protocol_version": 1,
  "host": {
    "host_id": "spencers-mac",
    "display_name": "Spencer's MacBook Pro",
    "platform": "darwin",
    "architecture": "arm64",
    "host_version": "0.1.0",
    "application_version": "0.1.0",
    "backends": [
      {"slug": "ego_lite", "available": true, "preferred": true,
       "version": "1.2.6", "capabilities": ["navigate", "semantic_snapshot", "..."],
       "detail": "ego-browser ready (flat surface)"},
      {"slug": "playwright", "available": false, "preferred": false,
       "detail": "playwright is not installed"}
    ]
  }
}
```

Only fields that serve diagnosis or capability matching are sent. No username,
no home directory, no full environment, no serial number.

The server replies `registered` with the negotiated protocol version and the
host's persisted record id, or closes with a typed error.

### 3.3 Security

The connection drives a real person's authenticated browser, so it is treated
as privileged.

* **Pairing.** `POST /api/v1/browser-hosts/pair` mints a 32-byte
  `secrets.token_urlsafe` credential and stores only a SHA-256 hash plus a
  short non-secret prefix for display. The plaintext is returned once.
* **Authentication.** Every connection presents the credential in the
  `Authorization: Bearer` header. Verification is constant-time.
  Authentication happens before any command can be dispatched.
* **Revocation.** `POST /api/v1/browser-hosts/{id}/revoke` marks the credential
  revoked and closes live connections using it.
* **Separate from human auth.** A browser-host credential cannot call the human
  API, and a UI session cannot open a host socket.
* **Schema validation.** Every frame is validated by a Pydantic model with
  `extra="forbid"`. Unknown command types are refused, not guessed.
* **Replay and staleness.** Command ids are unique per connection. A host
  rejects a duplicate id and refuses a command whose `expires_at` has passed.
* **Session ownership.** A command naming a session the host does not own is
  refused.
* **Timeouts.** Every command carries a deadline. The server fails a pending
  command when the deadline lapses instead of waiting forever.
* **No arbitrary execution.** The protocol has no shell command, no filesystem
  read or write, and no "run this JavaScript program in ego lite" frame. File
  upload takes a path already known to the host through its own configured
  documents directory, validated against traversal.
* **Never logged.** Credentials, page text, cookies and answers are excluded
  from log payloads; `core/redaction.py` covers the keys.

Trust boundary: the host trusts the server to send only protocol commands; the
server trusts the host to be the paired machine and nothing more. Neither
trusts the network.

### 3.4 Command set

Commands are semantic. There is no host shell.

```
create_session   close_session    navigate      observe
click            fill             select        set_checked
upload           download         evaluate      screenshot
open_tab         close_tab        activate_tab  list_tabs
request_handoff  reclaim_control  control_state checkpoint
health           cancel
```

`evaluate` exists because the existing `BrowserSession` contract already
exposes JavaScript evaluation for control scanning, and both local backends
implement it. It is gated by the `JAVASCRIPT_EVAL` capability and is refused by
a host whose selected backend does not advertise it.

### 3.5 Capability matching

`BrowserRequirements` states what a workflow needs. Selection is deterministic:

```
requires: persistent_login, human_handoff, file_upload
    |
    +--> registered ego lite Browser Host      (all three: selected)
    +--> local ego lite                        (macOS native install)
    +--> other registered interactive host
    +--> Playwright                            (no persistent_login: rejected)
    +--> BackendUnavailableError listing every rejection reason
```

Capabilities override preference order. A workflow that needs
`PERSISTENT_LOGIN` never silently receives an isolated backend.

## 4. Application attempts

An attempt is the durable execution record for one try at one application. It
is not `apply(job)`.

```
ApplicationAttempt 01JABC...
  application_id, job_id
  driver "greenhouse", driver_version "1"
  browser_host_id, browser_backend "ego_lite", browser_session_id
  task_space_id "applyuminati:01JABC..."   (name) / 481203941 (numeric id)
  current_step, workflow state, submission mode
  started_at / updated_at / completed_at
    |
    +-- checkpoints        APPLICATION_OPENED ... SUBMISSION_CONFIRMED
    +-- questions/answers  with provenance and policy decision
    +-- uploads            document kind, locator, confirmation
    +-- observations        redacted page summaries, not full DOM dumps
    +-- failures           structured category, step, retryability
    +-- interventions      typed reason, browser handoff or not
    +-- events             append-only workflow log
    +-- submission evidence with an explicit certainty
```

Retention: page text is truncated and sanitised before storage, screenshots are
kept as data-dir-relative paths under the attempt id, and observation bodies
older than the configured retention window are prunable without deleting the
attempt.

### 4.1 Checkpoints

Canonical checkpoints are shared vocabulary; drivers may add their own with a
`driver:` prefix so the enum never has to grow per ATS.

```
APPLICATION_OPENED  ACCOUNT_AUTHENTICATED  PERSONAL_INFORMATION_COMPLETE
EMPLOYMENT_HISTORY_COMPLETE  EDUCATION_COMPLETE  QUESTIONNAIRE_COMPLETE
DOCUMENTS_UPLOADED  REVIEW_PAGE_REACHED  SUBMISSION_CONFIRMED
workday:employment_history_page   (driver-specific)
```

Recovery reasons from checkpoints, so a failure reads
"Greenhouse validation failed on the questionnaire section after
QUESTIONNAIRE_COMPLETE" rather than "browser error".

### 4.2 Idempotency

Submission is the one irreversible action, so it is treated specially:

* `Application.submission_fingerprint` already prevents re-applying to the same
  role reached through two sources.
* An attempt records `submission_attempted_at` **before** clicking the final
  control, so a crash between click and confirmation cannot be replayed as a
  fresh submission.
* On resume, a driver whose attempt has `submission_attempted_at` set must
  verify rather than submit, and may only report `CONFIRMED`, `LIKELY` or
  `UNCERTAIN`.

## 5. Human intervention

Typed, durable, and never routed through retry policy.

```
AUTHENTICATION_REQUIRED  CAPTCHA_REQUIRED     MFA_REQUIRED
IDENTITY_VERIFICATION    LEGAL_ATTESTATION    AMBIGUOUS_QUESTION
DOCUMENT_REQUIRED        PAYMENT_OR_FEE       USER_REVIEW
AUTOMATION_BLOCKED       UNKNOWN_INTERACTION
```

Not every reason needs the browser. `requires_browser_handoff` is a property of
the reason, defaulted per reason and overridable per intervention:

| Reason | Browser handoff | Resolution |
|---|---|---|
| `AUTHENTICATION_REQUIRED` | yes | user signs in, then "Done, continue" |
| `CAPTCHA_REQUIRED` | yes | user solves it; we never bypass it |
| `MFA_REQUIRED` | yes | user completes the challenge |
| `AMBIGUOUS_QUESTION` | no | user answers inside Applyuminati |
| `LEGAL_ATTESTATION` | no | explicit approval per policy |
| `DOCUMENT_REQUIRED` | no | user selects or provides an artifact |

Entering `WAITING_FOR_HUMAN` is a persistence operation, not a blocking call:

```
1. persist the checkpoint
2. persist browser host, backend, session and task-space identity
3. persist the intervention with its reason and instruction
4. transition the attempt to WAITING_FOR_HUMAN
5. release the worker  (the task leaves the queue)
6. surface the intervention in "Needs you"
7. on resolution, requeue the workflow from the checkpoint
```

Resolution actions: `done_continue`, `skip_application`, `keep_control`,
`answer`, `approve`, `reject`, `provide_document`, `cancel`.

`keep_control` is honoured: the attempt stays in `WAITING_FOR_HUMAN` and the
agent does not reclaim the session. No timer reclaims control. Only an explicit
completion event does.

## 6. ego lite as a first-class integration

Corrected facts, checked against the public project:

* The public repository is <https://github.com/citrolabs/ego-lite>, MIT
  licensed. It contains the `ego-browser` agent skill: `SKILL.md`, references,
  scripts, and reusable per-site `learnings/`.
* The ego lite desktop application is a separate free download and is not open
  source. It is macOS only today; Windows and Linux are on its roadmap.
* The integration surface is therefore **documented**, not a black box. Task
  spaces, ownership, handoff, takeover, snapshots, helpers and site learnings
  are all specified in the public harness.

Two integration bugs this milestone fixes:

1. **Task-space creation.** `useOrCreateTaskSpace(nameOrId)` creates a space
   when given a *name string*; given a *number* it only matches an existing
   numeric id and otherwise fails. Applyuminati was passing a derived numeric
   id, so first use could never create the space. The adapter now opens with the
   stable name `applyuminati:<attempt id>` and persists the numeric `task.id`
   the call returns, which is what later rounds and `takeOverTaskSpace` use.
2. **Control reclaim.** `waitForAgentControl` is a read-only poll and
   `takeOverTaskSpace` has no ownership check; the harness is explicit that an
   agent must never take control back on its own initiative. The old code did
   both in one script. Reclaim is now a separate operation that requires an
   explicit user completion event, and the blocking poll is only used inside a
   handoff the same call initiated.

Applyuminati keeps ego lite behind the generic browser contract and does not
reimplement ownership. Ownership maps directly:

| ego lite | Applyuminati |
|---|---|
| `ownership: agent` | `ControlOwner.AGENT` |
| `ownership: agentDelegatedToUser` | `ControlOwner.DELEGATED_TO_USER` |
| `ownership: user` | `ControlOwner.USER` |

## 7. JobSource and ApplicationDriver are different extension points

Discovery ends at a normalised job. Execution begins from the application URL.

```
LinkedIn / Indeed / Greenhouse board / RSS
        |  discovery, normalisation, freshness, provenance
        v
Normalised Job  --application_url-->  company.wd5.myworkdayjobs.com/...
                                              |
                                        ATS detection
                                              v
                                    Workday ApplicationDriver
                                              v
                                    capability-matched browser
```

A driver owns ATS detection hints, workflow identification, authentication
expectations, step interpretation, question extraction, form filling, document
upload, validation handling, submission, submission verification, recovery
hints and per-ATS learned knowledge. A source owns none of that.

Registry: `APPLICATION_DRIVER_REGISTRY`, entry-point group
`applyuminati.application_drivers`.

## 8. Questionnaire answer authority

Policies are per sensitivity class, with optional ATS and employer overrides:

| Policy | Behaviour |
|---|---|
| `always_answer` | answer from any available source, including generated wording |
| `answer_if_verified` | answer only from `VERIFIED` or `USER_APPROVED` evidence |
| `reuse_approved` | answer only from a previously user-approved answer |
| `decline_if_optional` | decline when optional, otherwise ask |
| `require_review` | always stop for the user |
| `never_answer` | always stop, and never propose wording |

Defaults keep the existing safe behaviour: everything in
`REVIEW_REQUIRED_CLASSES` starts at `require_review`, except
`WORK_AUTHORIZATION` which is `answer_if_verified` and `DEMOGRAPHIC`,
`DISABILITY` and `VETERAN` which are `decline_if_optional`. Users can change
these; the engine records which policy produced each decision.

Every answer keeps provenance and an `AnswerSource`. A generated suggestion
that gets submitted does not become a verified fact: promotion into the claim
ledger still requires `by_user=True`, which `core/provenance.py` already
enforces.

## 9. Structured failure and recovery

`ExecutionFailure` records category, driver, step, checkpoint, a redacted
observation summary, attempted recovery, retryability and whether a human is
required. Categories:

```
navigation_failed        page_changed            element_unavailable
validation_rejected      authentication_required browser_host_unavailable
capability_unavailable   provider_unavailable    rate_limited
network_failed           document_rejected       question_unresolved
submission_uncertain     driver_bug
```

Each maps to a `FailureCategory` so the existing recovery policy applies
without a second taxonomy.

## 10. Submission evidence

Clicking the last button is not submission. Evidence carries a certainty:

| Certainty | Meaning |
|---|---|
| `confirmed` | confirmation page, id, or known success route observed |
| `likely` | strong signal without an identifier |
| `uncertain` | the click happened, the outcome is unknown |
| `not_submitted` | no submission was attempted |

`UNCERTAIN` is reported honestly and produces a `USER_REVIEW` intervention
rather than a `SUBMITTED` transition.

## 11. Capability maturity

Scaffolding must be distinguishable from production-tested behaviour. Every
plugin descriptor carries a `maturity`:

| Level | Meaning |
|---|---|
| `contract_only` | the contract exists, no adapter |
| `adapter_exists` | an adapter exists, untested against the real system |
| `health_probe_working` | health detection verified on a real install |
| `workflow_integrated` | used by a complete workflow with tests |
| `production_tested` | exercised end to end against the real system |

Nothing in this repository claims `production_tested` until it has been run
against a live employer flow by a human. The matrix is generated from the
registries by `applyuminati capabilities` and asserted by tests, so it cannot
drift from the code.

## 12. Delivery phases

| Phase | Content | State |
|---|---|---|
| 1 | Audit and design (this document, ATS roadmap) | done |
| 2 | Security and release correctness | done |
| 3 | Browser Host protocol and native companion | done |
| 4 | Durable HITL, attempts, "Needs you" | done |
| 5 | ApplicationDriver contract, ATS detection | done |
| 6 | Questionnaire policy engine | done |
| 7 | Greenhouse driver | done |
| 8 | Lever driver, abstraction cleanup | done |
| 9 | Documentation and hardening | done |
| later | Workday, generic fallback, email-derived events | not started |

Explicitly out of scope: Kubernetes, microservices, hosted SaaS, a vector
database, a workflow-engine rewrite, a browser implementation, CAPTCHA
circumvention, a mass ATS sprint, an email client, replacing JSON Resume.
