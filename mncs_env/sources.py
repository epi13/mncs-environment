"""Generic external-source contract for background reconciliation.

A source answers one question -- "events since cursor X" -- with an
ordered delta, or an explicit RESET / HISTORY_EXPIRED / UNKNOWN result.
Cursors are provider-owned and opaque; Environment stores them durably
but never interprets them, and never silently jumps one forward.

Environment normalizes references (session/work/repository/subject) and
relevance. The provider remains authoritative for the meaning of its
results: no provider domain semantics are copied here.
"""

from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .identity import digest_hex

#: Maximum observations admitted from one source per cycle.
MAX_OBSERVATIONS = 100

#: Maximum Store generations walked in one replay.
MAX_REPLAY_WALK = 256


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Observation:
    """One normalized external observation (bounded, content-keyed)."""

    source: str
    stream: str
    cursor: str
    identity: str
    subject: str
    observed_at: str
    generation: str
    kind: str
    severity: str = "info"
    summary: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)
    relations: dict[str, Any] = field(default_factory=dict)
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SourceResult:
    """Outcome of one poll: delta | reset | unknown."""

    status: str  # "ok" | "reset" | "unknown"
    events: list[Observation]
    cursor: str | None
    detail: str = ""


def observe_identity(source: str, stream: str, cursor: str,
                     subject: str, kind: str) -> str:
    return "obs_" + digest_hex(
        {"source": source, "stream": stream, "cursor": cursor,
         "subject": subject, "kind": kind}
    )[:16]


class Source:
    """Pollable external source. Subclass per provider."""

    name = "base"

    def observe(self, since: str | None) -> SourceResult:
        raise NotImplementedError


class GitHeadsSource(Source):
    """Workspace head/branch/dirty transitions via bounded git polling."""

    name = "git-heads"

    def __init__(self, workspace_root: str | Path):
        from . import workspace as workspace_module
        self._workspace = workspace_module
        self.root = str(workspace_root)

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        view = self._workspace.discover_workspace(self.root)
        return {
            repo["name"]: {
                "head": repo.get("head"), "branch": repo.get("branch"),
                "dirty": bool(repo.get("dirty")),
            }
            for repo in view.get("repositories", [])
        }

    @staticmethod
    def _encode(heads: dict[str, dict[str, Any]]) -> str:
        return "v1:" + digest_hex(heads)

    def observe(self, since: str | None) -> SourceResult:
        try:
            current = self._snapshot()
        except Exception as error:
            return SourceResult("unknown", [], since, f"workspace unreadable: {error}")
        cursor = self._encode(current)
        if since is None:
            return SourceResult("ok", [], cursor, "baseline adopted")
        if since == cursor:
            return SourceResult("ok", [], cursor, "no change")
        # Heads digests are opaque and the previous map is not retained
        # (bounded state): any mismatch reports one workspace-level
        # observation naming current heads. Per-repo before/after comes
        # from session observe_workspace, which keeps head history.
        events = [Observation(
            source=self.name, stream="heads", cursor=cursor,
            identity=observe_identity(self.name, "heads", cursor, ".", "workspace.changed"),
            subject=".", observed_at=utcnow(), generation=cursor,
            kind="workspace.changed", severity="info",
            summary=f"workspace heads diverged from reconciler cursor ({len(current)} repos)",
            provenance={"workspace_root": self.root},
            relations={"repositories": sorted(current)},
            payload={"heads": {name: info["head"] for name, info in current.items()}},
        )]
        return SourceResult("ok", events[:MAX_OBSERVATIONS], cursor, "heads diverged")


class StoreReplaySource(Source):
    """Missed Store generations replayed with domain-identity classification.

    Walks (cursor, head] via the Store's verified historical projection
    (added in mncs-store for exactly this purpose), classifies new domain
    identities by prefix, and skips the reconciler's own writes. Event
    payloads (ses_*:evt:*) are owned by session logs and never duplicated
    here; snapshots and claims become coordination observations.
    """

    name = "store-replay"

    def __init__(self, store, own_session_prefix: str):
        self.store = store
        self.own_prefix = own_session_prefix

    def _objects_at(self, generation: int) -> list[Any] | None:
        objects_at = getattr(self.store, "objects_at", None)
        if not callable(objects_at):
            return None
        try:
            return list(objects_at(generation))
        except Exception:
            return None

    def observe(self, since: str | None) -> SourceResult:
        generation = getattr(self.store, "generation", None)
        if not callable(generation):
            return SourceResult("unknown", [], since, "no generation feed on this backend")
        try:
            head = int(generation())
        except Exception as error:
            return SourceResult("unknown", [], since, f"generation unreadable: {error}")
        if since is None:
            return SourceResult("ok", [], str(head), "baseline adopted")
        try:
            last = int(since)
        except (TypeError, ValueError):
            return SourceResult("reset", [], str(head), "unparsable cursor; re-baselined")
        if head <= last:
            return SourceResult("ok", [], since, "no change")
        if head - last > MAX_REPLAY_WALK:
            return SourceResult(
                "reset", [], str(head),
                f"gap {head - last} exceeds walk bound {MAX_REPLAY_WALK}; re-baselined")
        seen: set[bytes] = set()
        events: list[Observation] = []
        baseline = self._objects_at(last)
        if baseline is None:
            return SourceResult(
                "reset", [], str(head),
                f"cursor generation {last} unreadable; re-baselined")
        previous = {bytes(item.domain_identity) for item in baseline}
        for gen in range(last + 1, head + 1):
            objects = self._objects_at(gen)
            if objects is None:
                return SourceResult(
                    "reset", [], str(head),
                    f"generation {gen} unreadable; re-baselined")
            current = {bytes(item.domain_identity) for item in objects}
            for identity in sorted(current - previous):
                event = self._classify(identity, objects, gen)
                if event is not None and identity not in seen:
                    seen.add(identity)
                    events.append(event)
                    if len(events) >= MAX_OBSERVATIONS:
                        break
            previous = current
            if len(events) >= MAX_OBSERVATIONS:
                break
        return SourceResult("ok", events, str(head),
                            f"replayed {head - last} generations, {len(events)} observations")

    def _classify(self, identity: bytes, objects: list[Any],
                  generation: int) -> Observation | None:
        try:
            text = identity.decode("utf-8", "replace")
        except Exception:
            return None
        if text.startswith(self.own_prefix):
            return None
        if ":evt:" in text:
            return None  # session logs own event payloads
        cursor = f"{generation}:{digest_hex({'g': generation, 'i': text})[:12]}"
        moment = utcnow()
        if text.startswith("claim:"):
            return Observation(
                source=self.name, stream="claims", cursor=cursor,
                identity=observe_identity(self.name, "claims", cursor, text, "claim.changed"),
                subject=text, observed_at=moment, generation=str(generation),
                kind="claim.changed", severity="notice",
                summary=f"claim record changed: {text}",
                provenance={"domain_identity": text},
                relations={"claim": text.split(":")[1] if ":" in text else text},
                payload={},
            )
        if ":snap:" in text:
            session_id = text.split(":snap:")[0]
            return Observation(
                source=self.name, stream="sessions", cursor=cursor,
                identity=observe_identity(self.name, "sessions", cursor, text, "session.changed"),
                subject=session_id, observed_at=moment, generation=str(generation),
                kind="session.changed", severity="info",
                summary=f"session snapshot advanced: {session_id}",
                provenance={"domain_identity": text},
                relations={"session_id": session_id},
                payload={},
            )
        return Observation(
            source=self.name, stream="external", cursor=cursor,
            identity=observe_identity(self.name, "external", cursor, text, "store.changed"),
            subject=text, observed_at=moment, generation=str(generation),
            kind="store.changed", severity="info",
            summary=f"unclassified store identity appeared: {text[:64]}",
            provenance={"domain_identity": text[:128]},
            relations={},
            payload={},
        )


def _load_commons_client():
    try:
        from mncs_commons.local_service import CommonsClient, default_service_root
    except ImportError as error:
        raise LookupError(f"mncs_commons unavailable: {error}") from error
    return CommonsClient, default_service_root


class CommonsSyncSource(Source):
    """Family coordination ledger via the live Commons sync cursor.

    The Commons cursor is provider-owned and passed through untouched.
    Record meaning stays provider-owned; Environment normalizes identity
    and relations only.
    """

    name = "commons-sync"

    def __init__(self, socket_path: str | Path | None = None):
        self.socket_path = str(socket_path) if socket_path else None

    def observe(self, since: str | None) -> SourceResult:
        try:
            CommonsClient, default_service_root = _load_commons_client()
        except LookupError as error:
            return SourceResult("unknown", [], since, str(error))
        path = self.socket_path or str(default_service_root() / "commons.sock")
        if not Path(path).exists():
            return SourceResult("unknown", [], since, f"commons socket absent: {path}")
        cursor = None
        if since is not None:
            try:
                cursor = json.loads(since)
            except (TypeError, ValueError):
                return SourceResult("reset", [], None, "unparsable cursor; restart sync")
        try:
            client = CommonsClient.connect(path)
            try:
                if since is None:
                    # First contact drains to the tip cursor (bounded pages)
                    # without replaying backlog as new: history predates
                    # observation; sessions catch up on demand.
                    result = {"entries": [], "nextCursor": None, "hasMore": False}
                    for _ in range(5):
                        page = client.sync(cursor=cursor, limit=MAX_OBSERVATIONS)
                        if isinstance(page.get("nextCursor"), dict):
                            cursor = page["nextCursor"]
                        result = page
                        if not page.get("hasMore"):
                            break
                else:
                    result = client.sync(cursor=cursor, limit=MAX_OBSERVATIONS)
            finally:
                client.close()
        except Exception as error:
            return SourceResult("unknown", [], since, f"commons sync failed: {error}")
        entries = result.get("entries", []) if isinstance(result, dict) else []
        next_cursor = result.get("nextCursor") if isinstance(result, dict) else None
        cursor_out = json.dumps(next_cursor, sort_keys=True) if next_cursor else since
        events: list[Observation] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            digest = str(entry.get("entryDigest", ""))
            payload = entry.get("payload", {})
            kind = str(entry.get("entryType", "record"))
            subject = digest or observe_identity(self.name, "ledger", cursor_out or "", kind, kind)
            work_id = ""
            if isinstance(payload, dict):
                work_id = str(payload.get("workId", "") or payload.get("work_id", ""))
            events.append(Observation(
                source=self.name, stream="ledger",
                cursor=cursor_out or "",
                identity=observe_identity(
                    self.name, "ledger", digest or subject, subject, kind),
                subject=work_id or subject, observed_at=utcnow(),
                generation=digest, kind=f"commons.{kind}", severity="info",
                summary=f"commons ledger entry: {kind} {digest[:24]}",
                provenance={"entryDigest": digest},
                relations={"commons_work": work_id} if work_id else {},
                payload={"entryType": kind},
            ))
        if since is None and events:
            # First contact adopts the cursor without replaying backlog as
            # new: history predates observation and sessions catch up on
            # demand. The cursor still advances.
            return SourceResult("ok", [], cursor_out, "baseline adopted")
        detail = f"{len(events)} ledger entries"
        if isinstance(result, dict) and result.get("hasMore"):
            detail += "; backlog remains, next cycle continues"
        return SourceResult("ok", events, cursor_out, detail)


class LanguageServiceSource(Source):
    """Resident semantic deltas via the language-service poll_events RPC.

    Speaks the service's JSON-RPC unix-socket protocol directly (stdlib
    only). No host running, or a stream-identity change, yields UNKNOWN
    or RESET explicitly -- never silence, never invention. Semantic
    meaning stays provider-owned; observations carry identities,
    generations, and diagnostic/obligation deltas by reference.
    """

    name = "language-service"

    def __init__(self, socket_path: str | Path | None = None):
        self.socket_path = str(socket_path) if socket_path else None

    def _call(self, method: str, params: dict[str, Any],
              timeout: float = 20.0) -> Any:
        if not self.socket_path:
            raise ConnectionError("no language-service socket configured")
        if not Path(self.socket_path).exists():
            raise ConnectionError(f"language-service socket absent: {self.socket_path}")
        request = json.dumps({"id": 1, "method": method, "params": params}) + "\n"
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall(request.encode("utf-8"))
            chunks: list[bytes] = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
        finally:
            sock.close()
        response = json.loads(b"".join(chunks).decode("utf-8"))
        if not response.get("ok"):
            raise RuntimeError(f"language-service refused: {response.get('error')}")
        return response.get("result")

    def observe(self, since: str | None) -> SourceResult:
        stream: str | None = None
        after = 0
        if since is not None:
            try:
                saved = json.loads(since)
                stream = saved.get("stream")
                after = int(saved.get("cursor", 0))
            except (TypeError, ValueError):
                return SourceResult("reset", [], None, "unparsable cursor; restart sync")
        try:
            result = self._call("poll_events", {
                "stream_identity": stream, "after_cursor": after,
                "max_events": MAX_OBSERVATIONS})
        except (ConnectionError, RuntimeError, OSError, ValueError) as error:
            return SourceResult("unknown", [], since, f"language-service unreachable: {error}")
        if not isinstance(result, dict):
            return SourceResult("unknown", [], since, "malformed poll_events result")
        if result.get("reset_required"):
            return SourceResult(
                "reset", [], None,
                "provider requires reset (history aged out or stream restarted)")
        current_stream = str(result.get("stream_identity", "") or "")
        if stream is not None and current_stream != stream:
            return SourceResult(
                "reset", [], None,
                "stream identity changed; restart sync")
        current_cursor = int(result.get("current_cursor", after))
        cursor_out = json.dumps({"stream": current_stream, "cursor": current_cursor},
                                sort_keys=True)
        if since is None:
            return SourceResult("ok", [], cursor_out, "baseline adopted")
        events: list[Observation] = []
        for item in result.get("events", []) or []:
            if not isinstance(item, dict):
                continue
            cursor = int(item.get("cursor", current_cursor))
            current_id = item.get("current", {})
            uri = current_id.get("uri", "") if isinstance(current_id, dict) else ""
            identity = current_id.get("identity", "") if isinstance(current_id, dict) else ""
            diag = item.get("diagnostics", {}) if isinstance(item.get("diagnostics"), dict) else {}
            added = diag.get("added", []) if isinstance(diag, dict) else []
            resolved = diag.get("resolved", []) if isinstance(diag, dict) else []
            severity = "warning" if added else "info"
            subject = identity or uri or f"cursor-{cursor}"
            events.append(Observation(
                source=self.name, stream=current_stream, cursor=cursor_out,
                identity=observe_identity(
                    self.name, current_stream, str(cursor), subject, "semantic.changed"),
                subject=subject, observed_at=utcnow(),
                generation=str(item.get("current_generation", "")),
                kind="semantic.changed", severity=severity,
                summary=(f"semantic change at {uri} "
                         f"(+{len(added) if isinstance(added, list) else 0} "
                         f"-{len(resolved) if isinstance(resolved, list) else 0} diagnostics)"),
                provenance={"stream": current_stream, "cursor": cursor},
                relations={"uri": uri, "source_identity": identity},
                payload={
                    "affected": [d.get("uri") for d in
                                 item.get("affected_documents", []) or []
                                 if isinstance(d, dict)][:10],
                    "diagnostics_added": (added if isinstance(added, list) else [])[:20],
                    "diagnostics_resolved": (resolved if isinstance(resolved, list) else [])[:20],
                },
            ))
        return SourceResult("ok", events, cursor_out, f"{len(events)} semantic events")
