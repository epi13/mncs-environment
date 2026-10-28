from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

from mncs_env.sessions import Session


def test_context_exposes_bounded_historical_capability_projection() -> None:
    state_dir = Path("/tmp/mncs state")
    session = SimpleNamespace(
        session_id="ses_history_projection",
        state_dir=state_dir,
        store=SimpleNamespace(backend=object()),
    )

    actions = Session.actions(session)
    latest = actions["environmental_capability_projection"]["argv"]
    activity = actions["session_activity_projection"]["argv"]
    resources = actions["session_resource_samples_projection"]["argv"]
    history = actions["environmental_capability_history_projection"]["argv"]
    prefix = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "scripts" / "mncs-env"),
        "--state-dir", str(state_dir.resolve()),
        "--persistence", "store",
    ]

    assert latest == [
        *prefix, "projections", "ses_history_projection",
        "--interpret", "environmental-capability",
    ]
    assert activity == [
        *prefix, "projections", "ses_history_projection",
        "--interpret", "session-activity",
    ]
    assert resources == [
        *prefix, "projections", "ses_history_projection",
        "--interpret", "session-activity", "--activity-section", "resources",
    ]
    assert history == [
        *prefix, "projections", "ses_history_projection",
        "--interpret", "environmental-capability",
        "--history-pages", "4", "--evidence-limit", "8",
    ]
