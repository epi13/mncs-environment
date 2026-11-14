"""Read owner-declared MNCDS structure expectations; never infer anatomy."""
from __future__ import annotations
import contextlib
import contextvars
import hashlib
import json
from pathlib import Path
from .projection_sources import confined
from .capabilities import DOCTOR_PROJECTION_HEALTH
from .identity import digest_hex

_STRUCTURE_PASS_CACHE: contextvars.ContextVar[
    dict[tuple[object, ...], str | None] | None
] = contextvars.ContextVar("mncs_env_structure_pass_cache", default=None)


@contextlib.contextmanager
def structure_pass_cache():
    """Share identical structure-policy verdicts across one projection pass."""
    token = _STRUCTURE_PASS_CACHE.set({})
    try:
        yield
    finally:
        _STRUCTURE_PASS_CACHE.reset(token)


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
        policy = _session_binding(session, DOCTOR_PROJECTION_HEALTH)
        if policy is None:
            return {'state': 'unknown',
                    'reason': 'structure-verdict-provider-unbound',
                    'provider_capability': DOCTOR_PROJECTION_HEALTH,
                    'gaps': []}
        if policy.get('availability', {}).get('status') != 'available':
            return {'state': 'unknown',
                    'reason': 'structure-verdict-provider-unavailable',
                    'provider_capability': DOCTOR_PROJECTION_HEALTH,
                    'gaps': []}
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
            pass_cache = _STRUCTURE_PASS_CACHE.get(None)
            pass_key = (
                (policy.get('binding_id'), digest_hex(observations['policy']), len(gaps))
                if isinstance(pass_cache, dict)
                else None
            )
            if pass_key is not None and pass_key in pass_cache:
                cached_state = pass_cache[pass_key]
                if cached_state is None:
                    raise ValueError('Doctor structure verdict unavailable')
                state = cached_state
            else:
                try:
                    response = session.invoke(DOCTOR_PROJECTION_HEALTH, ['--facts-json',
                        json.dumps({'structure': [1, len(gaps), 0]})], timeout_seconds=120)
                    result = json.loads(response['stdout'])
                    if response.get('status') != 'ok' or result.get('schema_version') != 'mncs.projection-structure/1':
                        raise ValueError('Doctor structure verdict unavailable')
                    state = result['state']
                except Exception:
                    if pass_key is not None:
                        pass_cache[pass_key] = None
                    raise
                if pass_key is not None:
                    pass_cache[pass_key] = state
            session.snapshot.setdefault('structure_observations', {})[str(checkout)] = {
                'identity': key,
                'state': state,
            }
        return {'state': state, 'profile': profile['profile'],
                'identity': identity, 'gaps': gaps,
                'roles': manifest.get('organization', {}).get('surfaces', [])}
    except Exception:
        return {'state': 'unknown', 'reason': 'structure-authority-unavailable', 'gaps': []}
