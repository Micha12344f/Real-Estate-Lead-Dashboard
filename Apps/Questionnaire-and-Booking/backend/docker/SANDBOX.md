# Isolated sandbox

## What the dashboard switch does

**Production** selects the five existing production stores, real IMAP and real SMTP.
**Sandbox** pauses live mailbox intake and selects five separate databases on
`pg-sandbox`, with its own account, internal network and `tve-pg-sandbox-data` volume:

| Domain | Sandbox database | Configuration |
|---|---|---|
| Contacts / viewing requests | `sandbox_contacts` | `TVE_SANDBOX_DB_CONTACTS` |
| Portal enquiries | `sandbox_enquiries` | `TVE_SANDBOX_DB_ENQUIRIES` |
| Questionnaire answers / legacy guest submissions | `sandbox_responses` | `TVE_SANDBOX_DB_RESPONSES` |
| Questionnaire / booking tokens | `sandbox_tokens` | `TVE_SANDBOX_DB_TOKENS` |
| Test settings | `sandbox_settings` | `TVE_SANDBOX_DB_SETTINGS` |

Compose injects these DSNs into both API and mail worker. It uses the same schema
sources as production and initializes them only on a new sandbox volume. Schema
changes still require explicit migrations against **both** environments.

Sandbox follow-ups, questionnaire submissions, guest bookings, viewing requests,
dashboard reads and dashboard actions use only sandbox journey records. All sandbox
email goes to Mailpit, even if real SMTP credentials exist. Acquaint properties and
appointments remain **live read-only GETs** through the existing allowlisted adapter.
Sandbox appointment creation, notes and tasks are mock operations; no real CRM
mutation is performed. Without Acquaint credentials, the existing property fixtures
remain available. Live appointment read failures do not become empty successful reads.

The n8n Agent's tools currently query production backing stores. Agent chat therefore
returns a clear unavailable message in Sandbox until those tools have their own
sandbox data path. This does not block properties, leads, questionnaire or booking UI.

## Shared control settings

The mode switch, webhook authentication and Acquaint credentials live in the existing
control settings database. This lets staff resume Production even when sandbox storage
is unavailable, and keeps the live read-only property/appointment feature working.
Acquaint and webhook settings are **read-only while in Sandbox**; only the mode switch
may write control settings. Other edits use the selected environment's settings DB.
Sandbox environment fallbacks use `TVE_SANDBOX_*`, never production `TVE_*` values.
The vault encryption master key is deployment-level infrastructure shared by the two
settings stores, not a sandbox journey secret.

Set `TVE_SANDBOX_TOKEN_KEY` to a separately generated signing secret. Sandbox signatures
also have their own HMAC domain, so test and production links cannot be interchanged
even if signing secrets are accidentally equal. Old unsigned production tokens are
not accepted in Sandbox.

## Frontend testing

1. Open **Settings → Switch to Sandbox**. The switch checks that every sandbox
   database/schema is reachable before persisting the change.
2. Click **Load test data**. Six synthetic `demo.*@sandbox.invalid` leads cover the
   application stages: new enquiry, invitation sent, questionnaire completed, viewing
   requested, booked and done. They use selectable real property IDs when available.
3. Open Leads, Overview, lead details, questionnaire/booking links and Mailpit to
   inspect the journey. Sandbox workers can process test leads normally.
4. Repeated **Load test data** clicks fill missing examples/links without resetting
   existing test journeys or answers. Test data persists across mode switches.
5. Switch back to Production to resume the real journey. No sandbox data is promoted.

The dashboard reloads after switching, keeps stars separate per environment and sends
`X-TVE-Mode` with data/action requests. A stale browser request receives 409 before
an action can act on a same-numbered contact in the other environment. Each API request
and worker batch pins its mode through awaits and SMTP threads; already-running work
finishes in its original environment. The worker observes the switch at its next poll.

Public frontend URLs can stay the same for both modes. Optional
`SANDBOX_QUESTIONNAIRE_APP_BASE_URL` / `SANDBOX_BOOKING_APP_BASE_URL` override sandbox
links. The data store and token signature define the environment boundary.

## Failure behaviour

Missing sandbox DSNs, a sandbox DSN matching a production DB, missing schemas, or an
unreachable sandbox store never fall back to production. Enabling Sandbox is rejected
when its stores are not ready; an already-selected sandbox worker pauses intake and
follow-ups if any sandbox store cannot be opened. The control mode endpoint remains
available to resume Production. Neither seeding nor storage initialization copies live
applicant data. Existing production rows changed by the old shared-store implementation
are not automatically repaired: review those separately against Mailpit/delivery records.

## First estate-server rollout

This is a one-time **operator-provisioned infrastructure addition**. The deployer never
creates a missing database volume/service/network. After `pg-sandbox` has been started
and healthy, its validation allows this exact new service plus the matching isolated
DSNs/network on the API and worker. Other DB, network, volume and app runtime changes
continue to fail closed.

Under the existing deployment lock described in [DEPLOYMENT.md](DEPLOYMENT.md):

1. Retain the current release source/image tags, runtime files and database backups.
   Stop the mail worker during the transition, especially if the old implementation
   is currently in Sandbox against shared production storage.
2. Preserve both existing `.env` files and every existing volume. Add a separately
   generated, URL-safe `SANDBOX_POSTGRES_PASSWORD` and `TVE_SANDBOX_TOKEN_KEY` to the
   backend runtime file; ensure app-root `.env` does not override the password
   unexpectedly. Never reuse production Postgres credentials or the production token
   signing key for Sandbox.
3. From the application directory, using interpolation order
   `--env-file backend/.env --env-file .env` (omit the second file if the server has no
   app-root `.env`), run only:

   ```powershell
   docker compose --env-file backend/.env --env-file .env -f backend/docker/docker-compose.yml up -d pg-sandbox
   ```

   This creates only the new sandbox database service/volume and initializes the five
   databases. It does not recreate the existing API, worker or production DB services.
4. Verify `pg-sandbox` is running/healthy on the new account, the sandbox volume is
   mounted, all five schemas and `tve_submissions` exist, and API/worker can reach it.
   Do not switch the persisted dashboard mode to Sandbox before this check.
5. Push/merge the tested release through normal Application CI. The deploy gate accepts
   this one additive Compose change only when the already-provisioned sandbox service,
   volume and network exactly match the release. It rebuilds/recreates the three app
   services, never the database. If validation fails, repair/provision the service under
   the same lock; do not bypass the gate.
6. Verify all six databases/services are healthy, auth is enforced, and the mode endpoint
   and dashboard read from the intended stores. In Sandbox, load synthetic data and
   verify mail appears in Mailpit. Resume Production as appropriate.

Rollback restores application/source/runtime configuration and original images; it
does not delete the operator-provisioned sandbox volume/service or reverse lead/status
changes. Keep the worker stopped if rollback would re-enable the old shared-store
Sandbox implementation.

## Verification

`backend/tests/test_sandbox_isolation.py` tests pool/config/cache isolation, pinned
operations, cross-mode token rejection, legacy guest storage and fail-closed workers.
Application CI runs the full contact journey followed by
`Tools/run_sandbox_isolation_e2e.py`, which exercises real Postgres mode switching,
seeding, stale-action rejection and unchanged production data fingerprints. The
integration script requires a sandbox-bootstrapped simulation stack and refuses the
estate-server's `sandbox=FALSE` bootstrap.
