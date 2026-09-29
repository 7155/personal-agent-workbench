"""A Jev verifier must review the bytes of the fixed workspace artifact."""

import tempfile
from dataclasses import asdict
from pathlib import Path
from threading import Event, Thread, current_thread
from unittest.mock import patch

from rag_ime.jev_tasks.candidates import build_candidates
from rag_ime.jev_tasks.types import GraphConflict, digest
from tests import test_jev_failure_lifecycle as failure
from tests import test_jev_host_application as host


class JevArtifactRevisionGuardTests(host.JevHostFixture):
    effects = failure.JevFailureLifecycleTests.effects
    active_effect = failure.JevFailureLifecycleTests.active_effect
    submit = failure.JevFailureLifecycleTests.submit
    finish = failure.JevFailureLifecycleTests.finish
    verdict = staticmethod(failure.JevFailureLifecycleTests.verdict)

    def setUp(self):
        super().setUp()
        self.gateway = failure.ControlToolGateway(
            sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service,
            background_jobs=self.service.background_jobs, delegation=self.service.delegation,
            work_documents=self.service.work_documents,
        )
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.app.driver.controller.decider = failure.JevChoices(failure.choose_valid_progress)
        self.terminals = {}
        original = self.app.execution_terminal
        terminal = patch.object(self.app, "execution_terminal", side_effect=lambda effect, **kwargs:
            self.terminals.get(effect["effectId"]) or original(effect, **kwargs))
        terminal.start()
        self.addCleanup(terminal.stop)
        for session in self.sessions:
            self.service.sessions.set_runtime_policy(
                session["id"], mode="coordinator",
                tool_profile_version="control-center-v1",
                execution_mode="workspace_managed",
                workspace_roots=[self.tmp.name], allowed_tools=None,
            )
        self.artifact = Path(self.tmp.name) / "result.md"
        self.artifact.write_text("first result\n", encoding="utf-8")

    def fixed_result(self, ref=None):
        created = self.create()
        worker = self.active_effect(created, "execute")
        self.submit(worker, "result_submit", {
            "resultSummary": "Saved result",
            "artifactRefs": [ref if ref is not None else "workspace:" + str(self.artifact)],
            "evidenceRefs": ["test:worker-readback"],
        })
        self.finish(worker)
        return created, worker

    def test_unchanged_workspace_artifact_has_fixed_revision_and_can_be_accepted(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        revisions = verifier["request"]["artifactRevisions"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["sourceRef"], "workspace:" + str(self.artifact))
        self.assertEqual(revisions[0]["status"], "available")
        self.assertTrue(revisions[0]["revision"].startswith("sha256:"))
        self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        for _ in range(4):
            self.app.tick(limit=16)
        self.assertEqual(self.snapshot(created).task(worker["request"]["taskId"]).state, "done")

    def test_changed_bytes_reject_old_verifier_and_prepare_new_subject(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.artifact.write_text("second result\n", encoding="utf-8")
        with self.assertRaises(GraphConflict):
            self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        replacement = self.active_effect(created, "verify")
        self.assertNotEqual(replacement["request"]["subjectHash"], verifier["request"]["subjectHash"])
        self.assertEqual(self.snapshot(created).task(worker["request"]["taskId"]).state, "review")

    def test_relative_docs_result_path_is_bound_and_reverified_after_mutation(self):
        self.artifact = Path(self.tmp.name) / "docs" / "result.md"
        self.artifact.parent.mkdir()
        self.artifact.write_text("first result\n", encoding="utf-8")
        created, worker = self.fixed_result("docs/result.md")
        verifier = self.active_effect(created, "verify")
        self.assertEqual(verifier["request"]["artifactRevisions"][0]["status"], "available")
        self.artifact.write_text("changed result\n", encoding="utf-8")
        with self.assertRaises(GraphConflict):
            self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        replacement = self.active_effect(created, "verify")
        self.assertNotEqual(replacement["request"]["subjectHash"], verifier["request"]["subjectHash"])
        self.assertEqual(self.snapshot(created).task(worker["request"]["taskId"]).state, "review")

    def test_absolute_workspace_path_is_bound(self):
        created, _worker = self.fixed_result(str(self.artifact))
        verifier = self.active_effect(created, "verify")
        self.assertEqual(verifier["request"]["artifactRevisions"][0]["status"], "available")

    def test_opaque_reference_does_not_create_a_file_read_requirement(self):
        created, _worker = self.fixed_result("fixture:artifact@1")
        verifier = self.active_effect(created, "verify")
        self.assertEqual(verifier["request"]["artifactRevisions"], [])
        self.submit(verifier, "verification_submit", self.verdict())

    def test_file_read_tool_revocation_hides_prior_positive_verdict(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        self.service.sessions.set_runtime_policy(
            verifier["request"]["sessionId"], mode="coordinator",
            tool_profile_version="control-center-v1", execution_mode="workspace_managed",
            workspace_roots=[self.tmp.name], allowed_tools=["room_partner"],
        )
        self.assertNotIn(worker["request"]["taskId"], self.app.lifecycle.verifications(self.snapshot(created)))

    def test_absolute_file_outside_session_roots_is_unavailable(self):
        outside = tempfile.TemporaryDirectory(prefix="paw-jev-outside-")
        self.addCleanup(outside.cleanup)
        external = Path(outside.name) / "private.md"
        external.write_text("not a session artifact\n", encoding="utf-8")
        created, _worker = self.fixed_result(str(external))
        verifier = self.active_effect(created, "verify")
        self.assertEqual(verifier["request"]["artifactRevisions"], [
            {"sourceRef": str(external), "status": "unavailable"},
        ])
        with self.assertRaises(GraphConflict):
            self.submit(verifier, "verification_submit", self.verdict())

    def test_changed_bytes_after_submission_do_not_settle_or_accept_old_verdict(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.submit(verifier, "verification_submit", self.verdict())
        self.artifact.write_text("second result\n", encoding="utf-8")
        self.finish(verifier)
        self.assertNotIn(worker["request"]["taskId"], self.app.lifecycle.verifications(self.snapshot(created)))
        replacement = self.active_effect(created, "verify")
        self.assertNotEqual(replacement["request"]["subjectHash"], verifier["request"]["subjectHash"])

    def test_revoked_workspace_read_cannot_reuse_passed_verdict(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        other = Path(self.tmp.name) / "other"
        other.mkdir()
        self.service.sessions.set_runtime_policy(
            verifier["request"]["sessionId"], mode="coordinator",
            tool_profile_version="control-center-v1", execution_mode="workspace_managed",
            workspace_roots=[str(other)], allowed_tools=None,
        )
        self.assertNotIn(worker["request"]["taskId"], self.app.lifecycle.verifications(self.snapshot(created)))

    def test_changed_bytes_after_settlement_cannot_be_accepted(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        snapshot = self.snapshot(created)
        observation = self.app.observe(snapshot, None)
        candidates = build_candidates(
            snapshot, event_id="artifact-race-test", executions=observation.executions,
            executors=observation.executors, eligible_pairs=observation.eligible_pairs,
            manifests=observation.manifests, verifications=observation.verifications,
        ).actions
        accept = next(action for action in candidates if action.operation == "accept")
        self.artifact.write_text("changed after verdict\n", encoding="utf-8")
        with self.assertRaises(GraphConflict):
            self.app.owner.apply(snapshot, accept, command_id="artifact-race-test")
        self.assertEqual(self.snapshot(created).task(worker["request"]["taskId"]).state, "review")
        replacement = self.active_effect(created, "verify")
        self.assertNotEqual(replacement["request"]["subjectHash"], verifier["request"]["subjectHash"])

    def test_missing_declared_file_cannot_submit_a_positive_verdict(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.artifact.unlink()
        with self.assertRaises(GraphConflict):
            self.submit(verifier, "verification_submit", self.verdict())
        self.assertEqual(self.snapshot(created).task(worker["request"]["taskId"]).state, "review")

    def test_manual_accept_preflight_allows_stop_and_epoch_fences_old_action(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        task = self.snapshot(created).task(worker["request"]["taskId"])
        original = self.app.owner.artifact_preflight
        entered, release, stopped = Event(), Event(), Event()
        accept_errors, stop_errors = [], []

        def pause_between_preflight_and_apply(snapshot, candidate):
            original(snapshot, candidate)
            entered.set()
            if not release.wait(10):
                raise TimeoutError("preflight pause was not released")

        def accept():
            try:
                self.app.command(self.room["id"], {
                    "action": "accept", "graphId": created["graphId"],
                    "clientMessageId": "manual-old-artifact-accept", "taskId": task.id,
                    "taskHash": digest(asdict(task)), "reason": "Observed current bytes",
                    "evidenceRefs": ["test:verification-passed"],
                    "operabilityVerdict": "passed", "requirementVerdict": "satisfied",
                })
            except Exception as exc:
                accept_errors.append(exc)

        def stop():
            try:
                self.app.stop(self.room["id"], created["rootId"])
                stopped.set()
            except Exception as exc:
                stop_errors.append(exc)

        accept_thread = Thread(target=accept, daemon=True)
        stop_thread = Thread(target=stop, daemon=True)
        with patch.object(self.app.owner, "artifact_preflight", side_effect=pause_between_preflight_and_apply):
            try:
                accept_thread.start()
                self.assertTrue(entered.wait(10), "manual accept did not enter preflight")
                stop_thread.start()
                self.assertTrue(stopped.wait(10), "Stop was blocked by the accept preflight")
            finally:
                release.set()
                if stop_thread.ident is not None:
                    stop_thread.join(10)
                accept_thread.join(10)
        self.assertFalse(accept_thread.is_alive())
        self.assertFalse(stop_thread.is_alive())
        self.assertFalse(stop_errors)
        self.assertEqual(len(accept_errors), 1)
        self.assertIsInstance(accept_errors[0], GraphConflict)
        self.assertNotEqual(self.snapshot(created).task(task.id).state, "done")

    def test_identical_accept_committed_during_stale_preflight_replays_receipt(self):
        created, worker = self.fixed_result()
        verifier = self.active_effect(created, "verify")
        self.submit(verifier, "verification_submit", self.verdict())
        self.finish(verifier)
        task = self.snapshot(created).task(worker["request"]["taskId"])
        payload = {
            "action": "accept", "graphId": created["graphId"],
            "clientMessageId": "same-artifact-accept", "taskId": task.id,
            "taskHash": digest(asdict(task)), "reason": "Observed current bytes",
            "evidenceRefs": ["test:verification-passed"],
            "operabilityVerdict": "passed", "requirementVerdict": "satisfied",
        }
        original = self.app.owner.artifact_preflight
        second_entered, release_second, first_done, stale_read_seen = Event(), Event(), Event(), Event()
        results, errors = {}, {}

        def controlled_preflight(snapshot, candidate):
            if current_thread().name == "second-review":
                second_entered.set()
                if not release_second.wait(10):
                    raise TimeoutError("second preflight was not released")
                try:
                    return original(snapshot, candidate)
                except GraphConflict:
                    stale_read_seen.set()
                    raise
            return original(snapshot, candidate)

        def review(label):
            try:
                results[label] = self.app.command(self.room["id"], payload)
            except Exception as exc:
                errors[label] = exc
            finally:
                if label == "first":
                    first_done.set()

        second = Thread(target=lambda: review("second"), name="second-review", daemon=True)
        first = Thread(target=lambda: review("first"), name="first-review", daemon=True)
        with patch.object(self.app.owner, "artifact_preflight", side_effect=controlled_preflight):
            try:
                second.start()
                self.assertTrue(second_entered.wait(10), "second accept did not enter preflight")
                first.start()
                self.assertTrue(first_done.wait(10), "first accept was blocked by second preflight")
                self.assertNotIn("first", errors)
                self.assertFalse(results["first"]["replayed"])
                self.artifact.write_text("changed after first accept\n", encoding="utf-8")
            finally:
                release_second.set()
                if first.ident is not None:
                    first.join(10)
                second.join(10)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(stale_read_seen.is_set(), "second preflight did not reject stale bytes")
        self.assertFalse(errors)
        self.assertEqual(results["second"], {**results["first"], "replayed": True})
        with self.app.ledger.connection() as conn:
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM agent_jev_commands WHERE command_id=?",
                (payload["clientMessageId"],),
            ).fetchone()[0], 1)
