#!/usr/bin/env python3
"""Fixed-purpose, locked lead-deletion migration and receipt maintenance."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import hmac
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import urllib.error
import urllib.request
import uuid

STATE = Path('/home/zubair/apps/thames-valley')
LIVE = STATE / 'repo'
APP = LIVE / 'Apps/Questionnaire-and-Booking'
LOCK = STATE / 'deploy.lock'
APP_REL = 'Apps/Questionnaire-and-Booking'
SQL_001 = APP_REL + '/backend/docker/init-contacts/001-contacts.sql'
SQL_002 = APP_REL + '/backend/docker/init-contacts/002-lead-delete-migration.sql'
HASH_001 = '2438f72881f271a9febc554ba07611104c16017b77bbf1d292795a21bed2955a'
HASH_002 = 'aeabc20a730d2c26a13687565d877ad60da82597cb5c88ff937daa5efa4aad34'
MIGRATION_DIR = STATE / 'lead-delete-migration'
BACKUPS = MIGRATION_DIR / 'backups'
RECEIPTS = MIGRATION_DIR / 'receipts'
JOURNAL = MIGRATION_DIR / 'transaction.json'
KEY = MIGRATION_DIR / '.receipt-key'
SHA_RE = re.compile(r'[0-9a-f]{40}')
JOURNEY = ('contacts', 'enquiries', 'responses', 'tokens')
OPS_TYPES = {'contact_id': ('bigint', 'NO'), 'contact_uid': ('uuid', 'NO'),
             'operation_id': ('uuid', 'NO'), 'initiated_at': ('timestamp with time zone', 'NO')}


class MaintenanceError(Exception):
    pass


def emit(event, **fields):
    print(json.dumps({'event': event, **fields}, sort_keys=True), flush=True)


def checked_sha(value):
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise MaintenanceError('invalid_full_sha')
    return value


def private_dir(path):
    reject_symlink_ancestors(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    reject_symlink_ancestors(path)
    if (hasattr(os, 'getuid') and path.stat().st_uid != os.getuid()):
        raise MaintenanceError('unsafe_private_directory')
    path.chmod(0o700)


def reject_symlink_ancestors(path):
    for parent in (path, *path.parents):
        if parent == STATE.parent:
            break
        if parent.is_symlink():
            raise MaintenanceError('unsafe_private_state_symlink')


def atomic(path, data, mode=0o600):
    private_dir(path.parent)
    if path.is_symlink():
        raise MaintenanceError('unsafe_private_file')
    tmp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        tmp.unlink(missing_ok=True)


def command(args, *, capture=False, input_data=None, timeout=300):
    result = subprocess.run(args, input=input_data,
                            stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, timeout=timeout, check=False)
    if result.returncode:
        raise MaintenanceError('fixed_command_failed')
    return result.stdout or b''


def docker(*args, capture=False, input_data=None, timeout=300):
    return command(['docker', '--host', 'unix:///var/run/docker.sock', *args],
                   capture=capture, input_data=input_data, timeout=timeout)


def github_json(url, token):
    request = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'Cache-Control': 'no-cache'})
    with urllib.request.urlopen(request, timeout=25) as response:
        data = response.read(3 * 1024 * 1024 + 1)
    if len(data) > 3 * 1024 * 1024:
        raise MaintenanceError('github_response_oversize')
    return json.loads(data)


def gate(repo,actor,triggering_actor,event_name,workflow_ref,ref,run_id,run_attempt,sha,token):
    expected_workflow_ref=(repo+'/.github/workflows/lead-delete-migration.yml@refs/heads/main')
    if (repo!='Micha12344f/thames-valley' or actor!='Micha12344f'
            or not triggering_actor or triggering_actor!='Micha12344f'
            or event_name!='workflow_dispatch' or ref!='refs/heads/main'
            or workflow_ref!=expected_workflow_ref or not token
            or not isinstance(run_id,str) or not run_id.isdigit()
            or not isinstance(run_attempt,str) or not run_attempt.isdigit()):
        raise MaintenanceError('authorized_owner_dispatch_required')
    if checked_sha(github_json(f'https://api.github.com/repos/{repo}/git/ref/heads/main', token)['object']['sha']) != sha:
        raise MaintenanceError('target_not_current_main')
    run=github_json(f'https://api.github.com/repos/{repo}/actions/runs/{run_id}',token)
    if (run.get('id')!=int(run_id) or run.get('name')!='Lead deletion schema maintenance'
            or run.get('event')!='workflow_dispatch' or run.get('head_branch')!='main'
            or run.get('head_sha')!=sha or run.get('run_attempt')!=int(run_attempt)
            or (run.get('actor') or {}).get('login')!='Micha12344f'
            or (run.get('triggering_actor') or {}).get('login')!='Micha12344f'
            or not isinstance(run.get('workflow_id'),int)):
        raise MaintenanceError('workflow_dispatch_identity_mismatch')
    workflow=github_json(f"https://api.github.com/repos/{repo}/actions/workflows/{run['workflow_id']}",token)
    if (workflow.get('path')!='.github/workflows/lead-delete-migration.yml'
            or workflow.get('state')!='active'):
        raise MaintenanceError('maintenance_workflow_identity_mismatch')
    runs = github_json(f'https://api.github.com/repos/{repo}/actions/workflows/ci.yml/runs?head_sha={sha}&per_page=100', token)
    trusted=[r for r in runs.get('workflow_runs',[]) if r.get('name')=='Application CI'
        and r.get('head_sha')==sha and r.get('event')=='push' and r.get('head_branch')=='main'
        and (r.get('head_repository') or {}).get('full_name')==repo
        and r.get('status')=='completed' and r.get('conclusion')=='success']
    if not trusted:
        raise MaintenanceError('successful_exact_sha_main_ci_required')
    trusted.sort(key=lambda item:(item.get('run_number',0),item.get('id',0)),reverse=True)
    ci_run_id=trusted[0].get('id')
    if not isinstance(ci_run_id,int) or ci_run_id<=0:
        raise MaintenanceError('successful_ci_run_id_invalid')
    return ci_run_id


def lock_host():
    if LOCK.is_symlink() or not LOCK.is_file():
        raise MaintenanceError('deployment_lock_missing')
    fp = LOCK.open('r+b')
    fcntl.flock(fp, fcntl.LOCK_EX)
    return fp


def deployed_marker():
    path = STATE / 'deployed-commit'
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError('deployed_marker_missing')
    return checked_sha(path.read_text(encoding='ascii').strip())


def load_deployer():
    # Validate against the deployer shipped with this exact, CI-gated checkout.
    # The live source remains at the old release until the normal deploy follows
    # this migration, so importing it can expose a stale Host API.
    path = Path(__file__).resolve().with_name('deploy_tve.py')
    spec = importlib.util.spec_from_file_location('tve_deployer', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git_blob(sha, path):
    out = command(['git', '-C', str(Path(__file__).resolve().parents[3]), 'show', f'{sha}:{path}'], capture=True)
    return out


def migration_blobs(sha):
    one, two = git_blob(sha, SQL_001), git_blob(sha, SQL_002)
    if hashlib.sha256(one).hexdigest() != HASH_001:
        raise MaintenanceError('fresh_schema_hash_mismatch')
    if hashlib.sha256(two).hexdigest() != HASH_002 or len(two) != 449:
        raise MaintenanceError('migration_hash_mismatch')
    return one, two


def migration_paths(base, target):
    changed = command(['git','-C',str(Path(__file__).resolve().parents[3]),'diff','--name-only',
                       checked_sha(base),checked_sha(target),'--',SQL_001,SQL_002],capture=True)
    paths = sorted(x for x in changed.decode('utf-8','strict').splitlines() if x)
    if SQL_002 not in paths or any(x not in (SQL_001,SQL_002) for x in paths):
        raise MaintenanceError('unsupported_sql_change_set')
    return paths


def runtime_config(deployer, allowed_stopped_writers=(), writer_journal=None,
                   allow_email_intake_reconcile=False):
    host = deployer.Host(LIVE, deployer.Commands())
    config = host.config(LIVE)
    host.check_config(config, LIVE)
    allowed=set(allowed_stopped_writers)
    if allowed and allowed!={'app-function-logic','imap-intake'}:
        raise MaintenanceError('recovery_stopped_writer_set_invalid')
    # Host compares resolved env/mount/network/port config for every service.
    allowed_env_drift = {'app-function-logic': {'TVE_EMAIL_INTAKE'}} if allow_email_intake_reconcile else None
    host.running_matches(config,allow_stopped=allowed,allow_environment_drift=allowed_env_drift)
    for service in allowed:
        info=host.inspect(service)
        if not info['State']['Running'] and info['State'].get('Status') not in ('exited','created'):
            raise MaintenanceError('recovery_writer_state_unexpected')
    if writer_journal is not None:
        records=writer_journal.get('rollback_images',{})
        expected_ids=writer_journal.get('writer_container_ids',{})
        verify_rollback_image_records(records)
        if set(expected_ids)!=allowed:
            raise MaintenanceError('recovery_writer_container_manifest_invalid')
        phase=writer_journal.get('state')
        for service,record in records.items():
            info=host.inspect(service)
            if info.get('Image')!=record.get('image_id'):
                raise MaintenanceError('recovery_writer_image_drift')
            if (not re.fullmatch(r'[0-9a-f]{64}',str(expected_ids.get(service,'')))
                    or (phase!='restarting_writers' and info.get('Id')!=expected_ids.get(service))):
                raise MaintenanceError('recovery_writer_container_drift')
    return host, config


def verify_release_baseline(base,host,services,allow_image_services=()):
    path=STATE/'deployed-provenance.json'
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError('release_provenance_missing')
    try:
        provenance=json.loads(path.read_text())
    except (ValueError,UnicodeError):
        raise MaintenanceError('release_provenance_invalid') from None
    if provenance.get('version')!=1 or provenance.get('source_sha')!=base:
        raise MaintenanceError('release_provenance_marker_mismatch')
    actual=host.images(services)
    expected=provenance.get('images',{})
    mismatched=sorted(name for name in set(expected)|set(actual) if expected.get(name)!=actual.get(name))
    allowed=set(allow_image_services)
    if allowed and allowed!={'imap-intake'}:
        raise MaintenanceError('invalid_baseline_image_exception')
    if mismatched and set(mismatched)!=allowed:
        error=MaintenanceError('release_running_images_drift')
        error.services=mismatched
        raise error
    for transaction in (STATE/'deploy-backups').glob('*/transaction.json'):
        if transaction.is_symlink():
            raise MaintenanceError('release_transaction_journal_unsafe')
        try:
            record=json.loads(transaction.read_text())
        except (ValueError,UnicodeError):
            raise MaintenanceError('release_transaction_journal_invalid') from None
        if record.get('status') not in ('released','rolled_back','aborted_before_overlay'):
            raise MaintenanceError('release_transaction_recovery_required')
    return provenance


def container(name):
    info = json.loads(docker('container', 'inspect', 'tve-' + name, capture=True))[0]
    if not info['State']['Running'] or info['State'].get('Restarting'):
        raise MaintenanceError('container_not_running')
    return info


def database_targets(config):
    result = []
    for db in JOURNEY:
        env = config['services']['pg-' + db]['environment']
        result.append({'mode': 'production', 'domain': db, 'container': 'tve-pg-' + db,
                       'database': str(env['POSTGRES_DB']), 'user': str(env['POSTGRES_USER'])})
    if 'pg-sandbox' not in config['services']:
        raise MaintenanceError('sandbox_database_service_missing')
    sandbox = config['services']['pg-sandbox']['environment']
    user = str(sandbox['POSTGRES_USER'])
    for db in JOURNEY:
        result.append({'mode': 'sandbox', 'domain': db, 'container': 'tve-pg-sandbox',
                       'database': 'sandbox_' + db, 'user': user})
    identities = {(x['container'], x['database']) for x in result}
    if len(identities) != 8:
        raise MaintenanceError('journey_database_targets_not_distinct')
    return result


SCHEMA_SQL = """SELECT json_build_object(
 'database', current_database(), 'server', COALESCE(inet_server_addr()::text,'local'),
 'system_identifier', (SELECT system_identifier::text FROM pg_control_system()),
 'id', (SELECT json_build_object('type',data_type,'nullable',is_nullable)
        FROM information_schema.columns WHERE table_schema='public' AND table_name='contacts' AND column_name='id'),
 'uid', (SELECT json_build_object('type',data_type,'nullable',is_nullable,'default',column_default)
         FROM information_schema.columns WHERE table_schema='public' AND table_name='contacts' AND column_name='contact_uid'),
 'ops', (SELECT json_agg(json_build_object('name',column_name,'type',data_type,'nullable',is_nullable)
         ORDER BY ordinal_position) FROM information_schema.columns
         WHERE table_schema='public' AND table_name='lead_delete_operations'),
 'pk', (SELECT json_agg(a.attname ORDER BY a.attnum) FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
         JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(i.indkey)
         WHERE n.nspname='public' AND t.relname='lead_delete_operations' AND i.indisprimary),
 'contacts_pk', (SELECT json_agg(a.attname ORDER BY a.attnum) FROM pg_index i JOIN pg_class t ON t.oid=i.indrelid
         JOIN pg_namespace n ON n.oid=t.relnamespace JOIN pg_attribute a ON a.attrelid=t.oid AND a.attnum=ANY(i.indkey)
         WHERE n.nspname='public' AND t.relname='contacts' AND i.indisprimary))::text;"""


def inspect_db(target):
    out = docker('exec', target['container'], 'psql', '-X', '-A', '-t', '-v', 'ON_ERROR_STOP=1',
                 '-U', target['user'], '-d', target['database'], '-c', SCHEMA_SQL, capture=True)
    try:
        schema = json.loads(out.decode().strip())
    except (ValueError, UnicodeError):
        raise MaintenanceError('schema_probe_invalid') from None
    c = container(target['container'].removeprefix('tve-'))
    mounts = [m.get('Name') for m in c.get('Mounts', []) if m.get('Destination') == '/var/lib/postgresql/data']
    if len(mounts) != 1 or not mounts[0]:
        raise MaintenanceError('database_volume_identity_unverified')
    schema['identity'] = {'container_id': c['Id'], 'volume': mounts[0],
                          'server': schema.get('server'), 'system_identifier':schema.get('system_identifier'),
                          'database': schema.get('database')}
    schema['identity_sha256'] = hashlib.sha256(json.dumps(schema['identity'], sort_keys=True).encode()).hexdigest()
    ops = {x['name']: x for x in schema.get('ops') or []}
    uid = schema.get('uid') or {}
    schema['complete'] = valid_schema({'id':schema.get('id'),'uid':uid,'ops':schema.get('ops') or [],
                                        'pk':schema.get('pk'),'contacts_pk':schema.get('contacts_pk')})
    schema['target'] = {k: target[k] for k in ('mode','domain','container','database')}
    return schema


def require_backup_capacity(config):
    estimated=0
    for target in database_targets(config):
        raw=docker('exec',target['container'],'psql','-X','-A','-t','-v','ON_ERROR_STOP=1',
                   '-U',target['user'],'-d',target['database'],'-c',
                   'SELECT pg_database_size(current_database());',capture=True)
        try:
            size=int(raw.decode().strip())
        except (ValueError,UnicodeError):
            raise MaintenanceError('database_backup_size_unavailable') from None
        if size<0:
            raise MaintenanceError('database_backup_size_invalid')
        estimated+=size
    private_dir(BACKUPS)
    # 2x raw database size plus headroom; conservative against compressed dump
    # ratios and filesystem metadata. Fail before stopping writers if insufficient.
    required=estimated*2+(1024**3)
    if shutil.disk_usage(BACKUPS).free < required:
        raise MaintenanceError('insufficient_private_backup_capacity')


def valid_schema(schema):
    cid=schema.get('id') or {}
    uid=schema.get('uid') or {}
    ops={x.get('name'):x for x in schema.get('ops') or []}
    return (cid.get('type')=='bigint' and cid.get('nullable')=='NO' and schema.get('contacts_pk')==['id']
        and uid.get('type') == 'uuid' and uid.get('nullable') == 'NO'
        and uid.get('default')=='gen_random_uuid()' and set(ops) == set(('contact_id','contact_uid','operation_id','initiated_at'))
        and all((ops[k].get('type'), ops[k].get('nullable')) == v for k, v in OPS_TYPES.items())
        and schema.get('pk') == ['contact_id'])


def schema_set(config):
    targets=[t for t in database_targets(config) if t['domain']=='contacts']
    if len(targets)!=2 or {x['mode'] for x in targets}!={'production','sandbox'}:
        raise MaintenanceError('both_contacts_databases_required')
    rows = [inspect_db(t) for t in targets]
    if len({x['identity_sha256'] for x in rows}) != 2:
        raise MaintenanceError('contacts_database_identity_collision')
    return rows


def verify_schema_identity_snapshot(snapshot,current):
    expected={('production','contacts'):('tve-pg-contacts','contacts'),
              ('sandbox','contacts'):('tve-pg-sandbox','sandbox_contacts')}
    def normalize(rows):
        result={}
        for row in rows:
            target=row.get('target',{})
            key=(target.get('mode'),target.get('domain'))
            identity=row.get('identity',{})
            if (key not in expected or key in result
                    or (target.get('container'),target.get('database'))!=expected[key]
                    or identity.get('database')!=expected[key][1]
                    or not re.fullmatch(r'[0-9a-f]{64}',str(row.get('identity_sha256','')))):
                raise MaintenanceError('contacts_database_identity_snapshot_invalid')
            result[key]=row['identity_sha256']
        if set(result)!=set(expected):
            raise MaintenanceError('contacts_database_identity_snapshot_incomplete')
        return result
    if normalize(snapshot)!=normalize(current):
        raise MaintenanceError('contacts_database_system_identity_drift')


def live_mode(config):
    service = config['services']['app-function-logic']
    env = service.get('environment', {})
    flags = [str(env[k]).lower() for k in ('sandbox','SANDBOX') if k in env]
    if not flags or any(x != 'false' for x in flags):
        raise MaintenanceError('effective_runtime_must_be_production')


def dynamic_security_state():
    # Read only the persisted mode; no deletion-specific credential exists.
    code = '''import asyncio,json,os,sys
from app.services.shared import settings_store
async def read():
    return await settings_store.get('TVE_SANDBOX_MODE')
mode=asyncio.run(read())
mode=mode or os.environ.get('TVE_SANDBOX_MODE','FALSE')
print(json.dumps({'mode':'sandbox' if str(mode).upper()=='TRUE' else ('production' if str(mode).upper()=='FALSE' else 'invalid')}))
'''
    out=docker('exec','tve-app-function-logic','python','-c',code,capture=True,timeout=45)
    try:
        result=json.loads(out.decode().strip())
    except (ValueError,UnicodeError):
        raise MaintenanceError('runtime_security_state_unavailable') from None
    if result.get('mode') not in ('sandbox','production'):
        raise MaintenanceError('effective_mode_unverifiable')
    return result


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def dump_database(target, destination):
    if destination.exists() or destination.is_symlink():
        raise MaintenanceError('backup_path_already_exists')
    with destination.open('xb') as output:
        os.fchmod(output.fileno(), 0o600)
        result = subprocess.run(['docker','--host','unix:///var/run/docker.sock','exec',target['container'],
            'pg_dump','--format=custom','--no-owner','-U',target['user'],'-d',target['database']],
            stdout=output, stderr=subprocess.DEVNULL, timeout=1800, check=False)
        output.flush(); os.fsync(output.fileno())
    if result.returncode or destination.stat().st_size < 128:
        destination.unlink(missing_ok=True)
        raise MaintenanceError('database_backup_failed')
    validate_archive_stream(destination, ['docker','--host','unix:///var/run/docker.sock','exec','-i',
        target['container'],'pg_restore'])
    return {'path': str(destination.relative_to(STATE)), 'sha256': digest(destination),
            'bytes': destination.stat().st_size, **target}


def validate_archive_stream(archive, restore_command):
    """Decompress every TOC/data block without connecting to or changing a DB."""
    with archive.open('rb') as source:
        result=subprocess.run([*restore_command,'--exit-on-error','--no-owner','--no-privileges',
                               '--file=/dev/null'],stdin=source,stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,timeout=1800,check=False)
    if result.returncode:
        raise MaintenanceError('database_backup_archive_invalid')


def retired_delete_environment_key_present():
    path = APP / 'backend/.env'
    if path.is_symlink() or not path.is_file():
        raise MaintenanceError('backend_env_missing_or_unsafe')
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise MaintenanceError('backend_env_permissions_too_open')
    raw = path.read_bytes()
    lines = raw.decode('utf-8').splitlines(keepends=True)
    found = [line for line in lines if re.match(r'^\s*TVE_LEAD_DELETE_SECRET\s*=', line)]
    if len(found)>1:
        raise MaintenanceError('duplicate_retired_delete_environment_key')
    return bool(found)


def remove_retired_delete_environment_key():
    path=APP/'backend/.env'
    if not retired_delete_environment_key_present():
        return False
    lines=path.read_text(encoding='utf-8').splitlines(keepends=True)
    retained=''.join(line for line in lines if not re.match(r'^\s*TVE_LEAD_DELETE_SECRET\s*=', line)).encode()
    atomic(path,retained,stat.S_IMODE(path.stat().st_mode))
    return True


def receipt_key(create=False):
    reject_symlink_ancestors(KEY)
    if KEY.is_symlink():
        raise MaintenanceError('receipt_key_unsafe')
    if KEY.exists():
        st = KEY.stat()
        if stat.S_IMODE(st.st_mode) != 0o600 or (hasattr(os,'getuid') and st.st_uid != os.getuid()):
            raise MaintenanceError('receipt_key_permissions_invalid')
        value = KEY.read_bytes()
        if len(value) != 32:
            raise MaintenanceError('receipt_key_invalid')
        return value
    if not create:
        raise MaintenanceError('receipt_key_missing')
    value = secrets.token_bytes(32)
    atomic(KEY, value)
    return value


def sign_receipt(payload, create_key=False):
    return hmac.new(receipt_key(create_key), json.dumps(payload, sort_keys=True, separators=(',',':')).encode(), hashlib.sha256).hexdigest()


def stop_writers():
    # Stop intake/API only; proxy remains up and cloudflared/sshd are never touched.
    for service in ('app-function-logic','imap-intake'):
        info=json.loads(docker('container','inspect','tve-'+service,capture=True))[0]
        if info.get('State',{}).get('Running'):
            docker('stop','--time','45','tve-'+service,timeout=60)


def verify_rollback_image_records(image_records):
    if not isinstance(image_records,dict) or set(image_records)!={'app-function-logic','imap-intake'}:
        raise MaintenanceError('rollback_image_records_invalid')
    for service,record in image_records.items():
        if not isinstance(record,dict):
            raise MaintenanceError('rollback_image_records_invalid')
        image_id=record.get('image_id','')
        tag=record.get('tag','')
        if (not re.fullmatch(r'sha256:[0-9a-f]{64}',image_id)
                or not re.fullmatch(r'[0-9a-f]{64}',str(record.get('container_id','')))
                or not re.fullmatch(r'tve-migration-rollback:[0-9a-f]{32}-'+service,tag)):
            raise MaintenanceError('rollback_image_records_invalid')
        current=docker('image','inspect','--format','{{.Id}}',tag,capture=True).decode().strip()
        if current!=image_id:
            raise MaintenanceError('rollback_image_tag_drift')
    return {service:record['image_id'] for service,record in image_records.items()}


def start_writers(image_records, host, config):
    image_ids=verify_rollback_image_records(image_records)
    host.check_config(config,LIVE)
    host.running_matches(config,allow_stopped=('app-function-logic','imap-intake'))
    app_path=LIVE/APP_REL
    override = MIGRATION_DIR / ('restart-' + uuid.uuid4().hex + '.json')
    # Use immutable engine image IDs; the local tags are verified audit handles,
    # not the identifiers used by Compose for recreation.
    payload = {'services': {s: {'image': image_ids[s], 'pull_policy': 'never'} for s in image_ids}}
    atomic(override, json.dumps(payload).encode())
    try:
        args = ['compose','--project-name','tve-questionnaire-booking','--project-directory',str(app_path/'backend/docker'),
            '--env-file',str(app_path/'backend/.env'),'--env-file',str(app_path/'.env'),
            '-f',str(app_path/'backend/docker/docker-compose.yml'),'-f',str(override),
            'up','-d','--no-deps','--no-build','--pull','never','--force-recreate',*image_ids.keys()]
        docker(*args,timeout=300)
    finally:
        override.unlink(missing_ok=True)
    container_ids={}
    for service,image_id in image_ids.items():
        info=json.loads(docker('container','inspect','tve-'+service,capture=True))[0]
        if not info['State']['Running'] or info.get('Image')!=image_id:
            raise MaintenanceError('restarted_writer_image_mismatch')
        container_ids[service]=info['Id']
    host.running_matches(config)
    return container_ids


def completed_migration_receipt(base,sha,schemas,allow_pending_cleanup=False):
    if RECEIPTS.is_symlink() or not RECEIPTS.is_dir():
        raise MaintenanceError('migration_receipt_directory_unsafe')
    paths=migration_paths(base,sha)
    migration_blobs(sha)
    current_receipt=RECEIPTS/(sha+'.json')
    if current_receipt.exists():
        if not allow_pending_cleanup or current_receipt.is_symlink():
            raise MaintenanceError('receipt_already_exists')
        verified=verify_receipt(base,sha,schemas,allow_email_intake_drift=True,check_current_env=False)
        verify_backup_set(verified.get('backups',[]),verified.get('migration_id'))
        verify_environment_snapshots(verified,allow_intake_change=True,
                                     allow_retired_secret_removal=True,check_runtime_hash=False)
        verify_rollback_image_records(verified.get('rollback_images'))
        return sha,verified,paths
    candidates=[]
    for path in RECEIPTS.glob('*.json'):
        if path.is_symlink() or not path.is_file():
            raise MaintenanceError('migration_receipt_directory_unsafe')
        try:
            value=json.loads(path.read_text())
        except (ValueError,UnicodeError):
            raise MaintenanceError('migration_receipt_invalid') from None
        if value.get('base_sha')==base and value.get('target_sha')!=sha:
            candidates.append(value)
    if not candidates:
        raise MaintenanceError('previous_migration_receipt_required')
    validated=[]
    invariant_fields=('migration_id','migration_hashes','migration_paths','backups','schema_before',
                      'databases','rollback_images','base_sha')
    invariant=None
    for candidate in candidates:
        target=checked_sha(candidate.get('target_sha'))
        verified=verify_receipt(base,target,schemas,allow_email_intake_drift=True,
                                check_current_env=False)
        if (candidate.get('migration_paths')!=paths or paths!=migration_paths(base,target)
                or candidate.get('migration_hashes')!={'001':HASH_001,'002':HASH_002}):
            raise MaintenanceError('completed_migration_plan_changed')
        identity={key:verified.get(key) for key in invariant_fields}
        if invariant is None:
            invariant=identity
        elif identity!=invariant:
            raise MaintenanceError('previous_migration_receipt_chain_mismatch')
        created=verified.get('created_at_epoch')
        if not isinstance(created,int) or created<=0:
            raise MaintenanceError('previous_migration_receipt_timestamp_invalid')
        validated.append((created,target,verified))
    validated.sort(key=lambda row:(row[0],row[1]),reverse=True)
    _,old_sha,verified=validated[0]
    verify_backup_set(verified.get('backups',[]),verified.get('migration_id'))
    verify_environment_snapshots(verified,allow_intake_change=True,allow_retired_secret_removal=True)
    verify_rollback_image_records(verified.get('rollback_images'))
    return old_sha,verified,paths


def verify_environment_snapshots(receipt,allow_intake_change=False,allow_retired_secret_removal=False,
                                 check_runtime_hash=True):
    migration_id=receipt.get('migration_id')
    if not isinstance(migration_id,str) or not re.fullmatch(r'[0-9a-f]{32}',migration_id):
        raise MaintenanceError('receipt_environment_snapshot_id_invalid')
    root=BACKUPS/migration_id
    reject_symlink_ancestors(root)
    if (root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode)!=0o700
            or (hasattr(os,'getuid') and root.stat().st_uid!=os.getuid())):
        raise MaintenanceError('receipt_environment_snapshot_directory_unsafe')
    expected=receipt.get('runtime_env_sha256',{})
    for name in ('.env','backend/.env'):
        path=root/(name.replace('/','_')+'.previous')
        live=APP/name
        reject_symlink_ancestors(path)
        reject_symlink_ancestors(live)
        if (path.is_symlink() or live.is_symlink() or not path.is_file() or not live.is_file()
                or stat.S_IMODE(path.stat().st_mode)!=0o600
                or (hasattr(os,'getuid') and path.stat().st_uid!=os.getuid())
                or stat.S_IMODE(live.stat().st_mode)&0o077
                or (hasattr(os,'getuid') and live.stat().st_uid!=os.getuid())):
            raise MaintenanceError('receipt_environment_snapshot_mismatch')
        snapshot=path.read_bytes(); current=live.read_bytes()
        if name=='.env':
            if snapshot!=current or check_runtime_hash and hashlib.sha256(current).hexdigest()!=expected.get(name):
                error=MaintenanceError('receipt_environment_snapshot_mismatch')
                error.env_file=name
                error.env_keys=[]
                raise error
            continue
        try:
            old_lines=snapshot.decode('utf-8').splitlines(keepends=True)
            current_lines=current.decode('utf-8').splitlines(keepends=True)
        except UnicodeError:
            raise MaintenanceError('receipt_environment_snapshot_mismatch') from None
        def selected(lines,key):
            return [line for line in lines if re.match(r'^\s*'+re.escape(key)+r'\s*=',line)]
        if check_runtime_hash and hashlib.sha256(current).hexdigest()!=expected.get(name):
            raise MaintenanceError('receipt_environment_snapshot_mismatch')
        old_retired=selected(old_lines,'TVE_LEAD_DELETE_SECRET')
        new_retired=selected(current_lines,'TVE_LEAD_DELETE_SECRET')
        if len(old_retired)>1 or len(new_retired)>1:
            raise MaintenanceError('receipt_environment_snapshot_mismatch')
        if old_retired and new_retired and old_retired[0]!=new_retired[0]:
            raise MaintenanceError('receipt_environment_snapshot_mismatch')
        if bool(old_retired)!=bool(new_retired) and not allow_retired_secret_removal:
            raise MaintenanceError('receipt_environment_snapshot_mismatch')
        old_intake=selected(old_lines,'TVE_EMAIL_INTAKE')
        new_intake=selected(current_lines,'TVE_EMAIL_INTAKE')
        if len(old_intake)>1 or len(new_intake)>1:
            error=MaintenanceError('receipt_environment_snapshot_mismatch')
            error.env_file=name; error.env_keys=['TVE_EMAIL_INTAKE']
            raise error
        if not allow_intake_change and old_intake!=new_intake:
            error=MaintenanceError('receipt_environment_snapshot_mismatch')
            error.env_file=name; error.env_keys=['TVE_EMAIL_INTAKE']
            raise error
        remove_keys=('TVE_LEAD_DELETE_SECRET','TVE_EMAIL_INTAKE')
        old_retained=''.join(line for line in old_lines
                             if not any(re.match(r'^\s*'+re.escape(key)+r'\s*=',line) for key in remove_keys))
        new_retained=''.join(line for line in current_lines
                             if not any(re.match(r'^\s*'+re.escape(key)+r'\s*=',line) for key in remove_keys))
        if old_retained!=new_retained:
            old_values={line.split('=',1)[0].strip():line for line in old_lines if '=' in line and not line.lstrip().startswith('#')}
            new_values={line.split('=',1)[0].strip():line for line in current_lines if '=' in line and not line.lstrip().startswith('#')}
            changed=sorted(key for key in set(old_values)|set(new_values)
                           if key not in remove_keys
                           and old_values.get(key)!=new_values.get(key))
            error=MaintenanceError('receipt_environment_snapshot_mismatch')
            error.env_file=name; error.env_keys=changed
            raise error


def receipt_proven_writer_images(provenance,receipt):
    records=receipt.get('rollback_images',{})
    if set(records)!={'app-function-logic','imap-intake'}:
        raise MaintenanceError('receipt_writer_image_set_invalid')
    expected=provenance.get('images',{})
    result={}
    for service,record in records.items():
        image=record.get('image_id') if isinstance(record,dict) else None
        if (not re.fullmatch(r'sha256:[0-9a-f]{64}',str(image))
                or expected.get(service)!=image):
            raise MaintenanceError('receipt_image_provenance_mismatch')
        result[service]=image
    return result


def reconcile_email_intake_env(host, config, immutable_images):
    """Restore API/worker to receipt-proven image IDs and declared Compose env."""
    retired_present=retired_delete_environment_key_present()
    host.check_config(config,LIVE)
    service='app-function-logic'
    expected=config['services'][service].get('environment',{})
    if 'TVE_EMAIL_INTAKE' not in expected:
        raise MaintenanceError('declared_email_intake_setting_missing')
    info=host.inspect(service)
    actual=dict(item.split('=',1) for item in info.get('Config',{}).get('Env',[]))
    mismatches=[key for key,value in expected.items() if actual.get(key)!=str(value)]
    if mismatches not in ([],['TVE_EMAIL_INTAKE']):
        raise MaintenanceError('environment_drift_outside_reconcile_allowlist')
    if not info['State'].get('Running'):
        raise MaintenanceError('api_reconcile_baseline_invalid')
    worker=host.inspect('imap-intake')
    if not worker['State'].get('Running'):
        raise MaintenanceError('worker_reconcile_baseline_invalid')
    image_ids={}
    for name in ('app-function-logic','imap-intake'):
        image=immutable_images.get(name)
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',str(image)):
            raise MaintenanceError('receipt_proven_image_id_invalid')
        image_ids[name]=image
    if info.get('Image')!=image_ids['app-function-logic']:
        raise MaintenanceError('api_image_not_at_recorded_baseline')
    observed_worker_image=worker.get('Image','')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',observed_worker_image):
        raise MaintenanceError('worker_current_image_id_invalid')
    api_env=dict(item.split('=',1) for item in info.get('Config',{}).get('Env',[]))
    worker_env=dict(item.split('=',1) for item in worker.get('Config',{}).get('Env',[]))
    legacy_loaded=('TVE_LEAD_DELETE_SECRET' in api_env or 'TVE_LEAD_DELETE_SECRET' in worker_env)
    services=[]
    if mismatches or retired_present or legacy_loaded:
        services.append('app-function-logic')
    if retired_present or legacy_loaded or observed_worker_image!=image_ids['imap-intake']:
        services.append('imap-intake')
    if not services:
        host.running_matches(config)
        host.check_config(config,LIVE)
        host.health(LIVE,config)
        emit('recorded_writer_baseline_already_reconciled',services=['app-function-logic','imap-intake'],
             receipt_images_verified=True)
        return
    retired_removed=remove_retired_delete_environment_key()
    if retired_removed!=retired_present:
        raise MaintenanceError('retired_delete_environment_changed_during_reconcile')
    config=host.config(LIVE)
    host.check_config(config,LIVE)
    for name in ('app-function-logic','imap-intake'):
        if 'TVE_LEAD_DELETE_SECRET' in config['services'][name].get('environment',{}):
            raise MaintenanceError('retired_delete_environment_key_still_declared')
    override=MIGRATION_DIR/('reconcile-'+uuid.uuid4().hex+'.json')
    atomic(override,json.dumps({'services':{name:{'image':image_ids[name],'pull_policy':'never'}
                                for name in services}}).encode())
    old_ids={service:info.get('Id'), 'imap-intake':worker.get('Id')}
    try:
        host.compose(LIVE,'up','-d','--no-deps','--no-build','--pull','never','--force-recreate',*services,
                     override=override,timeout=300)
    finally:
        override.unlink(missing_ok=True)
    for name in services:
        image=image_ids[name]
        now=host.inspect(name)
        if not now['State'].get('Running') or now.get('Image')!=image or now.get('Id')==old_ids[name]:
            raise MaintenanceError('reconciled_writer_image_or_container_verification_failed')
    for name in ('app-function-logic','imap-intake'):
        runtime_env=dict(item.split('=',1) for item in host.inspect(name).get('Config',{}).get('Env',[]))
        if 'TVE_LEAD_DELETE_SECRET' in runtime_env:
            raise MaintenanceError('retired_delete_environment_key_still_loaded')
    # This normal strict comparison must now pass for every service and every key.
    host.running_matches(config)
    host.check_config(config,LIVE)
    host.health(LIVE,config)
    security=dynamic_security_state()
    if security['mode']!='production':
        raise MaintenanceError('reconciled_runtime_mode_verification_failed')
    emit('recorded_writer_baseline_reconciled',services=services,
         intake_environment_reconciled='app-function-logic' in services,
         retired_delete_key_removed=retired_removed or legacy_loaded,
         receipt_images_verified=True)


def reattest_existing_migration(base,sha,schemas,actor,run_id,ci_run_id,previous=None):
    old_sha,verified,paths=previous or completed_migration_receipt(base,sha,schemas)
    destination=RECEIPTS/(sha+'.json')
    if destination.exists() or destination.is_symlink():
        raise MaintenanceError('receipt_already_exists')
    renewed=dict(verified)
    renewed.update(target_sha=sha,migration_paths=paths,actor=actor,
                   workflow_run_id=run_id,ci_run_id=ci_run_id,
                   created_at_epoch=int(__import__('time').time()))
    renewed['runtime_env_sha256']=runtime_hashes()
    renewed['hmac_sha256']=sign_receipt(renewed)
    atomic(destination,json.dumps(renewed,sort_keys=True,indent=2).encode())
    emit('completed_migration_reattested',target_sha=sha,previous_receipt_sha=old_sha,
         database_count=2,backup_count=8,sql_plan_unchanged=True)


def finalize_reconciled_receipt(base,sha,schemas):
    receipt=verify_receipt(base,sha,schemas,allow_email_intake_drift=True,check_current_env=False)
    receipt['runtime_env_sha256']=runtime_hashes()
    receipt['hmac_sha256']=sign_receipt(receipt)
    atomic(RECEIPTS/(sha+'.json'),json.dumps(receipt,sort_keys=True,indent=2).encode())
    emit('reconciled_migration_receipt_finalized',target_sha=sha,environment_hashes_refreshed=True)


def restart_recorded_writers(journal,next_state,host,config):
    journal.update(state='restarting_writers',resume_state=next_state)
    atomic(JOURNAL,json.dumps(journal,sort_keys=True).encode())
    ids=start_writers(journal['rollback_images'],host,config)
    journal.update(state=next_state,resume_state=None,writer_container_ids=ids)
    atomic(JOURNAL,json.dumps(journal,sort_keys=True).encode())
    return ids


def archive_targets(config, release_id):
    directory = BACKUPS / release_id
    private_dir(directory)
    manifest = []
    for target in database_targets(config):
        filename = f"{target['mode']}-{target['domain']}.dump"
        manifest.append(dump_database(target, directory/filename))
    if len(manifest) != 8:
        raise MaintenanceError('incomplete_backup_manifest')
    return manifest


def apply_migration(config, target_sha):
    sql = git_blob(checked_sha(target_sha), SQL_002)
    if len(sql) != 449 or hashlib.sha256(sql).hexdigest() != HASH_002:
        raise MaintenanceError('migration_bytes_not_allowlisted')
    targets=[t for t in database_targets(config) if t['domain']=='contacts']
    if len(targets)!=2 or {x['mode'] for x in targets}!={'production','sandbox'}:
        raise MaintenanceError('both_contacts_databases_required')
    for target in targets:
        cmd = ['docker','--host','unix:///var/run/docker.sock','exec','-i',target['container'],
               'psql','-X','-v','ON_ERROR_STOP=1','-U',target['user'],'-d',target['database']]
        command(cmd,input_data=b'BEGIN;\n'+sql+b'\nCOMMIT;\n',timeout=300)
        result = inspect_db(target)
        if not result['complete']:
            raise MaintenanceError('post_migration_schema_verification_failed')


def images_for(services):
    result={}
    for s in services:
        info=container(s)
        image=info.get('Image','')
        if not re.fullmatch(r'sha256:[0-9a-f]{64}',image):
            raise MaintenanceError('rollback_image_id_invalid')
        tag='tve-migration-rollback:'+uuid.uuid4().hex+'-'+s
        docker('image','tag',image,tag)
        result[s]={'image_id':image,'tag':tag,'container_id':info['Id']}
    return result


def runtime_hashes():
    return {name:digest(APP/name) for name in ('.env','backend/.env')}


def write_receipt(base, target, schemas, schema_before, backups, images, sql_hashes, actor, run_id, ci_run_id,
                  migration_id):
    receipt={'version':1,'base_sha':base,'target_sha':target,'migration_hashes':sql_hashes,
        'migration_paths':migration_paths(base,target),
        'migration_id':migration_id,
        'databases':schemas,'schema_before':schema_before,'backups':backups,'rollback_images':images,
        'runtime_env_sha256':runtime_hashes(),'actor':actor,'workflow_run_id':run_id,'ci_run_id':ci_run_id,
        'created_at_epoch':int(__import__('time').time())}
    receipt['hmac_sha256']=sign_receipt(receipt,create_key=True)
    path=RECEIPTS/(target+'.json')
    if path.exists() or path.is_symlink():
        raise MaintenanceError('receipt_already_exists')
    atomic(path,json.dumps(receipt,sort_keys=True,indent=2).encode())
    return path


def validate_backup_manifest(items,migration_id):
    if not isinstance(items,list) or len(items)!=8:
        raise MaintenanceError('recovery_backup_manifest_incomplete')
    if not isinstance(migration_id,str) or not re.fullmatch(r'[0-9a-f]{32}',migration_id):
        raise MaintenanceError('recovery_backup_migration_id_invalid')
    expected={}
    for mode in ('production','sandbox'):
        for domain in JOURNEY:
            container='tve-pg-'+domain if mode=='production' else 'tve-pg-sandbox'
            database=domain if mode=='production' else 'sandbox_'+domain
            key=(mode,domain)
            expected[key]={'container':container,'database':database,
                           'path':f'lead-delete-migration/backups/{migration_id}/{mode}-{domain}.dump'}
    seen=set()
    for item in items:
        key=(item.get('mode'),item.get('domain'))
        actual=expected.get(key)
        if (actual is None or key in seen or any(item.get(k)!=v for k,v in actual.items())
                or not isinstance(item.get('user'),str) or not item['user']
                or not isinstance(item.get('bytes'),int) or item['bytes']<128
                or not re.fullmatch(r'[0-9a-f]{64}',str(item.get('sha256','')))):
            raise MaintenanceError('recovery_backup_manifest_invalid')
        seen.add(key)
    if len(seen)!=8:
        raise MaintenanceError('recovery_backup_manifest_incomplete')


def verify_backup_set(items,migration_id):
    validate_backup_manifest(items,migration_id)
    for item in items:
        path=STATE/item['path']
        reject_symlink_ancestors(path)
        if path.is_symlink() or not path.is_file() or digest(path)!=item['sha256'] or path.stat().st_size!=item['bytes']:
            raise MaintenanceError('recovery_backup_invalid')
        validate_archive_stream(path,['docker','--host','unix:///var/run/docker.sock','exec','-i',
                                      item['container'],'pg_restore'])


def verify_journal_env(journal):
    backup_id=journal.get('migration_id')
    if not isinstance(backup_id,str) or not re.fullmatch(r'[0-9a-f]{32}',backup_id):
        raise MaintenanceError('recovery_environment_snapshot_invalid')
    root=BACKUPS/backup_id
    before_hashes=journal.get('runtime_env_before_sha256',{})
    for name in ('.env','backend/.env'):
        snapshot=root/(name.replace('/','_')+'.previous')
        live=APP/name
        reject_symlink_ancestors(snapshot)
        reject_symlink_ancestors(live)
        if snapshot.is_symlink() or live.is_symlink() or not snapshot.is_file() or not live.is_file():
            raise MaintenanceError('recovery_environment_snapshot_missing')
        old=snapshot.read_bytes(); current=live.read_bytes()
        if hashlib.sha256(old).hexdigest()!=before_hashes.get(name):
            raise MaintenanceError('recovery_environment_snapshot_hash_mismatch')
        if current!=old:
            raise MaintenanceError('recovery_environment_file_drift')


def resume_transaction(base,sha,config,host,actor,run_id,ci_run_id):
    if JOURNAL.is_symlink() or not JOURNAL.is_file():
        raise MaintenanceError('migration_recovery_journal_missing')
    journal=json.loads(JOURNAL.read_text())
    if (journal.get('version')!=1 or journal.get('base_sha')!=base or journal.get('target_sha')!=sha
            or journal.get('sql_002_sha256')!=HASH_002):
        raise MaintenanceError('migration_recovery_target_mismatch')
    if journal.get('allowed_stopped_writers')!=['app-function-logic','imap-intake']:
        raise MaintenanceError('recovery_stopped_writer_set_invalid')
    images=journal.get('rollback_images')
    verify_rollback_image_records(images)
    if set(journal.get('writer_container_ids',{}))!={'app-function-logic','imap-intake'}:
        raise MaintenanceError('recovery_writer_container_manifest_invalid')
    verify_journal_env(journal)
    receipt_path=RECEIPTS/(sha+'.json')
    if receipt_path.exists():
        rows=schema_set(config)
        verify_receipt(base,sha,rows)
        restart_recorded_writers(journal,'migrated',host,config)
        cfg=host.config(LIVE); host.check_config(cfg,LIVE); host.running_matches(cfg); host.health(LIVE,cfg)
        JOURNAL.unlink()
        emit('existing_migration_receipt_reconciled',target_sha=sha)
        return
    state=journal.get('state')
    if state=='restarting_writers':
        state=journal.get('resume_state')
    if state=='preparing':
        # The program durably changes this state before any DB writes.
        restart_recorded_writers(journal,'preparing',host,config)
        JOURNAL.unlink()
        emit('migration_preparation_aborted_without_schema_write',target_sha=sha)
        return
    if state not in ('backed_up','applying','migrated'):
        raise MaintenanceError('migration_recovery_state_invalid')
    backups=journal.get('backups',[])
    migration_id=journal.get('migration_id')
    verify_backup_set(backups,migration_id)
    schema_before=journal.get('schema_before',[])
    current_schema=schema_set(config)
    verify_schema_identity_snapshot(schema_before,current_schema)
    try:
        security=dynamic_security_state()
        if security['mode']!='production':
            raise MaintenanceError('production_mode_required_for_migration')
        # Starting for the security-state probe is itself journaled and uses only
        # the recorded immutable IDs after tag->ID validation.
        restart_recorded_writers(journal,state,host,config)
        stop_writers()
        journal.update(state='applying',resume_state=None)
        atomic(JOURNAL,json.dumps(journal,sort_keys=True).encode())
        apply_migration(config,sha)  # Fixed additive DDL is idempotent; reconcile both DBs.
        schemas=schema_set(config)
        if len(schemas)!=2 or any(not row['complete'] for row in schemas):
            raise MaintenanceError('recovery_schema_verification_failed')
        journal.update(state='migrated',resume_state=None,databases=schemas,runtime_env_sha256=runtime_hashes())
        atomic(JOURNAL,json.dumps(journal,sort_keys=True).encode())
        restart_recorded_writers(journal,'migrated',host,config)
        cfg=host.config(LIVE); host.check_config(cfg,LIVE); host.running_matches(cfg); host.health(LIVE,cfg)
        write_receipt(base,sha,schemas,schema_before,backups,images,
                      {'001':HASH_001,'002':HASH_002},actor,run_id,ci_run_id,migration_id)
        JOURNAL.unlink()
        emit('migration_receipt_created_after_reconciliation',target_sha=sha,database_count=2,backup_count=8,
             observed_mode=security['mode'])
    except BaseException:
        # Keep journal durable and best-effort restore only the fixed old images.
        try:
            resume_state=journal.get('resume_state') or journal.get('state','migrated')
            restart_recorded_writers(journal,resume_state,host,config)
        except BaseException:
            pass
        raise


def action(args):
    sha=checked_sha(args.sha)
    repo=os.environ.get('GITHUB_REPOSITORY','')
    actor=os.environ.get('GITHUB_ACTOR','')
    triggering_actor=os.environ.get('GITHUB_TRIGGERING_ACTOR','')
    event_name=os.environ.get('TVE_EVENT_NAME','')
    workflow_ref=os.environ.get('TVE_WORKFLOW_REF','')
    ref=os.environ.get('TVE_REF','')
    run_id=os.environ.get('TVE_RUN_ID','')
    run_attempt=os.environ.get('TVE_RUN_ATTEMPT','')
    token=os.environ.get('GITHUB_TOKEN','')
    ci_run_id=gate(repo,actor,triggering_actor,event_name,workflow_ref,ref,run_id,run_attempt,sha,token)
    checkout=Path(__file__).resolve().parents[3]
    head=command(['git','-C',str(checkout),'rev-parse','HEAD'],capture=True).decode().strip()
    if head!=sha:
        raise MaintenanceError('checkout_not_requested_target_sha')
    one,two=migration_blobs(sha)
    lock=lock_host()
    try:
        # Recheck after acquiring the host lock; the SHA may have advanced while
        # this maintenance run waited behind an in-progress release.
        ci_run_id=gate(repo,actor,triggering_actor,event_name,workflow_ref,ref,run_id,run_attempt,sha,token)
        base=deployed_marker()
        if base==sha:
            raise MaintenanceError('target_already_deployed')
        if JOURNAL.exists() and args.action!='resume':
            raise MaintenanceError('unfinished_migration_transaction_requires_resume')
        recovery_journal=None
        allowed_stopped=()
        if args.action=='resume':
            if JOURNAL.is_symlink() or not JOURNAL.is_file():
                raise MaintenanceError('migration_recovery_journal_missing')
            recovery_journal=json.loads(JOURNAL.read_text())
            if recovery_journal.get('base_sha')!=base or recovery_journal.get('target_sha')!=sha:
                raise MaintenanceError('migration_recovery_target_mismatch')
            allowed_stopped=tuple(recovery_journal.get('allowed_stopped_writers',()))
            if set(allowed_stopped)!={'app-function-logic','imap-intake'}:
                raise MaintenanceError('recovery_stopped_writer_set_invalid')
        deployer=load_deployer()
        reconcile=args.action=='reconcile'
        host,config=runtime_config(deployer,allowed_stopped,writer_journal=recovery_journal,
                                   allow_email_intake_reconcile=reconcile)
        provenance=verify_release_baseline(base,host,deployer.SERVICES,
            allow_image_services=('imap-intake',) if reconcile else ())
        live_mode(config)
        schemas=schema_set(config)
        if reconcile:
            if len(schemas)!=2 or any(not row['complete'] for row in schemas):
                raise MaintenanceError('completed_contacts_migration_required')
            security=dynamic_security_state()
            if security['mode']!='production':
                raise MaintenanceError('production_mode_required_for_reconcile')
            receipt_path=RECEIPTS/(sha+'.json')
            if receipt_path.exists() or receipt_path.is_symlink():
                previous=completed_migration_receipt(base,sha,schemas,allow_pending_cleanup=True)
            else:
                previous=completed_migration_receipt(base,sha,schemas)
                # Create a signed, same-plan target receipt before the only
                # runtime-file edit. It makes cleanup restartable after SIGKILL.
                reattest_existing_migration(base,sha,schemas,actor,run_id,ci_run_id,previous=previous)
                previous=completed_migration_receipt(base,sha,schemas,allow_pending_cleanup=True)
            immutable_images=receipt_proven_writer_images(provenance,previous[1])
            reconcile_email_intake_env(host,config,immutable_images)
            # Re-read all invariants after the narrowly scoped recreation.
            refreshed=host.config(LIVE)
            host.check_config(refreshed,LIVE)
            host.running_matches(refreshed)
            host.health(LIVE,refreshed)
            refreshed_schemas=schema_set(refreshed)
            finalize_reconciled_receipt(base,sha,refreshed_schemas)
            return
        if args.action=='inspect':
            mode_state=dynamic_security_state()
            emit('schema_inspection',sha=sha,base_sha=base,sql_002_sha256=HASH_002,
                 effective_mode=mode_state['mode'],
                 databases=[{'mode':r['target']['mode'],'domain':r['target']['domain'],
                             'database':r['identity']['database'],'identity_sha256':r['identity_sha256'],
                             'schema_complete':r['complete']} for r in schemas])
            return
        if args.action=='verify':
            verify_receipt(base,sha,schemas)
            emit('migration_receipt_verified',target_sha=sha,database_count=2,backup_count=8)
            return
        if args.action=='resume':
            resume_transaction(base,sha,config,host,actor,run_id,ci_run_id)
            return
        if args.action!='apply':
            raise MaintenanceError('unknown_fixed_action')
        receipt_path=RECEIPTS/(sha+'.json')
        if receipt_path.exists():
            raise MaintenanceError('receipt_exists_for_target')
        # Verify all expected services/config and capture exact old app/worker image IDs.
        services=('app-function-logic','imap-intake')
        old_images=images_for(services)
        migration_id=uuid.uuid4().hex
        private_dir(BACKUPS/migration_id)
        private_dir(MIGRATION_DIR)
        for env_name in ('.env','backend/.env'):
            atomic(BACKUPS/migration_id/(env_name.replace('/','_')+'.previous'),(APP/env_name).read_bytes())
        atomic(JOURNAL,json.dumps({'version':1,'state':'preparing','migration_id':migration_id,
            'base_sha':base,'target_sha':sha,'sql_002_sha256':HASH_002,
         'rollback_images':old_images,'allowed_stopped_writers':['app-function-logic','imap-intake'],
         'writer_container_ids':{service:record['container_id'] for service,record in old_images.items()},
            'runtime_env_before_sha256':{name:hashlib.sha256((APP/name).read_bytes()).hexdigest()
                                         for name in ('.env','backend/.env')}},sort_keys=True).encode())
        try:
            require_backup_capacity(config)
            stop_writers()
            backups=archive_targets(config,migration_id)
            # Verify production and sandbox database identities and all 8 backups before writes.
            schemas=schema_set(config)
            if any(r['identity']['database']!=r['target']['database'] for r in schemas):
                raise MaintenanceError('database_identity_changed')
            journal={'version':1,'state':'backed_up','migration_id':migration_id,'base_sha':base,
                     'target_sha':sha,'sql_002_sha256':HASH_002,'rollback_images':old_images,
                     'allowed_stopped_writers':['app-function-logic','imap-intake'],
                     'writer_container_ids':{service:record['container_id'] for service,record in old_images.items()},
                     'backups':backups,'schema_before':schemas,
                     'runtime_env_before_sha256':{name:hashlib.sha256((APP/name).read_bytes()).hexdigest()
                                                  for name in ('.env','backend/.env')}}
            atomic(JOURNAL,json.dumps(journal,sort_keys=True).encode())
            if journal['runtime_env_before_sha256']!={name:hashlib.sha256((APP/name).read_bytes()).hexdigest()
                                                      for name in ('.env','backend/.env')}:
                raise MaintenanceError('runtime_env_changed_before_migration')
            journal.update(state='applying')
            atomic(JOURNAL,json.dumps(journal,sort_keys=True).encode())
            apply_migration(config,sha)
            schemas=schema_set(config)
            if len(schemas)!=2 or any(not x['complete'] for x in schemas):
                raise MaintenanceError('both_contacts_schemas_must_verify')
            journal.update(state='migrated',resume_state=None,databases=schemas,
                           runtime_env_sha256=runtime_hashes())
            atomic(JOURNAL,json.dumps(journal,sort_keys=True).encode())
            restart_recorded_writers(journal,'migrated',host,config)
            # Normal deployer health validates API/frontend/worker before migration receipt.
            new_config=host.config(LIVE)
            host.check_config(new_config,LIVE)
            host.running_matches(new_config)
            host.health(LIVE,new_config)
            write_receipt(base,sha,schemas,journal['schema_before'],backups,old_images,
                          {'001':hashlib.sha256(one).hexdigest(),'002':hashlib.sha256(two).hexdigest()},
                          actor,run_id,ci_run_id,migration_id)
            JOURNAL.unlink()
            emit('migration_receipt_created',target_sha=sha,database_count=2,
                 backup_count=len(backups),observed_mode=dynamic_security_state()['mode'])
        except BaseException:
            # Preserve the verified backup/journal. Explicit `resume` reconciles.
            try:
                if JOURNAL.is_file() and not JOURNAL.is_symlink():
                    recovery=json.loads(JOURNAL.read_text())
                    verify_journal_env(recovery)
                    next_state=recovery.get('resume_state') or recovery.get('state','migrated')
                    restart_recorded_writers(recovery,next_state,host,config)
                else:
                    start_writers(old_images,host,config)
            except BaseException:
                pass
            raise
    finally:
        lock.close()


def verify_receipt(base,sha,live_schemas,allow_email_intake_drift=False,check_current_env=True):
    path=RECEIPTS/(sha+'.json')
    reject_symlink_ancestors(path)
    if path.is_symlink() or not path.is_file() or stat.S_IMODE(path.stat().st_mode)!=0o600:
        raise MaintenanceError('migration_receipt_missing_or_unsafe')
    receipt=json.loads(path.read_text())
    signature=receipt.pop('hmac_sha256',None)
    if not isinstance(signature,str) or not hmac.compare_digest(signature,sign_receipt(receipt)):
        raise MaintenanceError('migration_receipt_tampered')
    if receipt.get('version')!=1 or receipt.get('target_sha')!=sha or receipt.get('base_sha')!=base:
        raise MaintenanceError('migration_receipt_stale_or_wrong_release')
    if receipt.get('migration_hashes')!={'001':HASH_001,'002':HASH_002}:
        raise MaintenanceError('migration_receipt_sql_plan_mismatch')
    if receipt.get('migration_paths')!=migration_paths(base,sha):
        raise MaintenanceError('migration_receipt_changed_paths_mismatch')
    if receipt.get('runtime_env_sha256')!=runtime_hashes():
        if not allow_email_intake_drift:
            raise MaintenanceError('migration_receipt_runtime_env_drift')
        if check_current_env:
            verify_environment_snapshots(receipt,allow_intake_change=True)
    if (receipt.get('actor')!='Micha12344f' or not str(receipt.get('workflow_run_id','')).isdigit()
            or not str(receipt.get('ci_run_id','')).isdigit()):
        raise MaintenanceError('migration_receipt_authority_metadata_invalid')
    if len(live_schemas)!=2 or any(not r.get('complete') for r in live_schemas):
        raise MaintenanceError('live_migration_schema_incomplete')
    recorded=receipt.get('databases',[])
    verify_schema_identity_snapshot(recorded,live_schemas)
    before=receipt.get('schema_before',[])
    verify_schema_identity_snapshot(before,live_schemas)
    backups=receipt.get('backups',[])
    migration_id=receipt.get('migration_id')
    validate_backup_manifest(backups,migration_id)
    for item in backups:
        relative=Path(item.get('path',''))
        p=STATE/relative
        reject_symlink_ancestors(p)
        if p.is_symlink() or not p.is_file() or digest(p)!=item['sha256'] or p.stat().st_size!=item['bytes']:
            raise MaintenanceError('migration_receipt_backup_tampered')
    return receipt


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--action',choices=('inspect','apply','resume','verify','reconcile'),required=True)
    p.add_argument('--sha',required=True)
    action(p.parse_args())


if __name__=='__main__':
    try:
        main()
    except (MaintenanceError,OSError,ValueError,KeyError,json.JSONDecodeError,
            subprocess.SubprocessError,urllib.error.URLError) as exc:
        fields={}
        services=getattr(exc,'services',None)
        if (isinstance(exc,MaintenanceError) and exc.args[0]=='release_running_images_drift'
                and isinstance(services,list)
                and all(service in ('app-function-logic','imap-intake','web-frontend') for service in services)):
            fields['services']=services
        env_file=getattr(exc,'env_file',None)
        env_keys=getattr(exc,'env_keys',None)
        if (isinstance(exc,MaintenanceError) and exc.args[0]=='receipt_environment_snapshot_mismatch'
                and env_file in ('.env','backend/.env') and isinstance(env_keys,list)
                and all(isinstance(key,str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_]*',key) for key in env_keys)):
            fields['env_file']=env_file
            fields['changed_keys']=env_keys
        emit('lead_delete_maintenance_failed',code=exc.args[0] if isinstance(exc,MaintenanceError) else 'maintenance_failed',**fields)
        sys.exit(1)
