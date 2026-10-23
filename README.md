# Thames Valley Estates

The Questionnaire and Booking applications are production applications. The dashboard's **Sandbox** mode selects isolated test databases and Mailpit in the same codebase; **Production** selects the existing live stores and configured integrations. Both modes can read live Acquaint properties and appointments, but only read-only.

## Start locally (`SANDBOX=TRUE`)

Requirements: Windows 10/11, PowerShell, Git, and Docker Desktop with Compose v2. From the repository root:

```powershell
if (-not (Test-Path Apps/Questionnaire-and-Booking/backend/.env)) {
    Copy-Item Apps/Questionnaire-and-Booking/backend/.env.example Apps/Questionnaire-and-Booking/backend/.env
}
# Keep sandbox=TRUE in the file (SANDBOX is also accepted; use one spelling).
Set-Location Apps/Questionnaire-and-Booking
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up --build -d
```

No real mail credentials are required for Sandbox. Real Acquaint credentials can be present for live read-only properties/appointments; all other Sandbox data stays in dedicated databases, Mailpit receives every outgoing message, and the n8n Agent is paused because its tools currently read production data. Use **Settings → Load test data** to seed six synthetic example leads.

The stack can start with environment credentials alone. To enable encrypted credential editing, provision the vault using the [application setup instructions](Apps/Questionnaire-and-Booking/README.md#credential-settings). Without a valid master key, Settings displays **Vault locked** and Save/Clear are unavailable.

Open:

| App | URL |
|---|---|
| Booking | <http://127.0.0.1:3000> |
| Questionnaire | <http://127.0.0.1:3001> |
| Lead Intelligence dashboard | <http://127.0.0.1:3002> |
| Credential settings | <http://127.0.0.1:3002/#/settings> |
| Function Logic health | <http://127.0.0.1:8001/healthz> |
| Simulation-only mock inbox | <http://127.0.0.1:8025> |

To stop and resume without losing data, from `Apps/Questionnaire-and-Booking/`:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml stop
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml start
```

Do not use `down --volumes` for routine restarts; it deletes local database records and mock email.

## Automatic estate-server releases

Successful **Application CI** for trusted pushes to `main` automatically deploys
the exact tested commit to the estate server. Dashboard HTML, CSS, JavaScript and
assets are watched along with the entire frontend and backend runtime/build files.
Only affected application services are rebuilt; documentation-only releases do not
restart services. PRs, forks, failed CI and manual CI runs cannot trigger deployment.

See [sandbox operation and the one-time estate-server infrastructure rollout](Apps/Questionnaire-and-Booking/backend/docker/SANDBOX.md), then [deployment operations and host prerequisites](Apps/Questionnaire-and-Booking/backend/docker/DEPLOYMENT.md)
for runtime env precedence, shared locking, retained private backups, read-only health
checks, rollback and schema migration requirements. Vercel projects and the n8n Agent
are deployed separately; this workflow does not update them.

## Layout

```text
Apps/Questionnaire-and-Booking/
├── backend/       # Function Logic API, IMAP/SMTP worker and isolated data stores
├── frontend/      # Booking, questionnaire and Lead Intelligence dashboard
├── Tools/         # Fixture tools and simulation/E2E helpers
└── README.md      # Application setup and operation
```

See [Questionnaire and Booking](Apps/Questionnaire-and-Booking/README.md) for application behavior, mode configuration and test commands.

## Lead Intelligence dashboard

The dashboard brings together live leads, questionnaire answers, property associations and viewing stages. Use **Inbox** for triage, **Views** for saved segments and **My leads** for browser-local starred contacts. Lead drawers explain scores and flags and offer property assignment and invitation resending.

![Lead Intelligence Overview with live synthetic data](Apps/Questionnaire-and-Booking/Tools/screenshots/dashboard/overview.png)

See the [dashboard walkthrough and screenshot gallery](Apps/Questionnaire-and-Booking/frontend/dashboard/README.md#screenshot-walkthrough) for the eight hash-route pages, lead/action panels and credential settings. The existing screenshot set covers the six lead-management pages; Agent and Settings have written walkthroughs.

## Credential settings

**Settings** in the lower sidebar manages Acquaint, IMAP, SMTP and Security credentials through the same authenticated backend proxy as the dashboard. Each field reports whether it is configured and whether it comes from the settings database or environment; expandable Details show the exact key and latest database update.

- **Save** encrypts an override in the dedicated `pg-settings` database. Non-secret values remain visible; secrets are write-only and represented by status and a mask.
- **Clear** removes the override and restores the startup environment value, or leaves the field unset. It does not edit `backend/.env`.
- **Test connection** combines unsaved edits with current values without persisting them. Acquaint authenticates and reads one page, IMAP logs in and selects a mailbox read-only, and SMTP checks HELO without sending mail.
- Runtime credential consumers read the store dynamically. API writes invalidate their local cache immediately; other Python processes refresh cached overrides after a default five-second TTL and use them on the next operation or worker poll.
- Token-key changes affect applicant links. Webhook-key rotation preserves access through the bootstrap credential until the proxy and backend are recreated with the replacement environment value.

The master key remains in ignored `backend/.env` and the Python service environments. The frontend receives only its webhook bootstrap credential. The settings database stores encrypted values for both secret and non-secret fields and persists independently of container recreation.

For first-time provisioning and day-to-day use, see [application credential settings](Apps/Questionnaire-and-Booking/README.md#credential-settings). For the complete key reference, encryption model, rotation and recovery procedures, see [backend dynamic credential settings](Apps/Questionnaire-and-Booking/backend/README.md#dynamic-credential-settings).

## Documentation map

Start with the [application guide](Apps/Questionnaire-and-Booking/README.md), then follow the section you need:

| Guide | What to read it for |
|---|---|
| [Docker operations](Apps/Questionnaire-and-Booking/backend/docker/README.md) | Start/recreate services, ports and persistent volumes |
| [Sandbox operation and rollout](Apps/Questionnaire-and-Booking/backend/docker/SANDBOX.md) | Data isolation, test-data loading, shared read-only CRM access and first production provisioning |
| [Backend](Apps/Questionnaire-and-Booking/backend/README.md) | Credential reference, dynamic reads, encryption, key rotation, recovery and backend checks |
| [Function Logic package](Apps/Questionnaire-and-Booking/backend/app/README.md) | Routes, settings API contract, adapters, audit events and data-access organization |
| [Frontend](Apps/Questionnaire-and-Booking/frontend/README.md) | Web applications, mode-aware proxy configuration, settings routes and secret handling |
| [Lead Intelligence dashboard](Apps/Questionnaire-and-Booking/frontend/dashboard/README.md) | Screenshots, triage, scores, saved views, Agent, settings workflow and API mapping |
| [Booking](Apps/Questionnaire-and-Booking/frontend/booking/README.md) | Viewing requests and calendar behaviour |
| [Questionnaire](Apps/Questionnaire-and-Booking/frontend/questionnaire/README.md) | Responses and the submission screen |
| [Tools](Apps/Questionnaire-and-Booking/Tools/README.md) | Vault provisioning, fixtures, event viewer, settings checks and test-tool status |

Contact-bound questionnaire and viewing-request links use the persisted journey in both modes, including mode-selected SMTP and dashboard tracking. Acquaint remains read-only, appointments need staff confirmation, and legacy guest forms still have separate persistence limits. See [current implementation limits](Apps/Questionnaire-and-Booking/README.md#current-implementation-limits) before treating the local Compose setup as a completed production deployment.
