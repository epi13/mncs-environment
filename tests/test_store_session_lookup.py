"""Unit and real-Store integration proofs for generation-keyed session lookup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from mncs_env import claims, selection
from mncs_env.session_store import open_store
from mncs_env.store_backend import (
    SCHEMA_CLAIM,
    SCHEMA_EVENT,
    SCHEMA_PROJECTION,
    SCHEMA_SNAPSHOT,
    SCHEMA_SNAPSHOT_SECTION,
    StoreBackend,
    StoreIntegrityFailure,
    _BINDING_DIRECTORY_MAX_GENERATIONS,
    _SNAPSHOT_SECTIONS_FIELD,
)


class _ExactLookupStub:
    """In-memory stub mirroring EmbeddedStore's generation + exact binding contract."""

    def __init__(self, generation: int, bindings: list[tuple[bytes, bytes]], payloads: dict[tuple[bytes, bytes], bytes]):
        self.current_generation = generation
        self._bindings = list(bindings)
        self._payloads = dict(payloads)
        self.domain_bindings_at_calls = 0
        self.get_bound_calls: list[tuple[bytes, bytes]] = []
        self.find_bound_calls: list[tuple[bytes, bytes]] = []

    def domain_bindings_at(self, generation: int) -> tuple[tuple[bytes, bytes], ...]:
        assert generation == self.current_generation
        self.domain_bindings_at_calls += 1
        return tuple(self._bindings)

    def get_bound_object(self, schema: bytes, identity: bytes):
        self.get_bound_calls.append((schema, identity))
        key = (schema, identity)
        if key not in self._payloads:
            raise KeyError(key)
        payload = self._payloads[key]
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(domain_schema=schema, domain_identity=identity, payload=payload)

    def find_bound_objects(self, schema: bytes, prefix: bytes = b""):
        self.find_bound_calls.append((schema, prefix))
        return [
            SimpleNamespace(domain_schema=sch, domain_identity=ident, payload=data)
            for (sch, ident), data in self._payloads.items()
            if sch == schema and ident.startswith(prefix) and not isinstance(data, Exception)
        ]


def test_binding_directory_isolates_prefixes_malformed_and_unrelated_schemas():
    bindings = [
        (SCHEMA_SNAPSHOT, b"ses_1:snap:0000000001"),
        (SCHEMA_SNAPSHOT, b"ses_1:snap:0000000002"),
        (SCHEMA_SNAPSHOT, b"ses_10:snap:0000000001"),
        (SCHEMA_SNAPSHOT, b"ses_1:snap:bad-suffix"),
        (b"other.schema/1", b"ses_1:snap:0000000099"),
        (SCHEMA_SNAPSHOT, b"ses_1:chk:chk_a"),
        (SCHEMA_SNAPSHOT, b"ses_10:chk:chk_b"),
        (SCHEMA_EVENT, b"ses_1:evt:0000000001"),
        (SCHEMA_EVENT, b"ses_1:evt:2"),  # unpadded non-canonical
        (SCHEMA_EVENT, b"ses_10:evt:0000000001"),
        (SCHEMA_CLAIM, b"claim:repo:with:colon:0000000001"),
        (SCHEMA_CLAIM, b"claim:repo:with:colon:0000000002"),
        (SCHEMA_PROJECTION, b"proj:a:0000000001"),
        (SCHEMA_PROJECTION, b"proj:a:0000000003"),
    ]
    payloads = {
        (SCHEMA_SNAPSHOT, b"ses_1:snap:0000000001"): json.dumps({"session_id": "ses_1", "snapshot_sequence": 1}).encode(),
        (SCHEMA_SNAPSHOT, b"ses_1:snap:0000000002"): json.dumps({"session_id": "ses_1", "snapshot_sequence": 2}).encode(),
        (SCHEMA_SNAPSHOT, b"ses_10:snap:0000000001"): json.dumps({"session_id": "ses_10", "snapshot_sequence": 1}).encode(),
        (SCHEMA_SNAPSHOT, b"ses_1:chk:chk_a"): json.dumps({"session_id": "ses_1", "identity": "chk_a", "sequence": 1}).encode(),
        (SCHEMA_SNAPSHOT, b"ses_10:chk:chk_b"): json.dumps({"session_id": "ses_10", "identity": "chk_b", "sequence": 1}).encode(),
        (SCHEMA_EVENT, b"ses_1:evt:0000000001"): json.dumps({"session_id": "ses_1", "sequence": 1, "identity": "e1"}).encode(),
        (SCHEMA_EVENT, b"ses_1:evt:2"): json.dumps({"session_id": "ses_1", "sequence": 2, "identity": "e2"}).encode(),
        (SCHEMA_CLAIM, b"claim:repo:with:colon:0000000002"): json.dumps({"claim_id": "repo:with:colon", "version": 2}).encode(),
        (SCHEMA_PROJECTION, b"proj:a:0000000003"): json.dumps({"ok": True}).encode(),
    }
    stub = _ExactLookupStub(7, bindings, payloads)
    backend = StoreBackend.__new__(StoreBackend)
    backend._store = stub

    assert backend.list_sessions() == ["ses_1", "ses_10"]
    assert backend.read_snapshot("ses_1") == {"session_id": "ses_1", "snapshot_sequence": 2}
    assert backend.read_snapshot("ses_10") == {"session_id": "ses_10", "snapshot_sequence": 1}
    assert backend.read_snapshot("ses_missing") is None
    assert [c["identity"] for c in backend.list_checkpoints("ses_1")] == ["chk_a"]
    page = backend.read_event_page("ses_1", limit=10)
    assert page["event_count"] == 1
    assert [e["identity"] for e in page["events"]] == ["e1"]
    assert len(backend.read_events("ses_1")) == 2
    assert backend.read_projection_versions() == {"proj:a": 3}
    assert backend.read_projection_row("proj:a") == (3, {"ok": True})
    assert backend.read_latest_claims_for(["repo:with:colon"]) == [
        {"claim_id": "repo:with:colon", "version": 2}
    ]
    assert stub.domain_bindings_at_calls == 1
    assert stub.find_bound_calls == []


def test_store_backend_stub_without_init_invalidates_when_store_reassigned():
    first = _ExactLookupStub(
        7,
        [(SCHEMA_SNAPSHOT, b"ses_a:snap:0000000001")],
        {(SCHEMA_SNAPSHOT, b"ses_a:snap:0000000001"): json.dumps({"v": 1}).encode()},
    )
    second = _ExactLookupStub(
        7,
        [(SCHEMA_SNAPSHOT, b"ses_b:snap:0000000001")],
        {(SCHEMA_SNAPSHOT, b"ses_b:snap:0000000001"): json.dumps({"v": 2}).encode()},
    )
    backend = StoreBackend.__new__(StoreBackend)
    backend._store = first
    assert backend.list_sessions() == ["ses_a"]
    assert backend.read_snapshot("ses_a") == {"v": 1}

    backend._store = second
    assert backend.list_sessions() == ["ses_b"]
    assert backend.read_snapshot("ses_a") is None
    assert backend.read_snapshot("ses_b") == {"v": 2}


def test_read_snapshot_retries_once_on_mid_lookup_generation_advance():
    class AdvancingStore(_ExactLookupStub):
        def __init__(self):
            super().__init__(10, [(SCHEMA_SNAPSHOT, b"ses_r:snap:0000000001")], {})
            self.by_gen = {
                10: [(SCHEMA_SNAPSHOT, b"ses_r:snap:0000000001")],
                11: [
                    (SCHEMA_SNAPSHOT, b"ses_r:snap:0000000001"),
                    (SCHEMA_SNAPSHOT, b"ses_r:snap:0000000002"),
                ],
            }
            self._payloads = {
                (SCHEMA_SNAPSHOT, b"ses_r:snap:0000000001"): json.dumps({"rev": 1}).encode(),
                (SCHEMA_SNAPSHOT, b"ses_r:snap:0000000002"): json.dumps({"rev": 2}).encode(),
            }

        def domain_bindings_at(self, generation: int):
            self.domain_bindings_at_calls += 1
            return tuple(self.by_gen[generation])

        def get_bound_object(self, schema: bytes, identity: bytes):
            item = super().get_bound_object(schema, identity)
            if self.current_generation == 10:
                self.current_generation = 11
            return item

    stub = AdvancingStore()
    backend = StoreBackend.__new__(StoreBackend)
    backend._store = stub
    assert backend.read_snapshot("ses_r") == {"rev": 2}
    assert stub.get_bound_calls == [
        (SCHEMA_SNAPSHOT, b"ses_r:snap:0000000001"),
        (SCHEMA_SNAPSHOT, b"ses_r:snap:0000000002"),
    ]


def test_read_snapshot_bounded_under_continuous_generation_advances():
    class ContinuouslyAdvancingStore(_ExactLookupStub):
        def domain_bindings_at(self, generation: int):
            self.domain_bindings_at_calls += 1
            return (
                (SCHEMA_SNAPSHOT, f"ses_c:snap:{generation:010d}".encode()),
            )

        def get_bound_object(self, schema: bytes, identity: bytes):
            self.get_bound_calls.append((schema, identity))
            rev = int(identity.split(b":snap:")[1])
            self.current_generation += 1
            return SimpleNamespace(
                domain_schema=schema,
                domain_identity=identity,
                payload=json.dumps({"rev": rev}).encode(),
            )

    stub = ContinuouslyAdvancingStore(5, [], {})
    backend = StoreBackend.__new__(StoreBackend)
    backend._store = stub
    result = backend.read_snapshot("ses_c")
    assert result == {"rev": 6}
    assert len(stub.get_bound_calls) == 2
    assert stub.domain_bindings_at_calls == 2


def test_real_store_decomposed_fallback_and_integrity_preservation(tmp_path: Path):
    store = open_store(tmp_path / "state", "store", verify_on_open=False, defer_mutation=False)
    try:
        sid = "ses_sections"
        large_workspace = {
            "root": "/tmp/ws",
            "repositories": [
                {"name": f"repo_{i}", "head": f"{i:040x}", "branch": "main"}
                for i in range(25)
            ],
        }
        store.save_snapshot(sid, {
            "schema_version": "mncs.environment.session/5",
            "session_id": sid,
            "lifecycle": "active",
            "snapshot_sequence": 1,
            "workspace": large_workspace,
        })
        rev1 = store.load_snapshot(sid)
        assert rev1 is not None and rev1["snapshot_sequence"] == 1
        assert len(rev1["workspace"]["repositories"]) == 25

        # Write undecodable rev 2 directly via raw Store: must fall back to rev 1
        store.backend._put_immutable(
            schema=SCHEMA_SNAPSHOT,
            identity=b"ses_sections:snap:0000000002",
            payload=b"corrupt-json{",
        )
        fallback = store.load_snapshot(sid)
        assert fallback is not None and fallback["snapshot_sequence"] == 1

        # Write decomposed rev 3 pointing at a valid section identity with a wrong sha256:
        # must raise StoreIntegrityFailure and NEVER silently fall back to rev 1
        sec_bytes = json.dumps(large_workspace, ensure_ascii=False, sort_keys=True).encode("utf-8")
        actual_sha = hashlib.sha256(sec_bytes).hexdigest()
        bad_env = {
            "schema_version": "mncs.environment.session/5",
            "session_id": sid,
            "snapshot_sequence": 3,
            _SNAPSHOT_SECTIONS_FIELD: {
                "workspace": {
                    "sha256": "0" * 64 if actual_sha != "0" * 64 else "1" * 64,
                    "size": len(sec_bytes),
                }
            },
        }
        # Re-bind the tampered section under wrong sha identity so section lookup returns mismatched bytes
        store.backend._put_immutable(
            schema=SCHEMA_SNAPSHOT_SECTION,
            identity=f"snap-sec:{'0' * 64 if actual_sha != '0' * 64 else '1' * 64}".encode(),
            payload=b'{"tampered":true}',
        )
        store.backend._put_immutable(
            schema=SCHEMA_SNAPSHOT,
            identity=b"ses_sections:snap:0000000003",
            payload=json.dumps(bad_env).encode("utf-8"),
        )
        with pytest.raises(StoreIntegrityFailure):
            store.load_snapshot(sid)
    finally:
        store.close()


def test_retained_reader_observes_external_writer_and_keeps_cache_bounded(tmp_path: Path):
    state_dir = tmp_path / "shared"
    writer = open_store(state_dir, "store", verify_on_open=False, defer_mutation=False)
    try:
        writer.save_snapshot("ses_1", {
            "schema_version": "mncs.environment.session/5",
            "session_id": "ses_1",
            "lifecycle": "active",
            "snapshot_sequence": 1,
        })
        claim = claims.acquire(
            writer,
            repository="repo-ext",
            session_id="ses_1",
            consumer_id="agent-1",
            basis=claims.BASIS_EXPLICIT,
            reason="initial",
        )
        writer.put_event("ses_1", 1, {"session_id": "ses_1", "sequence": 1, "identity": "evt_1"})
        writer.save_checkpoint("ses_1", {"session_id": "ses_1", "identity": "chk_1", "sequence": 1})
        writer.write_projection_row("proj_1", 1, {"version": 1})

        reader = open_store(state_dir, "store", verify_on_open=False, defer_mutation=True)
        try:
            gen_initial = reader.backend.generation()
            assert reader.list_sessions() == ["ses_1"]
            assert reader.load_snapshot("ses_1")["snapshot_sequence"] == 1
            assert reader.read_latest_claims_for([claim["claim_id"]])[0]["version"] == 1
            assert reader.read_projection_versions() == {"proj_1": 1}

            # Historical read must not distort current lookups or unboundedly grow cache
            hist_bindings = reader.domain_bindings_at(gen_initial)
            assert any(ident == b"ses_1:snap:0000000001" for _, ident in hist_bindings)

            # Separate writer commits new session, revision, claim release, event, checkpoint, projection
            writer.save_snapshot("ses_1", {
                "schema_version": "mncs.environment.session/5",
                "session_id": "ses_1",
                "lifecycle": "active",
                "snapshot_sequence": 2,
            })
            writer.save_snapshot("ses_2", {
                "schema_version": "mncs.environment.session/5",
                "session_id": "ses_2",
                "lifecycle": "active",
                "snapshot_sequence": 1,
            })
            claims.release(
                writer,
                session_id="ses_1",
                claim_id=claim["claim_id"],
                request_id="ext-release",
            )
            writer.put_event("ses_1", 2, {"session_id": "ses_1", "sequence": 2, "identity": "evt_2"})
            writer.save_checkpoint("ses_1", {"session_id": "ses_1", "identity": "chk_2", "sequence": 2})
            writer.write_projection_row("proj_1", 2, {"version": 2})

            # Advance a few extra generations so we also exercise bounded directory LRU eviction
            for seq in range(3, 8):
                writer.put_event("ses_1", seq, {"session_id": "ses_1", "sequence": seq, "identity": f"evt_{seq}"})
                reader.load_snapshot("ses_1")
                assert len(reader.backend._binding_directories) <= _BINDING_DIRECTORY_MAX_GENERATIONS

            assert reader.list_sessions() == ["ses_1", "ses_2"]
            assert reader.load_snapshot("ses_1")["snapshot_sequence"] == 2
            assert reader.load_snapshot("ses_2")["snapshot_sequence"] == 1
            assert reader.read_latest_claims_for([claim["claim_id"]])[0]["status"] == "released"
            assert [c["identity"] for c in reader.list_checkpoints("ses_1")] == ["chk_1", "chk_2"]
            assert reader.read_projection_versions() == {"proj_1": 2}
            assert reader.read_projection_row("proj_1") == (2, {"version": 2})
            page = reader.read_event_page("ses_1", limit=2, before_sequence=5)
            assert [e["sequence"] for e in page["events"]] == [3, 4]
        finally:
            reader.close()
            assert len(reader.backend._binding_directories) == 0
    finally:
        writer.close()


@pytest.mark.parametrize(("n_sessions", "n_unrelated"), [(8, 24), (24, 96)])
def test_session_lookup_does_not_scale_with_sessions_times_bindings(
    tmp_path: Path, monkeypatch, n_sessions: int, n_unrelated: int,
):
    store = open_store(tmp_path / f"scale-{n_sessions}", "store", verify_on_open=False, defer_mutation=False)
    try:
        for i in range(n_sessions):
            sid = f"ses_scale_{i:03d}"
            for rev in range(1, 3):
                store.save_snapshot(sid, {
                    "schema_version": "mncs.environment.session/5",
                    "session_id": sid,
                    "lifecycle": "active",
                    "snapshot_sequence": rev,
                })
        for j in range(n_unrelated):
            store.write_projection_row(f"unrelated_{j}", 1, {"idx": j})

        raw = store.backend._store
        counts = {"dba": 0, "fbo": 0, "gbo": 0}
        orig_dba = raw.domain_bindings_at
        orig_fbo = raw.find_bound_objects
        orig_gbo = raw.get_bound_object

        def spy_dba(gen):
            counts["dba"] += 1
            return orig_dba(gen)

        def spy_fbo(sch, pref=b""):
            counts["fbo"] += 1
            return orig_fbo(sch, pref)

        def spy_gbo(sch, ident):
            counts["gbo"] += 1
            return orig_gbo(sch, ident)

        monkeypatch.setattr(raw, "domain_bindings_at", spy_dba)
        monkeypatch.setattr(raw, "find_bound_objects", spy_fbo)
        monkeypatch.setattr(raw, "get_bound_object", spy_gbo)

        sessions_found = store.list_sessions()
        assert len(sessions_found) == n_sessions
        for sid in sessions_found:
            loaded = store.load_snapshot(sid)
            assert loaded is not None and loaded["snapshot_sequence"] == 2
            assert store.list_checkpoints(sid) == []
            assert store.read_event_page(sid, limit=5)["event_count"] == 0
        versions = store.read_projection_versions()
        assert len(versions) == n_unrelated

        # O(1) binding directory build per generation: never scales with n_sessions
        assert counts["dba"] == 1
        assert counts["fbo"] == 0
        assert counts["gbo"] == n_sessions

        # And higher-level selection.select() only calls domain_bindings_at once for heads
        selected = selection.select(
            store, "active-scale-test", lambda snap: snap.get("lifecycle") == "active"
        )
        assert len(selected) == n_sessions
        assert counts["dba"] == 2  # 1 already cached directory + 1 selection._heads
        assert counts["fbo"] == 0
        assert counts["gbo"] == 2 * n_sessions
    finally:
        store.close()
