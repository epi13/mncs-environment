"""mncs-env: thin inspection/control surface over the session system.

Every command reads or mutates environment-owned session state only. The
CLI parses arguments and prints JSON; all semantics live in the package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import leases as leases_module
from . import pressures as pressures_module
from . import sessions as sessions_module
from . import workspace as workspace_module
from .persist import read_json

DEFAULT_STATE_DIR = Path.home() / ".local" / "share" / "mncs-environment"


def out(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def fail(message: str, code: int = 2) -> int:
    print(json.dumps({"error": message}), file=sys.stderr)
    return code


def load_definition(path: Path) -> dict:
    raw = read_json(path)
    if not isinstance(raw, dict):
        raise SystemExit(fail(f"{path} is not a JSON object"))
    return raw


def cmd_resolve(args: argparse.Namespace) -> int:
    definition = load_definition(args.definition)
    workspace_root = args.workspace or definition.get("workspace_root", ".")
    environment = sessions_module.resolve_environment(
        definition=definition,
        workspace_root=workspace_root,
        state_dir=args.state_dir,
        consumer_id=args.consumer,
    )
    out(environment)
    return 0


def cmd_enter(args: argparse.Namespace) -> int:
    definition = load_definition(args.definition)
    workspace_root = args.workspace or definition.get("workspace_root", ".")
    environment = sessions_module.resolve_environment(
        definition=definition,
        workspace_root=workspace_root,
        state_dir=args.state_dir,
        consumer_id=args.consumer,
    )
    session = sessions_module.Session.create(
        state_dir=args.state_dir,
        environment=environment,
        consumer_id=args.consumer,
        consumer_kind=args.consumer_kind,
    )
    session.transition("resolving", "enter: resolving environment")
    session.transition("ready", "environment resolved")
    session.transition("active", f"consumer {args.consumer} entered")
    out(session.inspect())
    return 0


def _open(args: argparse.Namespace) -> sessions_module.Session:
    try:
        return sessions_module.Session.resume(state_dir=args.state_dir, session_id=args.session)
    except sessions_module.LifecycleError as error:
        raise SystemExit(fail(str(error)))


def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        session = sessions_module.Session(args.state_dir, args.session)
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    out(session.inspect())
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    session = _open(args)
    if args.revalidate:
        revalidation = session.revalidate()
        session.observe_workspace(args.workspace or ".")
    else:
        revalidation = {"reprobed": 0, "changed": []}
    result = session.inspect()
    result["revalidation"] = revalidation
    if args.consumer and args.consumer != session.snapshot.get("consumer_id"):
        result["handoff_hint"] = (
            f"session belongs to {session.snapshot.get('consumer_id')}; "
            "use handoff accept to transfer"
        )
    out(result)
    return 0


def cmd_capabilities(args: argparse.Namespace) -> int:
    session = _open(args)
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
    session = _open(args)
    try:
        result = session.invoke(
            args.capability, args.argv, cwd=args.cwd, timeout_seconds=args.timeout
        )
    except (sessions_module.AuthorityDenied, sessions_module.LifecycleError) as error:
        return fail(str(error), code=3)
    out(result)
    return 0 if result["status"] == "ok" else 4


def cmd_events(args: argparse.Namespace) -> int:
    session = _open(args)
    if args.subscribe:
        out(session.subscribe(args.subscribe, source_filter=args.source))
        return 0
    if args.poll:
        out(session.poll(args.poll))
        return 0
    if args.observe_workspace:
        out(session.observe_workspace(args.observe_workspace))
        return 0
    out(session.inspect()["latest_events"])
    return 0


def cmd_checkpoint(args: argparse.Namespace) -> int:
    session = _open(args)
    out(session.checkpoint(progress=args.progress, remaining=args.remaining))
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    session = _open(args)
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
    session = _open(args)
    out(session.accept_handoff(consumer_id=args.consumer, consumer_kind=args.consumer_kind))
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    session = _open(args)
    try:
        out(session.complete(outcome=args.outcome, summary=args.summary))
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    return 0


def cmd_fail(args: argparse.Namespace) -> int:
    session = _open(args)
    try:
        out(session.fail(reason=args.reason))
    except sessions_module.LifecycleError as error:
        return fail(str(error))
    return 0


def cmd_leases(args: argparse.Namespace) -> int:
    state = Path(args.state_dir)
    if args.release:
        out({"released": leases_module.release(state, repository=args.release, owner_session=args.owner)})
        return 0
    if args.acquire:
        try:
            out(leases_module.acquire(
                state, repository=args.acquire, owner_session=args.owner,
                reason=args.reason or "", ttl_hours=args.ttl))
        except leases_module.LeaseConflict as error:
            return fail(str(error), code=3)
        return 0
    out(leases_module.read_leases(state))
    return 0


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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mncs-env")
    parser.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
    sub = parser.add_subparsers(dest="command", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--consumer", default="local-agent")
    common.add_argument("--consumer-kind", default="agent")

    resolve = sub.add_parser("resolve", help="resolve an environment definition")
    resolve.add_argument("--definition", type=Path, required=True)
    resolve.add_argument("--workspace", default=None)
    resolve.add_argument("--consumer", default="local-agent")
    resolve.set_defaults(func=cmd_resolve)

    enter = sub.add_parser("enter", help="resolve and enter (create a session)")
    enter.add_argument("--definition", type=Path, required=True)
    enter.add_argument("--workspace", default=None)
    enter.add_argument("--consumer", default="local-agent")
    enter.add_argument("--consumer-kind", default="agent")
    enter.set_defaults(func=cmd_enter)

    def session_parser(name: str, help_text: str):
        parser_ = sub.add_parser(name, parents=[common], help=help_text)
        parser_.add_argument("session")
        return parser_

    inspect = session_parser("inspect", "inspect session state")
    inspect.set_defaults(func=cmd_inspect)

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
    invoke.add_argument("argv", nargs="*")
    invoke.set_defaults(func=cmd_invoke)

    events = session_parser("events", "subscribe, poll, or observe events")
    events.add_argument("--subscribe", nargs="*", default=None)
    events.add_argument("--source", default=None)
    events.add_argument("--poll", default=None)
    events.add_argument("--observe-workspace", default=None)
    events.set_defaults(func=cmd_events)

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
    accept.set_defaults(func=cmd_accept)

    complete = session_parser("complete", "complete a session")
    complete.add_argument("--outcome", required=True)
    complete.add_argument("--summary", default="")
    complete.set_defaults(func=cmd_complete)

    fail_cmd = session_parser("fail", "fail a session")
    fail_cmd.add_argument("--reason", required=True)
    fail_cmd.set_defaults(func=cmd_fail)

    leases = sub.add_parser("leases", help="inspect or manage workspace leases")
    leases.add_argument("--acquire", default=None)
    leases.add_argument("--release", default=None)
    leases.add_argument("--owner", default="local-agent")
    leases.add_argument("--reason", default="")
    leases.add_argument("--ttl", type=int, default=24)
    leases.set_defaults(func=cmd_leases)

    pressures = sub.add_parser("pressures", help="record or list pressures")
    pressures.add_argument("--record", type=Path, default=None)
    pressures.set_defaults(func=cmd_pressures)

    workspace = sub.add_parser("workspace", help="discover workspace repositories")
    workspace.add_argument("--root", default=".")
    workspace.set_defaults(func=cmd_workspace)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
