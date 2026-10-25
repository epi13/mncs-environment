"""mncs-env: thin inspection/control surface over the session system.

The CLI parses arguments and prints JSON; session semantics live in the
package. Git retirement is delegated to the safety-checked retirement adapter
and requires Environment session authority before mutation.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

from . import actions as actions_module
from . import capabilities as capabilities_module
from . import capability_intelligence as capability_intelligence_module
from . import claims as claims_module
from . import diagnostics as diagnostics_module
from . import doctor as doctor_module
from . import entry as entry_module
from . import evidence_store
from . import family as family_module
from . import invocation_artifacts as invocation_artifacts_module
from . import netcheck as netcheck_module
from . import pressures as pressures_module
from . import projections as projections_module
from . import resources as resources_module
from . import retire as retire_module
from . import rights as rights_module
from . import sessions as sessions_module
from . import testcmd as testcmd_module
from . import verification as verification_module
from . import workspace as workspace_module
from .persist import read_json
from .session_store import ClaimBatchConflict, SnapshotConflict, open_store
from .store_backend import StoreIntegrityFailure, StoreUnavailable

DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "mncs-environment"
CAMPAIGN_STATE_OUTPUT_LIMIT_BYTES = 128 * 1024
CAMPAIGN_STATE_PROJECTION_MIN_BYTES = 4 * 1024
# Leave room for the largest currently supported native application-build
# artifact while keeping artifact-read output explicitly bounded.
MAX_ARTIFACT_READ_BYTES = 1_300_000
MAX_CLAIM_PAGE_OUTPUT_BYTES = 48 * 1024


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

    A project definition may opt into its containing Git checkout and derives
    the workspace from that repository's Git common directory. Other relative
    ``workspace_root`` values resolve against the definition file's directory.
    Provider-bound definitions still require an explicit workspace root.
    """
    working_checkout = entry_module.current_working_checkout(
        definition, getattr(args, "definition", None),
    )
    if args.workspace:
        if (working_checkout is not None
                and Path(args.workspace).expanduser().resolve()
                != Path(working_checkout["workspace_root"])):
            raise ValueError(
                "--workspace must match the current checkout's authenticated workspace root"
            )
        return args.workspace
    if working_checkout is not None:
        return working_checkout["workspace_root"]
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
            working_checkout=entry_module.current_working_checkout(
                definition, args.definition,
            ),
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
        working_checkout = entry_module.current_working_checkout(
            definition, definition_path,
        )
        result = entry_module.enter(definition=definition, definition_path=definition_path,
                               workspace_root=workspace_root, state_dir=args.state_dir,
                               backend=args.persistence, consumer_id=args.consumer,
                               consumer_kind=args.consumer_kind, new_session=args.new_session,
                               campaign_id=args.campaign_id,
                               task_identity=args.task_identity,
                               working_checkout=working_checkout,
                               authenticated_principal_id=os.environ.get(
                                   "MNCS_ENV_AUTH_PRINCIPAL_ID"
                               ),
                               continuation_request_id=os.environ.get(
                                   "MNCS_ENV_RPC_REQUEST_ID"
                               ))
        out(result)
        return 5 if result["readiness"]["status"] == "blocked" else 0
    except (workspace_module.WorkspaceResolutionError, entry_module.EntryError) as error:
        return fail(str(error), diagnostics=error.diagnostics)
    except ValueError as error:
        return fail(str(error), diagnostics={"code": "entry-invalid", "next": "check the selected definition and workspace"})
    except rights_module.RightsBlocked as error:
        return fail(str(error), diagnostics={"code": "rights-blocked", "provider": "mncs-rights-provenance",
                                           "next": "inspect provider-owned rights claims for the selected workspace"})


def cmd_campaign_continuation(_args: argparse.Namespace) -> int:
    return fail(
        "project-scoped campaign continuation requires the authenticated Control transport",
        diagnostics={
            "code": "persistence-service-unavailable",
            "transport": "mncs-control-environment-rpc",
            "next": "run this command through the MNCS Control protected execution path",
        },
    )


def cmd_campaign_task_bind(_args: argparse.Namespace) -> int:
    return fail(
        "campaign task binding requires the authenticated Control Environment transport",
        diagnostics={
            "code": "persistence-service-unavailable",
            "transport": "mncs-control-environment-rpc",
            "next": "run campaign-bind through the MNCS Control project-scoped protected execution path",
        },
    )


def cmd_campaign_state(args: argparse.Namespace) -> int:
    """Return one bounded page from the exact persisted campaign ledger."""
    try:
        with closing(_open(args)) as session:
            try:
                state = session.campaign_state(
                    evidence_offset=args.evidence_offset,
                    evidence_limit=args.evidence_limit,
                )
                state_size = len(json.dumps(
                    state, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"))
                projection_budget = min(
                    capability_intelligence_module.MAX_RESULT_BYTES,
                    CAMPAIGN_STATE_OUTPUT_LIMIT_BYTES - state_size - 64,
                )
                if projection_budget >= CAMPAIGN_STATE_PROJECTION_MIN_BYTES:
                    state["environmental_capability"] = capability_intelligence_module.project(
                        session, campaign_state=state,
                        max_result_bytes=projection_budget,
                    )
                encoded = json.dumps(
                    state, ensure_ascii=False, sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                if len(encoded) > CAMPAIGN_STATE_OUTPUT_LIMIT_BYTES:
                    state.pop("environmental_capability", None)
                    encoded = json.dumps(
                        state, ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                sys.stdout.write(encoded.decode("utf-8") + "\n")
            except sessions_module.LifecycleError as error:
                next_step = (
                    "read the authenticated continuation capsule and inspect its "
                    "campaign state status"
                )
                if str(error) == "campaign has no persisted state checkpoint":
                    next_step = (
                        "initialize campaign metadata with a checkpoint using "
                        "campaign_base_checkpoint='none' and campaign_evidence=[], "
                        "then retry the campaign-state read"
                    )
                return fail(
                    str(error),
                    diagnostics={
                        "code": "campaign-state-unavailable",
                        "next": next_step,
                    },
                )
        return 0
    except (StoreUnavailable, StoreIntegrityFailure) as error:
        diagnostics = getattr(error, "diagnostics", None)
        return fail(str(error), diagnostics=diagnostics)


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


def _request_operation(args: argparse.Namespace) -> str:
    command = getattr(getattr(args, "func", None), "__name__", "cli")
    values = {
        key: value for key, value in vars(args).items()
        if key not in {"func", "state_dir", "persistence"}
    }
    normalized = json.loads(json.dumps(values, sort_keys=True, default=str))
    digest = hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return f"{command}:{digest}"


def _resume_session(
    args: argparse.Namespace,
    *,
    store=None,
    request_operation: str | None = None,
) -> sessions_module.Session:
    return sessions_module.Session.resume(
        state_dir=args.state_dir,
        session_id=args.session,
        backend=args.persistence,
        store=store,
        request_id=os.environ.get("MNCS_ENV_RPC_REQUEST_ID"),
        request_operation=request_operation or _request_operation(args),
    )


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
                session = _resume_session(args)
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
            session = _resume_session(args)
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
    if args.evidence and args.interpret:
        return fail(
            "--evidence applies only to an unfiltered ambient projection read",
            diagnostics={
                "code": "incompatible-projection-options",
                "next": (
                    "run environmental-capability analysis without --evidence; "
                    "use --evidence without --interpret for the ambient projection trail"
                ),
            },
        )
    if args.history_pages != 1 and args.interpret != "environmental-capability":
        return fail('--history-pages requires --interpret environmental-capability')
    if (args.evidence_offset is not None
            and args.interpret != "environmental-capability"):
        return fail('--evidence-offset requires --interpret environmental-capability')
    if (args.evidence_limit != capability_intelligence_module.MAX_EVIDENCE_REFS
            and args.interpret != "environmental-capability"):
        return fail('--evidence-limit requires --interpret environmental-capability')
    if (args.session_event_before_sequence is not None
            and args.interpret != "environmental-capability"):
        return fail(
            '--session-event-before-sequence requires '
            '--interpret environmental-capability'
        )
    if (args.session_event_limit
            != capability_intelligence_module.MAX_SESSION_EVENT_WINDOW
            and args.interpret != "environmental-capability"):
        return fail(
            '--session-event-limit requires '
            '--interpret environmental-capability'
        )
    if args.interpret:
        if args.interpret != "environmental-capability" and not args.repository:
            return fail('--interpret requires --repository')
        with closing(_open(args)) as session:
            try:
                out(projections_module.interpretation(
                    session, args.repository, args.interpret,
                    history_pages=args.history_pages,
                    evidence_offset=args.evidence_offset,
                    evidence_limit=args.evidence_limit,
                    session_event_before_sequence=args.session_event_before_sequence,
                    session_event_limit=args.session_event_limit,
                ))
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
            session = _resume_session(args)
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
                    session = _resume_session(args)
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
            session = _resume_session(args)
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
            session = _resume_session(args)
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
            session = _resume_session(args)
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
            session = _resume_session(args)
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
        session = _resume_session(args)
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
    request_id = os.environ.get("MNCS_ENV_RPC_REQUEST_ID")
    request_operation = _request_operation(args)
    try:
        session = _resume_session(args, request_operation=request_operation)
    except (sessions_module.LifecycleError, ValueError) as error:
        diagnostics = getattr(error, "diagnostics", None)
        if isinstance(diagnostics, dict) and diagnostics:
            return fail(str(error), code=3, diagnostics=diagnostics)
        return fail(str(error))
    if args.revalidate:
        workspace_root = (
            args.workspace
            or session.snapshot.get("workspace", {}).get("root")
            or "."
        )
        try:
            revalidation = session.revalidate(
                request_id=request_id,
                request_operation=request_operation,
                workspace_root=workspace_root,
            )
        except (sessions_module.LifecycleError, ValueError) as error:
            diagnostics = getattr(error, "diagnostics", None)
            return fail(
                str(error), code=3,
                diagnostics=diagnostics if isinstance(diagnostics, dict) else None,
            )
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


def cmd_scope_grant(args: argparse.Namespace) -> int:
    """Admit one Control-authorized repository scope into Environment state."""
    session = _open(args)
    try:
        owner_authorized_checkout = None
        if args.owner_authorized_checkout_json is not None:
            try:
                owner_authorized_checkout = json.loads(
                    args.owner_authorized_checkout_json
                )
            except (TypeError, json.JSONDecodeError) as error:
                return fail(
                    f"Control-provided repository checkout identity is invalid: {error}",
                    code=3,
                    diagnostics={"code": "authority-denied", "publication_completed": False},
                )
            if (not isinstance(owner_authorized_checkout, dict)
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           for key, value in owner_authorized_checkout.items())):
                return fail(
                    "Control-provided repository checkout identity is malformed",
                    code=3,
                    diagnostics={"code": "authority-denied", "publication_completed": False},
                )
        grant = session.grant_repository_scope(
            repository=args.repository,
            campaign_id=args.campaign_id,
            expected_authority_revision=args.expected_authority_revision,
            approval_reference=args.approval_reference,
            owner_authorized_checkout=owner_authorized_checkout,
            authenticated_principal_id=os.environ.get("MNCS_ENV_AUTH_PRINCIPAL_ID"),
            request_id=os.environ.get("MNCS_ENV_RPC_REQUEST_ID"),
        )
        out(grant)
        return 0
    except sessions_module.AuthorityScopeConflict as error:
        return fail(str(error), code=3, diagnostics=error.diagnostics)
    except sessions_module.AuthorityDenied as error:
        return fail(str(error), code=3, diagnostics={
            "code": "authority-denied",
            "publication_completed": False,
            "next": "inspect the authenticated Environment scope and Control owner grant",
        })
    finally:
        session.close()


def cmd_invoke(args: argparse.Namespace) -> int:
    try:
        session = _resume_session(args)
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    try:
        try:
            input_artifacts = [
                {
                    "name": item[0],
                    "capability": item[1],
                    "binding_id": item[2],
                    "path": item[3],
                }
                for item in (getattr(args, "input_artifact", None) or [])
            ]
            result = session.invoke(
                args.capability, args.argv, cwd=args.cwd,
                timeout_seconds=args.timeout,
                output_limit_bytes=args.output_limit_bytes,
                input_artifacts=input_artifacts,
                effect_target=(
                    {"repository": getattr(args, "effect_repository", None)}
                    if getattr(args, "effect_repository", None) else None
                ),
            )
        except sessions_module.AuthorityDenied as error:
            return fail(str(error), code=3, diagnostics={
                "code": "authority-denied",
                "publication_completed": False,
            })
        except (sessions_module.LifecycleError,
                capabilities_module.CapabilityError) as error:
            return fail(str(error), code=3)
        except invocation_artifacts_module.InvocationArtifactError as error:
            return fail(str(error), code=4, diagnostics={
                "code": "provider-input-artifact-invalid",
                "publication_completed": False,
            })
        except StoreUnavailable as error:
            return fail(str(error), code=4, diagnostics={
                "code": error.code,
                "provider": "mncs-store",
                "publication_completed": False,
            })
        except StoreIntegrityFailure as error:
            return fail(str(error), code=4, diagnostics={
                "code": "store-corruption-or-binding-mismatch",
                "provider": "mncs-store",
                "publication_completed": False,
            })
        out(result)
        if result.get("truncated"):
            print("warning: provider invocation output was truncated by the capture limit",
                  file=sys.stderr)
            return 4
        return 0 if result["status"] in ("ok", "pending-escalation") else 4
    finally:
        session.close()


def cmd_artifact_read(args: argparse.Namespace) -> int:
    """Return one authorized file from the latest successful Store artifact."""
    try:
        invocation_artifacts_module._safe_relative(args.path)
    except StoreIntegrityFailure as error:
        return fail(str(error), code=3, diagnostics={
            "code": "artifact-path-invalid",
            "next": "supply one safe path relative to the provider artifact bundle",
        })
    try:
        relative = invocation_artifacts_module._safe_relative(args.path)
        with closing(_open(args)) as session:
            bindings = session.snapshot.get("bindings", [])
            matches = [
                item for item in bindings
                if isinstance(item, dict)
                and item.get("capability") == args.capability
                and item.get("binding_id") == args.binding_id
            ] if isinstance(bindings, list) else []
            if len(matches) != 1:
                return fail(
                    "artifact read requires one exact current capability binding",
                    code=3,
                    diagnostics={
                        "code": "artifact-binding-unavailable",
                        "next": "refresh the Environment capability inventory for the selected session",
                    },
                )
            binding = matches[0]
            shareable_artifacts = binding.get("shareable_artifacts", [])
            read_only = binding.get("effects") == ["read"]
            explicitly_shareable = (
                isinstance(shareable_artifacts, list)
                and relative.as_posix() in shareable_artifacts
            )
            if not read_only and not explicitly_shareable:
                return fail(
                    "artifact read requires read-only effects or an exact "
                    "declared shareable path",
                    code=3,
                    diagnostics={"code": "artifact-effect-denied"},
                )
            verdict = session.check(action="invoke", target=args.capability)
            if verdict.get("verdict") != "allow":
                return fail(
                    "Environment did not authorize access to this capability artifact",
                    code=3,
                    diagnostics={
                        "code": "authority-denied",
                        "reason": verdict.get("reason"),
                    },
                )

            artifacts = session.snapshot.get("artifacts", [])
            latest = next((
                item for item in reversed(artifacts)
                if isinstance(item, dict)
                and item.get("kind") == "invocation-result"
                and item.get("capability") == args.capability
                and item.get("binding_id") == args.binding_id
            ), None) if isinstance(artifacts, list) else None
            if latest is None:
                return fail(
                    "the selected capability has no recorded invocation artifact",
                    code=4,
                    diagnostics={"code": "artifact-not-found"},
                )
            if latest.get("status") != "ok":
                return fail(
                    "the latest capability invocation did not complete successfully",
                    code=4,
                    diagnostics={"code": "latest-invocation-unsuccessful"},
                )
            reference = latest.get("artifact_store_ref")
            if not evidence_store.is_store_reference(reference):
                return fail(
                    "the latest capability invocation has no canonical Store artifact",
                    code=4,
                    diagnostics={"code": "store-artifact-unavailable"},
                )

            with invocation_artifacts_module.InvocationArtifactWorkspace(
                session, args.capability, args.binding_id,
            ) as artifact_workspace:
                if not artifact_workspace.store_backed:
                    return fail(
                        "capability artifact read requires the strict Store provider",
                        code=4,
                        diagnostics={"code": "store-provider-operation-unsupported"},
                    )
                if artifact_workspace.previous_reference != reference:
                    raise StoreIntegrityFailure(
                        "latest invocation artifact does not match the current Store bundle"
                    )
                content = artifact_workspace.read_file(
                    relative.as_posix(), max_bytes=MAX_ARTIFACT_READ_BYTES,
                )
            out({
                "schema_version": "mncs.environment.artifact-read/1",
                "session_id": session.session_id,
                "capability": args.capability,
                "binding_id": args.binding_id,
                "store_reference": reference,
                "path": relative.as_posix(),
                "byte_count": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
                "content_base64": base64.b64encode(content).decode("ascii"),
            })
        return 0
    except StoreUnavailable as error:
        return fail(str(error), code=4, diagnostics={
            "code": error.code,
            "provider": "mncs-store",
            "next": "restore the authenticated Store provider operation and retry the read",
        })
    except StoreIntegrityFailure as error:
        return fail(str(error), code=4, diagnostics={
            "code": "store-corruption-or-binding-mismatch",
            "provider": "mncs-store",
            "next": "inspect Store integrity and the session artifact reference",
        })
    except invocation_artifacts_module.InvocationArtifactError as error:
        return fail(str(error), code=4, diagnostics={
            "code": "artifact-read-bound-exceeded",
            "next": "request a smaller artifact through its owning provider",
        })
    except (OSError, ValueError, sessions_module.LifecycleError) as error:
        return fail(str(error), code=4, diagnostics={
            "code": "artifact-read-failed",
            "next": "check the selected session, binding, and relative artifact path",
        })


def cmd_test(args: argparse.Namespace) -> int:
    try:
        session = _resume_session(args)
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
        return 4
    returncode = result.get("returncode")
    return returncode if isinstance(returncode, int) else 3


def cmd_events(args: argparse.Namespace) -> int:
    session = _open(args)
    if args.subscribe:
        # Subscribing changes cursor state: use a participating handle.
        participant = _resume_session(args)
        out(participant.subscribe(args.subscribe, source_filter=args.source))
        return 0
    if args.poll:
        participant = _resume_session(args)
        out(participant.poll(args.poll))
        return 0
    if args.observe_workspace:
        participant = _resume_session(args)
        out(participant.observe_workspace(args.observe_workspace))
        return 0
    if args.observe_store:
        participant = _resume_session(args)
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
        session = _resume_session(args)
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
        session = _resume_session(args)
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    campaign_evidence = None
    if args.campaign_evidence_json is not None:
        raw = args.campaign_evidence_json
        if len(raw.encode("utf-8")) > sessions_module.CAMPAIGN_STATE_INPUT_MAX_BYTES:
            raise ValueError("campaign evidence JSON exceeds the bounded publication size")
        campaign_evidence = json.loads(raw)
    campaign_evidence_delta = None
    if args.campaign_evidence_delta_json is not None:
        raw = args.campaign_evidence_delta_json
        if len(raw.encode("utf-8")) > sessions_module.CAMPAIGN_STATE_INPUT_MAX_BYTES:
            raise ValueError("campaign evidence append delta exceeds the bounded publication size")
        campaign_evidence_delta = json.loads(raw)
    campaign_delivery = None
    if args.campaign_delivery_json is not None:
        raw = args.campaign_delivery_json
        if len(raw.encode("utf-8")) > sessions_module.CAMPAIGN_STATE_INPUT_MAX_BYTES:
            raise ValueError("campaign delivery JSON exceeds the bounded publication size")
        campaign_delivery = json.loads(raw)
    try:
        checkpoint = session.checkpoint(
            progress=args.progress,
            remaining=args.remaining,
            request_id=os.environ.get("MNCS_ENV_RPC_REQUEST_ID"),
            campaign_evidence=campaign_evidence,
            campaign_evidence_delta=campaign_evidence_delta,
            campaign_delivery=campaign_delivery,
            campaign_base_checkpoint=args.campaign_base_checkpoint,
        )
    except sessions_module.CampaignStateConflict as error:
        return fail(str(error), code=3, diagnostics=error.diagnostics)
    out(checkpoint)
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    try:
        session = _resume_session(args)
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    out(
        session.handoff(
            to_consumer=args.to,
            notes=args.notes,
            blockers=args.blockers,
            next_actions=args.next,
            to_authenticated_principal_id=(
                args.to_principal or os.environ.get("MNCS_ENV_AUTH_PRINCIPAL_ID")
            ),
            request_id=os.environ.get("MNCS_ENV_RPC_REQUEST_ID"),
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
                                   consumer_kind=args.consumer_kind,
                                   authenticated_principal_id=os.environ.get(
                                       "MNCS_ENV_AUTH_PRINCIPAL_ID"
                                   ),
                                   request_id=os.environ.get("MNCS_ENV_RPC_REQUEST_ID")))
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session.open(
            state_dir=args.state_dir, session_id=args.session, backend=args.persistence
        )
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    with closing(session):
        try:
            result = session.complete(
                outcome=args.outcome,
                summary=args.summary,
                request_id=(
                    None if args.retry_pending
                    else os.environ.get("MNCS_ENV_RPC_REQUEST_ID")
                ),
                authenticated_principal_id=os.environ.get(
                    "MNCS_ENV_AUTH_PRINCIPAL_ID"
                ),
                consumer_id=args.consumer,
                retry_pending=args.retry_pending,
            )
        except (sessions_module.LifecycleError, ValueError) as error:
            return fail(str(error), code=3)
        out(result)
    return 0


def cmd_fail(args: argparse.Namespace) -> int:
    try:
        session = _resume_session(args)
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    try:
        out(session.fail(reason=args.reason))
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    return 0


def cmd_claims(args: argparse.Namespace) -> int:
    expected_generation = getattr(args, "expected_store_generation", None)
    if expected_generation is not None:
        if type(expected_generation) is not int or expected_generation < 0:
            return fail("expected Store generation must be a non-negative integer")
        if not args.acquire or any((
            args.release, args.transfer_to, args.claim_id, args.explain,
            args.repository is not None, args.offset is not None,
            args.limit is not None,
        )):
            return fail(
                "--expected-store-generation requires --acquire and cannot be combined with other claim operations"
            )
    if args.repository is None and (args.offset is not None or args.limit is not None):
        return fail("claim pagination requires --repository")
    if args.repository is not None:
        if (not isinstance(args.repository, str) or not args.repository.strip()
                or len(args.repository) > 120):
            return fail("claim repository filter must be bounded nonempty text")
        if (
            args.acquire or args.release or args.transfer_to or args.claim_id or args.explain
            or args.transfer_consumer or args.paths or args.worktree or args.branch
            or args.adopt or args.reason or args.basis != claims_module.BASIS_EXPLICIT
            or args.ttl != 24
        ):
            return fail(
                "--repository is a read-only latest-claim query and cannot be combined with claim operations"
            )
        offset = 0 if args.offset is None else args.offset
        limit = 100 if args.limit is None else args.limit
        if offset < 0 or limit < 1 or limit > 128:
            return fail("claim page requires offset >= 0 and limit between 1 and 128")
    store = open_store(args.state_dir, args.persistence, session_id=args.session)
    try:
        if expected_generation is not None and not callable(
            getattr(getattr(store, "backend", None), "generation", None)
        ):
            return fail(
                "--expected-store-generation requires canonical Store persistence; file-backed debug sessions have no canonical generation",
                code=3,
                diagnostics={
                    "code": "persistence-unavailable",
                    "operation": "claim-acquire",
                    "publication_completed": False,
                },
            )
        if args.release:
            session = _resume_session(args, store=store)
            try:
                out({"released": session.release_claim(
                    args.release, reason=args.reason, claim_id=args.claim_id,
                    request_id=os.environ.get("MNCS_ENV_RPC_REQUEST_ID"))})
            except claims_module.ClaimConflict as error:
                return fail(str(error), code=3, diagnostics={
                    "code": "claim-conflict", "publication_completed": False,
                    "next": "re-read the current claim generation and reconcile ownership",
                })
            return 0
        if args.transfer_to:
            session = _resume_session(args, store=store)
            if not args.claim_id:
                return fail("transfer needs --claim-id")
            try:
                out(session.transfer_claim(
                    args.claim_id, args.transfer_to, args.transfer_consumer,
                    reason=args.reason,
                    request_id=os.environ.get("MNCS_ENV_RPC_REQUEST_ID")))
            except claims_module.ClaimConflict as error:
                return fail(str(error), code=3, diagnostics={
                    "code": "claim-conflict", "publication_completed": False,
                    "next": "re-read the current claim generation and reconcile ownership",
                })
            return 0
        if args.acquire:
            if expected_generation is not None:
                # Resuming writes an event and snapshot. Open read-only here so
                # the caller's generation token fences the claim publication.
                session = sessions_module.Session.open(
                    state_dir=args.state_dir, session_id=args.session,
                    backend=args.persistence, store=store,
                )
                lifecycle = session.snapshot.get("lifecycle")
                if lifecycle in ("completed", "failed"):
                    return fail(
                        f"session {args.session} is terminal and cannot acquire a claim"
                    )
                if lifecycle == "abandoned":
                    return fail(
                        "an abandoned session must be explicitly resumed before a generation-fenced claim; "
                        "resume it, read the current Store generation, then retry"
                    )
            else:
                session = _resume_session(args, store=store)
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
            request_id = os.environ.get("MNCS_ENV_RPC_REQUEST_ID") or None
            if expected_generation is not None and request_id is None:
                request_id = _request_operation(args)
            try:
                out(session.acquire_claim(args.acquire, basis=basis, reason=args.reason,
                                          ttl_hours=args.ttl, scope=scope,
                                          request_id=request_id,
                                          expected_store_generation=expected_generation))
            except ClaimBatchConflict as error:
                supplied = (
                    error.supplied_generation
                    if error.supplied_generation is not None
                    else expected_generation
                )
                return fail(str(error), code=3, diagnostics={
                    "code": "publication-conflict",
                    "conflict_kind": "store-generation",
                    "conflict_stage": error.stage,
                    "expected_store_generation": supplied,
                    "publication_expected_generation": error.expected_generation,
                    "observed_store_generation": error.observed_generation,
                    "publication_completed": False,
                    "next": "read the current Store generation and submit a new authorized claim request",
                })
            except claims_module.ClaimConflict as error:
                return fail(str(error), code=3, diagnostics={
                    "code": "claim-conflict", "publication_completed": False,
                    "next": "re-read the current claim generation and reconcile ownership",
                })
            except claims_module.ClaimAdoptionRequired as error:
                return fail(f"adoption required: {error}", code=4)
            except sessions_module.AuthorityDenied as error:
                return fail(str(error), code=3, diagnostics={
                    "code": "authority-denied",
                    "action": "claim-acquire",
                    "repository": args.acquire,
                    "publication_completed": False,
                    "next": "enter an Environment whose effective writable scope includes this repository",
                })
            return 0
        records = store.read_claims()
        if args.repository is not None:
            repository = args.repository.strip()
            matching = [
                record for record in records
                if str(record.get("repository", "")) == repository
            ]
            latest_by_id: dict[str, dict[str, Any]] = {}
            for record in matching:
                claim_id = str(record.get("claim_id", ""))
                if not claim_id:
                    continue
                try:
                    version = int(record.get("version", 0))
                except (TypeError, ValueError):
                    version = 0
                previous = latest_by_id.get(claim_id)
                try:
                    previous_version = int(previous.get("version", 0)) if previous else -1
                except (TypeError, ValueError):
                    previous_version = 0
                if previous is None or (version, str(record.get("identity", ""))) > (
                    previous_version, str(previous.get("identity", ""))
                ):
                    latest_by_id[claim_id] = record
            latest_records = sorted(
                latest_by_id.values(), key=lambda record: str(record.get("claim_id", ""))
            )
            live = claims_module.active_claims(matching)
            live_keys = {
                (str(item.get("claim_id", "")), int(item.get("version", 0)))
                for item in live.values()
            }
            annotated = []
            for record in latest_records:
                copy = dict(record)
                try:
                    version = int(record.get("version", 0))
                except (TypeError, ValueError):
                    version = 0
                copy["live"] = (str(record.get("claim_id", "")), version) in live_keys
                annotated.append(copy)
            offset = 0 if args.offset is None else args.offset
            limit = 100 if args.limit is None else args.limit
            page = annotated[offset:offset + limit]
            while True:
                response = {
                    "schema_version": "mncs.environment.claim-page/1",
                    "repository": repository,
                    "offset": offset,
                    "limit": limit,
                    "total": len(annotated),
                    "next_offset": (
                        offset + len(page)
                        if offset + len(page) < len(annotated) else None
                    ),
                    "claims": page,
                }
                encoded = json.dumps(
                    response, ensure_ascii=False, indent=2, sort_keys=True,
                ).encode("utf-8")
                if len(encoded) <= MAX_CLAIM_PAGE_OUTPUT_BYTES:
                    break
                if len(page) <= 1:
                    return fail(
                        "one claim record exceeds the bounded query response size",
                        code=3,
                        diagnostics={
                            "code": "claim-record-too-large",
                            "publication_completed": False,
                            "claim_id": page[0]["claim_id"] if page else None,
                            "next_offset": offset + 1 if page else offset,
                            "next": "inspect the claim through a transport that can return its exact full scope",
                        },
                    )
                page = page[:-1]
            out(response)
            return 0
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
        if args.claim_id:
            matching = [record for record in records
                        if str(record.get("claim_id", "")) == args.claim_id]
            if not matching:
                return fail(f"unknown claim {args.claim_id}")
            records = [max(matching, key=lambda record: int(record.get("version", 0)))]
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


def cmd_claim_recover(args: argparse.Namespace) -> int:
    """Keep owner-authorized recovery exclusively behind Control's RPC boundary."""
    return fail(
        "claim recovery is available only through the authenticated Control owner operation",
        code=3,
        diagnostics={
            "code": "control-owner-recovery-required",
            "next": "run claim-recover from the selected Environment project through MNCS Control",
        },
    )


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


def _retirement_request_id(args: argparse.Namespace, operation: str) -> str:
    controlled = os.environ.get("MNCS_ENV_RPC_REQUEST_ID")
    supplied = getattr(args, "request_id", None)
    if (controlled and supplied and controlled != supplied):
        raise ValueError("--request-id cannot replace the authenticated Control request identity")
    request_id = controlled or supplied
    if request_id is not None:
        if not isinstance(request_id, str) or not re.fullmatch(
            r"req_[A-Za-z0-9_-]{1,76}", request_id,
        ):
            raise ValueError("retirement request id must use the bounded req_ identity format")
        return request_id
    return "req_retire_" + sessions_module.digest_hex({
        "session_id": args.session,
        "operation": operation,
    }, length=48)


def _retirement_specs(args: argparse.Namespace, repo: Path) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    if args.prune:
        specs.append({"kind": "prune", "target": str(repo),
                      "scope": {"kind": "repository"}})
    specs.extend({"kind": "branch", "target": branch,
                  "scope": {"kind": "repository"}}
                 for branch in args.branch or [])
    for raw_path in args.worktree or []:
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        checkout = str(path.resolve(strict=False))
        scope: dict[str, Any] = {"kind": "worktree", "checkout": checkout}
        specs.append({"kind": "worktree", "target": checkout, "scope": scope})
    for index, spec in enumerate(specs):
        spec["index"] = index
    return specs


def _retirement_target_request_id(parent: str, spec: dict[str, Any]) -> str:
    return "req_retire_" + sessions_module.digest_hex({
        "parent_request_id": parent,
        "target": {key: spec[key] for key in ("index", "kind", "target")},
    }, length=48)


def _retirement_target_operation_digest(parent_digest: str,
                                        spec: dict[str, Any]) -> str:
    return sessions_module.digest_hex({
        "operation": "mncs-environment-retirement-target/1",
        "parent_operation_digest": parent_digest,
        "target": {key: spec[key] for key in ("index", "kind", "target")},
    })


def _retirement_store_claims(args: argparse.Namespace) -> list[dict[str, Any]]:
    store = open_store(args.state_dir, args.persistence, session_id=args.session)
    try:
        return store.read_claims()
    finally:
        close_store(store)


def _retirement_admission(session: sessions_module.Session, repo: Path,
                          spec: dict[str, Any]) -> dict[str, Any]:
    verdict = session.check(
        action="retire", target=repo.name, scope=spec["scope"],
    )
    return {
        "target_index": spec["index"],
        "verdict": verdict.get("verdict", "unknown"),
        "reason": verdict.get("reason"),
        "scope": spec["scope"],
        "session_id": session.session_id,
        "consumer_id": session.snapshot.get("consumer_id"),
        "authenticated_principal_id": session.snapshot.get("authenticated_principal_id"),
        "campaign_id": (session.snapshot.get("campaign", {}).get("identity")
                        if isinstance(session.snapshot.get("campaign"), dict) else None),
    }


def _retirement_adapter(
    repo: Path,
    spec: dict[str, Any],
    expected: dict[str, Any],
    *,
    canonical: str,
    claim_records: list[dict[str, Any]],
    requesting_session: str,
    sandbox_root: Path | None,
    execution_root: Path | None,
) -> dict[str, Any]:
    kind, target = spec["kind"], spec["target"]
    if kind == "prune":
        return retire_module.prune_worktrees(
            repo, sandbox_root=sandbox_root, execution_root=execution_root,
            expected_candidates=expected.get("candidates", []),
        )
    if kind == "branch":
        return retire_module.retire_branch(
            repo, target, canonical=canonical, claim_records=claim_records,
            requesting_session=requesting_session,
            sandbox_root=sandbox_root, execution_root=execution_root,
            expected_head=expected.get("head"),
        )
    return retire_module.retire_worktree(
        repo, target, canonical=canonical, claim_records=claim_records,
        requesting_session=requesting_session,
        sandbox_root=sandbox_root, execution_root=execution_root,
        expected_identity=expected.get("identity"),
    )


def cmd_retire(args: argparse.Namespace) -> int:
    """Retire merged branches / spent worktrees after strict assessment."""
    with closing(_open(args)) as session:
        try:
            repo = _resolve_retire_repo(session, args.repository)
        except ValueError as error:
            return fail(str(error))
        sandbox_root_value = getattr(args, "sandbox_root", None)
        execution_root_value = getattr(args, "execution_root", None)
        if (sandbox_root_value is None) != (execution_root_value is None):
            return fail(
                "protected checkout path mapping is incomplete",
                diagnostics={
                    "code": "checkout-path-mapping-invalid",
                    "next": "retry through the authenticated project Control transport",
                },
            )
        sandbox_root = (Path(sandbox_root_value) if sandbox_root_value is not None
                        else None)
        execution_root = (Path(execution_root_value).resolve(strict=False)
                          if execution_root_value is not None else None)
        if execution_root is not None and execution_root != repo:
            return fail(
                "protected checkout path mapping does not match the resolved repository",
                diagnostics={
                    "code": "checkout-path-mapping-invalid",
                    "repository": str(repo),
                    "execution_root": str(execution_root),
                    "next": "retry through the authenticated project Control transport",
                },
            )
        try:
            claim_records = _retirement_store_claims(args)
        except Exception as error:  # noqa: BLE001 - provider failures need structured diagnostics
            return fail(
                f"retirement claims are unavailable: {error}", code=3,
                diagnostics={"code": "persistence-service-unavailable",
                            "mutation_performed": False},
            )
        if args.inventory:
            if (not args.dry_run or args.prune or args.branch or args.worktree):
                return fail("--inventory requires --dry-run and cannot be combined with mutation targets")
            root_value = session.snapshot.get("workspace", {}).get("root")
            workspace_root = Path(root_value) if isinstance(root_value, str) and root_value else None
            report = retire_module.inventory_repository(
                repo, canonical=args.canonical, claim_records=claim_records,
                requesting_session=args.session, workspace_root=workspace_root,
                execution_root=execution_root, sandbox_root=sandbox_root,
            )
            campaign = session.snapshot.get("campaign", {})
            if isinstance(campaign, dict):
                report["campaign_id"] = campaign.get("identity")
                report["work_intent"] = campaign.get("work_intent")
            out(report)
            return 0

        specs = _retirement_specs(args, repo)
        if not specs:
            out({"session_id": args.session, "repository": str(repo),
                 "canonical": args.canonical, "dry_run": args.dry_run,
                 "completion_status": "completed", "results": []})
            return 0
        if args.dry_run:
            results = []
            for spec in specs:
                if spec["kind"] == "prune":
                    results.append(retire_module.prune_worktrees(
                        repo, dry_run=True, sandbox_root=sandbox_root,
                        execution_root=execution_root,
                    ))
                elif spec["kind"] == "branch":
                    results.append(retire_module.retire_branch(
                        repo, spec["target"], canonical=args.canonical,
                        claim_records=claim_records, requesting_session=args.session,
                        dry_run=True, sandbox_root=sandbox_root,
                        execution_root=execution_root,
                    ))
                else:
                    results.append(retire_module.retire_worktree(
                        repo, spec["target"], canonical=args.canonical,
                        claim_records=claim_records, requesting_session=args.session,
                        dry_run=True, sandbox_root=sandbox_root,
                        execution_root=execution_root,
                    ))
            out({"session_id": args.session, "repository": str(repo),
                 "canonical": args.canonical, "dry_run": True,
                 "completion_status": "assessed", "results": results})
            return 0

        try:
            operation = _request_operation(args)
            request_id = _retirement_request_id(args, operation)
            session_operation_digest = sessions_module._request_operation_digest(
                session.session_id, request_id, operation, session.snapshot,
            )
            campaign = session.snapshot.get("campaign", {})
            campaign_id = campaign.get("identity") if isinstance(campaign, dict) else None
            operation_digest = sessions_module.digest_hex({
                "session_operation_digest": session_operation_digest,
                "campaign_id": campaign_id,
            })
        except (ValueError, sessions_module.LifecycleError) as error:
            return fail(str(error), diagnostics={"code": "request-identity-invalid"})

        try:
            completed_event = session.request_event(
                "environment.retirement.completed", request_id, operation_digest,
            )
            if completed_event is not None:
                completed_payload = completed_event.get("payload", {})
                response = completed_payload.get("response")
                if (completed_payload.get("schema_version")
                        != "mncs.environment.retirement-receipt/1"
                        or not isinstance(response, dict)
                        or response.get("session_id") != session.session_id
                        or response.get("request_id") != request_id
                        or response.get("completion_status") != "completed"):
                    return fail("durable retirement receipt is malformed", code=3,
                                diagnostics={"code": "store-corruption",
                                             "request_id": request_id})
                out({"replayed_request": True, **response})
                return int(response.get("exit_code", 0))
            intent_event = session.request_event(
                "environment.retirement.requested", request_id, operation_digest,
            )
        except Exception as error:  # noqa: BLE001 - Store history is an external boundary
            return fail(
                f"retirement request history is unavailable: {error}", code=3,
                diagnostics={"code": "persistence-service-unavailable",
                            "request_id": request_id, "mutation_performed": False},
            )

        admissions: dict[int, dict[str, Any]] = {}
        if intent_event is None:
            for spec in specs:
                try:
                    admission = _retirement_admission(session, repo, spec)
                except Exception as error:  # noqa: BLE001 - authority provider failures are structured
                    return fail(
                        f"retirement authority could not be revalidated: {error}",
                        code=3,
                        diagnostics={"code": "authority-revalidation-unavailable",
                                    "action": "retire", "scope": spec["scope"],
                                    "request_id": request_id,
                                    "mutation_performed": False},
                    )
                admissions[spec["index"]] = admission
                if admission["verdict"] != "allow":
                    status = str(admission["verdict"])
                    return fail(
                        f"retirement of {spec['kind']} {spec['target']} is not authorized: {admission.get('reason') or status}",
                        code=3,
                        diagnostics={"code": ("authority-denied" if status == "deny"
                                             else "authority-escalation-required"),
                                    "action": "retire", "target": repo.name,
                                    "scope": spec["scope"], "verdict": status,
                                    "reason": admission.get("reason"),
                                    "request_id": request_id,
                                    "mutation_performed": False},
                    )
            try:
                claim_records = _retirement_store_claims(args)
                states = []
                for spec in specs:
                    state = retire_module.retirement_target_state(
                        repo, spec["kind"], spec["target"],
                        sandbox_root=sandbox_root, execution_root=execution_root,
                    )
                    if state.get("status") == "unknown":
                        return fail(
                            f"cannot establish exact retirement target state: {state.get('reason')}",
                            code=3,
                            diagnostics={"code": "retirement-target-unknown",
                                        "request_id": request_id,
                                        "target": spec["target"],
                                        "mutation_performed": False},
                        )
                    if (spec["kind"] == "worktree"
                            and (state.get("registered") is not True
                                 or state.get("identity") is None)):
                        return fail(
                            "worktree registration and checkout identity are unavailable",
                            code=3,
                            diagnostics={"code": "retirement-target-unknown",
                                        "request_id": request_id,
                                        "target": spec["target"],
                                        "mutation_performed": False},
                        )
                    if spec["kind"] == "branch" and state.get("head") is None:
                        return fail(
                            "branch ref is unavailable before retirement intent",
                            code=3,
                            diagnostics={"code": "retirement-target-unknown",
                                        "request_id": request_id,
                                        "target": spec["target"],
                                        "mutation_performed": False},
                        )
                    states.append(state)
                intent_payload = {
                    "schema_version": "mncs.environment.retirement-intent/1",
                    "session_id": session.session_id,
                    "consumer_id": session.snapshot.get("consumer_id"),
                    "authenticated_principal_id": session.snapshot.get("authenticated_principal_id"),
                    "campaign_id": (session.snapshot.get("campaign", {}).get("identity")
                                    if isinstance(session.snapshot.get("campaign"), dict) else None),
                    "repository": str(repo), "repository_name": repo.name,
                    "canonical": args.canonical,
                    "target_specs": specs,
                    "target_states": states,
                    "admissions": [admissions[index] for index in sorted(admissions)],
                }
                session.record_observation(
                    "environment.retirement.requested", "mncs-env.retirement",
                    intent_payload, request_id=request_id,
                    operation_digest=operation_digest,
                )
            except Exception as error:  # noqa: BLE001 - Store publication failures are ambiguous
                return fail(
                    f"retirement intent could not be published: {error}", code=3,
                    diagnostics={"code": "persistence-service-unavailable",
                                "request_id": request_id,
                                "mutation_performed": False},
                )
        else:
            intent_payload = intent_event.get("payload", {})
            stored_specs = intent_payload.get("target_specs")
            campaign = session.snapshot.get("campaign", {})
            current_campaign_id = campaign.get("identity") if isinstance(campaign, dict) else None
            if (intent_payload.get("schema_version")
                    != "mncs.environment.retirement-intent/1"
                    or intent_payload.get("session_id") != session.session_id
                    or intent_payload.get("consumer_id") != session.snapshot.get("consumer_id")
                    or intent_payload.get("authenticated_principal_id")
                    != session.snapshot.get("authenticated_principal_id")
                    or intent_payload.get("campaign_id") != current_campaign_id
                    or stored_specs != specs
                    or intent_payload.get("repository") != str(repo)
                    or intent_payload.get("canonical") != args.canonical):
                return fail(
                    "retirement request identity is bound to different targets",
                    code=3,
                    diagnostics={"code": "request-identity-conflict",
                                "request_id": request_id,
                                "mutation_performed": False},
                )
            target_states = intent_payload.get("target_states")
            recorded_admissions = intent_payload.get("admissions")
            if (not isinstance(target_states, list) or len(target_states) != len(specs)
                    or not isinstance(recorded_admissions, list)
                    or len(recorded_admissions) != len(specs)):
                return fail("durable retirement intent is malformed", code=3,
                            diagnostics={"code": "store-corruption",
                                         "request_id": request_id})
            for admission in recorded_admissions:
                if isinstance(admission, dict):
                    index = admission.get("target_index")
                    if (not isinstance(index, int) or index < 0 or index >= len(specs)
                            or admission.get("verdict") != "allow"
                            or admission.get("session_id") != session.session_id
                            or admission.get("consumer_id") != session.snapshot.get("consumer_id")
                            or admission.get("authenticated_principal_id")
                            != session.snapshot.get("authenticated_principal_id")
                            or admission.get("campaign_id")
                            != current_campaign_id
                            or admission.get("scope") != specs[index].get("scope")
                            or index in admissions):
                        return fail("durable retirement admission is malformed", code=3,
                                    diagnostics={"code": "store-corruption",
                                                 "request_id": request_id})
                    admissions[index] = admission
                else:
                    return fail("durable retirement admission is malformed", code=3,
                                diagnostics={"code": "store-corruption",
                                             "request_id": request_id})
            if set(admissions) != set(range(len(specs))):
                return fail("durable retirement admission is incomplete", code=3,
                            diagnostics={"code": "store-corruption",
                                         "request_id": request_id})

        states_by_index = {
            spec["index"]: state
            for spec, state in zip(specs, intent_payload.get("target_states", []), strict=False)
            if isinstance(state, dict)
        }
        results: list[dict[str, Any]] = []
        incomplete = False
        for spec in specs:
            target_request_id = _retirement_target_request_id(request_id, spec)
            target_digest = _retirement_target_operation_digest(operation_digest, spec)
            expected = states_by_index.get(spec["index"])
            if expected is None:
                return fail("durable retirement intent omits a target identity", code=3,
                            diagnostics={"code": "store-corruption",
                                         "request_id": request_id,
                                         "target": spec["target"]})
            try:
                prior = session.request_event(
                    "environment.retirement.target-completed",
                    target_request_id, target_digest,
                )
            except Exception as error:  # noqa: BLE001 - Store history is an external boundary
                return fail(
                    f"retirement target history is unavailable: {error}", code=3,
                    diagnostics={"code": "persistence-service-unavailable",
                                "request_id": request_id,
                                "target": spec["target"],
                                "mutation_performed": bool(results)},
                )
            if prior is not None:
                payload = prior.get("payload", {})
                result = payload.get("result")
                if (payload.get("parent_request_id") != request_id
                        or payload.get("schema_version")
                        != "mncs.environment.retirement-target-receipt/1"
                        or payload.get("session_id") != session.session_id
                        or payload.get("consumer_id") != session.snapshot.get("consumer_id")
                        or payload.get("authenticated_principal_id")
                        != session.snapshot.get("authenticated_principal_id")
                        or payload.get("campaign_id") != intent_payload.get("campaign_id")
                        or payload.get("target_spec") != spec
                        or payload.get("expected_target_state") != expected
                        or not isinstance(payload.get("admission"), dict)
                        or payload["admission"].get("verdict") != "allow"
                        or not isinstance(result, dict)
                        or result.get("retired") is not True):
                    return fail("durable retirement target receipt is malformed", code=3,
                                diagnostics={"code": "store-corruption",
                                             "request_id": request_id,
                                             "target": spec["target"]})
                results.append(result)
                continue

            transition = retire_module.reconcile_retirement_target(
                repo, spec["kind"], spec["target"], expected,
                sandbox_root=sandbox_root, execution_root=execution_root,
            )
            if transition["state"] == "completed":
                result = {"target": spec["target"], "kind": spec["kind"],
                          "retired": True, "status": "reconciled",
                          "reason": transition["reason"], "reconciled": True}
                admission = admissions.get(spec["index"], {})
            elif transition["state"] == "retry":
                try:
                    admission = _retirement_admission(session, repo, spec)
                except Exception as error:  # noqa: BLE001 - authority provider failures are structured
                    return fail(
                        f"retirement authority could not be revalidated: {error}",
                        code=3,
                        diagnostics={"code": "authority-revalidation-unavailable",
                                    "request_id": request_id,
                                    "target": spec["target"],
                                    "mutation_performed": bool(results)},
                    )
                if admission["verdict"] != "allow":
                    result = {"target": spec["target"], "kind": spec["kind"],
                              "retired": False, "status": "authority-denied",
                              "reason": admission.get("reason") or admission["verdict"]}
                    incomplete = True
                    results.append(result)
                    continue
                try:
                    claim_records = _retirement_store_claims(args)
                    repeated = retire_module.reconcile_retirement_target(
                        repo, spec["kind"], spec["target"], expected,
                        sandbox_root=sandbox_root, execution_root=execution_root,
                    )
                    if repeated["state"] != "retry":
                        result = {"target": spec["target"], "kind": spec["kind"],
                                  "retired": False, "status": "unknown",
                                  "reason": repeated.get("reason", "target changed before mutation")}
                        incomplete = True
                        results.append(result)
                        continue
                    result = _retirement_adapter(
                        repo, spec, expected, canonical=args.canonical,
                        claim_records=claim_records,
                        requesting_session=session.session_id,
                        sandbox_root=sandbox_root, execution_root=execution_root,
                    )
                except Exception as error:  # noqa: BLE001 - Git and Store can fail after intent
                    return fail(
                        f"retirement mutation outcome is ambiguous: {error}", code=3,
                        diagnostics={"code": "ambiguous-publication-outcome",
                                    "request_id": request_id,
                                    "target": spec["target"],
                                    "mutation_performed": bool(results)},
                    )
                if result.get("retired") is not True:
                    incomplete = True
                    results.append(result)
                    continue
            else:
                result = {"target": spec["target"], "kind": spec["kind"],
                          "retired": False, "status": "unknown",
                          "reason": transition.get("reason", "target state is ambiguous")}
                incomplete = True
                results.append(result)
                continue

            receipt_payload = {
                "schema_version": "mncs.environment.retirement-target-receipt/1",
                "parent_request_id": request_id,
                "session_id": session.session_id,
                "consumer_id": session.snapshot.get("consumer_id"),
                "authenticated_principal_id": session.snapshot.get("authenticated_principal_id"),
                "campaign_id": intent_payload.get("campaign_id"),
                "target_spec": spec,
                "expected_target_state": expected,
                "admission": admission,
                "result": result,
            }
            try:
                session.record_observation(
                    "environment.retirement.target-completed", "mncs-env.retirement",
                    receipt_payload, request_id=target_request_id,
                    operation_digest=target_digest,
                )
            except Exception as error:  # noqa: BLE001 - receipt publication follows Git mutation
                return fail(
                    f"retirement may have completed but its receipt is unavailable: {error}",
                    code=3,
                    diagnostics={"code": "ambiguous-publication-outcome",
                                "request_id": request_id,
                                "target": spec["target"],
                                "mutation_performed": True},
                )
            results.append(result)

        response = {
            "session_id": session.session_id, "repository": str(repo),
            "canonical": args.canonical, "dry_run": False,
            "request_id": request_id,
            "completion_status": "pending" if incomplete else "completed",
            "mutation_performed": any(item.get("retired") is True for item in results),
            "results": results,
            "exit_code": 3 if incomplete else 0,
        }
        if incomplete:
            out(response)
            return 3
        try:
            session.record_observation(
                "environment.retirement.completed", "mncs-env.retirement",
                {"schema_version": "mncs.environment.retirement-receipt/1",
                 "response": response}, request_id=request_id,
                operation_digest=operation_digest,
            )
        except Exception as error:  # noqa: BLE001 - final Store receipt can be ambiguous
            return fail(
                f"retirement completed but its final receipt is unavailable: {error}",
                code=3,
                diagnostics={"code": "ambiguous-publication-outcome",
                            "request_id": request_id,
                            "mutation_performed": response["mutation_performed"]},
            )
        out(response)
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
        if args.generation:
            out({"generation": store.backend.generation()})
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
        with entry_module.entry_lock(args.state_dir, args.persistence), \
                closing(_open(args)) as session:
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
    enter.add_argument("--campaign-id", default=None,
                       help="resume a durable campaign identity (authorized Control transport only)")
    enter.add_argument(
        "--task-identity", default=None,
        help=("select a durable campaign using an authenticated, project-scoped task identity; "
              "a unique exact continuation is adopted; duplicate candidates remain ambiguous"),
    )
    enter.add_argument("--new-session", action="store_true", help="create independent work instead of reusing matching work")
    enter.set_defaults(func=cmd_enter)

    continuation = sub.add_parser(
        "continuation",
        help="discover the authenticated campaign capsule for this protected project",
    )
    continuation.add_argument(
        "--campaign-id", default=None,
        help="select one durable campaign when the project has multiple continuations",
    )
    continuation.add_argument(
        "--task-identity", default=None,
        help="resolve a previously bound identity scoped to this authenticated Environment definition and workspace",
    )
    continuation.set_defaults(func=cmd_campaign_continuation)

    task_bind = sub.add_parser(
        "campaign-bind",
        help="bind a stable task identity to one explicitly selected project session",
    )
    task_bind.add_argument("--campaign-id", required=True)
    task_bind.add_argument("--session-id", required=True)
    task_bind.add_argument("--task-identity", required=True)
    task_bind.set_defaults(func=cmd_campaign_task_bind)

    def session_parser(name: str, help_text: str):
        parser_ = sub.add_parser(name, parents=[common], help=help_text)
        parser_.add_argument("session")
        return parser_

    campaign_state = session_parser(
        "campaign-state",
        "read a bounded page from the durable campaign evidence and delivery ledger",
    )
    campaign_state.add_argument("--evidence-offset", type=int, default=None)
    campaign_state.add_argument(
        "--evidence-limit", type=int, default=sessions_module.CAMPAIGN_EVIDENCE_MAX,
    )
    campaign_state.set_defaults(func=cmd_campaign_state)

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
    projections.add_argument('--interpret', choices=(
        'capabilities', 'dependencies', 'blockers', 'architecture',
        'structure', 'why', 'environmental-capability'))
    projections.add_argument('--repository')
    projections.add_argument(
        '--history-pages', type=int, default=1,
        help="include this many bounded campaign evidence pages in environmental capability analysis (1-4)",
    )
    projections.add_argument(
        '--evidence-offset', type=int, default=None,
        help="start environmental capability analysis at this campaign evidence offset",
    )
    projections.add_argument(
        '--evidence-limit', type=int,
        default=capability_intelligence_module.MAX_EVIDENCE_REFS,
        help="campaign evidence references per page for environmental capability analysis (1-32)",
    )
    projections.add_argument(
        '--session-event-before-sequence', type=int, default=None,
        help="read the bounded session event page preceding this sequence",
    )
    projections.add_argument(
        '--session-event-limit', type=int,
        default=capability_intelligence_module.MAX_SESSION_EVENT_WINDOW,
        help="session event records per environmental capability page (1-256)",
    )
    projections.add_argument("--evidence", action="store_true",
                             help="print the ambient projection evidence trail (without --interpret)")
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

    scope_grant = session_parser(
        "scope-grant",
        "admit one owner-authorized repository scope into the current campaign",
    )
    scope_grant.add_argument("--campaign-id", required=True)
    scope_grant.add_argument("--repository", required=True)
    scope_grant.add_argument("--expected-authority-revision", required=True)
    scope_grant.add_argument("--approval-reference", required=True)
    scope_grant.add_argument("--owner-authorized-checkout-json", default=None)
    scope_grant.set_defaults(func=cmd_scope_grant)

    invoke = session_parser("invoke", "invoke a bound capability")
    invoke.add_argument(
        "--effect-repository", default=None,
        help="selected repository whose checkout is the provider's mutation target",
    )
    invoke.add_argument(
        "--input-artifact", action="append", nargs=4, default=[],
        metavar=("NAME", "SOURCE_CAPABILITY", "SOURCE_BINDING_ID", "PATH"),
        help=("pass one bounded file from the latest successful read-only Store artifact "
              "to the provider; repeat for additional named inputs"),
    )
    invoke.add_argument("capability")
    invoke.add_argument("--cwd", default=None)
    # Let Session.invoke use the binding's validated descriptor timeout unless
    # the caller explicitly narrows or extends it. The capability layer keeps
    # the existing 120-second fallback for bindings without a declaration.
    invoke.add_argument("--timeout", type=int, default=None)
    invoke.add_argument("--output-limit-bytes", type=int,
                        default=None,
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
    checkpoint.add_argument(
        "--campaign-base-checkpoint", default=None,
        help="campaign state checkpoint from the current capsule, or 'none' initially",
    )
    checkpoint.add_argument(
        "--campaign-evidence-json", default=None,
        help=("bounded JSON evidence window in append order; when new references "
              "overflow the window, only the oldest prefix may roll off because "
              "earlier immutable checkpoints retain the history"),
    )
    checkpoint.add_argument(
        "--campaign-evidence-delta-json", default=None,
        help=("bounded JSON array of new immutable evidence references; the "
              "Environment owner appends it to history after validating the "
              "current --campaign-base-checkpoint. Initialize campaign state "
              "with --campaign-evidence-json first"),
    )
    checkpoint.add_argument(
        "--campaign-delivery-json", default=None,
        help="bounded JSON delivery report; recorded with the checkpoint and its provenance",
    )
    checkpoint.set_defaults(func=cmd_checkpoint)

    handoff = session_parser("handoff", "hand off to another consumer")
    handoff.add_argument("--to", required=True)
    handoff.add_argument(
        "--to-principal", default=None,
        help="authenticated Control principal receiving the handoff (defaults to this principal)",
    )
    handoff.add_argument("--notes", nargs="*", default=[])
    handoff.add_argument("--blockers", nargs="*", default=[])
    handoff.add_argument("--next", nargs="*", default=[])
    handoff.set_defaults(func=cmd_handoff)

    accept = session_parser("accept", "accept a handed-off session")
    accept.add_argument("handoff", help="handoff identity to accept")
    accept.set_defaults(func=cmd_accept)

    complete = session_parser("complete", "complete a session")
    completion_mode = complete.add_mutually_exclusive_group(required=True)
    completion_mode.add_argument("--outcome")
    completion_mode.add_argument(
        "--retry-pending", action="store_true",
        help="reconcile a previously interrupted completion using its stored request identity",
    )
    complete.add_argument("--summary", default="")
    complete.set_defaults(func=cmd_complete)

    fail_cmd = session_parser("fail", "fail a session")
    fail_cmd.add_argument("--reason", required=True)
    fail_cmd.set_defaults(func=cmd_fail)

    claims = sub.add_parser("claims", help="inspect or manage workspace claims")
    claims.add_argument("session")
    claims.add_argument("--acquire", default=None)
    claims.add_argument(
        "--expected-store-generation", type=int, default=None,
        help="fence this claim against the current canonical Store generation",
    )
    claims.add_argument("--release", default=None)
    claims.add_argument(
        "--claim-id", default=None,
        help="target one claim for release/transfer, or inspect its latest record",
    )
    claims.add_argument(
        "--repository", default=None,
        help="read a bounded page of latest claim records for one repository",
    )
    claims.add_argument("--offset", type=int, default=None,
                        help="claim page offset (requires --repository)")
    claims.add_argument("--limit", type=int, default=None,
                        help="claim page size from 1 to 128 (requires --repository)")
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
    claims.add_argument("--ttl", type=int, default=24,
                        help="claim lease in hours (1..168; default 24)")
    claims.add_argument("--explain", default=None,
                        help="explain one claim id as live/stale/recoverable/not-recoverable")
    claims.set_defaults(func=cmd_claims)

    claim_recover = sub.add_parser(
        "claim-recover", help="request one Control-owner-approved claim recovery"
    )
    claim_recover.add_argument("session")
    claim_recover.add_argument("--campaign-id", required=True)
    claim_recover.add_argument("--claim-id", required=True)
    claim_recover.add_argument("--repository", required=True)
    claim_recover.add_argument("--owner-session-id", required=True)
    claim_recover.add_argument("--expected-version", type=int, required=True)
    claim_recover.add_argument("--approval-reference", required=True)
    claim_recover.add_argument("--reason", required=True)
    claim_recover.set_defaults(func=cmd_claim_recover)

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
    store_reads = store.add_mutually_exclusive_group()
    store_reads.add_argument("--generation", action="store_true",
                             help="read the selected canonical Store generation only")
    store_reads.add_argument("--verify", action="store_true")
    store.set_defaults(func=cmd_store)

    artifact_read = sub.add_parser(
        "artifact-read", help="read one bounded file from a capability Store artifact",
    )
    artifact_read.add_argument("session")
    artifact_read.add_argument("capability")
    artifact_read.add_argument("binding_id")
    artifact_read.add_argument("path")
    artifact_read.set_defaults(func=cmd_artifact_read)

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
    retire.add_argument("--inventory", action="store_true",
                        help="emit all branch/worktree dispositions; requires --dry-run")
    retire.add_argument("--execution-root", type=Path, help=argparse.SUPPRESS)
    retire.add_argument("--sandbox-root", type=Path, help=argparse.SUPPRESS)
    retire.add_argument("--request-id",
                        help="stable request identity for explicit retry/reconciliation")
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
    actual_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(actual_argv)
    if os.environ.get("MNCS_ENV_PERSISTENCE_TRANSPORT") == "control-rpc":
        from . import rpc_client
        return rpc_client.dispatch(actual_argv)
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
        store_code = (
            error.code if isinstance(error, StoreUnavailable)
            else "store-integrity-failure"
        )
        next_step = (
            "run this operation through the authenticated MNCS Control Environment transport"
            if store_code == "direct-filesystem-read-only"
            else "bind the intended Store checkout with MNCS_STORE_PYTHON; inspect Store recovery before retrying"
        )
        return fail(str(error), diagnostics={"code": store_code,
                                           "provider": "mncs-store", "state_dir": str(args.state_dir),
                                           "publication_completed": False,
                                           "transport": "direct-filesystem",
                                           "next": next_step})
    except (OSError, ValueError, sessions_module.LifecycleError) as error:
        message = str(error)
        code = "environment-command-failed"
        next_step = "check configuration and session state; retry entry"
        if (
            message.startswith("declared provider checkout ")
            and message.endswith(" changed")
        ):
            code = "declared-provider-checkout-changed"
            session_id = getattr(args, "session", None)
            if isinstance(session_id, str) and session_id:
                next_step = (
                    "inspect the current and declared provider checkout; if the "
                    "change is intended, run `mncs-env reconcile "
                    f"{session_id}` to refresh the session's declared provider "
                    "binding, then retry; if unexpected, restore or review the "
                    "provider checkout before reconciling"
                )
            else:
                next_step = (
                    "inspect the current and declared provider checkout; if the "
                    "change is intended, reconcile the affected session to "
                    "refresh its declared provider binding before retrying; if "
                    "unexpected, restore or review the provider checkout first"
                )
        return fail(message, diagnostics={
            "code": code,
            "command": args.command,
            "state_dir": str(args.state_dir),
            "next": next_step,
        })


if __name__ == "__main__":
    raise SystemExit(main())
