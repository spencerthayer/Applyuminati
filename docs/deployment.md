# Deployment modes

Applyuminati runs in three shapes. They differ in one respect that matters:
whether a native browser is reachable.

| Mode | Runs on | ego lite | Playwright | Application submission |
|---|---|---|---|---|
| Native | macOS or Linux desktop | yes, when installed | yes | full |
| Docker plus Browser Host | any Docker host, host on a Mac | through the Browser Host | yes | full |
| Docker only | any Docker host | no | yes, in-container | limited |

## Native

```bash
uv sync --all-extras
export APPLYUMINATI_SECURITY__PASSWORD='...'
uv run applyuminati serve
```

On macOS with ego lite installed, `applyuminati doctor` reports the `ego_lite`
backend as healthy and no Browser Host is needed: the local backend is selected
directly.

## Docker plus Browser Host (recommended)

The container holds the API, the UI, discovery, scoring and persistence. The
Browser Host runs natively on the machine where the browser lives, and dials out
to the container.

```
  Mac (or any desktop)                    Docker host
  applyuminati-browser-host  ---wss--->   applyuminati container
        |                                        |
     ego lite                              SQLite in a volume
```

1. Start the stack. The published port binds to `127.0.0.1` by default, so it is
   reachable from the Docker host and nowhere else.

   ```bash
   APPLYUMINATI_PASSWORD='your-password' docker compose up -d
   ```

2. Pair a host. This mints a credential and prints it once:

   ```bash
   curl -sS -X POST http://127.0.0.1:8420/api/v1/browser-hosts/pair \
     -H "Authorization: Bearer $SESSION" \
     -H 'Content-Type: application/json' \
     -d '{"host_id": "spencers-mac", "display_name": "Spencer'\''s MacBook"}'
   ```

3. Run the host on the desktop:

   ```bash
   uv tool install applyuminati        # or pipx install applyuminati
   APPLYUMINATI_HOST_CREDENTIAL='...' applyuminati-browser-host run \
     --server ws://127.0.0.1:8420/api/v1/browser-hosts/ws \
     --host-id spencers-mac
   ```

The host reconnects on its own with exponential backoff, so restarting either
side is safe, and an application attempt survives it: the attempt keeps its
task-space identity and resumes from its last checkpoint.

### Reaching the container from another machine

Two changes, both deliberate:

```bash
APPLYUMINATI_BIND_ADDRESS=0.0.0.0
APPLYUMINATI_PASSWORD='...'          # required: an open API is refused
```

`Settings` refuses to start an unauthenticated API on a non-loopback interface.
Overriding that needs
`APPLYUMINATI_SECURITY__ALLOW_UNAUTHENTICATED_EXPOSURE=true`, which exists for
the case where you have put your own authenticating proxy in front, and for no
other case.

### TLS

Anything beyond a trusted LAN needs TLS, because the session cookie and the
Browser Host credential both cross the wire.

Terminate TLS at a reverse proxy (Caddy, nginx, Traefik) and point it at the
container. Then:

```bash
APPLYUMINATI_HTTPS_ONLY=true          # adds Secure to the cookies
```

The Browser Host refuses a plaintext `ws://` URL unless the target is loopback,
so a remote host must be given a `wss://` URL. `--allow-insecure` exists for a
lab and logs a warning every time it is used.

A minimal Caddy configuration:

```
applyuminati.example.com {
    reverse_proxy 127.0.0.1:8420
}
```

Caddy proxies the WebSocket upgrade without extra configuration. nginx needs the
usual `proxy_set_header Upgrade`/`Connection` pair on
`/api/v1/browser-host/ws`.

## Docker only, no Browser Host

Everything except native-browser automation works: discovery, scoring,
research, resume tailoring, questionnaire preparation, the full UI.

What is limited:

* ego lite is unavailable. It is a native macOS application and there is no
  supported way for a Linux container to drive one. Applyuminati does not
  mount host executables or expose host shell execution to work around that.
* Playwright in the container has no access to your logged-in sessions, so any
  application flow behind a login wall stops with an
  `AUTHENTICATION_REQUIRED` intervention that cannot be resolved in place.
* Workflows that declare `PERSISTENT_LOGIN` or `HUMAN_HANDOFF` requirements are
  refused with a `BackendUnavailableError` naming the missing capability, rather
  than silently running against a backend that cannot satisfy them.

To use Playwright inside the container, install the browsers into the data
volume:

```bash
docker compose exec applyuminati python -m playwright install chromium
```

## Backups

Everything is in one volume: the SQLite database, generated documents, captured
artifacts and `config.toml`.

```bash
docker run --rm -v applyuminati-data:/data -v "$PWD":/backup alpine \
  tar czf /backup/applyuminati-$(date +%F).tar.gz -C /data .
```

Deleting that volume deletes the job search. There is no cloud copy, by design.
