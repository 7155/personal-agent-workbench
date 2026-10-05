from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from rag_ime.agent_background_jobs import (
    AgentBackgroundJobError,
    AgentBackgroundJobService,
)
from rag_ime.agent_delegation import (
    AgentDelegationCoordinator,
    AgentDelegationStore,
    _ActiveDelegatedRun,
)
from rag_ime.agent_execution_policy import (
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_TRUST_EXECUTION_MODE,
)
from rag_ime.agent_service import AgentService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_workspace import PreparedWorkspaceCommand, WorkspaceHarness
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.db.migration_runner import apply_database_migrations, load_migrations


class _Events:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, object], str]] = []

    def publish(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, object],
        *,
        turn_id: str = "",
    ) -> None:
        self.items.append((session_id, event_type, payload, turn_id))


class _Runtime:
    def __init__(self, callback=None, receipt=None, error: Exception | None = None) -> None:
        self.callback = callback
        self.receipt = receipt or {
            "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
            "turnId": "turn-provider-1",
            "pid": 999,
            "processHandle": "must-not-persist",
            "lifecycle": {"drained": True, "failedOperationIds": [], "pendingOperations": []},
        }
        self.error = error
        self.calls = 0

    def abort(self, session_id: str):
        self.calls += 1
        if self.callback is not None:
            self.callback(session_id)
        if self.error is not None:
            raise self.error
        return self.receipt


class _Jobs:
    def __init__(self, *, error: Exception | None = None, jobs=None, excluded=None) -> None:
        self.error = error
        self.jobs = list(jobs or [])
        self.excluded = list(excluded or [])
        self.calls = 0

    def cancel_causal(self, session_id: str, **payload):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {
            "schemaVersion": "rag-ime.agent-background-job-lifecycle-cancellation-summary.v1",
            "requestId": payload["request_id"],
            "sessionId": session_id,
            "scopeKind": payload["scope_kind"],
            "scopeId": payload["scope_id"],
            "sourceRevision": payload["source_revision"],
            "jobs": self.jobs,
            "excludedRoomBoundJobIds": self.excluded,
        }

    def list(self, _session_id: str, *, limit: int):
        del limit
        return {"items": []}


class _Delegation:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        state: str = "terminated",
    ) -> None:
        self.error = error
        self.state = state
        self.calls = 0

    def cancel_causal(self, session_id: str, **payload):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return {
            "schemaVersion": "rag-ime.agent-delegation-lifecycle-cancellation-summary.v1",
            "requestId": payload["request_id"],
            "parentSessionId": session_id,
            "scopeKind": payload["scope_kind"],
            "scopeId": payload["scope_id"],
            "sourceRevision": payload["source_revision"],
            "state": self.state,
            "pendingRunIds": (
                ["subagent-run:pending"]
                if self.state == "requested"
                else []
            ),
            "batches": [],
            "excludedRoomBoundBatchIds": [],
        }


class _RoomTurns:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def session_turn_active(self, _session_id: str) -> bool:
        return self.active


class _WorkDocuments:
    def observe_authority(self, _kind: str, _identifier: str) -> None:
        return None


class _Rooms:
    def room_ids_for_session(self, _session_id: str) -> list[str]:
        return []


def _service(
    store: AgentSessionStore,
    *,
    runtime: _Runtime,
    jobs: _Jobs,
    delegation: _Delegation | None = None,
    room_active: bool = False,
) -> AgentService:
    service = AgentService.__new__(AgentService)
    service.sessions = store
    service.runtime = runtime
    service.background_jobs = jobs
    service.delegation = delegation or _Delegation()
    service.events = _Events()
    service.room_turns = _RoomTurns(room_active)
    service.work_documents = _WorkDocuments()
    service.rooms = _Rooms()
    service._active_room_dispatch_authorizes_work = lambda _session_id: room_active
    return service


class AgentLifecycleCancellationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-lifecycle-cancel-")
        self.db_path = Path(self.tmp.name) / "agent.sqlite"
        self.store = AgentSessionStore(self.db_path)
        self.store.initialize()
        self.session = self.store.create(title="lifecycle")
        self.session_id = str(self.session["id"])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _execution_context(
        self,
        *,
        task: str = "run",
    ) -> tuple[dict[str, object], dict[str, object]]:
        todo = self.store.mutate_agent_todo(
            self.session_id,
            {"op": "init", "phase": "Execution", "items": [task]},
        )["todo"]
        todo = self.store.mutate_agent_todo(
            self.session_id,
            {"op": "start", "task": task},
        )["todo"]
        goal = self.store.mutate_agent_goal(
            self.session_id,
            {
                "action": "confirm_setup",
                "expectedRevision": 0,
                "confirmed": True,
                "objective": "finish",
            },
        )["workflow"]["goal"]
        return todo, goal

    def _latest_lifecycle_audit(
        self,
        *,
        action: str | None = None,
    ) -> dict[str, object]:
        audits = self.store.lifecycle_cancellation_audits(self.session_id, limit=100)
        matches = [
            audit
            for audit in audits
            if action is None or audit["action"] == action
        ]
        self.assertTrue(matches)
        return matches[0]

    def test_0123_corrects_0115_backfill_at_each_resource_causal_epoch(
        self,
    ) -> None:
        migrations_0114 = Path(self.tmp.name) / "migrations-0114"
        migrations_0114.mkdir()
        migrations_0122 = Path(self.tmp.name) / "migrations-0122"
        migrations_0122.mkdir()
        for migration in load_migrations():
            if migration.version <= 114:
                shutil.copy2(migration.path, migrations_0114 / migration.path.name)
            if migration.version <= 122:
                shutil.copy2(migration.path, migrations_0122 / migration.path.name)

        upgrade_db = Path(self.tmp.name) / "lifecycle-upgrade.sqlite"
        session_id = "session-lifecycle-upgrade"
        todo_id = f"todo:{session_id}"
        goal_id = "goal-created-later"
        legacy_items_table = "agent_" + "p" + "lan" + "_events"
        legacy_states_table = "agent_" + "p" + "lan" + "_state_events"
        legacy_causal_id = "causal_" + "p" + "lan" + "_id"
        legacy_causal_revision = "causal_" + "p" + "lan" + "_revision"
        legacy_scope_id = "pl" + "an:" + session_id
        digest = hashlib.sha256(b"echo migration").hexdigest()
        resources = (("before", 100), ("middle", 250), ("after", 450))
        job_ids = {
            "before": "bg_" + "1" * 32,
            "middle": "bg_" + "2" * 32,
            "after": "bg_" + "3" * 32,
        }
        with sqlite3.connect(upgrade_db) as conn:
            apply_database_migrations(conn, migrations_dir=migrations_0114)
            conn.execute(
                """
                INSERT INTO agent_sessions(
                    id, title, session_mode, role_id, role_version, model_profile,
                    tool_profile_version, created_at_ms, updated_at_ms,
                    last_opened_at_ms, status
                ) VALUES(
                    ?, 'Lifecycle upgrade', 'assistant', 'assistant', 'v1',
                    'pi/default', 'tool-profile-v1', 1, 1, 1, 'active'
                )
                """,
                (session_id,),
            )
            conn.execute(
                f"""
                INSERT INTO {legacy_items_table}(
                    event_id, session_id, sequence, item_id, title, status,
                    created_at_ms
                ) VALUES(
                    'todo-item-approved', ?, 1, 'step-1', 'Run',
                    'pending', 190
                )
                """,
                (session_id,),
            )
            conn.executemany(
                f"""
                INSERT INTO {legacy_states_table}(
                    event_id, session_id, sequence, title, status, actor,
                    created_at_ms
                ) VALUES(?, ?, ?, 'Lifecycle execution', ?, 'fixture', ?)
                """,
                (
                    ("execution-approved", session_id, 1, "approved", 200),
                    ("execution-later", session_id, 2, "executing", 300),
                ),
            )
            conn.executemany(
                """
                INSERT INTO agent_thread_goal_events(
                    event_id, session_id, goal_id, sequence, objective, status,
                    actor, created_at_ms
                ) VALUES(
                    ?, ?, ?, ?, 'Lifecycle goal', ?, 'fixture', ?
                )
                """,
                (
                    ("goal-active", session_id, goal_id, 1, "active", 400),
                    ("goal-paused-later", session_id, goal_id, 2, "paused", 500),
                ),
            )
            for resource_name, created_at_ms in resources:
                conn.execute(
                    """
                    INSERT INTO agent_approvals(
                        approval_id, session_id, tool_name, operation,
                        payload_sha256, preview_json, risk_level, state,
                        requested_at_ms, expires_at_ms
                    ) VALUES(
                        ?, ?, 'workspace_write', 'apply', ?, '{}', 'R2',
                        'pending', ?, 1000
                    )
                    """,
                    (
                        f"approval-{resource_name}",
                        session_id,
                        "a" * 64,
                        created_at_ms,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_background_jobs(
                        job_id, session_id, label, status, command,
                        command_sha256, cwd, network_allowed, max_run_seconds,
                        log_path, created_at_ms, updated_at_ms
                    ) VALUES(
                        ?, ?, ?, 'running', 'echo migration', ?, ?, 0, 60, ?,
                        ?, ?
                    )
                    """,
                    (
                        job_ids[resource_name],
                        session_id,
                        resource_name,
                        digest,
                        self.tmp.name,
                        str(Path(self.tmp.name) / f"job-{resource_name}.log"),
                        created_at_ms,
                        created_at_ms,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_subagent_batches(
                        id, parent_session_id, context_mode, state, depth,
                        max_depth, created_at_ms, updated_at_ms
                    ) VALUES(?, ?, 'fresh', 'running', 1, 2, ?, ?)
                    """,
                    (
                        f"batch-{resource_name}",
                        session_id,
                        created_at_ms,
                        created_at_ms,
                    ),
                )

            legacy_backfill = apply_database_migrations(
                conn,
                migrations_dir=migrations_0122,
            )
            self.assertIn(115, legacy_backfill.applied_versions)
            self.assertEqual(
                conn.execute(
                    f"""
                    SELECT {legacy_causal_id}, {legacy_causal_revision},
                           causal_goal_id, causal_goal_revision
                    FROM agent_approvals
                    WHERE approval_id = 'approval-before'
                    """
                ).fetchone(),
                (legacy_scope_id, 3, goal_id, 2),
            )
            self.assertEqual(
                conn.execute(
                    f"""
                    SELECT {legacy_causal_id}, {legacy_causal_revision},
                           causal_goal_id, causal_goal_revision
                    FROM agent_background_jobs
                    WHERE job_id = ?
                    """,
                    (job_ids["middle"],),
                ).fetchone(),
                ("", 0, "", 0),
            )

            corrected = apply_database_migrations(conn)
            replay = apply_database_migrations(conn)
            self.assertEqual(corrected.applied_versions[:4], (123, 124, 125, 126))
            self.assertEqual(replay.applied_versions, ())
            expected = {
                "before": ("", 0, "", 0),
                "middle": (todo_id, 1, "", 0),
                "after": (todo_id, 1, goal_id, 1),
            }
            for table, key, prefix in (
                ("agent_approvals", "approval_id", "approval-"),
                ("agent_background_jobs", "job_id", "bg_"),
                ("agent_subagent_batches", "id", "batch-"),
            ):
                for resource_name, _created_at_ms in resources:
                    with self.subTest(table=table, resource=resource_name):
                        identifier = (
                            job_ids[resource_name]
                            if table == "agent_background_jobs"
                            else (
                                f"batch-{resource_name}"
                                if table == "agent_subagent_batches"
                                else f"{prefix}{resource_name}"
                            )
                        )
                        row = conn.execute(
                            f"""
                            SELECT causal_todo_id, causal_todo_revision,
                                   causal_goal_id, causal_goal_revision
                            FROM {table}
                            WHERE {key} = ?
                            """,
                            (identifier,),
                        ).fetchone()
                        self.assertEqual(row, expected[resource_name])

        upgraded_store = AgentSessionStore(upgrade_db)
        with self.assertRaises(ValueError):
            upgraded_store.cancel_causal_approvals(
                session_id,
                request_id="migration-todo-approval-cancel",
                scope_kind="todo",
                scope_id=todo_id,
                source_revision=1,
                reason="Todo tracking does not cancel work",
                decided_at_ms=600,
            )
        goal_approvals = upgraded_store.cancel_causal_approvals(
            session_id,
            request_id="migration-goal-approval-cancel",
            scope_kind="goal",
            scope_id=goal_id,
            source_revision=1,
            reason="Goal cancelled",
            decided_at_ms=601,
        )
        self.assertEqual(
            goal_approvals["cancelledApprovalIds"],
            ["approval-after"],
        )

        jobs = AgentBackgroundJobService(
            upgrade_db,
            events=lambda *args, **kwargs: None,
            execution_owner=True,
        )
        goal_jobs = jobs.cancel_causal(
            session_id,
            request_id="migration-goal-job-cancel",
            scope_kind="goal",
            scope_id=goal_id,
            source_revision=1,
            reason="Goal cancelled",
        )
        jobs.close()
        self.assertEqual(
            [item["jobId"] for item in goal_jobs["jobs"]],
            [job_ids["after"]],
        )

        delegation = AgentDelegationStore(upgrade_db)
        goal_batches = delegation.request_causal_abort(
            request_id="migration-goal-batch-cancel",
            scope_kind="goal",
            scope_id=goal_id,
            source_revision=1,
            reason="Goal cancelled",
            requested_at_ms=603,
        )
        self.assertEqual(goal_batches["batchIds"], ["batch-after"])

        with sqlite3.connect(upgrade_db) as conn:
            self.assertEqual(
                dict(
                    conn.execute(
                        "SELECT approval_id, state FROM agent_approvals"
                    )
                ),
                {
                    "approval-before": "pending",
                    "approval-middle": "pending",
                    "approval-after": "stale",
                },
            )
            self.assertEqual(
                dict(
                    conn.execute(
                        "SELECT job_id, status FROM agent_background_jobs"
                    )
                ),
                {
                    job_ids["before"]: "running",
                    job_ids["middle"]: "running",
                    job_ids["after"]: "cancelling",
                },
            )
            self.assertEqual(
                dict(
                    conn.execute(
                        "SELECT id, state FROM agent_subagent_batches"
                    )
                ),
                {
                    "batch-before": "running",
                    "batch-middle": "running",
                    "batch-after": "aborted",
                },
            )

    def test_goal_cancel_closes_admission_before_provider_turn_and_approval_owners(self) -> None:
        todo, goal = self._execution_context()
        self.store.record_runtime_event(
            event_id="provider-started",
            session_id=self.session_id,
            turn_id="turn-provider-1",
            sequence=1,
            event_type="status_changed",
            created_at_ms=100,
        )
        approval = self.store.create_approval(
            session_id=self.session_id,
            tool_name="workspace_write",
            operation="apply",
            payload_sha256="a" * 64,
            preview={},
            risk_level="R2",
            requested_at_ms=101,
        )
        self.assertEqual(approval["causalMetadata"]["todoId"], todo["id"])
        self.assertEqual(
            approval["causalMetadata"]["todoRevision"],
            todo["revision"],
        )
        self.assertEqual(approval["causalMetadata"]["goalId"], goal["goalId"])
        self.assertEqual(
            approval["causalMetadata"]["goalRevision"],
            goal["revision"],
        )
        self.assertEqual(
            approval["causalMetadata"]["turnId"],
            "turn-provider-1",
        )

        def assert_store_transition_first(_session_id: str) -> None:
            self.assertEqual(
                self.store.agent_goal(self.session_id)["status"],
                "cancelled",
            )

        runtime = _Runtime(callback=assert_store_transition_first)
        service = _service(self.store, runtime=runtime, jobs=_Jobs())
        state = service.mutate_goal(
            self.session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "operator stopped the Goal",
            },
        )
        self.assertNotIn("lifecycleCancellationAudit", state)
        self.assertEqual(state["goal"]["status"], "cancelled")
        validate_contract(state, "agent-workflow-state.v1.json")

        audit = self._latest_lifecycle_audit()
        self.assertEqual(audit["scopeKind"], "goal")
        self.assertEqual(audit["sourceTurnId"], "turn-provider-1")
        self.assertEqual(audit["state"], "completed")
        self.assertEqual(
            audit["owners"]["delegation"]["status"],
            "succeeded",
        )
        self.assertEqual(self.store.get_approval(str(approval["approvalId"]))["state"], "stale")
        serialized = json.dumps(audit, sort_keys=True).casefold()
        self.assertNotIn('"pid"', serialized)
        self.assertNotIn("processhandle", serialized)
        self.assertEqual(service.events.items[-1][1], "workflow_changed")
        self.assertTrue(any(item[1] == "lifecycle_cancellation_changed" for item in service.events.items))

    def test_completed_retry_after_restart_replays_durable_owner_receipts(self) -> None:
        _todo, goal = self._execution_context()
        first_runtime = _Runtime()
        first_jobs = _Jobs()
        first = _service(self.store, runtime=first_runtime, jobs=first_jobs)
        first.mutate_goal(
            self.session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "operator stopped the Goal",
            },
        )
        initial = self._latest_lifecycle_audit()

        restarted_runtime = _Runtime(error=AssertionError("runtime owner replayed"))
        restarted_jobs = _Jobs(error=AssertionError("job owner replayed"))
        restarted = _service(
            AgentSessionStore(self.db_path),
            runtime=restarted_runtime,
            jobs=restarted_jobs,
        )
        restarted.mutate_goal(
            self.session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "operator stopped the Goal",
            },
        )
        replay = self._latest_lifecycle_audit()

        self.assertEqual(replay["requestId"], initial["requestId"])
        self.assertEqual(replay["owners"], initial["owners"])
        self.assertEqual(restarted_runtime.calls, 0)
        self.assertEqual(restarted_jobs.calls, 0)

    def test_partial_and_unknown_owner_outcomes_are_durable(self) -> None:
        _todo, goal = self._execution_context()
        runtime = _Runtime(
            receipt={
                "turnId": "turn-provider-1",
                "lifecycle": {
                    "drained": False,
                    "failedOperationIds": ["operation-1"],
                    "pendingOperations": [],
                },
            }
        )
        service = _service(
            self.store,
            runtime=runtime,
            jobs=_Jobs(error=RuntimeError("job gateway disconnected")),
        )
        service.mutate_goal(
            self.session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "operator stopped the Goal",
            },
        )
        audit = self._latest_lifecycle_audit()

        self.assertEqual(audit["state"], "partial")
        self.assertEqual(audit["owners"]["runtime"]["status"], "partial")
        self.assertEqual(audit["owners"]["job"]["status"], "unknown")
        durable = self.store.lifecycle_cancellation_audit(str(audit["requestId"]))
        self.assertEqual(durable, audit)

    def test_pending_delegation_is_a_durable_partial_owner_outcome(self) -> None:
        _todo, goal = self._execution_context()
        delegation = _Delegation(state="requested")
        service = _service(
            self.store,
            runtime=_Runtime(),
            jobs=_Jobs(),
            delegation=delegation,
        )
        service.mutate_goal(
            self.session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "operator stopped the Goal",
            },
        )
        audit = self._latest_lifecycle_audit()
        self.assertEqual(
            audit["owners"]["delegation"]["status"],
            "partial",
        )
        self.assertEqual(audit["state"], "partial")
        self.assertEqual(delegation.calls, 1)


    def test_goal_pause_and_cancel_share_coordinator_without_goal_audit_duplication(self) -> None:
        goal = self.store.mutate_agent_goal(
            self.session_id,
            {
                "action": "confirm_setup",
                "expectedRevision": 0,
                "confirmed": True,
                "objective": "finish",
            },
        )["workflow"]["goal"]
        service = _service(
            self.store,
            runtime=_Runtime(),
            jobs=_Jobs(excluded=["bg_room"]),
            room_active=True,
        )
        paused = service.mutate_goal(
            self.session_id,
            {"action": "pause", "expectedRevision": goal["revision"]},
        )
        pause_audit = self._latest_lifecycle_audit(action="pause")
        self.assertEqual(pause_audit["action"], "pause")
        self.assertEqual(pause_audit["owners"]["runtime"]["status"], "succeeded")
        self.assertEqual(
            pause_audit["owners"]["delegation"]["status"],
            "succeeded",
        )
        self.assertIsNone(paused["goal"]["cancellationAudit"])
        resumed = self.store.mutate_agent_goal(
            self.session_id,
            {"action": "resume", "expectedRevision": paused["goal"]["revision"]},
        )["workflow"]["goal"]
        cancelled = service.mutate_goal(
            self.session_id,
            {
                "action": "cancel",
                "expectedRevision": resumed["revision"],
                "reason": "operator stopped the Goal",
            },
        )
        cancel_audit = self._latest_lifecycle_audit(action="cancel")
        self.assertEqual(cancel_audit["action"], "cancel")
        self.assertIsNotNone(cancelled["goal"]["cancellationAudit"])
        self.assertNotEqual(
            cancelled["goal"]["cancellationAudit"]["auditId"],
            cancel_audit["requestId"],
        )

    def test_todo_tracking_never_authorizes_or_cancels_work(self) -> None:
        todo = self.store.mutate_agent_todo(
            self.session_id,
            {"op": "init", "phase": "Execution", "items": ["run"]},
        )["todo"]
        approval = self.store.create_approval(
            session_id=self.session_id,
            tool_name="workspace_write",
            operation="apply",
            payload_sha256="e" * 64,
            preview={},
            risk_level="R2",
            causal_metadata={
                "todoId": todo["id"],
                "todoRevision": todo["revision"],
            },
        )
        self.assertEqual(approval["state"], "pending")
        self.assertEqual(approval["causalMetadata"]["todoId"], todo["id"])
        self.assertEqual(
            approval["causalMetadata"]["todoRevision"],
            todo["revision"],
        )
        self.assertEqual(approval["causalMetadata"]["goalId"], "")
        advanced = self.store.mutate_agent_todo(
            self.session_id,
            {"op": "start", "task": "run"},
        )["todo"]
        self.assertEqual(advanced["revision"], todo["revision"] + 1)
        self.assertEqual(
            self.store.get_approval(str(approval["approvalId"]))["state"],
            "pending",
        )
        self.assertFalse(self.store.agent_goal(self.session_id)["configured"])
        self.assertEqual(
            self.store.lifecycle_cancellation_audits(self.session_id),
            [],
        )
        with self.assertRaises(ValueError):
            self.store.cancel_causal_approvals(
                self.session_id,
                request_id="todo-must-not-cancel-approval",
                scope_kind="todo",
                scope_id=str(todo["id"]),
                source_revision=int(todo["revision"]),
                reason="Todo tracking does not cancel work",
            )

        jobs = AgentBackgroundJobService(
            self.db_path,
            events=lambda *args, **kwargs: None,
            execution_owner=True,
        )
        with self.assertRaises(AgentBackgroundJobError):
            jobs.cancel_causal(
                self.session_id,
                request_id="todo-must-not-cancel-job",
                scope_kind="todo",
                scope_id=str(todo["id"]),
                source_revision=int(todo["revision"]),
                reason="Todo tracking does not cancel work",
            )
        jobs.close()

        delegation = AgentDelegationStore(self.db_path)
        with self.assertRaises(ValueError):
            delegation.request_causal_abort(
                request_id="todo-must-not-cancel-delegation",
                scope_kind="todo",
                scope_id=str(todo["id"]),
                source_revision=int(todo["revision"]),
                reason="Todo tracking does not cancel work",
            )

    def test_background_job_inherits_causal_metadata_from_approval(self) -> None:
        todo, goal = self._execution_context()
        approval = self.store.create_approval(
            session_id=self.session_id,
            tool_name="workspace_job",
            operation="start",
            payload_sha256="c" * 64,
            preview={},
            risk_level="R2",
        )
        approval = self.store.decide_approval(
            str(approval["approvalId"]),
            approved=True,
            payload_sha256=str(approval["payloadSha256"]),
        )

        class FailingHarness:
            def spawn_background(self, _prepared, **_kwargs):
                raise RuntimeError("launch deliberately stopped")

        jobs = AgentBackgroundJobService(
            self.db_path,
            events=lambda *args, **kwargs: None,
            workspace_harness=FailingHarness(),
            execution_owner=True,
        )
        prepared = PreparedWorkspaceCommand(
            command="echo causal",
            cwd=Path(self.tmp.name),
            roots=(Path(self.tmp.name),),
            timeout_seconds=60,
            allow_network=False,
        )
        with self.assertRaises(AgentBackgroundJobError):
            jobs.start(
                self.session_id,
                prepared,
                approval_id=str(approval["approvalId"]),
            )
        failed = jobs.list(self.session_id)["items"][0]
        self.assertEqual(failed["causalMetadata"]["todoId"], todo["id"])
        self.assertEqual(
            failed["causalMetadata"]["todoRevision"],
            todo["revision"],
        )
        self.assertEqual(failed["causalMetadata"]["goalId"], goal["goalId"])
        self.assertEqual(
            failed["causalMetadata"]["goalRevision"],
            goal["revision"],
        )
        jobs.close()

    def test_goal_cancellation_fences_start_blocked_after_process_spawn(self) -> None:
        # This race exercises real process ownership, independent of sandbox policy.
        self.session = self.store.create(
            title="full-trust lifecycle race",
            mode="coordinator",
            tool_profile_version=DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            execution_mode=FULL_TRUST_EXECUTION_MODE,
            workspace_roots=[str(Path(self.tmp.name))],
        )
        self.session_id = str(self.session["id"])
        _todo, goal = self._execution_context()
        approval = self.store.create_approval(
            session_id=self.session_id,
            tool_name="workspace_job",
            operation="start",
            payload_sha256="d" * 64,
            preview={},
            risk_level="R2",
        )
        approval = self.store.decide_approval(
            str(approval["approvalId"]),
            approved=True,
            payload_sha256=str(approval["payloadSha256"]),
        )

        class BlockingHarness(WorkspaceHarness):
            def __init__(self) -> None:
                super().__init__()
                self.spawned = threading.Event()
                self.release = threading.Event()
                self.launched = None

            def spawn_background(self, prepared, **kwargs):
                self.launched = super().spawn_background(prepared, **kwargs)
                self.spawned.set()
                if not self.release.wait(timeout=5):
                    raise TimeoutError("cancellation race release timed out")
                return self.launched

        harness = BlockingHarness()
        jobs = AgentBackgroundJobService(
            self.db_path,
            events=lambda *_args, **_kwargs: None,
            workspace_harness=harness,
        )
        prepared = harness.prepare_background_command(
            self.session,
            {
                "command": "python3 -c \"import time; time.sleep(30)\"",
                "cwd": str(Path(self.tmp.name)),
                "timeoutSeconds": 60,
            },
        )
        self.assertTrue(prepared.unrestricted)
        failures: list[BaseException] = []

        def start() -> None:
            try:
                jobs.start(
                    self.session_id,
                    prepared,
                    approval_id=str(approval["approvalId"]),
                )
            except BaseException as exc:
                failures.append(exc)

        thread = threading.Thread(target=start)
        thread.start()
        try:
            self.assertTrue(harness.spawned.wait(timeout=5))
            request = {
                "requestId": "lifecycle-race-goal-cancel",
                "scopeKind": "goal",
                "scopeId": str(goal["goalId"]),
                "sourceRevision": int(goal["revision"]),
                "action": "cancel",
                "reason": "race regression",
                "sourceTurnId": "",
            }
            self.store.mutate_agent_goal(
                self.session_id,
                {
                    "action": "cancel",
                    "expectedRevision": goal["revision"],
                    "reason": "race regression",
                },
                lifecycle_request=request,
            )
            jobs.cancel_causal(
                self.session_id,
                request_id=str(request["requestId"]),
                scope_kind="goal",
                scope_id=str(goal["goalId"]),
                source_revision=int(goal["revision"]),
                reason="race regression",
            )
        finally:
            harness.release.set()
            thread.join(timeout=5)
            jobs.close()

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], AgentBackgroundJobError)
        self.assertIn("causal lifecycle was cancelled", str(failures[0]))
        self.assertIsNotNone(harness.launched.process.poll())
        failed = jobs.list(self.session_id)["items"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertGreater(failed["endedAtMs"], 0)

    def test_background_job_cancellation_is_goal_causal_and_excludes_room_and_unrelated_jobs(self) -> None:
        todo, goal = self._execution_context()
        todo_id = str(todo["id"])
        goal_id = str(goal["goalId"])
        unrelated_goal_id = "goal:unrelated"
        digest = hashlib.sha256(b"echo test").hexdigest()
        rows = [
            ("bg_" + "1" * 32, todo_id, goal_id, 1, 0),
            ("bg_" + "2" * 32, todo_id, unrelated_goal_id, 1, 0),
            ("bg_" + "3" * 32, todo_id, goal_id, 1, 1),
        ]
        with sqlite3.connect(self.db_path) as conn:
            for index, (
                job_id,
                causal_todo_id,
                causal_goal_id,
                revision,
                room_bound,
            ) in enumerate(rows):
                conn.execute(
                    """
                    INSERT INTO agent_background_jobs(
                        job_id, session_id, label, status, command, command_sha256,
                        cwd, network_allowed, max_run_seconds, log_path,
                        causal_todo_id, causal_todo_revision,
                        causal_goal_id, causal_goal_revision, room_bound,
                        created_at_ms, updated_at_ms
                    ) VALUES (?, ?, ?, 'running', 'echo test', ?, ?, 0, 60, ?,
                              ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        self.session_id,
                        f"job-{index}",
                        digest,
                        self.tmp.name,
                        str(Path(self.tmp.name) / f"{job_id}.log"),
                        causal_todo_id,
                        todo["revision"],
                        causal_goal_id,
                        revision,
                        room_bound,
                        200 + index,
                        200 + index,
                    ),
                )
        jobs = AgentBackgroundJobService(
            self.db_path,
            events=lambda *args, **kwargs: None,
            execution_owner=True,
        )
        receipt = jobs.cancel_causal(
            self.session_id,
            request_id="lifecycle:test-job",
            scope_kind="goal",
            scope_id=goal_id,
            source_revision=int(goal["revision"]),
            reason="Goal cancelled",
        )

        self.assertEqual(receipt["excludedRoomBoundJobIds"], ["bg_" + "3" * 32])
        self.assertEqual([item["jobId"] for item in receipt["jobs"]], ["bg_" + "1" * 32])
        replay = jobs.cancel_causal(
            self.session_id,
            request_id="lifecycle:test-job",
            scope_kind="goal",
            scope_id=goal_id,
            source_revision=int(goal["revision"]),
            reason="Goal cancelled",
        )
        self.assertEqual(replay["jobs"], receipt["jobs"])
        with sqlite3.connect(self.db_path) as conn:
            states = dict(conn.execute("SELECT job_id, status FROM agent_background_jobs"))
        self.assertEqual(states["bg_" + "1" * 32], "cancelling")
        self.assertEqual(states["bg_" + "2" * 32], "running")
        self.assertEqual(states["bg_" + "3" * 32], "running")
        jobs.close()

    def test_delegation_owner_aborts_matching_running_children_and_replays_receipt(self) -> None:
        todo, goal = self._execution_context(task="run child")
        todo_id = str(todo["id"])
        goal_id = str(goal["goalId"])
        unrelated_goal_id = "goal:unrelated"
        delegation_store = AgentDelegationStore(self.db_path)
        delegation_store.initialize()

        def create_batch(*, causal_goal_id: str, room_bound: bool):
            child = self.store.create(
                title=f"child-{causal_goal_id}-{room_bound}",
                session_kind="subagent_runtime",
            )
            return delegation_store.create_batch(
                parent_session_id=self.session_id,
                parent_run_id="",
                context_mode="fresh",
                depth=1,
                max_depth=2,
                causal_metadata={
                    "todoId": todo_id,
                    "todoRevision": todo["revision"],
                    "goalId": causal_goal_id,
                    "goalRevision": goal["revision"],
                    "roomBound": room_bound,
                },
                runs=[
                    {
                        "childSessionId": child["id"],
                        "templateId": "worker",
                        "templateVersion": "1",
                        "task": "delegated lifecycle work",
                        "expectedOutput": "receipt",
                        "acceptanceCriteria": ["child is stopped"],
                        "outputSchema": {},
                        "todoTask": "run child",
                        "todoPhase": "Execution",
                        "maxTurns": 1,
                        "maxToolCalls": 1,
                        "maxTotalTokens": 256,
                        "maxDurationMs": 1_000,
                        "maxOutputChars": 256,
                    }
                ],
            )

        target = create_batch(causal_goal_id=goal_id, room_bound=False)
        unrelated = create_batch(
            causal_goal_id=unrelated_goal_id,
            room_bound=False,
        )
        room_bound = create_batch(causal_goal_id=goal_id, room_bound=True)
        self.assertEqual(target["causalMetadata"]["todoId"], todo_id)
        self.assertEqual(
            target["causalMetadata"]["todoRevision"],
            todo["revision"],
        )
        self.assertEqual(target["causalMetadata"]["goalId"], goal_id)
        self.assertEqual(target["runs"][0]["todoTask"], "run child")
        self.assertEqual(target["runs"][0]["todoPhase"], "Execution")
        target_run = delegation_store.start_run(
            str(target["runs"][0]["id"])
        )
        terminal = threading.Event()

        class DelegatedRuntime:
            def __init__(self) -> None:
                self.calls = 0

            def abort(self, _session_id: str) -> None:
                self.calls += 1
                delegation_store.finish_run(
                    str(target_run["id"]),
                    state="aborted",
                    error="Parent lifecycle cancelled",
                )
                terminal.set()
            def stop(self) -> None:
                return None

        runtime = DelegatedRuntime()
        active = _ActiveDelegatedRun(
            runtime=runtime,
            owns_runtime=True,
            child_session_id=str(target_run["childSessionId"]),
            terminal=terminal,
            forced=threading.Event(),
            lock=threading.RLock(),
        )
        coordinator = AgentDelegationCoordinator.__new__(
            AgentDelegationCoordinator
        )
        coordinator.store = delegation_store
        coordinator._lock = threading.RLock()
        coordinator._active_runs = {str(target_run["id"]): active}
        coordinator._cancellation_grace_ms = 50
        coordinator._schedule_pending_result_contexts = (
            lambda *, hydrate_artifacts: self.assertFalse(hydrate_artifacts)
        )

        receipt = coordinator.cancel_causal(
            self.session_id,
            request_id="lifecycle:test-delegation",
            scope_kind="goal",
            scope_id=goal_id,
            source_revision=int(goal["revision"]),
            reason="Goal cancelled",
        )
        self.assertEqual(receipt["state"], "terminated")
        self.assertEqual(runtime.calls, 1)
        self.assertEqual(
            delegation_store.get_run(str(target_run["id"]))["state"],
            "aborted",
        )
        self.assertEqual(
            delegation_store.get_batch(str(unrelated["id"]))["state"],
            "queued",
        )
        self.assertEqual(
            delegation_store.get_batch(str(room_bound["id"]))["state"],
            "queued",
        )
        self.assertEqual(
            receipt["excludedRoomBoundBatchIds"],
            [room_bound["id"]],
        )

        replay = coordinator.cancel_causal(
            self.session_id,
            request_id="lifecycle:test-delegation",
            scope_kind="goal",
            scope_id=goal_id,
            source_revision=int(goal["revision"]),
            reason="Goal cancelled",
        )
        self.assertEqual(replay["batches"], receipt["batches"])
        self.assertEqual(runtime.calls, 1)

    def test_reconnect_snapshot_projection_is_separate_from_goal_cancellation_audit(self) -> None:
        _todo, goal = self._execution_context()
        service = _service(self.store, runtime=_Runtime(), jobs=_Jobs())
        service.mutate_goal(
            self.session_id,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "operator stopped the Goal",
            },
        )
        audit = self._latest_lifecycle_audit()
        projection = self.store.lifecycle_cancellation_audits(self.session_id)
        self.assertEqual(projection[0]["requestId"], audit["requestId"])
        self.assertNotIn("cancellationAudit", projection[0])


if __name__ == "__main__":
    unittest.main()
