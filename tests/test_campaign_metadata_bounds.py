from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env.sessions import (
    CAMPAIGN_STATE_INPUT_MAX_BYTES,
    normalize_campaign_evidence,
)


def _reference(index: int, *, path: str | None = None) -> dict[str, str]:
    return {
        "identity": f"ev_bound_{index:02d}",
        "repository": "mncs-environment",
        "commit": f"{index + 1:040x}",
        "path": path or f"tests/evidence_{index:02d}.json",
        "sha256": f"{index + 1:064x}",
    }


def test_campaign_evidence_allows_growth_past_previous_transport_limit() -> None:
    references = [
        _reference(index, path=f"evidence/{'x' * 120}_{index:03d}.json")
        for index in range(128)
    ]
    encoded = json.dumps(references, sort_keys=True, separators=(",", ":")).encode()

    assert len(encoded) > 32 * 1024
    assert len(encoded) <= CAMPAIGN_STATE_INPUT_MAX_BYTES
    assert len(normalize_campaign_evidence(references)) == 128
    with pytest.raises(ValueError, match="at most 128 references"):
        normalize_campaign_evidence([*references, _reference(128)])


def test_campaign_evidence_remains_bounded_by_normalized_utf8_size() -> None:
    references = [
        _reference(index, path=f"evidence/{'x' * 470}_{index:03d}.json")
        for index in range(128)
    ]

    with pytest.raises(ValueError, match="bounded publication size"):
        normalize_campaign_evidence(references)


def test_environment_capability_observation_reference_requires_digest() -> None:
    observation = _reference(1, path=(
        "evidence/environmental-capability/observation.json"
    ))
    observation.pop("sha256")

    with pytest.raises(ValueError, match="environmental-capability.*require sha256"):
        normalize_campaign_evidence([observation])

    signed_observation = _reference(3, path=(
        "evidence/environmental-capability/observation.json"
    ))
    assert normalize_campaign_evidence([signed_observation])[0]["sha256"] == (
        signed_observation["sha256"]
    )

    generic = _reference(2, path="evidence/compiler/source.json")
    generic.pop("sha256")
    normalized = normalize_campaign_evidence([generic])
    assert normalized[0]["path"] == "evidence/compiler/source.json"
    assert "sha256" not in normalized[0]
