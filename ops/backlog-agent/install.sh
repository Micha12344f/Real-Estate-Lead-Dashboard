#!/usr/bin/env bash
# Install / update the read-only backlog (container log) agent on the TVE server.
#
# Run by the self-hosted GitHub Actions runner as the `zubair` user. Idempotent:
# re-running updates the code and recreates containers without touching the API
# key. The API key is generated once and written to .env (mode 600); it is never
# printed to the workflow log. Retrieve it with the operator key-fetch helper.
#
# Usage: install.sh [install|status|uninstall]
set -euo pipefail

ACTION="${1:-install}"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${BACKLOG_HOME:-$HOME/apps/backlog-agent}"
PORT="${BACKLOG_PORT:-9801}"
COMPOSE="docker compose"

log() { printf '%s\n' "$*"; }

case "$ACTION" in
  install)
    mkdir -p "$DEST/api"
    # Sync the deployable files (code only; .env is preserved).
    cp "$SRC_DIR/docker-compose.yml" "$DEST/docker-compose.yml"
    cp "$SRC_DIR/api/app.py" "$DEST/api/app.py"
    cp "$SRC_DIR/api/Dockerfile" "$DEST/api/Dockerfile"
    cp "$SRC_DIR/api/requirements.txt" "$DEST/api/requirements.txt"

    if [ ! -f "$DEST/.env" ] || ! grep -q '^BACKLOG_API_KEY=' "$DEST/.env" 2>/dev/null; then
      # Generate a key without echoing it. python3 is present on the host.
      KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
      umask 077
      printf 'BACKLOG_API_KEY=%s\nBACKLOG_PORT=%s\n' "$KEY" "$PORT" > "$DEST/.env"
      chmod 600 "$DEST/.env"
      log "Generated new API key at $DEST/.env (value not shown)."
    else
      log "Existing API key preserved at $DEST/.env."
    fi
    # Ensure the port line is current without altering the key.
    if ! grep -q "^BACKLOG_PORT=" "$DEST/.env"; then
      printf 'BACKLOG_PORT=%s\n' "$PORT" >> "$DEST/.env"
    fi

    cd "$DEST"
    $COMPOSE --env-file .env up -d --build --remove-orphans
    log "Containers:"
    $COMPOSE --env-file .env ps

    # Local health probe (loopback only).
    key="$(grep '^BACKLOG_API_KEY=' .env | cut -d= -f2-)"
    for i in $(seq 1 20); do
      code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/healthz || true)"
      [ "$code" = "200" ] && break
      sleep 1
    done
    log "healthz -> ${code:-none}"
    [ "$code" = "200" ] || { log "healthcheck failed"; exit 1; }

    auth="$(curl -s -o /dev/null -w '%{http_code}' -H "X-API-Key: $key" http://127.0.0.1:${PORT}/containers || true)"
    log "authenticated /containers -> ${auth:-none}"
    [ "$auth" = "200" ] || { log "authenticated check failed"; exit 1; }
    ;;

  status)
    cd "$DEST"
    $COMPOSE --env-file .env ps
    code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${PORT}/healthz || true)"
    log "healthz -> ${code:-none}"
    ;;

  uninstall)
    cd "$DEST" 2>/dev/null || { log "not installed"; exit 0; }
    $COMPOSE --env-file .env down --remove-orphans || true
    log "stopped and removed containers (kept $DEST/.env)."
    ;;

  *)
    log "unknown action: $ACTION"; exit 2;;
esac
