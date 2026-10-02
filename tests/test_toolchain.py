import os

import pytest

from mncs_env.session_store import (store_provider_from_environment,
    write_session_store_provider, _session_store_provider)
from mncs_env.store_backend import _selected_store_runtime
from mncs_env.toolchain import selected_stdlib_root


def test_selected_stdlib_survives_fresh_store_open(tmp_path, monkeypatch):
    store = tmp_path / "mncs-store"
    package = store / "python" / "mncs_store"
    package.mkdir(parents=True)
    (package / "__init__.py").touch()
    language = tmp_path / "mncs-language"
    binary = language / "target" / "release" / "mncs"
    binary.parent.mkdir(parents=True)
    binary.touch()
    embed = binary.parent / "libmncs_embed.so"
    embed.touch()
    stdlib = tmp_path / "mncs-stdlib" / ".worktrees" / "selected"
    (stdlib / "library").mkdir(parents=True)
    facts = {"workspace": {"root": str(tmp_path)}, "selected_checkouts": {
        "mncs-store": {"path": str(store), "head": "store-rev"},
        "mncs-language": {"path": str(language), "head": "language-rev"},
        "mncs-stdlib": {"path": str(stdlib), "head": "stdlib-rev"}},
        "toolchain": {"checkout": str(language), "revision": "language-rev",
                      "binary": str(binary), "status": "available"}}
    binding = store_provider_from_environment(facts)
    write_session_store_provider(tmp_path / "state", "session", binding)
    reopened = _session_store_provider(tmp_path / "state", "session")
    assert reopened["runtime_environment"]["MNCS_STDLIB_ROOT"] == str(stdlib)
    monkeypatch.setenv("MNCS_STDLIB_ROOT", "/foreign/provider")
    with _selected_store_runtime(reopened["runtime_environment"]):
        assert os.environ["MNCS_STDLIB_ROOT"] == str(stdlib)
    assert os.environ["MNCS_STDLIB_ROOT"] == "/foreign/provider"
    runtime_without_stdlib = dict(reopened["runtime_environment"])
    runtime_without_stdlib.pop("MNCS_STDLIB_ROOT")
    with _selected_store_runtime(runtime_without_stdlib):
        assert "MNCS_STDLIB_ROOT" not in os.environ
    assert os.environ["MNCS_STDLIB_ROOT"] == "/foreign/provider"


def test_selected_stdlib_cannot_escape_or_fall_back(tmp_path):
    with pytest.raises(ValueError, match="unavailable inside"):
        selected_stdlib_root(tmp_path, {"path": str(tmp_path.parent)})
    with pytest.raises(ValueError, match="unavailable inside"):
        selected_stdlib_root(tmp_path, {"path": "missing"})
