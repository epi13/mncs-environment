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
from . import family as family_module
from . import netcheck as netcheck_module
from . import projections as projections_module
from . import resources as resources_module
from . import retire as retire_module
from . import verification as verification_module
from . import entry as entry_module
from . import claims as claims_module
from . import pressures as pressures_module
from . import rights as rights_module
from . import sessions as sessions_module
from . import testcmd as testcmd_module
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
            projected = projections_module.ambient_pass(session)
            payload['projections'] = projected['summary']
            out(payload)
            return 5 if result["summary"]["blockers"] else 0
        finally:
            session.close()


def cmd_projections(args: argparse.Namespace) -> int:
    if args.interpret:
        if not args.repository:
            return fail('--interpret requires --repository')
        with closing(_open(args)) as session:
            try:
                out(projections_module.interpretation(session, args.repository, args.interpret))
            except ValueError as error:
                return fail(str(error))
        return 0
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


def cmd_family(args: argparse.Namespace) -> int:
    with entry_module.entry_lock(args.state_dir, args.persistence):
        try:
            session = sessions_module.Session.resume(
                state_dir=args.state_dir, session_id=args.session,
                backend=args.persistence)
        except sessions_module.LifecycleError as error:
            return fail(str(error))
        try:
            if args.publish:
                try:
                    draft = json.loads(Path(args.publish).read_text(
                        encoding="utf-8"))
                except (OSError, ValueError) as error:
                    return fail(f"draft unreadable: {error}")
                try:
                    out(family_module.publish_change(session, draft))
                except family_module.FamilyError as error:
                    return fail(str(error))
                session._save()
                return 0
            if args.establish:
                try:
                    result = family_module.establish_change(
                        session, args.establish)
                except family_module.FamilyError as error:
                    return fail(str(error))
                session._save()
                out(result)
                return 0
            if args.transition:
                identity, _, state = args.transition.rpartition(":")
                if not identity or not state:
                    return fail("--transition needs CHANGE:STATE")
                try:
                    result = family_module.transition_change(
                        session, identity, state)
                except family_module.FamilyError as error:
                    return fail(str(error))
                session._save()
                out(result)
                return 0
            if args.converge:
                repository, _, consumer = args.converge.rpartition(":")
                if not repository or not consumer:
                    return fail("--converge needs CHANGE:CONSUMER")
                try:
                    result = family_module.converge(
                        session, repository, consumer,
                        dry_run=args.dry_run)
                except family_module.FamilyError as error:
                    return fail(str(error))
                session._save()
                out(result)
                return 0
            result = family_module.ambient_pass(session, mode="explicit")
            payload = {"session_id": args.session,
                       "capsule": family_module.capsule(session),
                       "summary": result["summary"]}
            out(payload)
            return 0
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


def cmd_test(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.resume(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    try:
        try:
            result = testcmd_module.run_tests(
                session, args.checkout, output_format=args.format,
                timeout_seconds=args.timeout, max_executions=args.max_executions,
                no_store=args.no_store, execution=args.execution,
            )
        except testcmd_module.TestRoutingError as error:
            return fail(str(error), code=3, diagnostics=error.diagnostics)
        except (sessions_module.AuthorityDenied, sessions_module.LifecycleError,
                capabilities_module.CapabilityError) as error:
            return fail(str(error), code=3)
    finally:
        session.close()
    report = result.get("report", "")
    if report and not report.endswith("\n"):
        report += "\n"
    sys.stdout.write(report)
    if result.get("stderr"):
        sys.stderr.write(result["stderr"])
        if not result["stderr"].endswith("\n"):
            sys.stderr.write("\n")
    if result.get("truncated"):
        print("warning: provider report was truncated by the capture limit",
              file=sys.stderr)
    returncode = result.get("returncode")
    return returncode if isinstance(returncode, int) else 3


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
        records = store.read_claims()
        if args.explain:
            latest = None
            for record in records:
                if str(record.get("claim_id", "")) != args.explain:
                    continue
                if latest is None or int(record.get("version", 0)) > int(latest.get("version", 0)):
                    latest = record
            if latest is None:
                return fail(f"unknown claim {args.explain}")
            out(claims_module.explain(latest, store))
            return 0
        live = claims_module.active_claims(records)
        live_keys = {(str(item.get("claim_id", "")),
                      int(item.get("version", 0)))
                     for item in live.values()}
        annotated = []
        for record in records:
            copy = dict(record)
            copy["live"] = (str(record.get("claim_id", "")),
                            int(record.get("version", 0))) in live_keys
            annotated.append(copy)
        out(annotated)
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
    document = workspace_module.discover_workspace(args.root)
    if args.summary:
        document = {key: document[key] for key in
                    ("schema_version", "root", "scan", "repository_count") if key in document}
        document["schema_version"] = "mncs.environment.workspace-readiness/1"
    out(document)
    return 0


def _resolve_retire_repo(session: sessions_module.Session, repository: str) -> Path:
    root_value = session.snapshot.get("workspace", {}).get("root")
    if not isinstance(root_value, str) or not root_value:
        raise ValueError("session has no resolved workspace root")
    root = Path(root_value).resolve()
    candidate = Path(repository)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        raise ValueError(f"repository escapes the workspace root: {repository}")
    if not (resolved / ".git").exists():
        raise ValueError(f"not a git checkout: {resolved}")
    return resolved


def cmd_retire(args: argparse.Namespace) -> int:
    """Retire merged branches / spent worktrees after strict assessment."""
    with closing(_open(args)) as session:
        try:
            repo = _resolve_retire_repo(session, args.repository)
        except ValueError as error:
            return fail(str(error))
        store = open_store(args.state_dir, args.persistence, session_id=args.session)
        try:
            claim_records = store.read_claims()
        finally:
            close_store(store)
        results: list[dict] = []
        if args.prune:
            results.append(retire_module.prune_worktrees(repo, dry_run=args.dry_run))
        for branch in args.branch or []:
            results.append(retire_module.retire_branch(
                repo, branch, canonical=args.canonical,
                claim_records=claim_records,
                requesting_session=args.session, dry_run=args.dry_run))
        for path in args.worktree or []:
            results.append(retire_module.retire_worktree(
                repo, path, canonical=args.canonical,
                claim_records=claim_records,
                requesting_session=args.session, dry_run=args.dry_run))
        out({"session_id": args.session, "repository": str(repo),
             "canonical": args.canonical, "dry_run": args.dry_run,
             "results": results})
        return 0


def cmd_resources(args: argparse.Namespace) -> int:
    """Observe caller ancestry or selected PIDs without opening Store."""
    out(resources_module.observe(args.pid))
    return 0


def cmd_netcheck(args: argparse.Namespace) -> int:
    """Probe layered GitHub reachability (DNS, TLS, HTTPS, git)."""
    out(netcheck_module.check(args.host, remote=args.remote, timeout=args.timeout))
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


def cmd_resident(args):
    """Resume an exact session and service the same incremental owner router."""
    import time
    if args.interval <= 0 or args.max_ticks < 0:
        raise ValueError('resident interval must be positive and max-ticks nonnegative')
    count = 0
    while True:
        with entry_module.entry_lock(args.state_dir, args.persistence):
            with closing(_open(args)) as session:
                source = session.snapshot.get('configuration', {}).get('source')
                definition, _ = entry_module.select_definition(Path(source) if source and source != 'orientation-default' else None,
                    session.snapshot['workspace']['root'])
                expected = session.snapshot.get('provenance', {}).get('definition_id')
                if expected and entry_module.identity.environment_id(definition) != expected:
                    raise ValueError('resident definition changed; re-enter to select and bind the new environment')
                _, trace = entry_module.ambient_tick(session, definition)
                if not args.watch or trace.get('scheduled') or trace.get('events'):
                    out({'session_id': session.session_id, 'trace': trace})
        count += 1
        if not args.watch or (args.max_ticks and count >= args.max_ticks):
            return 0
        time.sleep(args.interval)


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
    projections.add_argument('--interpret', choices=('capabilities', 'dependencies', 'blockers', 'architecture', 'structure', 'why'))
    projections.add_argument('--repository')
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

    family = session_parser("family", "family shared-workspace observation and convergence")
    family.add_argument("--publish", default=None, metavar="DRAFT_JSON",
                        help="validate and publish a draft working change")
    family.add_argument("--establish", default=None, metavar="CHANGE",
                        help="validate and establish a published change")
    family.add_argument("--transition", default=None, metavar="CHANGE:STATE",
                        help="advance change lifecycle (native law judges)")
    family.add_argument("--converge", default=None, metavar="CHANGE:CONSUMER",
                        help="explicitly converge one consumer (requires a claim)")
    family.add_argument("--dry-run", action="store_true",
                        help="plan convergence without mutating")
    family.set_defaults(func=cmd_family)

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

    test = session_parser("test", "run selected tests through the canonical VM or Stage-0 oracle")
    test.add_argument("--checkout", default=None, required=True,
                      help="repository to verify (must be selected in the session)")
    test.add_argument("--format", choices=("text", "json"), default="text",
                      help="provider report format")
    test.add_argument("--execution", choices=("canonical-vm", "stage0-reference"),
                      default=None,
                      help="default: canonical VM for the checkout that provides it; Stage-0 oracle for other targets")
    test.add_argument("--timeout", type=int, default=600,
                      help="provider execution timeout in seconds")
    test.add_argument("--max-executions", type=int, default=16,
                      help="Stage-0 reference coherence execution budget")
    test.add_argument("--no-store", action="store_true",
                      help="skip Test Store admission (Stage-0 reference lane only)")
    test.set_defaults(func=cmd_test)

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

    resident = sub.add_parser('resident', help='service an existing session through the incremental owner router')
    resident.add_argument('session')
    resident.add_argument('--watch', action='store_true')
    resident.add_argument('--interval', type=float, default=1.0)
    resident.add_argument('--max-ticks', type=int, default=0)
    resident.set_defaults(func=cmd_resident)

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
    claims.add_argument("--explain", default=None,
                        help="explain one claim id as live/stale/recoverable/not-recoverable")
    claims.set_defaults(func=cmd_claims)

    pressures = sub.add_parser("pressures", help="record or list pressures")
    pressures.add_argument("--record", type=Path, default=None)
    pressures.set_defaults(func=cmd_pressures)

    workspace = sub.add_parser("workspace", help="discover workspace repositories")
    workspace.add_argument("--root", default=".")
    workspace.add_argument("--summary", action="store_true", help="bounded readiness response; omit repository details")
    workspace.set_defaults(func=cmd_workspace)

    store = sub.add_parser("store", help="inspect the backing store")
    store.add_argument(
        "--session", default=None,
        help="use this session's selected mncs-store checkout",
    )
    store.add_argument("--verify", action="store_true")
    store.set_defaults(func=cmd_store)

    retire = sub.add_parser("retire", help="retire merged branches and spent worktrees")
    retire.add_argument("session", help="session id (claim context; own claims do not block)")
    retire.add_argument("--repository", required=True,
                        help="repository name (under the session workspace) or absolute path")
    retire.add_argument("--branch", action="append", default=[],
                        help="local branch to retire (repeatable)")
    retire.add_argument("--worktree", action="append", default=[],
                        help="registered worktree path to retire (repeatable)")
    retire.add_argument("--canonical", default="origin/main",
                        help="canonical ref merged branches must reach (default: origin/main)")
    retire.add_argument("--prune", action="store_true",
                        help="drop worktree records whose directories are gone")
    retire.add_argument("--dry-run", action="store_true",
                        help="assess only; change nothing")
    retire.set_defaults(func=cmd_retire)

    resources = sub.add_parser("resources", help="live bounded Linux resource observations (no Store open)")
    resources.add_argument("--pid", type=int, action="append", default=None,
                           help="observe a PID (repeatable, at most eight); defaults to caller ancestry")
    resources.set_defaults(func=cmd_resources)

    netcheck = sub.add_parser("netcheck", help="probe layered GitHub reachability")
    netcheck.add_argument("--host", default="github.com")
    netcheck.add_argument("--remote", default=None,
                          help="optional git remote URL for a read-only ls-remote layer")
    netcheck.add_argument("--timeout", type=float, default=5.0)
    netcheck.set_defaults(func=cmd_netcheck)

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
