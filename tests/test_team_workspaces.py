from __future__ import annotations

import concurrent.futures
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from contextlib import contextmanager

from rag_ime.team.errors import TeamError
from rag_ime.team.grants import TeamGrantStore
from rag_ime.team.identity import TeamIdentityStore
from rag_ime.team.project_context import TeamProjectContext
from rag_ime.team.workspaces import TeamWorkspaceService


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": "/usr/bin:/bin",
        }
    )
    return env


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=_git_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode:
        raise AssertionError(f"git {args!r} failed: {completed.stderr}")
    return completed.stdout.strip()


class TeamWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="paw-team-workspaces-")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "team.sqlite3"
        self.identity = TeamIdentityStore(self.db_path)
        self.admin = self.identity.bootstrap_admin("admin", "administrator-password")
        self.alice = self.identity.create_member(
            self.admin["id"], "alice", "alice-password-123"
        )
        self.bob = self.identity.create_member(self.admin["id"], "bob", "bob-password-123")
        self.project = self.identity.create_project(self.admin["id"], "Shared site")
        self.project_id = str(self.project["id"])
        self.identity.add_project_member(
            self.admin["id"], self.project_id, self.alice["id"], "maintainer"
        )
        self.identity.add_project_member(
            self.admin["id"], self.project_id, self.bob["id"], "contributor"
        )

        self.source = self.root / "source"
        self.source.mkdir()
        _git(self.source, "init", "--initial-branch=main")
        _git(self.source, "config", "user.name", "fixture")
        _git(self.source, "config", "user.email", "fixture@example.test")
        (self.source / "README.md").write_text("initial\n", encoding="utf-8")
        _git(self.source, "add", "--", "README.md")
        _git(self.source, "commit", "-m", "initial")

        self.grants = TeamGrantStore(self.db_path, self.identity)
        self.quiesce_events: list[str] = []
        self.verifier_contexts: list[dict[str, object]] = []
        self.verifier_ok = True

        @contextmanager
        def quiesce(session_id: str):
            self.quiesce_events.append(f"enter:{session_id}")
            try:
                yield
            finally:
                self.quiesce_events.append(f"exit:{session_id}")

        def verifier(candidate_path: Path, context: dict[str, object]) -> dict[str, object]:
            self.assertTrue(candidate_path.is_dir())
            self.verifier_contexts.append(dict(context))
            self.assertEqual(context["candidatePath"], str(candidate_path))
            temporary_path = Path(str(context["temporaryPath"]))
            self.assertTrue(temporary_path.is_relative_to(self.service.data_root))
            self.assertNotEqual(temporary_path.stat().st_mode & 0o200, 0)
            for entry in (candidate_path, *candidate_path.rglob("*")):
                self.assertEqual(entry.stat().st_mode & 0o222, 0)
            return {"ok": self.verifier_ok}

        self.service = TeamWorkspaceService(
            self.root / "team-data",
            self.identity,
            self.grants,
            quiesce,
            verifier,
        )
        self.project_record = self.service.ensure_project(self.project_id, self.source)
        self.project_context = TeamProjectContext(self)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _allocate(self, owner: dict[str, object], session_id: str) -> dict[str, object]:
        return self.service.allocate_workspace(
            self.project_id,
            session_id,
            owner_user_id=str(owner["id"]),
            session_id=session_id,
        )

    def test_imported_preview_source_is_fixed_and_requires_validation(self) -> None:
        (self.source / "README.md").write_text("uncommitted private edit")
        baseline = self.service.preview_baseline(self.admin['id'], self.project_id)
        deployment_id = 'pv-' + '1' * 32
        source = self.service.create_preview_source(
            self.admin['id'], self.project_id, branch=baseline['branch'], commit=baseline['commit'],
            requirements_revision=baseline['requirementsRevision'], deployment_id=deployment_id,
        )
        self.assertEqual((source / 'README.md').read_text(), 'initial\n')
        self.assertFalse((source / '.git').exists())
        self.assertEqual(len(self.verifier_contexts), 1)
        self.service.remove_preview_source(deployment_id)
        self.assertFalse(source.parent.exists())

    def test_applied_exact_candidate_verification_can_be_reused_by_preview(self) -> None:
        workspace = self._allocate(self.admin, 'session-preview')
        Path(str(workspace['path']), 'app.txt').write_text('delivered app\n')
        draft = self.service.share_draft('session-preview', self.admin['id'], 'Preview candidate')
        integration = self.service.integrate_draft(draft['draftId'], self.admin['id'])
        self.assertEqual(integration['status'], 'integrated')
        self.assertEqual(len(self.verifier_contexts), 1)
        source = self.service.create_preview_source(
            self.admin['id'], self.project_id, branch='main', commit=integration['headCommit'],
            requirements_revision=0, deployment_id='pv-' + '2' * 32,
        )
        self.assertEqual((source / 'app.txt').read_text(), 'delivered app\n')
        self.assertEqual(len(self.verifier_contexts), 1)
        self.service.remove_preview_source('pv-' + '2' * 32)

    def test_preview_failed_validation_removes_source_and_never_advances_repository(self) -> None:
        self.verifier_ok = False
        head = self.project_record['headCommit']
        with self.assertRaises(TeamError) as error:
            self.service.create_preview_source(
                self.admin['id'], self.project_id, branch='main', commit=head,
                requirements_revision=0, deployment_id='pv-' + '3' * 32,
            )
        self.assertEqual(error.exception.code, 'preview_validation_failed')
        self.assertEqual(self.service.preview_baseline(self.admin['id'], self.project_id)['commit'], head)
        self.assertFalse((self.service.staging_root / ('preview-pv-' + '3' * 32)).exists())

    def test_workspace_and_draft_freeze_requirements_and_stale_integration_preserves_head(self) -> None:
        first_brief = self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 0,
                "objective": "Ship the first requirements",
                "acceptanceCriteria": ["The first contract is explicit"],
            },
        )
        workspace = self._allocate(self.admin, "session-requirements-old")
        self.assertEqual(workspace["requirementsRevision"], first_brief["revision"])

        second_brief = self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 1,
                "objective": "Ship the revised requirements",
                "acceptanceCriteria": ["The revised contract is explicit"],
            },
        )
        self.assertEqual(second_brief["revision"], 2)
        inherited = self.service.allocate_workspace(
            self.project_id,
            "session-requirements-inherited",
            owner_user_id=str(self.admin["id"]),
            session_id="session-requirements-inherited",
            requirements_revision=1,
        )
        self.assertEqual(inherited["requirementsRevision"], 1)

        Path(str(workspace["path"]), "requirements.txt").write_text(
            "first baseline\n", encoding="utf-8"
        )
        draft = self.service.share_draft(
            "session-requirements-old",
            str(self.admin["id"]),
            "first requirements draft",
        )
        self.assertEqual(draft["requirementsRevision"], 1)
        repo = Path(str(self.project_record["repositoryPath"]))
        head_before = _git(repo, "rev-parse", "refs/heads/main")
        with self.assertRaises(TeamError) as stale:
            self.service.integrate_draft(str(draft["draftId"]), str(self.admin["id"]))
        self.assertEqual((stale.exception.status, stale.exception.code), (409, "stale_requirements"))
        self.assertEqual(_git(repo, "rev-parse", "refs/heads/main"), head_before)
        self.assertEqual(
            self.service.read_draft(
                self.project_id,
                str(draft["draftId"]),
                str(self.admin["id"]),
            )["status"],
            "pending",
        )

    def test_allocate_rejects_an_unavailable_inherited_requirements_revision(self) -> None:
        with self.assertRaises(TeamError) as unavailable:
            self.service.allocate_workspace(
                self.project_id,
                "session-requirements-missing",
                owner_user_id=str(self.admin["id"]),
                session_id="session-requirements-missing",
                requirements_revision=1,
            )
        self.assertEqual(
            (unavailable.exception.status, unavailable.exception.code),
            (409, "requirements_revision_not_found"),
        )

    def test_accept_requirements_changes_baseline_once_and_replay_does_not_quiesce_again(self) -> None:
        initial = self._allocate(self.admin, "session-accept-requirements")
        with self.assertRaises(TeamError) as unpublished:
            self.service.accept_requirements(
                "session-accept-requirements",
                str(self.admin["id"]),
                base_revision=0,
                revision=0,
            )
        self.assertEqual(
            (unpublished.exception.status, unpublished.exception.code),
            (409, "requirements_not_published"),
        )

        self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 0,
                "objective": "First published requirements",
                "acceptanceCriteria": ["First criteria"],
            },
        )
        changed = self.service.accept_requirements(
            "session-accept-requirements",
            str(self.admin["id"]),
            base_revision=int(initial["requirementsRevision"]),
            revision=1,
        )
        self.assertEqual(changed["previousRequirementsRevision"], 0)
        self.assertEqual(changed["requirementsRevision"], 1)
        self.assertEqual(
            self.quiesce_events,
            ["enter:session-accept-requirements", "exit:session-accept-requirements"],
        )

        self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 1,
                "objective": "Second published requirements",
                "acceptanceCriteria": ["Second criteria"],
            },
        )
        self.service.accept_requirements(
            "session-accept-requirements",
            str(self.admin["id"]),
            base_revision=1,
            revision=2,
        )
        event_count = len(self.quiesce_events)
        replay = self.service.accept_requirements(
            "session-accept-requirements",
            str(self.admin["id"]),
            # An old client may still send its earlier base after the first
            # successful update; the already-current request is idempotent.
            base_revision=1,
            revision=2,
        )
        self.assertEqual(replay["requirementsRevision"], 2)
        self.assertEqual(replay["previousRequirementsRevision"], 2)
        self.assertEqual(len(self.quiesce_events), event_count)

    def test_requirements_for_session_returns_fixed_brief_and_current_revision(self) -> None:
        self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 0,
                "objective": "Fixed session objective",
                "acceptanceCriteria": ["Fixed criterion"],
            },
        )
        self._allocate(self.admin, "session-requirements-context")
        self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 1,
                "objective": "New project objective",
                "acceptanceCriteria": ["New criterion"],
            },
        )
        fixed = self.service.requirements_for_session("session-requirements-context")
        self.assertEqual(
            fixed,
            {
                "spaceId": self.project_id,
                "requirementsRevision": 1,
                "currentRequirementsRevision": 2,
                "objective": "Fixed session objective",
                "acceptanceCriteria": ["Fixed criterion"],
            },
        )
        self.service.accept_requirements(
            "session-requirements-context",
            str(self.admin["id"]),
            base_revision=1,
            revision=2,
        )
        current = self.service.requirements_for_session("session-requirements-context")
        self.assertEqual(current["requirementsRevision"], 2)
        self.assertEqual(current["currentRequirementsRevision"], 2)
        self.assertEqual(current["objective"], "New project objective")

        self._allocate(self.alice, "session-requirements-revoked")
        self.identity.remove_project_member(
            self.admin["id"],
            self.project_id,
            self.alice["id"],
        )
        with self.assertRaises(TeamError):
            self.service.requirements_for_session("session-requirements-revoked")

    def test_requirements_change_during_verification_cannot_advance_the_target(self) -> None:
        self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 0,
                "objective": "Verifier baseline",
                "acceptanceCriteria": ["Baseline is checked"],
            },
        )
        workspace = self._allocate(self.admin, "session-requirements-race")
        Path(str(workspace["path"]), "race.txt").write_text("candidate\n", encoding="utf-8")
        draft = self.service.share_draft(
            "session-requirements-race",
            str(self.admin["id"]),
            "candidate under verification",
        )
        repo = Path(str(self.project_record["repositoryPath"]))
        head_before = _git(repo, "rev-parse", "refs/heads/main")
        verifier_entered = threading.Event()
        release_verifier = threading.Event()

        def blocking_verifier(_candidate_path: Path, _context: dict[str, object]) -> dict[str, object]:
            verifier_entered.set()
            if not release_verifier.wait(timeout=10):
                return {"ok": False, "error": "test verifier timeout"}
            return {"ok": True}

        self.service.verifier = blocking_verifier
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                self.service.integrate_draft,
                str(draft["draftId"]),
                str(self.admin["id"]),
            )
            try:
                self.assertTrue(verifier_entered.wait(timeout=10))
                self.project_context.publish_brief(
                    self.admin["id"],
                    self.project_id,
                    {
                        "baseRevision": 1,
                        "objective": "Verifier revised requirements",
                        "acceptanceCriteria": ["The revised baseline is checked"],
                    },
                )
            finally:
                release_verifier.set()
            with self.assertRaises(TeamError) as stale:
                future.result(timeout=15)
        self.assertEqual((stale.exception.status, stale.exception.code), (409, "stale_requirements"))
        self.assertEqual(_git(repo, "rev-parse", "refs/heads/main"), head_before)
        self.assertEqual(
            self.service.read_draft(
                self.project_id,
                str(draft["draftId"]),
                str(self.admin["id"]),
            )["status"],
            "pending",
        )

    def test_terminal_integration_receipt_replays_after_new_requirements(self) -> None:
        self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 0,
                "objective": "Receipt baseline",
                "acceptanceCriteria": ["Receipt is durable"],
            },
        )
        workspace = self._allocate(self.admin, "session-requirements-receipt")
        Path(str(workspace["path"]), "receipt.txt").write_text("done\n", encoding="utf-8")
        draft = self.service.share_draft(
            "session-requirements-receipt",
            str(self.admin["id"]),
            "durable receipt",
        )
        result = self.service.integrate_draft(str(draft["draftId"]), str(self.admin["id"]))
        self.assertEqual(result["status"], "integrated")
        self.project_context.publish_brief(
            self.admin["id"],
            self.project_id,
            {
                "baseRevision": 1,
                "objective": "Receipt revised requirements",
                "acceptanceCriteria": ["New work uses the revision"],
            },
        )
        self.assertEqual(
            self.service.integrate_draft(str(draft["draftId"]), str(self.admin["id"])),
            result,
        )

    def test_parallel_independent_drafts_integrate_against_latest_head(self) -> None:
        first = self._allocate(self.admin, "session-first")
        second = self._allocate(self.alice, "session-second")
        Path(str(first["path"]), "one.txt").write_text("one\n", encoding="utf-8")
        Path(str(second["path"]), "two.txt").write_text("two\n", encoding="utf-8")

        draft_one = self.service.share_draft("session-first", str(self.admin["id"]), "first")
        draft_two = self.service.share_draft("session-second", str(self.alice["id"]), "second")
        repo = Path(str(self.project_record["repositoryPath"]))
        self.assertEqual(
            _git(repo, "rev-list", "--parents", "-1", str(draft_one["draftCommit"])).split()[1],
            draft_one["baseCommit"],
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            first_result, second_result = list(
                executor.map(
                    lambda item: self.service.integrate_draft(item, str(self.admin["id"])),
                    (str(draft_one["draftId"]), str(draft_two["draftId"])),
                )
            )

        self.assertEqual(first_result["status"], "integrated")
        self.assertEqual(second_result["status"], "integrated")
        self.assertEqual((repo / "one.txt").read_text(encoding="utf-8"), "one\n")
        self.assertEqual((repo / "two.txt").read_text(encoding="utf-8"), "two\n")
        self.assertEqual(self.quiesce_events, ["enter:session-first", "exit:session-first", "enter:session-second", "exit:session-second"])

    def test_conflicting_edit_preserves_target_and_returns_actionable_result(self) -> None:
        first = self._allocate(self.admin, "session-first")
        second = self._allocate(self.alice, "session-second")
        Path(str(first["path"]), "README.md").write_text("first\n", encoding="utf-8")
        Path(str(second["path"]), "README.md").write_text("second\n", encoding="utf-8")
        draft_one = self.service.share_draft("session-first", str(self.admin["id"]), "first")
        draft_two = self.service.share_draft("session-second", str(self.alice["id"]), "second")
        self.assertEqual(
            self.service.integrate_draft(str(draft_one["draftId"]), str(self.admin["id"]))["status"],
            "integrated",
        )
        result = self.service.integrate_draft(str(draft_two["draftId"]), str(self.admin["id"]))
        self.assertEqual(result["status"], "conflict")
        repo = Path(str(self.project_record["repositoryPath"]))
        self.assertEqual((repo / "README.md").read_text(encoding="utf-8"), "first\n")
        self.assertEqual(_git(repo, "rev-parse", "refs/heads/main"), str(result["headCommit"]))

    def test_untrusted_git_metadata_is_never_executed_or_published(self) -> None:
        workspace = self._allocate(self.admin, "session-hooks")
        workspace_path = Path(str(workspace["path"]))
        marker = self.root / "hook-ran"
        hook = workspace_path / ".git" / "hooks" / "pre-commit"
        hook.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        hook.chmod(0o755)
        (workspace_path / ".git" / "config").write_text(
            "[core]\n\thooksPath = .git/hooks\n\tfsmonitor = true\n",
            encoding="utf-8",
        )
        (workspace_path / ".gitattributes").write_text(
            "*.txt filter=untrusted\n", encoding="utf-8"
        )
        (workspace_path / "change.txt").write_text("safe\n", encoding="utf-8")

        draft = self.service.share_draft("session-hooks", str(self.admin["id"]), "safe")
        result = self.service.integrate_draft(str(draft["draftId"]), str(self.admin["id"]))
        self.assertEqual(result["status"], "integrated")
        self.assertFalse(marker.exists())
        self.assertNotIn(".git", {str(item["path"]) for item in draft["manifest"]})
        self.assertNotIn(".gitattributes", {str(item["path"]) for item in draft["manifest"]})

    def test_generic_source_directories_are_retained_while_explicit_private_files_are_skipped(self) -> None:
        workspace = self._allocate(self.admin, "session-source-layout")
        for relative in (
            "packages/runtime/source.py",
            "src/sessions/session.py",
            "src/logs/output.txt",
            "private/notes.txt",
        ):
            path = Path(str(workspace["path"]), relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(relative + "\n", encoding="utf-8")
        (Path(str(workspace["path"])) / ".env").write_text("TOKEN=private\n", encoding="utf-8")
        (Path(str(workspace["path"])) / "credentials.json").write_text("{}\n", encoding="utf-8")
        private_state = Path(str(workspace["path"])) / ".pi" / "state.json"
        private_state.parent.mkdir()
        private_state.write_text("{}\n", encoding="utf-8")

        draft = self.service.share_draft("session-source-layout", str(self.admin["id"]), "source layout")
        paths = {str(item["path"]) for item in draft["manifest"]}
        self.assertTrue(
            {
                "packages/runtime/source.py",
                "src/sessions/session.py",
                "src/logs/output.txt",
                "private/notes.txt",
            }.issubset(paths)
        )
        self.assertFalse({".env", "credentials.json", ".pi/state.json"} & paths)
        self.assertEqual(
            set(draft["excludedPaths"]),
            {".env", "credentials.json", ".pi"},
        )
        self.assertFalse(draft["excludedPathsTruncated"])
        reread = self.service.read_draft(
            self.project_id,
            str(draft["draftId"]),
            str(self.admin["id"]),
        )
        self.assertEqual(reread["excludedPaths"], draft["excludedPaths"])
        integrated = self.service.integrate_draft(str(draft["draftId"]), str(self.admin["id"]))
        self.assertEqual(integrated["status"], "integrated")
        integrated_read = self.service.read_draft(
            self.project_id,
            str(draft["draftId"]),
            str(self.admin["id"]),
        )
        self.assertEqual(integrated_read["excludedPaths"], draft["excludedPaths"])

    def test_symlink_and_workspace_id_traversal_are_rejected(self) -> None:
        with self.assertRaises(TeamError) as invalid_workspace:
            self.service.allocate_workspace(
                self.project_id,
                "../escape",
                owner_user_id=str(self.admin["id"]),
                session_id="session-invalid",
            )
        self.assertEqual(invalid_workspace.exception.code, "invalid_input")

        workspace = self._allocate(self.admin, "session-symlink")
        outside = self.root / "outside.txt"
        outside.write_text("private\n", encoding="utf-8")
        Path(str(workspace["path"]), "linked.txt").symlink_to(outside)
        with self.assertRaises(TeamError) as unsafe:
            self.service.share_draft("session-symlink", str(self.admin["id"]), "unsafe")
        self.assertEqual((unsafe.exception.status, unsafe.exception.code), (400, "unsafe_workspace"))

    def test_verifier_failure_does_not_move_stable_head_and_replay_is_idempotent(self) -> None:
        workspace = self._allocate(self.admin, "session-verify")
        Path(str(workspace["path"]), "candidate.txt").write_text("candidate\n", encoding="utf-8")
        draft = self.service.share_draft("session-verify", str(self.admin["id"]), "candidate")
        stable_head = str(self.project_record["headCommit"])
        self.verifier_ok = False
        result = self.service.integrate_draft(str(draft["draftId"]), str(self.admin["id"]))
        self.assertEqual(result["status"], "verification_failed")
        self.assertEqual(result["headCommit"], stable_head)
        self.assertEqual(
            self.service.integrate_draft(str(draft["draftId"]), str(self.admin["id"])), result
        )
        repo = Path(str(self.project_record["repositoryPath"]))
        self.assertEqual(_git(repo, "rev-parse", "refs/heads/main"), stable_head)

    def test_draft_listing_and_reading_require_current_membership_and_revoked_member_cannot_publish(self) -> None:
        workspace = self._allocate(self.alice, "session-revoked")
        Path(str(workspace["path"]), "member.txt").write_text("member\n", encoding="utf-8")
        draft = self.service.share_draft("session-revoked", str(self.alice["id"]), "member")
        listed = self.service.list_drafts(self.project_id, str(self.admin["id"]))
        self.assertEqual([item["draftId"] for item in listed], [draft["draftId"]])
        read = self.service.read_draft(self.project_id, str(draft["draftId"]), str(self.admin["id"]))
        self.assertEqual(read["manifest"], draft["manifest"])

        self.identity.remove_project_member(
            self.admin["id"], self.project_id, self.alice["id"]
        )
        with self.assertRaises(TeamError):
            self.service.share_draft("session-revoked", str(self.alice["id"]), "again")
        with self.assertRaises(TeamError):
            self.service.list_drafts(self.project_id, str(self.alice["id"]))

    def test_published_draft_survives_source_member_removal(self) -> None:
        maintainer = self.identity.create_member(
            self.admin["id"], "delivery-maintainer", "delivery-maintainer-password"
        )
        self.identity.add_project_member(
            self.admin["id"], self.project_id, maintainer["id"], "maintainer"
        )
        source_workspace = self._allocate(self.alice, "session-source-removed")
        target_workspace = self._allocate(maintainer, "session-target-removed")
        Path(str(source_workspace["path"]), "delivered.txt").write_text(
            "kept after author leaves\n", encoding="utf-8"
        )
        draft = self.service.share_draft(
            "session-source-removed", str(self.alice["id"]), "retain published work"
        )

        self.identity.remove_project_member(
            self.admin["id"], self.project_id, self.alice["id"]
        )
        with self.assertRaises(TeamError):
            self.service.read_draft(
                self.project_id, str(draft["draftId"]), str(self.alice["id"])
            )
        self.assertEqual(
            self.service.read_draft(
                self.project_id, str(draft["draftId"]), str(self.bob["id"])
            )["draftId"],
            draft["draftId"],
        )
        self.assertEqual(
            self.service.adopt_draft(
                self.project_id,
                str(draft["draftId"]),
                str(maintainer["id"]),
                "session-target-removed",
            )["status"],
            "adopted",
        )
        self.assertEqual(
            Path(str(target_workspace["path"]), "delivered.txt").read_text(encoding="utf-8"),
            "kept after author leaves\n",
        )
        self.assertEqual(
            self.service.integrate_draft(
                str(draft["draftId"]), str(maintainer["id"])
            )["status"],
            "integrated",
        )
        self.assertEqual(self.verifier_contexts[-1]["actorUserId"], str(maintainer["id"]))

    def test_contributor_cannot_integrate_and_personal_workspace_can_be_allocated(self) -> None:
        workspace = self.service.allocate_workspace(
            str(next(space for space in self.identity.list_spaces(self.admin["id"]) if space["kind"] == "personal")["id"]),
            "personal-session",
            owner_user_id=str(self.admin["id"]),
            session_id="personal-session",
        )
        self.assertEqual(set(("workspaceId", "path", "baseCommit")), set(workspace) & {"workspaceId", "path", "baseCommit"})

        contributor_workspace = self._allocate(self.bob, "session-contributor")
        Path(str(contributor_workspace["path"]), "contributor.txt").write_text("x\n", encoding="utf-8")
        draft = self.service.share_draft("session-contributor", str(self.bob["id"]), "x")
        with self.assertRaises(TeamError) as denied:
            self.service.integrate_draft(str(draft["draftId"]), str(self.bob["id"]))
        self.assertEqual((denied.exception.status, denied.exception.code), (403, "forbidden"))

    def test_adopt_fixed_draft_into_pristine_target_records_source_version(self) -> None:
        source_workspace = self._allocate(self.admin, "session-source")
        target_workspace = self._allocate(self.alice, "session-target")
        Path(str(source_workspace["path"]), "adopted.txt").write_text("from draft\n", encoding="utf-8")
        draft = self.service.share_draft("session-source", str(self.admin["id"]), "handoff")

        result = self.service.adopt_draft(
            self.project_id,
            str(draft["draftId"]),
            str(self.alice["id"]),
            "session-target",
        )
        self.assertEqual(result["status"], "adopted")
        self.assertEqual(
            Path(str(target_workspace["path"]), "adopted.txt").read_text(encoding="utf-8"),
            "from draft\n",
        )
        refreshed = self.service.allocate_workspace(
            self.project_id,
            "session-target",
            owner_user_id=str(self.alice["id"]),
            session_id="session-target",
        )
        self.assertEqual(refreshed["sourceDraftId"], draft["draftId"])
        self.assertEqual(refreshed["sourceDraftCommit"], draft["draftCommit"])
        self.assertEqual(
            self.service.adopt_draft(
                self.project_id,
                str(draft["draftId"]),
                str(self.alice["id"]),
                "session-target",
            ),
            result,
        )

    def test_adoption_rejects_uncommitted_target_without_overwriting_it(self) -> None:
        source_workspace = self._allocate(self.admin, "session-source")
        target_workspace = self._allocate(self.alice, "session-target")
        Path(str(source_workspace["path"]), "adopted.txt").write_text("from draft\n", encoding="utf-8")
        draft = self.service.share_draft("session-source", str(self.admin["id"]), "handoff")
        target_file = Path(str(target_workspace["path"]), "local.txt")
        target_file.write_text("keep me\n", encoding="utf-8")

        with self.assertRaises(TeamError) as dirty:
            self.service.adopt_draft(
                self.project_id,
                str(draft["draftId"]),
                str(self.alice["id"]),
                "session-target",
            )
        self.assertEqual((dirty.exception.status, dirty.exception.code), (409, "workspace_dirty"))
        self.assertEqual(target_file.read_text(encoding="utf-8"), "keep me\n")

    def test_adoption_conflict_leaves_target_and_replay_is_stable(self) -> None:
        source_workspace = self._allocate(self.admin, "session-source")
        Path(str(source_workspace["path"]), "README.md").write_text("from source\n", encoding="utf-8")
        draft = self.service.share_draft("session-source", str(self.admin["id"]), "conflicting handoff")
        # Advance the target branch on the trusted repository, then allocate a
        # pristine target from that newer baseline. The source draft's base is
        # its parent, so the same README edit produces a real three-way clash.
        repo = Path(str(self.project_record["repositoryPath"]))
        _git(repo, "config", "user.name", "fixture")
        _git(repo, "config", "user.email", "fixture@example.test")
        (repo / "README.md").write_text("target baseline\n", encoding="utf-8")
        _git(repo, "add", "--", "README.md")
        _git(repo, "commit", "-m", "target baseline")
        self.project_record = self.service.ensure_project(self.project_id)
        target_workspace = self._allocate(self.alice, "session-target")
        self.assertEqual(
            Path(str(target_workspace["path"]), "README.md").read_text(encoding="utf-8"),
            "target baseline\n",
        )
        result = self.service.adopt_draft(
            self.project_id,
            str(draft["draftId"]),
            str(self.alice["id"]),
            "session-target",
        )
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(
            Path(str(target_workspace["path"]), "README.md").read_text(encoding="utf-8"),
            "target baseline\n",
        )
        self.assertEqual(
            self.service.adopt_draft(
                self.project_id,
                str(draft["draftId"]),
                str(self.alice["id"]),
                "session-target",
            ),
            result,
        )

    def test_server_allocation_can_attach_final_agent_session_later(self) -> None:
        personal_id = str(
            next(
                space
                for space in self.identity.list_spaces(self.admin["id"])
                if space["kind"] == "personal"
            )["id"]
        )
        workspace = self.service.allocate_workspace(personal_id, "workspace-pending")
        self.assertEqual(workspace["sessionId"], "")
        attached = self.service.attach_session(
            personal_id,
            str(workspace["workspaceId"]),
            "agent:final-session",
            str(self.admin["id"]),
        )
        self.assertEqual(attached["sessionId"], "agent:final-session")
        self.assertEqual(
            self.grants.binding("agent:final-session")["workspacePath"], workspace["path"]
        )

    def test_target_branches_share_one_trusted_project_repository(self) -> None:
        feature = self.service.ensure_project(self.project_id, target_branch="feature/demo")
        self.assertEqual(feature["repositoryPath"], self.project_record["repositoryPath"])
        repo = Path(str(self.project_record["repositoryPath"]))
        self.assertEqual(_git(repo, "rev-parse", "refs/heads/feature/demo"), feature["headCommit"])


if __name__ == "__main__":
    unittest.main()
