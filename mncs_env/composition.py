"""Selection of distinct execution roles by references to provider bindings.

This module neither establishes compiler build origin nor admits executable
artifacts. Provider declarations, observed bytes and Doctor proofs stay distinct.
"""
from pathlib import Path
from .observations import observe_artifact
from .identity import digest_hex


def _provider_build_origin(role: str, selected: dict, component: dict,
                           service_identity: str, observed_at: str | None) -> dict:
    """Link Doctor's owner-verified component receipt to selected executable bytes.

    Environment records provider evidence and identity joins; it does not
    interpret compiler lowering or VM execution semantics.
    """
    executable = selected.get("executable") or {}
    checkout = selected.get("checkout") or {}
    component_executable = component.get("executable")
    component_sha = component.get("executable_sha256")
    observed_identity = executable.get("artifact_identity")
    expected_sha = observed_identity.removeprefix("sha256:") if isinstance(observed_identity, str) else ""
    selected_path = executable.get("address")
    selected_checkout = checkout.get("path")
    selected_revision = checkout.get("head")

    receipt_identity = None
    component_checkout = None
    component_revision = None
    provider_status = "unknown"
    assurance = None
    dependency_revision = None
    if role == "compiler":
        producer = component.get("producer") or {}
        receipt = producer.get("receipt") or {}
        receipt_identity = producer.get("identity")
        component_checkout = component.get("checkout")
        component_revision = receipt.get("source_revision")
        dependency_revision = receipt.get("stage0_revision")
        provider_status = component.get("build_origin", "unknown")
        assurance = receipt.get("assurance", "embedded provider receipt; locally observed inputs")
        provider_current = (component.get("state") == "ready"
                            and not component.get("mismatches"))
    else:
        origin = component.get("build_origin") or {}
        receipt_identity = origin.get("receipt_identity")
        provider_status = origin.get("status", "unknown")
        assurance = origin.get("assurance")
        if role == "reference":
            component_revision = origin.get("source_revision")
            component_checkout = component.get("executable")
            try:
                component_checkout = str(__import__("pathlib").Path(component_checkout).resolve().parents[2])
            except (TypeError, OSError, IndexError):
                component_checkout = None
        elif role == "runtime":
            revisions = origin.get("source_revisions") or {}
            closures = origin.get("dependency_closure") or []
            vm_closure = next((item for item in closures
                               if isinstance(item, dict) and item.get("repository") == "mncs-vm"), {})
            component_revision = revisions.get("mncs-vm") or vm_closure.get("revision")
            component_checkout = vm_closure.get("checkout")
            dependency_revision = revisions.get("mncs-compiler")
        provider_current = (provider_status == "matches-embedded-inputs"
                            and origin.get("mismatch_count", 0) == 0)

    # The reference executable lives at <checkout>/target/<profile>/mncs;
    # its build receipt independently names the source checkout and revision.
    if role == "reference":
        # Doctor calls the language provider with this selected checkout as
        # expected_checkout; a current receipt therefore binds this path.
        component_checkout = selected_checkout if provider_current else None

    checks = {
        "executable_bytes": isinstance(component_sha, str) and component_sha == expected_sha,
        "executable_path": isinstance(component_executable, str) and component_executable == selected_path,
        "checkout": isinstance(component_checkout, str) and component_checkout == selected_checkout,
        "source_revision": isinstance(component_revision, str) and component_revision == selected_revision,
        "provider_receipt": provider_current and isinstance(receipt_identity, str) and bool(receipt_identity),
    }
    return {
        "schema_version": "mncs.environment.provider-build-evidence/1",
        "state": "matches-selected-inputs" if all(checks.values()) else "mismatch",
        "provider_status": provider_status,
        "service_identity": service_identity,
        "component_identity": digest_hex(component),
        "receipt_identity": receipt_identity,
        "executable_identity": executable.get("artifact_identity"),
        "source_revision": component_revision,
        "dependency_revision": dependency_revision,
        "assurance": assurance,
        "checks": checks,
        "observed_at": observed_at,
    }


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
    evidence = next((item for item in (service_observations or [])
                     if item.get('identity') == compatibility_service), None)
    # Composition identity binds only selected providers and executable bytes.
    # Provider evidence is joined afterward so an observation cannot change
    # selection identity or manufacture its own compatibility claim.
    identity = digest_hex({'roles': roles})
    if (evidence is not None and evidence.get("status") == "ready"
            and evidence.get("composition_identity") == identity):
        components = evidence.get("provider_components")
        if isinstance(components, dict):
            for name, role_value in roles.items():
                component = components.get(name)
                if isinstance(component, dict):
                    role_value["provider_build_origin"] = _provider_build_origin(
                        name, role_value, component, str(compatibility_service),
                        evidence.get("observed_at"))
    compatibility = {'state':'unproven', 'authority':'selected provider readiness contract'}
    if compatibility_service:
        compatibility['service_identity'] = compatibility_service
        if evidence is not None:
            compatibility['service_status'] = evidence.get('status')
            current = evidence.get('composition_identity') == identity
            if evidence.get('status') == 'ready' and current:
                doctor_contract = evidence.get("response_schema") == "mncs.doctor.compiler-vm/1"
                evidence_states = {
                    name: (roles.get(name, {}).get("provider_build_origin") or {}).get("state", "missing")
                    for name in ("reference", "compiler", "runtime")
                    if name in roles
                }
                provider_evidence_matches = (
                    not doctor_contract or (
                        bool(evidence_states)
                        and all(state == "matches-selected-inputs" for state in evidence_states.values())
                    )
                )
                if provider_evidence_matches:
                    compatibility.update(state='verified', evidence_identity=compatibility_service,
                                         observed_at=evidence.get('observed_at'))
                else:
                    compatibility.update(state='unproven', evidence_stale=True,
                                         provider_build_evidence=evidence_states,
                                         reason='Doctor receipt evidence does not match every selected executable and checkout')
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
