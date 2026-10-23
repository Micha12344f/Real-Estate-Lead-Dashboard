"""Offline tests for fixed-schema migration and release receipt guards."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import uuid
from types import SimpleNamespace

import pytest

TOOL=Path(__file__).with_name('lead_delete_maintenance.py')
spec=importlib.util.spec_from_file_location('lead_delete_maintenance',TOOL)
m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ROOT=TOOL.parents[3]
APP=ROOT/'Apps/Questionnaire-and-Booking'


def test_load_deployer_uses_guarded_checkout_not_old_live_source(monkeypatch, tmp_path):
    monkeypatch.setattr(m, 'APP', tmp_path/'old-live-release')
    deployer=m.load_deployer()
    assert Path(deployer.__file__).resolve()==TOOL.with_name('deploy_tve.py').resolve()


def test_reconcile_removes_retired_env_key_and_restores_receipt_images(monkeypatch,tmp_path,capsys):
    state={
        'app-function-logic':{'Id':'a'*64,'Image':'sha256:'+'b'*64,'State':{'Running':True},
            'Config':{'Env':['TVE_EMAIL_INTAKE=old-mode','TVE_AUTH_DISABLED=false','TVE_LEAD_DELETE_SECRET=retired']}},
        'imap-intake':{'Id':'d'*64,'Image':'sha256:'+'c'*64,'State':{'Running':True},
            'Config':{'Env':['TVE_EMAIL_INTAKE=declared-mode','TVE_AUTH_DISABLED=false','TVE_LEAD_DELETE_SECRET=retired']}}}
    expected_images={'app-function-logic':'sha256:'+'b'*64,'imap-intake':'sha256:'+'e'*64}
    calls=[]
    class Host:
        def config(self,root):
            calls.append(('config',root)); return cfg
        def inspect(self,service):
            return dict(state[service])
        def compose(self,root,*args,**kwargs):
            calls.append(('compose',args,kwargs))
            assert args==('up','-d','--no-deps','--no-build','--pull','never','--force-recreate',
                          'app-function-logic','imap-intake')
            for name,image in expected_images.items():
                state[name]['Id']='f'*64 if name=='app-function-logic' else '1'*64
                state[name]['Image']=image
                state[name]['Config']['Env']=['TVE_EMAIL_INTAKE=declared-mode','TVE_AUTH_DISABLED=false']
        def running_matches(self,cfg): calls.append(('strict-running-check',cfg))
        def check_config(self,cfg,root): calls.append(('config-check',root))
        def health(self,root,cfg): calls.append(('health',root))
    monkeypatch.setattr(m,'MIGRATION_DIR',tmp_path/'migration')
    monkeypatch.setattr(m,'LIVE',tmp_path/'live')
    app=tmp_path/'live'/m.APP_REL
    (app/'backend').mkdir(parents=True)
    (app/'backend/.env').write_text('TVE_EMAIL_INTAKE=old-mode\nTVE_LEAD_DELETE_SECRET=retired\n')
    (app/'backend/.env').chmod(0o600)
    (app/'.env').write_text('')
    monkeypatch.setattr(m,'APP',app)
    monkeypatch.setattr(m,'dynamic_security_state',lambda:{'mode':'production'})
    cfg={'services':{'app-function-logic':{'environment':{
        'TVE_EMAIL_INTAKE':'declared-mode','TVE_AUTH_DISABLED':'false'}},
        'imap-intake':{'environment':{'TVE_EMAIL_INTAKE':'declared-mode','TVE_AUTH_DISABLED':'false'}}}}
    m.reconcile_email_intake_env(Host(),cfg,expected_images)
    out=capsys.readouterr().out
    assert 'TVE_LEAD_DELETE_SECRET' not in out
    assert [x[0] for x in calls].count('compose')==1
    assert [x[0] for x in calls].count('strict-running-check')==1
    assert 'TVE_LEAD_DELETE_SECRET' not in (app/'backend/.env').read_text()
    assert all('TVE_LEAD_DELETE_SECRET' not in item for info in state.values() for item in info['Config']['Env'])
    assert state['app-function-logic']['Image']==expected_images['app-function-logic']
    assert state['imap-intake']['Image']==expected_images['imap-intake']


def test_reconcile_rejects_any_additional_environment_drift(monkeypatch,tmp_path):
    class Host:
        def config(self,root): return cfg
        def check_config(self,cfg,root): pass
        def inspect(self,service):
            return {'Id':'a'*64,'Image':'sha256:'+'b'*64,'State':{'Running':True},
                    'Config':{'Env':['TVE_EMAIL_INTAKE=old','TVE_AUTH_DISABLED=true']}}
    called=[]
    monkeypatch.setattr(m,'docker',lambda *args,**kwargs:called.append(args))
    cfg={'services':{'app-function-logic':{'environment':{
        'TVE_EMAIL_INTAKE':'new','TVE_AUTH_DISABLED':'false'}}}}
    app=tmp_path/'app'; (app/'backend').mkdir(parents=True); (app/'backend/.env').write_text('')
    (app/'backend/.env').chmod(0o600); (app/'.env').write_text('')
    monkeypatch.setattr(m,'APP',app); monkeypatch.setattr(m,'LIVE',tmp_path)
    with pytest.raises(m.MaintenanceError,match='environment_drift_outside_reconcile_allowlist'):
        m.reconcile_email_intake_env(Host(),cfg,{'app-function-logic':'sha256:'+'b'*64,'imap-intake':'sha256:'+'c'*64})
    assert called==[]


def test_reconcile_uses_only_receipt_proven_baseline_writer_images():
    provenance={'images':{'app-function-logic':'sha256:'+'a'*64,'imap-intake':'sha256:'+'b'*64}}
    receipt={'rollback_images':{
        'app-function-logic':{'image_id':'sha256:'+'a'*64},
        'imap-intake':{'image_id':'sha256:'+'b'*64}}}
    assert m.receipt_proven_writer_images(provenance,receipt)==provenance['images']
    receipt['rollback_images']['imap-intake']['image_id']='sha256:'+'c'*64
    with pytest.raises(m.MaintenanceError,match='receipt_image_provenance_mismatch'):
        m.receipt_proven_writer_images(provenance,receipt)


def test_reconcile_requires_private_env_snapshots_matching_receipt_hashes(tmp_path,monkeypatch):
    migration_id='d'*32
    root=tmp_path/migration_id; root.mkdir(mode=0o700)
    app_env=b'APP_RUNTIME=unchanged\n'; backend_env=b'BACKEND_RUNTIME=unchanged\nTVE_EMAIL_INTAKE=imap\nTVE_LEAD_DELETE_SECRET=legacy\n'
    live_app=tmp_path/'app'; (live_app/'backend').mkdir(parents=True)
    (live_app/'.env').write_bytes(app_env)
    backend_current=b'BACKEND_RUNTIME=unchanged\nTVE_EMAIL_INTAKE=webhook\n'
    (live_app/'backend/.env').write_bytes(backend_current)
    (live_app/'.env').chmod(0o600); (live_app/'backend/.env').chmod(0o600)
    (root/'.env.previous').write_bytes(app_env)
    (root/'backend_.env.previous').write_bytes(backend_env)
    (root/'.env.previous').chmod(0o600)
    (root/'backend_.env.previous').chmod(0o600)
    monkeypatch.setattr(m,'BACKUPS',tmp_path)
    monkeypatch.setattr(m,'APP',live_app)
    receipt={'migration_id':migration_id,'runtime_env_sha256':{
        '.env':hashlib.sha256(app_env).hexdigest(),
        'backend/.env':hashlib.sha256(backend_current).hexdigest()}}
    m.verify_environment_snapshots(receipt,allow_intake_change=True,allow_retired_secret_removal=True)
    (live_app/'backend/.env').write_bytes((live_app/'backend/.env').read_bytes()+b'TVE_SMTP_PORT=2525\n')
    (live_app/'backend/.env').chmod(0o600)
    with pytest.raises(m.MaintenanceError,match='environment_snapshot_mismatch') as drift:
        m.verify_environment_snapshots(receipt,allow_intake_change=True,allow_retired_secret_removal=True)
    assert '2525' not in str(drift.value)


def test_dispatch_gate_binds_owner_initiator_rerunner_run_and_workflow(monkeypatch):
    repo='Micha12344f/thames-valley'; sha='a'*40; run_id='987654'; workflow_id=375591068
    calls=[]
    def api(url,token):
        calls.append(url)
        if url.endswith('/git/ref/heads/main'):
            return {'object':{'sha':sha}}
        if url.endswith('/actions/runs/'+run_id):
            return {'id':int(run_id),'name':'Lead deletion schema maintenance','event':'workflow_dispatch',
                'head_branch':'main','head_sha':sha,'run_attempt':2,'workflow_id':workflow_id,
                'actor':{'login':'Micha12344f'},'triggering_actor':{'login':'Micha12344f'}}
        if url.endswith('/actions/workflows/'+str(workflow_id)):
            return {'path':'.github/workflows/lead-delete-migration.yml','state':'active'}
        if '/actions/workflows/ci.yml/runs?' in url:
            return {'workflow_runs':[{'name':'Application CI','head_sha':sha,'event':'push','head_branch':'main',
                'head_repository':{'full_name':repo},'status':'completed','conclusion':'success',
                'run_number':44,'id':123456789}]}
        raise AssertionError('Unexpected GitHub API endpoint')
    monkeypatch.setattr(m,'github_json',api)
    result=m.gate(repo,'Micha12344f','Micha12344f','workflow_dispatch',
        repo+'/.github/workflows/lead-delete-migration.yml@refs/heads/main','refs/heads/main',
        run_id,'2',sha,'read-only-token')
    assert result==123456789
    assert any('/actions/runs/'+run_id in url for url in calls)


def test_gate_rejects_owner_initiated_run_rerun_by_other_actor_before_api(monkeypatch):
    calls=[]
    monkeypatch.setattr(m,'github_json',lambda *args:calls.append(args))
    with pytest.raises(m.MaintenanceError,match='authorized_owner_dispatch_required'):
        m.gate('Micha12344f/thames-valley','Micha12344f','OtherMaintainer','workflow_dispatch',
            'Micha12344f/thames-valley/.github/workflows/lead-delete-migration.yml@refs/heads/main',
            'refs/heads/main','123456','2','a'*40,'token')
    assert calls==[]


def test_gate_rejects_api_confirmed_nonowner_rerun_even_if_step_env_claims_owner(monkeypatch):
    repo='Micha12344f/thames-valley'; sha='a'*40; run_id='123456'; workflow_id=375591068
    def api(url,token):
        if url.endswith('/git/ref/heads/main'): return {'object':{'sha':sha}}
        if url.endswith('/actions/runs/'+run_id):
            return {'id':int(run_id),'name':'Lead deletion schema maintenance','event':'workflow_dispatch',
                'head_branch':'main','head_sha':sha,'run_attempt':1,'workflow_id':workflow_id,
                'actor':{'login':'Micha12344f'},'triggering_actor':{'login':'OtherMaintainer'}}
        raise AssertionError('Gate must reject from run metadata before further API calls')
    monkeypatch.setattr(m,'github_json',api)
    with pytest.raises(m.MaintenanceError,match='workflow_dispatch_identity_mismatch'):
        m.gate(repo,'Micha12344f','Micha12344f','workflow_dispatch',
            repo+'/.github/workflows/lead-delete-migration.yml@refs/heads/main','refs/heads/main',
            run_id,'1',sha,'token')


@pytest.mark.parametrize('action',['apply','resume'])
def test_action_rerun_by_nonowner_stops_before_checkout_lock_or_docker(action,tmp_path,monkeypatch):
    for key,value in {'GITHUB_REPOSITORY':'Micha12344f/thames-valley','GITHUB_ACTOR':'Micha12344f',
        'GITHUB_TRIGGERING_ACTOR':'OtherMaintainer','TVE_EVENT_NAME':'workflow_dispatch',
        'TVE_WORKFLOW_REF':'Micha12344f/thames-valley/.github/workflows/lead-delete-migration.yml@refs/heads/main',
        'TVE_REF':'refs/heads/main','TVE_RUN_ID':'123456','TVE_RUN_ATTEMPT':'2',
        'GITHUB_TOKEN':'fixture-token'}.items():
        monkeypatch.setenv(key,value)
    calls=[]
    monkeypatch.setattr(m,'command',lambda *args,**kwargs:calls.append(('checkout',args)))
    monkeypatch.setattr(m,'lock_host',lambda:calls.append(('lock',)))
    monkeypatch.setattr(m,'docker',lambda *args,**kwargs:calls.append(('docker',args)))
    monkeypatch.setattr(m,'github_json',lambda *args:calls.append(('api',args)))
    with pytest.raises(m.MaintenanceError,match='authorized_owner_dispatch_required'):
        m.action(SimpleNamespace(action=action,sha='a'*40))
    assert calls==[]


def test_pinned_migration_hash_and_length():
    raw=(APP/'backend/docker/init-contacts/002-lead-delete-migration.sql').read_bytes()
    assert len(raw)==449
    assert hashlib.sha256(raw).hexdigest()==m.HASH_002


def test_current_main_source_has_pinned_fresh_schema_hash():
    raw=(APP/'backend/docker/init-contacts/001-contacts.sql').read_bytes()
    assert hashlib.sha256(raw).hexdigest()==m.HASH_001


def test_database_targets_are_all_eight_distinct_stores():
    config={'services':{f'pg-{db}':{'environment':{'POSTGRES_DB':db,'POSTGRES_USER':'prod'}}
                        for db in m.JOURNEY}}
    config['services']['pg-sandbox']={'environment':{'POSTGRES_DB':'sandbox_contacts','POSTGRES_USER':'sandbox'}}
    targets=m.database_targets(config)
    assert len(targets)==8
    assert {x['database'] for x in targets if x['mode']=='sandbox'}=={f'sandbox_{x}' for x in m.JOURNEY}
    assert len({(x['container'],x['database']) for x in targets})==8


def test_schema_validator_requires_types_not_nulls_and_primary_key():
    schema={'id':{'type':'bigint','nullable':'NO'},'contacts_pk':['id'],
      'uid':{'type':'uuid','nullable':'NO','default':'gen_random_uuid()'},
      'ops':[{'name':k,'type':v[0],'nullable':v[1]} for k,v in m.OPS_TYPES.items()],
      'pk':['contact_id']}
    assert m.valid_schema(schema)
    schema['ops'][0]['nullable']='YES'
    assert not m.valid_schema(schema)
    schema['ops'][0]['nullable']='NO'; schema['pk']=['operation_id']
    assert not m.valid_schema(schema)


def test_receipt_mac_detects_tamper_and_is_bound_to_exact_base_target(tmp_path,monkeypatch):
    key=tmp_path/'key'; receipt_root=tmp_path/'receipts'
    monkeypatch.setattr(m,'KEY',key); monkeypatch.setattr(m,'RECEIPTS',receipt_root)
    monkeypatch.setattr(m,'MIGRATION_DIR',tmp_path)
    m.atomic(key,b'k'*32)
    payload={'version':1,'base_sha':'a'*40,'target_sha':'b'*40,'migration_hashes':{'001':m.HASH_001,'002':m.HASH_002}}
    payload['hmac_sha256']=m.sign_receipt(payload)
    path=receipt_root/(payload['target_sha']+'.json')
    m.atomic(path,json.dumps(payload).encode())
    loaded=json.loads(path.read_text()); mac=loaded.pop('hmac_sha256')
    assert mac==m.sign_receipt(loaded)
    loaded['base_sha']='c'*40
    assert mac!=m.sign_receipt(loaded)
    assert stat.S_IMODE(path.stat().st_mode)==0o600


def test_backup_manifest_rejects_missing_and_tampered_archives(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'STATE',tmp_path)
    with pytest.raises(m.MaintenanceError,match='incomplete'):
        m.verify_backup_set([],'a'*32)
    backup=tmp_path/'backup.dump'; backup.write_bytes(b'archive')
    manifest=[]
    for mode in ('production','sandbox'):
        for domain in m.JOURNEY:
            container='tve-pg-'+domain if mode=='production' else 'tve-pg-sandbox'
            database=domain if mode=='production' else 'sandbox_'+domain
            manifest.append({'path':f'lead-delete-migration/backups/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/{mode}-{domain}.dump',
                'sha256':hashlib.sha256(b'archive').hexdigest(),'bytes':len(b'archive'),
                'mode':mode,'domain':domain,'container':container,'database':database,'user':'u'})
    manifest[0]['sha256']='0'*64
    with pytest.raises(m.MaintenanceError,match='invalid'):
        m.verify_backup_set(manifest,'a'*32)


def test_backup_manifest_requires_exact_eight_database_targets_and_paths():
    migration_id='a'*32
    items=[]
    for mode in ('production','sandbox'):
        for domain in m.JOURNEY:
            container='tve-pg-'+domain if mode=='production' else 'tve-pg-sandbox'
            database=domain if mode=='production' else 'sandbox_'+domain
            items.append({'mode':mode,'domain':domain,'container':container,'database':database,'user':'dbuser',
                'path':f'lead-delete-migration/backups/{migration_id}/{mode}-{domain}.dump',
                'bytes':512,'sha256':'a'*64})
    m.validate_backup_manifest(items,migration_id)
    bad=[dict(item) for item in items]
    bad[-1]['container']='tve-pg-tokens'
    with pytest.raises(m.MaintenanceError,match='manifest_invalid'):
        m.validate_backup_manifest(bad,migration_id)
    bad=[dict(item) for item in items]
    bad[-1]['path']=f'lead-delete-migration/backups/{migration_id}/sandbox-settings.dump'
    with pytest.raises(m.MaintenanceError,match='manifest_invalid'):
        m.validate_backup_manifest(bad,migration_id)
    with pytest.raises(m.MaintenanceError,match='incomplete|manifest_invalid'):
        m.validate_backup_manifest(items[:-1],migration_id)


def test_receipt_and_journal_paths_are_fixed_private_state():
    source=TOOL.read_text()
    workflow=(TOOL.parents[3]/'.github/workflows/lead-delete-migration.yml').read_text()
    assert "choices=('inspect','apply','resume','verify','reconcile')" in source
    assert "flock(fp, fcntl.LOCK_EX)" in source
    assert "exec','-i',target['container']" in source
    assert 'DROP ' not in source and 'shell=True' not in source
    assert "github.actor == 'Micha12344f'" in workflow
    assert "github.triggering_actor == 'Micha12344f'" in workflow
    assert 'GITHUB_TRIGGERING_ACTOR: ${{ github.triggering_actor }}' in workflow
    assert 'options: [inspect, apply, resume, verify, reconcile]' in workflow


def test_release_baseline_drift_identifies_services_without_image_values(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'STATE',tmp_path)
    (tmp_path/'deployed-provenance.json').write_text(json.dumps({
        'version':1,'source_sha':'a'*40,
        'images':{'app-function-logic':'sha256:'+('1'*64),'imap-intake':'sha256:'+('2'*64)}}))
    class Host:
        def images(self,services):
            return {'app-function-logic':'sha256:'+('3'*64),'imap-intake':'sha256:'+('2'*64)}
    with pytest.raises(m.MaintenanceError,match='release_running_images_drift') as caught:
        m.verify_release_baseline('a'*40,Host(),('app-function-logic','imap-intake'))
    assert caught.value.services==['app-function-logic']
    assert 'sha256:' not in str(caught.value)


def test_reconcile_baseline_image_exception_allows_only_receipt_worker(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'STATE',tmp_path)
    expected={'app-function-logic':'sha256:'+('1'*64),'imap-intake':'sha256:'+('2'*64),
              'web-frontend':'sha256:'+('3'*64)}
    (tmp_path/'deployed-provenance.json').write_text(json.dumps({
        'version':1,'source_sha':'a'*40,'images':expected}))
    class Host:
        def __init__(self,actual): self.actual=actual
        def images(self,services): return self.actual
    for name in ('app-function-logic','imap-intake','web-frontend'):
        (tmp_path/'deploy-backups').mkdir(exist_ok=True)
        actual=dict(expected); actual[name]='sha256:'+('4'*64)
        host=Host(actual)
        if name=='imap-intake':
            result=m.verify_release_baseline('a'*40,host,tuple(expected),allow_image_services=('imap-intake',))
            assert result['images']==expected
        else:
            with pytest.raises(m.MaintenanceError,match='release_running_images_drift'):
                m.verify_release_baseline('a'*40,host,tuple(expected),allow_image_services=('imap-intake',))


def receipt_fixture(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'STATE',tmp_path)
    monkeypatch.setattr(m,'MIGRATION_DIR',tmp_path/'lead-delete-migration')
    monkeypatch.setattr(m,'RECEIPTS',tmp_path/'lead-delete-migration/receipts')
    monkeypatch.setattr(m,'KEY',tmp_path/'lead-delete-migration/.receipt-key')
    monkeypatch.setattr(m,'runtime_hashes',lambda:{'.env':'env-a','backend/.env':'env-b'})
    monkeypatch.setattr(m,'migration_paths',lambda base,sha:[m.SQL_002])
    schemas=[]; before=[]; backups=[]
    for mode in ('production','sandbox'):
        for i,domain in enumerate(m.JOURNEY):
            ident=f'{mode}-{domain}'
            if domain=='contacts':
                container='tve-pg-contacts' if mode=='production' else 'tve-pg-sandbox'
                database='contacts' if mode=='production' else 'sandbox_contacts'
                row={'identity_sha256':hashlib.sha256(ident.encode()).hexdigest(),'complete':True,
                     'identity':{'database':database,'system_identifier':ident},
                     'target':{'mode':mode,'domain':'contacts','container':container,'database':database}}
                schemas.append(row)
                before.append({**row,'complete':False})
            migration_id='c'*32
            path=tmp_path/'lead-delete-migration/backups'/migration_id/f'{mode}-{domain}.dump'
            path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes((ident*20).encode())
            backups.append({'path':str(path.relative_to(tmp_path)),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                            'bytes':path.stat().st_size,'mode':mode,'domain':domain,
                            'container':'tve-pg-'+domain if mode=='production' else 'tve-pg-sandbox',
                            'database':domain if mode=='production' else 'sandbox_'+domain,'user':'u'})
    return schemas,before,backups


def test_receipt_freshness_tamper_path_sql_backup_and_environment_checks(tmp_path,monkeypatch):
    schemas,before,backups=receipt_fixture(tmp_path,monkeypatch)
    receipt=m.write_receipt('a'*40,'b'*40,schemas,before,backups,
        {'app-function-logic':{'image_id':'sha256:'+'1'*64,'tag':'tve-migration-rollback:'+'a'*32+'-app-function-logic','container_id':'1'*64},
         'imap-intake':{'image_id':'sha256:'+'2'*64,'tag':'tve-migration-rollback:'+'b'*32+'-imap-intake','container_id':'2'*64}},
        {'001':m.HASH_001,'002':m.HASH_002},'Micha12344f','123456','789012','c'*32)
    assert m.verify_receipt('a'*40,'b'*40,schemas)['target_sha']=='b'*40
    with pytest.raises(m.MaintenanceError,match='stale'):
        m.verify_receipt('c'*40,'b'*40,schemas)
    monkeypatch.setattr(m,'migration_paths',lambda base,sha:[m.SQL_001,m.SQL_002])
    with pytest.raises(m.MaintenanceError,match='changed_paths'):
        m.verify_receipt('a'*40,'b'*40,schemas)
    monkeypatch.setattr(m,'migration_paths',lambda base,sha:[m.SQL_002])
    raw=json.loads(receipt.read_text()); raw['migration_hashes']['002']='0'*64
    receipt.write_text(json.dumps(raw))
    with pytest.raises(m.MaintenanceError,match='tampered'):
        m.verify_receipt('a'*40,'b'*40,schemas)
    raw['hmac_sha256']=m.sign_receipt({k:v for k,v in raw.items() if k!='hmac_sha256'})
    receipt.write_text(json.dumps(raw))
    with pytest.raises(m.MaintenanceError,match='sql_plan'):
        m.verify_receipt('a'*40,'b'*40,schemas)


def test_receipt_rejects_runtime_env_and_backup_drift(tmp_path,monkeypatch):
    schemas,before,backups=receipt_fixture(tmp_path,monkeypatch)
    receipt=m.write_receipt('a'*40,'b'*40,schemas,before,backups,{},
                            {'001':m.HASH_001,'002':m.HASH_002},'Micha12344f','123456','789012','c'*32)
    monkeypatch.setattr(m,'runtime_hashes',lambda:{'.env':'changed','backend/.env':'env-b'})
    with pytest.raises(m.MaintenanceError,match='runtime_env_drift'):
        m.verify_receipt('a'*40,'b'*40,schemas)
    monkeypatch.setattr(m,'runtime_hashes',lambda:{'.env':'env-a','backend/.env':'env-b'})
    (tmp_path/backups[0]['path']).write_bytes(b'tampered')
    with pytest.raises(m.MaintenanceError,match='backup_tampered'):
        m.verify_receipt('a'*40,'b'*40,schemas)


def test_reattest_accepts_only_same_base_and_unchanged_sql_plan(tmp_path,monkeypatch,capsys):
    receipts=tmp_path/'receipts'; receipts.mkdir()
    monkeypatch.setattr(m,'RECEIPTS',receipts)
    base,old_sha,new_sha='a'*40,'b'*40,'c'*40
    paths=[m.SQL_002]
    env_snapshot=tmp_path/'backups'/('d'*32)
    env_snapshot.mkdir(parents=True)
    env_snapshot.chmod(0o700)
    app_env=b'app-env\n'; backend_env=b'backend-env\n'
    secret_line=b'TVE_LEAD_DELETE_SECRET='+b'x'*64+b'\n'
    live_app=tmp_path/'app'; (live_app/'backend').mkdir(parents=True)
    (live_app/'.env').write_bytes(app_env); (live_app/'backend/.env').write_bytes(backend_env+secret_line)
    (live_app/'.env').chmod(0o600); (live_app/'backend/.env').chmod(0o600)
    (env_snapshot/'.env.previous').write_bytes(app_env)
    (env_snapshot/'backend_.env.previous').write_bytes(backend_env)
    (env_snapshot/'.env.previous').chmod(0o600)
    (env_snapshot/'backend_.env.previous').chmod(0o600)
    monkeypatch.setattr(m,'BACKUPS',tmp_path/'backups')
    monkeypatch.setattr(m,'APP',live_app)
    original={'version':1,'base_sha':base,'target_sha':old_sha,'migration_paths':paths,
        'migration_hashes':{'001':m.HASH_001,'002':m.HASH_002},'migration_id':'d'*32,
        'backups':[{'sha256':'e'*64}],'schema_before':[],'databases':[],
        'rollback_images':{'app-function-logic':{},'imap-intake':{}},
        'runtime_env_sha256':{'.env':hashlib.sha256(app_env).hexdigest(),
                              'backend/.env':hashlib.sha256(backend_env+secret_line).hexdigest()},'actor':'Micha12344f',
        'workflow_run_id':'111','ci_run_id':'222','created_at_epoch':1}
    latest_sha='d'*40; latest={**original,'target_sha':latest_sha,'created_at_epoch':2}
    original['runtime_env_sha256']={**original['runtime_env_sha256'],'backend/.env':'0'*64}
    (receipts/(old_sha+'.json')).write_text(json.dumps(original))
    (receipts/(latest_sha+'.json')).write_text(json.dumps(latest))
    monkeypatch.setattr(m,'migration_paths',lambda b,s:paths if b==base else [m.SQL_001,m.SQL_002])
    monkeypatch.setattr(m,'migration_blobs',lambda sha:(b'001',b'002'))
    monkeypatch.setattr(m,'verify_receipt',lambda b,s,rows,**kwargs:dict(latest if s==latest_sha else original))
    verified_backups=[]
    monkeypatch.setattr(m,'verify_backup_set',lambda items,mid:verified_backups.append((items,mid)))
    monkeypatch.setattr(m,'verify_rollback_image_records',lambda images:images)
    monkeypatch.setattr(m,'sign_receipt',lambda payload:'signed-hmac')
    m.reattest_existing_migration(base,new_sha,[], 'Micha12344f','333','444')
    assert latest_sha in capsys.readouterr().out
    renewed=json.loads((receipts/(new_sha+'.json')).read_text())
    assert renewed['target_sha']==new_sha and renewed['base_sha']==base
    assert renewed['workflow_run_id']=='333' and renewed['ci_run_id']=='444'
    assert renewed['hmac_sha256']=='signed-hmac' and verified_backups

    (receipts/(new_sha+'.json')).unlink()
    monkeypatch.setattr(m,'migration_paths',lambda b,s:[m.SQL_001,m.SQL_002])
    with pytest.raises(m.MaintenanceError,match='completed_migration_plan_changed'):
        m.reattest_existing_migration(base,new_sha,[], 'Micha12344f','333','444')

    monkeypatch.setattr(m,'migration_paths',lambda b,s:paths)
    monkeypatch.setattr(m,'verify_backup_set',lambda *args:(_ for _ in ()).throw(m.MaintenanceError('backup_archive_invalid')))
    with pytest.raises(m.MaintenanceError,match='backup_archive_invalid'):
        m.reattest_existing_migration(base,new_sha,[], 'Micha12344f','333','444')
    with pytest.raises(m.MaintenanceError,match='previous_migration_receipt_required'):
        m.reattest_existing_migration('f'*40,new_sha,[], 'Micha12344f','333','444')


def test_fixed_sql_plan_applies_one_transaction_per_database_and_stops_on_partial_failure(monkeypatch):
    calls=[]
    targets=[{'container':'tve-pg-contacts','user':'u','database':'contacts','mode':'production','domain':'contacts'},
             {'container':'tve-pg-sandbox','user':'u','database':'sandbox_contacts','mode':'sandbox','domain':'contacts'}]
    monkeypatch.setattr(m,'database_targets',lambda config:targets)
    sql=(APP/'backend/docker/init-contacts/002-lead-delete-migration.sql').read_bytes()
    monkeypatch.setattr(m,'git_blob',lambda sha,path:sql)
    def fake_command(args,**kwargs):
        calls.append((args,kwargs['input_data']))
        if len(calls)==2: raise m.MaintenanceError('injected partial migration failure')
        return b''
    monkeypatch.setattr(m,'command',fake_command)
    monkeypatch.setattr(m,'inspect_db',lambda target:{'complete':True})
    with pytest.raises(m.MaintenanceError,match='partial'):
        m.apply_migration({},'b'*40)
    assert len(calls)==2
    assert all('-v' in args and 'ON_ERROR_STOP=1' in args for args,_ in calls)
    assert all(sql.startswith(b'BEGIN;\n') and sql.endswith(b'\nCOMMIT;\n') for _,sql in calls)


def test_recovery_rejects_runtime_env_changes(tmp_path,monkeypatch):
    app=tmp_path/'repo/app'; backup=(tmp_path/'lead-delete-migration/backups')/('a'*32)
    (app/'backend').mkdir(parents=True)
    backup.mkdir(parents=True)
    monkeypatch.setattr(m,'APP',app)
    monkeypatch.setattr(m,'STATE',tmp_path)
    monkeypatch.setattr(m,'BACKUPS',tmp_path/'lead-delete-migration/backups')
    env=b'PORT=3002\n'; backend=b'TVE_WEBHOOK_SECRET=bootstrap-secret\n'
    (app/'.env').write_bytes(env); (app/'backend/.env').write_bytes(backend)
    (backup/'.env.previous').write_bytes(env); (backup/'backend_.env.previous').write_bytes(backend)
    journal={'migration_id':'a'*32,'runtime_env_before_sha256':{
        '.env':hashlib.sha256(env).hexdigest(),'backend/.env':hashlib.sha256(backend).hexdigest()}}
    m.verify_journal_env(journal)
    (app/'backend/.env').write_bytes(backend+b'TVE_LEAD_DELETE_SECRET='+b'x'*64+b'\n')
    with pytest.raises(m.MaintenanceError,match='recovery_environment_file_drift'):
        m.verify_journal_env(journal)
    (app/'backend/.env').write_bytes(backend+b'ANOTHER_KEY=unapproved\n')
    with pytest.raises(m.MaintenanceError,match='recovery_environment_file_drift'):
        m.verify_journal_env(journal)


@pytest.mark.skipif(not os.environ.get('TVE_TEST_PG_BIN'),
                    reason='set TVE_TEST_PG_BIN to run isolated PostgreSQL archive integration')
def test_real_postgres_archive_stream_detects_truncation_and_restores(tmp_path):
    bindir=Path(os.environ['TVE_TEST_PG_BIN'])
    initdb=bindir/'initdb'; pg_ctl=bindir/'pg_ctl'; createdb=bindir/'createdb'
    psql=bindir/'psql'; pg_dump=bindir/'pg_dump'; pg_restore=bindir/'pg_restore'
    assert all(path.is_file() for path in (initdb,pg_ctl,createdb,psql,pg_dump,pg_restore))
    env=os.environ.copy()
    env['PATH']=str(bindir)+os.pathsep+env.get('PATH','')
    data=tmp_path/'data'; sockets=tmp_path/'socket'; sockets.mkdir()
    log=tmp_path/'postgres.log'
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]
    user='archive_test'
    subprocess.run([str(initdb),'-D',str(data),'-U',user,'-A','trust','--no-locale','-E','UTF8'],
                   env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True,timeout=120)
    started=False
    try:
        subprocess.run([str(pg_ctl),'-D',str(data),'-l',str(log),'-o',
                        f'-h 127.0.0.1 -k {sockets} -p {port}','-w','start'],
                       env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True,timeout=120)
        started=True
        conn=['-h','127.0.0.1','-p',str(port),'-U',user]
        subprocess.run([str(createdb),*conn,'journey'],env=env,check=True,timeout=30)
        subprocess.run([str(psql),*conn,'-d','journey','-v','ON_ERROR_STOP=1','-c',
                        "CREATE TABLE sample (id integer primary key, value text); INSERT INTO sample VALUES (1, repeat('x', 200000));"],
                       env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True,timeout=30)
        archive=tmp_path/'journey.dump'
        with archive.open('wb') as stream:
            subprocess.run([str(pg_dump),*conn,'-d','journey','--format=custom','--no-owner'],
                           env=env,stdout=stream,stderr=subprocess.PIPE,check=True,timeout=60)
        m.validate_archive_stream(archive,[str(pg_restore)])
        corrupt=tmp_path/'truncated.dump'
        blob=archive.read_bytes()
        corrupt.write_bytes(blob[:len(blob)//2])
        with pytest.raises(m.MaintenanceError,match='archive_invalid'):
            m.validate_archive_stream(corrupt,[str(pg_restore)])
        subprocess.run([str(createdb),*conn,'journey_restore'],env=env,check=True,timeout=30)
        subprocess.run([str(pg_restore),'--exit-on-error','--no-owner','--no-privileges',
                        '-h','127.0.0.1','-p',str(port),'-U',user,'-d','journey_restore',str(archive)],
                       env=env,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,check=True,timeout=60)
        result=subprocess.run([str(psql),*conn,'-d','journey_restore','-A','-t','-c',
                               'SELECT length(value) FROM sample WHERE id=1;'],
                              env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True,timeout=30)
        assert result.stdout.strip()==b'200000'
    finally:
        if started:
            subprocess.run([str(pg_ctl),'-D',str(data),'-m','fast','-w','stop'],env=env,
                           stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,check=True,timeout=60)


def backup_manifest(state,migration_id,config):
    result=[]
    for target in m.database_targets(config):
        path=m.BACKUPS/migration_id/(target['mode']+'-'+target['domain']+'.dump')
        path.parent.mkdir(parents=True,exist_ok=True)
        data=(target['mode']+'-'+target['domain']+'-safe fixture').encode()*20
        path.write_bytes(data)
        result.append({'path':str(path.relative_to(state)),'sha256':hashlib.sha256(data).hexdigest(),
                       'bytes':len(data),**target})
    return result


def schema_row(mode,*,complete):
    database='contacts' if mode=='production' else 'sandbox_contacts'
    container='tve-pg-contacts' if mode=='production' else 'tve-pg-sandbox'
    identity={'database':database,'system_identifier':'system-'+mode,'container_id':mode*64,
              'volume':'volume-'+mode,'server':'local'}
    return {'mode':mode,'domain':'contacts','container':container,'database':database,
        'identity':identity,'identity_sha256':hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
        'complete':complete,'target':{'mode':mode,'domain':'contacts','container':container,'database':database}}


def orchestration_fixture(tmp_path,monkeypatch,*,stopped=False):
    live=tmp_path/'live'; app=live/m.APP_REL
    (app/'backend').mkdir(parents=True)
    (app/'.env').write_text('DASHBOARD_HOST_PORT=3002\n')
    (app/'backend/.env').write_text('TVE_WEBHOOK_SECRET=bootstrap-secret\n')
    (app/'backend/.env').chmod(0o600)
    state=tmp_path/'state'; migration_dir=state/'lead-delete-migration'
    for key,value in {'STATE':state,'LIVE':live,'APP':app,'LOCK':state/'deploy.lock',
        'MIGRATION_DIR':migration_dir,'BACKUPS':migration_dir/'backups',
        'RECEIPTS':migration_dir/'receipts','JOURNAL':migration_dir/'transaction.json',
        'KEY':migration_dir/'.receipt-key'}.items():
        monkeypatch.setattr(m,key,value)
    target='a'*40; base='b'*40
    config={'name':'tve-questionnaire-booking','services':{
        'app-function-logic':{'environment':{'TVE_WEBHOOK_SECRET':'bootstrap-secret','sandbox':'false'}},
        'imap-intake':{'environment':{'sandbox':'false'}},
        **{f'pg-{db}':{'environment':{'POSTGRES_DB':db,'POSTGRES_USER':'prod'}} for db in m.JOURNEY},
        'pg-sandbox':{'environment':{'POSTGRES_DB':'sandbox_contacts','POSTGRES_USER':'sandbox'}},
        'pg-settings':{'environment':{'POSTGRES_DB':'settings','POSTGRES_USER':'prod'}},
        'web-frontend':{},'mock-inbox':{}}}
    writer_images={
        'app-function-logic':{'image_id':'sha256:'+'1'*64,'tag':'tve-migration-rollback:'+'a'*32+'-app-function-logic','container_id':'a'*64},
        'imap-intake':{'image_id':'sha256:'+'2'*64,'tag':'tve-migration-rollback:'+'b'*32+'-imap-intake','container_id':'b'*64}}
    container_state={service:{'Id':record['container_id'],'Image':record['image_id'],
        'State':{'Running':not stopped,'Status':'exited' if stopped else 'running'},
        'Config':{'Env':['TVE_WEBHOOK_SECRET=bootstrap-secret']}}
        for service,record in writer_images.items()}
    tag_images={record['tag']:record['image_id'] for record in writer_images.values()}
    commands=[]; host_calls=[]; compose_overrides=[]

    class FakeHost:
        def config(self,root):
            host_calls.append(('config',root)); return config
        def check_config(self,cfg,root): host_calls.append(('check_config',root))
        def running_matches(self,cfg,*,allow_stopped=(),allow_environment_drift=None):
            host_calls.append(('running_matches',set(allow_stopped)))
            if getattr(self,'drift',False): raise m.MaintenanceError('injected_dependency_drift')
            for service,info in container_state.items():
                if not info['State']['Running'] and service not in allow_stopped:
                    raise m.MaintenanceError('unexpected_stopped_writer')
        def inspect(self,service): return container_state[service]
        def images(self,services):
            return {service:writer_images.get(service,{'image_id':'sha256:'+'3'*64})['image_id'] for service in services}
        def health(self,root,cfg): host_calls.append(('health',root))

    host=FakeHost()
    fake_deployer=SimpleNamespace(Host=lambda live_arg,commands_arg:host,Commands=lambda:None,
        SERVICES=('app-function-logic','imap-intake','web-frontend'))
    monkeypatch.setattr(m,'gate',lambda *args:987654321)
    monkeypatch.setattr(m,'command',lambda args,**kwargs:(target+'\n').encode())
    monkeypatch.setattr(m,'migration_blobs',lambda sha:((APP/'backend/docker/init-contacts/001-contacts.sql').read_bytes(),
        (APP/'backend/docker/init-contacts/002-lead-delete-migration.sql').read_bytes()))
    monkeypatch.setattr(m,'lock_host',lambda:SimpleNamespace(close=lambda:None))
    monkeypatch.setattr(m,'deployed_marker',lambda:base)
    monkeypatch.setattr(m,'load_deployer',lambda:fake_deployer)
    monkeypatch.setattr(m,'verify_release_baseline',lambda *args,**kwargs:{'images':{
        service:record['image_id'] for service,record in writer_images.items()}})
    monkeypatch.setattr(m,'live_mode',lambda cfg:None)
    monkeypatch.setattr(m,'images_for',lambda services:writer_images)
    monkeypatch.setattr(m,'dynamic_security_state',lambda:{'mode':'production'})
    monkeypatch.setattr(m,'require_backup_capacity',lambda cfg:None)
    monkeypatch.setattr(m,'archive_targets',lambda cfg,migration_id:backup_manifest(state,migration_id,cfg))
    monkeypatch.setattr(m,'validate_archive_stream',lambda *args:None)
    monkeypatch.setattr(m,'apply_migration',lambda cfg,sha:commands.append(('apply_sql',sha)))
    monkeypatch.setattr(m,'write_receipt',lambda *args:commands.append(('receipt',*args[:2])))
    monkeypatch.setattr(m,'runtime_hashes',lambda:{'.env':'app-hash','backend/.env':'backend-hash'})
    pre=[schema_row('production',complete=False),schema_row('sandbox',complete=False)]
    post=[schema_row('production',complete=True),schema_row('sandbox',complete=True)]
    schema_calls={'count':0,'rows':[pre,pre,post]}
    def schema_set(cfg):
        index=schema_calls['count']; schema_calls['count']+=1
        return schema_calls['rows'][min(index,len(schema_calls['rows'])-1)]
    monkeypatch.setattr(m,'schema_set',schema_set)

    def fake_docker(*args,capture=False,input_data=None,timeout=300):
        commands.append(tuple(args))
        if args[0:2]==('image','inspect'):
            return (tag_images.get(args[-1],'').encode()+b'\n') if capture else b''
        if args[0:2]==('container','inspect'):
            service=args[2].removeprefix('tve-')
            return json.dumps([container_state[service]]).encode() if capture else b''
        if args[0]=='compose':
            up_index=args.index('up'); override=Path(args[up_index-1])
            payload=json.loads(override.read_text()); compose_overrides.append(payload)
            for service,definition in payload['services'].items():
                info=container_state[service]
                info.update(Id=(uuid.uuid4().hex*2)[:64],Image=definition['image'])
                info['State']={'Running':True,'Status':'running'}
            return b''
        if args[0]=='stop':
            info=container_state[args[-1].removeprefix('tve-')]
            info['State']={'Running':False,'Status':'exited'}
        return b''
    monkeypatch.setattr(m,'docker',fake_docker)
    for key,value in {'GITHUB_REPOSITORY':'Micha12344f/thames-valley','GITHUB_ACTOR':'Micha12344f',
                      'GITHUB_TRIGGERING_ACTOR':'Micha12344f','TVE_EVENT_NAME':'workflow_dispatch',
                      'TVE_WORKFLOW_REF':'Micha12344f/thames-valley/.github/workflows/lead-delete-migration.yml@refs/heads/main',
                      'TVE_REF':'refs/heads/main','TVE_RUN_ID':'123456','TVE_RUN_ATTEMPT':'1',
                      'GITHUB_TOKEN':'fixture-token','GITHUB_RUN_ID':'123456'}.items():
        monkeypatch.setenv(key,value)
    return SimpleNamespace(state=state,live=live,app=app,target=target,base=base,config=config,host=host,
        writer_images=writer_images,container_state=container_state,commands=commands,host_calls=host_calls,
        compose_overrides=compose_overrides,schema_calls=schema_calls,
        run=lambda action:m.action(SimpleNamespace(action=action,sha=target)),set_schema=lambda rows:schema_calls.update(count=0,rows=rows))


def test_full_apply_orchestration_uses_repo_root_and_immutable_writer_images(tmp_path,monkeypatch):
    f=orchestration_fixture(tmp_path,monkeypatch)
    monkeypatch.setattr(m,'migration_paths',lambda base,target:[m.SQL_002])
    f.run('apply')
    assert not m.JOURNAL.exists()
    assert ('config',f.live) in f.host_calls and ('config',f.app) not in f.host_calls
    assert ('check_config',f.live) in f.host_calls and ('health',f.live) in f.host_calls
    assert len(f.compose_overrides)==1
    used={service['image'] for service in f.compose_overrides[0]['services'].values()}
    assert used=={record['image_id'] for record in f.writer_images.values()}
    assert used.isdisjoint({record['tag'] for record in f.writer_images.values()})
    assert sum(call[0]=='apply_sql' for call in f.commands)==1
    assert sum(call[0]=='receipt' for call in f.commands)==1


def test_writer_restart_refuses_mutated_rollback_tag_before_compose(tmp_path,monkeypatch):
    f=orchestration_fixture(tmp_path,monkeypatch)
    f.writer_images['app-function-logic']['tag']='tve-migration-rollback:'+'c'*32+'-app-function-logic'
    with pytest.raises(m.MaintenanceError,match='rollback_image_tag_drift'):
        m.start_writers(f.writer_images,f.host,f.config)
    assert not any(call and call[0]=='compose' for call in f.commands)


def test_full_resume_checks_dependency_drift_before_mutation(tmp_path,monkeypatch):
    f=orchestration_fixture(tmp_path,monkeypatch,stopped=True)
    migration_id='e'*32
    backups=backup_manifest(f.state,migration_id,f.config)
    env_hashes={name:hashlib.sha256((f.app/name).read_bytes()).hexdigest() for name in ('.env','backend/.env')}
    before=[schema_row('production',complete=False),schema_row('sandbox',complete=False)]
    journal={'version':1,'state':'backed_up','migration_id':migration_id,'base_sha':f.base,
        'target_sha':f.target,'sql_002_sha256':m.HASH_002,'rollback_images':f.writer_images,
        'allowed_stopped_writers':['app-function-logic','imap-intake'],
        'writer_container_ids':{s:r['container_id'] for s,r in f.writer_images.items()},
        'backups':backups,'schema_before':before,'runtime_env_before_sha256':env_hashes}
    m.private_dir(m.BACKUPS/migration_id); m.private_dir(m.MIGRATION_DIR)
    for name in ('.env','backend/.env'):
        m.atomic(m.BACKUPS/migration_id/(name.replace('/','_')+'.previous'),(f.app/name).read_bytes())
    m.atomic(m.JOURNAL,json.dumps(journal).encode())
    f.host.drift=True
    with pytest.raises(m.MaintenanceError,match='injected_dependency_drift'):
        f.run('resume')
    assert not any(call and call[0] in ('compose','stop','apply_sql','receipt') for call in f.commands)
    assert m.JOURNAL.is_file()


def test_full_resume_reconciles_exact_backups_schema_and_writer_images(tmp_path,monkeypatch):
    f=orchestration_fixture(tmp_path,monkeypatch,stopped=True)
    migration_id='e'*32
    backups=backup_manifest(f.state,migration_id,f.config)
    env_hashes={name:hashlib.sha256((f.app/name).read_bytes()).hexdigest() for name in ('.env','backend/.env')}
    before=[schema_row('production',complete=False),schema_row('sandbox',complete=False)]
    journal={'version':1,'state':'backed_up','migration_id':migration_id,'base_sha':f.base,
        'target_sha':f.target,'sql_002_sha256':m.HASH_002,'rollback_images':f.writer_images,
        'allowed_stopped_writers':['app-function-logic','imap-intake'],
        'writer_container_ids':{s:r['container_id'] for s,r in f.writer_images.items()},
        'backups':backups,'schema_before':before,'runtime_env_before_sha256':env_hashes}
    m.private_dir(m.BACKUPS/migration_id); m.private_dir(m.MIGRATION_DIR)
    for name in ('.env','backend/.env'):
        m.atomic(m.BACKUPS/migration_id/(name.replace('/','_')+'.previous'),(f.app/name).read_bytes())
    m.atomic(m.JOURNAL,json.dumps(journal).encode())
    f.run('resume')
    assert not m.JOURNAL.exists()
    assert sum(call[0]=='apply_sql' for call in f.commands)==1
    assert sum(call[0]=='receipt' for call in f.commands)==1
    assert len(f.compose_overrides)==2
    for payload in f.compose_overrides:
        assert {entry['image'] for entry in payload['services'].values()}=={
            row['image_id'] for row in f.writer_images.values()}
    assert ('config',f.live) in f.host_calls and ('config',f.app) not in f.host_calls
    assert ('check_config',f.live) in f.host_calls and ('health',f.live) in f.host_calls


def test_full_resume_rejects_contacts_system_identity_drift_before_restart(tmp_path,monkeypatch):
    f=orchestration_fixture(tmp_path,monkeypatch,stopped=True)
    migration_id='f'*32
    backups=backup_manifest(f.state,migration_id,f.config)
    env_hashes={name:hashlib.sha256((f.app/name).read_bytes()).hexdigest() for name in ('.env','backend/.env')}
    before=[schema_row('production',complete=False),schema_row('sandbox',complete=False)]
    journal={'version':1,'state':'backed_up','migration_id':migration_id,'base_sha':f.base,
        'target_sha':f.target,'sql_002_sha256':m.HASH_002,'rollback_images':f.writer_images,
        'allowed_stopped_writers':['app-function-logic','imap-intake'],
        'writer_container_ids':{s:r['container_id'] for s,r in f.writer_images.items()},
        'backups':backups,'schema_before':before,'runtime_env_before_sha256':env_hashes}
    m.private_dir(m.BACKUPS/migration_id); m.private_dir(m.MIGRATION_DIR)
    for name in ('.env','backend/.env'):
        m.atomic(m.BACKUPS/migration_id/(name.replace('/','_')+'.previous'),(f.app/name).read_bytes())
    m.atomic(m.JOURNAL,json.dumps(journal).encode())
    changed=[schema_row('production',complete=False),schema_row('sandbox',complete=False)]
    changed[0]['identity']['system_identifier']='unexpected-system'
    changed[0]['identity_sha256']=hashlib.sha256(json.dumps(changed[0]['identity'],sort_keys=True).encode()).hexdigest()
    f.set_schema([changed,changed])
    with pytest.raises(m.MaintenanceError,match='system_identity_drift'):
        f.run('resume')
    assert not any(call and call[0] in ('compose','stop','apply_sql','receipt') for call in f.commands)
    assert m.JOURNAL.is_file()
