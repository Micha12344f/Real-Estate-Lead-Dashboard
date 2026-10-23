"""Windows-only, persistent synthetic recovery stack; never invokes Docker/WSL.

Run: python Tools/native_recovery.py provision|start|status|stop|e2e
Only this stack's recorded processes may be stopped. No live env is imported.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile

APP = Path(__file__).resolve().parents[1]
ROOT = Path(os.environ.get("LOCALAPPDATA", str(APP / ".local-run"))) / "ThamesValleyRecovery"
BIN = ROOT / "runtime"
DATA = ROOT / "questionnaire-booking"
LOG = APP / ".local-run" / "native"
STATE = DATA / "processes.json"
PG = BIN / "pgsql" / "bin"
DOMAINS = ("contacts", "enquiries", "responses", "tokens", "settings")
PORT = 55432
FLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def setup_paths():
    if os.name != "nt":
        raise RuntimeError("This recovery helper requires Windows.")
    for path in (ROOT, BIN, DATA, LOG):
        path.mkdir(parents=True, exist_ok=True)


def install():
    setup_paths()
    packages = [
        ("postgresql.zip", "https://sbp.enterprisedb.com/getfile.jsp?fileid=1260630",
         PG / "postgres.exe", None),
        ("mailpit.zip", "https://github.com/axllent/mailpit/releases/download/v1.27.0/mailpit-windows-amd64.zip",
         BIN / "mailpit.exe", "48f605b63d9a37dedbaecac03f7b138c47495c41ce52b5e2d90835b4dd3d99b8"),
    ]
    for name, url, executable, digest in packages:
        if executable.exists():
            continue
        archive = BIN / name
        if not archive.exists():
            print(f"Downloading official {name} ...", flush=True)
            temp = archive.with_suffix(".part")
            req = urllib.request.Request(url, headers={"User-Agent": "TVE-native-recovery/1"})
            with urllib.request.urlopen(req, timeout=120) as response, temp.open("wb") as out:
                shutil.copyfileobj(response, out)
            temp.replace(archive)
        if digest and hashlib.sha256(archive.read_bytes()).hexdigest() != digest:
            raise RuntimeError("Mailpit official release checksum mismatch.")
        with zipfile.ZipFile(archive) as zipped:
            for entry in zipped.infolist():
                parts = Path(entry.filename).parts
                if ".." in parts or Path(entry.filename).is_absolute():
                    raise RuntimeError("Unsafe archive path.")
                if name == "postgresql.zip" and not entry.filename.startswith(("pgsql/bin/", "pgsql/lib/", "pgsql/share/")):
                    continue
                zipped.extract(entry, BIN)
        print(f"Installed {executable.name}", flush=True)


def credentials():
    path = DATA / "credentials.json"
    if not path.exists():
        from cryptography.fernet import Fernet
        path.write_text(json.dumps({"password": secrets.token_urlsafe(32),
                                    "webhook": secrets.token_urlsafe(32),
                                    "token": secrets.token_urlsafe(32),
                                    "master": Fernet.generate_key().decode()}), encoding="utf-8")
        # Keep runtime keys out of shared/public locations and restrict the file to this user.
        user = subprocess.check_output(["whoami"], text=True).strip()
        subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:(F)"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return json.loads(path.read_text(encoding="utf-8"))


def environment():
    # Remove inherited provider credentials/DSNs too, not just backend/.env.
    prefixes = ("TVE_", "ACQUAINT_", "N8N_", "SMTP_", "IMAP_", "POSTGRES_", "MP_",
                "SANDBOX_", "APP_FUNCTION_", "QUESTIONNAIRE_", "BOOKING_", "SANDBOX", "DATABASE_URL")
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(prefixes)}
    keys = credentials()
    env.update({"sandbox": "TRUE", "TVE_LOCAL_RECOVERY": "true", "TVE_AUTH_DISABLED": "false",
                "TVE_WEBHOOK_SECRET": keys["webhook"], "TVE_TOKEN_KEY": keys["token"],
                "TVE_SANDBOX_TOKEN_KEY": keys["token"], "TVE_SETTINGS_MASTER_KEY": keys["master"],
                "TVE_NOTIFICATION_MODE": "local", "SANDBOX_FIXTURES_DIR": str(APP / "Tools" / "fixtures"),
                "TVE_MOCK_INBOX_API": "http://127.0.0.1:8025", "TVE_MOCK_INBOX_SMTP_HOST": "127.0.0.1",
                "TVE_MOCK_INBOX_SMTP_PORT": "11025", "PYTHONUNBUFFERED": "1",
                "APP_FUNCTION_LOGIC_URL": "http://127.0.0.1:8001", "APP_LISTEN_HOST": "127.0.0.1",
                "QUESTIONNAIRE_APP_BASE_URL": "http://127.0.0.1:3001/questionnaire",
                "BOOKING_APP_BASE_URL": "http://127.0.0.1:3000/booking"})
    for domain in DOMAINS:
        env[f"TVE_SANDBOX_DB_{domain.upper()}"] = f"postgresql://tve_native:{keys['password']}@127.0.0.1:{PORT}/sandbox_{domain}"
    # A local control vault is necessary for mode/auth metadata. It is NOT a production journey store.
    env["TVE_DB_SETTINGS"] = f"postgresql://tve_native:{keys['password']}@127.0.0.1:{PORT}/recovery_control"
    return env


def run(args, **kwargs):
    result = subprocess.run([str(a) for a in args], capture_output=True, text=True,
                            creationflags=FLAGS, timeout=90, **kwargs)
    if result.returncode:
        # Never print args or child stderr: either can include runtime credentials.
        (LOG / "provision-error.log").write_text(result.stderr, encoding="utf-8")
        raise RuntimeError(f"{Path(args[0]).name} failed (see local provision-error.log).")
    return result


def listening(port):
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait(port, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if listening(port):
            return
        time.sleep(0.3)
    raise RuntimeError(f"Local port {port} did not open; inspect {LOG}.")


def ensure_postgres():
    cluster = DATA / "postgres-data"
    keys = credentials()
    if not (cluster / "PG_VERSION").exists():
        if cluster.exists() and any(cluster.iterdir()):
            raise RuntimeError("Refusing to initialize a nonempty PostgreSQL data directory.")
        pwfile = DATA / "init-password.tmp"
        pwfile.write_text(keys["password"], encoding="utf-8")
        try:
            run([PG / "initdb.exe", "-D", cluster, "-U", "tve_native", "--pwfile", pwfile,
                 "--auth=scram-sha-256", "--encoding=UTF8", "--locale=C"])
        finally:
            pwfile.unlink(missing_ok=True)
    if not listening(PORT):
        run([PG / "pg_ctl.exe", "-D", cluster, "-l", LOG / "postgres.log", "-o",
             f"-h 127.0.0.1 -p {PORT}", "-w", "start"])
    import psycopg2
    from psycopg2 import sql
    connection = psycopg2.connect(host="127.0.0.1", port=PORT, user="tve_native",
                                  password=keys["password"], dbname="postgres", connect_timeout=3)
    connection.autocommit = True
    with connection.cursor() as cursor:
        cursor.execute("SHOW data_directory")
        if Path(cursor.fetchone()[0]).resolve() != cluster.resolve():
            raise RuntimeError("Port is owned by another PostgreSQL cluster; refusing to use it.")
        for domain in (*DOMAINS, "control"):
            db = "recovery_control" if domain == "control" else f"sandbox_{domain}"
            cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (db,))
            if cursor.fetchone():
                continue  # Existing records and schema are never reset or overwritten.
            cursor.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
            with psycopg2.connect(host="127.0.0.1", port=PORT, user="tve_native", password=keys["password"], dbname=db) as target:
                with target.cursor() as schema:
                    source = "settings" if domain == "control" else domain
                    for path in sorted((APP / "backend" / "docker" / f"init-{source}").glob("*.sql")):
                        schema.execute(path.read_text(encoding="utf-8"))
                    if domain == "responses":
                        schema.execute((APP / "backend/docker/init-sandbox/002-submissions.sql").read_text(encoding="utf-8"))
    connection.close()
    print(f"PostgreSQL ready on 127.0.0.1:{PORT}; persistent synthetic stores only.", flush=True)


def load_state():
    return json.loads(STATE.read_text(encoding="utf-8")) if STATE.exists() else {"app": str(APP), "processes": {}}


def save_state(state):
    temp = STATE.with_suffix(".tmp")
    temp.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temp.replace(STATE)


def process_info(pid):
    # Narrow query, no environment dump. Caller verifies identity before stopping.
    script = f"Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' | Select-Object ProcessId,CreationDate,CommandLine | ConvertTo-Json -Compress"
    result = subprocess.run(["pwsh", "-NoProfile", "-Command", script], capture_output=True, text=True, creationflags=FLAGS)
    return json.loads(result.stdout) if result.stdout.strip() else None


def owned(record):
    info = process_info(record["pid"])
    return bool(info and info["CreationDate"] == record["created"] and info["CommandLine"] == record["command"])


def launch(name, args, cwd, env, state):
    old = state["processes"].get(name)
    if old and owned(old):
        return
    with (LOG / f"{name}.out.log").open("ab") as out, (LOG / f"{name}.err.log").open("ab") as err:
        process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=out, stderr=err,
                                   creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    time.sleep(0.4)
    info = process_info(process.pid)
    if not info:
        raise RuntimeError(f"{name} exited; inspect {LOG}.")
    state["processes"][name] = {"pid": process.pid, "created": info["CreationDate"], "command": info["CommandLine"]}
    save_state(state)
    print(f"Started {name}: PID {process.pid}", flush=True)


def api(path, payload=None, method=None):
    req = urllib.request.Request("http://127.0.0.1:3002/sandbox/api/" + path,
                                 data=json.dumps(payload).encode() if payload is not None else None,
                                 method=method, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as response:
        return json.load(response)


def start():
    install()
    ensure_postgres()
    env = environment()
    state = load_state()
    if state["app"] != str(APP):
        raise RuntimeError("Recovery process state belongs to a different checkout.")
    services = [
        ("mailpit", [BIN / "mailpit.exe", "--listen", "127.0.0.1:8025", "--smtp", "127.0.0.1:11025",
                     "--database", DATA / "mailpit.db", "--disable-version-check"], APP, (8025, 11025)),
        ("backend", [sys.executable, "-B", "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8001"], APP / "backend", (8001,)),
        ("frontend", [shutil.which("node") or "node", APP / "frontend/serve-local.js"], APP / "frontend", (3000, 3001, 3002)),
        ("intake", [sys.executable, "-B", "-m", "app.workers.imap_intake"], APP / "backend", ()),
    ]
    for name, args, cwd, ports in services:
        record = state["processes"].get(name)
        if any(listening(port) for port in ports) and not (record and owned(record)):
            raise RuntimeError(f"{name} port already occupied by an unowned process; no process was killed.")
        launch(name, args, cwd, env, state)
        for port in ports:
            wait(port)
    # Seed only once, using the application's additive/idempotent synthetic-data service.
    marker = DATA / "seeded.json"
    if not marker.exists():
        result = api("settings/sandbox/seed", {})
        marker.write_text(json.dumps(result), encoding="utf-8")
        print(f"Existing fixture seeder added {result['added']} synthetic leads.")
    status()


def status():
    result = {"dashboard": "http://127.0.0.1:3002", "storage": str(DATA), "logs": str(LOG),
              "ports": {str(p): listening(p) for p in (PORT, 8001, 3000, 3001, 3002, 8025, 11025)}}
    try:
        result["mode"] = api("settings/mode")
        leads = api("leads")
        result["lead_count"] = len(leads.get("leads", []))
    except Exception as error:
        result["api_error_type"] = type(error).__name__
    result["processes"] = {name: {"pid": row["pid"], "running": owned(row)} for name, row in load_state()["processes"].items()}
    print(json.dumps(result, indent=2))
    if not all(result["ports"].values()) or "api_error_type" in result:
        raise RuntimeError("Recovery health check failed.")


def stop():
    state = load_state()
    for name in ("intake", "frontend", "backend", "mailpit"):
        record = state["processes"].get(name)
        if record and owned(record):
            run(["taskkill", "/PID", str(record["pid"]), "/F"])
            print(f"Stopped owned {name}: PID {record['pid']}")
    if (DATA / "postgres-data/postmaster.pid").exists():
        # pg_ctl uses this cluster's PID file, not a port-based kill.
        run([PG / "pg_ctl.exe", "-D", DATA / "postgres-data", "-m", "fast", "-w", "stop"])


def e2e():
    # Existing end-to-end script is run with the same scrubbed, sandbox-only environment.
    result = subprocess.run([sys.executable, APP / "Tools/run_sandbox_e2e.py"], cwd=APP,
                            env=environment(), timeout=150)
    if result.returncode:
        raise RuntimeError("Synthetic end-to-end journey failed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("provision", "start", "status", "stop", "e2e"))
    action = parser.parse_args().action
    try:
        if action == "provision":
            install()
            ensure_postgres()
        else:
            {"start": start, "status": status, "stop": stop, "e2e": e2e}[action]()
    except Exception as error:
        print(f"Recovery failed: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
