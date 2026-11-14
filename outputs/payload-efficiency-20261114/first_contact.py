#!/usr/bin/env python3
"""Scripted first contact from the actual README and returned CLI actions.

This is an author-defined exercise in fresh processes, not an independent agent.
It uses real repository/capability data with an owned temporary canonical Store.
No fixture services or preassembled context are substituted.
"""
import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--workspace-guide', type=Path)
a = parser.parse_args()
root = a.source.resolve()
workspace_guide = a.workspace_guide.resolve() if a.workspace_guide else None
if workspace_guide:
    workspace_text = workspace_guide.read_text()
    reference = re.search(r'\[MNCS Environment entry\]\(([^)#]+)(?:#[^)]*)?\)', workspace_text)
    assert reference, 'workspace guidance has no discoverable Environment entry'
    readme = (workspace_guide.parent / reference.group(1)).resolve()
    assert readme.parent == root, 'workspace guidance selects a different source checkout'
else:
    readme = root / 'README.md'
guide = readme.read_text()
# Match the documented command for the protected project's selected definition.
match = re.search(r'^\./scripts/mncs-env enter --definition \.mncs/project-environment\.json --consumer my-agent$', guide, re.M)
assert match, 'actual entry guidance does not advertise the selected project definition'
entry = shlex.split(match.group())
entry[0] = str(root / entry[0])
steps = []
env = dict(os.environ)
for key in ('MNCS_ENV_PERSISTENCE_TRANSPORT', 'MNCS_ENV_RPC_SOCKET', 'MNCS_ENV_RPC_GRANT_FILE'):
    env.pop(key, None)

with tempfile.TemporaryDirectory(prefix='mncs-first-contact-', dir='/tmp') as temporary:
    temporary = Path(temporary)
    probe = temporary / 'measure_process.py'
    probe.write_text("""import atexit, runpy, sys
from pathlib import Path
peak = Path(sys.argv.pop(1))
def record():
    peak.write_text(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmHWM:')))
atexit.register(record)
target = sys.argv.pop(1)
sys.argv[0] = target
runpy.run_path(target, run_name='__main__')
""")
    base = [entry[0], '--state-dir', str(temporary / 'state'), '--persistence', 'store']
    enter = [*base, *entry[1:]]
    def run(label, argv, cwd=temporary):
        started = time.perf_counter()
        peak_file = temporary / 'peak.txt'
        interpreter, script_argv = ((argv[0], argv[1:]) if Path(argv[0]).name.startswith('python') else (sys.executable, argv))
        result = subprocess.run([interpreter, str(probe), str(peak_file), *script_argv],
            cwd=cwd, env=env, text=True, capture_output=True, timeout=180)
        elapsed = time.perf_counter() - started
        payload = json.loads(result.stdout or result.stderr)
        steps.append({'operation': label, 'exit_code': result.returncode,
            'bytes': len(result.stdout.encode()), 'elapsed_seconds': elapsed,
            'peak_rss_bytes': int(peak_file.read_text()) * 1024})
        assert result.returncode == 0, payload
        return payload
    first = run('fresh entry from README', enter, root)
    sid = first['session_id']
    assert first['configuration']['source'].endswith('/.mncs/project-environment.json')
    assert first['workspace_root'] and first['work_intent']
    assert first['projects'][0]['repository'] in first['writable_repositories']
    assert first['capabilities']['unknown_count'] or first['capabilities']['unavailable_count']
    before = run('generation before read navigation', [*base, 'store', '--session', sid, '--generation'])
    summary = run('suggested next command from unrelated cwd', shlex.split(first['next_commands'][0]))
    assert summary['session_id'] == sid and summary['binding_count'] >= len(summary['bindings'])
    selected = next(row for row in summary['bindings'] if row['availability']['status'] != 'available')
    detail_action = first['actions']['capabilities']
    detail = run('exact binding detail', [*detail_action['argv'],
        *detail_action['binding_detail']['append_argv'], selected['binding_id']])
    assert detail[0]['availability'] == selected['availability']
    assert 'provenance' in detail[0]
    workspace = run('compact full checkout inventory', first['actions']['workspace']['argv'])
    expanded = run('expanded authoritative checkout detail', [item for item in first['actions']['workspace']['argv'] if item != '--compact'])
    def resolve(value):
        if isinstance(value, dict):
            if set(value) == {'$ref'}:
                target = workspace
                for part in value['$ref'][2:].split('/'):
                    part = part.replace('~1', '/').replace('~0', '~')
                    target = target[int(part)] if isinstance(target, list) else target[part]
                return resolve(target)
            return {key: resolve(item) for key, item in value.items() if key != 'representation'}
        if isinstance(value, list):
            return [resolve(item) for item in value]
        return value
    assert resolve(workspace) == expanded
    claims = run('claim navigation', first['actions']['claims']['argv'])
    health = run('live readiness navigation', first['actions']['health']['argv'])
    after = run('generation after read navigation', [*base, 'store', '--session', sid, '--generation'])
    assert before == after, 'read-only navigation published a Store generation'
    continued = run('continuation in fresh process', enter, root)
    assert continued['session_id'] == sid and continued['entry']['reused'] is True
    run('complete owned exercise session', [*base, 'complete', sid, '--outcome', 'first-contact exercise complete'])
    report = {'method': 'scripted exercise from actual README and returned actions; not an independent unfamiliar agent',
        'source': str(root), 'workspace_guidance': str(workspace_guide) if workspace_guide else None,
        'discovered_readme': str(readme), 'selected_definition': first['configuration']['source'],
        'manual_workarounds': [], 'source_selection': 'documented explicit match to the protected project definition',
        'same_session_on_continuation': True, 'read_navigation_preserved_generation': True,
        'availability': first['capabilities'], 'readiness': first['readiness']['status'],
        'workspace_checkouts': expanded['workspace']['repository_count'],
        'projects_prioritize_writable_selection': True, 'binding_detail_retained_provenance': True,
        'compact_workspace_exactly_matches_full_detail': True, 'steps': steps}
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'steps': len(steps), 'same_session_on_continuation': True,
                  'read_navigation_preserved_generation': True, 'manual_workarounds': []}))
