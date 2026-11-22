"""Ambient verification coherence: keep test evidence current while agents work.

This module composes provider-owned testing semantics; it owns no test
discovery, no assertion evaluation, and no result classification. On
every environment entry it observes declared verification obligations,
measures the current world (repository state, declaration digests,
toolchain identity, recorded evidence), asks the native coherence
policy which obligations are current and which need execution, runs
queued native suites through the bound test capability, records
identity-bound evidence, and stays quiet when the world is current.

Policy lives in `mncs.test.verification_coherence` (current / queue /
defer / escalate). This file transports facts to that policy and
carries out admitted effects: filesystem reads, provider invocation,
and evidence recording. Verification is read-only: it never updates
snapshots, rewrites goldens, mutates sources, stages, or commits.
Execution artifacts land under the session state directory, never in
a provider checkout. Nothing here seizes claims or touches sessions
it does not own.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import capabilities as capabilities_module
from . import evidence_store
from . import workspace as workspace_module
from .identity import digest_hex

SCHEMA = "mncs.environment.verification/1"
COHERENCE_REQUEST_SCHEMA = "mncs.test-verification-coherence-request/1"
COHERENCE_REQUEST_SCHEMA_V2 = "mncs.test-verification-coherence-request/2"
COHERENCE_RESULT_SCHEMA = "mncs.test-verification-coherence/1"
EVIDENCE_SCHEMA = "mncs.session-evidence/1"
MAX_HISTORY = 10
MAX_OBLIGATIONS = 32
MAX_DISCOVERED_OBLIGATIONS = 256
MAX_INVENTORY_IDS = 64
MAX_IDENTITY_BYTES = 1024
MAX_PATTERNS = 8
MAX_DEP_FILES = 512
MAX_DEP_BYTES = 8 * 1024 * 1024
MAX_CLOSURE_FILES = 256
MAX_CLOSURE_BYTES = 16 * 1024 * 1024
MAX_DIRTY_FILES = 256
MAX_DIRTY_BYTES = 1024 * 1024
MAX_FAILED_IDS = 8
COHERENCE_CAPABILITY = "mncs.test-verification-coherence/1"
COHERENCE_CAPABILITY_V2 = "mncs.test-verification-coherence/2"
TEST_CAPABILITY = "mncs.test-result/1"
CLOSURE_FILESET = "env-resolved-imports-v1"
DEFAULT_MAX_EXECUTIONS = 8
EXECUTION_TIMEOUT_SECONDS = 300

_FILE_SHA_CACHE: dict[tuple[str, int, int], str] = {}
_MODULE_NAME_RE = re.compile(r"^[a-z0-9_]+(?:\.[a-z0-9_]+)*$")
_USE_STMT_RE = re.compile(
    r"^(?:(?:pub|export)\s+)?use\s+([a-z0-9_.]+)(?:\s+as\s+[A-Za-z_][A-Za-z0-9_]*)?\s*$"
)

LIFECYCLES = ("permanent", "transitional", "scheduled", "reference_only", "retired")
EXECUTOR_KINDS = ("native_first_class_test", "compile_experiment", "diagnostic_test",
                  "backend_check", "differential_oracle", "migration_parity",
                  "external_integration")
VERDICTS = ("PASS", "FAIL", "UNKNOWN")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def bytes_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _artifact_directory(session, *parts: str) -> Path:
    state_root = session.store.state_dir.resolve()
    path = (state_root / "sessions" / session.session_id
            / "verification-artifacts" / Path(*parts)).resolve()
    if not path.is_relative_to(state_root):
        raise ValueError("verification artifact path escapes the state root")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def _workspace_root(session) -> Path | None:
    root = session.snapshot.get("workspace", {}).get("root")
    if not root:
        return None
    path = Path(root)
    return path if path.is_dir() else None


def _inventory_name(repo: Path) -> str | None:
    """Resolve the obligation inventory path from the repo manifest."""
    try:
        manifest = json.loads((repo / ".mncs" / "project.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(manifest, dict):
        return None
    verification = manifest.get("verification")
    name = verification.get("obligation_inventory") if isinstance(verification, dict) else None
    if not isinstance(name, str) or not name:
        return None
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        return None
    return name


def _validate_obligation(entry: Any) -> str | None:
    if not isinstance(entry, dict):
        return "obligation-not-an-object"
    identity = entry.get("identity")
    if not isinstance(identity, str) or not identity:
        return "identity-missing"
    if len(identity.encode("utf-8")) > MAX_IDENTITY_BYTES:
        return "identity-too-long"
    if entry.get("lifecycle") not in LIFECYCLES:
        return "lifecycle-unknown"
    executor = entry.get("executor")
    if not isinstance(executor, dict):
        return "executor-missing"
    if executor.get("kind") not in EXECUTOR_KINDS:
        return "executor-kind-unknown"
    patterns = executor.get("declaration_identities", [])
    if patterns is None:
        patterns = []
    if (not isinstance(patterns, list) or len(patterns) > MAX_PATTERNS
            or any(not isinstance(pattern, str) or not pattern
                   or len(pattern.encode("utf-8")) > MAX_IDENTITY_BYTES
                   for pattern in patterns)):
        return "patterns-invalid"
    deps = entry.get("invalidation_dependencies", [])
    if (not isinstance(deps, list)
            or any(not isinstance(dep, str) or not dep for dep in deps)):
        return "dependencies-invalid"
    sources = executor.get("source_paths", [])
    if sources is None:
        sources = []
    if (not isinstance(sources, list)
            or any(not isinstance(source, str) or not source for source in sources)):
        return "source-paths-invalid"
    return None


def discover_obligations(workspace_root: Path, checkouts: dict[str, Path] | None = None) -> tuple[list[dict], list[dict]]:
    """Collect verification obligations from workspace manifests.

    Only direct-child repositories carrying `.mncs/project.json` with a
    `verification.obligation_inventory` pointer are considered; anything
    else costs small JSON reads. Invalid obligations are reported,
    never executed.
    """
    obligations: list[dict] = []
    invalid: list[dict] = []
    try:
        children = (sorted(checkouts.items()) if checkouts is not None else
                    [(path.name, path) for path in sorted(workspace_root.iterdir())
                     if path.is_dir() and not path.name.startswith(".")])
    except OSError:
        return [], [{"repository": "", "reason": "workspace-unreadable"}]
    for repository, child in children:
        name = _inventory_name(child)
        if name is None:
            continue
        try:
            payload = json.loads((child / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            invalid.append({"repository": repository, "reason": "inventory-unreadable"})
            continue
        entries = payload.get("obligations") if isinstance(payload, dict) else None
        if not isinstance(entries, list):
            invalid.append({"repository": repository, "reason": "obligations-not-a-list"})
            continue
        for entry in entries:
            problem = _validate_obligation(entry)
            if problem is not None:
                identity = entry.get("identity") if isinstance(entry, dict) else None
                invalid.append({"repository": repository, "reason": problem,
                                "identity": identity})
                continue
            obligations.append({"repository": repository, "checkout": str(child),
                                "declaration": entry})
    obligations.sort(key=lambda item: str(item["declaration"].get("identity")))
    if len(obligations) > MAX_DISCOVERED_OBLIGATIONS:
        for item in obligations[MAX_DISCOVERED_OBLIGATIONS:]:
            invalid.append({"repository": item["repository"], "reason": "obligation-bound-exceeded",
                            "identity": item["declaration"].get("identity")})
        obligations = obligations[:MAX_DISCOVERED_OBLIGATIONS]
    return obligations, invalid


def _routing_priority(obligation: dict, measured: dict[str, dict | None],
                      actions_rows: dict[str, Any]) -> tuple[int, str]:
    from . import actions as actions_module

    declaration = obligation["declaration"]
    identity = str(declaration.get("identity", ""))
    lifecycle = str(declaration.get("lifecycle", ""))
    kind = str(declaration.get("executor", {}).get("kind", ""))
    active = lifecycle in ("permanent", "transitional")
    if kind == "native_first_class_test" and active and measured.get(identity) is not None:
        tier = 0
    elif kind == "native_first_class_test":
        tier = 1
    elif actions_module.external_admission(actions_rows, identity) is not None:
        tier = 2
    elif active:
        tier = 3
    else:
        tier = 4
    return (tier, identity)


def route_verification_obligations(
    obligations: list[dict],
    measured: dict[str, dict | None],
    actions_rows: dict[str, Any],
    limit: int = MAX_OBLIGATIONS,
) -> tuple[list[dict], list[dict]]:
    """Select up to `limit` obligations for native coherence policy evaluation.

    Runnable native first-class suites and obligations with admitted external
    evidence route ahead of non-native obligations without admitted evidence,
    preventing large external inventories from starving native suites whose
    identities sort later. Selected and overflow entries preserve canonical
    identity order.
    """
    if len(obligations) <= limit:
        return list(obligations), []
    ranked = sorted(
        obligations,
        key=lambda item: _routing_priority(item, measured, actions_rows),
    )
    selected = sorted(
        ranked[:limit],
        key=lambda item: str(item["declaration"].get("identity", "")),
    )
    overflow = sorted(
        ranked[limit:],
        key=lambda item: str(item["declaration"].get("identity", "")),
    )
    return selected, overflow


def _digest_files(checkout: Path, relpaths: list[str]) -> tuple[str | None, str | None]:
    """Digest dependency contents; unmeasurable entries fail closed."""
    digester = hashlib.sha256()
    count = 0
    total = 0
    for relpath in sorted(relpaths):
        relative = Path(relpath)
        if relative.is_absolute() or ".." in relative.parts:
            return None, f"dependency-escapes:{relpath}"
        target = checkout / relative
        if target.is_dir():
            try:
                members = sorted(path for path in target.rglob("*") if path.is_file())
            except OSError:
                return None, f"dependency-unreadable:{relpath}"
            nested = [str(path.relative_to(checkout)) for path in members]
            digest, problem = _digest_files(checkout, nested)
            if digest is None:
                return None, problem
            digester.update(f"{relpath}={digest}".encode())
            continue
        if not target.is_file():
            return None, f"dependency-missing:{relpath}"
        try:
            data = target.read_bytes()
        except OSError:
            return None, f"dependency-unreadable:{relpath}"
        count += 1
        total += len(data)
        if count > MAX_DEP_FILES or total > MAX_DEP_BYTES:
            return None, "dependency-bound-exceeded"
        digester.update(relpath.encode())
        digester.update(b"\0")
        digester.update(data)
        digester.update(b"\0")
    return "sha256:" + digester.hexdigest(), None


def _porcelain_name(line: str) -> str | None:
    if len(line) <= 3 or line.startswith("!!"):
        return None
    name = line[3:].strip()
    if " -> " in name:
        name = name.split(" -> ", 1)[1]
    return name.strip().strip('"') or None


def dirty_content_digest(checkout: Path) -> str:
    """Digest current dirty/untracked content (bounded, empty when clean)."""
    try:
        state = workspace_module.inspect_repo(checkout, _refresh=True)
    except (OSError, ValueError):
        return "repo-unreadable"
    if state is None:
        return "repo-unreadable"
    if not getattr(state, "dirty", False):
        return "clean"
    names = sorted({name for line in (getattr(state, "dirty_files", None) or [])
                    if (name := _porcelain_name(str(line))) is not None
                    and name != ".worktrees"
                    and not name.startswith(".worktrees/")})[:MAX_DIRTY_FILES]
    digester = hashlib.sha256()
    total = 0
    for name in names:
        try:
            data = (checkout / name).read_bytes() if (checkout / name).is_file() else b"<absent>"
        except OSError:
            data = b"<unreadable>"
        total += len(data)
        if total > MAX_DIRTY_BYTES:
            digester.update(b"<dirty-overflow>")
            break
        digester.update(name.encode())
        digester.update(b"\0")
        digester.update(data)
        digester.update(b"\0")
    return "sha256:" + digester.hexdigest()


def repo_revision(checkout: Path) -> str:
    facts = workspace_module.quick_repo_facts(checkout, _refresh=True)
    if facts is None:
        return "repo-unreadable"
    return str(facts.get("head") or "head-unknown")


def toolchain_identity(session) -> str:
    toolchain = session.snapshot.get("toolchain")
    if not isinstance(toolchain, dict):
        return "toolchain-unknown"
    return digest_hex({key: toolchain.get(key) for key in
                       ("binary", "revision", "checkout", "repository")})


def _file_sha256_cached(path: Path) -> str | None:
    """Content SHA-256 of a regular file, cached in-process by stat identity."""
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return None
    if not resolved.is_file():
        return None
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    cached = _FILE_SHA_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        digester = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digester.update(chunk)
    except OSError:
        return None
    digest = "sha256:" + digester.hexdigest()
    if len(_FILE_SHA_CACHE) > 2048:
        _FILE_SHA_CACHE.clear()
    _FILE_SHA_CACHE[key] = digest
    return digest


def _strip_mncs_comments(text: str) -> str | None:
    """Strip // and nested /* ... */ comments while preserving string literals."""
    out: list[str] = []
    i = 0
    n = len(text)
    depth = 0
    in_string = False
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if in_string:
            out.append(" ")
            if ch == "\\" and i + 1 < n:
                out.append(" ")
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if depth > 0:
            if ch == "/" and nxt == "*":
                depth += 1
                i += 2
            elif ch == "*" and nxt == "/":
                depth -= 1
                out.append(" ")
                i += 2
            else:
                if ch == "\n":
                    out.append("\n")
                i += 1
            continue
        if ch == "/" and nxt == "/":
            i += 2
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and nxt == "*":
            depth = 1
            i += 2
            continue
        if ch == '"':
            in_string = True
            out.append(" ")
            i += 1
            continue
        out.append(ch)
        i += 1
    if depth != 0 or in_string:
        return None
    return "".join(out)


def _declared_module_name(text: str) -> str | None:
    """Exact mirror of `mncs_syntax::declared_module_name`."""
    for line in text.splitlines():
        trimmed = line.lstrip()
        if trimmed.startswith("module "):
            rest = trimmed[len("module "):].rstrip()
            if rest.endswith(";"):
                rest = rest[:-1]
            name = rest.strip()
            if name:
                return name
    return None


def _without_version_tail(name: str) -> str | None:
    if "." not in name:
        return None
    head, tail = name.rsplit(".", 1)
    if not head or ".." in head or not tail.startswith("v"):
        return None
    digits = tail[1:]
    if not digits or not digits.isdigit():
        return None
    return head


def _module_names_compatible(requested: str, declared: str) -> bool:
    """Exact mirror of `mncs_syntax::module_names_compatible`."""
    if requested == declared:
        return True
    return (_without_version_tail(declared) == requested
            or _without_version_tail(requested) == declared)


def _module_candidates(root: Path, module: str) -> list[Path]:
    """Exact mirror of `FileModuleResolver::candidates`."""
    dotted = module.replace(".", "/")
    tail = module.rsplit(".", 1)[-1]
    paths = [root / f"{dotted}.mncs"]
    head = _without_version_tail(module)
    if head is not None:
        paths.append(root / f"{head.replace('.', '/')}.mncs")
    if module.startswith("mncs."):
        rest = module[len("mncs."):]
        paths.append(root / f"{rest.replace('.', '/')}.mncs")
        if head is not None and head.startswith("mncs."):
            stripped = head[len("mncs."):]
            paths.append(root / f"{stripped.replace('.', '/')}.mncs")
    paths.append(root / f"{tail}.mncs")
    return paths


def _extract_mncs_imports(text: str) -> list[str] | None:
    """Extract top-level `use` module names; return None on malformed syntax."""
    stripped = _strip_mncs_comments(text)
    if stripped is None:
        return None
    imports: list[str] = []
    brace_depth = 0
    buf: list[str] = []
    for ch in stripped:
        if ch == "{":
            brace_depth += 1
            buf.clear()
            continue
        if ch == "}":
            brace_depth = max(0, brace_depth - 1)
            buf.clear()
            continue
        if brace_depth > 0:
            continue
        if ch == ";":
            stmt = " ".join("".join(buf).split())
            buf.clear()
            if not stmt:
                continue
            head = stmt.split(None, 2)
            is_use = (head[0] == "use") or (
                len(head) >= 2 and head[0] in ("pub", "export") and head[1] == "use"
            )
            if not is_use:
                continue
            match = _USE_STMT_RE.match(stmt)
            if match is None:
                return None
            mod = match.group(1)
            if not _MODULE_NAME_RE.match(mod) or ".." in mod:
                return None
            if mod not in imports:
                imports.append(mod)
        else:
            buf.append(ch)
    return imports


def _derive_stdlib_library_root(session) -> Path | None:
    """Mirror `bin/mncs-test` stdlib library root derivation."""
    if "MNCS_STDLIB_ROOT" in os.environ:
        raw = os.environ.get("MNCS_STDLIB_ROOT", "").strip()
        if raw:
            candidate = Path(raw) / "library"
            return candidate.resolve() if candidate.is_dir() else None
        return None
    toolchain = session.snapshot.get("toolchain")
    lang_root: Path | None = None
    if isinstance(toolchain, dict):
        if isinstance(toolchain.get("checkout"), str) and toolchain["checkout"]:
            cand = Path(toolchain["checkout"])
            if cand.is_dir():
                lang_root = cand.resolve()
        if lang_root is None and isinstance(toolchain.get("binary"), str) and toolchain["binary"]:
            bin_path = Path(toolchain["binary"])
            if bin_path.is_file():
                try:
                    lang_root = bin_path.resolve().parents[2]
                except IndexError:
                    lang_root = None
    if lang_root is None:
        binding = session_binding(session, TEST_CAPABILITY)
        tc_addr = (binding or {}).get("toolchain_address")
        if isinstance(tc_addr, str) and tc_addr and Path(tc_addr).is_file():
            try:
                lang_root = Path(tc_addr).resolve().parents[2]
            except IndexError:
                lang_root = None
    if lang_root is None:
        return None
    sibling = lang_root.parent / "mncs-stdlib" / "library"
    if sibling.is_dir():
        return sibling.resolve()
    legacy = lang_root / "library"
    if legacy.is_dir():
        return legacy.resolve()
    return None


def _resolve_library_roots(
    session,
    checkout: Path,
    suite: Path,
    executor: dict[str, Any],
) -> tuple[list[Path], str | None, bool]:
    """Build exact module search roots matching `bin/mncs-test` + `FileModuleResolver`."""
    try:
        suite_dir = suite.resolve().parent
    except OSError:
        return [], None, False
    roots: list[Path] = [suite_dir]
    if suite_dir.parent != suite_dir:
        roots.append(suite_dir.parent)

    stdlib_root = _derive_stdlib_library_root(session)
    if stdlib_root is not None:
        roots.append(stdlib_root)

    test_binding = session_binding(session, TEST_CAPABILITY) or {}
    for raw_root in (test_binding.get("provenance") or {}).get("adapter_library_paths") or []:
        if not isinstance(raw_root, str) or not raw_root:
            continue
        cand = Path(raw_root)
        if not cand.is_dir():
            return [], None, False
        roots.append(cand.resolve())

    workspace_root = _workspace_root(session)
    ws_resolved = workspace_root.resolve() if workspace_root is not None else None
    for library in executor.get("library_paths") or []:
        if not isinstance(library, str) or not library:
            continue
        rel = Path(library)
        if rel.is_absolute():
            return [], None, False
        cand = (checkout / rel)
        try:
            cand_resolved = cand.resolve()
        except OSError:
            return [], None, False
        if ws_resolved is not None:
            if not cand_resolved.is_relative_to(ws_resolved):
                return [], None, False
        elif ".." in rel.parts:
            return [], None, False
        if not cand_resolved.is_dir():
            return [], None, False
        roots.append(cand_resolved)

    inherited = os.environ.get("MNCS_LIBRARY_PATH", "")
    for entry in inherited.split(":"):
        entry = entry.strip()
        if not entry:
            continue
        cand = Path(entry)
        if cand.is_dir():
            roots.append(cand.resolve())

    bundle_digest: str | None = None
    bundle_raw = os.environ.get("MNCS_STDLIB_BUNDLE", "").strip()
    if bundle_raw:
        bundle_digest = _file_sha256_cached(Path(bundle_raw))
        if bundle_digest is None:
            return [], None, False

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped, bundle_digest, True


def _provider_and_toolchain_material(session, executor: dict[str, Any]) -> dict[str, Any] | None:
    """Bind toolchain and test/coherence provider identities for closure reuse."""
    toolchain = session.snapshot.get("toolchain")
    tc_dict = toolchain if isinstance(toolchain, dict) else {}
    test_binding = session_binding(session, TEST_CAPABILITY) or {}
    coherence_cap, _ = _active_coherence_capability(session)
    coherence_binding = (
        session_binding(session, coherence_cap) if coherence_cap is not None else None
    ) or {}

    def binding_material(binding: dict[str, Any]) -> dict[str, Any] | None:
        address = binding.get("address")
        addr_digest: str | None = None
        if isinstance(address, str) and address:
            addr_digest = _file_sha256_cached(Path(address))
            if addr_digest is None:
                return None
        tc_addr = binding.get("toolchain_address")
        tc_addr_digest: str | None = None
        if isinstance(tc_addr, str) and tc_addr:
            tc_addr_digest = _file_sha256_cached(Path(tc_addr))
            if tc_addr_digest is None:
                return None
        prov = binding.get("provenance") or {}
        return {
            "capability": binding.get("capability"),
            "provider": binding.get("provider"),
            "address": address,
            "address_sha256": addr_digest,
            "toolchain_address": tc_addr,
            "toolchain_address_sha256": tc_addr_digest,
            "adapter_library_paths": list(prov.get("adapter_library_paths") or []),
            "source": prov.get("source"),
        }

    test_mat = binding_material(test_binding)
    coh_mat = binding_material(coherence_binding)
    if test_mat is None or coh_mat is None:
        return None

    tc_bin = tc_dict.get("binary")
    tc_bin_digest: str | None = None
    if isinstance(tc_bin, str) and tc_bin:
        tc_path = Path(tc_bin)
        if tc_path.is_file():
            tc_bin_digest = _file_sha256_cached(tc_path)
            if tc_bin_digest is None:
                return None
        elif test_mat.get("address"):
            # Real provider executable is bound, so a missing toolchain binary fails closed.
            return None

    return {
        "toolchain_identity": toolchain_identity(session),
        "toolchain_binary_sha256": tc_bin_digest,
        "verifier_identity": str(executor.get("verifier_identity", "")),
        "test_provider": test_mat,
        "coherence_provider": coh_mat,
    }


def _measure_resolved_closure(session, obligation: dict) -> tuple[str, bool]:
    """Measure the exact resolved source + transitive import closure for one suite.

    Binds reuse to:
    - the suite source and every transitively imported `.mncs` module resolved
      under the exact library root order,
    - the ordered library resolution roots (and MNCS_STDLIB_BUNDLE if set),
    - selected test declaration patterns,
    - toolchain binary and provider identities.

    Fails closed to `("", False)` on any unreadable, ambiguous, conflicting,
    unresolved, malformed, or over-budget closure so coherence v2 falls back
    conservatively to repository revision + dirty fingerprint.
    """
    declaration = obligation["declaration"]
    checkout = Path(str(obligation["checkout"]))
    executor = declaration["executor"]
    sources = executor.get("source_paths") or []
    if not sources:
        return "", False
    suite_rel = sources[0]
    if Path(suite_rel).is_absolute() or ".." in Path(suite_rel).parts:
        return "", False
    suite = checkout / suite_rel
    if not suite.is_file():
        return "", False

    roots, bundle_digest, roots_ok = _resolve_library_roots(session, checkout, suite, executor)
    if not roots_ok:
        return "", False
    provider_material = _provider_and_toolchain_material(session, executor)
    if provider_material is None:
        return "", False

    visited_files: dict[str, dict[str, Any]] = {}
    queue: list[Path] = []
    try:
        suite_real = suite.resolve()
    except OSError:
        return "", False
    queue.append(suite_real)
    total_bytes = 0

    while queue:
        current_path = queue.pop(0)
        key = str(current_path)
        if key in visited_files:
            continue
        if len(visited_files) >= MAX_CLOSURE_FILES:
            return "", False
        try:
            raw = current_path.read_bytes()
        except OSError:
            return "", False
        total_bytes += len(raw)
        if total_bytes > MAX_CLOSURE_BYTES:
            return "", False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return "", False
        imports = _extract_mncs_imports(text)
        if imports is None:
            return "", False
        resolved_edges: list[dict[str, str]] = []
        for module in imports:
            authorities: list[tuple[str, bytes, Path]] = []
            for root in roots:
                for cand in _module_candidates(root, module):
                    if not cand.is_file():
                        continue
                    try:
                        cand_real = cand.resolve()
                        cand_bytes = cand_real.read_bytes()
                        cand_text = cand_bytes.decode("utf-8")
                    except (OSError, UnicodeDecodeError):
                        return "", False
                    declared = _declared_module_name(cand_text)
                    if declared is None or not _module_names_compatible(module, declared):
                        continue
                    locator = str(cand_real)
                    if any(loc == locator or data == cand_bytes for loc, data, _ in authorities):
                        continue
                    authorities.append((locator, cand_bytes, cand_real))
            if len(authorities) != 1:
                # 0 = unresolved import (or bundle-only), >1 = divergent module conflict:
                # fail closed so coherence v2 falls back to revision+dirty binding.
                return "", False
            auth_loc, _, auth_path = authorities[0]
            resolved_edges.append({"module": module, "path": auth_loc})
            if auth_loc not in visited_files:
                queue.append(auth_path)

        visited_files[key] = {
            "path": key,
            "sha256": bytes_digest(raw),
            "imports": resolved_edges,
        }

    closure_material = {
        "fileset": CLOSURE_FILESET,
        "suite": str(suite_real),
        "library_roots": [str(r) for r in roots],
        "bundle_digest": bundle_digest,
        "selected_patterns": sorted(str(p) for p in (executor.get("declaration_identities") or [])),
        "provider_toolchain": provider_material,
        "files": [visited_files[k] for k in sorted(visited_files)],
    }
    return "sha256:" + digest_hex(closure_material, length=64), True


def _active_coherence_capability(session) -> tuple[str | None, bool]:
    """Prefer coherence v2 (semantic closure) when bound, else fall back to v1."""
    if binding_available(session, COHERENCE_CAPABILITY_V2):
        return COHERENCE_CAPABILITY_V2, True
    if binding_available(session, COHERENCE_CAPABILITY):
        return COHERENCE_CAPABILITY, False
    return None, False


def _measured_current(session, obligation: dict) -> tuple[dict | None, str | None]:
    """Measure the bound identities for one obligation, or explain why not."""
    declaration = obligation["declaration"]
    checkout = Path(str(obligation["checkout"]))
    executor = declaration["executor"]
    if executor.get("kind") != "native_first_class_test":
        return None, "executor-not-native"
    sources = executor.get("source_paths") or []
    if not sources:
        return None, "suite-undeclared"
    patterns = executor.get("declaration_identities") or []
    if not patterns:
        return None, "declarations-undeclared"
    suite = checkout / sources[0]
    if not suite.is_file():
        return None, "suite-missing"
    fingerprint, problem = _digest_files(
        checkout, list(declaration.get("invalidation_dependencies") or []))
    if fingerprint is None:
        return None, problem or "subject-unmeasurable"
    head = repo_revision(checkout)
    closure_identity, closure_trusted = _measure_resolved_closure(session, obligation)
    return {
        "definition_identity": digest_hex({"declaration": declaration}),
        "subject_identity": f"{obligation['repository']}:{sources[0]}",
        "subject_fingerprint": fingerprint,
        "executor_identity": digest_hex({"executor": executor}),
        "invalidation_identity": digest_hex(
            {"dependencies": list(declaration.get("invalidation_dependencies") or [])}),
        "toolchain_identity": toolchain_identity(session),
        "inventory_identity": "",
        "repository_revision": head,
        "repository_fingerprint": dirty_content_digest(checkout),
        "closure_identity": closure_identity,
        "closure_trusted": closure_trusted,
        "closure_fileset": CLOSURE_FILESET,
    }, None


def _empty_current(*, include_closure: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "definition_identity": "", "subject_identity": "",
        "subject_fingerprint": "", "executor_identity": "",
        "invalidation_identity": "", "toolchain_identity": "",
        "inventory_identity": "", "repository_revision": "",
        "repository_fingerprint": "",
    }
    if include_closure:
        row["closure_identity"] = ""
        row["closure_trusted"] = False
        row["closure_fileset"] = CLOSURE_FILESET
    return row


def _state_rows(session) -> dict[str, dict[str, Any]]:
    rows = session.snapshot.get("verification_state")
    return dict(rows) if isinstance(rows, dict) else {}


def session_binding(session, capability: str) -> dict[str, Any] | None:
    from .sessions import AuthorityDenied

    try:
        binding = session._binding(capability)
    except (AuthorityDenied, KeyError, ValueError, AttributeError):
        return None
    return binding if isinstance(binding, dict) else None


def binding_available(session, capability: str) -> bool:
    binding = session_binding(session, capability)
    if binding is None:
        return False
    return binding.get("availability", {}).get("status") == "available"


def _empty_evidence(*, include_closure: bool = False) -> dict[str, Any]:
    row: dict[str, Any] = {
        "definition_identity": "", "subject_identity": "",
        "subject_fingerprint": "", "executor_identity": "",
        "verifier_identity": "", "invalidation_identity": "",
        "toolchain_identity": "", "inventory_identity": "",
        "repository_revision": "", "repository_fingerprint": "",
        "verdict": "UNKNOWN", "evidence_id": "",
    }
    if include_closure:
        row["closure_identity"] = ""
        row["closure_trusted"] = False
        row["closure_fileset"] = ""
    return row


def _coherence_request(session, obligations: list[dict],
                       measured: dict[str, dict | None],
                       rows: dict[str, dict[str, Any]],
                       max_executions: int,
                       *,
                       use_v2: bool = False) -> dict[str, Any]:
    provider_available = binding_available(session, TEST_CAPABILITY)
    # Imported locally: actions.py already imports this module.
    from . import actions as actions_module
    actions_rows = session.snapshot.get("actions_state")
    if not isinstance(actions_rows, dict):
        actions_rows = {}
    items = []
    for obligation in obligations:
        declaration = obligation["declaration"]
        identity = str(declaration["identity"])
        current = measured.get(identity)
        row = rows.get(identity) or {}
        recorded = row.get("evidence") if isinstance(row.get("evidence"), dict) else None
        coherence_current = _empty_current(include_closure=use_v2)
        if current is not None:
            for key in coherence_current:
                if key in current:
                    coherence_current[key] = current[key]
            coherence_current["inventory_identity"] = str(
                (recorded or {}).get("inventory_identity", ""))
        evidence = _empty_evidence(include_closure=use_v2)
        if recorded is not None:
            for key in evidence:
                if key in recorded:
                    evidence[key] = recorded[key]
            if use_v2:
                evidence["closure_trusted"] = recorded.get("closure_trusted", False) is True
                if not isinstance(evidence.get("closure_identity"), str):
                    evidence["closure_identity"] = ""
                if not isinstance(evidence.get("closure_fileset"), str):
                    evidence["closure_fileset"] = ""
        inventory_ids = list(row.get("inventory_test_identities") or [])
        admission = actions_module.external_admission(actions_rows, identity)
        items.append({
            "identity": identity,
            "lifecycle": declaration["lifecycle"],
            "executor_kind": declaration["executor"]["kind"],
            "runnable_native": current is not None,
            "external_evidence_present": admission is not None,
            "external_evidence": admission or {"obligation": "",
                                               "subject_digest": "",
                                               "verdict": "UNKNOWN",
                                               "evidence_id": ""},
            "current": coherence_current,
            "declared_patterns": list(declaration["executor"].get("declaration_identities") or []),
            "inventory_test_identities": inventory_ids[:MAX_INVENTORY_IDS],
            "inventory_truncated": bool(row.get("inventory_truncated", False)),
            "evidence_present": recorded is not None,
            "evidence": evidence,
            "evidence_conflict": False,
            "provider_available": provider_available,
        })
    schema = COHERENCE_REQUEST_SCHEMA_V2 if use_v2 else COHERENCE_REQUEST_SCHEMA
    return {"schema_version": schema,
            "max_executions": max_executions, "obligations": items}


def request_coherence(session, request: dict[str, Any],
                      capability: str = COHERENCE_CAPABILITY) -> dict[str, Any] | None:
    """Invoke the native coherence policy through its bound capability."""
    try:
        directory = _artifact_directory(session, "coherence")
    except (OSError, ValueError):
        return None
    tag = digest_hex({"at": utcnow(), "request": request})[:12]
    workdir = directory / tag
    # run-app defaults its compiled-artifact cache beside the provider
    # descriptor; pin it into session state so ambient verification never
    # writes into a provider checkout.
    cache = _artifact_directory(session, "cache")
    try:
        workdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        (workdir / "request.json").write_text(
            json.dumps(request, sort_keys=True), encoding="utf-8")
        result = session.invoke(
            capability, ["request.json", "result.json"],
            cwd=str(workdir), timeout_seconds=180,
            env={"MNCS_NATIVE_APPLICATION_CACHE_DIR": str(cache)})
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("status") != "ok":
        return None
    try:
        verdicts = json.loads((workdir / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(verdicts, dict):
        return None
    if verdicts.get("schema_version") != COHERENCE_RESULT_SCHEMA:
        return None
    return verdicts


def _verdict_of(result: dict[str, Any]) -> str:
    verdict = result.get("verdict")
    if verdict in VERDICTS:
        return str(verdict)
    summary = result.get("native_suite_summary") or {}
    if isinstance(summary, dict) and summary.get("verdict") in VERDICTS:
        return str(summary.get("verdict"))
    classification = result.get("classification")
    if classification == "passed":
        return "PASS"
    if classification == "failed":
        return "FAIL"
    return "UNKNOWN"


def _failed_test_ids(result: dict[str, Any]) -> list[dict[str, str]]:
    failed = []
    tests = result.get("tests")
    if not isinstance(tests, list):
        return failed
    for entry in tests:
        if not isinstance(entry, dict):
            continue
        verdict = entry.get("verdict")
        if verdict == "PASS" or verdict == "SKIP":
            continue
        failed.append({"id": str(entry.get("id", entry.get("entry", "?"))),
                       "verdict": str(verdict)})
        if len(failed) >= MAX_FAILED_IDS:
            break
    return failed


def _inventory_of(result: dict[str, Any]) -> tuple[list[str], bool]:
    execution = result.get("execution") or {}
    ids: list[str] = []
    if isinstance(execution, dict):
        for identity in execution.get("test_case_identities") or []:
            if isinstance(identity, str) and identity:
                ids.append(identity)
    if not ids:
        for entry in result.get("tests") or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ids.append(entry["id"])
    seen: list[str] = []
    for identity in ids:
        if identity not in seen:
            seen.append(identity)
    return seen[:MAX_INVENTORY_IDS], len(seen) > MAX_INVENTORY_IDS


def execute_suite(session, obligation: dict, measured: dict,
                  run_tag: str) -> tuple[dict[str, Any] | None, str | None]:
    """Execute one runnable suite; return (result, applicability-problem)."""
    declaration = obligation["declaration"]
    checkout = Path(str(obligation["checkout"]))
    suite = str((checkout / declaration["executor"]["source_paths"][0]).resolve())
    argv = [suite, "--format", "json", "--result", "result.json"]
    # Declared libraries travel as environment roots, never --library
    # flags: explicit flags replace the provider adapter's own roots and
    # would silently drop the provider native library.
    libraries = []
    for library in declaration["executor"].get("library_paths") or []:
        if not isinstance(library, str) or not library:
            continue
        libraries.append(str((checkout / library).resolve()))
    inherited = os.environ.get("MNCS_LIBRARY_PATH", "")
    if inherited:
        libraries.append(inherited)
    child_env = {"MNCS_LIBRARY_PATH": ":".join(libraries)} if libraries else None
    try:
        directory = _artifact_directory(session, "runs", run_tag)
        result = session.invoke(TEST_CAPABILITY, argv, cwd=str(directory),
                                timeout_seconds=EXECUTION_TIMEOUT_SECONDS,
                                env=child_env)
    except Exception as error:
        return None, f"transport-error:{type(error).__name__}"
    status = result.get("status") if isinstance(result, dict) else None
    if status in ("timeout", "transport-error"):
        return None, f"transport-failed:{status}"
    if status not in ("ok", "failed"):
        return None, f"transport-failed:{status}"
    try:
        document = json.loads((directory / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        detail = (result.get("stderr") or result.get("stdout") or "")[:500]
        return None, f"result-unreadable:{detail or result.get('status')}"
    if not isinstance(document, dict):
        return None, "result-invalid"
    fresh_head = repo_revision(checkout)
    fresh_dirty = dirty_content_digest(checkout)
    if (fresh_head != measured.get("repository_revision")
            or fresh_dirty != measured.get("repository_fingerprint")):
        return None, "state-changed-during-execution"
    if measured.get("closure_trusted"):
        fresh_closure, fresh_trusted = _measure_resolved_closure(session, obligation)
        if not fresh_trusted or fresh_closure != measured.get("closure_identity"):
            return None, "state-changed-during-execution"
    document["_environment_operation"] = {
        "demand_reference": result.get("demand_reference"),
        "provider_request_id": result.get("provider_request_id"),
        "result_kind": "observed",
    }
    return document, None


def _epoch_inputs(session, obligations: list[dict],
                  measured: dict[str, dict | None], invalid: list[dict] | None = None) -> dict[str, Any]:
    bindings: dict[str, str] = {}
    for capability in (COHERENCE_CAPABILITY_V2, COHERENCE_CAPABILITY, TEST_CAPABILITY):
        binding = session_binding(session, capability)
        if binding is None:
            bindings[capability] = "unbound"
        else:
            bindings[capability] = str(binding.get("availability", {}).get("status"))
    # External evidence admitted by the Actions layer participates in
    # the epoch so a newly current receipt invalidates a reused pass.
    # Imported locally: actions.py already imports this module.
    from . import actions as actions_module
    return {
        "invalid": invalid or [],
        "declarations": {str(item["declaration"]["identity"]): digest_hex(
            {"declaration": item["declaration"]}) for item in obligations},
        "measured": {identity: (digest_hex({"current": current})
                                 if current is not None else "unrunnable")
                     for identity, current in measured.items()},
        "external": actions_module.external_epoch_material(session),
        "bindings": bindings,
        "toolchain": toolchain_identity(session),
        "lifecycle": session.snapshot.get("lifecycle", "active"),
    }


def ambient_pass(session, *, mode: str = "ambient",
                 max_executions: int = DEFAULT_MAX_EXECUTIONS,
                 only: str | None = None) -> dict[str, Any]:
    """Evaluate verification obligations; execute only stale runnable ones."""
    started = utcnow()
    clock_started = time.monotonic()
    workspace_root = _workspace_root(session)
    rows = _state_rows(session)
    if workspace_root is None:
        return _finish(session, started, clock_started, [], rows, [], mode,
                       {"reason": "workspace-unavailable"}, "no-workspace", None)
    selected = session.snapshot.get("selected_checkouts")
    checkouts = None
    if isinstance(selected, dict):
        checkouts = {}
        for repository, facts in selected.items():
            if isinstance(facts, dict) and isinstance(facts.get("path"), str):
                path = Path(facts["path"])
                checkouts[repository] = path if path.is_absolute() else workspace_root / path
    obligations, invalid = discover_obligations(workspace_root, checkouts)
    if only is not None:
        obligations = [item for item in obligations
                       if str(item["declaration"]["identity"]) == only]
        if not obligations:
            return _finish(session, started, clock_started, [], rows,
                           invalid, mode,
                           {"reason": f"unknown-obligation:{only}"},
                           f"unknown:{only}", None)
        selected_repos = {item["repository"] for item in obligations}
        invalid = [entry for entry in invalid
                   if entry.get("repository") in selected_repos]
    measured: dict[str, dict | None] = {}
    unmeasurable: dict[str, str] = {}
    for obligation in obligations:
        identity = str(obligation["declaration"]["identity"])
        current, problem = _measured_current(session, obligation)
        measured[identity] = current
        if problem is not None:
            unmeasurable[identity] = problem
    epoch_inputs = _epoch_inputs(session, obligations, measured, invalid)
    epoch = digest_hex(epoch_inputs)
    stored = session.snapshot.get("verification_epoch") or {}
    if (stored.get("epoch") == epoch and mode == "ambient"
            and evidence_store.cacheable(
                session, "verification", stored.get("evidence_ref"))):
        summary = dict(stored.get("summary") or {})
        summary["epoch_reused"] = True
        summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
        return {"summary": summary, "reused": True,
                "evidence": stored.get("evidence_ref"),
                "operation_status": "complete"}
    if not obligations:
        # Invalid declarations remain structural blockers. With no admitted
        # obligations there is no verification policy request to execute.
        # Their exact diagnostic material participates in the epoch.
        return _finish(session, started, clock_started, [], rows, invalid, mode,
                       None, epoch, {"summary": {}})
    coherence_cap, use_v2 = _active_coherence_capability(session)
    if coherence_cap is None:
        return _finish(session, started, clock_started, [], rows, invalid,
                       mode, {"reason": "coherence-unavailable"},
                       f"uncached:{mode}:{epoch}", None)
    actions_rows = session.snapshot.get("actions_state")
    if not isinstance(actions_rows, dict):
        actions_rows = {}
    routed_obligations, overflow_obligations = route_verification_obligations(
        obligations, measured, actions_rows, MAX_OBLIGATIONS)
    request = _coherence_request(
        session, routed_obligations, measured, rows, max_executions, use_v2=use_v2)
    coherence = request_coherence(session, request, capability=coherence_cap)
    if coherence is None:
        return _finish(session, started, clock_started, [], rows, invalid,
                       mode, {"reason": "coherence-failed"},
                       f"uncached:{mode}:{epoch}", None)
    by_identity = {str(item["declaration"]["identity"]): item for item in routed_obligations}
    verdicts = {item["identity"]: item for item in coherence.get("verdicts", [])
                if isinstance(item, dict) and isinstance(item.get("identity"), str)}
    queue = [identity for identity in coherence.get("run_queue", [])
             if isinstance(identity, str) and identity in by_identity]
    results: list[dict[str, Any]] = []
    shared_runs: dict[str, tuple[dict | None, str | None, str]] = {}
    failure: dict | None = None
    for identity, item in by_identity.items():
        verdict = verdicts.get(identity) or {}
        record: dict[str, Any] = {
            "obligation": identity,
            "repository": item["repository"],
            "executor_kind": item["declaration"]["executor"]["kind"],
            "status": verdict.get("status", "unknown"),
            "reason": verdict.get("reason", "unknown"),
            "deferred": bool(verdict.get("deferred", False)),
            "unmeasurable": unmeasurable.get(identity),
        }
        if identity in queue and verdict.get("status") in (
                "new_execution_required", "stale"):
            current = measured.get(identity)
            if current is None:
                record["outcome"] = "not-executed"
                record["detail"] = unmeasurable.get(identity, "unrunnable")
            else:
                key = digest_hex({
                    "suite": current.get("subject_identity"),
                    "libraries": item["declaration"]["executor"].get("library_paths") or [],
                    "toolchain": current.get("toolchain_identity"),
                    "closure": current.get("closure_identity"),
                    "patterns": item["declaration"]["executor"].get("declaration_identities") or [],
                    "verifier": item["declaration"]["executor"].get("verifier_identity", ""),
                })
                if key not in shared_runs:
                    run_tag = f"{digest_hex({'identity': identity})[:8]}-{digest_hex({'at': started})[:8]}"
                    shared_runs[key] = (*execute_suite(session, item, current, run_tag), run_tag)
                document, problem, run_tag = shared_runs[key]
                record["ran"] = document is not None
                if document is None:
                    record["outcome"] = "unknown"
                    record["detail"] = problem
                    failure = {"reason": f"execution-failed:{identity}",
                               "detail": problem}
                else:
                    outcome_verdict = _verdict_of(document)
                    inventory_ids, truncated = _inventory_of(document)
                    evidence_id = f"ver-{digest_hex({'identity': identity, 'at': started})[:12]}"
                    evidence = dict(current)
                    compiler_inventory = str((document.get("execution") or {}).get(
                        "inventory_identity", ""))
                    evidence["inventory_identity"] = digest_hex({
                        "ids": inventory_ids,
                        "compiler_inventory": compiler_inventory,
                    })
                    evidence["verifier_identity"] = str(
                        item["declaration"]["executor"].get("verifier_identity", ""))
                    evidence["verdict"] = outcome_verdict
                    evidence["evidence_id"] = evidence_id
                    rows[identity] = {
                        "evidence": evidence,
                        "verdict": outcome_verdict,
                        "failure_class": str(document.get("failure_class", "none")),
                        "classification": str(document.get("classification", "none")),
                        "failed_test_ids": _failed_test_ids(document),
                        "inventory_test_identities": inventory_ids,
                        "inventory_truncated": truncated,
                        "covered_test_identities": list(
                            verdict.get("resolved_test_identities") or []),
                        "executed_at": utcnow(),
                        "run_id": str((document.get("execution") or {}).get(
                            "run_identity", "")),
                        "run_tag": run_tag,
                    }
                    record["outcome"] = ("failed" if outcome_verdict == "FAIL"
                                         else "passed" if outcome_verdict == "PASS"
                                         else "unknown")
                    record["evidence_id"] = evidence_id
                    record["failure_class"] = str(document.get("failure_class", "none"))
                    record["classification"] = str(document.get("classification", "none"))
                    record["run_tag"] = run_tag
                    record["failed_test_ids"] = _failed_test_ids(document)
                    record["verification_event"] = {
                        "type": "verification.verified" if record["outcome"] == "passed" else "verification.failed",
                        "payload": {"obligation": identity, "evidence_id": evidence_id,
                                    "outcome": record["outcome"],
                                    **document.get("_environment_operation", {})},
                    }

        elif verdict.get("status") in ("new_execution_required", "stale"):
            record["outcome"] = "deferred"
            session._emit("verification.deferred", "environment",
                          {"obligation": identity, "reason": "budget_deferred"})
        elif verdict.get("status") == "current":
            record["outcome"] = ("failed" if verdict.get("verdict") == "FAIL"
                                 else "passed" if verdict.get("verdict") == "PASS"
                                 else "unknown")
            record["evidence_id"] = verdict.get("evidence_id", "")
        elif verdict.get("status") == "selection_unresolved":
            record["outcome"] = "unresolved"
            session._emit("verification.deferred", "environment",
                          {"obligation": identity, "reason": "selection_unresolved"})
        elif verdict.get("status") == "contradictory":
            record["outcome"] = "contradictory"
            session._emit("verification.failed", "environment",
                          {"obligation": identity, "outcome": "contradictory"})
        elif verdict.get("status") == "escalation_required":
            record["outcome"] = "escalated"
            session._emit("verification.deferred", "environment",
                          {"obligation": identity, "reason": "escalation_required"})
        elif verdict.get("status") == "not_selected":
            record["outcome"] = "excluded"
        else:
            record["outcome"] = "unknown"
        results.append(record)
    for item in overflow_obligations:
        declaration = item["declaration"]
        identity = str(declaration["identity"])
        inactive = declaration.get("lifecycle") in ("scheduled", "reference_only", "retired")
        outcome = "excluded" if inactive else "deferred"
        results.append({
            "obligation": identity,
            "repository": item["repository"],
            "executor_kind": declaration["executor"]["kind"],
            "status": "not_selected" if inactive else "deferred_overflow",
            "reason": "obligation_window_overflow",
            "deferred": not inactive,
            "overflow": True,
            "unmeasurable": unmeasurable.get(identity),
            "outcome": outcome,
        })
        if not inactive:
            session._emit("verification.deferred", "environment",
                          {"obligation": identity, "reason": "obligation_window_overflow"})
    results.sort(key=lambda entry: str(entry.get("obligation", "")))
    return _finish(session, started, clock_started, results, rows,
                   invalid, mode, failure, epoch, coherence)


def _finish(session, started: str, clock_started: float,
            results: list[dict], rows: dict, invalid: list[dict],
            mode: str, failure: dict | None,
            epoch: str, coherence: dict | None) -> dict[str, Any]:
    native = (coherence or {}).get("summary") or {}
    overflow_records = [record for record in results if record.get("overflow")]
    overflow_ids = [str(record.get("obligation")) for record in overflow_records]
    overflow_deferred = sum(1 for record in overflow_records if record.get("deferred"))
    overflow_excluded = sum(1 for record in overflow_records if record.get("outcome") == "excluded")
    summary: dict[str, Any] = {
        "obligations": len(results),
        "current": 0, "executed": 0, "failed": 0, "unknown": 0,
        "unsupported": int(native.get("unsupported", 0)),
        "unresolved": int(native.get("unresolved", 0)),
        "contradictory": int(native.get("contradictory", 0)),
        "excluded": int(native.get("excluded", 0)) + overflow_excluded,
        "deferred": int(native.get("deferred", 0)) + overflow_deferred,
        "overflow": len(overflow_records),
        "overflow_ids": overflow_ids,
        "invalid": len(invalid), "blockers": 0,
        "epoch_reused": False,
    }
    failed_ids: list[str] = []
    for record in results:
        outcome = record.get("outcome")
        if outcome == "passed":
            summary["current"] += 1
        if record.get("ran"):
            summary["executed"] += 1
        if outcome == "failed":
            summary["failed"] += 1
            failed_ids.append(str(record.get("obligation")))
        elif outcome == "unknown":
            summary["unknown"] += 1
    summary["failed_ids"] = failed_ids[:MAX_FAILED_IDS]
    summary["blockers"] = (summary["failed"] + summary["unknown"]
                           + summary["unresolved"] + summary["contradictory"]
                           + len(invalid) + (1 if failure is not None else 0))
    evidence = {"schema_version": SCHEMA, "mode": mode,
                "started_at": started, "finished_at": utcnow(),
                "summary": summary, "results": results,
                "overflow": overflow_records,
                "invalid": invalid, "failure": failure,
                "coherence_summary": dict(native)}
    evidence_ref = _write_evidence(session, evidence, mode)
    # Publish the existing immutable verification evidence before its event.
    # This gives a request/result join a readable owner record, not just a tag.
    for record in results:
        observed = record.get("verification_event")
        if isinstance(observed, dict):
            session._emit(observed["type"], "environment", {
                **observed["payload"], "verification_evidence": evidence_ref,
                "verification_evidence_digest": digest_hex(evidence, length=64),
            })
    summary["elapsed_seconds"] = round(time.monotonic() - clock_started, 3)
    if failure is None:
        session.snapshot["verification_state"] = rows
    history = list(session.snapshot.get("verification_history") or [])
    history.append({"at": utcnow(), "mode": mode, "summary": dict(summary),
                    "evidence_ref": evidence_ref})
    session.snapshot["verification_history"] = history[-MAX_HISTORY:]
    # Transport/execution failures never cache their epoch: the next pass
    # must re-observe, not replay a degraded world. Recorded FAIL verdicts
    # cache normally because fail-is-current knowledge is exact.
    if mode == "ambient" and failure is None:
        stored_epoch = epoch
    else:
        stored_epoch = f"uncached:{mode}:{epoch}"
    session.snapshot["verification_epoch"] = {
        "epoch": stored_epoch,
        "summary": dict(summary), "evidence_ref": evidence_ref,
        "at": utcnow()}
    _append_session_evidence(session, summary, results, mode)
    try:
        session._save()
    except (OSError, ValueError):
        summary["persisted"] = False
    else:
        summary["persisted"] = True
    return {"summary": summary, "reused": False,
            "evidence": evidence_ref,
            "operation_status": "retry" if failure is not None else "complete"}


def _write_evidence(session, evidence: dict[str, Any],
                    mode: str) -> str:
    stored = evidence_store.publish(session, "verification", evidence)
    if stored is not None:
        return stored
    directory = _artifact_directory(session, "evidence")
    ref = f"verification-{mode}-{digest_hex(evidence['finished_at'])[:12]}.json"
    path = directory / ref
    try:
        path.write_text(json.dumps(evidence, indent=2, sort_keys=True),
                        encoding="utf-8")
    except OSError:
        return "verification-evidence-unwritable"
    return f"sessions/{session.session_id}/verification-artifacts/evidence/{ref}"


def _append_session_evidence(session, summary: dict[str, Any],
                             results: list[dict], mode: str) -> None:
    entry = {
        "schema_version": EVIDENCE_SCHEMA,
        "session": session.session_id,
        "consumer": session.snapshot.get("consumer_id"),
        "mode": mode,
        "summary": {key: summary.get(key) for key in
                    ("obligations", "current", "executed", "failed",
                     "unknown", "unsupported", "unresolved", "blockers")},
        "obligations": [
            {"obligation": record.get("obligation"),
             "repository": record.get("repository"),
             "executor_kind": record.get("executor_kind"),
             "outcome": record.get("outcome"),
             "status": record.get("status"),
             "reason": record.get("reason"),
             "evidence_id": record.get("evidence_id"),
             "failure_class": record.get("failure_class"),
             "classification": record.get("classification")}
            for record in results],
        "recorded_at": utcnow(),
    }
    try:
        directory = _artifact_directory(session)
        path = directory / "session-evidence.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        pass


def terse(session) -> dict[str, Any]:
    """Compact verification status for normal agent context."""
    stored = session.snapshot.get("verification_epoch") or {}
    summary = dict(stored.get("summary") or {})
    capsule: dict[str, Any] = {
        "current": summary.get("current", 0),
        "executed": summary.get("executed", 0),
        "failed": summary.get("failed", 0),
        "deferred": summary.get("deferred", 0),
        "blockers": summary.get("blockers", 0),
        "failed_ids": summary.get("failed_ids", []),
        "evidence": stored.get("evidence_ref"),
    }
    if summary.get("overflow", 0):
        capsule["overflow"] = summary.get("overflow", 0)
    return {"verification": capsule}


def read_evidence(session) -> dict[str, Any]:
    """Full verification evidence for explicit inspection."""
    stored = session.snapshot.get("verification_epoch") or {}
    ref = stored.get("evidence_ref", "")
    if evidence_store.is_store_reference(ref):
        return {"evidence": evidence_store.read(session, "verification", ref),
                "history": session.snapshot.get("verification_history") or []}
    if not ref or not ref.startswith("sessions/"):
        return {"evidence": None, "history": session.snapshot.get(
            "verification_history") or []}
    path = (session.store.state_dir / ref).resolve()
    try:
        if not path.is_relative_to(session.store.state_dir.resolve()):
            raise OSError("evidence escapes state root")
        evidence = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        evidence = None
    return {"evidence": evidence, "history": session.snapshot.get(
        "verification_history") or []}
