# Web Frontend

[Repository overview](../../../README.md) → [Application guide](../README.md) → **Frontend**

One local Node service serves the three production web applications and proxies fixed API routes to `app-function-logic`:

| Application | Port | Purpose |
|---|---:|---|
| Booking | 3000 | Choose and request a viewing |
| Questionnaire | 3001 | Complete the contact questionnaire |
| Lead Intelligence dashboard | 3002 | Live leads, triage, funnel, Agent and credential settings |

Docker Compose builds and runs this service. Start the complete stack using the instructions in [`../README.md`](../README.md). Do not also launch `node serve-local.js` while Compose owns the ports.

### Serving the frontend natively during UI work

The Compose stack owns ports 3000–3002 through the `web-frontend` container, so editing
`dashboard/styles.css` or `dashboard/app.js` normally means rebuilding that image. To iterate
on the UI without a rebuild, run Compose for the data and logic tier only and serve the
frontend from Node:

```powershell
powershell -ExecutionPolicy Bypass -File ..\start-local.ps1
```

`start-local.ps1` brings up Postgres, Mailpit and `app-function-logic` with
`docker compose up -d`, deliberately omitting `web-frontend`, then runs
`node serve-local.js` on the host with `APP_FUNCTION_LOGIC_URL=http://127.0.0.1:8001`. It also
supervises that Node process and re-applies the Compose tier if the backend stops, so an
interrupted terminal no longer takes the dashboard down. Pass `-Once` to start, verify, and
exit without supervising. Startup reuses an already healthy native frontend, leaves port
owners intact, bounds each Docker engine probe to ten seconds, and requires HTTP 200 from
both backend `/healthz` and the proxied leads API. Because the containerised frontend is skipped, stop it first if it
is already running (`docker compose stop web-frontend`), otherwise the port bindings collide.

The Vercel proxy files are retained for deployments; the local Compose setup uses `serve-local.js` and does not require Vercel.

## Host configuration

| Environment variable | Compose value/default | Purpose |
|---|---|---|
| `APP_FUNCTION_LOGIC_URL` | `http://app-function-logic:8000`; direct-host default `http://127.0.0.1:8001` | Fixed backend origin for allowed proxy routes |
| `APP_LISTEN_HOST` | `0.0.0.0` in the container; direct-host default `127.0.0.1` | Node listener binding; Compose publishes the ports only on host loopback |
| `TVE_WEBHOOK_SECRET` | Interpolated from the Compose environment; local fallback `sandbox-webhook-secret` | Startup credential injected into proxied backend requests as `X-TVE-Auth` |

Use Compose `--env-file backend/.env` from the application directory so the proxy's interpolated webhook credential matches the backend bootstrap value. The frontend does not have a service `env_file` loading all backend credentials. It receives no vault master key, SMTP/IMAP password or Acquaint API key through its startup environment.

When running `node serve-local.js` directly, the script reads the process environment; it does not parse `backend/.env` itself. Set the backend URL/webhook credential in that process when they differ from the local defaults. Restarting a direct Node process can load a changed process environment; Compose containers require recreation to load edits to an environment file.

The Lead Intelligence dashboard is a single-page app under [`dashboard/`](dashboard/README.md) that reads mode-specific contacts, journey stages, funnel and metrics through `/sandbox/api`, plus triage actions and settings administration. In Sandbox, **Load test data** seeds synthetic leads; the Agent is paused because its n8n tools read production data. Mode-aware requests carry an environment header and stale actions are rejected. See [sandbox operation](../backend/docker/SANDBOX.md). The optional event viewer at port 3003 is started separately from `Tools/live-log-viewer.py`.

## Credential settings proxy

Open <http://127.0.0.1:3002/#/settings>. The browser sends same-origin requests to the Node proxy; the proxy adds webhook authentication and forwards only its allowlisted paths/methods to Function Logic. It does not decrypt settings, connect directly to `pg-settings` or make provider-authentication requests itself.

| Browser route | Method | Backend target |
|---|---|---|
| `/sandbox/api/settings` | GET | `/dashboard/settings` |
| `/sandbox/api/settings/mode` | GET | `/dashboard/settings/mode` |
| `/sandbox/api/settings/mode` | PUT | `/dashboard/settings/mode` |
| `/sandbox/api/settings/sandbox/seed` | POST | `/dashboard/settings/sandbox/seed` |
| `/sandbox/api/settings/{ENV_KEY}` | PUT | `/dashboard/settings/{ENV_KEY}` |
| `/sandbox/api/settings/{ENV_KEY}` | DELETE | `/dashboard/settings/{ENV_KEY}` |
| `/sandbox/api/settings/test/{group}` | POST | `/dashboard/settings/test/{group}` |

Dynamic setting keys use uppercase environment-key syntax; the backend enforces the actual credential allowlist. Test groups are restricted to `acquaint`, `imap` and `smtp`. PUT forwards JSON `{value:string}`; tests forward an optional `{values:{ENV_KEY:string}}` candidate map. GET/DELETE do not forward a body. Unsupported routes/methods are rejected before contacting the backend.

### Secret handling and errors

- Backend metadata returns secret status, source, mask and update metadata, with no secret `value`. Only non-secret fields expose plaintext effective values.
- The dashboard also discards any unexpected `value` for a known secret field and leaves password inputs empty until the user types a replacement.
- Candidates remain in input elements and outgoing request bodies only; they are not saved to browser storage. A successful write discards that candidate, and leaving Settings clears inputs.
- Proxy JSON responses use `Cache-Control: no-store`. Request IDs are supplied/preserved through `X-Request-ID` and error envelopes.
- Non-success settings responses preserve the HTTP status but use `settings_request_failed` and a fixed sanitized message instead of raw backend validation/provider detail. Backend-unreachable responses use 502/`backend_unavailable`; invalid JSON and oversized bodies are rejected by the proxy.
- A valid connection probe that fails returns HTTP 200 with `ok=false` and a sanitized message. The UI displays its failed-test result rather than treating it as a Save operation.

The browser necessarily holds user-entered replacements while editing/submitting them; write-only means existing secret values cannot be retrieved through the metadata API. There is no reveal/copy-current-secret feature.

### Rotation and client-side refresh

The proxy reads `TVE_WEBHOOK_SECRET` once at startup. Saving a vault replacement leaves the proxy using its earlier value until recreation, while the backend's dual acceptance keeps it connected. Complete rotation by updating `backend/.env` and recreating both `app-function-logic` and `web-frontend` with the same explicit interpolation file; see [backend webhook rotation](../backend/README.md#webhook-authentication-rotation).

Settings loads when opened and when **Refresh settings** is chosen. Normal lead polling does not rebuild credential inputs. Save/Clear of an Acquaint field invalidates the dashboard property list so the next Properties load re-reads the catalogue. The backend separately replaces its authenticated Acquaint client when the effective credential snapshot changes.

The [dashboard settings walkthrough](dashboard/README.md#settings--workspace-credentials) explains field controls, connection tests, key confirmations and locked-vault behaviour. The [Function Logic contract](../backend/app/README.md#credential-settings-api) documents request/response shapes and statuses.

## Dashboard preview

![Lead Intelligence dashboard Overview](../Tools/screenshots/dashboard/overview.png)

The [dashboard walkthrough](dashboard/README.md#screenshot-walkthrough) covers Overview, Inbox, Leads, Views, My leads, Properties, Agent and Settings. The existing screenshots cover the six lead-management pages, lead drawer and property-assignment dialog. The app uses hash routes, inline icons and a self-hosted Inter Variable font, with no frontend build step. Leads refresh every five seconds; Overview analytics refresh every ten seconds while visible. Starred leads are saved only in the current browser; credential candidates are not stored there.

## Frontend checks

From `frontend/`:

```powershell
node --check dashboard/app.js
node --check serve-local.js
```

See [Tools settings verification](../Tools/README.md#credential-settings-verification) for the backend test subset and a live panel check, and [dashboard screenshots](../Tools/README.md#dashboard-screenshots-and-browser-smoke-check) for the existing capture script's coverage.

Next: [Questionnaire](questionnaire/README.md) → [Booking](booking/README.md) for the applicant journey. See [Backend](../backend/README.md) for the API/mail worker, [Docker operations](../backend/docker/README.md) for startup, and [Tools](../Tools/README.md) for the optional viewer.
