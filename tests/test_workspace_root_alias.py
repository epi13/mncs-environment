from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from mncs_env import sessions, workspace  # noqa: E402


class ControlWorkspaceAliasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.mount = self.base / "control-workspace"
        self.mount.mkdir()
        self.alias = self.base / "Projects"
        self.alias.symlink_to(self.mount, target_is_directory=True)

        self.repository = self.mount / "mncs-environment"
        (self.repository / ".mncs").mkdir(parents=True)
        (self.repository / ".mncs" / "project.json").write_text(
            json.dumps({
                "schema_version": "mncs.project/1",
                "repository": "mncs-environment",
            }) + "\n",
            encoding="utf-8",
        )
        (self.repository / "README.md").write_text("test checkout\n", encoding="utf-8")
        self._git(self.repository, "init", "-b", "main")
        self._git(self.repository, "config", "user.name", "MNCS Environment Test")
        self._git(
            self.repository, "config", "user.email", "environment-test@example.invalid"
        )
        self._git(self.repository, "add", ".mncs/project.json", "README.md")
        self._git(self.repository, "commit", "-m", "Create test checkout")
        self.campaign = self.repository / ".worktrees" / "campaign"
        self._git(
            self.repository, "worktree", "add", "-b", "campaign/test", str(self.campaign)
        )

    @staticmethod
    def _git(repository: Path, *arguments: str) -> None:
        subprocess.run(
            ["git", "-C", str(repository), *arguments],
            check=True,
            capture_output=True,
            text=True,
        )

    @contextmanager
    def _control_mount(self) -> Iterator[None]:
        with (
            mock.patch.object(workspace, "CONTROL_WORKSPACE_MOUNT", self.mount),
            mock.patch.object(
                workspace, "_is_control_workspace_mount", return_value=True
            ),
        ):
            yield

    def test_scope_checkout_accepts_control_mount_alias_and_selected_anchor(self) -> None:
        main_facts = workspace.verify_registered_checkout(
            self.repository, repository="mncs-environment"
        )
        campaign_facts = workspace.verify_registered_checkout(
            self.campaign,
            repository="mncs-environment",
            workspace_root=self.mount,
            expected_branch="campaign/test",
        )
        selected = {
            "repository": "mncs-environment",
            "path": str(self.alias / "mncs-environment"),
            "branch": "main",
            "checkout_identity": main_facts["checkout_identity"],
            "git_common_directory_identity": main_facts[
                "git_common_directory_identity"
            ],
            "observed_path": main_facts["observed_path"],
        }
        authorized_checkout = {
            "path": str(self.alias / "mncs-environment" / ".worktrees" / "campaign"),
            "branch": "campaign/test",
            "checkout_identity": campaign_facts["checkout_identity"],
            "git_common_directory_identity": campaign_facts[
                "git_common_directory_identity"
            ],
            "observed_path": campaign_facts["observed_path"],
        }

        with self._control_mount():
            checkout, identity = sessions._verified_scope_checkout(
                self.alias,
                "mncs-environment",
                selected,
                authorized_checkout=authorized_checkout,
            )

        self.assertEqual(checkout, self.repository)
        self.assertEqual(identity["branch"], "main")
        self.assertEqual(
            identity["git_common_directory_identity"],
            campaign_facts["git_common_directory_identity"],
        )

    def test_only_exact_control_mount_alias_is_accepted(self) -> None:
        with self._control_mount():
            self.assertEqual(
                workspace.resolve_control_workspace_alias(self.alias), self.mount
            )

            unrelated = self.base / "unrelated"
            unrelated.mkdir()
            unrelated_alias = self.base / "other-projects"
            unrelated_alias.symlink_to(unrelated, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                workspace.resolve_control_workspace_alias(unrelated_alias)

            nested_alias = self.repository / "checkout-alias"
            nested_alias.symlink_to(self.campaign, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                workspace.resolve_control_workspace_alias(
                    self.alias / "mncs-environment" / "checkout-alias"
                )

        with (
            mock.patch.object(workspace, "CONTROL_WORKSPACE_MOUNT", self.mount),
            mock.patch.object(
                workspace, "_is_control_workspace_mount", return_value=False
            ),
        ):
            with self.assertRaisesRegex(ValueError, "verified mount"):
                workspace.resolve_control_workspace_alias(self.alias)


if __name__ == "__main__":
    unittest.main()
