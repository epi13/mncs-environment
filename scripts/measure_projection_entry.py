#!/usr/bin/env python3
"""Measure normal Store entry without manufacturing source changes.

Process counts cover this process's launches; use strace -f for the process tree.
Store counts are public backend calls, with generation delta recording commits.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mncs_env import entry, session_store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--definition', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--consumer', required=True)
    parser.add_argument('--samples', type=int, default=2)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    definition = json.loads(args.definition.read_text())
    rows = []
    real_open, real_popen = entry.open_store, subprocess.Popen
    for _ in range(args.samples):
        calls, launches, generations = Counter(), Counter(), []
        def observe_open(*a, **kw):
            store = real_open(*a, **kw)
            generations.append([store.generation(), None])
            position = generations[-1]
            for name in ('get_record', 'put_record', 'read_snapshot', 'put_snapshot',
                         'read_projection_row', 'write_projection_row', 'read_projection_versions',
                         'read_evidence', 'write_evidence', 'read_claims', 'put_claim',
                         'read_events', 'put_event'):
                original = getattr(store.backend, name, None)
                if original is None:
                    continue
                def observed(*a, _name=name, _original=original, **kw):
                    calls[_name] += 1
                    return _original(*a, **kw)
                setattr(store.backend, name, observed)
            close = store.close
            def observed_close():
                position[1] = store.generation()
                return close()
            store.close = observed_close
            return store
        def observe_process(command, *a, **kw):
            name = Path(str(command[0])).name
            launches['native' if name == 'mncs' else 'git' if name == 'git' else 'other'] += 1
            return real_popen(command, *a, **kw)
        started = time.monotonic()
        with patch.object(entry, 'open_store', observe_open), patch.object(subprocess, 'Popen', observe_process):
            result = entry.enter(definition=definition, definition_path=args.definition,
                workspace_root=definition['workspace_root'], state_dir=args.state_dir,
                backend='store', consumer_id=args.consumer, consumer_kind='agent')
        rows.append({'seconds': round(time.monotonic()-started, 6),
            'context_bytes': len(json.dumps(result, sort_keys=True, separators=(',', ':')).encode()),
            'projection_context_bytes': len(json.dumps(result.get('projection', {}), separators=(',', ':')).encode()),
            'process_launches': dict(launches), 'store_calls': dict(calls),
            'store_commits': sum(end-start for start,end in generations if end is not None),
            'projection': result.get('projection'), 'session_id': result['session_id']})
    args.output.write_text(json.dumps({'schema_version':'mncs.entry-measurement/1',
        'method':'normal entry; parent process launches; backend API calls; Store generation delta',
        'samples': rows}, indent=2)+'\n')
    print(json.dumps(rows, indent=2))


if __name__ == '__main__':
    main()
