# Function Logic and IMAP/SMTP worker

[Repository overview](../../../README.md) → [Application guide](../README.md) → **Backend**

This is the backend of the production Questionnaire and Booking application.
`SANDBOX=TRUE` selects isolated sandbox storage and Mailpit; `SANDBOX=FALSE` selects
production storage and integrations. Acquaint properties/appointments remain live
read-only reads in Sandbox. It is one codebase in both modes; see
[sandbox operation, test data and first rollout](docker/SANDBOX.md).

## Services

- `app-function-logic`: FastAPI routes for booking, questionnaire, health, contact dashboard and credential administration.
- `imap-intake`: outbound SMTP sender and (legacy) inbound mail poller. Inbound intake is **webhook-based by default** (`TVE_EMAIL_INTAKE=webhook`); set `TVE_EMAIL_INTAKE=imap` to restore mailbox polling. When `SANDBOX=TRUE`, it reads from `mock-inbox` and sends through its mock SMTP instead.
- Five one-table Postgres databases: `contacts`, `enquiries`, `responses`, `tokens`, `settings`.
- A dedicated `pg-sandbox` service with five corresponding sandbox databases.

## Inbound email intake (webhook)

Inbound mail arrives over HTTP instead of IMAP polling. The mail provider (or a
mail rule / inbound-parse service) POSTs the raw message to:

```text
POST /webhook/thames-valley-estates/email-intake
  Content-Type: message/rfc822      -> the body is the raw RFC822 message
  Content-Type: application/json    -> {"raw": "<base64 RFC822>"}
Auth: X-TVE-Auth header (same secret as the other webhooks)
```

The message is parsed, matched and persisted by the same pipeline the other
adapters use, then any owed questionnaire invitation is sent. Responses:

```json
{"status":"accepted","contactId":42,"isEnquiry":true,"messageId":"<...>","invitationsSent":1}
```

`contactId` is `null` (and `invitationsSent` `0`) when the message is not a
genuine enquiry. `GET /webhook/thames-valley-estates/email-intake/health`
reports liveness. In webhook mode the worker no longer polls a mailbox; it only
sends follow-up invitations.

## Enquiry detection and property matching

Detection is strict. A message counts as a lettings enquiry only when it is a
real enquiry template from a known portal sender (template phrase **and** an
expected field: a property address/reference, a labelled field, or an
unambiguous recipient email), or a direct email carrying explicit rental or
viewing intent. Known non-enquiry categories — sales valuations, tenant
administration, account statements, tenancy/legal mail — are rejected even when
they reference a property.

Property matching runs in a fixed order: Acquaint reference, then exact address,
then a unique postcode-and-street match. If no stage resolves to exactly one
property but a stage found candidates, the result is **ambiguous** and the
contact is left for manual selection (no guessing).

A genuine enquiry with no unique match is stored as `unmatched_enquiry` (never
invited). Manual assignment links the property, moves the contact to
`enquiried`, and sends exactly one questionnaire invitation.

## Configuration

Compose loads the full `backend/.env` into Function Logic and the mail worker. Supplying `--env-file backend/.env` also makes its values available for Compose interpolation, including the frontend's `TVE_WEBHOOK_SECRET`. The frontend receives only its explicitly configured environment fields, not the full file or master key. Database DSNs are injected by Compose, so they do not need to be repeated in `backend/.env`.

Optional app-root `.env` overrides are not automatically merged with an explicit `--env-file`. See [Docker environment loading](docker/README.md#environment-loading) for multiple-file precedence and host-port/Postgres overrides. Environment-file edits require container recreation; dashboard overrides are resolved dynamically and take precedence over the running process environment.

For a local simulation, copy this directory's `.env.example` to `.env` and keep `sandbox=TRUE`. Compose supplies an isolated `pg-sandbox` service and synthetic test records can be added from Dashboard Settings. Mailpit handles all Sandbox mail; Acquaint property/appointment GET reads are enabled by valid shared credentials, but no CRM writes are made. Use one mode-variable spelling (`sandbox` or `SANDBOX`), with no conflicting values.

For production, keep `sandbox=FALSE` as the bootstrap default and supply real IMAP/SMTP and Acquaint values. On an initialized server, the persisted dashboard override selects the current mode. Before enabling Sandbox on an existing deployment, provision the separate sandbox database volume and unique account/signing key as described in [SANDBOX.md](docker/SANDBOX.md). Do not commit `.env` files.

## Dynamic credential settings

Open **Settings** in the lower sidebar at <http://127.0.0.1:3002/#/settings>. The panel uses authenticated `/dashboard/settings` endpoints and a same-origin proxy; see the [dashboard workflow](../frontend/dashboard/README.md#settings--workspace-credentials) and [settings API contract](app/README.md#credential-settings-api).

```text
Dashboard Settings
  → same-origin /sandbox/api/settings requests
  → Node proxy adds X-TVE-Auth
  → FastAPI validates, writes/tests and audits
  → settings_store encrypts/decrypts and invalidates its cache
  → pg-settings persists ciphertext and update metadata

Python credential consumers
  → settings_store: cached/decrypted override → process environment → unset
```

The browser gets safe field metadata and test results. Plaintext entered by a user is sent only as a replacement/candidate request; plaintext read from the store stays in trusted Python code, except for explicitly visible non-secret metadata fields.

### Resolution and persistence

For each allowlisted key, resolution is **encrypted database override → process environment → unset**. Integration-specific defaults are applied after resolution where supported. The store does not seed database rows from `.env`; an environment-backed value stays environment-backed until Save creates an override.

Sandbox uses its own settings database, namespaced cache and `TVE_SANDBOX_*`
environment fallbacks. Mode, webhook authentication and live read-only Acquaint
credentials are shared control settings; only mode can be edited while in Sandbox.
Other sandbox edits cannot overwrite production credentials.

| Operation | Effect |
|---|---|
| Read | Return a cached decrypted override, read/decrypt its database row, or fall back to the process environment when no row exists |
| Save | Validate one value, encrypt it, upsert the row with `updated_via=dashboard`, invalidate the local cache and return field metadata |
| Clear | Delete the override, invalidate the local cache and return metadata for the restored environment/unset source |
| Test connection | Combine candidate values with current settings, validate them and run a bounded probe without writing settings |

Clear does not erase `.env`, disable a configured environment credential or restore the previous database value. There is one current override per key, with no version history. Save/Clear are per-field operations, not an atomic update of a whole card. Editing `.env` while an override exists changes the eventual fallback, but does not replace that override.

Existing rows that cannot be decrypted fail closed. A configured but unavailable settings database is not treated as an empty database and does not silently switch credential consumers to environment values. Code running without a settings DSN, such as isolated tests/CLI callers, can use the environment-only path.

### Editable credential reference

All saved values must be strings of at most 4,096 characters, without control characters. Empty/whitespace-only values and angle-bracket placeholders such as `<set-a-real-key>` are rejected. Use Clear to remove an override. The table below distinguishes display secrecy from storage: **every saved value is encrypted**, including visible hostnames and ports.

| Group | Key | Display | Rules and runtime use |
|---|---|---|---|
| Acquaint | `ACQUAINT_BASE` | Visible | HTTPS origin, with a hostname; no embedded username/password, non-root path, query, fragment or invalid port |
| Acquaint | `ACQUAINT_API_KEY` | Masked | API credential used by `/v1/auth` |
| Acquaint | `ACQUAINT_SITE_PREFIX` | Masked | 1–64 letters, digits, underscores or hyphens; used by `/v1/auth` |
| IMAP | `TVE_IMAP_HOST` | Visible | Mailbox server hostname; production intake and explicit connection tests use TLS |
| IMAP | `TVE_IMAP_PORT` | Visible | Integer from 1 to 65,535; runtime/test default is 993 when unset |
| IMAP | `TVE_IMAP_USERNAME` | Visible | Mailbox login identity |
| IMAP | `TVE_IMAP_PASSWORD` | Masked | Mailbox login password |
| IMAP | `TVE_IMAP_MAILBOX` | Visible | Mailbox name; defaults to `INBOX` when unset |
| SMTP | `TVE_SMTP_HOST` | Visible | Outgoing server hostname; sandbox sends use the fixed mock-inbox route instead |
| SMTP | `TVE_SMTP_PORT` | Visible | Integer from 1 to 65,535; production/test default is 587 when unset |
| SMTP | `TVE_SMTP_USER` | Visible | Production SMTP login identity |
| SMTP | `TVE_SMTP_PASSWORD` | Masked | Production SMTP login password |
| SMTP | `TVE_ENQUIRY_FROM` | Visible | Plain email address, such as `lettings@example.com`; used on outgoing messages in both modes |
| Security | `TVE_TOKEN_KEY` | Masked | Signs/verifies applicant tokens; Save and Clear require rotation confirmation |
| Security | `TVE_WEBHOOK_SECRET` | Masked | Backend `X-TVE-Auth` credential; Save and Clear require rotation confirmation |
| Agent | `TVE_AGENT_N8N_WEBHOOK_URL` | Visible | Must exactly match the trusted HTTPS n8n Agent endpoint; other hosts, paths, schemes and URL decorations are rejected |
| Agent | `TVE_AGENT_N8N_WEBHOOK_SECRET` | Masked | Dedicated n8n Agent webhook authentication header; never the dashboard `TVE_WEBHOOK_SECRET` |
| Agent | `TVE_AGENT_DEFAULT_MODEL` | Visible | Allowlisted local model default (`qwen3.5:27b`, `qwen3:14b`, or `qwen3.6:latest`) |

The template values for hosts, ports and sender addresses are bootstrap values, not all store-level defaults. Configuration status reports presence/source, not successful provider authentication. The compact panel labels fields **Database override**, **Environment** or **Not configured**; the API retains `set`/`source` and masked secret metadata without exposing plaintext. The panel does not render secret suffix masks. Empty/template-placeholder environment values are reported as unconfigured; provider validity is checked by tests and actual operations.

The dashboard mode switch is persisted as the shared control setting
`TVE_SANDBOX_MODE`. The bootstrap sandbox flag, public URLs, viewing/tuning values,
DB DSNs, Postgres accounts, Compose ports, master key and cache TTL are startup
configuration, outside the editable credential scope.

The Agent proxy fails closed unless `TVE_AGENT_N8N_WEBHOOK_SECRET` is available from encrypted Settings or the backend environment. It uses that dedicated value as `X-TVE-Auth` on outbound n8n requests and never falls back to `TVE_WEBHOOK_SECRET`. To prevent credential forwarding to an untrusted destination, the configured URL must exactly match `https://tve-n8n.hedgedge.info/webhook/tve-lead-intelligence-agent`; other hosts, paths, HTTP, redirects via an alternate URL, userinfo, query strings and fragments are rejected before a network request. Configure the exact header credential expected by the n8n Agent webhook in the Agent Settings group. The secret is write-only in the dashboard.

Simulated booking-email events/debug output retain only minimal event and operation metadata, recipient role and contact IDs. Customer-facing email subjects and bodies are not recorded or exposed in `/debug/events` or the live-event viewer; email generation itself is unchanged.

### Provisioning and vault configuration

Run from the application directory:

```powershell
python -m pip install -r backend/requirements.txt
python Tools/provision-settings.py
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up --build -d
```

The helper requires an existing `backend/.env` and the `cryptography` dependency. It provisions a missing/empty/placeholder master key, preserves an existing valid key and refuses to overwrite an invalid non-placeholder key. It also adds the local webhook bootstrap assignment when absent and sets `TVE_SETTINGS_TTL_SECONDS=5`. Rerunning preserves the encryption key but resets the TTL; see [tool behaviour](../Tools/README.md#credential-vault-provisioning).

| Startup variable | Meaning |
|---|---|
| `TVE_SETTINGS_MASTER_KEY` | Fernet key used by both Python processes; absent/invalid values lock the vault |
| `TVE_SETTINGS_TTL_SECONDS` | Override cache lifetime; default 5 seconds, 0 disables caching, negatives become 0, maximum 3,600 seconds, invalid integers use the default |
| `TVE_DB_SETTINGS` | Named settings-pool DSN, injected by Compose; not editable in the panel |

The persistent source for the master key is ignored `backend/.env`; it is loaded into Function Logic and the mail worker, not the frontend or Postgres container. The frontend receives only the webhook bootstrap credential needed for proxy authentication.

### Encryption, metadata and audit

`pg-settings` holds the `settings` table, with `key`, `group_name`, `is_secret`, `ciphertext`, `updated_at` and `updated_via`. Fernet from `cryptography` provides authenticated encryption using AES-128-CBC and HMAC; each encryption uses a fresh IV. Keys/group names and update metadata are visible, but the value column contains ciphertext only.

Python consumers decrypt values in memory. The TTL cache holds only successful decrypted database results; environment fallbacks and missing values are not cached. The API's metadata snapshot refreshes database-backed fields before returning them. Secrets have no `value` property in responses: they expose configuration status and `••••1234`-style masks. Values of four characters or fewer expose only `••••`, so a short site prefix is not revealed in full.

Save/Clear/probe-result events use `settings_saved`, `settings_cleared` and `settings_tested`. The existing privacy-filtered `record_event` mechanism permits only allowlisted key/group/operation metadata for these events; candidate values, passwords and provider error text are excluded. The operational event feed is bounded and process-local, not a persistent credential history or staff-identity audit database. Validation failures before a probe/write are recorded through normal request auditing rather than a successful settings event.

### Dynamic runtime behaviour

| Consumer | When it resolves settings | Effect of a change |
|---|---|---|
| Acquaint | Before dynamic reads | A changed base/key/site-prefix snapshot replaces the authenticated client and clears its cached property catalogue |
| SMTP | Immediately before each send, including follow-up batches and dashboard resends | Subsequent messages use the effective configuration; in-flight sends are not restarted |
| IMAP | At the start of each production poll | New settings apply on the next poll; host/port/username/mailbox changes select a separate persistent checkpoint; password edits retain it |
| Applicant tokens | On issuance and verification | New links use the effective key; signatures made with a different key are rejected |
| Webhook auth | On each protected request | Accepts the effective settings secret and the process's startup bootstrap secret during rotation |

Save/Clear immediately invalidates the changed key in the API process. Other processes observe cached-override changes after their configured TTL when they next read the key. At the default five-second TTL, the production mail worker normally observes edits at its next poll; the production poll interval defaults to 60 seconds. Increasing the TTL can span multiple polls. An already-started request/send is allowed to finish with its earlier credential snapshot.

Sandbox mode bypasses real IMAP intake and routes outgoing mail to Mailpit. Only the
sandbox sender/settings are effective for those messages. Missing/incomplete Acquaint
credentials select fixtures and an empty synthetic appointments list in sandbox;
configured appointment-read failures remain errors. The existing journey catalogue
can fall back to synthetic properties on provider HTTP failures. Acquaint settings
are read-only in Sandbox; edits in Production invalidate the frontend property list.

Production IMAP uses a private atomic `checkpoint.json` in `TVE_IMAP_STATE_DIR` (default `/var/lib/tve-imap`), mounted only in the worker from the isolated `tve-imap-checkpoint-data` volume. First connection to a new mailbox identity baselines the server's current `UIDNEXT-1` without fetching history; subsequent polls resume after the last completed UID. Actual `UIDVALIDITY` is passed to intake. A changed `UIDVALIDITY` baselines the fresh mailbox again, with a reason-only log. Selection is read-only and retrieval uses `BODY.PEEK[]`, leaving Seen flags untouched.

The file stores hashed host/port/username/mailbox identities, actual UIDVALIDITY/cursors and at most 8,192 SHA-256 digests of recently completed Message-ID/content dedupe keys; it contains no raw mail, usernames or credentials. Checkpoints for up to 128 identities are preserved across configuration changes and worker recreation; reaching that identity bound pauses intake rather than evicting old cursors. Recent-key dedupe also covers repeated messages arriving under changed identities, but is bounded rather than a permanent mail ledger. A successfully classified non-enquiry advances as a completed no-op. Failed fetch/parser/intake/checkpoint writes stop the batch without advancing past the failed UID; earlier completed UIDs are already durable.

Corrupt/unavailable state or missing/invalid UID metadata fails closed: production intake and, for checkpoint failures, the worker's follow-up delivery pause until corrected. The directory must already exist and be writable on persistent storage; Compose fixes its path, and the Docker image provisions UID 0 ownership and mode `0700` for its current root worker. JSON and lock files use mode `0600` on Linux. An OS lock permits only one poll owner, and JSON replacement fsyncs both file and directory. `sandbox=TRUE` does not access this state and keeps Mailpit behaviour. See [Docker persistent data](docker/README.md#persistent-data) for backup/recovery.

Intake database writes and checkpoint replacement are separate commits. A crash after intake commits but before its checkpoint is durable retries that UID (at-least-once intake); the existing email-keyed contact upsert and forward-only journey status remain in use. This does not provide transactional exactly-once SMTP delivery.

### Connection tests

Tests accept optional unsaved values from the relevant card, with omitted fields resolved from the store/environment. Candidate keys must belong to that group. All candidates and configured non-empty values are validated before opening a connection; success does not automatically save anything. A response such as `{"ok": false, "message": "Connection test failed..."}` represents a completed failed probe, while malformed/missing configuration is rejected with 422.

| Group | Probe | Successful result establishes |
|---|---|---|
| Acquaint | Authenticate at `/v1/auth`, then GET one property-list page with page size 1 | Authentication and that read-only endpoint are available |
| IMAP | TLS connection, login and mailbox selection with `readonly=True`, then close | The login and selected mailbox are accessible without modifying messages |
| SMTP | Connect and issue EHLO/HELO; port 465 uses implicit TLS, other ports use a plain SMTP connection | The server is reachable and accepts EHLO; authentication, STARTTLS and mail delivery are not tested |

Acquaint requests and mail sockets use five-second timeouts; the probe operation has a 12-second async deadline. Provider failures become a fixed sanitized failure message rather than raw exception text. There is no Security-group connection test.

Explicit tests use the current/candidate endpoints even in sandbox mode. For example, testing configured production IMAP credentials can log into that real mailbox while the worker continues using Mailpit. To probe local SMTP without changing saved configuration, use candidate host `mock-inbox` and port `1025` through the Compose backend.

### Token signing key rotation

1. Enter the replacement `TVE_TOKEN_KEY` in Settings and choose Save.
2. Confirm that links signed with the previous key will fail verification.
3. Reissue affected applicant links. For contacts at `enquiried`/`Questionnaire_sent`, use **Resend invitation**; other stages require the appropriate journey link-issuance flow.

New contact-bound questionnaire/booking links are purpose-scoped signed opaque tokens stored in the tokens database. Signed guest tokens also resolve the current key per request. A key change does not erase contact/questionnaire/viewing records or token rows; it changes which signatures verify. Clearing an override also changes the key when the environment fallback differs.

For compatibility, older unsigned opaque tokens are accepted while the effective key equals the running process's environment/bootstrap key. A differing vault key disables that legacy path; issuance/resend upgrades legacy or stale-key rows to signed tokens. Restoring a previous signing key can make previously issued signatures valid again if their database row and expiry still permit use. Rotation is not a separate permanent-revocation ledger.

### Webhook authentication rotation

The proxy holds its webhook credential at startup. The backend accepts both the current vault value and its startup bootstrap value so saving a new override does not immediately disconnect the existing proxy.

1. Save the new `TVE_WEBHOOK_SECRET` in Settings and confirm the rotation prompt.
2. Set that same replacement value in `backend/.env`.
3. Recreate Function Logic and the frontend using the explicit interpolation file, from the application directory:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up -d --force-recreate app-function-logic web-frontend
```

4. Refresh Settings. The proxy now sends the replacement credential, and the recreated backend no longer accepts the old bootstrap value.

A plain `restart` reloads neither edited `.env` values nor the proxy's startup credential. Recreating only the frontend switches what it sends but leaves the old bootstrap secret accepted by the backend. The database override survives recreation. Clear restores the current bootstrap value and also requires confirmation; align the proxy with that effective value if necessary.

### Locked vault, recovery and backups

| Condition | Behaviour | Recovery |
|---|---|---|
| Master key absent/invalid | `vault_unlocked=false`; Save/Clear return 503; environment-only keys still resolve | Provision/correct the key and recreate both Python services |
| Valid key cannot decrypt an existing row | That override fails closed; metadata keeps its source/update fields but cannot reveal its value/mask | Restore the original key, or replace/clear affected overrides using a valid key |
| Settings database unavailable | Settings requests return an unavailable response; credential consumers do not treat a configured outage as an empty store | Check `pg-settings` health and the injected DSN/network |
| Browser displays a locked/unavailable state after recovery | The page retains its last loaded state until refreshed | Choose **Refresh settings** |

**Vault unlocked** means the configured key has valid Fernet syntax; it does not prove every existing row was encrypted with that key or that a provider accepts its credential. With the settings DB reachable, metadata remains available when encrypted rows cannot be read. The normal process bootstrap webhook credential is still accepted if the vault cannot resolve its webhook override, allowing access to repair the configuration.

Back up `tve-pg-settings-data` together with the original master key and relevant bootstrap configuration. Restoring only ciphertext with a new key cannot recover the values. Master-key replacement does not automatically re-encrypt stored rows. If the original key is unavailable, establish a valid replacement and re-enter overrides; Clear can remove unreadable rows and restore environment values. There is no bulk master-key rotation/re-encryption or historical-value restore action in the panel.

## Development checks

Run from this `backend/` directory:

```powershell
python -m pytest -q
python -m compileall -q app tests
```

For the credential rewire specifically:

```powershell
python -m pytest -q tests/test_settings_store.py tests/test_settings_api.py tests/test_dynamic_credentials.py
```

The subset covers ciphertext roundtrips, write-only masking, short-secret protection, environment fallback, TTL expiry/invalidation, locked/decryption-failed behaviour, credential-only scope, validation, protected routes, dual webhook auth, candidate-only tests, read-only probes, dynamic consumers and token/cursor rotation. Provider/database operations are stubbed in these unit/API tests; Settings provides the separate live connection probes.

Local fixture helpers, viewer tests and the contact-driven API E2E check live under [`../Tools/`](../Tools/README.md). `tests/test_production_journey.py` verifies `sandbox=FALSE` questionnaire persistence, purpose-scoped booking links, real-catalogue ID resolution, viewing-request storage, SMTP selection and dashboard tracking using mocked databases/providers/SMTP. Acquaint remains read-only and requests need staff confirmation. See [implementation limits](../README.md#current-implementation-limits) for legacy guest persistence and delivery limitations.

Next: [Docker operations](docker/README.md) for service commands; [Function Logic package](app/README.md) for code organization; [Frontend](../frontend/README.md) for the caller-facing applications. The [environment template](.env.example) lists the provider connection fields.
