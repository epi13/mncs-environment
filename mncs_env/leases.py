"""Environment-side workspace leases (file-backed, session-scoped).

MNCS has no canonical ownership/lease mechanism today, so Environment
keeps a clean local representation: a lease names a repository, the
owning session, a reason, and an expiry. Leases are ADVISORY across
processes (no kernel locking) but ENFORCED inside session authority
evaluation. Recorded pressure: canonical lease ownership belongs in the
control plane, not here.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


def _leases_path(state_dir: Path) -> Path:
    return Path(state_dir) / "leases.json"


def read_leases(state_dir: Path) -> list[dict[str, Any]]:
    path = _leases_path(state_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return payload if isinstance(payload, list) else []


def _write_leases(state_dir: Path, leases: list[dict[str, Any]]) -> None:
    path = _leases_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(leases, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _alive(lease: dict[str, Any]) -> bool:
    try:
        return datetime.fromisoformat(lease["expires_at"]) > datetime.now(timezone.utc)
    except (KeyError, ValueError):
        return False


def active_holders(state_dir: Path) -> dict[str, str]:
    """Map repository -> owning session for live leases."""
    holders: dict[str, str] = {}
    for lease in read_leases(state_dir):
        if _alive(lease):
            holders.setdefault(str(lease.get("repository", "")), str(lease.get("owner_session", "")))
    return holders


def acquire(
    state_dir: Path,
    *,
    repository: str,
    owner_session: str,
    reason: str,
    ttl_hours: int = 24,
) -> dict[str, Any]:
    """Acquire a lease; raises LeaseConflict when another live session holds it."""
    leases = [lease for lease in read_leases(state_dir) if _alive(lease)]
    for lease in leases:
        if lease.get("repository") == repository and lease.get("owner_session") != owner_session:
            raise LeaseConflict(
                f"{repository} leased to {lease.get('owner_session')}: {lease.get('reason')}"
            )
    leases = [lease for lease in leases if lease.get("repository") != repository]
    record = {
        "repository": repository,
        "owner_session": owner_session,
        "reason": reason,
        "acquired_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=ttl_hours)).isoformat(
            timespec="seconds"
        ),
    }
    leases.append(record)
    _write_leases(state_dir, leases)
    return record


def release(state_dir: Path, *, repository: str, owner_session: str) -> bool:
    leases = read_leases(state_dir)
    kept = [
        lease
        for lease in leases
        if not (lease.get("repository") == repository and lease.get("owner_session") == owner_session)
    ]
    if len(kept) == len(leases):
        return False
    _write_leases(state_dir, kept)
    return True


class LeaseConflict(Exception):
    """Raised when a repository is already leased to another session."""
