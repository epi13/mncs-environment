"""Bounded supplemental scaling fixture, separate from real workspace measurements."""
import importlib.util
import json
import time
import tracemalloc
from pathlib import Path
from mncs_env import payloads

spec = importlib.util.spec_from_file_location('payload_fixture', Path(__file__).resolve().parents[2] / 'tests/test_payload_efficiency.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
results = []
for repositories, worktrees, bindings in [(1, 0, 1), (4, 8, 16), (4, 32, 64), (4, 64, 128)]:
    document = module.fixture(repositories, worktrees, bindings)
    record = {'repositories': repositories, 'worktrees_per_repository': worktrees,
              'bindings_per_repository': bindings}
    for kind in ('expanded', 'compact'):
        tracemalloc.start()
        begin = time.perf_counter()
        result = payloads.compact(document) if kind == 'compact' else document
        encoded = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True)
        elapsed = time.perf_counter() - begin
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        record[kind] = {'bytes': len(encoded.encode()), 'serialization_seconds': elapsed,
                        'peak_serialization_allocation_bytes': peak}
        if kind == 'compact':
            assert module.expand(json.loads(encoded)) == document
    results.append(record)
Path(__file__).with_name('scaling.json').write_text(json.dumps(results, indent=2) + '\n')
print(json.dumps(results, indent=2))
