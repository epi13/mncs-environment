#!/usr/bin/env python3
"""Produce evidence for declared projection conformance, not execution maturity.

JSON schema validation consumes the Commons contract and MNCDS profile.
Doctor's native policy owns the structural verdict. Evidence binds all inputs.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mncs_env.projection_sources import identity, confined
from jsonschema import Draft202012Validator


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    root = args.workspace.resolve()
    checkout = confined(root, args.repository)
    contract_path = root/'MNCS-Commons/schemas/semantic-projection-1.schema.json'
    contract = json.loads(contract_path.read_text())
    manifest = json.loads((checkout/'.mncs/project.json').read_text())
    inventory = json.loads(confined(checkout, manifest['projection_inventory']).read_text())
    if inventory['schema_version'] != 'mncs.projection-inventory/1':
        raise ValueError('unsupported inventory')
    declarations = inventory['projections']
    ids = set()
    for declaration in declarations:
        Draft202012Validator(contract).validate(declaration)
        if declaration['id'] in ids:
            raise ValueError('duplicate projection identity')
        ids.add(declaration['id'])
        target = confined(checkout, declaration['output'])
        for subject in declaration['subjects']:
            source = confined(checkout, subject['path'])
            if source == target or source.is_dir() and target.is_relative_to(source):
                raise ValueError('projection feeds its own source')
    binding = manifest['structure_profile']
    authority = confined(root, binding['repository'])
    raw = confined(authority, binding['path']).read_bytes()
    if 'sha256:'+hashlib.sha256(raw).hexdigest() != binding['identity']:
        raise ValueError('profile identity moved')
    profile = json.loads(raw)
    profile_schema = json.loads((authority/'schemas/project-structure-profile-1.schema.json').read_text())
    Draft202012Validator(profile_schema).validate(profile)
    missing = sum(not confined(checkout,p).exists() for p in profile['required_paths'])
    invocation = subprocess.run([sys.executable, str(root/'mncs-doctor/tools/projections.py'),
        '--facts-json', json.dumps({'structure':[1,missing,0]})], capture_output=True, text=True, check=True)
    verdict = json.loads(invocation.stdout)
    record = {'schema_version':'mncs.semantic-evidence/1',
        'producer':'mncs-environment:projection-conformance',
        'subject_identity':identity(declarations), 'verdict':verdict['state'],
        'validation':{'contract':'MNCS-Commons:semantic-projection/1',
            'identity':identity(contract),'profile':binding['identity'],
            'profile_schema':identity(profile_schema), 'native_receipt':verdict['provenance']}}
    record['receipt_identity'] = identity(record)
    result = {'projection-conformance':record}
    if args.output:
        args.output.write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps(result,indent=2))
    return 0 if record['verdict'] == 'pass' else 1


if __name__ == '__main__':
    raise SystemExit(main())
