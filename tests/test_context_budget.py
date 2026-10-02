import json
from types import SimpleNamespace

import pytest

from mncs_env import context_budget


def test_healthy_output_has_no_extra_context(tmp_path):
    session = SimpleNamespace(state_dir=tmp_path, session_id="session")
    context = {"doctor": {"summary": {"blockers": 0}}, "session_id": "session"}
    assert context_budget.apply(session, context, context_budget.validate({})) == context
    assert not list(tmp_path.iterdir())


def test_expansion_preserves_full_reasons_and_writes_once(tmp_path):
    session = SimpleNamespace(state_dir=tmp_path, session_id="session")
    block = {"summary": {"blockers": 30, "reason": "long" * 1000},
             "capsule": {"attention": [{"reason": "precise", "change": i} for i in range(8)]}}
    result = context_budget.apply(session, {"family": block}, context_budget.validate({}))
    assert result["family"]["summary"]["blockers"] == 30
    assert result["family"]["attention_count"] == 8
    evidence = tmp_path / result["family"]["evidence"]
    assert json.loads(evidence.read_text()) == block
    before = evidence.stat().st_mtime_ns
    context_budget.apply(session, {"family": block}, context_budget.validate({}))
    assert evidence.stat().st_mtime_ns == before


def test_invalid_budgets_refuse():
    for bad in [{"healthy_bytes": 1}, {"attention_items": True}, {"surprise": 1}]:
        with pytest.raises(ValueError):
            context_budget.validate({"context_budget": bad})


def test_all_ambient_layers_fit_total_budget_with_complete_expansions(tmp_path):
    session = SimpleNamespace(state_dir=tmp_path, session_id="ses_context_proof")
    blocks = {key: {"summary": {"blockers": 1, "reason": "reason" * 1000},
                    "items": list(range(1000))} for key in context_budget.LAYERS}
    result = context_budget.apply(session, blocks.copy(), context_budget.validate({}))
    from mncs_env.identity import canonical_bytes
    assert sum(len(canonical_bytes(block)) for block in result.values()) <= 4096
    for key, block in result.items():
        assert json.loads((tmp_path / block["evidence"]).read_text()) == blocks[key]
