"""External pressure registry: blockers owned elsewhere, recorded honestly.

A pressure names a missing contract, its owning repository, the evidence,
why Environment cannot solve it locally, the desired contract, and the
local effect plus any workaround. Workarounds never duplicate the owning
system's semantics; they are narrow, labeled, and removable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .identity import digest_hex
from .persist import read_json, write_json

SCHEMA = "mncs.environment.pressure/1"


def record_path(state_dir: Path | str) -> Path:
    return Path(state_dir) / "pressures.json"


def record(
    *,
    title: str,
    description: str,
    owner: str,
    evidence: str,
    why_not_local: str,
    desired_contract: str,
    effect: str,
    workaround: str | None = None,
) -> dict[str, Any]:
    entry = {
        "schema_version": SCHEMA,
        "identity": "",
        "title": title,
        "description": description,
        "owner": owner,
        "evidence": evidence,
        "why_not_local": why_not_local,
        "desired_contract": desired_contract,
        "effect": effect,
        "workaround": workaround,
    }
    entry["identity"] = "prs_" + digest_hex({key: entry[key] for key in sorted(entry) if key != "identity"})
    return entry


def file_record(state_dir: Path | str, entry: dict[str, Any]) -> dict[str, Any]:
    path = record_path(state_dir)
    entries = read_json(path, default=[])
    if not isinstance(entries, list):
        entries = []
    entries = [item for item in entries if item.get("identity") != entry.get("identity")]
    entries.append(entry)
    entries.sort(key=lambda item: item.get("identity", ""))
    write_json(path, entries)
    return entry


def list_pressures(state_dir: Path | str) -> list[dict[str, Any]]:
    entries = read_json(record_path(state_dir), default=[])
    return entries if isinstance(entries, list) else []
