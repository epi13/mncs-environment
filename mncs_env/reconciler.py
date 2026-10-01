"""Persistent background reconciliation for session working worlds.

The reconciler runs as a janitor session (consumer
``environment-reconciler``): it polls generic sources, emits only
meaningful deltas as session events, and keeps durable cursors in its
own snapshot so a restart resumes without loss or silent jumps.

Janitor authority is minimal by construction: its intent grants no
writable scope and forbids every mutating action, so capability
invocation through this session denies by default. It reads workspace
facts and provider state, updates its own cursors, and records
observations. It never edits repositories, merges, publishes, deletes
worktrees, reclaims data, or declares another agent's task complete.

Cost discipline: idle cycles perform no persistent writes. Cursors
persist when observations are emitted, on source resets, on fresh cursor
adoption (so a restart never re-baselines past unseen history), every
CURSOR_PERSIST_EVERY quiet cycles, and on clean shutdown. Dedup is
content-keyed over a bounded ring of emitted observation identities.
"""

from __future__ import annotations

import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import sources as sources_module
from .identity import digest_hex

RECONCILER_CONSUMER = "environment-reconciler"
RECONCILER_KIND = "service"

#: Quiet cycles between cursor-only persistence.
CURSOR_PERSIST_EVERY = 30

#: Bound on remembered emitted observation identities.
EMITTED_RING = 500

#: Bound on observations recorded per cycle.
MAX_CYCLE_OBSERVATIONS = 50


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def reconciler_definition(workspace_root: str | Path) -> dict[str, Any]:
    return {
        "name": "environment-reconciler",
        "workspace_root": str(workspace_root),
        "intent": {
            "goal": "background reconciliation of session working worlds",
            "repositories": [],
            "protected_repositories": [],
            "forbidden_actions": ["write", "mutate", "execute", "publish",
                                  "merge", "delete", "delegate", "verify"],
            "authority_requirements": ["read workspace state",
                                       "read provider state",
                                       "update own cursors"],
        },
    }


def build_sources(*, store, workspace_root: str | Path,
                  commons_socket: str | None = None,
                  language_socket: str | None = None,
                  own_session_prefix: str) -> list[sources_module.Source]:
    return [
        sources_module.GitHeadsSource(workspace_root),
        sources_module.StoreReplaySource(store, own_session_prefix),
        sources_module.CommonsSyncSource(commons_socket),
        sources_module.LanguageServiceSource(language_socket),
    ]


def find_session(state_dir: str | Path, store, workspace_root: str | Path | None = None):
    """Open the live reconciler session without writing (status-safe)."""
    from . import sessions as sessions_module
    for session_id in store.list_sessions():
        try:
            candidate = sessions_module.Session.open(
                state_dir=state_dir, session_id=session_id, store=store)
        except Exception:
            continue
        snapshot = candidate.snapshot
        if (snapshot.get("consumer_id") == RECONCILER_CONSUMER
                and (workspace_root is None or snapshot.get("workspace", {}).get("root") == str(Path(workspace_root).resolve()))
                and snapshot.get("lifecycle") not in ("completed", "failed")):
            return candidate
    return None


def open_or_create_session(state_dir: str | Path, store,
                           workspace_root: str | Path):
    """Resume the live reconciler session or create it (restart-safe)."""
    from . import sessions as sessions_module
    candidate = find_session(state_dir, store, workspace_root)
    if candidate is not None:
        if candidate.snapshot.get("lifecycle") == "abandoned":
            candidate.transition("active", "reconciler restarted")
        return candidate, False
    environment = sessions_module.resolve_environment(
        definition=reconciler_definition(workspace_root),
        workspace_root=workspace_root, state_dir=state_dir,
        consumer_id=RECONCILER_CONSUMER, store=store)
    session = sessions_module.Session.create(
        state_dir=state_dir, environment=environment,
        consumer_id=RECONCILER_CONSUMER, consumer_kind=RECONCILER_KIND,
        store=store)
    session.transition("resolving", "reconciler bootstrap")
    session.transition("ready", "reconciler ready")
    session.transition("active", "reconciler active")
    return session, True


def _snapshot_state(session) -> dict[str, Any]:
    state = session.snapshot.get("reconciler")
    if not isinstance(state, dict):
        state = {"cursors": {}, "emitted": [], "cycles": 0,
                 "quiet_cycles": 0, "sources": {}, "last_run": None,
                 "observations_total": 0}
        session.snapshot["reconciler"] = state
    return state


def reconcile_once(session, store, workspace_root: str | Path,
                   *, commons_socket: str | None = None,
                   language_socket: str | None = None) -> dict[str, Any]:
    """One bounded reconciliation pass. Returns a run report (not persisted)."""
    state = _snapshot_state(session)
    cursors = state.setdefault("cursors", {})
    emitted: list[str] = state.setdefault("emitted", [])
    seen = set(emitted)
    report: dict[str, Any] = {
        "at": utcnow(), "observations": [], "resets": [], "unknown": [],
        "errors": [], "cursors_advanced": [], "adopted": [],
    }
    own_prefix = f"{session.session_id}:"
    for source in build_sources(
            store=store, workspace_root=workspace_root,
            commons_socket=commons_socket, language_socket=language_socket,
            own_session_prefix=own_prefix):
        previous = cursors.get(source.name)
        try:
            result = source.observe(previous)
        except Exception as error:
            report["errors"].append({"source": source.name, "error": str(error)})
            state.setdefault("sources", {})[source.name] = {
                "status": "unknown", "detail": f"observer crashed: {error}"}
            continue
        state.setdefault("sources", {})[source.name] = {
            "status": result.status, "detail": result.detail,
            "at": utcnow()}
        if result.status == "unknown":
            report["unknown"].append({"source": source.name, "detail": result.detail})
            continue
        if result.status == "reset":
            report["resets"].append({"source": source.name, "detail": result.detail})
            session._emit("reconciler.source-reset", RECONCILER_CONSUMER,
                          {"source": source.name, "detail": result.detail})
            if result.cursor is not None:
                cursors[source.name] = result.cursor
            continue
        fresh = [event for event in result.events if event.identity not in seen]
        for event in fresh[:MAX_CYCLE_OBSERVATIONS]:
            session._emit("reconciler.observed", source.name,
                          {"observation": event.identity, "kind": event.kind,
                           "subject": event.subject, "severity": event.severity,
                           "summary": event.summary,
                           "provenance": event.provenance,
                           "relations": event.relations,
                           "payload": event.payload})
            seen.add(event.identity)
            emitted.append(event.identity)
            report["observations"].append(
                {"source": source.name, "kind": event.kind,
                 "subject": event.subject, "summary": event.summary})
        if result.cursor is not None and result.cursor != previous:
            if previous is None:
                # Fresh baseline adoption: persist even a quiet cycle, or a
                # restart before the next persist would re-baseline at a
                # later head and silently drop the interim history.
                report["adopted"].append(source.name)
            cursors[source.name] = result.cursor
            report["cursors_advanced"].append(source.name)
    del emitted[:max(0, len(emitted) - EMITTED_RING)]
    state["cycles"] = int(state.get("cycles", 0)) + 1
    state["observations_total"] = int(state.get("observations_total", 0)) + len(
        report["observations"])
    state["last_run"] = {"at": report["at"],
                         "observations": len(report["observations"]),
                         "resets": len(report["resets"]),
                         "unknown": len(report["unknown"]),
                         "errors": len(report["errors"])}
    meaningful = bool(report["observations"] or report["resets"] or report["errors"])
    # Cursor advances over already-skipped generations (own writes, session
    # event payloads) stay memory-only: persisting them would chase the
    # save's own generation forever. Fresh adoptions persist so a restart
    # never re-baselines past unseen history.
    if meaningful or report["adopted"]:
        state["quiet_cycles"] = 0
        session._save()
    else:
        state["quiet_cycles"] = int(state.get("quiet_cycles", 0)) + 1
        if state["quiet_cycles"] >= CURSOR_PERSIST_EVERY:
            state["quiet_cycles"] = 0
            session._save()
    report["persisted"] = meaningful or bool(report["adopted"]) or state["quiet_cycles"] == 0
    return report


def session_health(session) -> dict[str, Any]:
    """Structured machine status for status/doctor surfaces."""
    state = session.snapshot.get("reconciler", {})
    return {
        "session_id": session.session_id,
        "lifecycle": session.snapshot.get("lifecycle"),
        "consumer_id": session.snapshot.get("consumer_id"),
        "cycles": state.get("cycles", 0),
        "observations_total": state.get("observations_total", 0),
        "last_run": state.get("last_run"),
        "sources": state.get("sources", {}),
        "cursors": sorted(state.get("cursors", {})),
    }


class Daemon:
    """Foreground reconciler loop with PID file, bounded logs, clean shutdown."""

    def __init__(self, *, state_dir: str | Path, workspace_root: str | Path,
                 interval_seconds: float = 60.0,
                 commons_socket: str | None = None,
                 language_socket: str | None = None,
                 pid_file: str | Path | None = None,
                 log_file: str | Path | None = None):
        self.state_dir = Path(state_dir)
        self.workspace_root = workspace_root
        self.interval = max(5.0, float(interval_seconds))
        self.commons_socket = commons_socket
        self.language_socket = language_socket
        run_dir = self.state_dir / "reconciler"
        self.pid_file = Path(pid_file) if pid_file else run_dir / "reconciler.pid"
        self.log_file = str(log_file) if log_file else str(run_dir / "reconciler.log")
        self._stop = False

    def _setup_logging(self) -> logging.Logger:
        from logging.handlers import RotatingFileHandler
        Path(self.log_file).parent.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("mncs.env.reconciler")
        logger.setLevel(logging.INFO)
        logger.handlers.clear()
        handler = RotatingFileHandler(self.log_file, maxBytes=1_000_000, backupCount=3)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
        logger.addHandler(handler)
        return logger

    def run(self) -> int:
        from .session_store import open_store
        logger = self._setup_logging()
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                import os
                os.kill(pid, 0)
                logger.error(f"reconciler already running (pid {pid})")
                return 2
            except (ValueError, OSError):
                pass
        import os
        self.pid_file.write_text(str(os.getpid()))
        logger.info("reconciler starting interval=%s state=%s", self.interval, self.state_dir)

        def _handle(signum, _frame):
            logger.info(f"reconciler received signal {signum}; shutting down")
            self._stop = True

        signal.signal(signal.SIGTERM, _handle)
        signal.signal(signal.SIGINT, _handle)
        store = open_store(self.state_dir, "store")
        try:
            session, created = open_or_create_session(
                self.state_dir, store, self.workspace_root)
            logger.info(f"reconciler session {session.session_id} "
                        f"({'created' if created else 'resumed'})")
            # Self-triggered startup: one immediate pass discovers what
            # changed while the service was away.
            report = reconcile_once(
                session, store, self.workspace_root,
                commons_socket=self.commons_socket,
                language_socket=self.language_socket)
            logger.info(f"startup pass: {len(report['observations'])} observations, "
                        f"{len(report['resets'])} resets, {len(report['unknown'])} unknown")
            while not self._stop:
                time.sleep(self.interval)
                if self._stop:
                    break
                try:
                    report = reconcile_once(
                        session, store, self.workspace_root,
                        commons_socket=self.commons_socket,
                        language_socket=self.language_socket)
                    logger.info(
                        f"cycle {session.snapshot.get('reconciler', {}).get('cycles')}: "
                        f"{len(report['observations'])} obs, "
                        f"{len(report['unknown'])} unknown")
                except Exception as error:
                    logger.exception(f"reconcile cycle failed: {error}")
            # Clean shutdown persists cursors exactly once.
            session._save()
            logger.info("reconciler stopped cleanly")
            return 0
        finally:
            try:
                store.close()
            except Exception:
                pass
            try:
                self.pid_file.unlink()
            except OSError:
                pass

    def status(self) -> dict[str, Any]:
        """Structured machine status without requiring the daemon to run."""
        import os
        running: dict[str, Any] = {"running": False}
        try:
            pid = int(self.pid_file.read_text().strip())
            os.kill(pid, 0)
            running = {"running": True, "pid": pid}
        except (OSError, ValueError):
            pass
        logs: list[str] = []
        try:
            lines = Path(self.log_file).read_text().splitlines()
            logs = lines[-5:]
        except OSError:
            pass
        result: dict[str, Any] = {
            "daemon": running, "interval_seconds": self.interval,
            "state_dir": str(self.state_dir),
            "workspace_root": str(self.workspace_root),
            "recent_log": logs,
        }
        try:
            from .session_store import open_store
            store = open_store(self.state_dir, "store")
            try:
                session = find_session(self.state_dir, store, self.workspace_root)
                if session is None:
                    result["session"] = None
                else:
                    result["session"] = session_health(session)
            finally:
                store.close()
        except Exception as error:
            result["session_error"] = str(error)
        return result
