"""Selection of distinct execution roles by references to provider bindings.

This module neither establishes compiler build origin nor admits executable
artifacts. Provider declarations, observed bytes and Doctor proofs stay distinct.
"""
from pathlib import Path
from .observations import observe_artifact
from .identity import digest_hex


def validate(selectors):
    if not isinstance(selectors, dict) or len(selectors) > 8:
        raise ValueError('execution_roles must be a bounded role/selector mapping')
    for name, selector in selectors.items():
        if (not isinstance(name, str) or not name or not isinstance(selector, dict)
                or set(selector) - {'capability', 'use_reference_toolchain'}
                or not isinstance(selector.get('capability'), str) or not selector['capability']
                or type(selector.get('use_reference_toolchain', False)) is not bool):
            raise ValueError('execution role requires a capability reference and optional reference-toolchain selection')
    return selectors


def resolve(selectors, bindings, toolchain, prior=None, *, compatibility_service=None,
            service_observations=None):
    validate(selectors)
    by_id = {b['capability']:b for b in bindings}
    roles = {}
    prior_roles = (prior or {}).get('roles', {})
    for role, selector in sorted(selectors.items()):
        binding = by_id.get(selector['capability'])
        if binding is None:
            roles[role] = {'capability':selector['capability'], 'state':'unbound'}
            continue
        provenance = binding.get('provenance') or {}
        binary = (toolchain or {}).get('binary') if selector.get('use_reference_toolchain') else binding.get('toolchain_address')
        if not binary:
            address = binding.get('address')
            binary = address if isinstance(address, str) and not address.startswith('python:') else None
        observed = observe_artifact(Path(binary), prior_roles.get(role, {}).get('executable')) if binary else None
        roles[role] = {'binding_id':binding['binding_id'], 'capability':binding['capability'],
                       'provider':binding['provider'], 'checkout':provenance.get('checkout'),
                       'contract_revision':binding['contract_revision'], 'artifact_contract':provenance.get('artifact_contract'),
                       'executable':observed, 'state':'selected' if observed and observed.get('status')=='observed' else 'unavailable',
                       'verification':'selection and executable bytes; provider readiness/admission are separate'}
    identity = digest_hex({'roles': roles})
    evidence = next((item for item in (service_observations or [])
                     if item.get('identity') == compatibility_service), None)
    compatibility = {'state':'unproven', 'authority':'selected provider readiness contract'}
    if compatibility_service:
        compatibility['service_identity'] = compatibility_service
        if evidence is not None:
            compatibility['service_status'] = evidence.get('status')
            current = evidence.get('composition_identity') == identity
            if evidence.get('status') == 'ready' and current:
                compatibility.update(state='verified', evidence_identity=compatibility_service,
                                     observed_at=evidence.get('observed_at'))
            elif not current:
                compatibility['evidence_stale'] = True
    return {'schema_version':'mncs.environment.execution-composition/1', 'identity':identity,
            'roles':roles, 'compatibility':compatibility}


def validate_compatibility_service(identity, services):
    """Validate an explicit link to a declared provider-owned readiness service."""
    if identity is None:
        return None
    if not isinstance(identity, str) or not identity:
        raise ValueError('execution_compatibility_service must be a nonempty service identity')
    if not any(item.get('identity') == identity for item in services):
        raise ValueError('execution_compatibility_service must reference a declared service')
    return identity
