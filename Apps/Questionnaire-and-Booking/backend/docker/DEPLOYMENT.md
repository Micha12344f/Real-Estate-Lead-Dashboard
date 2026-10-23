# Automatic estate-server deployment

[Docker operations](README.md) · [Application guide](../../README.md)

## Trigger and trust boundary

`.github/workflows/deploy-tve.yml` runs **after Application CI completes successfully**
for a **push to `main` in this repository**. A GitHub-hosted job checks current main
through the read-only GitHub API before scheduling `tve-estate-server`, whose labels
are `self-hosted`, `linux`, `x64`, `thames-valley`. Both jobs independently gate the
CI conclusion, original event, branch and repository. PRs, forks, failed CI and
manually dispatched CI are ineligible for deployment. Re-running a successful
main-push CI run is eligible if its SHA remains current main.

The runner checks out `workflow_run.head_sha`, with full history and
`persist-credentials: false`. The host script validates the original event and
checked-out HEAD again, then reads source from that commit's **Git objects**, not
from a mutable `main` checkout, downloaded artifacts or a working-tree copy.
Permissions are only `contents: read`; the GitHub token is used for current-main
checks, never persisted in the live repository. No extra deployment secret or
SSH session is needed.

GitHub concurrency `tve-production-deployment` uses `cancel-in-progress: false`.
GitHub may supersede pending runs; the running deployment is allowed to finish.
An exclusive Linux `flock` on `/home/zubair/apps/thames-valley/deploy.lock` covers
the entire transaction, including backup/build/health/rollback. Current main is
checked again **inside the lock**, after building and immediately before rollout.
A superseded SHA exits successfully with an explicit skip, restoring any copied
source if necessary without restarting services. Main can advance after rollout
starts; a healthy release then finishes and the next eligible CI release follows.

## Host prerequisites (provision separately before enabling)

The isolated sandbox database is a one-time infrastructure rollout; follow
[SANDBOX.md](SANDBOX.md#first-estate-server-rollout) before merging/deploying that
change. The operator must start `pg-sandbox` and initialize its volume using the
new Compose definition first. Once that service, `tve-pg-sandbox-data` and its network
are already present and healthy, automatic release validation permits this one additive
service and verifies runtime configuration. The release workflow never creates/resets
the database service, volume or network.

This implementation does not provision or repair the server. Before the workflow
is merged/enabled, verify as the **existing runner user**:

- Python 3.10+, Git, Docker Engine on `/var/run/docker.sock`, and Docker Compose v2
  supporting multiple `--env-file`, JSON `config`, `--no-build` and `--pull never`.
  BuildKit/buildx must support the existing Dockerfiles. The runner needs socket
  access and write/rename permissions throughout the live tracked-source tree and
  the state directory. Private backup directories must belong to this user.
- Live repository: `/home/zubair/apps/thames-valley/repo`; application:
  `repo/Apps/Questionnaire-and-Booking`. The existing
  `/home/zubair/apps/thames-valley/deployed-commit` must contain the **full 40-character
  SHA corresponding to the running release**. That commit must be available in the
  checkout's history. There is no guessed/automatic bootstrap or fallback to HEAD.
  The Git metadata in the live repository is not moved; the marker is authoritative.
- Existing app `.env` and `backend/.env` must resolve the current running stack with
  this exact interpolation order: `--env-file backend/.env --env-file .env`.
  The backend file is first and **app-root `.env` wins duplicate interpolation keys**,
  preserving host ports/Postgres overrides. Python services still load the complete
  backend file through service `env_file`; explicit service `environment` wins there.
  The script strips inherited shell overrides. If the current stack was created
  with other precedence or shell overrides, reconcile its configuration explicitly
  before enabling automation; deployment fails closed on running-config drift.
- Keep production `sandbox=FALSE` or `SANDBOX=FALSE` explicitly set without conflicting
  aliases as the safe bootstrap default. The dashboard's encrypted
  `TVE_SANDBOX_MODE` override can switch the running API and IMAP worker between
  production and sandbox without recreating containers; the worker observes it on
  its next poll. Keep `mock-inbox` in the normal Compose service set and running so
  Sandbox mode can route mail to Mailpit immediately. The frontend/bootstrap webhook credential must match the backend, with
  auth enabled and no sandbox webhook fallback. Retain signing, master, provider and
  auth credentials in their existing files/settings database. Automation neither
  provisions nor rotates them and never copies an example onto a runtime file.
- Expected loopback bindings: API `127.0.0.1:5000`, booking `127.0.0.1:13000`,
  questionnaire `127.0.0.1:13001`, dashboard `127.0.0.1:3002`. Containers, mounts,
  resolved environment and networks must match the live Compose configuration.
  All five Postgres containers must already be healthy, all other services running.
- All seven existing volumes must exist with their intended data:
  `tve-pg-{contacts,enquiries,responses,tokens,settings}-data`,
  `tve-imap-checkpoint-data`, `tve-mock-inbox-data`. The production worker must have
  a valid `/var/lib/tve-imap/checkpoint.json` already established, and remain stable.
  Automation will not create a missing volume or initialize a missing checkpoint.
- The runner must reach `api.github.com` over HTTPS and its normal image build sources.
  Keep adequate disk space for builds, old image tags and retained private backups.
- Restrict repository/runner access to trusted maintainers and protect `main` and
  workflow changes. PR workflows use GitHub-hosted runners. A runner with Docker
  socket access must never execute untrusted PR code or PR artifacts. The existing
  `estate-runner.yml` remains a manually invoked health check, not a deployment.
- **Every manual deployment/configuration maintenance path must acquire the same
  host lock**. Disable/wrap old uncoordinated deployment jobs before enabling this
  workflow. Do not delete/replace the lock inode. For a maintenance shell, use
    `flock --exclusive /home/zubair/apps/thames-valley/deploy.lock bash` and keep all
    operations within that shell. Do not invoke this auto-deploy script inside that
    shell: it acquires its own lock and requires the validated workflow environment.

## Lead deletion schema maintenance

The lead-delete migration is a separate, fixed-purpose maintenance operation, not an
exception that makes arbitrary SQL eligible for automatic deployment. The only
permitted migration bytes are
`backend/docker/init-contacts/002-lead-delete-migration.sql` (449 bytes,
SHA-256 `aeabc20a730d2c26a13687565d877ad60da82597cb5c88ff937daa5efa4aad34`).
Both existing contacts databases (`contacts` and `sandbox_contacts`) must be inspected
and migrated before the deletion writer can be released. The four journey stores in
each environment must be backed up before any schema write; settings is not a journey
store and is not part of this migration's rollback set.

`.github/workflows/lead-delete-migration.yml` is the explicit maintenance entry point
in this proposed release. Fixed choices are `inspect`, `apply`, `resume`, `verify`, and
`reconcile`;
there are no caller-supplied SQL, shell, database, or path inputs. The workflow may be
dispatched only from `main` when both the original run actor and the current
triggering actor are repository owner `Micha12344f` (including workflow reruns), with
a full target SHA that is still current main and has a successful trusted main-push
Application CI run. The executable cross-checks the run ID, attempt, workflow path,
event, original actor and triggering actor with GitHub's authenticated Actions API.
It checks out that exact SHA, uses read-only `contents`/`actions` permissions, runs on
the existing estate runner, and shares concurrency group `tve-production-deployment`.
The executable locks the existing `/home/zubair/apps/thames-valley/deploy.lock` for
all operations. It needs no SSH credential or tunnel and never touches cloudflared or
sshd.

The `inspect` action is read-only and reports only mode, schema flags and opaque
database identity hashes. The `reconcile` action is only for a completed migration
whose target SHA advanced without changing the fixed SQL plan, or for the one
documented `app-function-logic` `TVE_EMAIL_INTAKE` environment mismatch. It first
verifies the existing signed receipt, same deployed base marker, live contacts schema,
all eight hash-verified/stream-validated backups, runtime env hashes, mode and original
release image provenance. Its sole drift exception is that one key on that one
service; all other env keys and all service state, mounts, ports and networks retain
strict validation. A worker image mismatch is tolerated only for `imap-intake`, only
when the previous signed migration receipt's immutable image ID equals deployed
provenance; the fixed reconciliation restores that receipt-proven image. An environment
file delta is allowed only for `TVE_EMAIL_INTAKE` in `backend/.env`, plus the already
recorded removal of the retired legacy deletion override; app-root `.env`, all other
backend keys, and the receipt's pre-migration env snapshot remain hash-verified. It recreates
only the API and/or worker actually needing reconciliation, with immutable images and
declared Compose configuration, then requires the normal all-service strict check and
health check to pass. It reattests the completed migration to current main only
when SQL hashes and changed SQL path set are unchanged; it never runs SQL or edits the
deployed marker. Any other drift, stale/missing receipt, changed backup, or changed
migration plan fails closed.

### Action sequence after reviewed merge

1. Dispatch `inspect` for the full current-main SHA. It validates the deployed marker
   and provenance against actual running image IDs, Compose/runtime configuration,
   all required healthy DB services and the effective persisted mode. It fails on
   ambiguous baseline, unfinished deploy journal, missing sandbox DB or drift. It
   does not change application mode, environment, services, or schema.
2. Dispatch `apply` for that same still-current SHA after successful trusted CI. It
   checks free disk conservatively against twice the aggregate size of eight databases
   plus 1 GiB, records private copies of both runtime environment files, tags immutable
   currently running API/worker images, then gracefully stops only `app-function-logic`
   and `imap-intake`. No database or volume is recreated. It creates verified
   PostgreSQL custom-format backups for `contacts`, `enquiries`, `responses`, and
   `tokens` in both production and sandbox (8 archives total), streams every archive
   through `pg_restore --exit-on-error --no-owner --no-privileges --file=/dev/null`
   from stdin (without connecting to or modifying a database), and records hashes
   and identities in private state. Settings is
   not part of the deletion journey backup set.
3. Only after all 8 verified archives exist, run the fixed 449-byte `002` SQL in its
   own `psql -v ON_ERROR_STOP=1` transaction against `contacts` and `sandbox_contacts`
   (two distinct databases only). The tool verifies the `contacts.id` primary key,
   `contact_uid` UUID/non-null/default, and exact `lead_delete_operations` columns and
   primary key. It never applies contacts SQL to enquiries, responses, or tokens.
4. No additional deletion credential is provisioned. The DELETE route uses the
   dashboard's existing standard authenticated API path, while mode and replay
   references remain mandatory. The global persisted mode is observed, never switched.
5. Only after both contacts schemas, private backup manifest, env hashes, database
   identities and image IDs verify does the tool write a mode-0600 HMAC-authenticated
   receipt under `lead-delete-migration/receipts/<target-sha>.json`. It is tied to the
   exact base marker and target SHA, the target `001`/`002` file hashes and changed SQL
   paths, prior/current schema attestations, 8 backup hashes, rollback image IDs,
   workflow actor/run ID, runtime environment hashes and timestamp. It includes no
   credential values or customer data. A private HMAC key protects the receipt from
   undetected edits. Any partial operation leaves a durable journal and no receipt.
6. If a run is interrupted after its `backed_up` journal state, dispatch `resume` for
   the same SHA. It validates the exact stopped-writer set, all dependency runtime
   configuration, Contacts DB system identities, private environment snapshots and
   the complete exact eight-archive manifest before a restart or SQL. Each restart
   rechecks the rollback-tag-to-image-ID binding and recreates with immutable image
   IDs, then verifies the resulting container IDs before health/receipt. It then
   idempotently reapplies the exact pinned SQL to both contacts DBs and verifies
   before issuing a receipt. An unfinished journal for a different SHA fails closed.
   Do not hand-edit/remove it or manually roll back additive DDL.
7. Dispatch `verify` for the same SHA before release. Then re-run the exact successful
   main-push Application CI run (no manual deployment dispatch) so the existing
   `workflow_run` deployment workflow gets another trusted completion event. The
   deployer accepts only the exact changed `001`/`002` path set and hash plan with a
   valid receipt; under the same host lock it rechecks receipt HMAC, marker, env hashes,
   backup hashes, rollback image IDs, database identities and both live schemas.
   Missing/stale/tampered receipts, unknown SQL, or any other runtime drift still
   fails closed. The normal tested-SHA rollout/health/rollback path remains in force.

The receipt does not pre-advance `deployed-commit`; only a healthy normal release does.
Application rollback does not reverse the additive DB migration. The guarded
reconciliation removes the retired deletion-only env override; standard dashboard
authentication remains. Post-release verification probes the OpenAPI DELETE operation
and actual direct plus same-origin proxy handlers without deleting real data: with
valid standard authentication and mode but deliberately missing operation references,
both Production and Sandbox must return the handler's 422 validation response before
any DB access. A 401/403 auth middleware response or 405 method response is not a pass. No
production lead is read or deleted and no global mode switch is used. The regular
simulation CI separately tests qualifying and low-score synthetic contacts. A full
sandbox deletion against a synthetic target/neighbor is intentionally deferred to an
explicitly coordinated acceptance window; it is not silently performed by deploy.

## Changes watched and services rebuilt

Paths are compared against the **marker re-read under the host lock**, not just the
previous push. Changes accumulated across failed/skipped CI runs are included.
There is no trigger path filter, and dashboard deployment is automatic.

| Changed paths | Build and recreate |
|---|---|
| Entire `frontend/**` except Markdown documentation | `web-frontend` |
| Dashboard HTML, JS, CSS, images/fonts/Meesix assets | `web-frontend` (covered by the entire frontend rule) |
| `frontend/serve-local.js`, `Dockerfile`, `.dockerignore`, booking/questionnaire code | `web-frontend` |
| `backend/app/**` except Markdown, `backend/requirements.txt`, `backend/.dockerignore`, backend Docker files except Markdown | `app-function-logic`, `imap-intake` |
| `Tools/fixtures/**`, `Tools/fixture-manifest.json` | `app-function-logic`, `imap-intake` |
| Shared `backend/docker/docker-compose.yml` | All three application services, subject to configuration validation |
| Markdown/docs, workflows and deployment/test scripts | Copy tested tracked source and smoke-check; **no build/restart** |
| `backend/docker/init-*/**`, `backend/docker/init/**`, backend SQL or `migrations/` files | **Stop; except exact 001/002 lead-delete plan with verified target receipt** |

The optional log viewer, Vercel booking/questionnaire projects, n8n Agent workflows
and local model infrastructure are **separate deployments**. Updating their source
in this repository does not import/redeploy them. Shared frontend changes still
rebuild the estate-server frontend image; that does not publish anything to Vercel.

## Transaction and rollback

1. Check current main and marker; already-deployed SHA is an explicit successful skip.
2. Resolve old/new tracked manifests. Refuse tracked runtime environment files,
   symlinks/submodules and collisions between new tracked paths and unfamiliar live
   files. Create a retained private backup of **actual prior live source/config bytes**,
   recorded modes, marker/provenance and private environment snapshots before overlay.
3. Materialize tested Git objects in the private release tree. Resolve Compose in
   memory with copied runtime env snapshots. Validate all existing volumes, expected
   ports, live mode/auth, running env/mount/network configuration and baseline health.
   Infra, database/Mailpit configuration and application runtime env/ports/mounts/
   networks/security cannot be changed automatically. Application image/build,
   command and restart-policy changes are allowed; other configuration changes stop
   for operator review. Destructive schema work must be an explicit migration, not
   application startup side effects.
4. Tag currently running image IDs as unique rollback handles. Build selected images
   under SHA-specific tags **while existing containers are still running**, from the
   private tested source tree. Re-check main and verify runtime env bytes unchanged.
5. Overlay only tracked files and remove only paths from the previous tracked
   manifest. Unfamiliar files, `.git` and runtime env files are untouched. New empty
   directories may remain after rollback; unrelated directory contents are not pruned.
6. Recreate **only selected apps**, using the release-image override with
   `up -d --no-deps --no-build --pull never --force-recreate`. No infrastructure
   services are recreated and no `down`, orphan removal, database/volume recreation
   or `server_deploy` helper is used.
7. Perform read-only smoke checks. Only after health succeeds, atomically replace
   provenance and the deployed marker. A failure writing either release record also
   invokes rollback and restores the previous records.

Copy, rollout or post-rollout health failure restores actual prior source/config and
markers. If rollout was attempted, selected apps are recreated using their old tagged
image IDs and the restored Compose source plus rollback override, with building/pulling
disabled. Runtime env files are **not restored/rolled back**; env snapshots are private
recovery evidence and interpolation inputs, not an instruction to rotate keys. Manual
env/config changes must therefore use the same lock. Databases, Mailpit messages and
worker checkpoints are not snapshotted or automatically restored. Code rollback cannot
undo runtime data writes; releases must remain compatible with existing schemas.

TERM/INT interrupt the transaction and perform best-effort rollback; command children
are terminated before restoration. SIGKILL, runner termination or power loss cannot
be trapped. A durable unfinished transaction journal causes the next deployment to
stop for operator recovery, rather than overlaying another release on partial state.

## Health and log privacy

Smoke checks use only loopback URLs, with HTTP redirects and environment proxies
disabled: API `5000/healthz`, frontend `3002/healthz`, authenticated direct and
same-origin Settings/leads/funnel GETs, and the actual dashboard `index.html`,
`styles.css`, `app.js`, `assets/meesix-mark.svg`. Served asset bytes must exactly
match the live released source. A direct unauthenticated Settings GET must be denied.
The proxy supplies its internal bootstrap credential for same-origin requests; the
script obtains the direct API credential internally from the resolved runtime config.
Worker checks validate explicit live mode and existing checkpoint JSON read-only,
then require the worker to stay running with unchanged restart count/start time over
five seconds. Post-rollout checks retry up to twelve times with a three-minute retry
budget (a final in-flight bounded request can extend that budget).

These checks do not fetch CRM properties/appointments, call Settings connection tests,
send mail, invoke Agent/n8n, create leads or exercise production journeys. The normal
production worker resumes its configured intake independently; smoke tests do not
trigger it. Checks do not prove external provider availability.

Actions output contains bounded structured events, fixed sanitized error codes,
source SHA, selected service names, counts, and a fixed health-check stage name on
health failure. Command stderr/build output, resolved
Compose config, full container/image inspection, runtime env and HTTP response bodies
are never printed or uploaded. Do not enable shell tracing/config dumps or upload
private backup trees to Actions artifacts.

## State schema, retention and recovery

All state is under `/home/zubair/apps/thames-valley/`:

- `deployed-commit`: full successful source SHA followed by newline, atomic, mode `0600`.
- `deployed-provenance.json`: version `1`; `source_sha`, `previous_sha`, `ci_run_id`,
  selected `services`, running application `images` (IDs), selected `rollback_images`,
  UTC `released_at`, and backup-directory basename. No credentials or lead data.
- `deploy-backups/<sha-prefix>-<transaction-id>/`: retained, mode `0700`; actual prior
  tracked bytes in `previous-source/` (files `0600`), previous marker/provenance,
  `manifest.json` mapping tracked path to original mode or `null` if absent, private
  runtime env snapshots, the materialized `release/` source, release/rollback image
  overrides and `transaction.json`.
- `transaction.json`: version `1`, tested/previous SHA, selected services, prior image
  IDs, rollback tags, changed-path count and durable phase `status`. Completed phases
  are `released`, `rolled_back`, `aborted_before_overlay`. Any other phase, including
  `rollback_failed_operator_required`, blocks subsequent deployments.

Backups and old image tags are deliberately retained, not pruned by a release. Keep
them private (they contain environment snapshots), and apply an operator retention
policy after verified releases without removing an unfinished transaction or rollback
images needed by a retained backup. Maintain independent database/checkpoint backups
under the existing application recovery policy.

For an interrupted/failed rollback, acquire the host lock and use the journal to
identify the previous marker, affected services and rollback tags. Restore **only
the paths listed in `manifest.json`** from `previous-source/`, with original modes;
remove only manifest paths whose previous mode is `null`. Do not copy the whole backup
tree onto live: env snapshots are intentionally outside that manifest. Restore the
previous Compose/source first, then recreate the listed apps with the private
`rollback-images.json` override and `--no-deps --no-build --pull never`. Use the same
two-file interpolation order (the private previous env snapshots may supply
interpolation), while preserving the live backend service env file and all keys.
Re-run the read-only smoke checks, restore previous marker/provenance, and only then
record the recovered journal as `rolled_back`. Do not simply delete the journal to
bypass the block. Database/checkpoint recovery or schema/key changes are separate
explicit operator procedures; never use `down --volumes` as release recovery.

## Offline verification

From the application directory:

```powershell
python -m py_compile Tools/deploy_tve.py
python -m pytest -q Tools/test_deploy_tve.py
```

These temporary-filesystem/fake-Git/Docker/HTTP scenarios run as a separate Application
CI step. They exercise service selection, exact tested SHA trust, marker re-reading,
env preservation/precedence, stale skips at each pre-rollout boundary, copy/build/
partial-rollout/health failure, rollback images/source/markers, private metadata and
provider-free read-only smoke coverage. They make no live Docker, GitHub or server calls.
