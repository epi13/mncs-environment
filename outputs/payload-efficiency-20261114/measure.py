#!/usr/bin/env python3
"""Bounded real CLI measurement; run separately for baseline and candidate.

Cold means the first request in a new Python process, not flushed OS caches.
Warm means the next request in that same process, using the existing caches.
The selected canonical commands remain read-only. Output stays in memory in
this harness to measure actual serialized bytes; raw operational state is not
copied into the report.
"""
import argparse
import contextlib
import io
import json
import resource
import sys
import time
from collections import Counter
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--source', type=Path, required=True)
p.add_argument('--output', type=Path, required=True)
p.add_argument('--repeat', type=int, choices=(1, 2), default=2)
p.add_argument('argv', nargs=argparse.REMAINDER)
a = p.parse_args()
sys.path.insert(0, str(a.source.resolve()))
from mncs_env import capabilities, cli, workspace
counts = Counter()
original_git = workspace._git
original_probe = capabilities.probe_availability
original_out = cli.out
serialization = 0.0

def git(*args, **kwargs):
    counts['git_commands'] += 1
    if args[1:3] == ('worktree', 'list'):
        counts['worktree_inventory_commands'] += 1
    return original_git(*args, **kwargs)

def probe(*args, **kwargs):
    counts['availability_probes'] += 1
    return original_probe(*args, **kwargs)

def out(*args, **kwargs):
    global serialization
    started = time.perf_counter()
    try:
        return original_out(*args, **kwargs)
    finally:
        serialization += time.perf_counter() - started

workspace._git, capabilities.probe_availability, cli.out = git, probe, out
results = []
for index in range(a.repeat):
    counts.clear()
    serialization = 0.0
    stdout, stderr = io.StringIO(), io.StringIO()
    started = time.perf_counter()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        code = cli.main(a.argv[1:] if a.argv[:1] == ['--'] else a.argv)
    elapsed = time.perf_counter() - started
    peak = int(next(line.split()[1] for line in Path('/proc/self/status').read_text().splitlines() if line.startswith('VmHWM:'))) * 1024
    raw = stdout.getvalue()
    document = json.loads(raw) if raw else None
    repeated = Counter()
    reference_count = 0
    def visit(value, field=''):
        global reference_count
        if isinstance(value, dict):
            if set(value) == {'$ref'}:
                reference_count += 1
            for key, item in value.items():
                visit(item, key)
        elif isinstance(value, list):
            if field in {'worktrees', 'toolchain_checkouts'} and value:
                repeated[json.dumps(value, sort_keys=True, separators=(',', ':'))] += 1
            for item in value:
                visit(item)
    visit(document)
    metadata = document if isinstance(document, dict) else {}
    results.append({'request': 'cold-process' if index == 0 else 'warm-process',
        'exit_code': code, 'stderr': stderr.getvalue()[:2000],
        'bytes': len(raw.encode()), 'elapsed_seconds': elapsed,
        'serialization_seconds': serialization, 'peak_rss_bytes': peak,
        'invocations': dict(counts), 'reference_count': reference_count,
        'repeated_collection_bytes': sum(len(key.encode()) * (count - 1) for key, count in repeated.items()),
        'repeated_collection_copies': sum(count - 1 for count in repeated.values()),
        'workspace_checkouts': metadata.get('repository_count',
            (metadata.get('workspace') or {}).get('repository_count')),
        'bindings': len(document) if isinstance(document, list) else len(metadata.get('bindings', [])),
    })
a.output.parent.mkdir(parents=True, exist_ok=True)
a.output.write_text(json.dumps({'source': str(a.source.resolve()), 'argv': a.argv,
                               'measurements': results}, indent=2) + '\n')
print(json.dumps(results, indent=2))
