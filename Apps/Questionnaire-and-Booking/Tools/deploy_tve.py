#!/usr/bin/env python3
"""Transactional TVE deployment. stdlib only; never invoke server_deploy.

Only main-push workflow_run input is accepted. All command output, HTTP bodies,
resolved Compose config and runtime credentials remain private/in memory.
See backend/docker/DEPLOYMENT.md for the host contract and recovery procedure.
"""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import stat
import subprocess
import sys
import time
import urllib.request
import urllib.error
from urllib.parse import unquote, urlsplit
import uuid

APP = 'Apps/Questionnaire-and-Booking'
COMPOSE = 'backend/docker/docker-compose.yml'
SERVICES = ('app-function-logic', 'imap-intake', 'web-frontend')
DATABASES = tuple('pg-' + name for name in ('contacts', 'enquiries', 'responses', 'tokens', 'settings'))
VOLUMES = {f'pg_{name}_data': f'tve-pg-{name}-data' for name in
           ('contacts', 'enquiries', 'responses', 'tokens', 'settings')}
VOLUMES.update(imap_checkpoint_data='tve-imap-checkpoint-data', mock_inbox_data='tve-mock-inbox-data')
SANDBOX_VOLUMES = {'pg_sandbox_data': 'tve-pg-sandbox-data'}
SANDBOX_DATABASES = ('contacts', 'enquiries', 'responses', 'tokens', 'settings')
SHA = re.compile(r'[0-9a-f]{40}')
LIVE = Path('/home/zubair/apps/thames-valley/repo')
STATE = LIVE.parent


class DeployError(Exception):
    """Only fixed, non-sensitive error codes may be emitted."""


def environment_mismatch_keys(expected, actual):
    """Return key names only; never include expected or observed values."""
    return sorted(key for key, value in expected.items() if actual.get(key) != str(value))


HEALTH_CHECKS = {'api_healthz','frontend_healthz','unauthenticated_settings','direct_settings',
    'proxy_settings','direct_leads','proxy_leads','direct_funnel','proxy_funnel','asset_index',
    'asset_styles','asset_app','asset_brand','worker_running','worker_checkpoint','worker_stability',
    'delete_openapi','delete_mode','delete_api_handler','delete_proxy_handler','health_probe_unknown'}


class StaleRelease(Exception):
    pass


def log(event, **fields):
    print(json.dumps({'event': event, **fields}, sort_keys=True), flush=True)


def validated_sha(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise DeployError('invalid_commit_sha')
    return value


def validate_event(event, environ):
    run = event.get('workflow_run', {})
    repo = environ.get('GITHUB_REPOSITORY', '')
    sha = validated_sha(environ.get('TESTED_SHA'))
    if (environ.get('GITHUB_EVENT_NAME') != 'workflow_run'
            or run.get('name') != 'Application CI' or run.get('conclusion') != 'success'
            or run.get('event') != 'push' or run.get('head_branch') != 'main'
            or run.get('head_sha') != sha
            or run.get('head_repository', {}).get('full_name') != repo
            or event.get('repository', {}).get('full_name') != repo
            or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)):
        raise DeployError('untrusted_ci_event')
    return sha, repo


def runtime_path(name):
    path = PurePosixPath(name)
    return path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example'


LEAD_DELETE_SCHEMA_PATHS = {
    APP + '/backend/docker/init-contacts/001-contacts.sql',
    APP + '/backend/docker/init-contacts/002-lead-delete-migration.sql',
}
LEAD_DELETE_SCHEMA_HASHES = {
    APP + '/backend/docker/init-contacts/001-contacts.sql': '2438f72881f271a9febc554ba07611104c16017b77bbf1d292795a21bed2955a',
    APP + '/backend/docker/init-contacts/002-lead-delete-migration.sql': 'aeabc20a730d2c26a13687565d877ad60da82597cb5c88ff937daa5efa4aad34',
}


class VerifiedLeadDeleteMigration:
    """Proof returned only after live schema and signed receipt validation."""
    __slots__ = ('previous_sha', 'target_sha', 'paths')

    def __init__(self, previous_sha, target_sha, paths):
        self.previous_sha = previous_sha
        self.target_sha = target_sha
        self.paths = tuple(paths)


def lead_delete_schema_candidate(paths):
    for path in paths:
        rel = path[len(APP) + 1:] if path.startswith(APP + '/') else ''
        init_path = rel.startswith(('backend/docker/init-', 'backend/docker/init/'))
        if init_path and not rel.startswith('backend/docker/init-sandbox/') and path not in LEAD_DELETE_SCHEMA_PATHS:
            raise DeployError('database_init_change_requires_explicit_migration')
    schema_paths = sorted(path for path in paths if path.startswith(APP + '/backend/')
                          and (path.endswith('.sql') or '/migrations/' in path))
    if not schema_paths:
        return False
    if (not set(schema_paths).issubset(LEAD_DELETE_SCHEMA_PATHS)
            or APP + '/backend/docker/init-contacts/002-lead-delete-migration.sql' not in schema_paths):
        raise DeployError('database_init_change_requires_explicit_migration')
    return True


def classify(paths, verified_migration=None):
    """Use the *last deployed* manifest, not the preceding push or CI paths."""
    paths = set(paths)
    schema_changed = tuple(sorted(path for path in paths if path.startswith(APP + '/backend/')
                                  and (path.endswith('.sql') or '/migrations/' in path)))
    proof_matches = (isinstance(verified_migration, VerifiedLeadDeleteMigration)
                     and verified_migration.paths == schema_changed)
    selected = set()
    for path in paths:
        if not path.startswith(APP + '/'):
            continue
        relative = path[len(APP) + 1:]
        if ((relative.startswith(('backend/docker/init-', 'backend/docker/init/'))
                 and not relative.startswith('backend/docker/init-sandbox/'))
                or (relative.startswith('backend/') and not relative.startswith('backend/docker/init-sandbox/')
                    and (relative.endswith('.sql') or '/migrations/' in relative))):
            if not (proof_matches
                    and path in LEAD_DELETE_SCHEMA_PATHS):
                raise DeployError('database_init_change_requires_explicit_migration')
        if relative.startswith('frontend/') and not relative.endswith('.md'):
            selected.add('web-frontend')  # Includes every dashboard asset/style/script.
        elif relative == COMPOSE:
            selected.update(SERVICES)
        elif (relative.startswith('backend/app/') and not relative.endswith('.md')
              or relative.startswith('backend/docker/') and not relative.endswith('.md')
              or relative in ('backend/requirements.txt', 'backend/.dockerignore')
              or relative.startswith('Tools/fixtures/') or relative == 'Tools/fixture-manifest.json'
              or relative.startswith('Tools/run_sandbox_isolation_e2e.py')):
            selected.update(SERVICES[:2])
    return [service for service in SERVICES if service in selected]


def validate_lead_delete_migration(source, state, previous_sha, target_sha, changed_paths, host, old_config):
    """Verify exact one-time migration receipt and live schema; never skip unknown SQL."""
    if not lead_delete_schema_candidate(changed_paths):
        return None
    schema_changed = sorted(path for path in changed_paths
                            if path.startswith(APP + '/backend/') and
                            (path.endswith('.sql') or '/migrations/' in path))
    if (not set(schema_changed).issubset(LEAD_DELETE_SCHEMA_PATHS)
            or APP + '/backend/docker/init-contacts/002-lead-delete-migration.sql' not in schema_changed):
        raise DeployError('database_init_change_requires_explicit_migration')
    for path, expected in LEAD_DELETE_SCHEMA_HASHES.items():
        if path not in source.manifest(target_sha):
            raise DeployError('migration_schema_source_missing')
        blob = source.manifest(target_sha)[path][0]
        data = source.git('cat-file', 'blob', blob)
        import hashlib
        if hashlib.sha256(data).hexdigest() != expected:
            raise DeployError('migration_schema_source_hash_mismatch')
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import lead_delete_maintenance as maintenance
        maintenance.STATE = state
        maintenance.LIVE = host.live
        maintenance.APP = host.live / APP
        maintenance.LOCK = state / 'deploy.lock'
        maintenance.MIGRATION_DIR = state / 'lead-delete-migration'
        maintenance.BACKUPS = maintenance.MIGRATION_DIR / 'backups'
        maintenance.RECEIPTS = maintenance.MIGRATION_DIR / 'receipts'
        maintenance.JOURNAL = maintenance.MIGRATION_DIR / 'transaction.json'
        maintenance.KEY = maintenance.MIGRATION_DIR / '.receipt-key'
        if maintenance.JOURNAL.exists():
            raise DeployError('unfinished_migration_recovery_required')
        maintenance.verify_release_baseline(previous_sha, host, SERVICES)
        rows = maintenance.schema_set(old_config)
        if len(rows) != 2 or any(not row.get('complete') for row in rows):
            raise DeployError('migration_live_schema_incomplete')
        receipt = maintenance.verify_receipt(previous_sha, target_sha, rows)
        if receipt.get('migration_paths') != schema_changed:
            raise DeployError('migration_receipt_changed_paths_mismatch')
        rollback_images = receipt.get('rollback_images', {})
        if set(rollback_images) != {'app-function-logic', 'imap-intake'}:
            raise DeployError('migration_receipt_rollback_images_missing')
        for service, saved in rollback_images.items():
            if service not in ('app-function-logic', 'imap-intake') or not isinstance(saved, dict):
                raise DeployError('migration_receipt_rollback_image_invalid')
            if (not re.fullmatch(r'sha256:[0-9a-f]{64}', str(saved.get('image_id', '')))
                    or not re.fullmatch(r'[0-9a-f]{64}', str(saved.get('container_id', '')))
                    or not re.fullmatch(r'tve-migration-rollback:[0-9a-f-]+-' + service, str(saved.get('tag', '')))):
                raise DeployError('migration_receipt_rollback_image_invalid')
            if host.inspect(service).get('Image') != saved.get('image_id'):
                raise DeployError('migration_rollback_baseline_image_drift')
            tag_id = host.docker('image', 'inspect', '--format', '{{.Id}}', saved.get('tag', ''), capture=True).decode().strip()
            if tag_id != saved.get('image_id'):
                raise DeployError('migration_rollback_image_tag_drift')
    except DeployError:
        raise
    except Exception:
        raise DeployError('migration_receipt_invalid') from None
    return VerifiedLeadDeleteMigration(previous_sha, target_sha, tuple(schema_changed))


def safe_path(root, name):
    parts = PurePosixPath(name).parts
    if not parts or name.startswith('/') or any(part in ('..', '.', '.git') for part in parts) or '\\' in name:
        raise DeployError('unsafe_tracked_path')
    result = root.joinpath(*parts)
    for parent in (result, *result.parents):
        if parent == root.parent:
            break
        if parent.is_symlink():
            raise DeployError('symlink_in_release_path')
    return result


def private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or hasattr(os, 'getuid') and path.stat().st_uid != os.getuid():
        raise DeployError('unsafe_state_directory')
    path.chmod(0o700)


def atomic_write(path, data, mode=0o600):
    temporary = path.with_name('.tve-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        if os.name != 'nt':
            os.fchmod(fd, mode)  # Restore exact source modes despite private umask.
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        if os.name != 'nt':  # Transaction fixtures also run on Windows.
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class Commands:
    def __init__(self):
        # No inherited interpolation overrides; runtime files are authoritative.
        self.env = {key: os.environ[key] for key in ('PATH', 'HOME', 'LANG') if key in os.environ}

    def run(self, args, cwd=None, capture=False, timeout=120):
        with subprocess.Popen(args, cwd=cwd, env=self.env, start_new_session=True,
                              stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) as child:
            try:
                output, _ = child.communicate(timeout=timeout)
            except BaseException:
                # Wait for the CLI to die before restoring/recreating containers.
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.wait()
                raise
            if child.returncode:
                raise DeployError('command_failed_' + args[0])
            return output or b''


class GitSource:
    def __init__(self, root, sha, commands):
        self.root, self.sha, self.commands = root, sha, commands
        if self.git('rev-parse', 'HEAD').decode().strip() != sha:
            raise DeployError('checkout_not_tested_sha')
        if self.git('status', '--porcelain', '--untracked-files=no'):
            raise DeployError('dirty_checked_out_source')

    def git(self, *args):
        return self.commands.run(['git', '-C', str(self.root), *args], capture=True)

    def manifest(self, sha):
        validated_sha(sha)
        manifest = {}
        for record in self.git('ls-tree', '-rz', sha).split(b'\0'):
            if not record:
                continue
            metadata, raw_path = record.split(b'\t', 1)
            mode, kind, blob = metadata.decode().split()
            path = raw_path.decode('utf-8')
            safe_path(self.root, path)
            if runtime_path(path):
                raise DeployError('runtime_env_must_not_be_tracked')
            if mode not in ('100644', '100755') or kind != 'blob':
                raise DeployError('unsupported_tracked_file_type')
            manifest[path] = (blob, 0o755 if mode == '100755' else 0o644)
        if not manifest:
            raise DeployError('empty_tracked_manifest')
        return manifest

    def materialize(self, manifest, destination):
        for name, (blob, mode) in manifest.items():
            target = safe_path(destination, name)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(target, self.git('cat-file', 'blob', blob), mode)


class Overlay:
    """Backup actual prior bytes, including drift; only manifest-owned paths change."""
    def __init__(self, live, release, backup, old, new):
        if any(runtime_path(name) for name in old.keys() | new.keys()):
            raise DeployError('runtime_env_must_not_be_tracked')
        self.live, self.release, self.backup = live, release, backup
        self.old, self.new = old, new
        self.before = {}

    def prepare(self):
        private_dir(self.backup)
        for name in sorted(self.old.keys() | self.new.keys()):
            target = safe_path(self.live, name)
            if target.exists():
                if not target.is_file():
                    raise DeployError('unfamiliar_live_file_collision')
                if name not in self.old:
                    # New files in the release may already exist from operator
                    # provisioning (e.g. sandbox init scripts). Accept them
                    # without a rollback backup; the overlay overwrites them.
                    self.before[name] = None
                    continue
                saved = safe_path(self.backup, name)
                saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write(saved, target.read_bytes())
                self.before[name] = stat.S_IMODE(target.stat().st_mode)
            else:
                self.before[name] = None
        atomic_write(self.backup.parent / 'manifest.json', json.dumps(self.before, sort_keys=True).encode())

    def apply(self):
        for name in sorted(self.before):
            target = safe_path(self.live, name)
            if name in self.new:
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, safe_path(self.release, name).read_bytes(), self.new[name][1])
            else:
                target.unlink(missing_ok=True)  # Only paths in the old tracked manifest.

    def restore(self):
        for name, mode in self.before.items():
            target = safe_path(self.live, name)
            if mode is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                atomic_write(target, safe_path(self.backup, name).read_bytes(), mode)


class Host:
    def __init__(self, live, commands):
        self.live, self.commands = live, commands

    def docker(self, *args, capture=False, timeout=120):
        return self.commands.run(['docker', '--host', 'unix:///var/run/docker.sock', *args],
                                 capture=capture, timeout=timeout)

    def compose(self, root, *args, override=None, env_root=None, capture=False, timeout=120):
        app = root / APP
        env_app = (env_root or root) / APP
        command = ['compose', '--project-name', 'tve-questionnaire-booking',
                   '--project-directory', str((app / COMPOSE).parent),
                   '--env-file', str(env_app / 'backend/.env'), '--env-file', str(env_app / '.env'),
                   '-f', str(app / COMPOSE)]
        if override:
            command.extend(['-f', str(override)])
        return self.docker(*command, *args, capture=capture, timeout=timeout)

    def config(self, root):
        return json.loads(self.compose(root, 'config', '--format', 'json', capture=True))

    def inspect(self, service):
        return json.loads(self.docker('container', 'inspect', 'tve-' + service, capture=True))[0]

    def check_config(self, config, root):
        base_services = set(SERVICES + DATABASES + ('mock-inbox',))
        actual_services = set(config['services'])
        if config.get('name') != 'tve-questionnaire-booking' or actual_services not in (base_services, base_services | {'pg-sandbox'}):
            raise DeployError('unexpected_compose_services')
        volumes = {**VOLUMES, **(SANDBOX_VOLUMES if 'pg-sandbox' in actual_services else {})}
        if {key: value.get('name') for key, value in config.get('volumes', {}).items()} != volumes:
            raise DeployError('unexpected_persistent_volumes')
        for name in volumes.values():
            self.docker('volume', 'inspect', name)  # Never create a missing volume.
        if 'pg-sandbox' in actual_services:
            self.check_sandbox_service(config, release=root)
            pg_env = config['services']['pg-sandbox'].get('environment', {})
            sandbox_user, sandbox_password = str(pg_env.get('POSTGRES_USER', '')), str(pg_env.get('POSTGRES_PASSWORD', ''))
            if (not sandbox_user or not sandbox_password or sandbox_password == 'sandbox-only-local-password'
                    or sandbox_user == str(config['services']['pg-contacts'].get('environment', {}).get('POSTGRES_USER', ''))
                    or sandbox_password == str(config['services']['pg-contacts'].get('environment', {}).get('POSTGRES_PASSWORD', ''))):
                raise DeployError('sandbox_database_credentials_not_isolated')
            for service in SERVICES[:2]:
                env = config['services'][service].get('environment', {})
                for name in SANDBOX_DATABASES:
                    dsn = env.get('TVE_SANDBOX_DB_' + name.upper())
                    try:
                        parsed = urlsplit(str(dsn))
                        valid = (parsed.scheme in {'postgres', 'postgresql'}
                                 and parsed.hostname == 'pg-sandbox'
                                 and unquote(parsed.path).strip('/') == 'sandbox_' + name
                                 and unquote(parsed.username or '') == sandbox_user
                                 and unquote(parsed.password or '') == sandbox_password)
                    except (ValueError, TypeError):
                        valid = False
                    if not valid:
                        raise DeployError('sandbox_database_dsn_mismatch')
        for service in SERVICES[:2]:
            env = config['services'][service]['environment']
            flags = [str(env[key]).lower() for key in ('sandbox', 'SANDBOX') if key in env]
            if not flags or any(value != 'false' for value in flags):
                raise DeployError('live_mode_must_be_explicit_false')
        api_env = config['services']['app-function-logic']['environment']
        web_env = config['services']['web-frontend']['environment']
        if (str(api_env.get('TVE_AUTH_DISABLED')).lower() != 'false'
                or not api_env.get('TVE_WEBHOOK_SECRET')
                or api_env['TVE_WEBHOOK_SECRET'] != web_env.get('TVE_WEBHOOK_SECRET')
                or api_env['TVE_WEBHOOK_SECRET'] == 'sandbox-webhook-secret'):
            raise DeployError('runtime_webhook_auth_mismatch')
        expected = {'app-function-logic': {('127.0.0.1', '5000', 8000)},
                    'web-frontend': {('127.0.0.1', '13000', 3000), ('127.0.0.1', '13001', 3001),
                                     ('127.0.0.1', '3002', 3002)}}
        for service, ports in expected.items():
            actual = {(p.get('host_ip'), str(p['published']), int(p['target']))
                      for p in config['services'][service].get('ports', [])}
            if actual != ports:
                raise DeployError('unexpected_live_ports')
        for service, definition in config['services'].items():
            for mount in definition.get('volumes', []):
                if mount['type'] == 'bind':
                    source = Path(mount['source'])
                    try:
                        source.relative_to(root)
                    except ValueError:
                        raise DeployError('unexpected_bind_mount') from None
                    if not source.exists():
                        raise DeployError('missing_bind_source')

    def check_sandbox_service(self, config, *, release=None):
        """Require an operator-provisioned isolated database before app rollout."""
        definition = config['services']['pg-sandbox']
        if (definition.get('container_name') != 'tve-pg-sandbox'
                or definition.get('image') != 'postgres:15-alpine'
                or definition.get('networks') != {'net-sandbox': None}
                or {item.get('source') for item in definition.get('volumes', [])
                    if item.get('type') == 'volume'} != {'pg_sandbox_data'}):
            raise DeployError('unexpected_sandbox_database_configuration')
        container = self.inspect('pg-sandbox')
        if (not container['State']['Running']
                or container['State'].get('Health', {}).get('Status') != 'healthy'):
            raise DeployError('sandbox_database_not_preprovisioned_healthy')
        wanted_env = {key: str(value) for key, value in definition.get('environment', {}).items()}
        actual_env = dict(item.split('=', 1) for item in container['Config'].get('Env', []))
        if any(actual_env.get(key) != value for key, value in wanted_env.items()):
            raise DeployError('sandbox_database_configuration_drift')
        # Relocate bind-mount source paths from the release tree to the live tree
        # so they match the running container's absolute mount sources.
        live = self.live
        def relocate_mount(value):
            if release and isinstance(value, str):
                if value == str(release) or value.startswith(str(release) + os.sep):
                    return str(live) + value[len(str(release)):]
            return value
        wanted_mounts = set()
        for v in definition.get('volumes', []):
            source = v['source']
            if v['type'] == 'bind':
                source = relocate_mount(source)
            wanted_mounts.add((v['target'], v['type'], SANDBOX_VOLUMES.get(v['source'], source),
                               not v.get('read_only', False)))
        actual_mounts = {(v['Destination'], v['Type'], v.get('Name') if v['Type'] == 'volume' else v['Source'], v['RW'])
                         for v in container['Mounts'] if v['Type'] != 'tmpfs'}
        if actual_mounts != wanted_mounts:
            raise DeployError('sandbox_database_mount_drift')
        wanted_network = config['networks']['net-sandbox']['name']
        if set(container['NetworkSettings']['Networks']) != {wanted_network}:
            raise DeployError('sandbox_database_network_drift')

    def running_matches(self, config, *, allow_stopped=(), allow_environment_drift=None):
        allow_stopped = set(allow_stopped)
        permitted_drift = allow_environment_drift or {}
        if permitted_drift and permitted_drift != {'app-function-logic': {'TVE_EMAIL_INTAKE'}}:
            raise DeployError('invalid_environment_drift_exception')
        for service, definition in config['services'].items():
            container = self.inspect(service)
            if not container['State']['Running'] and service not in allow_stopped:
                raise DeployError('existing_service_not_running')
            if service in (*DATABASES, 'pg-sandbox') and container['State'].get('Health', {}).get('Status') != 'healthy':
                raise DeployError('existing_database_not_healthy')
            actual_env = dict(item.split('=', 1) for item in container['Config'].get('Env', []))
            drift_keys = environment_mismatch_keys(definition.get('environment', {}), actual_env)
            permitted = permitted_drift.get(service, set())
            if drift_keys and set(drift_keys) != set(permitted):
                error = DeployError('running_environment_drift')
                error.service = service
                error.keys = drift_keys
                raise error
            wanted_mounts = {(v['target'], v['type'], {**VOLUMES, **SANDBOX_VOLUMES}.get(v['source'], v['source']), not v.get('read_only', False))
                             for v in definition.get('volumes', [])}
            actual_mounts = {(v['Destination'], v['Type'], v.get('Name') if v['Type'] == 'volume' else v['Source'], v['RW'])
                             for v in container['Mounts'] if v['Type'] != 'tmpfs'}
            if actual_mounts != wanted_mounts:
                raise DeployError('running_mount_drift')
            wanted_ports = {(str(p['target']) + '/tcp', p.get('host_ip'), str(p['published']))
                            for p in definition.get('ports', [])}
            actual_ports = {(target, binding['HostIp'], binding['HostPort'])
                            for target, bindings in container['HostConfig'].get('PortBindings', {}).items()
                            for binding in (bindings or [])}
            if actual_ports != wanted_ports:
                raise DeployError('running_port_drift')
            wanted_networks = {config['networks'][n]['name'] for n in definition.get('networks', {})}
            if set(container['NetworkSettings']['Networks']) != wanted_networks:
                raise DeployError('running_network_drift')

    def compatible(self, old, new, release):
        # Only application image/build/command/restart policy may change automatically.
        normalized = json.loads(json.dumps(new))
        # Normalize relocated bind/env-file paths only, never secret values or
        # arbitrary strings in resolved environment/configuration.
        def relocate(value):
            if value == str(release) or value.startswith(str(release) + os.sep):
                return str(self.live) + value[len(str(release)):]
            return value

        for definition in normalized['services'].values():
            for mount in definition.get('volumes', []):
                if mount['type'] == 'bind':
                    mount['source'] = relocate(mount['source'])
            if 'env_file' in definition:
                definition['env_file'] = [
                    {**item, 'path': relocate(item['path'])} if isinstance(item, dict) else relocate(item)
                    for item in definition['env_file']]
        old_volumes, new_volumes = old.get('volumes', {}), normalized.get('volumes', {})
        old_networks, new_networks = old.get('networks', {}), normalized.get('networks', {})
        old_services, new_services = old['services'], normalized['services']
        provisioned_sandbox_addition = (
            'pg-sandbox' in new_services and 'pg-sandbox' not in old_services
            and set(old_volumes) == set(new_volumes) - {'pg_sandbox_data'}
            and set(old_networks) == set(new_networks) - {'net-sandbox'}
            and set(old_services) == set(new_services) - {'pg-sandbox'}
            and old_volumes == {k: v for k, v in new_volumes.items() if k != 'pg_sandbox_data'}
            and old_networks == {k: v for k, v in new_networks.items() if k != 'net-sandbox'}
            and old.get('name') == normalized.get('name')
        )
        if (old.get('name') != normalized.get('name')
                or (old_volumes != new_volumes or old_networks != new_networks)
                and not provisioned_sandbox_addition
                or set(old_services) != set(new_services) and not provisioned_sandbox_addition):
            raise DeployError('infrastructure_change_requires_operator')
        for service, before in old_services.items():
            after = normalized['services'][service]
            if service not in SERVICES:
                if before != after:
                    raise DeployError('database_or_mailpit_config_change_requires_operator')
            else:
                allowed = {'build', 'image', 'command', 'restart'}
                before_runtime = {k: v for k, v in before.items() if k not in allowed}
                after_runtime = {k: v for k, v in after.items() if k not in allowed}
                if provisioned_sandbox_addition and service in SERVICES[:2]:
                    sandbox_env_keys = {'TVE_SANDBOX_DB_' + name.upper() for name in SANDBOX_DATABASES}
                    before_env = dict(before_runtime.get('environment', {}))
                    after_env = dict(after_runtime.get('environment', {}))
                    additions = {key: after_env.pop(key) for key in set(after_env) - set(before_env)}
                    if set(additions) != sandbox_env_keys or any(key in before_env for key in sandbox_env_keys):
                        raise DeployError('application_runtime_config_change_requires_operator')
                    before_runtime['environment'] = before_env
                    after_runtime['environment'] = after_env
                    for runtime in (before_runtime, after_runtime):
                        networks = runtime.get('networks', {})
                        if isinstance(networks, dict):
                            networks.pop('net-sandbox', None)
                            if not networks:
                                runtime.pop('networks', None)
                        depends = runtime.get('depends_on', {})
                        if isinstance(depends, dict):
                            depends.pop('pg-sandbox', None)
                            if not depends:
                                runtime.pop('depends_on', None)
                if before_runtime != after_runtime:
                    raise DeployError('application_runtime_config_change_requires_operator')
        if provisioned_sandbox_addition:
            sandbox = new_services['pg-sandbox']
            if (sandbox.get('container_name') != 'tve-pg-sandbox'
                    or sandbox.get('image') != 'postgres:15-alpine'
                    or sandbox.get('networks') != {'net-sandbox': None}
                    or {item.get('source') for item in sandbox.get('volumes', [])
                        if item.get('type') == 'volume'} != {'pg_sandbox_data'}):
                raise DeployError('unexpected_sandbox_database_configuration')

    def images(self, services):
        result = {service: self.inspect(service)['Image'] for service in services}
        if any(not re.fullmatch(r'sha256:[0-9a-f]{64}', value) for value in result.values()):
            raise DeployError('invalid_running_image_id')
        return result

    def tags(self, images, sha, transaction):
        tags = {service: f'tve-rollback:{sha[:12]}-{transaction}-{service}' for service in images}
        for service, image in images.items():
            self.docker('image', 'tag', image, tags[service])
        return tags

    def rollout(self, root, services, override, env_root=None):
        if services:
            self.compose(root, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never',
                         '--force-recreate', *services, override=override, env_root=env_root, timeout=180)

    def health(self, root, config):
        self.health_check='health_probe_unknown'
        try:
            self._health_checks(root,config)
        except BaseException as error:
            if isinstance(error,DeployError):
                error.health_check=self.health_check
                raise
            failure=DeployError('health_probe_failed')
            failure.health_check=self.health_check
            raise failure from None

    def _health_checks(self, root, config):
        secret = config['services']['app-function-logic']['environment']['TVE_WEBHOOK_SECRET']
        # Direct and same-origin protected GETs; never provider tests, properties,
        # agent tools, writes, simulation, invitations or mail delivery.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        for base in ('http://127.0.0.1:5000', 'http://127.0.0.1:3002'):
            self.health_check='api_healthz' if base.endswith(':5000') else 'frontend_healthz'
            self.get(opener, base + '/healthz', json_body=True)
        self.health_check='unauthenticated_settings'
        try:
            self.get(opener, 'http://127.0.0.1:5000/dashboard/settings', json_body=True)
        except urllib.error.HTTPError as error:
            if error.code not in (401, 403):
                raise DeployError('health_auth_check_failed') from None
            error.close()
        else:
            raise DeployError('health_auth_not_enforced')
        for path in ('settings', 'leads', 'funnel'):
            self.health_check='direct_'+path
            self.get(opener, 'http://127.0.0.1:5000/dashboard/' + path, secret, json_body=True)
            self.health_check='proxy_'+path
            self.get(opener, 'http://127.0.0.1:3002/sandbox/api/' + path, json_body=True)
        for asset in ('index.html', 'styles.css', 'app.js', 'assets/meesix-mark.svg'):
            self.health_check={'index.html':'asset_index','styles.css':'asset_styles',
                               'app.js':'asset_app','assets/meesix-mark.svg':'asset_brand'}[asset]
            actual = self.get(opener, 'http://127.0.0.1:3002/' + asset)
            expected = (root / APP / 'frontend/dashboard' / asset).read_bytes()
            if actual != expected:
                raise DeployError('dashboard_asset_not_released')
        self.health_check='worker_running'
        worker = self.inspect('imap-intake')
        if not worker['State']['Running'] or worker['State'].get('Restarting'):
            raise DeployError('worker_not_running')
        # Validation only: does not lock/write/reset checkpoints or poll a mailbox.
        code = ('import os,json; from pathlib import Path; from app.workers.imap_checkpoint import _validate; '
                'assert [os.environ[k].lower() for k in ("sandbox","SANDBOX") if k in os.environ] '
                'and all(os.environ[k].lower()=="false" for k in ("sandbox","SANDBOX") if k in os.environ); '
                'p=Path("/var/lib/tve-imap/checkpoint.json"); assert p.is_file(); _validate(json.loads(p.read_text()))')
        self.health_check='worker_checkpoint'
        self.docker('exec', 'tve-imap-intake', 'python', '-B', '-c', code)
        self.health_check='worker_stability'
        time.sleep(5)
        after = self.inspect('imap-intake')
        if (not after['State']['Running'] or after['State'].get('Restarting')
                or after['RestartCount'] != worker['RestartCount']
                or after['State']['StartedAt'] != worker['State']['StartedAt']):
            raise DeployError('worker_not_stable')

    def verify_delete_endpoint(self, config):
        self.health_check='delete_openapi'
        try:
            self._verify_delete_endpoint(config)
        except BaseException as error:
            if isinstance(error,DeployError):
                error.health_check=self.health_check
                raise
            failure=DeployError('delete_probe_failed')
            failure.health_check=self.health_check
            raise failure from None

    def _verify_delete_endpoint(self, config):
        """Exercise the actual API and same-origin DELETE handler without deleting a lead."""
        secret = config['services']['app-function-logic']['environment']['TVE_WEBHOOK_SECRET']
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        mode_body = json.loads(self.get(opener, 'http://127.0.0.1:5000/dashboard/settings/mode',
                                        secret, json_body=True))
        proxy_mode = json.loads(self.get(opener, 'http://127.0.0.1:3002/sandbox/api/settings/mode',
                                         json_body=True))
        openapi = json.loads(self.get(opener, 'http://127.0.0.1:5000/openapi.json', json_body=True))
        if 'delete' not in openapi.get('paths', {}).get('/dashboard/leads/{contact_id}', {}):
            raise DeployError('delete_openapi_operation_missing')
        self.health_check='delete_mode'
        sandbox = mode_body.get('sandbox')
        if not isinstance(sandbox, bool) or proxy_mode.get('sandbox') is not sandbox:
            raise DeployError('delete_probe_mode_unverified')
        contact_id = 9223372036854775000  # positive synthetic probe ID; rejected before any mutation
        mode = 'sandbox' if sandbox else 'production'
        # Deliberately omit the operation reference: the handler returns 422
        # before any DB access. Standard dashboard authentication and mode are
        # still present, and there is no second deletion credential.
        headers = {'X-TVE-Auth': secret, 'X-TVE-Mode': mode}
        expected = (422, 'Deletion operation reference required')
        for url, proxy, check in ((f'http://127.0.0.1:5000/dashboard/leads/{contact_id}', False, 'delete_api_handler'),
                                  (f'http://127.0.0.1:3002/sandbox/api/leads/{contact_id}', True, 'delete_proxy_handler')):
            self.health_check=check
            request_headers = dict(headers)
            if proxy:
                request_headers.pop('X-TVE-Auth', None)
            request = urllib.request.Request(url, data=b'', headers=request_headers, method='DELETE')
            try:
                with opener.open(request, timeout=10) as response:
                    status, raw = response.status, response.read(65536)
            except urllib.error.HTTPError as error:
                status, raw = error.code, error.read(65536)
                error.close()
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeError):
                error=DeployError('delete_probe_unreadable_response')
                error.observed_status=status
                error.expected_status=expected[0]
                raise error from None
            # The app's Starlette HTTP exception handler intentionally emits a
            # stable sanitized envelope rather than FastAPI's default `detail`.
            # Match the exact status and message; a generic 403 (for example
            # auth middleware rejection) must not satisfy this handler probe.
            error_body = body.get('error')
            handler_match = (isinstance(error_body, dict)
                             and error_body.get('code') == 'request_rejected'
                             and error_body.get('message') == expected[1])
            if status != expected[0] or not handler_match:
                error=DeployError('delete_probe_handler_not_reached')
                error.observed_status=status
                error.expected_status=expected[0]
                error.detail_match=handler_match
                raise error

    @staticmethod
    def get(opener, url, secret=None, json_body=False):
        request = urllib.request.Request(url, headers={'X-TVE-Auth': secret} if secret else {})
        with opener.open(request, timeout=10) as response:
            data = response.read(8 * 1024 * 1024 + 1)
            if response.status != 200 or len(data) > 8 * 1024 * 1024:
                raise DeployError('health_response_rejected')
            if json_body:
                body = json.loads(data)
                if not isinstance(body, (dict, list)) or isinstance(body, dict) and 'error' in body:
                    raise DeployError('health_json_rejected')
            return data


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeployError('health_redirect_rejected')


def current_main(repo, token):
    request = urllib.request.Request('https://api.github.com/repos/' + repo + '/git/ref/heads/main',
                                     headers={'Authorization': 'Bearer ' + token,
                                              'Accept': 'application/vnd.github+json',
                                              'X-GitHub-Api-Version': '2022-11-28',
                                              'Cache-Control': 'no-cache'})
    opener = urllib.request.build_opener(NoRedirect())
    with opener.open(request, timeout=20) as response:
        data = response.read(1024 * 1024 + 1)
        if len(data) > 1024 * 1024:
            raise DeployError('github_ref_response_too_large')
        return validated_sha(json.loads(data)['object']['sha'])


def image_override(path, tags):
    atomic_write(path, json.dumps({'services': {service: {'image': tag, 'pull_policy': 'never'}
                                                for service, tag in tags.items()}}).encode())


class Deployment:
    def __init__(self, source, host, state, sha, latest, ci_run_id=None):
        self.source, self.host, self.state, self.sha, self.latest = source, host, state, sha, latest
        self.live = host.live
        self.marker = state / 'deployed-commit'
        self.provenance = state / 'deployed-provenance.json'
        self.journal = None
        self.ci_run_id = ci_run_id

    def check_latest(self):
        if self.latest() != self.sha:
            raise StaleRelease()

    def journal_status(self, status, **values):
        self.record.update(status=status, **values)
        atomic_write(self.journal, json.dumps(self.record, sort_keys=True).encode())

    def run(self):
        self.check_latest()  # Inside flock, before touching live source or Docker.
        if self.marker.is_symlink() or self.provenance.is_symlink():
            raise DeployError('unsafe_release_marker')
        marker_before = self.marker.read_bytes()  # Required baseline, never guess mutable HEAD.
        previous = validated_sha(marker_before.decode().strip())
        if previous == self.sha:
            log('skip_already_deployed', sha=self.sha)
            return
        metadata_before = self.provenance.read_bytes() if self.provenance.exists() else None
        old, new = self.source.manifest(previous), self.source.manifest(self.sha)
        changed = {name for name in old.keys() | new.keys() if old.get(name) != new.get(name)}
        # Reject arbitrary schema edits before host/Docker inspection or staging.
        migration_candidate = lead_delete_schema_candidate(changed)
        delete_endpoint_changed = bool({APP + '/backend/app/routes/dashboard/leads.py',
                                        APP + '/frontend/serve-local.js'} & changed)
        services = []
        transaction = uuid.uuid4().hex[:12]
        backup = self.state / 'deploy-backups' / (self.sha[:12] + '-' + transaction)
        private_dir(backup)
        release = backup / 'release'
        private_dir(release)
        self.source.materialize(new, release)
        overlay = Overlay(self.live, release, backup / 'previous-source', old, new)
        overlay.prepare()
        env_bytes = {}
        for name in (APP + '/.env', APP + '/backend/.env'):
            runtime = safe_path(self.live, name)
            if not runtime.is_file():
                raise DeployError('missing_runtime_environment')
            env_bytes[name] = runtime.read_bytes()
            for root in (release, backup / 'previous-source'):
                target = safe_path(root, name)
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                atomic_write(target, env_bytes[name])
        atomic_write(backup / 'previous-marker', marker_before)
        if metadata_before is not None:
            atomic_write(backup / 'previous-provenance.json', metadata_before)
        old_config, new_config = self.host.config(self.live), self.host.config(release)
        self.host.check_config(old_config, self.live)
        self.host.check_config(new_config, release)
        self.host.compatible(old_config, new_config, release)
        self.host.running_matches(old_config)
        # Prove the previous release healthy before calling it a rollback baseline.
        self.host.health(self.live, old_config)
        migration_ok = validate_lead_delete_migration(
            self.source, self.state, previous, self.sha, changed, self.host, old_config)
        services = classify(changed, verified_migration=migration_ok)
        if not services:
            # No deployable path changed (workflows/docs/deployment-tooling only):
            # record the release without overlay/rollout; the release health run
            # could never pass its asset comparison against containers that still
            # serve the prior release. Collision safety and the baseline health
            # check ran; release the marker only.
            self.record = {'version': 1, 'source_sha': self.sha, 'previous_sha': previous,
                           'services': [], 'previous_images': {}, 'rollback_tags': {},
                           'changed_path_count': len(changed)}
            self.journal = backup / 'transaction.json'
            self.journal_status('released')
            atomic_write(self.marker, (self.sha + '\n').encode())
            log('released', sha=self.sha, services=[], changed_path_count=len(changed))
            return
        images = self.host.images(services)
        rollback_tags = self.host.tags(images, previous, transaction)
        rollback_override = backup / 'rollback-images.json'
        release_override = backup / 'release-images.json'
        image_override(rollback_override, rollback_tags)
        release_tags = {service: f'tve-release:{self.sha}-{service}' for service in services}
        image_override(release_override, release_tags)
        self.record = {'version': 1, 'source_sha': self.sha, 'previous_sha': previous,
                       'services': services, 'previous_images': images, 'rollback_tags': rollback_tags,
                       'changed_path_count': len(changed)}
        self.journal = backup / 'transaction.json'
        self.journal_status('prepared')
        dirty = rollout = False
        try:
            if services:
                self.host.compose(release, 'build', *services, override=release_override, timeout=1200)
            self.check_latest()  # A newer push during build is a successful skip.
            self.assert_env(env_bytes)
            self.journal_status('overlaying')
            dirty = True  # Copy failure must also restore every touched path.
            overlay.apply()
            self.check_latest()
            self.assert_env(env_bytes)
            self.journal_status('rolling_out')
            rollout = bool(services)  # Up can fail after partially recreating a service.
            self.host.rollout(self.live, services, release_override)
            self.journal_status('checking_health')
            self.wait_healthy(old_config, verify_delete_endpoint=delete_endpoint_changed)
            self.assert_env(env_bytes)
            provenance = {'version': 1, 'source_sha': self.sha, 'previous_sha': previous,
                          'ci_run_id': self.ci_run_id, 'services': services,
                          'images': self.host.images(SERVICES), 'rollback_images': images,
                          'released_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                          'backup': backup.name}
            atomic_write(self.provenance, json.dumps(provenance, sort_keys=True).encode())
            atomic_write(self.marker, (self.sha + '\n').encode())  # Only after all health checks.
            self.journal_status('released')
            log('released', sha=self.sha, services=services, changed_path_count=len(changed))
        except BaseException:
            # Do not let a second TERM interrupt best-effort rollback. SIGKILL/power
            # loss requires operator recovery from the durable private journal.
            handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
            for sig in handlers:
                signal.signal(sig, signal.SIG_IGN)
            try:
                self.journal_status('rolling_back')
                atomic_write(self.marker, marker_before)
                if metadata_before is None:
                    self.provenance.unlink(missing_ok=True)
                else:
                    atomic_write(self.provenance, metadata_before)
                if dirty:
                    overlay.restore()
                if rollout:
                    self.host.rollout(self.live, services, rollback_override, env_root=backup / 'previous-source')
                    self.wait_healthy(old_config)
                self.journal_status('rolled_back' if dirty else 'aborted_before_overlay')
                log('rollback_complete' if dirty else 'aborted_before_overlay', sha=self.sha, services=services)
            except BaseException:
                self.journal_status('rollback_failed_operator_required')
                raise DeployError('rollback_failed_operator_required') from None
            finally:
                for sig, handler in handlers.items():
                    signal.signal(sig, handler)
            raise

    def assert_env(self, before):
        if any(safe_path(self.live, name).read_bytes() != data for name, data in before.items()):
            raise DeployError('runtime_environment_changed_during_deploy')

    def wait_healthy(self, config, verify_delete_endpoint=False):
        deadline = time.monotonic() + 180
        failed_check='health_probe_unknown'
        observed_status=expected_status=None
        detail_match=None
        for attempt in range(12):
            try:
                self.host.health(self.live, config)
                if verify_delete_endpoint:
                    self.host.verify_delete_endpoint(config)
                return
            except InterruptedError:
                raise  # Cancellation must enter rollback, not become a health retry.
            except Exception as error:
                check=getattr(error,'health_check',None)
                if check in HEALTH_CHECKS:
                    failed_check=check
                if check in ('delete_api_handler','delete_proxy_handler'):
                    status=getattr(error,'observed_status',None)
                    expected=getattr(error,'expected_status',None)
                    if isinstance(status,int) and 100<=status<=599 and expected in (403,422):
                        observed_status,expected_status=status,expected
                        match=getattr(error,'detail_match',None)
                        if isinstance(match,bool):
                            detail_match=match
                if attempt == 11 or time.monotonic() >= deadline:
                    failure=DeployError('release_health_failed')
                    failure.health_check=failed_check
                    if observed_status is not None:
                        failure.observed_status=observed_status
                        failure.expected_status=expected_status
                        if detail_match is not None:
                            failure.detail_match=detail_match
                    raise failure from None
                time.sleep(5)


@contextlib.contextmanager
def host_lock(state):
    import fcntl  # Linux host only; tests run on Windows without importing this.
    lock = state / 'deploy.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if os.fstat(fd).st_uid != os.getuid():
            raise DeployError('unsafe_deployment_lock')
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Refuse a previous SIGKILL/power-loss transaction, do not layer on top.
        for journal in (state / 'deploy-backups').glob('*/transaction.json'):
            status = json.loads(journal.read_bytes())['status']
            if status not in ('released', 'rolled_back', 'aborted_before_overlay'):
                raise DeployError('incomplete_previous_transaction_operator_required')
        yield
    finally:
        os.close(fd)


def interrupted(_signum, _frame):
    raise InterruptedError('deployment_interrupted')


def main():
    os.umask(0o077)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, interrupted)
    try:
        event = json.loads(Path(os.environ['GITHUB_EVENT_PATH']).read_bytes())
        sha, repo = validate_event(event, os.environ)
        token = os.environ['GH_TOKEN']
        if not token:
            raise DeployError('missing_read_only_github_token')
        source = GitSource(Path(os.environ['GITHUB_WORKSPACE']), sha, Commands())
        if not LIVE.is_dir() or STATE.is_symlink():
            raise DeployError('missing_or_unsafe_live_root')
        private_dir(STATE / 'deploy-backups')
        with host_lock(STATE):
            Deployment(source, Host(LIVE, source.commands), STATE, sha,
                       lambda: current_main(repo, token), ci_run_id=event['workflow_run'].get('id')).run()
        return 0
    except StaleRelease:
        log('skip_stale_successful_sha')
        return 0
    except BaseException as error:
        # Never include command arguments, stderr, HTTP bodies, paths from API,
        # arbitrary exception strings or runtime env/config in Actions output.
        code = str(error) if isinstance(error, DeployError) else type(error).__name__
        if (code=='release_health_failed' and getattr(error,'health_check',None) in HEALTH_CHECKS):
            fields={'code':code,'health_check':error.health_check}
            status=getattr(error,'observed_status',None)
            expected=getattr(error,'expected_status',None)
            if (error.health_check in ('delete_api_handler','delete_proxy_handler')
                    and isinstance(status,int) and 100<=status<=599 and expected in (403,422)):
                fields.update(observed_status=status,expected_status=expected,
                              detail_match=getattr(error,'detail_match',None) is True)
            log('deployment_failed',**fields)
        elif (code == 'running_environment_drift'
                and getattr(error, 'service', None) in SERVICES + DATABASES + ('pg-sandbox', 'mock-inbox')
                and isinstance(getattr(error, 'keys', None), list)
                and all(isinstance(key, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*', key)
                        for key in error.keys)):
            log('deployment_failed', code=code, service=error.service, mismatched_environment_keys=error.keys)
        else:
            log('deployment_failed', code=code)
        return 1


if __name__ == '__main__':
    sys.exit(main())
