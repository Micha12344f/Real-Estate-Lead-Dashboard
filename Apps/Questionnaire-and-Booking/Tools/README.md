# Simulation and development tools

[Repository overview](../../../README.md) → [Application guide](../README.md) → **Tools**

These tools provision local credential settings and exercise the shared application, primarily in Sandbox mode. `run_sandbox_isolation_e2e.py` requires the isolated Compose databases; it switches modes and verifies production data fingerprints remain unchanged. They are not a second copy of the application. Commands below run from `Apps/Questionnaire-and-Booking/` unless a different working directory is stated.

## Automatic deployment tool

`deploy_tve.py` is the stdlib-only host transaction used by the post-Application-CI
deployment workflow. It accepts only the validated successful same-repository
main-push CI SHA, preserves runtime env/data, uses the shared host lock and rolls
back source/config and selected images on failure. See
[deployment operations](../backend/docker/DEPLOYMENT.md) for host prerequisites,
env precedence, private state schema and recovery.

Run `python -m pytest -q Tools/test_deploy_tve.py` from the app root for temporary
filesystem/fake-Git/Docker/HTTP tests; Application CI runs them separately.

## n8n workflow sources

[`n8n/`](n8n/README.md) contains the twelve sanitized Lead Intelligence workflow sources and their manifest, including the main Agent, nine configured tools and two inactive task/note placeholders. Credential references are metadata only. Run `node Tools/check_n8n.cjs` for offline integrity, privacy and embedded-code checks; importing these sources does not deploy them.

## Credential vault provisioning

`provision-settings.py` prepares the ignored `backend/.env` for the encrypted settings store. It does not start services, write settings database rows, import provider credentials into the database or rotate applicant signing keys.

Create `backend/.env` from its template first, then install the helper's dependency and run it:

```powershell
python -m pip install -r backend/requirements.txt
python Tools/provision-settings.py
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up --build -d
```

The helper locates `backend/.env` relative to its own script location, so it does not depend on the shell's current directory for that file lookup. It requires a host Python with `cryptography` installed; dependencies installed only inside Docker are not available to the host script.

| Input/state | Helper behaviour |
|---|---|
| `backend/.env` missing | Exit with a message asking for the environment file; do not create it from a template automatically |
| Master-key assignment missing, empty or a placeholder beginning `<` | Generate a fresh Fernet key and write it to `TVE_SETTINGS_MASTER_KEY` |
| Existing valid Fernet key | Preserve the key and report preservation without printing it |
| Invalid non-placeholder key | Exit rather than silently replace it |
| Webhook assignment absent | Add the local `TVE_WEBHOOK_SECRET=sandbox-webhook-secret` bootstrap value |
| Webhook assignment already present | Preserve it, including an explicitly empty value; the helper does not validate or rotate it |
| Settings TTL absent or already configured | Write/reset `TVE_SETTINGS_TTL_SECONDS=5` |

Rerunning is safe for an existing valid encryption key but is not a complete no-op: it resets the TTL to five seconds. If a different TTL is intended, set it after provisioning and recreate the Python services. An existing key with valid syntax is not proof it matches existing ciphertext.

After provisioning/editing the master key or TTL, recreate `app-function-logic` and `imap-intake` so they load the changed environment. Include `web-frontend` when the webhook bootstrap assignment was added/changed. The helper prints status messages only, not generated keys or provider values. The frontend receives no master key.

Keep the original master key with the settings-volume backup. Generating a replacement does not decrypt or re-encrypt old rows. See [backend provisioning](../backend/README.md#provisioning-and-vault-configuration), [recovery](../backend/README.md#locked-vault-recovery-and-backups) and [Docker environment loading](../backend/docker/README.md#environment-loading).

## Credential settings verification

### Automated backend checks

From `backend/`:

```powershell
python -m pytest -q tests/test_settings_store.py tests/test_settings_api.py tests/test_dynamic_credentials.py
```

| Test module | Coverage |
|---|---|
| `test_settings_store.py` | Ciphertext roundtrip, masked/visible metadata, short-secret protection, fallback, locked/invalid/changed keys and TTL invalidation/expiry |
| `test_settings_api.py` | Protected routes, credentials-only scope, 422 validation, write-only responses/audit, Save/Clear, dual webhook rotation and candidate probes |
| `test_dynamic_credentials.py` | Acquaint client/auth replacement, SMTP per-send reads, sandbox sender routing, dynamic signed links, legacy compatibility and IMAP cursor/dedupe behaviour |
| `test_production_journey.py` | Production contact-token API journey, questionnaire/viewing persistence, real catalogue ID resolution, SMTP selection, dashboard tracking, rejection and retry idempotency with mocked databases/HTTP/SMTP |

Frontend production-proxy checks and JavaScript syntax validation (including inline HTML scripts) run without contacting services:

```powershell
node --test Tools/test_frontend_proxies.cjs
node Tools/check_javascript.cjs
```

Run these commands from `Apps/Questionnaire-and-Booking/`. The proxy tests intercept all `fetch` calls.

These use in-memory/fake database connections and mocked provider transports. They do not authenticate with live providers or prove a running deployment's actual credentials are valid. The full backend suite remains `python -m pytest -q` from `backend/`.

From `frontend/`, validate the served JavaScript:

```powershell
node --check dashboard/app.js
node --check serve-local.js
```

### Manual live panel check

Use a running stack and open <http://127.0.0.1:3002/#/settings>. This workflow verifies the deployed proxy/API/store/consumer path rather than only isolated unit behaviour:

1. Confirm the credential cards, source/status badges and update metadata. Secret inputs should be empty. A provisioned stack should show **Vault unlocked**; Set does not imply valid provider credentials. The Settings page also shows the **Sandbox / Production** mode card and (in Sandbox) the **Load test data** button.
2. Enter a candidate in an Acquaint/IMAP/SMTP card and choose **Test connection**. Confirm the response has `ok` and a sanitized message, with no candidate persistence or email delivery. Refresh metadata to confirm the source/update time is unchanged by the probe.
3. Type an unsaved candidate, wait through a lead poll and choose **Refresh settings**. Confirm the input survives. Navigate away and back; confirm candidates are discarded rather than restored from browser storage.
4. To check saved Acquaint propagation, record the original source and have any database-backed original credential available independently of the panel. Save a syntactically valid, deliberately invalid API key. Open Properties and confirm the provider failure is displayed instead of stale auth/fixtures. Restore the original override with Save, or use Clear if the original source was Environment. Confirm the catalogue recovers.
5. To check mail propagation in sandbox, save a test `TVE_ENQUIRY_FROM`, trigger an eligible invitation/journey message and inspect its From address in Mailpit at <http://127.0.0.1:8025>. Restore the original source/value afterward. A sender edit alone does not send mail, and resending a real contact invitation also updates its token expiry.
6. In browser developer tools, inspect settings GET/PUT/DELETE/probe responses. Existing/entered full secrets must not appear; secret metadata may include only the intended mask suffix. Password fields must not be prefilled, and no credential candidates should be written to local/session storage.
7. Open the token/webhook Save/Clear confirmations and choose Cancel to verify no write is made. Performing an actual rotation has the link/proxy effects documented in [backend rotation](../backend/README.md#token-signing-key-rotation).

A failed connection probe returns HTTP 200 with `ok=false`; invalid/missing configuration returns 422, and locked/unavailable storage returns 503. Explicit probes can contact real configured endpoints even in sandbox. SMTP HELO success is not evidence that authentication or delivery will succeed. Test candidates are not saved automatically.

The existing screenshot/full-journey scripts below are not dedicated Settings browser tests. Follow this workflow separately when verifying the deployed credential panel. For detailed controls and recovery states, see [dashboard Settings](../frontend/dashboard/README.md#settings--workspace-credentials).

## Fixtures

`fixtures/` contains synthetic Rightmove/OnTheMarket emails, enquiry records, questionnaire samples and a derived property catalogue. `fixture-manifest.json` preserves the original source commit, historical paths and SHA-256 checksums. Historical `Sandbox/` source paths describe that original commit; they are not current local paths and must not be rewritten.

Compose mounts `Tools/fixtures` read-only into Function Logic and the mail worker. Host-side parser tests resolve the same folder.

## Optional event viewer

From `Apps/Questionnaire-and-Booking/`:

```powershell
python Tools/live-log-viewer.py
```

The viewer listens at <http://127.0.0.1:3003> and polls the backend's `/debug/events` feed. It is not a Compose service and does not start automatically with the application.

Settings Save/Clear and completed connection probes add privacy-filtered events to that backend feed. They contain fixed messages, allowlisted key/group names and succeeded/failed operation status, never credential values or raw provider exceptions. The feed is bounded in memory and disappears when Function Logic is recreated; it is not a persistent edit-history database. A custom webhook bootstrap secret must also be supplied to any viewer/client authenticating with the backend.

The optional viewer has its own display allowlist and currently does not render the dedicated `settings_saved`, `settings_cleared` or `settings_tested` events. Inspect Function Logic logs or the authenticated backend `/debug/events` response for those records; their presence in the backend feed does not imply the viewer displays them.

Run its regression checks from the same app directory:

```powershell
python -m pytest -q Tools/test_live_log_viewer.py
```

Backend regression tests are run separately from `backend/` with `python -m pytest -q`.

## End-to-end scripts

`run_sandbox_e2e.py` drives a fresh contact through SMTP intake, property linking, invitation, questionnaire submission, viewing request, idempotent retries and booked-link reload. It chooses an unused synthetic fixture from the contact list. Run it against a running local Sandbox stack:

```powershell
python Tools/run_sandbox_e2e.py
```

`run_sandbox_isolation_e2e.py` checks the environment boundary against a disposable
stack with its own containers and volumes. It loads synthetic leads, switches modes,
rejects stale-mode actions and cross-mode tokens, then compares database fingerprints
to confirm Production journey data/settings were unchanged. It refuses to run unless
the API bootstrapped in Sandbox mode. Run only on a local/CI stack, never estate-server:

```powershell
python Tools/run_sandbox_isolation_e2e.py
```

`browser_sandbox_e2e.cjs` drives the contact dashboard and multi-step forms through Playwright/Microsoft Edge. It requires an installed Playwright module (or `SANDBOX_PLAYWRIGHT_MODULE` pointing to one). Run browser checks against a fresh isolated stack so its fixture has no previous viewing request. Historical result JSON and screenshots are not proof of a new deployment.

For current setup and a manual contact journey, use the [application README](../README.md). Sending a scenario or submitting a form creates or updates local database records and mock email.

## Dashboard screenshots and browser smoke check

`capture_dashboard.cjs` opens the running dashboard at <http://127.0.0.1:3002/#/overview> in headless Microsoft Edge. It checks the six lead-management pages (Overview, Inbox, Leads, Views, My leads and Properties), live analytics, a lead drawer, a saved-view filter, browser-local starring, the property catalogue and the property-assignment dialog. It does not currently capture/check Agent or Settings. Page errors and failed dashboard API calls fail the check.

With Microsoft Edge and Playwright installed, run from the application directory:

```powershell
# If Playwright is installed outside this project, set its module path first:
$env:SANDBOX_PLAYWRIGHT_MODULE = 'C:\path\to\node_modules\playwright'
node Tools/capture_dashboard.cjs
```

The script requires existing synthetic leads (email domains ending `.invalid` or `.test`) and a non-empty property catalogue. To populate an empty simulation stack, run the contact E2E above first. It captures nine images under `screenshots/dashboard/` (starting with [Overview](screenshots/dashboard/overview.png)) and refreshes [`sandbox-dashboard.png`](sandbox-dashboard.png) with the Overview image. It opens the property-assignment dialog without submitting it and stars a lead only in a disposable browser context.

See the [dashboard screenshot walkthrough](../frontend/dashboard/README.md#screenshot-walkthrough) for captions and feature explanations. The capture script is the lead-management screenshot/browser check; use the [settings verification workflow](#credential-settings-verification) for credential functionality. The older full-form browser script uses the previous dashboard's selectors.

Related guides: [Backend checks](../backend/README.md#development-checks), [Frontend](../frontend/README.md), and [Docker operations](../backend/docker/README.md). Fixture provenance is recorded in the [manifest](fixture-manifest.json).
