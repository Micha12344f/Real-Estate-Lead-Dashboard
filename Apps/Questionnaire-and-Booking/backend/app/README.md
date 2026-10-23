# Function Logic package

[Application guide](../../README.md) → [Backend](../README.md) → **Function Logic**

`main.py` creates the FastAPI application, mounts the booking, questionnaire, contact-dashboard, credential-settings and health routes, and adds request IDs and privacy-filtered operational events. `frontend/serve-local.js` proxies only fixed local API paths to this service.

Provider adapters and journey logic are under `services/`; HTTP endpoints are under `routes/`; the inbound/outbound mail worker is `workers/imap_intake.py`. Sandbox uses dedicated databases for every journey/settings record, synthetic fixtures and Mailpit. Acquaint property/appointment reads remain live and read-only. The same code serves Production. See [sandbox operations and first rollout](../docker/SANDBOX.md).

The authenticated lead dashboard lives in `routes/dashboard/`: `/dashboard/leads` and `/dashboard/leads/{contact_id}` return per-contact read models stitched from the four journey containers (the detail route adds the raw enquiry email), `/dashboard/funnel` and `/dashboard/metrics` aggregate them, and `/dashboard/actions/*` assign a property or resend the questionnaire invitation. `/dashboard/settings/*` administers credentials in the separate fifth database. `services/dashboard/` builds the read models (`readmodel.py`), derives scores and attention flags (`scoring.py`) and computes the funnel and metrics (`analytics.py`); property details resolve through the same selectable catalogue (live Acquaint reads of statusId 0 and 1 when credentials are configured, fixtures otherwise) as the journey.

See the [dashboard guide](../../frontend/dashboard/README.md) for screenshots, score weights, attention thresholds and the UI-to-API mapping. The list endpoint returns the 200 most recently updated contacts; analytics use all contacts. All dashboard routes require the same webhook authentication dependency as the applicant APIs.

`DELETE /dashboard/leads/{contact_id}` requires the dashboard's existing standard `X-TVE-Auth` authentication, `X-TVE-Mode` matching the request's pinned mode, `X-TVE-Delete-Contact-UID` matching the UUID returned with that lead, `X-TVE-Delete-Operation` (a per-confirmation UUID), and `X-TVE-Delete-Initiated-At` (timezone-aware ISO timestamp). The explicit **Delete lead** confirmation immediately sends the authenticated DELETE; there is no second credential prompt. A new operation must start within seven days of that timestamp; a matching unresolved operation can be retried later without expiring. The dashboard keeps these non-secret replay values stable across retries and same-tab reloads in session storage, clearing them on success. The same standard dashboard authentication protects Production and Sandbox deletion; do not add a public or unauthenticated destructive endpoint. Missing/mismatched mode returns 409; missing contact returns 404; partial/unverified failure returns 503; an operation/identity conflict returns 409. Request auditing includes route/method/status, never authentication headers.

After a deletion attempt, a same-tab reload shows a **Deletion attempt needs retry or review** notice on the dashboard, even if the contact is absent from the lead list. **Retry deletion** reopens the confirmation with the saved ID/UUID/operation reference and immediately retries under the same standard dashboard authentication. A 404 on retry is not proof that all child databases are clear, so investigate it rather than discarding the reference. If session storage is lost, an operator with access to the *selected mode's contacts database* can look up `contact_id, contact_uid, operation_id, initiated_at` in `lead_delete_operations` for the exact requested ID and retry `DELETE /dashboard/leads/{id}` with those values in the four named headers plus the matching `X-TVE-Mode` and standard `X-TVE-Auth`. Do not substitute a newly generated operation or another mode; when no pending row exists, inspect the four stores before manually concluding what happened. Never place authentication headers in shared logs or browser storage.

**Required migration before deploying any new backend writer:** apply [`002-lead-delete-migration.sql`](../docker/init-contacts/002-lead-delete-migration.sql) to each *existing* contacts database (production and sandbox) via your normal migration process; new volumes receive the same schema from `001-contacts.sql`. Existing volumes do not rerun Docker init scripts. Verify `contacts.contact_uid` and `lead_delete_operations` are present in each DB before enabling deletion. If absent, guarded writers fail closed; do not roll out the new backend first.

Deletion uses parameterized `id`/`contact_id` predicates in the selected four journey databases only: all tokens for the ID, questionnaire response, enquiry, then contact. The contacts DB stores a **pending-only** operation marker containing contact ID/UUID, operation UUID and timestamp. Known enquiry/response/token writers hold the contact row lock through their child write and reject a pending marker. The marker is committed before child deletions, so the same operation can reconcile a post-commit verification failure even if the contact row is gone. After verifying all four tables absent, deletion removes that marker before returning 200; a repeated DELETE then returns 404. The immutable contact UUID supplied by the client prevents that old request from deleting a newly assigned contact with the same numeric ID. Unresolved markers do **not** expire: they fence writers until verified reconciliation, and require manual attention if the original browser tab/session is lost. Legacy backfill scripts are disabled as soon as the migration table exists and also recheck under the current contact lock before each write. No cross-database transaction exists. There is no guarantee about writers outside these guarded paths, already-queued mail, later re-intake, or external/legacy data; `tve_submissions`, CRM and settings are not deleted. Demo mode in the local frontend deletes only in-memory demo leads and does not call the backend.

Design references: [OWASP object-level authorization](https://owasp.org/API-Security/editions/2023/en/0xa1-broken-object-level-authorization/), [PostgreSQL DELETE](https://www.postgresql.org/docs/18/sql-delete.html), [PostgreSQL database isolation](https://www.postgresql.org/docs/18/manage-ag-overview.html), and [RFC 9110 DELETE semantics](https://www.rfc-editor.org/rfc/rfc9110). A repeated DELETE returning 404 does not change the deleted state.

## Package responsibilities

| Module | Responsibility |
|---|---|
| `main.py` | Router mounting, dynamic webhook authentication, request IDs, normalized errors and privacy-filtered audit events |
| `routes/dashboard/settings.py` | Settings request models, key/group validation, metadata responses, writes/deletes and bounded connection probes |
| `services/shared/settings_store.py` | Credential scope, validation, Fernet encryption, environment fallback, decrypted-result TTL cache and safe metadata snapshots |
| `services/shared/database.py` | Named async connection pools for `contacts`, `enquiries`, `responses`, `tokens` and `settings` |
| `services/shared/sandbox_data.py` | Synthetic journey examples loaded from the Dashboard without resetting existing test journeys |
| `services/shared/acquaint.py` | Read-only clients rebuilt on credential changes; auth/property caches tied to the current credential snapshot |
| `services/enquiries/followup.py` | Current mail settings immediately before delivery, including sandbox sender resolution |
| `workers/imap_intake.py` | Per-poll IMAP settings, persistent per-identity replay checkpoints, Mailpit routing and due invitations |
| `workers/imap_checkpoint.py` | Private atomic worker state, UIDVALIDITY baselines, bounded hashed dedupe and fail-closed storage |
| `services/shared/tokens_store.py` | Purpose-scoped signed contact tokens with per-issuance/per-verification key resolution |
| `routes/booking/guest.py`, `routes/questionnaire/submissions.py` | Signed guest-token issuance/verification using the current settings key |

Cross-container journey references are stitched in application code using `contact_id`. Settings rows are independent key/value overrides, not contact records. The settings DSN is injected by Compose and registered under the named `settings` pool.

`services/shared/config.py` pins a mode snapshot per API request/worker batch. `database.py` selects pools per mode and rejects missing or production-aliasing sandbox DSNs. Sandbox enabling checks all stores; sandbox uses Mailpit and sandbox-only sender/token/settings, while shared Acquaint/webhook settings are read-only. Live Acquaint property and appointment GETs remain available, but CRM mutations are blocked. Agent chat is paused in Sandbox because its n8n tools currently read production data. Explicit connection probes use configured/candidate endpoints in either mode.

## Credential settings API

Every route below is mounted behind `Depends(verify_webhook_auth)`, the same `X-TVE-Auth` dependency used by the dashboard/applicant APIs. The normal Compose stack forces `TVE_AUTH_DISABLED=false`. The frontend proxy supplies its startup secret server-side; the browser is not required to know it.

| Method and route | Request | Response/effect |
|---|---|---|
| `GET /dashboard/settings` | No body | Vault status and grouped metadata for every allowlisted credential field |
| `GET /dashboard/settings/mode` | No body | Effective mode, source and bootstrap mode |
| `PUT /dashboard/settings/mode` | `{"sandbox": true}` | Verify sandbox stores before switching mode |
| `POST /dashboard/settings/sandbox/seed` | Empty JSON body | Add six synthetic test leads; Sandbox-only and preserves existing journeys |
| `PUT /dashboard/settings/{key}` | `{"value": "replacement"}` | Validate/encrypt one override, invalidate local cache, audit and return the updated field metadata |
| `DELETE /dashboard/settings/{key}` | No body | Delete one override, invalidate local cache, audit and return the fallback field metadata |
| `POST /dashboard/settings/test/{group}` | Optional `{"values": {"ENV_KEY": "candidate"}}` | Validate candidates/current settings, probe without persistence and return `{"ok": true/false, "message": "..."}` |

`{key}` is the exact case-sensitive environment-key name, such as `ACQUAINT_API_KEY` or `TVE_SMTP_PORT`. Test groups are lowercase `acquaint`, `imap` and `smtp`; `security` has no test route. Shared mode/auth/Acquaint settings live in the control database; Acquaint/webhook writes are refused in Sandbox. The [backend key reference](../README.md#editable-credential-reference) lists the full allowlist and rules.

### Request validation

- Save accepts a strict string `value` and forbids extra top-level fields.
- Test accepts a `values` map whose values are strict strings and forbids extra top-level fields. Ports must therefore be submitted as strings such as `"993"`, not JSON numbers.
- Each candidate must be allowlisted and belong to the requested group. Omitted candidates use current store/environment values; a supplied candidate replaces that field for the probe only.
- Values must be non-empty/non-placeholder, at most 4,096 characters and free of control characters. Acquaint origin/site-prefix, mail ports and sender address have additional key-specific rules.
- Empty input is not a request to clear a credential. DELETE is the override-removal operation.
- Save/DELETE each affect one field. A candidate test can cover several unsaved edits, but does not turn their later individual saves into a transaction.

### Metadata contract

GET returns `vault_unlocked` and a `groups` array; each group contains `id`, `label` and `fields`. This abbreviated example shows one secret field; the live response includes every group and field:

```json
{
  "vault_unlocked": true,
  "groups": [
    {
      "id": "acquaint",
      "label": "Acquaint",
      "fields": [
        {
          "key": "ACQUAINT_API_KEY",
          "group": "acquaint",
          "is_secret": true,
          "label": "Acquaint API key",
          "env": "ACQUAINT_API_KEY",
          "set": true,
          "source": "database",
          "updated_at": "2026-10-06T10:00:00+00:00",
          "updated_via": "dashboard",
          "masked": "••••1234"
        }
      ]
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `key`, `env` | Allowlisted identifier and matching bootstrap environment-variable name |
| `group`, `label` | Card membership and human-readable label |
| `is_secret` | Whether plaintext must be omitted from responses and secret inputs treated as write-only |
| `set` | A database row or non-placeholder environment value is configured; not an authentication result |
| `source` | `database`, `environment` or `unset` |
| `updated_at`, `updated_via` | Current database-row update timestamp/channel; null without an override |
| `placeholder` | Optional input hint for selected visible fields |
| `masked` | Secret fields only: a mask plus last four characters when the value is longer than four; null if unset/unreadable |
| `value` | Non-secret fields only: effective visible value, or null when unset/unreadable |

Secret fields never include `value`, including PUT/DELETE responses. Both write routes return a single refreshed field object, not the grouped GET envelope. Locked/decryption-failed rows can still report their database source and update metadata without decrypting the value. `vault_unlocked` checks key format/availability, not successful decryption of every row.

### Candidate connection tests

For example, a Compose-backend SMTP probe against Mailpit uses this body without replacing the saved SMTP host/port:

```json
{
  "values": {
    "TVE_SMTP_HOST": "mock-inbox",
    "TVE_SMTP_PORT": "1025"
  }
}
```

Required effective fields are base/API-key/site-prefix for Acquaint, host/username/password for IMAP and host for SMTP. Optional port/mailbox values use their runtime defaults. Configured non-empty values are validated even when the particular probe does not authenticate with them.

Acquaint performs authentication and one read-only property page. IMAP performs TLS login and read-only selection. SMTP performs EHLO/HELO, using implicit TLS on port 465; it does not authenticate, negotiate STARTTLS or send mail. The probe has five-second request/socket timeouts and a 12-second async deadline. Missing/invalid configuration is rejected before network work; provider errors return a fixed sanitized `ok=false` message. Candidates can replace unreadable rows for a probe because explicitly replaced values are not decrypted from the store.

### Errors and authentication rotation

| Status | Meaning |
|---|---|
| 401 | Neither the effective webhook secret nor the process bootstrap credential matched |
| 404 | Unknown settings key or test group |
| 422 | Invalid request model/value, cross-group candidate or missing required probe configuration |
| 503 | Vault locked, ciphertext could not be decrypted, or settings storage unavailable |
| 409 | Shared control settings cannot be edited or test-data seeding requested outside Sandbox |
| 200 with `ok=false` | Valid probe request completed with a provider/connection failure |

Backend errors use the application's normalized `error` envelope with code, message and request ID. `RequestValidationError` responses do not echo input-bearing validation detail. The settings proxy preserves the HTTP status and replaces backend error details with its own sanitized message/code; see [frontend secret handling](../../frontend/README.md#credential-settings-proxy).

Webhook authentication resolves the current settings key per request and uses constant-time comparisons. During rotation, it also accepts the startup bootstrap value. If resolving the vault secret fails, bootstrap access remains available to repair settings. To stop accepting the old bootstrap secret, update the environment and recreate Function Logic and the proxy together; see [rotation steps](../README.md#webhook-authentication-rotation).

## Store API and dynamic consumers

`settings_store.get(key)` returns the current plaintext value to trusted Python code, or `None` for unset/unknown keys. `get_many(group)` resolves a group's values. `set(...)` verifies key/group/secrecy metadata, encrypts and upserts; `delete(key)` removes the row. `snapshot()` is the safe dashboard-facing metadata boundary.

The cache contains successful decrypted database results only and is namespaced by mode. Its default TTL is five seconds; `invalidate(key)` follows writes/deletes, and `invalidate_all()` clears all local entries. Other processes have independent caches. No missing/environment result is cached, so a newly introduced override can be observed on the next read. Master-key changes prevent cached plaintext from bypassing locked/decryption-failed handling.

Acquaint clients compare the resolved credential snapshot before using cached auth/catalogue data. SMTP resolves immediately before each individual send. IMAP resolves at each production poll, selects a durable hashed host/port/username/mailbox checkpoint, and uses bounded durable hashed Message-ID/content dedupe. New identities and UIDVALIDITY changes baseline without historic fetch; returning identities resume their saved UID. Token issuance/verification resolves the signing key each time. Detailed timing, legacy-token compatibility and integration-mode behaviour are in [backend dynamic settings](../README.md#dynamic-runtime-behaviour).

## Operational events

Settings Save/Clear and completed probes call `record_event` with the event name, allowlisted `setting_key`/`setting_group` where applicable, and `operation=succeeded/failed`. The logger selects fixed messages and never accepts settings values as audit fields. Request auditing supplies method, status, request ID and allowlisted route templates rather than raw credential-bearing paths or request bodies.

The optional `/debug/events` feed is a bounded in-memory operational feed, not an events database or settings-history store. It retains at most 500 events in process memory and returns at most 100 per response, with an optional `after` cursor. Legacy guest questionnaire persistence remains in `services/questionnaire/store.py`; see [current implementation limits](../../README.md#current-implementation-limits) for the remaining migration work.

Next: [Docker operations](../docker/README.md) to run this package; [Frontend](../../frontend/README.md) to see its callers; [Tools](../../Tools/README.md) for fixtures and the event viewer.
