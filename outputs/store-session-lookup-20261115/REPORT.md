# Store Session Lookup and Cold Continuation Recovery

- **Repository**: `mncs-environment` (branched from `main` at `beda734`, sibling context: `mncs-store` `5464f04`, `mncs-control-mcp` `55941a6`, `mncs-language` `f8742e9`)
- **Production Store snapshot during measurement**: `generation = 131819`, `132,078` bindings (`51,291` snapshots across `616` sessions, `60,530` events, `3,279` claims, `176` sections, `1,370` projections; strictly read-only across all checks, `start_gen == end_gen == 131819`)
- **Deliverable files**:
  - Implementation: `mncs_env/store_backend.py`
  - Regression & scaling tests: `tests/test_store_session_lookup.py`
  - Machine-readable benchmark & attribution artifact: `outputs/store-session-lookup-20261115/measurements.json`

---

## 1. Root Cause Attribution (Baseline at `beda734`, Generation `131819`)

Before edits, cold and warm Control `campaign_continuation` calls exhibited quadratic binding-enumeration amplification inside `StoreBackend` (`mncs_env/store_backend.py`), NOT in `mncs-store` generation-parts cache overflow (`gen_parts_cache_entries_after_read = 3`, well below `16 MiB`) and NOT in `mncs-control-mcp`:

1. **`read_snapshot(session_id)`** called `self._store.domain_bindings_at(generation)` plus `self._store.find_bound_objects(SCHEMA_SNAPSHOT, prefix)` on **every** snapshot lookup (`763` cold / `318` warm snapshot loads during `not-found-scan`).
2. `domain_bindings_at(131819)` yielded all `132,078` binding tuples on every call (`764` cold calls -> `100,907,592` tuples yielded; `319` warm calls -> `42,132,882` tuples yielded), even though `EmbeddedStore._domain_binding_index` only rebuilt once (`8.5549 s`).
3. `find_bound_objects` scanned the full `132,078`-entry index with `bytes.startswith` on every call (`911` cold calls -> `87,171,390` `bytes.startswith` calls in `cProfile`; `466` warm calls).
4. **Same per-call enumeration pattern across peer `StoreBackend` methods**: `list_checkpoints`, `read_event_page`, `read_events`, `read_latest_claims_for`, `list_sessions`, `read_projection_row`, and `read_projection_versions` each re-scanned `domain_bindings_at` or `find_bound_objects` without sharing metadata for the active Store generation.
5. **Measured Store lookup asymmetry (`EmbeddedStore`)**:
   - `find_bound_objects(SCHEMA_CLAIM)` read all `3,279` claims in `0.4475 s` (`0` native `call_batch` invocations; pure index scan + file read), whereas a single `get_bound_object` executed `1` native batch (`store.content.v1:digest_window`) taking `0.0049 s` (`~5 ms`).
   - Therefore, replacing bulk multi-object schema reads (`read_claims`, large `read_events`) with per-object `get_bound_object` loops would regress latency via `call_batch` overhead, whereas single-object / bounded lookups (`read_snapshot`, `read_projection_row`, `read_latest_claims_for`, `list_checkpoints`, `read_event_page`, and small `0..4`-event `read_events`) benefit directly from exact `get_bound_object` once binding identities are indexed per generation.

---

## 2. Changes Implemented (`mncs_env/store_backend.py`)

Added an ephemeral, generation-keyed derived metadata index (`_BindingDirectory`, bounded LRU of `_BINDING_DIRECTORY_MAX_GENERATIONS = 2` per `EmbeddedStore` instance) in `mncs_env/store_backend.py`:

- **`_BindingDirectory` & single-pass builders** (`_decode_utf8`, `_index_snapshot_binding`, `_index_event_binding`, `_index_claim_binding`, `_index_projection_binding`, `_build_binding_directory`):
  - Parses one generation's `domain_bindings_at(generation)` tuple in a single pass into pre-grouped maps (`session_ids`, `snapshot_revisions`, `checkpoints`, `events`, `claims_latest`, `projections`, `projection_versions`), keeping strict isolation between similar session prefixes (`ses_1:` vs `ses_10:`), ignoring malformed suffixes / non-UTF-8 identities, and filtering unrelated schemas.
- **Lifecycle & invalidation safety** (`_binding_directory_for`, `_clear_binding_directory`):
  - Keyed by `(store instance identity `_binding_cache_store is store`, integer `generation`)` under `_binding_cache_lock`, excluding `None`/`bool` test stubs and lazily initializing attributes when test fixtures instantiate `StoreBackend.__new__(StoreBackend)`.
  - Automatically observes external writers whenever `current_generation` advances, caps cached directories at `2` generations (`OrderedDict` LRU eviction), and clears cleanly in `StoreBackend.close()`.
- **Exact payload verification preserved**:
  - `read_snapshot`: walks candidate revisions descending via exact `get_bound_object` (with `_reconstruct_sectioned_snapshot` verifying section SHA-256 digests and raising `StoreIntegrityFailure` on tampered/mismatched section or JSON-valid section corruption, while falling back to prior revisions on missing/undecodable snapshot JSON); retries once (`attempt in range(2)`) if `current_generation` advances mid-lookup.
  - `list_sessions`, `list_checkpoints`, `read_event_page`, `read_projection_versions`, `read_projection_row`, `read_latest_claims_for`: index from `_BindingDirectory` and read only targeted objects via `_read_exact_or_find` / `get_record_strict` (with `get_record_strict` also falling back to `_load_store_api()` for `__new__` stubs).
  - `read_events`: hybrid dispatch using `_EVENT_DIRECT_GET_MAX = 4` — reads `0..4` events via exact `_read_exact_or_find` (`0` `find_bound_objects` calls) and delegates larger event histories (`>4` events) to `find_bound_objects(SCHEMA_EVENT, prefix)` to avoid `5 ms × N` native `call_batch` round trips.
  - `read_claims` remains on bulk `find_bound_objects(SCHEMA_CLAIM)` (`0` native batches).
- **Zero payload caching**: Only immutable binding metadata (`(revision, identity)`, `(version, identity)`) is cached per exact Store generation; all object payloads still come live from `EmbeddedStore` verification paths.

---

## 3. Production Benchmark & Attribution Results (Generation `131819`)

### 3.1 Fresh-Process Unprofiled Continuation (3 Trials Each, Medians; `ambiguous-or-denied`: 1 Trial)

| Scenario | Metric | `main` (`beda734`) | Worktree (`fix/store-session-lookup-20261115`) | Speedup / Change | Semantic Parity |
|---|---|---:|---:|---:|---|
| `not-found-scan` (`mncs-control-mcp`, full scan, 3 trials) | Cold wall median | `48.7032 s` | `17.6424 s` | `2.76×` (`-63.8%`) | `equal: true` (`status="not-found"`) |
| `not-found-scan` | Cold CPU median | `48.4364 s` | `17.4940 s` | `2.77×` (`-63.9%`) | |
| `not-found-scan` | Warm wall median | `20.8005 s` | `7.7025 s` | `2.70×` (`-63.0%`) | |
| `not-found-scan` | Warm CPU median | `20.6122 s` | `7.6353 s` | `2.70×` (`-63.0%`) | |
| `not-found-scan` | Peak RSS median | `640,784 KiB` | `640,068 KiB` | `-716 KiB` (flat) | |
| `ready` (`mncs-environment` active campaign `campaign-store-session-lookup-20261115`, 3 trials) | Cold wall median | `38.7000 s` | `13.4712 s` | `2.87×` (`-65.2%`) | `equal: true` (`status="ready"`, `18,409` B) |
| `ready` | Cold CPU median | `38.0364 s` | `13.1081 s` | `2.90×` (`-65.5%`) | |
| `ready` | Warm wall median | `1.2074 s` | `1.0402 s` | `1.16×` (`-13.8%`) | |
| `ready` | Warm CPU median | `1.1790 s` | `1.0250 s` | `1.15×` (`-13.1%`) | |
| `ready` | Peak RSS median | `643,884 KiB` | `654,764 KiB` | `+1.7%` | |
| `ambiguous-or-denied` (unconstrained `mncs-environment`, 1 trial) | Cold wall | `38.0640 s` | `12.9574 s` | `2.94×` (`-66.0%`) | `equal: true` (`ControlError` parity) |
| `ambiguous-or-denied` | Warm wall | `6.0561 s` | `1.9366 s` | `3.13×` (`-68.0%`) | |

### 3.2 Direct Read-Only `StoreSessionStore` Operations (Generation `131819`, `616` Sessions, `1,370` Projections)

| Operation | `main` (`beda734`) | Worktree | Speedup |
|---|---:|---:|---:|
| `list_sessions()` (cold 1st call, builds Store `_domain_binding_index`) | `3.6401 s` | `3.7839 s` | `1.0×` |
| `load_snapshot` × 20 sessions | `1.1281 s` | `0.3347 s` | `3.37×` (`5.88×` when dir warm: `0.9938 s -> 0.1691 s`) |
| `list_checkpoints` × 20 sessions | `0.6751 s` | `0.0913 s` | `7.39×` |
| `read_event_page(limit=10)` × 20 sessions | `1.3224 s` | `0.7327 s` | `1.80×` |
| `read_projection_versions()` (`1,370` rows) | `2.5780 s` | `0.0000 s` (`<0.1 ms`) | `>2,500×` |
| `read_projection_row()` × 5 rows | `0.0721 s` | `0.0310 s` | `2.33×` |
| `read_latest_claims_for()` (4 claim IDs) | `0.0517 s` | `0.0191 s` | `2.71×` |
| Post-list `domain_bindings_at` calls across all 7 ops above | `61` | `0` | eliminated |
| Post-list `find_bound_objects` calls across all 7 ops above | `27` | `1` (`read_claims` only) | `-96.3%` |
| Direct worker peak RSS | `617,112 KiB` | `393,932 KiB` | `-36.2%` |

### 3.3 Profiled Attribution Counters (`not-found-scan`, Generation `131819`)

| Counter | Cold `main` -> Worktree | Warm `main` -> Worktree |
|---|---:|---:|
| `domain_bindings_at` calls | `764 -> 2` (`-99.7%`) | `319 -> 1` (`-99.7%`) |
| Binding tuples yielded | `100,907,592 -> 264,156` (`-99.74%`) | `42,132,882 -> 132,078` (`-99.69%`) |
| `find_bound_objects` calls | `911 -> 93` (`-89.8%`) | `466 -> 93` (`-80.0%`) |
| `bytes.startswith` calls (cold `cProfile`) | `87,171,390 -> 5,575,321` (`-93.6%`) | — |
| Total Python calls (cold `cProfile`) | `211,597,095 -> 30,874,544` (`-85.4%`) | — |
| `load_snapshot` cumtime (profiled) | `73.1973 s -> 8.3847 s` (`8.73×`) | `15.9903 s -> 3.2774 s` (`4.88×`) |
| Native `call_batch` calls | `166 -> 1,062` (`4.7869 s` profiled) | `42 -> 493` (`2.2119 s` profiled) |

---

## 4. Verification Summary

- **`tests/test_store_session_lookup.py`**: `8 passed in 13.78 s` (prefix isolation, `__new__` stub `_store` invalidation, mid-lookup generation retry + bounded retry exhaustion, real-`EmbeddedStore` decomposed section reconstruction & corrupt/tampered `StoreIntegrityFailure`, external writer visibility + cache bound `<= 2` & `close()` cleanup, non-quadratic `domain_bindings_at`/`find_bound_objects`/`get_bound_object` counts at `[(8, 24), (24, 96)]` + `selection.select`).
- **Full `mncs-environment` suite** (`PYTHONPATH=.../mncs-store/python:.../mncs-rights-provenance/src:.`, `MNCS_TEST_FAMILY=/home/epi13/Documents/Projects`): `1327 passed, 83 skipped, 47 subtests passed in 936.12s (0:15:36)`, exit `0` (zero failures; the 83 skips are environment/toolchain guards in a linked worktree lacking native `MNCS_BIN`/`Automation`/`pristine` paths or resolving family via `ROOT.parent`).
- **`scripts/vertical_proof.py`**: exit `0`, `VERTICAL PROOF: PASS`.
- **`mncs-control-mcp` integration suites** (`test_environment_sessions.py`, `test_environment_rpc.py`, `test_local_environment_mcp.py`, `test_environment_response_views.py`, `test_environment_mcp_client.py`, `test_environment_read_efficiency.py`) redirected via `EnvironmentService.environment_path` to the worktree checkout with `mncs-rights-provenance/src` on `PYTHONPATH`: all `223` tests passed (exit `0`).

---

## 5. Out-of-Scope Observations / Follow-Ups

1. **Per-object `call_batch` (`digest_window`) cost (~`4.5–5 ms` each)**: With quadratic `domain_bindings_at` scans eliminated, remaining `not-found-scan` time is dominated by `1,062` native `get_bound_object` batches (`4.7869 s` profiled), `93` `find_bound_objects` calls (`7.1809 s` profiled for `>4`-event sessions + claims), and one cold `_domain_binding_index` build (`8.6198 s` profiled / `~3.8 s` unprofiled). A batched `get_bound_objects` native Store primitive or faster content digest check in `mncs-store` would reduce per-object native round trips across `read_snapshot`, sections, and small event histories.
2. **Control-plane `not-found-scan` / authority-grant scans (`mncs-control-mcp` & `sessions.py`)**: Cold/warm `not-found-scan` still calls `read_events` `92` times (`>4` events per session) and re-reads snapshots `318` times on warm continuation, whereas targeted `"ready"` warm continuation finishes in `1.0402 s`. Inspecting why fallback project scanning re-evaluates sessions/grants would cut the remaining `7.70 s` warm fallback and reduce `93` `find_bound_objects` calls.
