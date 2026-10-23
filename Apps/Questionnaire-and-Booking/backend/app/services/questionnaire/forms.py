"""Validated presentation configuration; scoring keys and answer values stay stable."""
import base64
import copy
import json
from pathlib import Path
from urllib.parse import urlparse

from ..shared import settings_store

KEY = 'TVE_FORMS_CONFIG'
DEFAULTS = json.loads(Path(__file__).with_name('form_defaults.json').read_text())


def validate(data):
    if not isinstance(data, dict) or set(data) != {'logo_url', 'questions'}:
        raise ValueError('Provide a logo and questionnaire questions.')
    logo = data['logo_url']
    if not isinstance(logo, str) or len(logo) > 350000:
        raise ValueError('Logo must be an HTTPS URL or an image up to 250 KB.')
    if logo.startswith('data:'):
        try:
            header, encoded = logo.split(',', 1)
            raw = base64.b64decode(encoded, validate=True)
            valid = (header == 'data:image/png;base64' and raw.startswith(b'\x89PNG\r\n\x1a\n') or
                     header == 'data:image/jpeg;base64' and raw.startswith(b'\xff\xd8\xff') or
                     header == 'data:image/webp;base64' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP')
            if not valid or len(raw) > 250 * 1024:
                raise ValueError()
        except Exception:
            raise ValueError('Upload a valid PNG, JPEG or WebP image up to 250 KB.') from None
    elif logo:
        parsed = urlparse(logo)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or len(logo) > 2048 or any(c.isspace() for c in logo):
            raise ValueError('Use a public HTTPS image URL.')
    questions = data['questions']
    if not isinstance(questions, list) or len(questions) != len(DEFAULTS['questions']):
        raise ValueError('All workflow questions must be retained.')
    # Fields used by scoring, identity validation and automation are immutable.
    defaults_by_id = {question['id']: question for question in DEFAULTS['questions']}
    result = {'logo_url': logo, 'questions': []}
    seen_ids = set()
    for source in questions:
        if not isinstance(source, dict) or source.get('id') not in defaults_by_id or source['id'] in seen_ids:
            raise ValueError('Question order and workflow identifiers must be retained.')
        target = copy.deepcopy(defaults_by_id[source['id']])
        seen_ids.add(source['id'])
        allowed = set(target) | {'subtitle', 'placeholder'}
        if set(source) - allowed:
            raise ValueError('Unknown question field.')
        for key, value in target.items():
            if key not in {'label', 'subtitle', 'placeholder', 'options'} and source.get(key) != value:
                raise ValueError('Workflow question types and validation cannot be changed.')
        for key in ('label', 'subtitle', 'placeholder'):
            value = source.get(key, '')
            if not isinstance(value, str) or len(value) > (300 if key == 'label' else 500) or (key == 'label' and not value.strip()):
                raise ValueError('Question text is required (300 characters); help text is limited to 500 characters.')
            target[key] = value.strip()
        if 'options' in target:
            options = source.get('options')
            if not isinstance(options, list) or len(options) != len(target['options']):
                raise ValueError('Keep the existing answer choices.')
            for option, original in zip(options, target['options']):
                if not isinstance(option, dict) or set(option) != {'label', 'value'} or option.get('value') != original['value']:
                    raise ValueError('Answer values used by the workflow cannot be changed.')
                label = option.get('label')
                if not isinstance(label, str) or not label.strip() or len(label) > 150:
                    raise ValueError('Answer labels must contain 1–150 characters.')
                original['label'] = label.strip()
        result['questions'].append(target)
    return result


async def read():
    value = await settings_store.get(KEY)
    return validate(json.loads(value)) if value else copy.deepcopy(DEFAULTS)


async def save(data):
    validated = validate(data)
    await settings_store.set(KEY, value=json.dumps(validated, separators=(',', ':')), group='forms', is_secret=False, updated_via='dashboard')
    return validated
