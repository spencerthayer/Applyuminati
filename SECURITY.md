# Security Policy

## Reporting a vulnerability

Email security@spencerthayer.com with a description of the issue and steps to reproduce. Do not open a public issue.

## Principles

- **Local-first.** User data (career profile, credentials, job history, generated materials, application records, memory, configuration) stays local unless a configured external provider needs specific data to perform a task.
- **Secrets never logged.** The redaction layer (`core/redaction.py`) scrubs API keys, passwords, session cookies, and sensitive application answers at the logging boundary. Secrets are `SecretStr` in Pydantic models and only accessed via `get_secret_value()` at the moment of use.
- **No access-control evasion.** Applyuminati does not implement CAPTCHA bypass, credential theft, fingerprint spoofing, stealth malware techniques, or instructions intended to defeat access controls. When automation is blocked, the condition is detected and reported, and the user is asked to take over.
- **No fabrication.** The fabrication guard (`resume/guard.py`) refuses generated content that asserts facts not present in the canonical profile. An LLM may never silently promote an inference into a verified fact.
- **Autonomous submission is opt-in.** The default execution mode is `research_only`. Enabling `autonomous_submit` is an explicit, recorded configuration act, and sensitive questions always require human review even in autonomous mode.

## Credential handling

- Browser Host pairing secrets are shown once. Only a hash is stored, so a
  copy of the database cannot drive the user's browser. Remote hosts require
  `wss://` unless the operator passes `--allow-insecure`.
- API keys are stored as `pydantic.SecretStr` and never serialized into API responses or logs.
- `Settings.public_dict()` exposes `has_api_key: bool`, never the key itself.
- The `.env.example` file contains only empty placeholders.
- `config.toml` may contain secrets but is in the user's data directory (`~/.applyuminati/`), not the repository.

## What Applyuminati does NOT do

- Store passwords in plaintext
- Send email on behalf of the user without explicit action
- Submit applications without the execution mode permitting it
- Bypass any access control (CAPTCHA, login wall, bot detection)
- Promote model inferences to verified facts without human approval
