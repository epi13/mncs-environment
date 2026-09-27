"""Rights/provenance gate for session environments.

Claim records come from the environment definition (``rights_claims``
inline, or ``rights_claims_file`` pointing at a JSON list resolved
relative to the workspace root). Evaluation delegates to the generic
host surface in ``mncs_rights_provenance.host_gate`` -- this module
holds no rights logic of its own.

When the rights package is unavailable the gate reports ``review``
for every subject with ``available: false``: unknown provenance never
passes silently, and it never blocks on a missing library either.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


class RightsBlocked(Exception):
    """A workspace repository is rights-blocked for this session."""


def load_claim_records(
    definition: dict[str, Any], workspace_root: str | Path,
) -> list[dict[str, Any]]:
    records = list(definition.get("rights_claims", []) or [])
    claims_file = definition.get("rights_claims_file")
    if claims_file:
        path = Path(claims_file)
        if not path.is_absolute():
            path = Path(workspace_root) / path
        records.extend(json.loads(path.read_text()))
    return records


def evaluate_rights(
    *,
    workspace_repos: list[str],
    records: list[dict[str, Any]],
    now: int | None = None,
) -> dict[str, Any]:
    moment = int(now if now is not None else time.time())
    try:
        from mncs_rights_provenance import host_gate
    except ImportError:
        return {
            "available": False,
            "overall": "review",
            "subjects": {name: "review" for name in workspace_repos},
            "detail": {name: ["rights library unavailable: unknown"] for name in workspace_repos},
            "evaluated_at": moment,
            "claim_count": len(records),
        }
    verdict = host_gate.evaluate_subjects(records, now=moment, subjects=workspace_repos)
    verdict["available"] = True
    verdict["evaluated_at"] = moment
    verdict["claim_count"] = len(records)
    return verdict


def enforced_repos(rights: dict[str, Any], workspace_repos: list[str]) -> list[str]:
    """Workspace repos whose rights verdict is ``blocked``."""
    subjects = rights.get("subjects", {})
    return [name for name in workspace_repos if subjects.get(name) == "blocked"]


def check_enter(
    workspace_repos: list[str], rights: dict[str, Any],
) -> None:
    blocked = enforced_repos(rights, workspace_repos)
    if blocked:
        raise RightsBlocked(
            "rights-blocked repositories: " + ", ".join(sorted(blocked))
        )
