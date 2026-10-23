# Application Compose stack

[Application guide](../../README.md) → [Backend](../README.md) → **Docker operations**

`docker-compose.yml` runs one application codebase with separate production and sandbox
storage. `SANDBOX=TRUE` selects isolated sandbox databases and Mailpit while retaining
live read-only Acquaint properties/appointments. See [sandbox operation and first
estate-server rollout](SANDBOX.md).

| Service | Host address | Purpose |
|---|---|---|
| `web-frontend` | 3000, 3001, 3002 | Booking, questionnaire and contact dashboard |
| `app-function-logic` | 8001 | FastAPI application |
| `imap-intake` | internal | Inbound IMAP and outbound SMTP worker; simulation mode uses the mock inbox |
| `mock-inbox` | 8025 | Sandbox-only Mailpit inbox / mock SMTP |
| `pg-contacts` | internal | One-table Contacts database |
| `pg-enquiries` | internal | One-table Enquiries database |
| `pg-responses` | internal | One-table Questionnaire Responses database |
| `pg-tokens` | internal | One-table Tokens database |
| `pg-settings` | internal | Fernet-encrypted credential overrides |
| `pg-sandbox` | internal | Five isolated sandbox databases, separate account/network/volume |

All commands in this guide run from `Apps/Questionnaire-and-Booking/`. Create `backend/.env` from the template first; see [application startup](../../README.md#start-locally-with-sandboxtrue).

## Start and inspect the stack

Start the stack:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up --build -d
```

Check health and service logs:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml ps
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml logs --tail 100 app-function-logic imap-intake pg-settings mock-inbox
```

Function Logic and the worker wait for all six Postgres service health checks before starting. The frontend depends on Function Logic; the worker also depends on the mock inbox. Compose checks Postgres readiness, not whether external provider credentials authenticate successfully. Use Settings connection tests for that separate check.

## Environment loading

`backend/.env` is required: it selects the integration mode and holds bootstrap/provider connection details, the master key and any explicit settings TTL. There are two distinct uses of environment files:

| Mechanism | Consumer | Purpose |
|---|---|---|
| Service `env_file` in Compose | `app-function-logic`, `imap-intake` | Load the full backend startup environment into both Python services |
| CLI `--env-file backend/.env` | Compose interpolation | Resolve `${...}` values, including the frontend webhook bootstrap credential and any host-port/Postgres overrides |
| Service `environment` in Compose | Each affected container | Supply explicit service variables, including internal DB DSNs; these override same-name values from service `env_file` |

The frontend receives only `APP_FUNCTION_LOGIC_URL`, `APP_LISTEN_HOST` and the interpolated `TVE_WEBHOOK_SECRET`. It does not receive `TVE_SETTINGS_MASTER_KEY`, provider passwords or the rest of `backend/.env`. Postgres services receive their own database/user/password configuration, not the vault key.

The optional app-root `.env` is not automatically merged when an explicit `--env-file` is supplied. If that file exists and holds desired host-port/Postgres overrides, load both explicitly:

```powershell
docker compose --env-file .env --env-file backend/.env -f backend/docker/docker-compose.yml up --build -d
```

For duplicate interpolation variables, later `--env-file` files take precedence; shell environment values can override interpolation-file values. Keep the same file selection for subsequent recreation commands. Existing Postgres volumes retain their initialized accounts: changing the interpolation password alone does not change the database password inside those volumes.

The `web-frontend` host ports can be overridden independently with `BOOKING_HOST_PORT`, `QUESTIONNAIRE_HOST_PORT` and `DASHBOARD_HOST_PORT`. Their defaults remain `3000`, `3001` and `3002`; container ports and loopback-only binding do not change. For example, to avoid a server application already using host port 3000 while keeping the dashboard at host port 3002, run from the application directory:

```powershell
$env:BOOKING_HOST_PORT = "13000"
$env:QUESTIONNAIRE_HOST_PORT = "13001"
$env:DASHBOARD_HOST_PORT = "3002"
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up -d web-frontend
```

Unset the three shell variables (or open a fresh shell) to return to the local defaults. You may also put them in the Compose interpolation environment file used with `--env-file`.

With `sandbox=TRUE`, the worker bypasses real IMAP intake and sends through Mailpit,
using only sandbox contacts/tokens/settings and the sandbox sender. Complete Acquaint
credentials enable read-only appointment and property-catalogue reads. With
`sandbox=FALSE`, the worker uses production stores and real IMAP/SMTP automatically.
Explicit Settings tests still probe their current/candidate endpoints in either mode.

## Credential-vault service

`pg-settings` follows the one-database-per-concern model: Postgres 15 Alpine, its own volume, a `pg_isready` health check and the `net-settings` network. It publishes no host port. The backend and worker join `net-settings` and receive `TVE_DB_SETTINGS`; the frontend reaches settings only through Function Logic, not through a direct database connection.

On an empty settings volume, `init-settings/001-settings.sql` creates the `settings` table and group index. PostgreSQL entrypoint initialization scripts run on initial database creation, not on every restart/rebuild. Recreating `pg-settings` with its existing volume preserves all rows.

Provision the master key before enabling editing:

```powershell
python -m pip install -r backend/requirements.txt
python Tools/provision-settings.py
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up -d --force-recreate app-function-logic imap-intake
```

The helper modifies the ignored backend environment file, preserves valid keys and sets the settings TTL to five seconds. If it adds a webhook bootstrap value or that value was changed, include `web-frontend` in the recreation command to align the proxy. The complete stack can start in environment-only mode without an unlocked vault; Save/Clear then return 503. See [backend vault configuration](../README.md#provisioning-and-vault-configuration).

## Persistent data

The five production Postgres services have separate named volumes and networks and
publish no host ports. `pg-sandbox` has its own account, internal network and volume;
its five databases are selected only in Sandbox. This Compose file always starts
`mock-inbox`; its automatic mail routing is used only in Sandbox.

| Persistent volume | Contents |
|---|---|
| `tve-pg-contacts-data` | Contacts and viewing fields |
| `tve-pg-enquiries-data` | Enquiry bodies and parsed fields |
| `tve-pg-responses-data` | Questionnaire responses |
| `tve-pg-tokens-data` | Contact-bound links |
| `tve-pg-settings-data` | Encrypted credential overrides (requires the original master key) |
| `tve-pg-sandbox-data` | Isolated test contacts, enquiries, responses, tokens and settings |
| `tve-mock-inbox-data` | Captured mock email |
| `tve-imap-checkpoint-data` | Private production IMAP cursors and bounded hashed message dedupe; worker-only |

Settings rows store ciphertext for visible and masked fields alike. A volume/dump backup preserves ciphertext and update metadata, but cannot decrypt values without the matching `TVE_SETTINGS_MASTER_KEY`. Keep that key and the intended bootstrap configuration with the restore procedure. The master key is not part of the database contents, and generating a new key is not a migration of existing rows.

Container recreation preserves named volumes; deleting volumes deletes their data. The bounded operational event feed lives in Function Logic memory, so it is cleared on recreation and is not restored from the settings volume. Production IMAP checkpoints survive recreation in the new worker-only volume; existing database schemas/volumes are independent of it.

The checkpoint volume mounts read-write at `/var/lib/tve-imap` despite the worker's `read_only: true` root filesystem; `/tmp` remains tmpfs and is not replay storage. The Dockerfile currently has no `USER` override (UID 0), and provisions the mount directory as `0700`, owned by `0:0`; an empty named volume inherits those permissions. If the worker UID is changed later, align this directory/volume ownership before starting it. State/lock files are `0600` on Linux. Compose fixes `TVE_IMAP_STATE_DIR` to the mounted path rather than accepting an environment-only change to transient storage.

On the first production connection for each hashed mailbox identity, or a UIDVALIDITY change, the worker saves `UIDNEXT-1` and does not fetch historic mail. New arrivals are processed in UID order with per-completed-message atomic checkpoints. Up to 128 identity checkpoints are retained without eviction, alongside the latest 8,192 hashed dedupe keys. A corrupt/unwritable state file pauses production intake and follow-up delivery with sanitized error-type logging; it never falls back to a historical replay.

Back up this volume with the worker stopped, and restore its private ownership/modes along with the corresponding intake database backups. For corruption, stop the worker and restore a valid checkpoint backup before resuming. Deleting/replacing state with an empty volume deliberately baselines at the next connection and skips mail already present, including unprocessed arrivals; it is not a replay/retry recovery procedure. Keep this volume through release recreation. `sandbox=TRUE` neither reads nor writes production checkpoints.

## Applying changes

On the estate server, successful trusted `main`-push **Application CI** now triggers
an exact-tested-SHA deployment of the affected application services, including all
dashboard UI/assets. It does not recreate Postgres, Mailpit or checkpoint volumes.
Docs-only and deployment-script changes are smoke-checked without restarts. Use
[automatic deployment operations](DEPLOYMENT.md) for server prerequisites, production
env precedence (`backend/.env` first, app `.env` last), host locking, backups/rollback
and explicit database migrations. The commands below remain manual/local operations;
server maintenance must acquire the documented shared host lock. Vercel and n8n Agent
deployments remain separate.

| Change | Required action |
|---|---|
| Credential override saved/cleared in Settings | No routine restart; consumers use local invalidation/default five-second TTL and their next operation/poll |
| Bootstrap provider value edited in `.env` | Recreate the relevant Python service(s); an existing database override still takes precedence |
| Master key, TTL or sandbox mode edited in `.env` | Recreate Function Logic and the worker |
| Webhook bootstrap secret changed | Recreate Function Logic and the frontend with matching interpolation values |
| Python/frontend source changed | Rebuild and recreate the affected service(s) |
| Host port or DB DSN/interpolation configuration changed | Recreate affected services; DB account changes also need an explicit database migration |

Stop and resume the same containers without changing configuration:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml stop
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml start
```

Reload edited environment values:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up -d --force-recreate
```

Existing named volumes survive recreation. `down --volumes` deletes local records and mock email. The local webhook-auth defaults and port bindings are development settings; see [current implementation limits](../../README.md#current-implementation-limits) before production deployment.

Rebuild the rewire's three application services after code changes:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up --build -d app-function-logic imap-intake web-frontend
```

### Complete webhook rotation

After saving the replacement in Settings, set `TVE_WEBHOOK_SECRET` to the same value in `backend/.env`, then recreate the proxy and backend:

```powershell
docker compose --env-file backend/.env -f backend/docker/docker-compose.yml up -d --force-recreate app-function-logic web-frontend
```

The proxy reads the value only at startup. The backend keeps accepting its bootstrap credential alongside the vault override until recreation; switching only the proxy does not retire the old bootstrap secret. A plain `restart` keeps the old container environment. See [backend webhook rotation](../README.md#webhook-authentication-rotation) for Save/Clear behaviour.

## Settings troubleshooting

| Symptom | What to inspect |
|---|---|
| Settings reports **Vault locked** | Master-key assignment in the intended `backend/.env` and whether both Python containers were recreated after provisioning |
| Vault is unlocked but a stored value is unreadable | Whether the master key matches the one used for those rows; syntax validity does not imply successful decryption |
| Settings returns 503/unavailable | `pg-settings` health/logs and the Python services' settings DSN/network wiring |
| Settings access is denied | Matching frontend/backend webhook bootstrap values and the CLI interpolation file used at creation |
| An edited `.env` credential appears unchanged | An existing database override may still be active, or the consumer container was only restarted rather than recreated |
| A worker has not used a new override yet | Configured TTL, production poll interval and whether the operation began before the update |
| SMTP test passes but delivery fails | HELO does not test authentication, STARTTLS, sender acceptance or message delivery |

After correcting configuration or availability, use **Refresh settings** to reload the page's vault state. Recovery and matching-key backups are documented in [backend recovery](../README.md#locked-vault-recovery-and-backups).

For the first-run environment setup, return to [Start locally](../../README.md#start-locally-with-sandboxtrue). For routes and adapters, continue to [Function Logic](../app/README.md); for web-server settings, see [Frontend](../../frontend/README.md).
