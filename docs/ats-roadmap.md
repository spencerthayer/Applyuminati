# ATS and source roadmap

Discovery coverage and application-driver coverage are tracked separately. A
system Applyuminati can *discover* jobs from is not necessarily a system it can
*apply* through, and the work involved is different.

Long-term target: broad direct discovery across dozens of ATS platforms,
dedicated application drivers for roughly the highest-value 15 to 20, and a
generic agent-driven fallback for the long tail.

Maturity levels are the same ones the code reports
(`applyuminati capabilities`): `contract_only`, `adapter_exists`,
`health_probe_working`, `workflow_integrated`, `production_tested`.

## Application drivers

### P0

| ATS | Driver | Discovery | Notes |
|---|---|---|---|
| Greenhouse | `workflow_integrated` | public board API | first complete driver |
| Lever | `workflow_integrated` | public postings API | proves the abstraction |
| Ashby | not started | public API available | posting API is clean |
| SmartRecruiters | not started | public API available | |
| Workday | not started | tenant-scoped | deliberately last of the P0 set: account creation, MFA, multi-page wizard |
| iCIMS | not started | mostly HTML | |
| Oracle Recruiting / Oracle HCM | not started | tenant-scoped | |
| UKG Pro Recruiting / UltiPro | not started | tenant-scoped | |

Workday is strategically the most valuable and operationally the most complex.
It is scheduled after the durable handoff system exists, so its account,
authentication and MFA requirements are ordinary workflow events rather than
architectural emergencies.

### P1

ADP Recruiting, SAP SuccessFactors, Dayforce, Jobvite, BambooHR, Paylocity,
Paycom, Hireology, Teamtailor, Recruitee, JazzHR, Workable,
Rippling Recruiting.

### P2 and long tail

Zoho Recruit, Apploi, ApplicantPro, ApplicantStack, ClearCompany, Comeet,
Pinpoint, Breezy HR, Freshteam, Manatal, Recruit CRM, CEIPAL, Bullhorn,
Avature, Phenom, Eightfold AI, Cornerstone Recruiting, PageUp, Taleo,
PeopleFluent, Trakstar Hire, Arcoro, Paycor Recruiting.

The long tail is the case for the generic driver, not for 24 more dedicated
drivers. The generic driver is built after Greenhouse and Lever have shown what
the contract actually needs.

## Discovery sources

Aggregators are a separate plugin family. They discover and normalise; they
never own application behaviour. Several of them resolve to the same ATS
driver, which is the point of detecting the driver from the application URL.

| Family | Members |
|---|---|
| Direct ATS | Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, Teamtailor |
| Aggregators | LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Dice, Wellfound, Built In |
| Remote boards | Remote OK, We Work Remotely, Remotive |
| Public sector | USAJobs |
| Community | Hacker News "Who's Hiring?", Craigslist |
| User-defined | RSS, JSON feeds, custom APIs, company career pages, search-engine discovery |

Implemented today: Greenhouse, Lever, and a local file feed.

## Blocking posture

Recorded per source in `SourceMetadata.blocking`, so a run can pick a supported
strategy up front. Applyuminati never attempts to defeat any of these:

| Posture | Meaning |
|---|---|
| `none` | permits automated access |
| `rate_limited` | permits it within a budget |
| `login_wall` | needs a signed-in session, so needs a capable browser |
| `bot_challenge` | serves interstitials to non-browser clients |
| `prohibited` | terms forbid automated collection; detect-only |

## What "supported" means

A driver is listed as supported only when it has: ATS detection, attempt
creation, capability-matched backend selection, form and step detection,
question extraction wired to the policy engine, document upload, validation
capture, human handoff with checkpoint resume, idempotent submission, and
submission evidence. Anything short of that is scaffolding and is labelled as
such.
