import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_CONTRACTS = {
    "ambient-projection-coherence",
    "projection-evidence-inspection",
    "campaign-state-inspection",
}
PROJECT_VIEW_FIELDS = (
    "version",
    "kind",
    "effects",
    "fingerprint_sources",
    "invocation",
    "output_limit_bytes",
    "output_scope",
    "stability",
)


def test_machine_project_view_exposes_declared_capability_interfaces():
    project = json.loads((ROOT / ".mncs/project.json").read_text(encoding="utf-8"))
    project_view = json.loads(
        (ROOT / ".mncs/project-view.json").read_text(encoding="utf-8")
    )
    declared = {
        item["contract"]: item
        for item in project["contracts"]["provides"]
        if item.get("contract") in DISCOVERY_CONTRACTS
    }
    exposed = {
        item["contract"]: item
        for item in project_view["capabilities"]
        if item.get("contract") in DISCOVERY_CONTRACTS
    }

    assert project_view["schema_version"] == "mncs.project-view/1"
    assert set(declared) == DISCOVERY_CONTRACTS
    assert set(exposed) == DISCOVERY_CONTRACTS
    for identity, declaration in declared.items():
        for field in PROJECT_VIEW_FIELDS:
            if field in declaration:
                assert exposed[identity].get(field) == declaration[field]
