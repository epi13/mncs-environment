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

#: Maximum observations admitted from one source per cycle. Keep this equal
#: to the reconciler's per-source emit bound so a provider cursor never moves
#: past observations that the owner did not durably publish.
MAX_OBSERVATIONS = 50
# First contact establishes an authoritative baseline without replaying old
# Commons ledger entries as new work. The page count is bounded, but a ledger
# beyond that bound is reported unknown and leaves the consumer cursor
# untouched so it cannot silently adopt a partial tip.
MAX_COMMONS_BASELINE_PAGES = 64

#: Maximum Store generation gap inspected in one replay. Store's canonical
#: implementation compares the immutable cursor and head snapshots once, so
#: the bound no longer multiplies a full Store scan per generation.
MAX_REPLAY_WALK = 4096

# These records are Environment's own durable products. Their Store
# publications must not recursively invalidate the owners that wrote them.
# They remain fully readable and verifiable through their owning contracts.
_DERIVED_STORE_SCHEMAS = {
    b"mncs.environment.verification-evidence/1",
    b"mncs.provider-execution-provenance/1",
}


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
        identities_at = getattr(self.store, "domain_bindings_at", None)
        if callable(identities_at):
            try:
                from types import SimpleNamespace
                return [SimpleNamespace(domain_schema=schema, domain_identity=identity)
                        for schema, identity in identities_at(generation)]
            except Exception:
                return None
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
        delta_at = getattr(self.store, "domain_bindings_since", None)
        if callable(delta_at):
            try:
                deltas = delta_at(last, max_generations=MAX_REPLAY_WALK)
            except Exception as error:
                return SourceResult(
                    "unknown", [], since,
                    f"bounded Store identity delta unavailable; cursor held: {error}")
            events: list[Observation] = []
            seen: set[tuple[bytes, bytes]] = set()
            for generation, schema, identity in deltas:
                binding = (bytes(schema), bytes(identity))
                event = self._classify(binding[1], [], int(generation), binding[0])
                if event is not None and binding not in seen:
                    seen.add(binding)
                    events.append(event)
                    if len(events) >= MAX_OBSERVATIONS:
                        return SourceResult(
                            "reset", [], str(head),
                            "observation bound exceeded; reconcile before adopting head")
            return SourceResult(
                "ok", events, str(head),
                f"replayed {head - last} generations from committed Store deltas, {len(events)} observations")
        seen: set[tuple[bytes, bytes]] = set()
        events: list[Observation] = []
        baseline = self._objects_at(last)
        if baseline is None:
            return SourceResult(
                "unknown", [], since,
                f"cursor generation {last} unreadable; cursor held")
        previous = {(bytes(getattr(item, "domain_schema", b"")), bytes(item.domain_identity)) for item in baseline}
        for gen in range(last + 1, head + 1):
            objects = self._objects_at(gen)
            if objects is None:
                return SourceResult(
                    "unknown", [], since,
                    f"generation {gen} unreadable; cursor held")
            current = {(bytes(getattr(item, "domain_schema", b"")), bytes(item.domain_identity)) for item in objects}
            for binding in sorted(current - previous):
                schema, identity = binding
                event = self._classify(identity, objects, gen, schema)
                if event is not None and binding not in seen:
                    seen.add(binding)
                    events.append(event)
                    if len(events) >= MAX_OBSERVATIONS:
                        return SourceResult("reset", [], str(head),
                                            "observation bound exceeded; reconcile before adopting head")
            previous = current
            if len(events) >= MAX_OBSERVATIONS:
                break
        return SourceResult("ok", events, str(head),
                            f"replayed {head - last} generations, {len(events)} observations")

    def _classify(self, identity: bytes, objects: list[Any],
                  generation: int, schema: bytes = b"") -> Observation | None:
        try:
            text = identity.decode("utf-8", "replace")
        except Exception:
            return None
        if schema in _DERIVED_STORE_SCHEMAS:
            return None
        if (schema == b"mncs.environment.projection-state/1"
                and text.startswith("mncs-")):
            return None
        if text.startswith(self.own_prefix):
            return None
        if ":evt:" in text:
            return None  # session logs own event payloads
        cursor = f"{generation}:{digest_hex({'g': generation, 'i': text, 'schema': schema.hex()})[:12]}"
        moment = utcnow()
        if text.startswith("claim:"):
            parts = text.split(":")
            repository = (parts[2] if len(parts) > 2 and parts[1] == "claim"
                          else parts[1] if len(parts) > 1 else text)
            return Observation(
                source=self.name, stream="claims", cursor=cursor,
                identity=observe_identity(self.name, "claims", cursor, text, "claim.changed"),
                subject=text, observed_at=moment, generation=str(generation),
                kind="claim.changed", severity="notice",
                summary=f"claim record changed: {text}",
                provenance={"domain_identity": text},
                relations={"claim": repository},
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
            provenance={"domain_identity": text, "domain_schema": schema.decode("utf-8", "replace")},
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
        baseline_adopted = since is None
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
                    complete = False
                    for _ in range(MAX_COMMONS_BASELINE_PAGES):
                        page = client.sync(cursor=cursor, limit=MAX_OBSERVATIONS)
                        next_cursor = page.get("nextCursor")
                        if not isinstance(next_cursor, dict):
                            return SourceResult(
                                "unknown", [], since,
                                "Commons first-contact baseline returned no authoritative cursor")
                        if page.get("hasMore") and next_cursor == cursor:
                            return SourceResult(
                                "unknown", [], since,
                                "Commons first-contact baseline cursor did not advance")
                        cursor = next_cursor
                        result = page
                        if not page.get("hasMore"):
                            complete = True
                            break
                    if not complete:
                        return SourceResult(
                            "unknown", [], since,
                            f"Commons first-contact baseline exceeds {MAX_COMMONS_BASELINE_PAGES} pages; retry after bounded reconciliation")
                else:
                    result = client.sync(cursor=cursor, limit=MAX_OBSERVATIONS)
            finally:
                client.close()
        except Exception as error:
            return SourceResult("unknown", [], since, f"commons sync failed: {error}")
        # First contact intentionally adopts the ledger tip. Entries read
        # while advancing to that tip predate this consumer and are not work.
        entries = ([] if baseline_adopted else
                   result.get("entries", []) if isinstance(result, dict) else [])
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
                if (not isinstance(saved, dict)
                        or not isinstance(saved.get("stream"), str)
                        or not saved.get("stream")
                        or type(saved.get("cursor")) is not int
                        or saved["cursor"] < 0):
                    raise ValueError("invalid stream cursor")
                stream = saved["stream"]
                after = saved["cursor"]
            except (AttributeError, TypeError, ValueError):
                return SourceResult("reset", [], None, "unparsable cursor; restart sync")
        # Converge the resident to disk truth before polling, mirroring
        # the provider probe: shell-made edits bypass LSP notifications,
        # so polling a stale resident would report quiet as current. A
        # refused refresh (one already running) is tolerated.
        try:
            self._call("refresh_workspace", {})
        except (ConnectionError, RuntimeError, OSError, ValueError):
            pass
        try:
            params: dict[str, Any] = {"after_cursor": after,
                                      "max_events": MAX_OBSERVATIONS}
            if stream is not None:
                params["stream_identity"] = stream
            result = self._call("poll_events", params)
        except (ConnectionError, RuntimeError, OSError, ValueError) as error:
            return SourceResult("unknown", [], since, f"language-service unreachable: {error}")
        if not isinstance(result, dict):
            return SourceResult("unknown", [], since, "malformed poll_events result")
        current_stream = str(result.get("stream_identity", "") or "")
        current_cursor = result.get("current_cursor")
        oldest_cursor = result.get("oldest_cursor")
        reset_flag = result.get("reset_required")
        if (not current_stream or type(current_cursor) is not int or current_cursor < 0
                or type(oldest_cursor) is not int or oldest_cursor < 1
                or type(reset_flag) is not bool):
            return SourceResult("unknown", [], since,
                                "malformed Language Service stream/cursor identity")
        response_after = result.get("after_cursor")
        if type(response_after) is not int or response_after != after:
            return SourceResult("unknown", [], since,
                                "Language Service poll response does not match requested cursor")
        reset_required = reset_flag or current_cursor < after
        if reset_required:
            # A reset is a request for bounded owner reconciliation. Preserve
            # the provider's exact high-water candidate so the caller can
            # acknowledge it only after that reconciliation succeeds. Without
            # this candidate, expired or restarted streams reset forever; the
            # old cursor must never be reinterpreted in the new stream.
            recovery_cursor = json.dumps(
                {"stream": current_stream, "cursor": current_cursor}, sort_keys=True)
            return SourceResult(
                "reset", [], recovery_cursor,
                f"provider requires bounded semantic reconciliation: stream={current_stream}, "
                f"current_cursor={current_cursor}, oldest_cursor={oldest_cursor}")
        if stream is not None and current_stream != stream:
            recovery_cursor = json.dumps(
                {"stream": current_stream, "cursor": current_cursor}, sort_keys=True)
            return SourceResult(
                "reset", [], recovery_cursor,
                f"stream identity changed; bounded semantic reconciliation required: "
                f"stream={current_stream}, current_cursor={current_cursor}")
        raw_events = result.get("events", [])
        if not isinstance(raw_events, list) or len(raw_events) > MAX_OBSERVATIONS:
            return SourceResult("unknown", [], since,
                                "malformed or over-bound poll_events page")
        page_cursor = after
        for item in raw_events:
            if not isinstance(item, dict) or type(item.get("cursor")) is not int:
                return SourceResult("unknown", [], since,
                                    "poll_events returned an invalid event cursor")
            item_cursor = item["cursor"]
            if item_cursor <= page_cursor or item_cursor > current_cursor:
                return SourceResult("unknown", [], since,
                                    "poll_events event cursors are not strictly ordered")
            page_cursor = item_cursor
        if since is not None and current_cursor > after and not raw_events:
            recovery_cursor = json.dumps(
                {"stream": current_stream, "cursor": current_cursor}, sort_keys=True)
            return SourceResult(
                "reset", [], recovery_cursor,
                "stream cursor advanced without a replay page; bounded semantic reconciliation required")
        if (since is not None and raw_events and page_cursor < current_cursor
                and len(raw_events) < MAX_OBSERVATIONS):
            recovery_cursor = json.dumps(
                {"stream": current_stream, "cursor": current_cursor}, sort_keys=True)
            return SourceResult(
                "reset", [], recovery_cursor,
                "replay page ends before the provider high-water; bounded semantic reconciliation required")
        acknowledged_cursor = current_cursor if since is None or not raw_events else page_cursor
        cursor_out = json.dumps({"stream": current_stream, "cursor": acknowledged_cursor},
                                sort_keys=True)
        if since is None:
            return SourceResult("ok", [], cursor_out, "baseline adopted")
        events: list[Observation] = []
        for item in raw_events:
            cursor = item["cursor"]
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
                    "semantic_subjects": (item.get("semantic_subjects") or [])[:64],
                    "obligations": item.get("obligations") or {},
                    "impact": item.get("impact") or {},
                    "impact_identity": item.get("impact_identity"),
                    "impact_complete": bool(item.get("impact_complete")) and len(item.get("semantic_subjects") or []) <= 64,
                    "affected": [d.get("uri") for d in
                                 item.get("affected_documents", []) or []
                                 if isinstance(d, dict)][:10],
                    "diagnostics_added": (added if isinstance(added, list) else [])[:20],
                    "diagnostics_resolved": (resolved if isinstance(resolved, list) else [])[:20],
                },
            ))
        detail = f"{len(events)} semantic events"
        if acknowledged_cursor < current_cursor:
            detail += "; backlog remains"
        return SourceResult("ok", events, cursor_out, detail)
