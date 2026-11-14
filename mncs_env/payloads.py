"""Lossless, opt-in wire representation of repeated Environment observations.

This is a response view, never persisted state or a second authority. Existing
expanded responses remain the default. Only known observation fields can become
references, and every reference resolves inside this same response (RFC 6901).
No provider calls, historical reads, or full-document JSON round trip is needed.
"""
from __future__ import annotations

import json
from typing import Any


SHARED_FIELDS = frozenset({
    "worktrees", "toolchain_checkouts", "runtime_toolchain_checkouts",
    "selected_checkouts", "foreign_signals",
})
REPRESENTATION = "shared-json-pointers"


def compact(document: dict[str, Any]) -> dict[str, Any]:
    """Retain every fact, replacing equal repeated observations with pointers.

    Discovery shares identical observation objects in memory. Identity lookup
    avoids even fingerprinting those objects again. Content lookup also handles
    expanded historical snapshots loaded from Store. Traverse only the first
    occurrence, so repeated collections are never expanded into a second tree.
    Provider-supplied JSON outside the named fields is left uninterpreted.
    """
    by_object: dict[int, str] = {}
    by_content: dict[str, str] = {}

    def visit(value: Any, path: str, field: str = "", parent: str = "") -> Any:
        shared = (field in SHARED_FIELDS or (field == "checkout" and parent == "provenance"))
        if shared and isinstance(value, (dict, list)) and value:
            previous = by_object.get(id(value))
            if previous is None:
                fingerprint = json.dumps(value, sort_keys=True, ensure_ascii=False,
                                         separators=(",", ":"))
                previous = by_content.get(fingerprint)
                if previous is None:
                    by_content[fingerprint] = path
                by_object[id(value)] = previous or path
            if previous is not None:
                return {"$ref": previous}
        if isinstance(value, dict):
            return {key: visit(item, path + "/" + key.replace("~", "~0").replace("/", "~1"),
                               key, field) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item, path + "/" + str(index), field, parent)
                    for index, item in enumerate(value)]
        return value

    if "representation" in document:
        raise ValueError("document already declares a response representation")
    return {**visit(document, "#"), "representation": REPRESENTATION}
