"""Selected library addressing shared by Environment's native transports."""
from __future__ import annotations

import os
from pathlib import Path


def language_library_for(binary: str, session=None) -> Path | None:
    explicit = os.environ.get("MNCS_STDLIB_ROOT")
    if explicit is not None:
        candidate = Path(explicit).expanduser().resolve() / "library" if explicit.strip() else None
        return candidate if candidate and candidate.is_dir() else None
    explicit_library = os.environ.get("MNCS_LIBRARY_ROOT")
    if explicit_library is not None:
        candidate = Path(explicit_library).expanduser().resolve() if explicit_library.strip() else None
        return candidate if candidate and candidate.is_dir() else None
    selected = getattr(session, "snapshot", {}).get("selected_checkouts", {})
    root = getattr(session, "snapshot", {}).get("workspace", {}).get("root", "")
    if isinstance(selected, dict) and "mncs-stdlib" in selected:
        path = Path(str(selected["mncs-stdlib"].get("path", "")))
        candidate = (path if path.is_absolute() else Path(root) / path) / "library"
        return candidate if candidate.is_dir() else None
    language = selected.get("mncs-language", {}) if isinstance(selected, dict) else {}
    path = language.get("path")
    if path:
        checkout = Path(path)
        checkout = checkout if checkout.is_absolute() else Path(root) / checkout
    else:
        try:
            checkout = Path(binary).resolve().parents[2]
        except IndexError:
            return None
    sibling = checkout.parent / "mncs-stdlib" / "library"
    if sibling.is_dir():
        return sibling
    legacy = checkout / "library"
    return legacy if legacy.is_dir() else None


def selected_stdlib_root(workspace_root: Path, checkout: dict) -> Path:
    """Validate provider addressing; module identity remains stdlib-owned."""
    value = checkout.get("path")
    if not isinstance(value, str) or not value:
        raise ValueError("selected mncs-stdlib checkout has no provider-owned path")
    root = Path(value)
    root = (root if root.is_absolute() else workspace_root / root).resolve()
    if not root.is_relative_to(workspace_root.resolve()) or not (root / "library").is_dir():
        raise ValueError("selected mncs-stdlib library is unavailable inside its workspace")
    return root
