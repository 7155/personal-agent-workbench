from __future__ import annotations

import concurrent.futures
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from rag_ime.agent_background_jobs import (
    AgentBackgroundJobConflict,
    AgentBackgroundJobService,
)
from rag_ime.agent_execution_policy import (
    DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
    FULL_TRUST_EXECUTION_MODE,
)
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_workspace import WorkspaceHarness
from rag_ime.db import apply_database_migrations, sqlite_connection
from rag_ime.db.migration_runner import load_migrations


class AgentBackgroundJobIdempotencyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-ime-background-job-idempotency-"
        )
        self.root = Path(self.temporary.name) / "workspace"
        self.root.mkdir()
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.session = self.sessions.create(
            title="background job idempotency",
            mode="coordinator",
            tool_profile_version=DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            execution_mode=FULL_TRUST_EXECUTION_MODE,
            workspace_roots=[str(self.root)],
            created_at_ms=1,
        )
        self.harness = WorkspaceHarness()
        self.service = AgentBackgroundJobService(
            self.db_path,
            events=lambda *_args, **_kwargs: None,
            workspace_harness=self.harness,
        )
        self.service.initialize()

    def tearDown(self) -> None:
        self.service.close()
        self.temporary.cleanup()

    def _prepared(
        self,
        command: str,
        *,
        session: dict[str, object] | None = None,
        timeout_seconds: int = 10,
    ):
        return self.harness.prepare_background_command(
            session or self.session,
            {
                "command": command,
                "cwd": str(self.root),
                "timeoutSeconds": timeout_seconds,
            },
        )

    def _wait_for_terminal(
        self,
        job_id: str,
        *,
        session: dict[str, object] | None = None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            owner = session or self.session
            job = self.service.status(str(owner["id"]), job_id)["job"]
            if str(job["status"]) in {
                "completed",
                "failed",
                "cancelled",
                "orphaned",
            }:
                # Initial commands must succeed before replay/conflict checks;
                # a failed launcher must never count as successful execution.
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual(job["exitCode"], 0, job)
                return job
            time.sleep(0.02)
        self.fail(f"background job {job_id} did not become terminal")

    def test_concurrent_same_key_launches_one_real_fulltrust_process(self) -> None:
        marker = self.root / "same-key.marker"
        command = (
            "python3 -c "
            f"\"from pathlib import Path; import time; "
            f"Path({str(marker)!r}).open('a').write('x'); time.sleep(0.4)\""
        )
        prepared = self._prepared(command)
        self.assertTrue(prepared.unrestricted)

        def launch(_: int) -> dict[str, object]:
            return self.service.start(
                str(self.session["id"]),
                prepared,
                idempotency_key="same-key",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            receipts = list(pool.map(launch, range(4)))

        job_ids = {str(receipt["job"]["jobId"]) for receipt in receipts}
        self.assertEqual(len(job_ids), 1)
        self.assertEqual(sum(receipt.get("replayed") is True for receipt in receipts), 3)
        self._wait_for_terminal(next(iter(job_ids)))
        self.assertEqual(marker.read_text(encoding="utf-8"), "x")

    def test_same_key_different_execution_digest_is_a_conflict(self) -> None:
        first = self.service.start(
            str(self.session["id"]),
            self._prepared("printf first"),
            idempotency_key="digest-key",
        )
        self._wait_for_terminal(str(first["job"]["jobId"]))

        with self.assertRaises(AgentBackgroundJobConflict):
            self.service.start(
                str(self.session["id"]),
                self._prepared("printf second"),
                idempotency_key="digest-key",
            )

    def test_terminal_orphaned_replay_returns_existing_job_without_spawn(self) -> None:
        first = self.service.start(
            str(self.session["id"]),
            self._prepared("printf terminal"),
            idempotency_key="terminal-key",
        )
        job_id = str(first["job"]["jobId"])
        self._wait_for_terminal(job_id)

        with patch.object(self.harness, "spawn_background", side_effect=AssertionError):
            replay = self.service.start(
                str(self.session["id"]),
                self._prepared("printf terminal"),
                idempotency_key="terminal-key",
            )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["job"]["jobId"], job_id)

        with sqlite_connection(self.db_path, foreign_keys=True) as conn:
            conn.execute(
                "UPDATE agent_background_jobs SET status='orphaned', "
                "error='execution_outcome_unknown' WHERE job_id = ?",
                (job_id,),
            )
        with patch.object(self.harness, "spawn_background", side_effect=AssertionError):
            orphan_replay = self.service.start(
                str(self.session["id"]),
                self._prepared("printf terminal"),
                idempotency_key="terminal-key",
            )
        self.assertTrue(orphan_replay["replayed"])
        self.assertEqual(orphan_replay["job"]["status"], "orphaned")

    def test_distinct_keys_execute_distinct_tasks(self) -> None:
        one = self.root / "one.marker"
        two = self.root / "two.marker"
        first = self.service.start(
            str(self.session["id"]),
            self._prepared(f"printf one > {str(one)!r}"),
            idempotency_key="task-one",
        )
        second = self.service.start(
            str(self.session["id"]),
            self._prepared(f"printf two > {str(two)!r}"),
            idempotency_key="task-two",
        )
        first_id = str(first["job"]["jobId"])
        second_id = str(second["job"]["jobId"])
        self.assertNotEqual(first_id, second_id)
        self._wait_for_terminal(first_id)
        self._wait_for_terminal(second_id)
        self.assertEqual(one.read_text(encoding="utf-8"), "one")
        self.assertEqual(two.read_text(encoding="utf-8"), "two")

    def test_same_key_is_scoped_to_the_session(self) -> None:
        second_session = self.sessions.create(
            title="second background job session",
            mode="coordinator",
            tool_profile_version=DANGEROUS_AUTO_APPROVE_TOOL_PROFILE,
            execution_mode=FULL_TRUST_EXECUTION_MODE,
            workspace_roots=[str(self.root)],
            created_at_ms=2,
        )
        marker = self.root / "session-scoped.marker"
        command = f"printf x >> {str(marker)!r}"
        first = self.service.start(
            str(self.session["id"]),
            self._prepared(command),
            idempotency_key="session-scoped-key",
        )
        second = self.service.start(
            str(second_session["id"]),
            self._prepared(command, session=second_session),
            idempotency_key="session-scoped-key",
        )
        first_id = str(first["job"]["jobId"])
        second_id = str(second["job"]["jobId"])
        self.assertNotEqual(first_id, second_id)
        self._wait_for_terminal(first_id)
        self._wait_for_terminal(second_id, session=second_session)
        self.assertEqual(marker.read_text(encoding="utf-8"), "xx")


class AgentBackgroundJobIdentityMigrationTests(unittest.TestCase):
    def test_0192_preserves_legacy_blank_keys_and_enforces_session_unique_keys(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-background-job-migration-") as temporary:
            migrations_0191 = Path(temporary) / "migrations-0191"
            migrations_0191.mkdir()
            for migration in load_migrations():
                if migration.version <= 191:
                    shutil.copy2(migration.path, migrations_0191 / migration.path.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                initial = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0191,
                    applied_at_ms=191,
                )
                self.assertEqual(initial.current_version, 191)
                conn.execute(
                    """
                    INSERT INTO agent_sessions(
                        id, title, session_mode, role_id, role_version, model_profile,
                        tool_profile_version, created_at_ms, updated_at_ms,
                        last_opened_at_ms, status
                    ) VALUES (
                        'session:migration', 'Migration Session', 'coordinator',
                        'coordinator', '1', 'test/model', 'test-tools',
                        1, 1, 1, 'idle'
                    )
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO agent_background_jobs(
                        job_id, session_id, label, status, command, command_sha256,
                        cwd, max_run_seconds, log_path, created_at_ms, updated_at_ms
                    ) VALUES (?, 'session:migration', ?, 'completed', ?, ?, ?, 60, ?, 1, 1)
                    """,
                    (
                        (
                            "legacy-job-1", "legacy one", "printf one", "a" * 64,
                            "/tmp", "/tmp/legacy-one.log",
                        ),
                        (
                            "legacy-job-2", "legacy two", "printf two", "b" * 64,
                            "/tmp", "/tmp/legacy-two.log",
                        ),
                    ),
                )
                conn.commit()

                for migration in load_migrations():
                    if migration.version == 192:
                        shutil.copy2(migration.path, migrations_0191 / migration.path.name)
                upgraded = apply_database_migrations(
                    conn, migrations_dir=migrations_0191, applied_at_ms=192,
                )

                self.assertEqual(upgraded.applied_versions, (192,))
                self.assertEqual(upgraded.current_version, 192)
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT job_id, idempotency_key, idempotency_digest
                        FROM agent_background_jobs
                        WHERE session_id = 'session:migration'
                        ORDER BY job_id
                        """
                    ).fetchall(),
                    [("legacy-job-1", "", ""), ("legacy-job-2", "", "")],
                )
                conn.execute(
                    """
                    INSERT INTO agent_background_jobs(
                        job_id, session_id, label, status, command, command_sha256,
                        cwd, max_run_seconds, log_path, idempotency_key,
                        idempotency_digest, created_at_ms, updated_at_ms
                    ) VALUES (
                        'explicit-job-1', 'session:migration', 'explicit', 'completed',
                        'printf explicit', ?, '/tmp', 60, '/tmp/explicit.log',
                        'explicit-key', ?, 2, 2
                    )
                    """,
                    ("c" * 64, "d" * 64),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO agent_background_jobs(
                            job_id, session_id, label, status, command, command_sha256,
                            cwd, max_run_seconds, log_path, idempotency_key,
                            idempotency_digest, created_at_ms, updated_at_ms
                        ) VALUES (
                            'explicit-job-2', 'session:migration', 'duplicate', 'completed',
                            'printf duplicate', ?, '/tmp', 60, '/tmp/duplicate.log',
                            'explicit-key', ?, 3, 3
                        )
                        """,
                        ("e" * 64, "f" * 64),
                    )


if __name__ == "__main__":
    unittest.main()
