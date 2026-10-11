from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from mncs_env import coherence, observations
from mncs_env.evidence_store import is_store_reference, read
from mncs_env.session_store import open_store


def _session(state_dir: Path) -> SimpleNamespace:
    store = open_store(state_dir, "store", verify_on_open=False)
    return SimpleNamespace(
        session_id="ses_store_evidence_test",
        state_dir=state_dir,
        store=store,
    )


def test_repository_observation_catalogue_uses_store_and_reuses_exact_content():
    with TemporaryDirectory() as directory:
        base = Path(directory)
        checkout = base / "checkout"
        checkout.mkdir()
        subprocess.run(["git", "init", "-q", str(checkout)], check=True)
        source = checkout / "source.txt"
        source.write_text("first\n", encoding="utf-8")
        session = _session(base / "state")
        try:
            first, events, _ = observations.observe_repository(session, checkout, None)
            assert events == []
            assert is_store_reference(first["reference"])
            assert session.store.generation() == 1
            assert not (session.state_dir / "sessions" / session.session_id).exists()
            assert read(session, observations.REPOSITORY_CATALOGUE_OWNER,
                        first["reference"])["checkout"] == str(checkout.resolve())
            session.store.close()

            # Reopen a fresh Store client and read the owner-bound immutable
            # catalogue before the next observation. This is a real isolated
            # EmbeddedStore, never the canonical Environment Store.
            session = _session(base / "state")
            same, events, _ = observations.observe_repository(session, checkout, first)
            assert same == first
            assert events == []
            assert session.store.generation() == 1

            source.write_text("second\n", encoding="utf-8")
            changed, events, _ = observations.observe_repository(session, checkout, first)
            assert changed["reference"] != first["reference"]
            assert any(event["kind"] == "file.changed" for event in events)
            assert session.store.generation() == 2
            assert not (session.state_dir / "sessions" / session.session_id).exists()
        finally:
            session.store.close()


def test_incremental_coherence_result_references_use_store_without_sidecars():
    with TemporaryDirectory() as directory:
        state_dir = Path(directory) / "state"
        session = _session(state_dir)
        results = {"doctor": {"summary": {"reconciled": 1}, "reused": False}}
        try:
            refs = coherence._result_refs(session, results)
            assert is_store_reference(refs["doctor"]["reference"])
            assert "artifact" not in refs["doctor"]
            assert not (state_dir / "sessions" / session.session_id).exists()
            assert session.store.generation() == 1
            session.store.close()

            session = _session(state_dir)
            loaded, known = coherence._load_results(session, refs)
            assert known is True
            assert loaded == results
            assert session.store.generation() == 1
        finally:
            session.store.close()


def test_file_debug_backend_keeps_explicit_sidecar_projection():
    with TemporaryDirectory() as directory:
        state_dir = Path(directory) / "state"
        session = SimpleNamespace(
            session_id="ses_file_evidence_test",
            state_dir=state_dir,
            store=SimpleNamespace(state_dir=state_dir),
        )
        results = {"doctor": {"summary": {"reconciled": 1}}}

        refs = coherence._result_refs(session, results)
        assert "artifact" in refs["doctor"]
        loaded, known = coherence._load_results(session, refs)
        assert known is True
        assert loaded == results
        assert Path(refs["doctor"]["artifact"]).is_file()
