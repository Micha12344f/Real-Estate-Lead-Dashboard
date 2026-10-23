"""Offline release scenarios: temporary host tree and fake Git/Docker/HTTP only."""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import urllib.error

import pytest

spec = importlib.util.spec_from_file_location('deploy_tve', Path(__file__).with_name('deploy_tve.py'))
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


def test_environment_drift_diagnostics_report_names_not_values():
    expected = {'TVE_EMAIL_INTAKE': 'imap', 'sandbox': 'false'}
    actual = {'TVE_EMAIL_INTAKE': 'webhook', 'sandbox': 'true'}
    names = deploy.environment_mismatch_keys(expected, actual)
    rendered = json.dumps({'keys': names})
    assert names == ['TVE_EMAIL_INTAKE', 'sandbox']
    assert 'imap' not in rendered
    assert 'webhook' not in rendered


def test_environment_drift_exception_is_exactly_scoped():
    host=deploy.Host(Path('/unused'),None)
    with pytest.raises(deploy.DeployError,match='invalid_environment_drift_exception'):
        host.running_matches({'services':{}},allow_environment_drift={
            'app-function-logic':{'TVE_WEBHOOK_SECRET'}})


def test_health_probe_retains_only_fixed_check_name(monkeypatch):
    host=deploy.Host(Path('/unused'),None)
    def fail(*args,**kwargs):
        raise deploy.DeployError('health_response_rejected')
    monkeypatch.setattr(host,'get',fail)
    cfg={'services':{'app-function-logic':{'environment':{'TVE_WEBHOOK_SECRET':'must-not-log'}}}}
    with pytest.raises(deploy.DeployError,match='health_response_rejected') as caught:
        host.health(Path('/unused'),cfg)
    assert caught.value.health_check=='api_healthz'
    assert 'must-not-log' not in str(caught.value)


def test_delete_health_failure_retains_only_expected_and_observed_status(monkeypatch):
    deployment=deploy.Deployment.__new__(deploy.Deployment)
    class Host:
        def health(self,root,config):
            error=deploy.DeployError('delete_probe_handler_not_reached')
            error.health_check='delete_api_handler'
            error.observed_status=405
            error.expected_status=403
            error.detail_match=False
            raise error
    deployment.host=Host()
    deployment.live=Path('/unused')
    ticks=iter((0,200))
    monkeypatch.setattr(deploy.time,'monotonic',lambda:next(ticks))
    with pytest.raises(deploy.DeployError,match='release_health_failed') as caught:
        deployment.wait_healthy(Path('/unused'),verify_delete_endpoint=True)
    assert caught.value.health_check=='delete_api_handler'
    assert caught.value.observed_status==405 and caught.value.expected_status==403
    assert caught.value.detail_match is False
    assert 'response body' not in str(caught.value)


OLD = '1' * 40
NEW = '2' * 40
LATER = '3' * 40
PREFIX = deploy.APP + '/'
OLD_IMAGE = 'sha256:' + 'a' * 64
NEW_IMAGE = 'sha256:' + 'b' * 64


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Short host paths keep the complete release tree portable on Windows.
    return tmp_path_factory.mktemp('tve')


def manifest(files):
    return {name: (hashlib.sha1(data).hexdigest(), 0o644) for name, data in files.items()}


class FakeSource:
    def __init__(self, versions):
        self.versions = versions
        self.requested = []

    def manifest(self, sha):
        self.requested.append(sha)
        return manifest(self.versions[sha])

    def materialize(self, entries, destination):
        objects = {hashlib.sha1(data).hexdigest(): data for files in self.versions.values() for data in files.values()}
        for name, (blob, mode) in entries.items():
            path = destination / name
            path.parent.mkdir(parents=True, exist_ok=True)
            deploy.atomic_write(path, objects[blob], mode)


class FakeHost:
    """Model built images, partial rollout, health and source observed by containers."""
    def __init__(self, live):
        self.live = live
        self.calls = []
        self.current_images = {service: OLD_IMAGE for service in deploy.SERVICES}
        self.fail_build = self.fail_rollout = self.fail_health = False
        self.fail_copy = False
        self.config_value = {'services': {'app-function-logic': {'environment': {'TVE_WEBHOOK_SECRET': 'private-runtime-secret'}}}}

    def config(self, root):
        self.calls.append(('config', root))
        # Resolve the staged bytes rather than reading checkout/untrusted env.
        assert (root / PREFIX / '.env').read_bytes() == b'APP_FUNCTION_LOGIC_PORT=5000\n'
        assert (root / PREFIX / 'backend/.env').read_bytes() == b'sandbox=FALSE\nTVE_WEBHOOK_SECRET=private-runtime-secret\n'
        return self.config_value

    def check_config(self, config, root):
        self.calls.append(('check_config', root))

    def compatible(self, old, new, release):
        self.calls.append(('compatible', release))

    def running_matches(self, config):
        self.calls.append(('running_matches',))

    def health(self, root, config):
        self.calls.append(('health',))
        if self.fail_health and NEW_IMAGE in self.current_images.values():
            raise deploy.DeployError('fixture_health_failed')

    def images(self, services):
        return {service: self.current_images[service] for service in services}

    def tags(self, images, sha, transaction):
        self.calls.append(('tag_old_images', dict(images)))
        return {service: 'old-' + service for service in images}

    def compose(self, root, *args, **kwargs):
        assert args[0] == 'build'
        self.calls.append(('build', args[1:], root))
        if self.fail_build:
            raise deploy.DeployError('fixture_build_failed')

    def rollout(self, root, services, override, env_root=None):
        if not services:
            return
        rollback = override.name == 'rollback-images.json'
        self.calls.append(('rollback' if rollback else 'up', tuple(services)))
        tags = json.loads(override.read_bytes())['services']
        assert set(tags) == set(services)
        for service in services:
            self.current_images[service] = OLD_IMAGE if rollback else NEW_IMAGE
        if rollback:
            assert env_root is not None
        elif self.fail_rollout:
            raise InterruptedError('fixture_cancel_after_partial_up')


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy.time, 'sleep', lambda seconds: None)
    state = tmp_path / 'host'
    live = state / 'repo'
    live.mkdir(parents=True)
    old = {
        'README.md': b'old docs',
        PREFIX + 'frontend/dashboard/index.html': b'<html>old dashboard</html>',
        PREFIX + 'frontend/dashboard/styles.css': b'old css',
        PREFIX + 'frontend/dashboard/app.js': b'old js',
        PREFIX + 'frontend/dashboard/assets/meesix-mark.svg': b'<svg>old mark</svg>',
        PREFIX + 'backend/app/main.py': b'old backend',
        PREFIX + deploy.COMPOSE: b'old compose',
        PREFIX + '.env.example': b'EXAMPLE_ONLY=true',
    }
    for name, data in old.items():
        path = live / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (live / PREFIX / '.env').write_bytes(b'APP_FUNCTION_LOGIC_PORT=5000\n')
    (live / PREFIX / 'backend/.env').write_bytes(b'sandbox=FALSE\nTVE_WEBHOOK_SECRET=private-runtime-secret\n')
    (live / 'operator-notes.txt').write_bytes(b'unfamiliar file must survive')
    (state / 'deployed-commit').write_text(OLD + '\n')
    (state / 'deployed-provenance.json').write_bytes(b'{"previous":"metadata"}')
    host = FakeHost(live)

    def run(new, latest=None, versions=None):
        source = FakeSource(versions or {OLD: old, NEW: new})
        transaction = deploy.Deployment(source, host, state, NEW, latest or (lambda: NEW), ci_run_id=123)
        transaction.run()
        return source

    return state, live, old, host, run


@pytest.mark.parametrize(('paths', 'services'), [
    (['frontend/dashboard/app.js'], ['web-frontend']),
    (['frontend/dashboard/styles.css', 'frontend/dashboard/assets/meesix-mark.svg'], ['web-frontend']),
    (['frontend/dashboard/index.html', 'frontend/serve-local.js', 'frontend/.dockerignore', 'frontend/Dockerfile'], ['web-frontend']),
    (['frontend/booking/api/confirm.js'], ['web-frontend']),
    (['backend/app/main.py'], list(deploy.SERVICES[:2])),
    (['backend/requirements.txt', 'backend/docker/Dockerfile', 'Tools/fixtures/properties.json'], list(deploy.SERVICES[:2])),
    ([deploy.COMPOSE], list(deploy.SERVICES)),
    (['README.md', 'backend/docker/README.md', 'frontend/dashboard/README.md'], []),
    (['Tools/deploy_tve.py', 'Tools/test_deploy_tve.py'], []),
])
def test_service_selection_for_operational_changes(paths, services):
    assert deploy.classify([PREFIX + path for path in paths]) == services


def test_ui_release_rebuilds_only_frontend_and_preserves_runtime_and_data(sandbox, capsys):
    state, live, old, host, run = sandbox
    new = {**old, PREFIX + 'frontend/dashboard/styles.css': b'new visible css',
           PREFIX + '.env.example': b'TVE_WEBHOOK_SECRET=do-not-use-template'}
    run(new)
    assert (live / PREFIX / 'frontend/dashboard/styles.css').read_bytes() == b'new visible css'
    assert ('up', ('web-frontend',)) in host.calls
    assert not any(call[0] == 'rollback' for call in host.calls)
    assert [call[1] for call in host.calls if call[0] == 'build'] == [('web-frontend',)]
    assert host.current_images['imap-intake'] == OLD_IMAGE
    assert (live / PREFIX / 'backend/.env').read_bytes().endswith(b'private-runtime-secret\n')
    assert (live / 'operator-notes.txt').read_bytes() == b'unfamiliar file must survive'
    assert (state / 'deployed-commit').read_text().strip() == NEW
    metadata = json.loads((state / 'deployed-provenance.json').read_bytes())
    assert metadata['source_sha'] == NEW and metadata['ci_run_id'] == 123
    assert metadata['services'] == ['web-frontend']
    assert metadata['images']['web-frontend'] == NEW_IMAGE
    assert 'private-runtime-secret' not in json.dumps(metadata) + capsys.readouterr().out
    assert host.calls.index(('health',)) < next(i for i, call in enumerate(host.calls) if call[0] == 'build')
    if os.name != 'nt':
        for path in (state / 'deploy-backups').glob('*'):
            assert path.stat().st_mode & 0o777 == 0o700
        for path in (state / 'deploy-backups').glob('*/previous-source/**/*'):
            if path.is_file():
                assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(('relative', 'services'), [
    ('backend/app/main.py', deploy.SERVICES[:2]),
    (deploy.COMPOSE, deploy.SERVICES),
    ('README.md', ()),
    ('Tools/deploy_tve.py', ()),
])
def test_release_uses_service_classification_and_docs_never_restart(sandbox, relative, services):
    state, live, old, host, run = sandbox
    run({**old, PREFIX + relative: b'new tracked content'})
    assert [call[1] for call in host.calls if call[0] == 'up'] == ([services] if services else [])
    assert any(call[0] == 'health' for call in host.calls)
    assert (state / 'deployed-commit').read_text().strip() == NEW


@pytest.mark.parametrize('failure', ['fail_build', 'fail_rollout', 'fail_health'])
def test_failed_release_restores_deleted_source_images_config_and_markers(sandbox, failure):
    state, live, old, host, run = sandbox
    new = {**old, PREFIX + 'frontend/dashboard/app.js': b'new js',
           PREFIX + 'frontend/dashboard/assets/new.svg': b'new asset'}
    del new[PREFIX + 'frontend/dashboard/styles.css']
    setattr(host, failure, True)
    with pytest.raises((deploy.DeployError, InterruptedError)):
        run(new)
    for name, data in old.items():
        assert (live / name).read_bytes() == data
    assert not (live / PREFIX / 'frontend/dashboard/assets/new.svg').exists()
    assert host.current_images == {service: OLD_IMAGE for service in deploy.SERVICES}
    assert (state / 'deployed-commit').read_text().strip() == OLD
    assert (state / 'deployed-provenance.json').read_bytes() == b'{"previous":"metadata"}'
    assert (live / 'operator-notes.txt').exists()
    assert ('rollback', ('web-frontend',)) in host.calls if failure != 'fail_build' else not any(c[0] == 'up' for c in host.calls)


def test_partial_copy_failure_restores_actual_previous_bytes(sandbox, monkeypatch):
    state, live, old, host, run = sandbox
    # Back up actual known-good source/config, not a reconstruction from Git.
    (live / PREFIX / 'backend/app/main.py').write_bytes(b'prior host source drift')
    original = deploy.atomic_write
    failed = False

    def failing_write(path, data, mode=0o600):
        nonlocal failed
        if path == live / PREFIX / 'frontend/dashboard/app.js' and data == b'new js' and not failed:
            failed = True
            raise OSError('fixture partial copy failure')
        original(path, data, mode)

    monkeypatch.setattr(deploy, 'atomic_write', failing_write)
    with pytest.raises(OSError):
        run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js', 'README.md': b'new docs'})
    assert (live / 'README.md').read_bytes() == b'old docs'
    assert (live / PREFIX / 'backend/app/main.py').read_bytes() == b'prior host source drift'
    assert (state / 'deployed-commit').read_text().strip() == OLD
    assert not any(call[0] == 'up' for call in host.calls)


@pytest.mark.parametrize('stale_at', [1, 2, 3])
def test_stale_main_is_successful_skip_before_docker_or_after_build_or_copy(sandbox, stale_at):
    state, live, old, host, run = sandbox
    calls = 0

    def latest():
        nonlocal calls
        calls += 1
        return LATER if calls >= stale_at else NEW

    with pytest.raises(deploy.StaleRelease):
        run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js'}, latest)
    assert (state / 'deployed-commit').read_text().strip() == OLD
    assert (live / PREFIX / 'frontend/dashboard/app.js').read_bytes() == b'old js'
    assert not any(call[0] == 'up' for call in host.calls)
    if stale_at == 1:
        assert host.calls == []
        assert not (state / 'deploy-backups').exists()


def test_marker_baseline_is_reread_when_another_release_was_deployed(sandbox):
    state, live, old, host, run = sandbox
    intermediate = {**old, PREFIX + 'backend/app/main.py': b'intermediate backend'}
    (state / 'deployed-commit').write_text(LATER + '\n')
    (live / PREFIX / 'backend/app/main.py').write_bytes(b'intermediate backend')
    final = {**intermediate, PREFIX + 'frontend/dashboard/app.js': b'latest frontend'}
    source = run(final, versions={OLD: old, LATER: intermediate, NEW: final})
    assert source.requested == [LATER, NEW]
    assert ('up', ('web-frontend',)) in host.calls
    assert not any(call[0] == 'up' and 'imap-intake' in call[1] for call in host.calls)


def test_already_released_sha_has_no_docker_side_effects(sandbox):
    state, live, old, host, run = sandbox
    (state / 'deployed-commit').write_text(NEW + '\n')
    run(old)
    assert host.calls == []
    assert not (state / 'deploy-backups').exists()


def test_unhealthy_baseline_stops_before_image_build_or_source_copy(sandbox):
    state, live, old, host, run = sandbox

    def health(*args):
        raise deploy.DeployError('fixture_previous_release_unhealthy')

    host.health = health
    with pytest.raises(deploy.DeployError, match='previous_release_unhealthy'):
        run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js'})
    assert not any(call[0] in ('build', 'up', 'tag_old_images') for call in host.calls)
    assert (state / 'deployed-commit').read_text().strip() == OLD
    assert (live / PREFIX / 'frontend/dashboard/app.js').read_bytes() == b'old js'


def test_marker_is_not_advanced_until_all_health_checks_pass(sandbox):
    state, live, old, host, run = sandbox
    original = host.health

    def health(root, config):
        assert (state / 'deployed-commit').read_text().strip() == OLD
        assert (state / 'deployed-provenance.json').read_bytes() == b'{"previous":"metadata"}'
        original(root, config)

    host.health = health
    run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js'})
    assert (state / 'deployed-commit').read_text().strip() == NEW


def test_release_record_write_failure_rolls_back_healthy_rollout(sandbox, monkeypatch):
    state, live, old, host, run = sandbox
    original = deploy.atomic_write
    failed = False

    def write(path, data, mode=0o600):
        nonlocal failed
        if path == state / 'deployed-commit' and data == (NEW + '\n').encode() and not failed:
            failed = True
            raise OSError('fixture marker write failed')
        original(path, data, mode)

    monkeypatch.setattr(deploy, 'atomic_write', write)
    with pytest.raises(OSError):
        run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js'})
    assert ('rollback', ('web-frontend',)) in host.calls
    assert (state / 'deployed-commit').read_text().strip() == OLD
    assert (state / 'deployed-provenance.json').read_bytes() == b'{"previous":"metadata"}'
    assert (live / PREFIX / 'frontend/dashboard/app.js').read_bytes() == b'old js'


def test_interrupt_during_health_enters_rollback_immediately(sandbox):
    state, live, old, host, run = sandbox
    original = host.health
    interrupted = 0

    def health(root, config):
        nonlocal interrupted
        if NEW_IMAGE in host.current_images.values():
            interrupted += 1
            raise InterruptedError('fixture TERM during health')
        original(root, config)

    host.health = health
    with pytest.raises(InterruptedError):
        run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js'})
    assert interrupted == 1
    assert ('rollback', ('web-frontend',)) in host.calls
    assert (state / 'deployed-commit').read_text().strip() == OLD
    assert (live / PREFIX / 'frontend/dashboard/app.js').read_bytes() == b'old js'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX source permissions')
def test_private_umask_does_not_change_restored_source_modes(sandbox):
    state, live, old, host, run = sandbox
    source = live / PREFIX / 'frontend/dashboard/app.js'
    source.chmod(0o640)
    host.fail_rollout = True
    mask = os.umask(0o077)
    try:
        with pytest.raises(InterruptedError):
            run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js'})
    finally:
        os.umask(mask)
    assert source.stat().st_mode & 0o777 == 0o640


def test_unexpected_runtime_env_edit_aborts_without_restoring_or_rotating_secrets(sandbox):
    state, live, old, host, run = sandbox
    original = host.compose

    def build(*args, **kwargs):
        original(*args, **kwargs)
        (live / PREFIX / 'backend/.env').write_bytes(b'operator_changed_key=preserve-this-edit')

    host.compose = build
    with pytest.raises(deploy.DeployError, match='runtime_environment_changed'):
        run({**old, PREFIX + 'frontend/dashboard/app.js': b'new js'})
    assert not any(call[0] == 'up' for call in host.calls)
    assert (live / PREFIX / 'backend/.env').read_bytes() == b'operator_changed_key=preserve-this-edit'
    assert (state / 'deployed-commit').read_text().strip() == OLD


def test_unfamiliar_collision_blocks_release_without_deleting_file(sandbox):
    state, live, old, host, run = sandbox
    # A new manifest path that exists as a directory (not a file) must block
    # the release. New manifest additions that pre-exist as files are accepted.
    new_path = PREFIX + 'backend/docker/init-sandbox/001-databases.sh'
    collision = live / new_path
    collision.parent.mkdir(parents=True, exist_ok=True)
    collision.mkdir()  # Directory where a file is expected
    with pytest.raises(deploy.DeployError, match='unfamiliar_live_file_collision'):
        run({**old, new_path: b'#!/bin/sh\n'})
    assert (state / 'deployed-commit').read_text().strip() == OLD


def test_new_manifest_file_preexisting_on_disk_is_accepted(sandbox):
    state, live, old, host, run = sandbox
    # A file that is new in the release but already exists on disk from operator
    # provisioning (e.g. sandbox init scripts) is accepted and overwritten.
    new_file = PREFIX + 'backend/docker/init-sandbox/001-databases.sh'
    (live / new_file).parent.mkdir(parents=True, exist_ok=True)
    (live / new_file).write_bytes(b'operator-provisioned version')
    run({**old, new_file: b'release version'})
    assert (live / new_file).read_bytes() == b'release version'
    assert (state / 'deployed-commit').read_text().strip() == NEW


@pytest.mark.parametrize('path', ['backend/docker/init-settings/002-destructive.sql',
                                 'backend/docker/init/002-change.sql',
                                 'backend/app/migrations/destructive.py'])
def test_database_initialization_change_requires_migration_before_any_docker(sandbox, path):
    state, live, old, host, run = sandbox
    with pytest.raises(deploy.DeployError, match='explicit_migration'):
        run({**old, PREFIX + path: b'DROP TABLE settings'})
    assert host.calls == []
    assert (state / 'deployed-commit').read_text().strip() == OLD


def test_sandbox_init_files_are_classified_as_app_source_not_blocked():
    # The sandbox init scripts only initialize the new, separately-provisioned
    # sandbox database; production init files remain migration-gated.
    paths = [
        PREFIX + 'backend/docker/init-sandbox/001-databases.sh',
        PREFIX + 'backend/docker/init-sandbox/002-submissions.sql',
    ]
    assert deploy.classify(paths) == ['app-function-logic', 'imap-intake']
    with pytest.raises(deploy.DeployError, match='explicit_migration'):
        deploy.classify([PREFIX + 'backend/docker/init-contacts/002-extra.sql'])


def test_lead_delete_schema_classifier_only_allows_exact_pinned_files():
    one = PREFIX + 'backend/docker/init-contacts/001-contacts.sql'
    two = PREFIX + 'backend/docker/init-contacts/002-lead-delete-migration.sql'
    assert deploy.lead_delete_schema_candidate({one, two})
    for path in (PREFIX + 'backend/docker/init-contacts/003-extra.sql',
                 PREFIX + 'backend/docker/init-settings/002-settings.sql',
                 PREFIX + 'backend/app/migrations/new.py',
                 PREFIX + 'backend/docker/init-contacts/other.sh'):
        with pytest.raises(deploy.DeployError, match='explicit_migration'):
            deploy.lead_delete_schema_candidate({two, path})


def test_classifier_requires_complete_exact_migration_proof():
    one=PREFIX+'backend/docker/init-contacts/001-contacts.sql'
    two=PREFIX+'backend/docker/init-contacts/002-lead-delete-migration.sql'
    changed={one,two}
    proof=deploy.VerifiedLeadDeleteMigration(OLD,NEW,sorted(changed))
    assert deploy.classify(changed,verified_migration=proof)==['app-function-logic','imap-intake']
    incomplete=deploy.VerifiedLeadDeleteMigration(OLD,NEW,[two])
    with pytest.raises(deploy.DeployError,match='explicit_migration'):
        deploy.classify(changed,verified_migration=incomplete)


def test_deploy_delete_endpoint_probe_reaches_authenticated_handler_before_db(tmp_path, monkeypatch):
    import io
    import urllib.error

    class Response:
        status = 200
        def __init__(self, body): self.body = body
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, limit=-1): return self.body

    calls = []
    class Opener:
        def open(self, request, timeout=10):
            calls.append(request)
            if request.full_url.endswith('/dashboard/settings/mode'):
                return Response(b'{"sandbox":false}')
            if request.full_url.endswith('/sandbox/api/settings/mode'):
                return Response(b'{"sandbox":false}')
            if request.full_url.endswith('/openapi.json'):
                return Response(b'{"paths":{"/dashboard/leads/{contact_id}":{"delete":{}}}}')
            err=urllib.error.HTTPError(request.full_url,422,'Unprocessable Entity',{},io.BytesIO(b'{"error":{"code":"request_rejected","message":"Deletion operation reference required","requestId":"synthetic"}}'))
            raise err

    monkeypatch.setattr(deploy.urllib.request,'build_opener',lambda *a:Opener())
    host=deploy.Host(tmp_path,deploy.Commands())
    host.verify_delete_endpoint({'services':{'app-function-logic':{'environment':{'TVE_WEBHOOK_SECRET':'proxy-secret'}}}})
    deletes=[r for r in calls if r.get_method()=='DELETE']
    assert len(deletes)==2
    assert all(r.full_url.endswith('/dashboard/leads/9223372036854775000')
               or r.full_url.endswith('/sandbox/api/leads/9223372036854775000') for r in deletes)
    assert deletes[0].headers.get('X-tve-auth')=='proxy-secret'
    assert deletes[1].headers.get('X-tve-auth') is None
    assert all(r.headers.get('X-tve-delete-operation') is None for r in deletes)
    assert all(r.headers.get('X-tve-delete-auth') is None for r in deletes)


def test_deploy_delete_endpoint_sandbox_probe_stops_at_handler_validation(tmp_path, monkeypatch):
    import io
    import urllib.error

    class Response:
        status=200
        def __init__(self,body): self.body=body
        def __enter__(self): return self
        def __exit__(self,*args): return False
        def read(self,limit=-1): return self.body
    calls=[]
    class Opener:
        def open(self,request,timeout=10):
            calls.append(request)
            if request.full_url.endswith('/dashboard/settings/mode') or request.full_url.endswith('/sandbox/api/settings/mode'):
                return Response(b'{"sandbox":true}')
            if request.full_url.endswith('/openapi.json'):
                return Response(b'{"paths":{"/dashboard/leads/{contact_id}":{"delete":{}}}}')
            error=urllib.error.HTTPError(request.full_url,422,'Unprocessable Entity',{},
                io.BytesIO(b'{"error":{"code":"request_rejected","message":"Deletion operation reference required","requestId":"synthetic"}}'))
            raise error
    monkeypatch.setattr(deploy.urllib.request,'build_opener',lambda *a:Opener())
    host=deploy.Host(tmp_path,deploy.Commands())
    host.verify_delete_endpoint({'services':{'app-function-logic':{'environment':{'TVE_WEBHOOK_SECRET':'proxy-secret'}}}})
    deletes=[r for r in calls if r.get_method()=='DELETE']
    assert len(deletes)==2
    assert all(r.headers.get('X-tve-mode')=='sandbox' for r in deletes)
    assert all(r.headers.get('X-tve-delete-operation') is None for r in deletes)
    assert all(r.headers.get('X-tve-delete-auth') is None for r in deletes)


def trusted_event():
    return {'repository': {'full_name': 'owner/repo'}, 'workflow_run': {
        'id': 123, 'name': 'Application CI', 'conclusion': 'success', 'event': 'push',
        'head_branch': 'main', 'head_sha': NEW, 'head_repository': {'full_name': 'owner/repo'}}}


def test_exact_tested_sha_validation_rejects_pr_fork_failed_ci_and_mutable_refs():
    env = {'GITHUB_EVENT_NAME': 'workflow_run', 'GITHUB_REPOSITORY': 'owner/repo', 'TESTED_SHA': NEW}
    assert deploy.validate_event(trusted_event(), env) == (NEW, 'owner/repo')
    for key, value in [('conclusion', 'failure'), ('conclusion', 'cancelled'), ('event', 'pull_request'), ('event', 'workflow_dispatch'),
                       ('head_branch', 'topic'), ('head_sha', OLD), ('head_repository', {'full_name': 'fork/repo'})]:
        event = trusted_event()
        event['workflow_run'][key] = value
        with pytest.raises(deploy.DeployError):
            deploy.validate_event(event, env)
    for sha in ('main', 'refs/heads/main', NEW + ';echo injected', ''):
        with pytest.raises(deploy.DeployError):
            deploy.validate_event(trusted_event(), {**env, 'TESTED_SHA': sha})


def test_github_current_main_reads_authenticated_ref_and_rejects_non_sha(monkeypatch, capsys):
    import io
    calls = []
    value = NEW

    class Opener:
        def open(self, request, timeout):
            calls.append(request)
            return io.BytesIO(json.dumps({'object': {'sha': value}}).encode())

    monkeypatch.setattr(deploy.urllib.request, 'build_opener', lambda *args: Opener())
    assert deploy.current_main('owner/repo', 'private-github-token') == NEW
    assert calls[0].full_url == 'https://api.github.com/repos/owner/repo/git/ref/heads/main'
    assert calls[0].get_header('Authorization') == 'Bearer private-github-token'
    value = 'main'
    with pytest.raises(deploy.DeployError, match='invalid_commit_sha'):
        deploy.current_main('owner/repo', 'private-github-token')
    assert capsys.readouterr().out == ''


def test_stale_top_level_exit_is_successful_skip_without_health_failure(tmp_path, monkeypatch, capsys):
    import contextlib
    import signal
    event = tmp_path / 'event.json'
    event.write_text(json.dumps(trusted_event()))
    for key, value in {'GITHUB_EVENT_PATH': str(event), 'GITHUB_EVENT_NAME': 'workflow_run',
                       'GITHUB_REPOSITORY': 'owner/repo', 'TESTED_SHA': NEW,
                       'GITHUB_WORKSPACE': str(tmp_path), 'GH_TOKEN': 'private-token'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(deploy, 'LIVE', tmp_path)
    monkeypatch.setattr(deploy, 'STATE', tmp_path)
    monkeypatch.setattr(deploy, 'GitSource', lambda *args: type('Source', (), {'commands': FakeCommands()})())
    monkeypatch.setattr(deploy, 'host_lock', lambda state: contextlib.nullcontext())

    class Stale:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            raise deploy.StaleRelease()

    monkeypatch.setattr(deploy, 'Deployment', Stale)
    handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    mask = os.umask(0o077)
    try:
        assert deploy.main() == 0
    finally:
        os.umask(mask)
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
    output = capsys.readouterr().out
    assert json.loads(output) == {'event': 'skip_stale_successful_sha'}
    assert 'private-token' not in output and 'failed' not in output


class FakeCommands:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        return self.responses.get(tuple(args), b'')


def test_checkout_must_match_ci_and_object_materialization_never_uses_worktree_bytes(tmp_path):
    prefix = ['git', '-C', str(tmp_path)]
    commands = FakeCommands({tuple(prefix + ['rev-parse', 'HEAD']): (OLD + '\n').encode()})
    with pytest.raises(deploy.DeployError, match='checkout_not_tested_sha'):
        deploy.GitSource(tmp_path, NEW, commands)
    commands.responses[tuple(prefix + ['rev-parse', 'HEAD'])] = (NEW + '\n').encode()
    commands.responses[tuple(prefix + ['ls-tree', '-rz', NEW])] = b'100644 blob abc\tREADME.md\0'
    commands.responses[tuple(prefix + ['cat-file', 'blob', 'abc'])] = b'tested Git object bytes'
    source = deploy.GitSource(tmp_path, NEW, commands)
    (tmp_path / 'README.md').write_bytes(b'locally altered worktree')
    destination = tmp_path / 'release'
    destination.mkdir()
    source.materialize(source.manifest(NEW), destination)
    assert (destination / 'README.md').read_bytes() == b'tested Git object bytes'
    assert not any('main' in args for args, kwargs in commands.calls)


def test_compose_env_priority_no_build_rollout_and_no_volume_lifecycle(tmp_path):
    commands = FakeCommands()
    host = deploy.Host(tmp_path, commands)
    host.rollout(tmp_path, list(deploy.SERVICES), tmp_path / 'images.json')
    args, kwargs = commands.calls[0]
    files = [args[index + 1] for index, arg in enumerate(args) if arg == '--env-file']
    assert files == [str(tmp_path / PREFIX / 'backend/.env'), str(tmp_path / PREFIX / '.env')]
    # Relative env_file/../../build-context paths in the compose file resolve
    # against the compose file's parent, not the application root.
    expected_project = str(tmp_path / PREFIX / 'backend/docker')
    assert args[args.index('--project-directory') + 1] == expected_project
    assert args[:3] == ['docker', '--host', 'unix:///var/run/docker.sock']
    assert '--no-deps' in args and '--no-build' in args and args[args.index('--pull') + 1] == 'never'
    assert not {'down', 'rm', '--volumes', '--remove-orphans', 'server_deploy'} & set(args)
    assert kwargs['capture'] is False
    env = deploy.Commands().env
    assert 'TVE_WEBHOOK_SECRET' not in env and 'POSTGRES_PASSWORD' not in env and 'SANDBOX' not in env


def test_candidate_cannot_change_database_config_secrets_mounts_or_networks(tmp_path):
    host = deploy.Host(tmp_path / 'live', FakeCommands())
    old = {'name': 'tve-questionnaire-booking', 'volumes': {}, 'networks': {},
           'services': {service: {'environment': {'SECRET': 'keep'}, 'build': {'context': 'old'}}
                        for service in deploy.SERVICES + ('pg-settings',)}}
    new = copy.deepcopy(old)
    new['services']['web-frontend']['build']['context'] = 'new'
    host.compatible(old, new, tmp_path / 'release')
    for service, key, value in [('pg-settings', 'image', 'postgres:99'),
                                ('app-function-logic', 'environment', {'SECRET': 'rotated'}),
                                ('imap-intake', 'volumes', []), ('web-frontend', 'ports', [])]:
        bad = copy.deepcopy(new)
        bad['services'][service][key] = value
        with pytest.raises(deploy.DeployError):
            host.compatible(old, bad, tmp_path / 'release')


def minimal_compose():
    config = {'name': 'tve-questionnaire-booking', 'volumes': {key: {'name': name} for key, name in deploy.VOLUMES.items()},
              'services': {service: {} for service in deploy.SERVICES + deploy.DATABASES + ('mock-inbox',)}}
    for service in deploy.SERVICES[:2]:
        config['services'][service]['environment'] = {'sandbox': 'FALSE', 'TVE_WEBHOOK_SECRET': 'private-key', 'TVE_AUTH_DISABLED': 'false'}
    config['services']['web-frontend']['environment'] = {'TVE_WEBHOOK_SECRET': 'private-key'}
    config['services']['app-function-logic']['ports'] = [{'host_ip': '127.0.0.1', 'published': '5000', 'target': 8000}]
    config['services']['web-frontend']['ports'] = [
        {'host_ip': '127.0.0.1', 'published': str(published), 'target': target}
        for published, target in ((13000, 3000), (13001, 3001), (3002, 3002))]
    return config


def add_sandbox_service(config):
    config['services']['pg-sandbox'] = {
        'container_name': 'tve-pg-sandbox', 'image': 'postgres:15-alpine',
        'environment': {'POSTGRES_USER': 'sandbox-user', 'POSTGRES_PASSWORD': 'private-test-only'},
        'networks': {'net-sandbox': None},
        'volumes': [{'source': 'pg_sandbox_data', 'target': '/var/lib/postgresql/data',
                     'type': 'volume', 'read_only': False}],
    }
    config['volumes']['pg_sandbox_data'] = {'name': 'tve-pg-sandbox-data'}
    config['networks'] = {'net-sandbox': {'name': 'tve-net-sandbox'}}
    for service in deploy.SERVICES[:2]:
        definition = config['services'][service]
        definition.setdefault('environment', {}).update({
            'TVE_SANDBOX_DB_' + name.upper():
                f'postgresql://sandbox-user:private-test-only@pg-sandbox:5432/sandbox_{name}'
            for name in deploy.SANDBOX_DATABASES})
        definition['networks'] = {'net-sandbox': None}
        definition['depends_on'] = {'pg-sandbox': {'condition': 'service_healthy'}}
    return config


def test_missing_persistent_volume_never_creates_or_recreates_database(tmp_path):
    commands = FakeCommands()
    host = deploy.Host(tmp_path, commands)
    original = host.docker

    def docker(*args, **kwargs):
        if args == ('volume', 'inspect', 'tve-pg-settings-data'):
            raise deploy.DeployError('missing_existing_volume')
        return original(*args, **kwargs)

    host.docker = docker
    with pytest.raises(deploy.DeployError, match='missing_existing_volume'):
        host.check_config(minimal_compose(), tmp_path)
    assert commands.calls
    assert all(args[3:5] == ['volume', 'inspect'] for args, kwargs in commands.calls)


def test_operator_provisioned_sandbox_is_validated_but_never_created(tmp_path):
    commands = FakeCommands()
    host = deploy.Host(tmp_path, commands)
    config = minimal_compose()
    add_sandbox_service(config)
    host.inspect = lambda service: {
        'State': {'Running': True, 'Health': {'Status': 'healthy'}},
        'Config': {'Env': ['POSTGRES_USER=sandbox-user', 'POSTGRES_PASSWORD=private-test-only']},
        'Mounts': [{'Destination': '/var/lib/postgresql/data', 'Type': 'volume',
                    'Name': 'tve-pg-sandbox-data', 'RW': True}],
        'NetworkSettings': {'Networks': {'tve-net-sandbox': {}}},
    }
    host.check_config(config, tmp_path)
    assert any(args[3:] == ['volume', 'inspect', 'tve-pg-sandbox-data'] for args, kwargs in commands.calls)
    assert all(args[3:5] == ['volume', 'inspect'] for args, kwargs in commands.calls)
    before = minimal_compose()
    host.compatible(before, config, tmp_path / 'release')
    assert not any(args[3:5] == ['volume', 'create'] for args, kwargs in commands.calls)


def test_sandbox_service_not_preprovisioned_blocks_release(tmp_path):
    commands = FakeCommands()
    host = deploy.Host(tmp_path, commands)
    config = minimal_compose()
    add_sandbox_service(config)
    host.inspect = lambda service: {'State': {'Running': False}}
    with pytest.raises(deploy.DeployError, match='sandbox_database_not_preprovisioned_healthy'):
        host.check_config(config, tmp_path)


@pytest.mark.parametrize('bad', ['ports', 'sandbox', 'secret', 'extra_volume'])
def test_unexpected_runtime_configuration_stops_before_rollout(tmp_path, bad):
    host = deploy.Host(tmp_path, FakeCommands())
    config = minimal_compose()
    host.check_config(config, tmp_path)
    if bad == 'ports':
        config['services']['app-function-logic']['ports'][0]['host_ip'] = '0.0.0.0'
    elif bad == 'sandbox':
        config['services']['imap-intake']['environment']['sandbox'] = 'TRUE'
    elif bad == 'secret':
        config['services']['web-frontend']['environment']['TVE_WEBHOOK_SECRET'] = 'mismatched-key'
    else:
        config['volumes']['unexpected'] = {'name': 'new-empty-database'}
    with pytest.raises(deploy.DeployError):
        host.check_config(config, tmp_path)
    assert not any('up' in args for args, kwargs in host.commands.calls)


def test_health_uses_only_readonly_local_routes_and_checks_real_assets(sandbox, monkeypatch):
    state, live, old, fake, run = sandbox
    host = deploy.Host(live, FakeCommands())
    calls = []

    def get(opener, url, secret=None, json_body=False):
        calls.append((url, secret))
        if url.endswith('/dashboard/settings') and secret is None:
            raise urllib.error.HTTPError(url, 401, 'unauthorized', {}, None)
        if not json_body:
            relative = url.removeprefix('http://127.0.0.1:3002/')
            return (live / PREFIX / 'frontend/dashboard' / relative).read_bytes()
        return b'{}'

    monkeypatch.setattr(host, 'get', get)
    monkeypatch.setattr(host, 'inspect', lambda service: {'State': {'Running': True, 'StartedAt': 'stable'}, 'RestartCount': 0})
    host.health(live, fake.config_value)
    assert len(calls) == 13
    assert all(url.startswith(('http://127.0.0.1:5000/', 'http://127.0.0.1:3002/')) for url, secret in calls)
    assert {url.rsplit('/', 1)[-1] for url, secret in calls} == {
        'healthz', 'settings', 'leads', 'funnel', 'index.html', 'styles.css', 'app.js', 'meesix-mark.svg'}
    assert all(secret is None for url, secret in calls if '/sandbox/api/' in url)
    args = host.commands.calls[0][0]
    assert args[3:7] == ['exec', 'tve-imap-intake', 'python', '-B']
    assert 'checkpoint.json' in args[-1] and 'write' not in args[-1]


def test_runtime_env_and_symlinks_are_not_overlay_targets(tmp_path):
    with pytest.raises(deploy.DeployError, match='runtime_env_must_not_be_tracked'):
        deploy.Overlay(tmp_path, tmp_path, tmp_path, {}, {PREFIX + 'backend/.env': ('blob', 0o644)})
    with pytest.raises(deploy.DeployError, match='unsafe_tracked_path'):
        deploy.safe_path(tmp_path, '../outside')
    if os.name != 'nt':
        (tmp_path / 'alias').symlink_to(tmp_path, target_is_directory=True)
        with pytest.raises(deploy.DeployError, match='symlink'):
            deploy.safe_path(tmp_path, 'alias/file')


@pytest.mark.skipif(os.name == 'nt', reason='Production flock is a Linux host contract')
def test_host_flock_blocks_manual_overlap_and_unfinished_transactions(tmp_path):
    import subprocess
    import sys
    deploy.private_dir(tmp_path / 'deploy-backups')
    code = ('import fcntl,sys; f=open(sys.argv[1],"a"); '
            'fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)')
    with deploy.host_lock(tmp_path):
        child = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'deploy.lock')], capture_output=True)
        assert child.returncode != 0
    child = subprocess.run([sys.executable, '-c', code, str(tmp_path / 'deploy.lock')], capture_output=True)
    assert child.returncode == 0
    interrupted = tmp_path / 'deploy-backups' / 'interrupted'
    deploy.private_dir(interrupted)
    deploy.atomic_write(interrupted / 'transaction.json', b'{"status":"rolling_out"}')
    with pytest.raises(deploy.DeployError, match='incomplete_previous_transaction'):
        with deploy.host_lock(tmp_path):
            pytest.fail('An interrupted transaction must block the next release')
