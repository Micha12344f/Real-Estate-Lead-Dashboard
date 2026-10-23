"""Form copy remains editable without changing qualification semantics."""
import copy
import json

import pytest

from app.services.questionnaire import forms
from app.services.shared import settings_store
from test_settings_api import vault, AUTH


def test_roundtrip_and_public_projection(vault):
    client, pool, _ = vault
    original = client.get('/dashboard/forms', headers=AUTH)
    assert original.status_code == 200
    assert original.headers['cache-control'] == 'no-store'
    data = original.json()
    data['questions'][4]['label'] = 'When would you like to move in?'
    data['questions'][4]['options'][0]['label'] = 'In the next week'
    data['logo_url'] = 'https://example.com/logo.png'
    saved = client.put('/dashboard/forms', headers=AUTH, json=data)
    assert saved.status_code == 200
    assert saved.json()['questions'][4]['label'] == data['questions'][4]['label']
    assert saved.json()['questions'][4]['options'][0]['value'] == 'Within 1 week'
    assert data['questions'][4]['label'].encode() not in pool.conn.rows[forms.KEY]['ciphertext']
    public = client.get('/webhook/thames-valley-estates/forms-config', headers=AUTH)
    assert public.json() == saved.json()
    assert client.get('/dashboard/forms', headers=AUTH).json() == saved.json()


def test_question_order_is_editable_without_changing_workflow_ids(vault):
    client, _, _ = vault
    data = copy.deepcopy(forms.DEFAULTS)
    data['questions'][4], data['questions'][5] = data['questions'][5], data['questions'][4]
    response = client.put('/dashboard/forms', headers=AUTH, json=data)
    assert response.status_code == 200
    assert [question['id'] for question in response.json()['questions'][:6]] == [
        'guest_name', 'guest_email', 'guest_phone', 'guest_property_address', 'monthly_budget', 'move_timing'
    ]
    public = client.get('/webhook/thames-valley-estates/forms-config', headers=AUTH)
    assert [question['id'] for question in public.json()['questions'][:6]] == [
        'guest_name', 'guest_email', 'guest_phone', 'guest_property_address', 'monthly_budget', 'move_timing'
    ]


def test_all_routes_require_auth(vault):
    client, _, _ = vault
    assert client.get('/dashboard/forms').status_code == 401
    assert client.put('/dashboard/forms', json=forms.DEFAULTS).status_code == 401
    assert client.get('/webhook/thames-valley-estates/forms-config').status_code == 401
    assert client.put('/webhook/thames-valley-estates/forms-config', headers=AUTH, json=forms.DEFAULTS).status_code == 405


@pytest.mark.parametrize('mutation', [
    lambda d: d['questions'][0].update(id=d['questions'][1]['id']),
    lambda d: d['questions'][4].update(label=' '),
    lambda d: d['questions'][4].update(required=False),
    lambda d: d['questions'][4].update(type='text'),
    lambda d: d['questions'][4]['options'][0].update(value='new-scoring-value'),
    lambda d: d['questions'][4]['options'].pop(),
    lambda d: d['questions'].pop(),
    lambda d: d.update(logo_url='javascript:alert(1)'),
    lambda d: d.update(logo_url='data:image/svg+xml;base64,PHN2Zz4='),
    lambda d: d.update(logo_url='data:image/png;base64,aGVsbG8='),
    lambda d: d.update(logo_url='https://user:password@example.com/image.png'),
])
def test_invalid_edits_rejected(vault, mutation):
    client, _, _ = vault
    data = copy.deepcopy(forms.DEFAULTS)
    mutation(data)
    assert client.put('/dashboard/forms', headers=AUTH, json=data).status_code == 422
    assert client.get('/dashboard/forms', headers=AUTH).json() == forms.DEFAULTS


def test_uploaded_logo_supported(vault):
    client, _, _ = vault
    data = copy.deepcopy(forms.DEFAULTS)
    data['logo_url'] = 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aXioAAAAASUVORK5CYII='
    assert client.put('/dashboard/forms', headers=AUTH, json=data).status_code == 200


def test_vault_validation_applies_to_generic_settings_write(vault):
    client, _, _ = vault
    data = copy.deepcopy(forms.DEFAULTS)
    data['questions'][4]['required'] = False
    response = client.put('/dashboard/settings/' + forms.KEY, headers=AUTH, json={'value': json.dumps(data)})
    assert response.status_code == 422


def test_settings_failure_is_not_a_false_success(vault, monkeypatch):
    client, _, _ = vault
    async def unavailable(*args, **kwargs):
        raise RuntimeError('unavailable')
    monkeypatch.setattr(settings_store, 'set', unavailable)
    assert client.put('/dashboard/forms', headers=AUTH, json=forms.DEFAULTS).status_code == 503
