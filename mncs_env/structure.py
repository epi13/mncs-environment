"""Read owner-declared MNCDS structure expectations; never infer anatomy."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .projection_sources import confined
from .capabilities import DOCTOR_PROJECTION_HEALTH
from .identity import digest_hex


def inspect_structure(session, checkout):
    manifest = json.loads((checkout / '.mncs/project.json').read_text())
    binding = manifest.get('structure_profile')
    if not binding:
        return {'state': 'not-declared', 'gaps': []}
    root = Path(session.snapshot['workspace']['root'])
    selected = session.snapshot.get('selected_checkouts') or {}
    owner = selected.get(binding['repository'], {}).get('path')
    owner = Path(owner) if owner else root / binding['repository']
    try:
        raw = confined(owner, binding['path']).read_bytes()
        identity = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if identity != binding['identity']:
            return {'state': 'unknown', 'reason': 'structure-profile-identity-moved', 'gaps': []}
        profile = json.loads(raw)
        schema = json.loads((owner / 'schemas/project-structure-profile-1.schema.json').read_text())
        from jsonschema import Draft202012Validator
        Draft202012Validator(schema).validate(profile)
        gaps = [relative for relative in profile['required_paths'] if not confined(checkout, relative).exists()]
        observations = {'identity': identity, 'gaps': gaps,
                        'schema': digest_hex(schema), 'roles': manifest.get('organization', {})}
        # A changed owning policy must not reuse an old conformance verdict.
        from .projections import _session_binding, input_digest
        policy = _session_binding(session, DOCTOR_PROJECTION_HEALTH) or {}
        policy_root = Path(policy['provider_root'])
        policy_manifest = json.loads((policy_root / '.mncs/project.json').read_text())
        descriptor = next(row for row in policy_manifest['contracts']['provides']
                          if row['contract'] == 'projection-health')
        observations['policy'] = {'descriptor': digest_hex(descriptor),
            'sources': input_digest(policy_root, descriptor.get('fingerprint_sources', []))}
        key = digest_hex(observations)
        cached = session.snapshot.setdefault('structure_observations', {}).get(str(checkout))
        if cached and cached.get('identity') == key:
            state = cached['state']
        else:
            response = session.invoke(DOCTOR_PROJECTION_HEALTH, ['--facts-json',
                json.dumps({'structure': [1, len(gaps), 0]})], timeout_seconds=120)
            result = json.loads(response['stdout'])
            if response.get('status') != 'ok' or result.get('schema_version') != 'mncs.projection-structure/1':
                raise ValueError('Doctor structure verdict unavailable')
            state = result['state']
            session.snapshot['structure_observations'][str(checkout)] = {'identity': key, 'state': state}
        return {'state': state, 'profile': profile['profile'],
                'identity': identity, 'gaps': gaps,
                'roles': manifest.get('organization', {}).get('surfaces', [])}
    except Exception:
        return {'state': 'unknown', 'reason': 'structure-authority-unavailable', 'gaps': []}
