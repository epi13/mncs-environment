#!/usr/bin/env python3
"""End-to-end vertical proof for mncs-environment (real workspace, real capability).

Structured WorkIntent
      -> environment resolution (real workspace discovery)
      -> session created
      -> real service capability invoked (atlas context capsule)
      -> result/event recorded
      -> checkpoint persisted
      -> NEW PROCESS resumes the session (via the CLI)
      -> handoff to a different consumer identity
      -> completion

Every step asserts. The workspace is only read; all mutations land in the
state directory. Distinguishes real proof from mocks: the invoked
capability is repository-owned code producing real output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from mncs_env import sessions  # noqa: E402

WORKSPACE_OVERRIDE = os.environ.get("MNCS_VERTICAL_WORKSPACE")
PROOF_CONSUMER_A = "proof-agent-a"
PROOF_CONSUMER_B = "proof-agent-b"


def cli(state_dir: Path, *args: str) -> dict:
    completed = subprocess.run(
        [sys.executable, "-m", "mncs_env.cli", "--state-dir", str(state_dir), *args],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"cli {' '.join(args)} failed: {completed.stderr[-2000:]}")
    return json.loads(completed.stdout)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="mncs-env-proof-") as directory:
        state = Path(directory) / "state"
        if WORKSPACE_OVERRIDE:
            workspace = Path(WORKSPACE_OVERRIDE).expanduser().resolve()
        else:
            # Read providers through bounded, isolated Git checkouts. Shared
            # objects avoid copying history; foreign working trees stay untouched.
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            for repository in ("mncs-atlas", "mncs-language"):
                subprocess.run(["git", "clone", "--shared", "--quiet",
                                str(REPO.parent / repository), str(workspace / repository)], check=True)
        atlas = workspace / "mncs-atlas"
        definition = json.loads(
            (REPO / "examples" / "development-environment" / "environment.json").read_text()
        )

        # 1. Resolve.
        environment = sessions.resolve_environment(
            definition=definition, workspace_root=workspace, state_dir=state,
            consumer_id=PROOF_CONSUMER_A,
        )
        repos = {repo["name"] for repo in environment["workspace"]["repositories"]}
        assert {"mncs-atlas", "mncs-language"} <= repos, "workspace discovery failed"
        assert "mncs-language" in environment["protected_repositories"]
        print(f"1. resolved {environment['identity']} "
              f"({environment['workspace']['repository_count']} repos, "
              f"{len(environment['bindings'])} bindings)")

        # 2. Enter.
        entered = cli(state, "enter", "--definition",
                      str(REPO / "examples" / "development-environment" / "environment.json"),
                      "--workspace", str(workspace), "--consumer", PROOF_CONSUMER_A)
        session_id = entered["session_id"]
        assert entered["lifecycle"] == "active", entered["lifecycle"]
        print(f"2. entered session {session_id}")

        # 3. Invoke a REAL capability: the atlas context capsule.
        caps = cli(state, "capabilities", session_id)
        names = [binding["capability"] for binding in caps]
        assert "mncs-atlas:context-capsule" in names, names
        result = cli(state, "invoke", session_id, "mncs-atlas:context-capsule",
                     "--cwd", str(atlas), "--", "context", str(atlas))
        assert result["status"] == "ok", result
        assert "mncs-atlas" in result["stdout"], result["stdout"][:200]
        print(f"3. invoked mncs-atlas:context-capsule "
              f"({len(result['stdout'])} bytes of real provider output)")

        # 4. Authority denial is enforced, not assumed.
        denied = subprocess.run(
            [sys.executable, "-m", "mncs_env.cli", "--state-dir", str(state),
             "authority", session_id, "--action", "write", "--target", "mncs-language"],
            cwd=str(REPO), capture_output=True, text=True, check=False,
        )
        evaluation = json.loads(denied.stdout)["evaluation"]
        assert evaluation["verdict"] == "deny", evaluation
        print("4. protected write to mncs-language denied with reason")

        # 5. Checkpoint.
        checkpoint = cli(state, "checkpoint", session_id, "--progress", "proof midpoint",
                         "--remaining", "resume", "handoff", "complete")
        assert checkpoint["identity"].startswith("chk_")
        print(f"5. checkpoint {checkpoint['identity']}")

        # 6. NEW PROCESS resumes (proves persistence beyond one process).
        resumed = cli(state, "resume", session_id, "--revalidate", "--workspace", str(workspace))
        assert resumed["session_id"] == session_id
        assert resumed["intent"]["goal"].startswith("prove the mncs-environment")
        assert any(a["capability"] == "mncs-atlas:context-capsule" for a in resumed["bindings"])
        print(f"6. resumed in a new process: intent, bindings, "
              f"{resumed['event_count']} events intact")

        # 7. Handoff to a different consumer identity.
        handoff = cli(state, "handoff", session_id, "--to", PROOF_CONSUMER_B,
                      "--notes", "vertical proof handoff", "--next", "complete")
        assert handoff["to_consumer"] == PROOF_CONSUMER_B
        accepted = cli(state, "accept", session_id, handoff["identity"],
                       "--consumer", PROOF_CONSUMER_B)
        assert accepted["consumer_id"] == PROOF_CONSUMER_B
        print(f"7. handoff {handoff['identity']} accepted by {PROOF_CONSUMER_B}")

        # 8. Complete.
        done = cli(state, "complete", session_id, "--outcome", "vertical proof green",
                   "--summary", "real capability invoked, resumed, handed off")
        assert done["outcome"] == "vertical proof green"
        final = cli(state, "inspect", session_id)
        assert final["lifecycle"] == "completed"
        print(f"8. completed; {final['event_count']} events, "
              f"{len(final['checkpoints'])} checkpoints, {len(final['handoffs'])} handoffs")

    print("VERTICAL PROOF: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
