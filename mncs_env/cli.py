"""mncs-env: thin inspection/control surface over the session system.

Every command reads or mutates environment-owned session state only. The
CLI parses arguments and prints JSON; all semantics live in the package.
Inspection commands open sessions read-only and never append events;
only resume/accept record participation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import closing
from pathlib import Path

from . import actions as actions_module
from . import capabilities as capabilities_module
from . import diagnostics as diagnostics_module
from . import doctor as doctor_module
from . import projections as projections_module
from . import verification as verification_module
from . import entry as entry_module
from . import claims as claims_module
from . import pressures as pressures_module
from . import rights as rights_module
from . import sessions as sessions_module
from . import workspace as workspace_module
from .persist import read_json
from .session_store import open_store, SnapshotConflict
from .store_backend import StoreUnavailable, StoreIntegrityFailure

DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "mncs-environment"


def out(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def fail(
    message: str,
    code: int = 2,
    *,
    diagnostics: dict | None = None,
) -> int:
    payload = {"error": message}
    if diagnostics:
        payload["diagnostics"] = diagnostics
    print(json.dumps(payload), file=sys.stderr)
    return code


def load_definition(path: Path) -> dict:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise SystemExit(fail(f"{path} is not a JSON object"))
    return raw


def resolve_workspace_root(args: argparse.Namespace, definition: dict) -> str:
    """Workspace root for a command.

    A relative ``workspace_root`` in the definition resolves against the
    definition file's directory. Provider-bound definitions must receive an
    explicit root from their caller because the provider owns workspace
    selection; inferring it from the definition's checkout can select the
    wrong workspace when that checkout is nested or isolated.
    """
    if args.workspace:
        return args.workspace
    scope = definition.get("workspace_scope")
    if definition.get("workspace_provider") or (
        isinstance(scope, dict) and scope.get("kind") == "campaign"
    ):
        raise ValueError(
            "campaign-scoped definitions require an explicit --workspace root"
        )
    declared = definition.get("workspace_root", ".")
    if not isinstance(declared, str) or not declared:
        return "."
    candidate = Path(declared)
    if candidate.is_absolute():
        return str(candidate)
    definition_file = getattr(args, "definition", None)
    if definition_file is not None:
        return os.path.normpath(Path(definition_file).resolve().parent / candidate)
    return str(candidate)


def cmd_resolve(args: argparse.Namespace) -> int:
    definition = load_definition(args.definition)
    try:
        workspace_root = resolve_workspace_root(args, definition)
        workspace_root = str(workspace_module.validate_workspace_root(
            workspace_root, definition=definition
        ))
        environment = sessions_module.resolve_environment(
            definition=definition,
            workspace_root=workspace_root,
            state_dir=args.state_dir,
            consumer_id=args.consumer,
            backend=args.persistence,
        )
    except workspace_module.WorkspaceResolutionError as error:
        return fail(str(error), diagnostics=error.diagnostics)
    except ValueError as error:
        return fail(str(error), diagnostics={"code": "entry-invalid", "next": "check the selected definition and workspace"})
    out(environment)
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    try:
        definition, definition_path = entry_module.select_definition(args.definition, args.workspace)
        args.definition = definition_path
        workspace_root = resolve_workspace_root(args, definition)
        result = entry_module.enter(definition=definition, definition_path=definition_path,
                               workspace_root=workspace_root, state_dir=args.state_dir,
                               backend=args.persistence, consumer_id=args.consumer,
                               consumer_kind=args.consumer_kind, new_session=args.new_session)
        out(result)
        return 5 if result["readiness"]["status"] == "blocked" else 0
    except (workspace_module.WorkspaceResolutionError, entry_module.EntryError) as error:
        return fail(str(error), diagnostics=error.diagnostics)
    except ValueError as error:
        return fail(str(error), diagnostics={"code": "entry-invalid", "next": "check the selected definition and workspace"})
    except rights_module.RightsBlocked as error:
        return fail(str(error), diagnostics={"code": "rights-blocked", "provider": "mncs-rights-provenance",
                                           "next": "inspect provider-owned rights claims for the selected workspace"})


def close_store(store) -> None:
    close = getattr(store, "close", None)
    if callable(close):
        close()


def _open(args: argparse.Namespace) -> sessions_module.Session:
    try:
        return sessions_module.Session.open(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        raise SystemExit(fail(str(error)))


def cmd_inspect(args: argparse.Namespace) -> int:
    with closing(_open(args)) as session:
        out(session.inspect())
    return 0


def cmd_context(args: argparse.Namespace) -> int:
    """Print the compact, read-only first-use session context."""
    with closing(_open(args)) as session:
        out(session.status())
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    session = _open(args)
    try:
        result = session.health(live=args.live)
        out(result)
        return 5 if result["readiness"]["status"] == "blocked" else 0
    finally:
        session.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    if args.evidence:
        with closing(_open(args)) as session:
            out(doctor_module.evidence(session))
        return 0
    if args.scope == "repository":
        if not args.checkout:
            return fail("repository remediation needs --checkout <repository>",
                        diagnostics={"code": "remediation-scope-unknown"})
        with entry_module.entry_lock(args.state_dir, args.persistence):
            try:
                session = sessions_module.Session.resume(
                    state_dir=args.state_dir, session_id=args.session, backend=args.persistence)
            except sessions_module.LifecycleError as error:
                return fail(str(error))
            try:
                result = doctor_module.remediate_repository(
                    session, args.checkout, dry_run=args.dry_run,
                    changed_paths=args.changed_path)
            except doctor_module.RemediationRefused as error:
                return fail(str(error), code=3, diagnostics=error.diagnostics)
            except claims_module.ClaimAdoptionRequired as error:
                return fail(str(error), code=3,
                            diagnostics={"code": "remediation-adoption-required",
                                         "facts": error.facts})
            except (sessions_module.AuthorityDenied, sessions_module.LifecycleError,
                    capabilities_module.CapabilityError) as error:
                return fail(str(error), code=3)
            finally:
                session.close()
            out(result)
            return 0
    with entry_module.entry_lock(args.state_dir, args.persistence) as lock:
        try:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence)
        except sessions_module.LifecycleError as error:
            return fail(str(error))
        try:
            result = doctor_module.ambient_pass(
                session, lock_waited=float(lock.get("waited_seconds", 0.0)))
            payload = {"session_id": args.session, "summary": result["summary"],
                       "remaining": result["remaining"],
                       "remaining_truncated": result.get("remaining_truncated", False),
                       "unavailable": result.get("unavailable", {"count": 0, "digest": None}),
                       "readiness": result["readiness"], "epoch": result["digest"],
                       "reused": result["reused"],
                       "elapsed_seconds": result.get("elapsed_seconds")}
            out(payload)
            return 5 if result["summary"]["blockers"] else 0
        finally:
            session.close()


def cmd_projections(args: argparse.Namespace) -> int:
    if args.evidence:
        with closing(_open(args)) as session:
            out(projections_module.read_evidence(session))
        return 0
    if args.watch is not None:
        return cmd_projections_watch(args)
    with entry_module.entry_lock(args.state_dir, args.persistence):
        try:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence)
        except sessions_module.LifecycleError as error:
            return fail(str(error))
        try:
            if args.apply:
                result = projections_module.ambient_pass(
                    session, mode="explicit", only=args.apply)
            else:
                result = projections_module.ambient_pass(session)
            payload = {"session_id": args.session,
                       "summary": result["summary"],
                       "reused": result["reused"],
                       "evidence": result.get("evidence")}
            out(payload)
            return 5 if result["summary"]["blockers"] else 0
        finally:
            session.close()


def cmd_projections_watch(args: argparse.Namespace) -> int:
    """Resident epoch-gated reconciliation: notice change without re-entry.

    Each tick re-resumes the session and runs one ambient pass, so
    commits, claim releases, and verification resolutions made by any
    agent are noticed on the next tick. Quiet ticks reuse the epoch
    (no renders, no native calls); the lock is held per tick, never
    across the sleep, so concurrent agents are never blocked.
    """
    import time as _time

    try:
        interval = float(args.watch)
    except (TypeError, ValueError):
        return fail("watch interval must be a number of seconds")
    if interval < 1.0:
        return fail("watch interval must be at least 1 second")
    iterations = args.watch_iterations
    if iterations is not None and iterations < 1:
        return fail("watch iterations must be positive")
    tick = 0
    blockers = 0
    try:
        while iterations is None or tick < iterations:
            with entry_module.entry_lock(args.state_dir, args.persistence):
                try:
                    session = sessions_module.Session.resume(
                        state_dir=args.state_dir, session_id=args.session,
                        backend=args.persistence)
                except sessions_module.LifecycleError as error:
                    return fail(str(error))
                try:
                    result = projections_module.ambient_pass(session)
                finally:
                    session.close()
            summary = result["summary"]
            blockers = summary.get("blockers", 0)
            # Compact JSON lines (not the pretty `out` envelope) so a
            # supervising agent can stream ticks incrementally.
            print(json.dumps({"session_id": args.session,
                              "iteration": tick, "summary": summary,
                              "reused": result["reused"]},
                             sort_keys=True), flush=True)
            tick += 1
            if iterations is None or tick < iterations:
                _time.sleep(interval)
    except KeyboardInterrupt:
        pass
    return 5 if blockers else 0


def cmd_verification(args: argparse.Namespace) -> int:
    if args.evidence:
        with closing(_open(args)) as session:
            out(verification_module.read_evidence(session))
        return 0
    with entry_module.entry_lock(args.state_dir, args.persistence):
        try:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence)
        except sessions_module.LifecycleError as error:
            return fail(str(error))
        try:
            if args.only:
                result = verification_module.ambient_pass(
                    session, mode="explicit", only=args.only)
            elif args.full:
                result = verification_module.ambient_pass(
                    session, mode="full",
                    max_executions=verification_module.MAX_OBLIGATIONS)
            else:
                result = verification_module.ambient_pass(session)
            payload = {"session_id": args.session,
                       "summary": result["summary"],
                       "reused": result["reused"],
                       "evidence": result.get("evidence")}
            out(payload)
            return 5 if result["summary"]["blockers"] else 0
        finally:
            session.close()


def cmd_diagnostic(args: argparse.Namespace) -> int:
    if args.evidence:
        with closing(_open(args)) as session:
            out(diagnostics_module.read_evidence(session))
        return 0
    with entry_module.entry_lock(args.state_dir, args.persistence):
        try:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence)
        except sessions_module.LifecycleError as error:
            return fail(str(error))
        try:
            depth = args.depth or "minimal"
            if args.only:
                result = diagnostics_module.ambient_pass(
                    session, mode="explicit", depth=depth, only=args.only)
            elif args.full:
                result = diagnostics_module.ambient_pass(
                    session, mode="full", depth=depth,
                    max_captures=diagnostics_module.MAX_FAILURES)
            else:
                result = diagnostics_module.ambient_pass(session)
            payload = {"session_id": args.session,
                       "summary": result["summary"],
                       "reused": result["reused"],
                       "evidence": result.get("evidence")}
            out(payload)
            return 5 if result["summary"]["blockers"] else 0
        finally:
            session.close()


def cmd_actions(args: argparse.Namespace) -> int:
    if args.evidence:
        with closing(_open(args)) as session:
            out(actions_module.read_evidence(session))
        return 0
    with entry_module.entry_lock(args.state_dir, args.persistence):
        try:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence)
        except sessions_module.LifecycleError as error:
            return fail(str(error))
        try:
            if args.only:
                result = actions_module.ambient_pass(
                    session, mode="explicit", only=args.only,
                    dispatch=args.dispatch)
            elif args.full:
                result = actions_module.ambient_pass(
                    session, mode="full",
                    max_dispatches=actions_module.MAX_OBLIGATIONS,
                    dispatch=args.dispatch)
            else:
                result = actions_module.ambient_pass(
                    session, dispatch=args.dispatch)
            payload = {"session_id": args.session,
                       "summary": result["summary"],
                       "reused": result["reused"],
                       "evidence": result.get("evidence")}
            out(payload)
            return 5 if result["summary"]["blockers"] else 0
        finally:
            session.close()


def cmd_reconcile(args: argparse.Namespace) -> int:
    with entry_module.entry_lock(args.state_dir, args.persistence):
        session = sessions_module.Session.resume(state_dir=args.state_dir, session_id=args.session, backend=args.persistence)
        try:
            result = session.reconcile()
            out(result)
            return 5 if result["readiness"]["status"] == "blocked" else 0
        finally:
            session.close()


def cmd_status(args: argparse.Namespace) -> int:
    """Print compact session status, or list session ids when none is given."""
    if args.session and args.terse:
        fast = doctor_module.serve_terse_fast(args.state_dir, args.session)
        if fast is not None:
            out(fast)
            return 0
        with closing(_open(args)) as session:
            snapshot = session.snapshot
            recorded = snapshot.get("doctor", {}).get("epoch", {})
            out({"session_id": args.session, "observation": "snapshot",
                 "epoch": recorded.get("digest"), "validated_at": recorded.get("validated_at"),
                 "summary": recorded.get("summary"),
                 "remaining": recorded.get("remaining", []),
                 "remaining_truncated": recorded.get("remaining_truncated", False),
                 "unavailable": recorded.get("unavailable", {"count": 0, "digest": None}),
                 "readiness": recorded.get("readiness"),
                 "lifecycle": snapshot.get("lifecycle"),
                 "note": "epoch stale or missing; run doctor for a fresh validated pass"})
            return 0
    if args.session:
        with closing(_open(args)) as session:
            out(session.status())
        return 0
    store = open_store(args.state_dir, args.persistence)
    try:
        session_ids = store.list_sessions()
        out({
            "schema_version": "mncs.environment.status/1",
            "sessions": session_ids,
            "session_count": len(session_ids),
        })
        return 0
    finally:
        close_store(store)


def cmd_resume(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    if args.revalidate:
        revalidation = session.revalidate()
        workspace_root = (
            args.workspace
            or session.snapshot.get("workspace", {}).get("root")
            or "."
        )
        session.observe_workspace(workspace_root)
    else:
        revalidation = {"reprobed": 0, "changed": []}
    result = session.inspect()
    result["revalidation"] = revalidation
    result["catchup"] = session.catchup()
    if args.consumer and args.consumer != session.snapshot.get("consumer_id"):
        result["handoff_hint"] = (
            f"session belongs to {session.snapshot.get('consumer_id')}; "
            "use accept with the handoff identity to transfer"
        )
    out(result)
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    with closing(_open(args)) as session:
        out(session.inspect()["bindings"])
    return 0


def cmd_authority(args: argparse.Namespace) -> int:
    session = _open(args)
    result = session.inspect()["authority"]
    if args.action:
        result = {
            "evaluation": session.check(action=args.action, target=args.target or "*"),
            "context": result,
        }
    out(result)
    return 0


def cmd_invoke(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    try:
        result = session.invoke(
            args.capability, args.argv, cwd=args.cwd, timeout_seconds=args.timeout,
            output_limit_bytes=args.output_limit_bytes,
        )
    except (sessions_module.AuthorityDenied, sessions_module.LifecycleError,
            capabilities_module.CapabilityError) as error:
        return fail(str(error), code=3)
    out(result)
    return 0 if result["status"] in ("ok", "pending-escalation") else 4


def cmd_events(args: argparse.Namespace) -> int:
    session = _open(args)
    if args.subscribe:
        # Subscribing changes cursor state: use a participating handle.
        participant = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
        out(participant.subscribe(args.subscribe, source_filter=args.source))
        return 0
    if args.poll:
        participant = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
        out(participant.poll(args.poll))
        return 0
    if args.observe_workspace:
        participant = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
        out(participant.observe_workspace(args.observe_workspace))
        return 0
    if args.observe_store:
        participant = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
        out(participant.observe_store())
        return 0
    out(session.inspect()["latest_events"])
    return 0


def cmd_brief(args: argparse.Namespace) -> int:
    out(_open(args).brief())
    return 0


def cmd_updates(args: argparse.Namespace) -> int:
    out(_open(args).updates(since=args.since))
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    out(session.ack(args.index))
    return 0


def cmd_reconciler(args: argparse.Namespace) -> int:
    import os

    from . import reconciler as reconciler_module
    store = open_store(args.state_dir, args.persistence)
    try:
        workspace_root = (args.workspace or
                          Path(args.state_dir).resolve().parent)
        commons_socket = args.commons_socket or os.environ.get("MNCS_COMMONS_SOCKET")
        language_socket = args.language_socket or os.environ.get("MNLS_SERVICE_SOCKET")
        daemon = reconciler_module.Daemon(
            state_dir=args.state_dir, workspace_root=workspace_root,
            interval_seconds=args.interval,
            commons_socket=commons_socket,
            language_socket=language_socket)
        if args.status:
            out(daemon.status())
            return 0
        if args.run:
            return daemon.run()
        session, _ = reconciler_module.open_or_create_session(
            args.state_dir, store, workspace_root)
        report = reconciler_module.reconcile_once(
            session, store, workspace_root,
            commons_socket=commons_socket,
            language_socket=language_socket)
        out(report)
        return 0
    finally:
        close_store(store)


def cmd_checkpoint(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    out(session.checkpoint(progress=args.progress, remaining=args.remaining))
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    out(
        session.handoff(
            to_consumer=args.to,
            notes=args.notes,
            blockers=args.blockers,
            next_actions=args.next,
        )
    )
    return 0


def cmd_accept(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.open(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    try:
        out(session.accept_handoff(args.handoff, consumer_id=args.consumer,
                                   consumer_kind=args.consumer_kind))
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    try:
        out(session.complete(outcome=args.outcome, summary=args.summary))
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    return 0


def cmd_fail(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    try:
        out(session.fail(reason=args.reason))
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    store = open_store(args.state_dir, args.persistence, session_id=args.session)
    try:
        if args.release:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence,
                store=store,
            )
            out({"released": session.release_claim(
                args.release, reason=args.reason, claim_id=args.claim_id)})
            return 0
        if args.transfer_to:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence,
                store=store,
            )
            if not args.claim_id:
                return fail("transfer needs --claim-id")
            try:
                out(session.transfer_claim(
                    args.claim_id, args.transfer_to, args.transfer_consumer,
                    reason=args.reason))
            except claims_module.ClaimConflict as error:
                return fail(str(error), code=3)
            return 0
        if args.acquire:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session, backend=args.persistence,
                store=store,
            )
            scope = None
            branch = args.branch
            if args.paths or args.worktree:
                checkout = args.worktree
                selected = session.snapshot.get("selected_checkouts", {}).get(args.acquire, {})
                if checkout:
                    checkout_path = Path(checkout)
                    if not checkout_path.is_absolute():
                        workspace = session.snapshot.get("workspace", {}).get("root")
                        if not workspace:
                            return fail("session has no resolved workspace root")
                        checkout = str((Path(workspace) / checkout_path).resolve())
                if args.worktree and branch is None:
                    branch = selected.get("branch")
                scope = {"kind": "worktree" if args.worktree else "paths",
                         "checkout": checkout, "branch": branch,
                         "paths": args.paths}
            basis = claims_module.BASIS_ADOPTION if args.adopt else args.basis
            try:
                out(session.acquire_claim(args.acquire, basis=basis, reason=args.reason,
                                          ttl_hours=args.ttl, scope=scope))
            except claims_module.ClaimConflict as error:
                return fail(str(error), code=3)
            except claims_module.ClaimAdoptionRequired as error:
                return fail(f"adoption required: {error}", code=4)
            return 0
        out(store.read_claims())
        return 0
    finally:
        close_store(store)


def cmd_pressures(args: argparse.Namespace) -> int:
    state = Path(args.state_dir)
    if args.record:
        definition = load_definition(args.record)
        entry = pressures_module.record(**definition)
        out(pressures_module.file_record(state, entry))
        return 0
    out(pressures_module.list_pressures(state))
    return 0


def cmd_workspace(args: argparse.Namespace) -> int:
    out(workspace_module.discover_workspace(args.root))
    return 0


def cmd_store(args: argparse.Namespace) -> int:
    store = open_store(args.state_dir, "store", session_id=args.session)
    try:
        if args.verify:
            out(store.backend.verify())
            return 0
        out({"generation": store.backend.generation(),
             "commit_feed": store.backend.commit_feed().hex()})
        return 0
    finally:
        close_store(store)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mncs-env")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    parser.add_argument("--persistence", choices=("store", "file"), default="store",
                        help="store is canonical; file is the debug projection")
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--consumer", default="local-agent")
    common.add_argument("--consumer-kind", default="agent")

    resolve = sub.add_parser("resolve", help="resolve an environment definition")
    resolve.add_argument("--definition", type=Path, required=True)
    resolve.add_argument("--workspace", default=None)
    resolve.add_argument("--consumer", default="local-agent")
    resolve.set_defaults(func=cmd_resolve)

    enter = sub.add_parser("enter", help="discover context, create/reuse a session, reconcile readiness")
    enter.add_argument("--definition", type=Path, default=None, help="defaults to closest .mncs/environment.json or read-only orientation")
    enter.add_argument("--workspace", default=None)
    enter.add_argument("--consumer", default="local-agent")
    enter.add_argument("--consumer-kind", default="agent")
    enter.add_argument("--new-session", action="store_true", help="create independent work instead of reusing matching work")
    enter.set_defaults(func=cmd_enter)

    def session_parser(name: str, help_text: str):
        parser_ = sub.add_parser(name, parents=[common], help=help_text)
        parser_.add_argument("session")
        return parser_

    inspect = session_parser("inspect", "inspect session state (read-only)")
    inspect.set_defaults(func=cmd_inspect)

    context = session_parser("context", "show compact first-use session context")
    context.set_defaults(func=cmd_context)

    health = session_parser("health", "probe live readiness without changing session state")
    health.add_argument("--live", action="store_true",
                        help="force full live probes instead of a validated epoch")
    health.set_defaults(func=cmd_health)
    reconcile = session_parser("reconcile", "refresh discovery, recover declared services, verify readiness")
    reconcile.set_defaults(func=cmd_reconcile)

    doctor = session_parser("doctor", "ambient remediation pass with a terse summary")
    doctor.add_argument("--evidence", action="store_true",
                        help="print the full evidence trail instead of running a pass")
    doctor.add_argument("--scope", choices=("session", "repository"), default="session",
                        help="session remediates environment state; repository invokes the bound remediation provider")
    doctor.add_argument("--checkout", default=None,
                        help="repository to remediate with --scope repository (requires a session claim)")
    doctor.add_argument("--dry-run", action="store_true",
                        help="plan repository remediation without mutating")
    doctor.add_argument("--changed-path", action="append", default=None,
                        help="narrow repository remediation to this checkout-relative path (repeatable)")
    doctor.set_defaults(func=cmd_doctor)

    projections = session_parser("projections", "ambient projection coherence pass with a terse summary")
    projections.add_argument("--evidence", action="store_true",
                             help="print the full projection evidence trail instead of running a pass")
    projections.add_argument("--apply", default=None,
                             help="explicitly apply one projection by id (region splicing allowed under claim)")
    projections.add_argument("--watch", default=None, metavar="SECONDS",
                             help="resident loop: one ambient pass per interval until interrupted")
    projections.add_argument("--watch-iterations", default=None, type=int,
                             help="stop the watch loop after this many passes")
    projections.set_defaults(func=cmd_projections)

    verification = session_parser("verify", "ambient verification coherence pass with a terse summary")
    verification.add_argument("--evidence", action="store_true",
                              help="print the full verification evidence trail instead of running a pass")
    verification.add_argument("--only", default=None,
                              help="explicitly verify one obligation by identity")
    verification.add_argument("--full", action="store_true",
                              help="explicit full pass without epoch reuse and with the full execution budget")
    verification.set_defaults(func=cmd_verification)

    diagnostic = session_parser("diagnostic", "ambient diagnostic coherence pass with a terse summary")
    diagnostic.add_argument("--evidence", action="store_true",
                            help="print the full diagnostic evidence trail instead of running a pass")
    diagnostic.add_argument("--only", default=None,
                            help="explicitly diagnose one failure by key")
    diagnostic.add_argument("--depth", default=None, choices=("minimal", "standard", "deep"),
                            help="explicit capture depth (ambient passes always use minimal)")
    diagnostic.add_argument("--full", action="store_true",
                            help="explicit full pass without epoch reuse and with the full capture budget")
    diagnostic.set_defaults(func=cmd_diagnostic)

    external = session_parser("actions", "ambient external-evidence coherence pass with a terse summary")
    external.add_argument("--evidence", action="store_true",
                          help="print the full external-evidence trail instead of running a pass")
    external.add_argument("--only", default=None,
                          help="explicitly reconcile one obligation by identity")
    external.add_argument("--dispatch", action="store_true",
                          help="explicitly dispatch admitted remote runs (requires a repository claim)")
    external.add_argument("--full", action="store_true",
                          help="explicit full pass without epoch reuse and with the full dispatch budget")
    external.set_defaults(func=cmd_actions)

    status = sub.add_parser(
        "status", parents=[common], help="show compact read-only session status"
    )
    status.add_argument("session", nargs="?", default=None)
    status.add_argument("--terse", action="store_true",
                        help="terse doctor summary, served without opening the store when the epoch is valid")
    status.set_defaults(func=cmd_status)

    resume = session_parser("resume", "resume a session in this process")
    resume.add_argument("--workspace", default=None)
    resume.add_argument("--revalidate", action="store_true")
    resume.set_defaults(func=cmd_resume)

    capabilities = session_parser("capabilities", "list bound capabilities")
    capabilities.set_defaults(func=cmd_capabilities)

    authority = session_parser("authority", "show authority or evaluate an action")
    authority.add_argument("--action", default=None)
    authority.add_argument("--target", default=None)
    authority.set_defaults(func=cmd_authority)

    invoke = session_parser("invoke", "invoke a bound capability")
    invoke.add_argument("capability")
    invoke.add_argument("--cwd", default=None)
    invoke.add_argument("--timeout", type=int, default=120)
    invoke.add_argument("--output-limit-bytes", type=int,
                        default=capabilities_module.DEFAULT_OUTPUT_LIMIT_BYTES,
                        help=f"bounded stdout/stderr capture limit (max {capabilities_module.MAX_OUTPUT_LIMIT_BYTES})")
    invoke.add_argument("argv", nargs="*")
    invoke.set_defaults(func=cmd_invoke)

    events = session_parser("events", "read, subscribe, poll, or observe events")
    events.add_argument("--subscribe", nargs="*", default=None)
    events.add_argument("--source", default=None)
    events.add_argument("--poll", default=None)
    events.add_argument("--observe-workspace", default=None)
    events.add_argument("--observe-store", action="store_true")
    events.set_defaults(func=cmd_events)

    brief = session_parser("brief", "compact update capsule since brief cursor")
    brief.set_defaults(func=cmd_brief)

    updates = session_parser("updates", "classified event delta since an index")
    updates.add_argument("--since", type=int, default=None)
    updates.set_defaults(func=cmd_updates)

    ack = session_parser("ack", "acknowledge the brief cursor at an event index")
    ack.add_argument("index", type=int)
    ack.set_defaults(func=cmd_ack)

    reconciler = sub.add_parser("reconciler", help="background reconciliation service")
    reconciler.add_argument("--workspace", default=None)
    reconciler.add_argument("--run-once", action="store_true")
    reconciler.add_argument("--run", action="store_true",
                            help="run the foreground reconcile loop")
    reconciler.add_argument("--interval", type=float, default=60.0)
    reconciler.add_argument("--status", action="store_true")
    reconciler.add_argument("--commons-socket", default=None,
                            help="commons sync socket (or MNCS_COMMONS_SOCKET)")
    reconciler.add_argument("--language-socket", default=None,
                            help="language-service event socket (or MNLS_SERVICE_SOCKET)")
    reconciler.set_defaults(func=cmd_reconciler)

    checkpoint = session_parser("checkpoint", "persist a checkpoint")
    checkpoint.add_argument("--progress", default="")
    checkpoint.add_argument("--remaining", nargs="*", default=[])
    checkpoint.set_defaults(func=cmd_checkpoint)

    handoff = session_parser("handoff", "hand off to another consumer")
    handoff.add_argument("--to", required=True)
    handoff.add_argument("--notes", nargs="*", default=[])
    handoff.add_argument("--blockers", nargs="*", default=[])
    handoff.add_argument("--next", nargs="*", default=[])
    handoff.set_defaults(func=cmd_handoff)

    accept = session_parser("accept", "accept a handed-off session")
    accept.add_argument("handoff", help="handoff identity to accept")
    accept.set_defaults(func=cmd_accept)

    complete = session_parser("complete", "complete a session")
    complete.add_argument("--outcome", required=True)
    complete.add_argument("--summary", default="")
    complete.set_defaults(func=cmd_complete)

    fail_cmd = session_parser("fail", "fail a session")
    fail_cmd.add_argument("--reason", required=True)
    fail_cmd.set_defaults(func=cmd_fail)

    claims = sub.add_parser("claims", help="inspect or manage workspace claims")
    claims.add_argument("session")
    claims.add_argument("--acquire", default=None)
    claims.add_argument("--release", default=None)
    claims.add_argument("--claim-id", default=None)
    claims.add_argument("--transfer-to", default=None,
                        help="session id receiving an explicit claim transfer")
    claims.add_argument("--transfer-consumer", default="",
                        help="consumer identity receiving a claim transfer")
    claims.add_argument("--paths", nargs="*", default=None,
                        help="repo-relative path scopes for a paths claim")
    claims.add_argument("--worktree", default=None,
                        help="checkout path for a worktree-scoped claim")
    claims.add_argument("--branch", default=None)
    claims.add_argument("--adopt", action="store_true",
                        help="use explicit-adoption basis for dirty checkouts")
    claims.add_argument("--basis", default=claims_module.BASIS_EXPLICIT,
                        choices=(claims_module.BASIS_EXPLICIT, claims_module.BASIS_INTENT_SCOPE,
                                 claims_module.BASIS_RECOVERY, claims_module.BASIS_ADOPTION))
    claims.add_argument("--reason", default="")
    claims.add_argument("--ttl", type=int, default=24)
    claims.set_defaults(func=cmd_claims)

    pressures = sub.add_parser("pressures", help="record or list pressures")
    pressures.add_argument("--record", type=Path, default=None)
    pressures.set_defaults(func=cmd_pressures)

    workspace = sub.add_parser("workspace", help="discover workspace repositories")
    workspace.add_argument("--root", default=".")
    workspace.set_defaults(func=cmd_workspace)

    store = sub.add_parser("store", help="inspect the backing store")
    store.add_argument(
        "--session", default=None,
        help="use this session's selected mncs-store checkout",
    )
    store.add_argument("--verify", action="store_true")
    store.set_defaults(func=cmd_store)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.state_dir = args.state_dir.expanduser().resolve()
    try:
        return args.func(args)
    except (workspace_module.WorkspaceResolutionError, entry_module.EntryError) as error:
        return fail(str(error), diagnostics=error.diagnostics)
    except SnapshotConflict as error:
        return fail(str(error), diagnostics={
            "code": "session-snapshot-conflict", "session_id": error.session_id,
            "revision": error.revision, "command": args.command,
            "snapshot_saved": False, "capability_may_have_run": args.command == "invoke",
            "next": "resume and inspect durable events and invocation artifacts before retrying; serialize mutating commands for this session",
        })
    except (StoreUnavailable, StoreIntegrityFailure) as error:
        return fail(str(error), diagnostics={"code": "store-unavailable" if isinstance(error, StoreUnavailable) else "store-integrity-failure",
                                           "provider": "mncs-store", "state_dir": str(args.state_dir),
                                           "next": "bind the intended Store checkout with MNCS_STORE_PYTHON; inspect Store recovery before retrying"})
    except (OSError, ValueError, sessions_module.LifecycleError) as error:
        return fail(str(error), diagnostics={"code": "environment-command-failed", "command": args.command,
                                           "state_dir": str(args.state_dir), "next": "check configuration and session state; retry entry"})


if __name__ == "__main__":
    raise SystemExit(main())
