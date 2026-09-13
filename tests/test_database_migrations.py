from __future__ import annotations

import hashlib
import json
import sqlite3
import shutil
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from rag_ime.agent_command_receipts import AgentCommandReceiptStore
from rag_ime.db.migration_runner import (
    DEFAULT_MIGRATIONS_DIR,
    MigrationChecksumError,
    apply_database_migrations,
    load_migrations,
    migration_status,
)

POST_0126_MIGRATIONS = tuple(range(127, 203))


class DatabaseMigrationTests(unittest.TestCase):
    def test_empty_database_applies_all_migrations_idempotently(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            first = apply_database_migrations(conn, applied_at_ms=123)
            second = apply_database_migrations(conn, applied_at_ms=456)
            status = migration_status(conn)

            self.assertEqual(
                first.applied_versions,
                (
                    1, 2, 3, 4, 5, 6, 7, 8, 9, 10,
                    11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                    21, 22, 23, 24, 25, 26, 27, 28, 29, 30,
                    31, 32, 33, 34, 35, 36, 37, 38, 39, 40,
                    41, 42, 43, 44, 45, 46, 47,
                    51, 52, 53, 54, 55, 56, 57, 58, 59, 60,
                    61, 62, 63, 64, 65, 66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, *POST_0126_MIGRATIONS,
                ),
            )
            self.assertEqual(second.applied_versions, ())
            self.assertEqual(status["currentVersion"], 202)
            self.assertEqual(status["pendingVersions"], [])
            self.assertTrue(status["ok"])
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("schema_migrations", tables)
            self.assertIn("memory_feedback_events", tables)
            self.assertIn("memory_compile_state", tables)
            self.assertIn("runtime_config_state", tables)
            self.assertIn("management_settings", tables)
            self.assertIn("input_events", tables)
            self.assertIn("collaboration_profile_versions", tables)
            self.assertIn("collaboration_profile_active_pointers", tables)
            self.assertIn("room_workspace_bindings", tables)
            self.assertIn("room_workspace_events", tables)
            self.assertIn("room_workspace_integration_leases", tables)
            self.assertIn("room_v2_prompt_compile_receipts", tables)
            self.assertIn("room_v2_prompt_compare_diffs", tables)
            self.assertIn("room_v2_session_context_epochs", tables)
            self.assertIn("room_v2_session_context_epoch_transitions", tables)
            self.assertIn("room_v2_capability_manifests", tables)
            self.assertIn("room_v2_tool_disclosure_receipts", tables)
            self.assertIn("room_v2_tool_invocation_receipts", tables)
            self.assertIn("room_kernel_settle_attempt_receipts", tables)
            self.assertIn("room_v2_requirement_anchors", tables)
            self.assertIn("room_v2_requirement_catalog_revisions", tables)
            self.assertIn("room_v2_verification_receipts", tables)
            self.assertIn("room_v2_delivery_gate_receipts", tables)
            self.assertIn("agent_recent_message_projections", tables)
            self.assertIn("agent_lab_experiment_revisions", tables)
            self.assertIn("room_v2_peer_judgment_rounds", tables)
            self.assertIn("room_v2_peer_judgments", tables)
            self.assertIn("room_v2_conflict_matrix_revisions", tables)
            self.assertIn("room_v2_delivery_gate_preview_receipts", tables)
            self.assertIn("room_v2_incidents", tables)
            self.assertIn("room_v2_lesson_candidates", tables)
            self.assertIn("room_v2_guard_candidates", tables)
            self.assertIn("room_v2_guard_activation_receipts", tables)
            self.assertIn("room_v2_promotion_candidates", tables)
            self.assertIn("room_v2_knowledge_claim_versions", tables)
            self.assertIn("room_v2_knowledge_retrieval_receipts", tables)
            self.assertIn("room_v2_external_import_intakes", tables)
            self.assertIn("room_v2_reflection_dead_letters", tables)
            self.assertIn("room_v2_guard_materialization_receipts", tables)
            self.assertIn("room_v2_knowledge_eval_fixture_datasets", tables)
            self.assertIn("room_v2_knowledge_search_use_eval_runs", tables)
            self.assertIn("memory_items", tables)
            self.assertIn("memory_books", tables)
            self.assertIn("memory_group_overrides", tables)
            self.assertIn("memory_tag_profiles", tables)
            self.assertIn("memory_semantic_groups", tables)
            self.assertIn("memory_semantic_group_members", tables)
            self.assertIn("planning_daily", tables)
            self.assertIn("planning_assistant_messages", tables)
            self.assertIn("memory_supersessions", tables)
            self.assertIn("agent_sessions", tables)
            self.assertIn("agent_approvals", tables)
            self.assertIn("agent_approval_terminal_projections", tables)
            terminal_projection_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(agent_approval_terminal_projections)"
                )
            }
            self.assertTrue(
                {
                    "session_id",
                    "approval_id",
                    "projection_kind",
                    "tool_call_id",
                    "turn_id",
                    "tool_name",
                    "room_id",
                    "room_root_id",
                    "room_dispatch_id",
                    "room_generation",
                    "event_id",
                    "event_sequence",
                    "projected_at_ms",
                }.issubset(terminal_projection_columns)
            )
            projection_unique_indexes = [
                str(row[1])
                for row in conn.execute(
                    "PRAGMA index_list(agent_approval_terminal_projections)"
                )
                if int(row[2]) == 1
            ]
            self.assertIn(
                ["session_id", "approval_id", "projection_kind"],
                [
                    [
                        str(column[2])
                        for column in conn.execute(
                            f"PRAGMA index_info({index_name})"
                        )
                    ]
                    for index_name in projection_unique_indexes
                ],
            )
            self.assertEqual(
                {
                    (str(row[2]), str(row[6]).upper())
                    for row in conn.execute(
                        "PRAGMA foreign_key_list("
                        "agent_approval_terminal_projections)"
                    )
                },
                {
                    ("agent_sessions", "CASCADE"),
                    ("agent_approvals", "CASCADE"),
                },
            )
            self.assertIn("agent_runtime_events", tables)
            self.assertIn("agent_plugin_usage_events", tables)
            self.assertIn("agent_memory_sources", tables)
            self.assertIn("memory_source_disposition_events", tables)
            self.assertIn("memory_curation_cursors", tables)
            self.assertIn("memory_evidence_historical_promotion_receipts", tables)
            self.assertIn("agent_media", tables)
            self.assertIn("agent_message_media", tables)
            self.assertIn("agent_rooms", tables)
            self.assertIn("agent_room_participants", tables)
            self.assertIn("agent_room_events", tables)
            self.assertIn("agent_room_start_gates", tables)
            self.assertIn("memory_catalog_consolidation_receipts", tables)
            catalog_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(memory_catalog_consolidation_receipts)"
                )
            }
            self.assertTrue(
                {
                    "receipt_sequence",
                    "receipt_id",
                    "project",
                    "state",
                    "last_attempt_at_ms",
                    "last_completion_at_ms",
                    "next_due_at_ms",
                    "catalog_digest",
                    "curation_run_id",
                    "attempt_count",
                    "retry_count",
                    "result_json",
                    "error",
                    "created_at_ms",
                    "updated_at_ms",
                }.issubset(catalog_columns)
            )
            self.assertIn("trace_repair_evidence", tables)
            self.assertIn("trace_repair_receipts", tables)
            self.assertIn("agent_room_public_projection_receipts", tables)
            self.assertIn("agent_room_topics", tables)
            self.assertIn("agent_room_artifacts", tables)
            self.assertIn("agent_subagent_batches", tables)
            self.assertIn("agent_subagent_runs", tables)
            self.assertIn("agent_subagent_controls", tables)
            self.assertIn("agent_subagent_inbox", tables)
            self.assertIn("agent_subagent_events", tables)
            self.assertIn("agent_runtime_bindings", tables)
            self.assertIn("agent_configuration_state", tables)
            self.assertIn("agent_control_events", tables)
            self.assertIn("agent_artifacts", tables)
            self.assertIn("agent_artifact_snapshots", tables)
            self.assertIn("agent_room_intercom_messages", tables)
            self.assertIn("management_work_previews", tables)
            self.assertIn("management_work_receipts", tables)
            self.assertIn("agent_personas", tables)
            self.assertIn("agent_role_runtime_preferences", tables)
            self.assertIn("agent_session_tool_policies", tables)
            self.assertIn("agent_todo_events", tables)
            self.assertNotIn("agent_plan_events", tables)
            self.assertNotIn("agent_plan_state_events", tables)
            self.assertIn("agent_thread_goal_events", tables)
            self.assertIn("agent_goal_completion_audits", tables)
            self.assertIn("agent_goal_usage_receipts", tables)
            self.assertIn("agent_goal_continuation_budgets", tables)
            self.assertIn("agent_goal_continuation_receipts", tables)
            self.assertIn("agent_goal_cancellation_audits", tables)
            self.assertIn("agent_background_jobs", tables)
            background_job_columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(agent_background_jobs)")
            }
            self.assertTrue(
                {
                    "raw_output_path",
                    "exit_status_path",
                    "temporary_path",
                    "raw_output_cursor",
                }.issubset(background_job_columns)
            )
            self.assertIn("work_documents", tables)
            self.assertIn("work_document_terminal_receipts", tables)
            self.assertIn("work_document_outbox", tables)
            self.assertIn("work_document_observer_failures", tables)
            self.assertIn("work_document_operation_receipts", tables)
            self.assertIn("agent_lifecycle_cancellation_audits", tables)
            self.assertIn("agent_context_traces", tables)
            self.assertIn("agent_context_trace_nodes", tables)
            self.assertIn("agent_context_items", tables)
            self.assertIn("agent_wake_schedules", tables)
            self.assertIn("agent_wake_runs", tables)
            self.assertIn("agent_observation_events", tables)
            self.assertIn("agent_command_receipts", tables)
            self.assertIn("agent_room_work_items", tables)
            self.assertIn("agent_room_work_events", tables)
            self.assertIn("agent_room_partner_dispatches", tables)
            self.assertIn("agent_room_delivery_cursors", tables)
            self.assertIn("agent_role_books", tables)
            self.assertIn("daily_activity_timelines", tables)
            self.assertIn("personal_context_draft_decisions", tables)
            self.assertIn("agent_role_book_revisions", tables)
            self.assertIn("agent_role_book_activation_events", tables)
            self.assertIn("agent_memory_evidence", tables)
            self.assertIn("personal_context_consolidation_runs", tables)
            self.assertIn("personal_context_consolidation_cursors", tables)
            self.assertIn("memory_governance_proposals", tables)
            self.assertIn("memory_atom_evidence_links", tables)
            self.assertIn("memory_legacy_atom_migration_audit", tables)
            self.assertIn("agent_lifecycle_hook_policies", tables)
            self.assertIn("agent_lifecycle_hook_events", tables)
            self.assertIn("room_v2_shadow_observations", tables)
            self.assertIn("room_v2_shadow_legacy_refs", tables)
            self.assertIn("room_v2_shadow_quarantine", tables)
            self.assertIn("room_v2_context_entries", tables)
            self.assertIn("room_v2_posts", tables)
            self.assertIn("room_v2_provider_projection_journals", tables)
            self.assertIn("room_v2_provider_projection_items", tables)
            self.assertIn("room_v2_provider_projection_receipts", tables)
            self.assertIn("agent_message_block_sidecars", tables)
            self.assertIn("agent_block_message_envelopes", tables)
            room_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agent_rooms)")
            }
            participant_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(agent_room_participants)")
            }
            self.assertIn("workspace_roots_json", room_columns)
            self.assertIn("routing_mode", room_columns)
            self.assertIn("active_topic_id", room_columns)
            self.assertIn("collaboration_role", participant_columns)
            start_gate_columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(agent_room_start_gates)")
            }
            self.assertTrue(
                {"attachment_ids_json", "retry_of_root_id"}.issubset(
                    start_gate_columns
                )
            )
            room_event_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_room_events'"
            ).fetchone()[0]
            self.assertIn("'room_start_confirmation_required'", room_event_sql)
            self.assertIn("'room_start_confirmation_confirmed'", room_event_sql)
            self.assertIn("'room_start_confirmation_rejected'", room_event_sql)
            partner_dispatch_columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(agent_room_partner_dispatches)"
                )
            }
            self.assertIn(
                "target_session_turn_id",
                partner_dispatch_columns,
            )
            self.assertIn("agent_observation_events", tables)
            session_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agent_sessions)")
            }
            self.assertIn("session_kind", session_columns)
            memory_source_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agent_memory_sources)")
            }
            self.assertTrue(
                {
                    "owner_kind",
                    "owner_id",
                    "source_kind",
                    "trust_class",
                    "disposition",
                    "coverage_end_entry_id",
                    "role_id",
                    "role_version",
                }.issubset(memory_source_columns)
            )

            self.assertIn("project_context_enabled", session_columns)
            self.assertIn("pi_skills_enabled", session_columns)
            self.assertIn("codex_skills_enabled", session_columns)
            self.assertIn("role_book_revision_id", session_columns)
            for table, primary_key in (
                ("memory_items", "memory_id"),
                ("memory_atoms", "id"),
                ("memory_books", "book_id"),
                ("memory_retrieval_docs", "doc_id"),
            ):
                with self.subTest(table=table, primary_key=primary_key):
                    columns = {
                        row[1] for row in conn.execute(f"PRAGMA table_info({table})")
                    }
                    self.assertIn(primary_key, columns)

                    self.assertIn("owner_kind", columns)
                    self.assertIn("owner_id", columns)
            command_receipt_sql = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='agent_command_receipts'"
            ).fetchone()[0]
            self.assertIn("'session_rewrite'", command_receipt_sql)
            command_receipt_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(agent_command_receipts)"
                )
            }
            self.assertTrue(
                {
                    "semantic_payload_sha256",
                    "retry_of_client_message_id",
                }.issubset(command_receipt_columns)
            )
            run_foreign_keys = {
                str(row[3]): (str(row[2]), str(row[6]))
                for row in conn.execute("PRAGMA foreign_key_list(agent_subagent_runs)")
            }
            self.assertNotIn("child_session_id", run_foreign_keys)
            self.assertEqual(run_foreign_keys["batch_id"], ("agent_subagent_batches", "CASCADE"))
            persona_columns = {
                row[1] for row in conn.execute("PRAGMA table_info(agent_personas)")
            }
            self.assertTrue(
                {
                    "persona_prompt",
                    "safety_policy_prompt",
                    "tool_policy_json",
                    "tool_profile_version",
                }.issubset(persona_columns)
            )

    def test_0143_preserves_runtime_rows_and_adds_completed_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0142-") as temporary:
            migrations_0142 = Path(temporary) / "migrations"
            migrations_0143 = Path(temporary) / "migrations-0143"
            migrations_0142.mkdir()
            migrations_0143.mkdir()
            for migration in load_migrations():
                if migration.version <= 142:
                    shutil.copy2(migration.path, migrations_0142 / migration.path.name)
                if migration.version <= 143:
                    shutil.copy2(migration.path, migrations_0143 / migration.path.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                initial = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0142,
                )
                self.assertEqual(initial.current_version, 142)
                conn.execute(
                    """INSERT INTO room_kernel_runtime_effects(
                       dispatch_id,root_id,session_id,dispatch_generation,
                       state,runtime_receipt_json,updated_at_ms)
                       VALUES ('dispatch:migration','root:migration',
                               'session:migration',3,'accepted','{}',10)"""
                )
                conn.execute(
                    """INSERT INTO room_kernel_abort_scopes(
                       dispatch_id,root_id,session_id,generation,state,
                       surfaces_json,cancel_receipt_json,updated_at_ms)
                       VALUES ('dispatch:migration','root:migration',
                               'session:migration',3,'registered','[]','{}',10)"""
                )

                upgraded = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0143,
                )

                self.assertEqual(upgraded.applied_versions, (143,))
                self.assertEqual(
                    conn.execute(
                        """SELECT state,runtime_receipt_json
                           FROM room_kernel_runtime_effects
                           WHERE dispatch_id='dispatch:migration'"""
                    ).fetchone(),
                    ("accepted", "{}"),
                )
                self.assertEqual(
                    conn.execute(
                        """SELECT state,surfaces_json
                           FROM room_kernel_abort_scopes
                           WHERE dispatch_id='dispatch:migration'"""
                    ).fetchone(),
                    ("registered", "[]"),
                )
                conn.execute(
                    """UPDATE room_kernel_runtime_effects SET state='completed'
                       WHERE dispatch_id='dispatch:migration'"""
                )
                conn.execute(
                    """UPDATE room_kernel_abort_scopes SET state='completed'
                       WHERE dispatch_id='dispatch:migration'"""
                )
                self.assertEqual(
                    conn.execute(
                        """SELECT effects.state,scopes.state
                           FROM room_kernel_runtime_effects effects
                           JOIN room_kernel_abort_scopes scopes USING(dispatch_id)
                           WHERE effects.dispatch_id='dispatch:migration'"""
                    ).fetchone(),
                    ("completed", "completed"),
                )
                self.assertIsNone(
                    conn.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type='table'
                             AND name IN (
                                 'room_kernel_runtime_effects_v142',
                                 'room_kernel_abort_scopes_v142'
                             )"""
                    ).fetchone()
                )

    def test_0144_adds_resumed_and_only_backfills_proven_resume_identity(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-ime-migrations-0143-"
        ) as temporary:
            migrations_0143 = Path(temporary) / "migrations-0143"
            migrations_0144 = Path(temporary) / "migrations-0144"
            migrations_0143.mkdir()
            migrations_0144.mkdir()
            for migration in load_migrations():
                if migration.version <= 143:
                    shutil.copy2(
                        migration.path,
                        migrations_0143 / migration.path.name,
                    )
                if migration.version <= 144:
                    shutil.copy2(
                        migration.path,
                        migrations_0144 / migration.path.name,
                    )

            with closing(sqlite3.connect(":memory:")) as conn:
                initial = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0143,
                )
                self.assertEqual(initial.current_version, 143)
                conn.execute(
                    """INSERT INTO room_kernel_roots(
                       root_id,room_id,generation,state,
                       facilitator_participant_id,requirement_anchor_ref,
                       budget_remaining,max_hops,max_depth,payload_json,
                       created_at_ms,updated_at_ms)
                       VALUES ('root:resume','room:resume',0,'running',
                               'participant:a','anchor:root',10,4,3,'{}',1,1)"""
                )
                conn.execute(
                    """INSERT INTO room_kernel_tasks(
                       task_id,root_id,state,payload_json,updated_at_ms,
                       current_owner_participant_id)
                       VALUES ('task:resume','root:resume','active','{}',1,
                               'participant:a')"""
                )
                for dispatch_id, parent_dispatch_id in (
                    ("dispatch:parent", None),
                    ("dispatch:resume-valid", "dispatch:parent"),
                    ("dispatch:resume-unproven", "dispatch:parent"),
                ):
                    conn.execute(
                        """INSERT INTO room_kernel_dispatches(
                           dispatch_id,root_id,task_id,parent_dispatch_id,
                           generation,hop_count,depth,budget_cost,
                           target_session_id,target_participant_id,trigger_id,
                           intent_kind,idempotency_key,state,payload_json,
                           created_at_ms,updated_at_ms)
                           VALUES (?,?, 'task:resume',?,0,0,0,1,
                                   'session:a','participant:a',?,'resume',?,
                                   'pending','{}',1,1)""",
                        (
                            dispatch_id,
                            "root:resume",
                            parent_dispatch_id,
                            f"trigger:{dispatch_id}",
                            f"key:{dispatch_id}",
                        ),
                    )
                conn.execute(
                    """INSERT INTO room_v2_requirement_anchors(
                       anchor_id,root_id,root_sequence,original_bytes,
                       original_sha256,created_by,authenticity,
                       provenance_json,created_at_ms)
                       VALUES ('anchor:answer','root:resume',1,X'61',?,
                               'user:test','original_user_bytes','{}',2)""",
                    ("a" * 64,),
                )
                for continuation_id, commit_id, payload in (
                    (
                        "continuation:valid",
                        "commit:valid",
                        {
                            "resumeDispatchId": "dispatch:resume-valid",
                            "answerAnchorId": "anchor:answer",
                        },
                    ),
                    (
                        "continuation:unproven",
                        "commit:unproven",
                        {
                            "resumeDispatchId": "dispatch:resume-unproven",
                            "answerAnchorId": "anchor:missing",
                        },
                    ),
                ):
                    conn.execute(
                        """INSERT INTO room_kernel_continuations(
                           continuation_id,root_id,task_id,
                           parent_dispatch_id,child_dispatch_id,commit_id,
                           decision,state,payload_json,created_at_ms)
                           VALUES (?,'root:resume','task:resume',
                                   'dispatch:parent',NULL,?,'wait','blocked',?,2)""",
                        (continuation_id, commit_id, json.dumps(payload)),
                    )

                upgraded = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0144,
                )

                self.assertEqual(upgraded.applied_versions, (144,))
                self.assertEqual(
                    conn.execute(
                        """SELECT continuation_id,state
                           FROM room_kernel_continuations
                           ORDER BY continuation_id"""
                    ).fetchall(),
                    [
                        ("continuation:unproven", "blocked"),
                        ("continuation:valid", "resumed"),
                    ],
                )
                conn.execute(
                    """UPDATE room_kernel_continuations SET state='resumed'
                       WHERE continuation_id='continuation:unproven'"""
                )

    def test_0122_backfills_failed_command_for_explicit_retry_lineage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-ime-migrations-0109-"
        ) as temporary:
            root = Path(temporary)
            migrations_0109 = root / "migrations-0109"
            migrations_0110 = root / "migrations-0110"
            migrations_0109.mkdir()
            migrations_0110.mkdir()
            for migration in load_migrations():
                if migration.version <= 109:
                    shutil.copy2(
                        migration.path,
                        migrations_0109 / migration.path.name,
                    )
                if migration.version <= 110:
                    shutil.copy2(
                        migration.path,
                        migrations_0110 / migration.path.name,
                    )

            database_path = root / "agent.sqlite"
            payload = {
                "message": "retry after durable failure",
                "attachments": [],
            }
            payload_sha256 = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()

            with closing(sqlite3.connect(database_path)) as conn:
                initial = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0109,
                    applied_at_ms=109,
                )
                self.assertEqual(initial.current_version, 109)
                conn.execute(
                    """
                    INSERT INTO agent_command_receipts(
                        command_scope, scope_id, client_message_id,
                        payload_sha256, claim_token, state, response_json,
                        error, created_at_ms, updated_at_ms
                    ) VALUES(
                        'session_prompt', 'session-legacy-retry',
                        'legacy-failed', ?, 'legacy-claim', 'failed', NULL,
                        'durable pre-accept failure', 108, 109
                    )
                    """,
                    (payload_sha256,),
                )
                conn.commit()

                original_0110 = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0110,
                    applied_at_ms=110,
                )
                self.assertEqual(original_0110.applied_versions, (110,))
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT semantic_payload_sha256,
                               retry_of_client_message_id
                        FROM agent_command_receipts
                        WHERE command_scope = 'session_prompt'
                          AND scope_id = 'session-legacy-retry'
                          AND client_message_id = 'legacy-failed'
                        """
                    ).fetchone(),
                    ("", ""),
                )
                migration_0110 = next(
                    migration
                    for migration in load_migrations()
                    if migration.version == 110
                )
                self.assertTrue(
                    migration_0110.checksum.startswith("0c14909d")
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT name, checksum
                        FROM schema_migrations
                        WHERE version = 110
                        """
                    ).fetchone(),
                    (
                        "agent_command_retry_lineage",
                        migration_0110.checksum,
                    ),
                )

                upgraded = apply_database_migrations(
                    conn,
                    applied_at_ms=122,
                )
                replay = apply_database_migrations(
                    conn,
                    applied_at_ms=123,
                )

                self.assertEqual(
                    upgraded.applied_versions,
                    (111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS,
                )
                self.assertEqual(replay.applied_versions, ())
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT semantic_payload_sha256,
                               retry_of_client_message_id
                        FROM agent_command_receipts
                        WHERE command_scope = 'session_prompt'
                          AND scope_id = 'session-legacy-retry'
                          AND client_message_id = 'legacy-failed'
                        """
                    ).fetchone(),
                    (payload_sha256, ""),
                )
                column_defaults = {
                    str(row[1]): str(row[4])
                    for row in conn.execute(
                        "PRAGMA table_info(agent_command_receipts)"
                    )
                }
                self.assertEqual(
                    column_defaults["semantic_payload_sha256"],
                    "''",
                )
                self.assertEqual(
                    column_defaults["retry_of_client_message_id"],
                    "''",
                )
                migration_0122 = next(
                    migration
                    for migration in load_migrations()
                    if migration.version == 122
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT name, checksum
                        FROM schema_migrations
                        WHERE version = 122
                        """
                    ).fetchone(),
                    (
                        "agent_command_retry_lineage_backfill",
                        migration_0122.checksum,
                    ),
                )

            store = AgentCommandReceiptStore(database_path)
            successor = store.begin(
                command_scope="session_prompt",
                scope_id="session-legacy-retry",
                client_message_id="explicit-retry",
                payload={
                    **payload,
                    "retryOfClientMessageId": "legacy-failed",
                },
            )
            self.assertFalse(successor.is_replay)

    def test_0111_through_0126_preserve_legacy_rows_and_upgrade_task_ownership(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0110-") as temporary:
            migrations_0110 = Path(temporary) / "migrations"
            migrations_0110.mkdir()
            for migration in load_migrations():
                if migration.version <= 110:
                    shutil.copy2(migration.path, migrations_0110 / migration.path.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                initial = apply_database_migrations(
                    conn,
                    migrations_dir=migrations_0110,
                    applied_at_ms=110,
                )
                self.assertEqual(initial.current_version, 110)
                conn.execute(
                    """
                    INSERT INTO agent_sessions(
                        id,title,session_mode,role_id,role_version,model_profile,
                        tool_profile_version,created_at_ms,updated_at_ms,
                        last_opened_at_ms,status
                    ) VALUES(
                        'session-legacy','Legacy Session','assistant','assistant',
                        'v1','pi/default','tool-profile-v1',1,2,3,'idle'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_rooms(
                        id,title,routing_policy,status,room_file,
                        created_at_ms,updated_at_ms
                    ) VALUES(
                        'room-legacy','Legacy Room','manual_mentions','active',
                        'room-legacy.jsonl',4,5
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_room_participants(
                        id,room_id,session_id,role_id,role_version,display_name,
                        participant_status,ordinal,created_at_ms
                    ) VALUES(
                        'participant-legacy','room-legacy','session-legacy',
                        'assistant','v1','Legacy Agent','active',0,6
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_session_tool_policies(
                        session_id,allowed_tools_json,updated_at_ms
                    ) VALUES('session-legacy','null',7)
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_plan_state_events(
                        event_id,session_id,sequence,title,status,actor,created_at_ms
                    ) VALUES(
                        'plan-state-legacy','session-legacy',1,'Legacy Plan',
                        'draft','legacy-user',8
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_thread_goal_events(
                        event_id,session_id,goal_id,sequence,objective,status,
                        token_budget,time_budget_ms,tokens_used,elapsed_ms,
                        actor,created_at_ms
                    ) VALUES(
                        'goal-event-legacy','session-legacy','goal-legacy',1,
                        'Preserve the goal','active',1000,60000,10,20,
                        'legacy-user',9
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_goal_usage_receipts(
                        receipt_id,session_id,goal_id,idempotency_key,turn_id,
                        token_delta,elapsed_delta_ms,goal_event_id,created_at_ms
                    ) VALUES(
                        'goal-usage-legacy','session-legacy','goal-legacy',
                        'usage-legacy','turn-legacy',10,20,'goal-event-legacy',10
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO room_kernel_roots(
                        root_id,room_id,generation,state,owner,
                        requirement_anchor_ref,budget_remaining,budget_reserved,
                        max_hops,max_depth,acceptance_criteria_json,
                        covered_criteria_json,terminal_receipt_id,payload_json,
                        created_at_ms,updated_at_ms
                    ) VALUES(
                        'root-legacy','room-legacy',0,'running',
                        'participant-facilitator','requirement:legacy',10,0,
                        3,2,'["criterion:legacy"]','[]',NULL,'{}',11,11
                    )
                    """
                )
                legacy_task_payload = {
                    "schemaVersion": "wisdom-weasel.room-task.v2",
                    "taskId": "task-legacy",
                    "rootId": "root-legacy",
                    "parentTaskId": None,
                    "ownerParticipantId": "participant-facilitator",
                    "assigneeParticipantId": "participant-worker",
                    "objective": "Preserve one legacy Task.",
                    "expectedOutput": "An upgraded Task.",
                    "requirementItemIds": ["requirement:legacy"],
                    "acceptanceCriterionIds": ["criterion:legacy"],
                    "contextEvidenceRefs": ["evidence:legacy"],
                    "revision": 0,
                    "state": "active",
                }
                conn.execute(
                    """
                    INSERT INTO room_kernel_tasks(
                        task_id,root_id,parent_task_id,state,payload_json,
                        updated_at_ms
                    ) VALUES('task-legacy','root-legacy',NULL,'active',?,11)
                    """,
                    (
                        json.dumps(
                            legacy_task_payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                    ),
                )

                upgraded = apply_database_migrations(conn, applied_at_ms=118)
                replay = apply_database_migrations(conn, applied_at_ms=119)

                self.assertEqual(
                    upgraded.applied_versions,
                    (111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS,
                )
                self.assertEqual(replay.applied_versions, ())
                self.assertEqual(
                    conn.execute(
                        "SELECT id,title FROM agent_sessions WHERE id='session-legacy'"
                    ).fetchone(),
                    ("session-legacy", "Legacy Session"),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT id,title FROM agent_rooms WHERE id='room-legacy'"
                    ).fetchone(),
                    ("room-legacy", "Legacy Room"),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT room_id,session_id FROM agent_room_participants "
                        "WHERE id='participant-legacy'"
                    ).fetchone(),
                    ("room-legacy", "session-legacy"),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT disclosure_preferences_json,policy_revision "
                        "FROM agent_session_tool_policies "
                        "WHERE session_id='session-legacy'"
                    ).fetchone(),
                    ("{}", 1),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT success_criteria,evidence_expectations_json,"
                        "cancellation_audit_id FROM agent_thread_goal_events "
                        "WHERE event_id='goal-event-legacy'"
                    ).fetchone(),
                    ("", "[]", None),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT goal_event_id,source_event_id "
                        "FROM agent_goal_usage_receipts "
                        "WHERE receipt_id='goal-usage-legacy'"
                    ).fetchone(),
                    ("goal-event-legacy", ""),
                )
                task_row = conn.execute(
                    """
                    SELECT current_owner_participant_id,ownership_revision,
                           ownership_receipt_id,payload_json
                    FROM room_kernel_tasks
                    WHERE task_id='task-legacy'
                    """
                ).fetchone()
                self.assertIsNotNone(task_row)
                assert task_row is not None
                self.assertEqual(task_row[0], "participant-worker")
                self.assertEqual(task_row[1], 0)
                self.assertIsNone(task_row[2])
                upgraded_task_payload = json.loads(str(task_row[3]))
                self.assertEqual(
                    upgraded_task_payload["schemaVersion"],
                    "wisdom-weasel.room-task.v3",
                )
                self.assertEqual(
                    upgraded_task_payload["currentOwnerParticipantId"],
                    "participant-worker",
                )
                self.assertEqual(
                    upgraded_task_payload["contextEvidenceRefs"],
                    ["evidence:legacy"],
                )
                self.assertNotIn(
                    "ownerParticipantId",
                    upgraded_task_payload,
                )
                self.assertNotIn(
                    "assigneeParticipantId",
                    upgraded_task_payload,
                )
                indexes = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
                self.assertTrue(
                    {
                        "idx_agent_background_jobs_session_updated",
                        "idx_agent_background_jobs_live",
                        "idx_work_documents_state_updated",
                        "idx_work_document_outbox_pending",
                        "idx_agent_thread_goal_events_goal_sequence",
                    }.issubset(indexes)
                )
                background_job_foreign_keys = {
                    str(row[3]): (str(row[2]), str(row[6]))
                    for row in conn.execute(
                        "PRAGMA foreign_key_list(agent_background_jobs)"
                    )
                }
                goal_usage_foreign_keys = {
                    str(row[3]): str(row[2])
                    for row in conn.execute(
                        "PRAGMA foreign_key_list(agent_goal_usage_receipts)"
                    )
                }
                work_document_outbox_foreign_keys = {
                    str(row[3]): (str(row[2]), str(row[6]))
                    for row in conn.execute(
                        "PRAGMA foreign_key_list(work_document_outbox)"
                    )
                }
                self.assertEqual(
                    background_job_foreign_keys["session_id"],
                    ("agent_sessions", "CASCADE"),
                )
                self.assertEqual(
                    goal_usage_foreign_keys["goal_event_id"],
                    "agent_thread_goal_events",
                )
                self.assertEqual(
                    work_document_outbox_foreign_keys["document_id"],
                    ("work_documents", "CASCADE"),
                )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_applied_0093_checksum_upgrades_to_0094_without_history_rewrite(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0093-") as temporary:
            migrations_0093 = Path(temporary) / "migrations"
            migrations_0093.mkdir()
            for migration in load_migrations():
                if migration.version <= 93:
                    shutil.copy2(migration.path, migrations_0093 / migration.path.name)

            migration_0093 = next(migrations_0093.glob("0093_*.sql"))
            self.assertEqual(
                hashlib.sha256(migration_0093.read_bytes()).hexdigest(),
                "8e862454a44a180e5712465dfca55b15de15bd2d5e1e74f15fbcee55f39c79a1",
            )
            with closing(sqlite3.connect(":memory:")) as conn:
                initial = apply_database_migrations(conn, migrations_dir=migrations_0093)
                self.assertEqual(initial.current_version, 93)
                conn.execute(
                    """
                    INSERT INTO agent_message_block_sidecars(
                        block_ref, block_id, message_id, session_id, root_id,
                        generation, block_type, visibility, lifecycle_status,
                        digest, summary, raw_json, raw_bytes,
                        created_at_ms, updated_at_ms
                    ) VALUES (
                        'block:1', 'table:1', 'message:1', 'session:1', 'root:1',
                        1, 'table', 'private_session', 'active',
                        ?, 'summary', '{}', 2, 10, 10
                    )
                    """,
                    ("a" * 64,),
                )

                upgraded = apply_database_migrations(conn)

                self.assertEqual(upgraded.applied_versions, (94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS)
                self.assertEqual(upgraded.current_version, 202)
                self.assertEqual(
                    conn.execute(
                        "SELECT checksum FROM schema_migrations WHERE version=93"
                    ).fetchone()[0],
                    "8e862454a44a180e5712465dfca55b15de15bd2d5e1e74f15fbcee55f39c79a1",
                )
                conn.execute(
                    "UPDATE agent_message_block_sidecars SET lifecycle_status='completed' WHERE block_ref='block:1'"
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT lifecycle_status FROM agent_message_block_sidecars WHERE block_ref='block:1'"
                    ).fetchone()[0],
                    "completed",
                )
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_0097_preserves_media_links_and_expands_text_preview_types(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0096-") as temporary:
            migrations_0096 = Path(temporary) / "migrations"
            migrations_0096.mkdir()
            for migration in load_migrations():
                if migration.version <= 96:
                    shutil.copy2(migration.path, migrations_0096 / migration.path.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                apply_database_migrations(conn, migrations_dir=migrations_0096)
                conn.execute(
                    """
                    INSERT INTO agent_sessions(
                        id, title, session_mode, role_id, role_version, model_profile,
                        tool_profile_version, created_at_ms, updated_at_ms,
                        last_opened_at_ms, status
                    ) VALUES (
                        'session:file-preview', '文件预览', 'assistant', 'companion', '1',
                        'test/model', 'test-tools', 1, 1, 1, 'idle'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_media(
                        media_id, session_id, file_name, storage_name, mime_type,
                        byte_size, sha256, origin, created_at_ms
                    ) VALUES (
                        'media_legacytext01', 'session:file-preview', 'notes.txt',
                        'media_legacytext01.blob', 'text/plain', 4, ?,
                        'tool_result', 1
                    )
                    """,
                    ("a" * 64,),
                )
                conn.execute(
                    """
                    INSERT INTO agent_message_media(
                        session_id, pi_entry_id, turn_id, media_id, ordinal, created_at_ms
                    ) VALUES (
                        'session:file-preview', 'entry:1', 'turn:1',
                        'media_legacytext01', 0, 1
                    )
                    """
                )

                upgraded = apply_database_migrations(conn)

                self.assertEqual(upgraded.applied_versions, (97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS)
                self.assertEqual(
                    conn.execute(
                        "SELECT file_name, mime_type FROM agent_media WHERE media_id='media_legacytext01'"
                    ).fetchone(),
                    ("notes.txt", "text/plain"),
                )
                self.assertEqual(
                    conn.execute(
                        "SELECT pi_entry_id FROM agent_message_media WHERE media_id='media_legacytext01'"
                    ).fetchone()[0],
                    "entry:1",
                )
                conn.execute(
                    """
                    INSERT INTO agent_media(
                        media_id, session_id, file_name, storage_name, mime_type,
                        byte_size, sha256, origin, created_at_ms
                    ) VALUES (
                        'media_markdownnew1', 'session:file-preview', 'handoff.md',
                        'media_markdownnew1.blob', 'text/markdown', 8, ?,
                        'tool_result', 2
                    )
                    """,
                    ("b" * 64,),
                )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.execute("DELETE FROM agent_sessions WHERE id='session:file-preview'")
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_media").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM agent_message_media").fetchone()[0], 0)

    def test_installed_0152_room_participant_steer_migration_stays_immutable(self) -> None:
        source = DEFAULT_MIGRATIONS_DIR / "0152_room_participant_steer_receipts.sql"
        self.assertEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            "17769f20b27d770388a59f2978c6e5e8ebc54cb9ada801db7b9141ccc0a3f8a0",
        )

    def test_installed_0153_through_0158_migrations_stay_immutable(self) -> None:
        expected = {
            153: "5278d10ce3f1e10804d05e6521e2f17f229c0e29a1e079072a4253c036be60fa",
            154: "5510001f54820e24d422fa096c3af8dd5bf3b2c8c473e79fca5da2a768d167ec",
            155: "70760cbbc06e8bf8539836e4839cc0a0c79fd572c3a7a38b0eeedbb2578d297d",
            156: "9a651dda74a8d037f32738928acd8be11d46297644903f4619e0ad03f45ab84d",
            157: "ab163c68917620dfc627bc29e138b975c3eb61a949cf4e2ca4c6e242177be752",
            158: "1ede70dad0b67c783911783d744b5674b87779f9a4ffab4edbd210f38810d79a",
        }
        for version, checksum in expected.items():
            with self.subTest(version=version):
                source = next(DEFAULT_MIGRATIONS_DIR.glob(f"{version:04d}_*.sql"))
                self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), checksum)

    def test_0153_through_0158_preserve_legacy_subagent_runs(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0158-") as temporary:
            migrations_0152 = Path(temporary) / "migrations-0152"
            migrations_0152.mkdir()
            for migration in load_migrations():
                if migration.version <= 152:
                    shutil.copy2(migration.path, migrations_0152 / migration.path.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                apply_database_migrations(conn, migrations_dir=migrations_0152)
                conn.executemany(
                    """
                    INSERT INTO agent_sessions(
                        id, title, session_mode, role_id, role_version,
                        model_profile, tool_profile_version, created_at_ms,
                        updated_at_ms, last_opened_at_ms, status
                    ) VALUES (?, ?, 'assistant', 'companion', '1',
                              'test/model', 'test-tools', 1, 1, 1, 'idle')
                    """,
                    (
                        ("session:legacy-parent", "Legacy parent"),
                        ("session:legacy-child-1", "Legacy child 1"),
                        ("session:legacy-child-2", "Legacy child 2"),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_subagent_batches(
                        id, parent_session_id, context_mode, state, depth,
                        max_depth, created_at_ms, updated_at_ms
                    ) VALUES (
                        'batch:legacy', 'session:legacy-parent', 'fresh',
                        'completed', 1, 2, 1, 1
                    )
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO agent_subagent_runs(
                        id, batch_id, child_session_id, template_id,
                        template_version, ordinal, task_text, state,
                        max_turns, max_tool_calls, max_total_tokens,
                        max_duration_ms, max_output_chars, created_at_ms,
                        updated_at_ms
                    ) VALUES (?, 'batch:legacy', ?, 'researcher', '1', ?, ?,
                              'completed', 4, 4, 1024, 1000, 1024, 1, 1)
                    """,
                    (
                        ("run:legacy-1", "session:legacy-child-1", 0, "Legacy task 1"),
                        ("run:legacy-2", "session:legacy-child-2", 1, "Legacy task 2"),
                    ),
                )

                upgraded = apply_database_migrations(conn)

                self.assertEqual(upgraded.applied_versions, tuple(range(153, 203)))
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT logical_node_id, attempt_id, attempt_number,
                               launch_digest_json, structured_output_json
                        FROM agent_subagent_runs ORDER BY ordinal
                        """
                    ).fetchall(),
                    [("", "", 1, "{}", "{}"), ("", "", 1, "{}", "{}")],
                )
                conn.execute(
                    """
                    UPDATE agent_subagent_runs
                    SET logical_node_id = 'node:new', attempt_number = 1
                    WHERE id = 'run:legacy-1'
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        UPDATE agent_subagent_runs
                        SET logical_node_id = 'node:new', attempt_number = 1
                        WHERE id = 'run:legacy-2'
                        """
                    )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_production_0062_history_stays_immutable_and_workflow_migrations_append(self) -> None:
        expected_history = {
            61: (
                "separate_activity_timeline_index",
                "fd7b2767870341e6551d232819d10edfab5a68b1e70ac89957e4672059a19b7c",
            ),
            62: (
                "retire_owner_umbrella_topic_books",
                "54207aee24349ffe2598b791208aa793500094c71dd317f3d68a678cda400baf",
            ),
        }
        with tempfile.TemporaryDirectory(prefix="rag-ime-production-0062-") as temporary:
            migrations_0062 = Path(temporary) / "migrations"
            migrations_0062.mkdir()
            for source in DEFAULT_MIGRATIONS_DIR.glob("*.sql"):
                version = int(source.name.split("_", 1)[0])
                if version <= 62:
                    shutil.copy2(source, migrations_0062 / source.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                initial = apply_database_migrations(conn, migrations_dir=migrations_0062)
                self.assertEqual(initial.current_version, 62)
                for version, (name, checksum) in expected_history.items():
                    source = next(migrations_0062.glob(f"{version:04d}_*.sql"))
                    self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), checksum)
                    self.assertEqual(
                        conn.execute(
                            "SELECT name, checksum FROM schema_migrations WHERE version = ?",
                            (version,),
                        ).fetchone(),
                        (name, checksum),
                    )

                appended = apply_database_migrations(conn)
                self.assertEqual(appended.applied_versions, (63, 64, 65, 66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS)
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                status = migration_status(conn)
                self.assertTrue(status["ok"])
                self.assertEqual(status["currentVersion"], 202)

    def test_legacy_atoms_preserve_supersession_lineage_and_require_evidence(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0058-") as temporary:
            migrations_0058 = Path(temporary) / "migrations"
            migrations_0058.mkdir()
            for source in DEFAULT_MIGRATIONS_DIR.glob("*.sql"):
                if source.name < "0059_":
                    shutil.copy2(source, migrations_0058 / source.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                apply_database_migrations(conn, migrations_dir=migrations_0058)
                conn.executemany(
                    """
                    INSERT INTO input_events(
                        id, created_at_ms, source, committed_text, project
                    ) VALUES (?, ?, 'test', ?, 'wisdom-weasel-rag-ime')
                    """,
                    (
                        (1, 100, "旧事实证据"),
                        (2, 200, "新事实证据"),
                        (3, 300, "已遗忘证据"),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO memory_tombstones(
                        created_at_ms, target_type, target_value, reason, active
                    ) VALUES (350, 'source_event_id', '3', '测试遗忘', 1)
                    """
                )
                for atom_id, status, updated_at_ms, event_ids in (
                    ("atom:legacy-old", "active", 200, "[1]"),
                    ("atom:legacy-successor", "active", 300, "[2]"),
                    ("atom:legacy-tombstoned", "active", 400, "[3]"),
                    ("atom:legacy-unsupported", "active", 500, "[]"),
                    ("atom:legacy-hidden", "hidden", 600, "[]"),
                ):
                    conn.execute(
                        """
                        INSERT INTO memory_atoms(
                            id, kind, text, source_event_ids_json,
                            source_memory_ids_json, confidence, quality_score,
                            echo_risk, privacy_level, status,
                            created_at_ms, updated_at_ms
                        ) VALUES (?, 'project_fact', ?, ?, '[]', 0.8, 0.8,
                                  0.0, 'local', ?, 100, ?)
                        """,
                        (atom_id, atom_id, event_ids, status, updated_at_ms),
                    )
                conn.execute(
                    """
                    INSERT INTO memory_supersessions(
                        supersession_id, old_memory_id, new_memory_id, reason,
                        status, created_at_ms
                    ) VALUES (
                        'supersession:test', 'atom:legacy-old',
                        'atom:legacy-successor', '事实更新', 'active', 250
                    )
                    """
                )

                result = apply_database_migrations(conn)
                rows = {
                    str(row[0]): tuple(row[1:])
                    for row in conn.execute(
                        """
                        SELECT id, status, claim_key, lineage_id, claim_state,
                               valid_from_ms, valid_to_ms, supersedes_id
                        FROM memory_atoms
                        ORDER BY id
                        """
                    )
                }
                audit_rows = {
                    str(row[0]): tuple(row[1:])
                    for row in conn.execute(
                        """
                        SELECT atom_id, previous_status, previous_claim_state,
                               disposition, representation_status,
                               representation_claim_state, reason, created_at_ms
                        FROM memory_legacy_atom_migration_audit
                        WHERE migration_version = 59
                        ORDER BY atom_id
                        """
                    )
                }

            self.assertEqual(
                result.applied_versions,
                (59, 60, 61, 62, 63, 64, 65, 66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS,
            )
            self.assertEqual(
                rows["atom:legacy-old"],
                (
                    "superseded",
                    "legacy:atom:legacy-successor",
                    "lineage:legacy:atom:legacy-successor",
                    "superseded",
                    100,
                    250,
                    None,
                ),
            )
            self.assertEqual(
                rows["atom:legacy-successor"],
                (
                    "active",
                    "legacy:atom:legacy-successor",
                    "lineage:legacy:atom:legacy-successor",
                    "current",
                    100,
                    None,
                    "atom:legacy-old",
                ),
            )
            self.assertEqual(rows["atom:legacy-tombstoned"][0], "hidden")
            self.assertEqual(rows["atom:legacy-tombstoned"][3], "retracted")
            self.assertEqual(rows["atom:legacy-unsupported"][0], "hidden")
            self.assertEqual(rows["atom:legacy-unsupported"][3], "retracted")
            self.assertEqual(rows["atom:legacy-hidden"][3:6], ("retracted", 100, 600))
            self.assertEqual(set(audit_rows), set(rows))
            self.assertEqual(
                {
                    value[2]
                    for value in audit_rows.values()
                },
                {
                    "quarantined_missing_visible_evidence",
                    "current_evidence_backed",
                    "superseded_history",
                },
            )
            self.assertEqual(
                audit_rows["atom:legacy-old"][:5],
                (
                    "active",
                    "current",
                    "superseded_history",
                    "superseded",
                    "superseded",
                ),
            )
            self.assertEqual(
                audit_rows["atom:legacy-successor"][:5],
                (
                    "active",
                    "current",
                    "current_evidence_backed",
                    "active",
                    "current",
                ),
            )
            quarantined = audit_rows["atom:legacy-unsupported"]
            self.assertEqual(
                quarantined[:5],
                (
                    "active",
                    "current",
                    "quarantined_missing_visible_evidence",
                    "hidden",
                    "retracted",
                ),
            )
            self.assertIn("not a user withdrawal", str(quarantined[5]))
            self.assertGreater(int(quarantined[6]), 0)

    def test_atom_first_migration_supersedes_preexisting_memory_book_draft(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-v38-") as tmp, closing(
            sqlite3.connect(":memory:")
        ) as conn, conn:
            migrations = Path(__file__).resolve().parents[1] / "rag_ime" / "db" / "migrations"
            migrations_v38 = Path(tmp)
            for source in sorted(migrations.glob("*.sql")):
                if int(source.name.split("_", 1)[0]) <= 38:
                    shutil.copy2(source, migrations_v38 / source.name)
            apply_database_migrations(
                conn,
                migrations_dir=migrations_v38,
            )
            conn.execute(
                """
                INSERT INTO memory_cleanup_runs(
                    run_id, created_at_ms, provider, model, status, summary, metadata_json
                ) VALUES (
                    'memory_book_legacy', 1, 'deepseek', 'legacy', 'draft', '旧草案',
                    '{"project":"wisdom-weasel-rag-ime"}'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO memory_cleanup_diffs(
                    run_id, op, payload_json, status, created_at_ms
                ) VALUES ('memory_book_legacy', 'upsert_memory_atom', '{}', 'pending', 1)
                """
            )
            conn.execute(
                """
                INSERT INTO memory_compile_state(
                    project, last_compiled_event_id, last_run_ms, pending_event_count,
                    last_bundle_hash, last_drafted_event_id, last_draft_ms,
                    last_draft_bundle_hash, last_draft_run_id
                ) VALUES (
                    'wisdom-weasel-rag-ime', 7, 1, 2, 'old', 9, 2, 'draft', 'memory_book_legacy'
                )
                """
            )

            result = apply_database_migrations(
                conn,
                migrations_dir=migrations,
            )

            self.assertEqual(
                result.applied_versions,
                (
                    39, 40, 41, 42, 43, 44, 45, 46, 47,
                    51, 52, 53, 54, 55, 56, 57, 58, 59, 60,
                    61, 62, 63, 64, 65, 66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126, *POST_0126_MIGRATIONS,
                ),
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM memory_cleanup_runs WHERE run_id = 'memory_book_legacy'"
                ).fetchone()[0],
                "superseded",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT status FROM memory_cleanup_diffs WHERE run_id = 'memory_book_legacy'"
                ).fetchone()[0],
                "rejected",
            )
            state = conn.execute(
                """
                SELECT last_drafted_event_id, last_draft_ms,
                       last_draft_bundle_hash, last_draft_run_id
                FROM memory_compile_state
                WHERE project = 'wisdom-weasel-rag-ime'
                """
            ).fetchone()
            self.assertEqual(state, (0, 0, "", ""))

    def test_timeline_migration_archives_daily_books_and_enqueues_projection(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0061-") as temporary:
            migrations_0060 = Path(temporary) / "migrations"
            migrations_0060.mkdir()
            for source in DEFAULT_MIGRATIONS_DIR.glob("*.sql"):
                if source.name < "0061_":
                    shutil.copy2(source, migrations_0060 / source.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                apply_database_migrations(conn, migrations_dir=migrations_0060)
                conn.execute(
                    """
                    INSERT INTO memory_books(
                        book_id, book_type, book_key, title, summary,
                        project, status, created_at_ms, updated_at_ms
                    ) VALUES (
                        'book:daily:test', 'daily', 'activity:2026-07-18',
                        '2026-07-18 活动时间线', '旧 Daily Book',
                        'wisdom-weasel-rag-ime', 'active', 10, 20
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO daily_activity_timelines(
                        timeline_id, project, timeline_date, status,
                        source_event_hash, approved_book_id,
                        created_at_ms, updated_at_ms
                    ) VALUES (
                        'timeline:test', 'wisdom-weasel-rag-ime',
                        '2026-07-18', 'approved', 'hash:test',
                        'book:daily:test', 10, 20
                    )
                    """
                )

                result = apply_database_migrations(conn)

                self.assertEqual(
                    result.applied_versions,
                    (61, 62, 63, 64, 65, 66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS,
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT status, archived_at_ms, archive_reason
                        FROM memory_books
                        WHERE book_id = 'book:daily:test'
                        """
                    ).fetchone(),
                    ("archived", 20, "migrated_to_timeline_index"),
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT approved_book_id
                        FROM daily_activity_timelines
                        WHERE timeline_id = 'timeline:test'
                        """
                    ).fetchone()[0],
                    "",
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT projection_kind, aggregate_type, aggregate_id,
                               operation, revision, state
                        FROM memory_projection_outbox
                        WHERE aggregate_id = '0061_separate_activity_timeline_index'
                        """
                    ).fetchone(),
                    (
                        "retrieval_docs",
                        "schema_migration",
                        "0061_separate_activity_timeline_index",
                        "rebuild",
                        61,
                        "pending",
                    ),
                )

    def test_thematic_book_migration_retires_only_redundant_owner_umbrella(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0062-") as temporary:
            migrations_0061 = Path(temporary) / "migrations"
            migrations_0061.mkdir()
            for source in DEFAULT_MIGRATIONS_DIR.glob("*.sql"):
                if source.name < "0062_":
                    shutil.copy2(source, migrations_0061 / source.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                apply_database_migrations(conn, migrations_dir=migrations_0061)
                conn.executemany(
                    """
                    INSERT INTO memory_books(
                        book_id, book_type, book_key, title, summary, project,
                        owner_kind, owner_id, status, created_at_ms, updated_at_ms
                    ) VALUES (?, 'topic', ?, ?, ?, ?, 'user', ?, 'active', 10, 20)
                    """,
                    (
                        (
                            "book:owner:with-topics",
                            "owner-with-topics",
                            "个人长期记忆",
                            "旧总书",
                            "wisdom-weasel-rag-ime",
                            "default",
                        ),
                        (
                            "book:owner:with-topics:topic:rag",
                            "owner-with-topics-topic-rag",
                            "记忆与 RAG 治理",
                            "主题书",
                            "wisdom-weasel-rag-ime",
                            "default",
                        ),
                        (
                            "book:owner:only-book",
                            "owner-only-book",
                            "个人长期记忆",
                            "唯一总书",
                            "other-project",
                            "other-user",
                        ),
                    ),
                )

                result = apply_database_migrations(conn)

                self.assertEqual(
                    result.applied_versions,
                    (62, 63, 64, 65, 66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS,
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT status, archived_at_ms, archive_reason
                        FROM memory_books
                        WHERE book_id = 'book:owner:with-topics'
                        """
                    ).fetchone(),
                    (
                        "archived",
                        20,
                        "migrated_to_thematic_topic_books",
                    ),
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT status
                        FROM memory_books
                        WHERE book_id = 'book:owner:only-book'
                        """
                    ).fetchone()[0],
                    "active",
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT operation, revision, state
                        FROM memory_projection_outbox
                        WHERE aggregate_id =
                              '0062_retire_owner_umbrella_topic_books'
                        """
                    ).fetchone(),
                    ("rebuild", 62, "pending"),
                )

    def test_owner_claim_identity_migration_repairs_cross_kind_duplicates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0066-") as temporary:
            migrations_0065 = Path(temporary) / "migrations"
            migrations_0065.mkdir()
            for source in DEFAULT_MIGRATIONS_DIR.glob("*.sql"):
                if source.name < "0066_":
                    shutil.copy2(source, migrations_0065 / source.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                apply_database_migrations(conn, migrations_dir=migrations_0065)
                conn.executemany(
                    """
                    INSERT INTO memory_atoms(
                        id, kind, text, canonical_text,
                        source_event_ids_json, source_memory_ids_json,
                        scope_app, scope_project, confidence, quality_score,
                        status, created_at_ms, updated_at_ms,
                        owner_kind, owner_id, claim_key, lineage_id,
                        claim_state, valid_from_ms
                    ) VALUES (?, ?, ?, ?, '[]', '[]', '', 'ime', 1.0, 1.0,
                              'active', ?, ?, 'user', 'default',
                              'ime:runtime-model', ?, 'current', ?)
                    """,
                    (
                        (
                            "atom:model-old",
                            "project_fact",
                            "输入法当前使用 Qwen 0.8B。",
                            "输入法当前使用 Qwen 0.8B。",
                            100,
                            100,
                            "lineage:old",
                            100,
                        ),
                        (
                            "atom:model-new",
                            "project_decision",
                            "输入法当前使用 100M 自训练模型。",
                            "输入法当前使用 100M 自训练模型。",
                            200,
                            200,
                            "lineage:new",
                            200,
                        ),
                    ),
                )

                result = apply_database_migrations(conn)
                rows = conn.execute(
                    """
                    SELECT id, status, claim_state, valid_to_ms, lineage_id
                    FROM memory_atoms
                    WHERE claim_key = 'ime:runtime-model'
                    ORDER BY valid_from_ms
                    """
                ).fetchall()

                self.assertEqual(
                    result.applied_versions,
                    (66, 68, 69, 70, 71, 72, 73, 74, 75, 76, 77, 80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118, 119, 120, 121, 122, 123, 124, 125, 126) + POST_0126_MIGRATIONS,
                )
                self.assertEqual(
                    rows,
                    [
                        (
                            "atom:model-old",
                            "superseded",
                            "superseded",
                            200,
                            "lineage:new",
                        ),
                        (
                            "atom:model-new",
                            "active",
                            "current",
                            None,
                            "lineage:new",
                        ),
                    ],
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT old_memory_id, new_memory_id, reason, status
                        FROM memory_supersessions
                        WHERE old_memory_id = 'atom:model-old'
                          AND new_memory_id = 'atom:model-new'
                        """
                    ).fetchone(),
                    (
                        "atom:model-old",
                        "atom:model-new",
                        "same_owner_claim_key_newer_value",
                        "active",
                    ),
                )

                # The same claim key is valid for independent owners after the
                # old global index has been replaced.
                conn.executemany(
                    """
                    INSERT INTO memory_atoms(
                        id, kind, text, source_event_ids_json,
                        source_memory_ids_json, scope_app, scope_project,
                        confidence, quality_score, status, created_at_ms,
                        updated_at_ms, owner_kind, owner_id, claim_key,
                        lineage_id, claim_state, valid_from_ms
                    ) VALUES (?, 'project_fact', ?, '[]', '[]', '', 'ime',
                              1.0, 1.0, 'active', 300, 300, 'agent', ?,
                              'ime:runtime-model', ?, 'current', 300)
                    """,
                    (
                        ("atom:role-a", "角色 A 使用模型 Alpha。", "role-a", "lineage:a"),
                        ("atom:role-b", "角色 B 使用模型 Beta。", "role-b", "lineage:b"),
                    ),
                )
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])

                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO memory_atoms(
                            id, kind, text, source_event_ids_json,
                            source_memory_ids_json, scope_app, scope_project,
                            confidence, quality_score, status, created_at_ms,
                            updated_at_ms, owner_kind, owner_id, claim_key,
                            lineage_id, claim_state, valid_from_ms
                        ) VALUES (
                            'atom:role-a-duplicate', 'project_decision',
                            '角色 A 的冲突 current。', '[]', '[]', '', 'ime',
                            1.0, 1.0, 'active', 400, 400, 'agent', 'role-a',
                            'ime:runtime-model', 'lineage:a', 'current', 400
                        )
                        """
                    )

    def test_legacy_feedback_table_is_rebuilt_without_losing_rows(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            conn.execute(
                """
                CREATE TABLE memory_feedback_events (
                    id TEXT PRIMARY KEY,
                    candidate_id TEXT,
                    candidate_text TEXT NOT NULL,
                    candidate_source TEXT NOT NULL,
                    action TEXT NOT NULL,
                    context_hash TEXT,
                    front_app_bundle_id TEXT,
                    raw_input TEXT,
                    preedit TEXT,
                    committed_tail TEXT,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at_ms INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO memory_feedback_events(id, candidate_text, candidate_source, action, created_at_ms) VALUES ('one', '候选', 'rag', 'shown', 7)"
            )

            apply_database_migrations(conn)

            row = conn.execute(
                "SELECT id, candidate_text, candidate_source FROM memory_feedback_events"
            ).fetchone()
            self.assertEqual(row, ("one", "候选", "rag"))
            columns = {item[1]: item for item in conn.execute("PRAGMA table_info(memory_feedback_events)")}
            self.assertEqual(columns["candidate_text"][4], "''")
            self.assertEqual(columns["candidate_source"][4], "'unknown'")

    def test_legacy_input_events_gain_context_group_columns_without_data_loss(self) -> None:
        with closing(sqlite3.connect(":memory:")) as conn, conn:
            conn.execute(
                """
                CREATE TABLE input_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    committed_text TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO input_events(created_at_ms, source, committed_text) VALUES (1, 'legacy', '保留内容')"
            )

            apply_database_migrations(conn)

            columns = {item[1]: item for item in conn.execute("PRAGMA table_info(input_events)")}
            self.assertEqual(columns["context_group_id"][4], "''")
            self.assertEqual(columns["context_group_level"][4], "'app'")
            self.assertEqual(columns["capture_metadata_json"][4], "'{}'")
            self.assertEqual(conn.execute("SELECT committed_text FROM input_events").fetchone()[0], "保留内容")

    def test_0135_and_0136_migrate_legacy_plan_state_to_todo_and_remove_old_schema(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-0134-") as temporary:
            migrations_0134 = Path(temporary) / "migrations"
            migrations_0134.mkdir()
            for migration in load_migrations():
                if migration.version <= 134:
                    shutil.copy2(migration.path, migrations_0134 / migration.path.name)

            with closing(sqlite3.connect(":memory:")) as conn:
                conn.execute("PRAGMA foreign_keys = ON")
                apply_database_migrations(conn, migrations_dir=migrations_0134)
                conn.execute(
                    """
                    INSERT INTO agent_sessions(
                        id, title, session_mode, role_id, role_version, model_profile,
                        tool_profile_version, created_at_ms, updated_at_ms,
                        last_opened_at_ms, status
                    ) VALUES (
                        'session:plan-cutover', '迁移会话', 'assistant', 'companion', '1',
                        'test/model', 'test-tools', 1, 1, 1, 'idle'
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_sessions(
                        id, title, session_mode, role_id, role_version, model_profile,
                        tool_profile_version, created_at_ms, updated_at_ms,
                        last_opened_at_ms, status
                    ) VALUES (
                        'session:cancelled-plan', '已取消迁移会话', 'assistant',
                        'companion', '1', 'test/model', 'test-tools', 1, 1, 1, 'idle'
                    )
                    """
                )
                conn.executemany(
                    """
                    INSERT INTO agent_plan_events(
                        event_id, session_id, sequence, item_id, title, status,
                        created_at_ms, position, is_deleted
                    ) VALUES (?, 'session:plan-cutover', ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (
                        ("plan-item:1", 1, "item:1", "核对输入", "completed", 10, 1),
                        ("plan-item:2", 2, "item:2", "完成迁移", "in_progress", 20, 2),
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO agent_plan_state_events(
                        event_id, session_id, sequence, title, status, actor, note, created_at_ms
                    ) VALUES (
                        'plan-state:1', 'session:plan-cutover', 1, '旧执行计划',
                        'executing', 'agent', '', 15
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_plan_events(
                        event_id, session_id, sequence, item_id, title, status,
                        created_at_ms, position, is_deleted
                    ) VALUES (
                        'cancelled-plan-item:1', 'session:cancelled-plan', 1,
                        'item:cancelled', '不再执行', 'pending', 30, 0, 0
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO agent_plan_state_events(
                        event_id, session_id, sequence, title, status, actor, note, created_at_ms
                    ) VALUES (
                        'cancelled-plan-state:1', 'session:cancelled-plan', 1,
                        '已取消计划', 'cancelled', 'user', '', 31
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO work_documents(
                        document_id, authority_kind, authority_id, authority_revision,
                        authority_key, title, workspace_root, relative_path,
                        active_relative_path, archive_relative_path, content_sha256,
                        state, created_at_ms, updated_at_ms
                    ) VALUES (
                        'document:plan-cutover', 'session_plan', 'session:plan-cutover', 2,
                        'session_plan:session:plan-cutover', '执行文档', '/workspace',
                        'docs/agent/plan.md', 'docs/agent/plan.md',
                        'archive/plan.md', ?, 'active', 20, 20
                    )
                    """,
                    ("a" * 64,),
                )
                conn.execute(
                    """
                    INSERT INTO work_documents(
                        document_id, authority_kind, authority_id, authority_revision,
                        authority_key, title, workspace_root, relative_path,
                        active_relative_path, archive_relative_path, content_sha256,
                        state, terminal_receipt_id, created_at_ms, updated_at_ms
                    ) VALUES (
                        'document:cancelled-plan', 'session_plan',
                        'session:cancelled-plan', 4,
                        'session_plan:session:cancelled-plan', '已归档执行文档',
                        '/workspace', 'docs/agent/cancelled-plan.md',
                        'docs/agent/cancelled-plan.md',
                        'archive/cancelled-plan.md', ?, 'archived',
                        'receipt:legacy-plan-terminal', 30, 31
                    )
                    """,
                    ("c" * 64,),
                )
                conn.execute(
                    """
                    INSERT INTO work_document_terminal_receipts(
                        receipt_id, authority_kind, authority_id, authority_revision,
                        terminal_state, receipt_sha256, created_at_ms
                    ) VALUES (
                        'receipt:legacy-plan-terminal', 'session_plan',
                        'session:cancelled-plan', 4, 'cancelled', ?, 31
                    )
                    """,
                    ("d" * 64,),
                )

                result = apply_database_migrations(conn)

                self.assertEqual(
                    result.applied_versions,
                    tuple(range(135, 203)),
                )
                todo = conn.execute(
                    """
                    SELECT revision, phases_json, actor, operation, created_at_ms
                    FROM agent_todo_events
                    WHERE session_id = 'session:plan-cutover'
                    """
                ).fetchone()
                self.assertEqual(todo[0], 1)
                self.assertEqual(todo[2:], ("agent", "migrate", 20))
                self.assertEqual(
                    json.loads(todo[1]),
                    [
                        {
                            "name": "旧执行计划",
                            "tasks": [
                                {"content": "核对输入", "status": "completed"},
                                {"content": "完成迁移", "status": "in_progress"},
                            ],
                        }
                    ],
                )
                cancelled_todo = conn.execute(
                    """
                    SELECT phases_json
                    FROM agent_todo_events
                    WHERE session_id = 'session:cancelled-plan'
                    """
                ).fetchone()
                self.assertEqual(
                    json.loads(cancelled_todo[0])[0]["tasks"],
                    [{"content": "不再执行", "status": "abandoned"}],
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT terminal_receipt_id, authority_revision
                        FROM work_documents
                        WHERE document_id = 'document:cancelled-plan'
                        """
                    ).fetchone(),
                    ("", 1),
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT COUNT(*)
                        FROM work_document_terminal_receipts
                        WHERE authority_id = 'session:cancelled-plan'
                        """
                    ).fetchone()[0],
                    0,
                )
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertNotIn("agent_plan_events", tables)
                self.assertNotIn("agent_plan_state_events", tables)
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT authority_kind, authority_id, authority_revision, authority_key
                        FROM work_documents
                        WHERE document_id = 'document:plan-cutover'
                        """
                    ).fetchone(),
                    (
                        "session_todo",
                        "session:plan-cutover",
                        1,
                        "session_todo:session:plan-cutover",
                    ),
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO work_documents(
                            document_id, authority_kind, authority_id, authority_revision,
                            authority_key, workspace_root, relative_path,
                            active_relative_path, archive_relative_path, content_sha256,
                            state, created_at_ms, updated_at_ms
                        ) VALUES (
                            'document:legacy-plan', 'session_plan', 'session:plan-cutover', 2,
                            'session_plan:legacy', '/workspace', 'legacy.md', 'legacy.md',
                            'archive/legacy.md', ?, 'active', 20, 20
                        )
                        """,
                        ("b" * 64,),
                    )
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_production_0159_history_upgrades_append_only_to_room_review(self) -> None:
        migration_0159 = (
            DEFAULT_MIGRATIONS_DIR / "0159_room_work_authority_events.sql"
        )
        self.assertEqual(
            hashlib.sha256(migration_0159.read_bytes()).hexdigest(),
            "3984cd3c44285707745160001e5d85d06c9d52cc34618c5007fac9047498ac1e",
        )

        with tempfile.TemporaryDirectory(
            prefix="rag-ime-production-0159-"
        ) as temporary, closing(sqlite3.connect(":memory:")) as conn, conn:
            migrations_0159 = Path(temporary) / "migrations"
            migrations_0159.mkdir()
            for migration in load_migrations():
                if migration.version <= 159:
                    shutil.copy2(migration.path, migrations_0159 / migration.path.name)

            applied = apply_database_migrations(
                conn,
                migrations_dir=migrations_0159,
                applied_at_ms=159,
            )
            self.assertEqual(applied.current_version, 159)
            self.assertEqual(
                conn.execute(
                    "SELECT name, checksum FROM schema_migrations WHERE version = 159"
                ).fetchone(),
                (
                    "room_work_authority_events",
                    "3984cd3c44285707745160001e5d85d06c9d52cc34618c5007fac9047498ac1e",
                ),
            )
            conn.execute(
                "ALTER TABLE agent_room_work_items "
                "ADD COLUMN review_operability_verdict TEXT NOT NULL DEFAULT '' "
                "CHECK (review_operability_verdict IN "
                "('', 'passed', 'failed', 'unverified'))"
            )
            conn.execute(
                "ALTER TABLE agent_room_work_items "
                "ADD COLUMN proposed_operability_verdict TEXT NOT NULL DEFAULT '' "
                "CHECK (proposed_operability_verdict IN "
                "('', 'passed', 'failed', 'unverified'))"
            )

            upgraded = apply_database_migrations(conn, applied_at_ms=161)
            self.assertEqual(
                upgraded.applied_versions,
                tuple(range(160, 203)),
            )
            self.assertEqual(upgraded.current_version, 202)
            review_columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(agent_room_work_items)"
                )
            }
            self.assertTrue(
                {
                    "review_operability_verdict",
                    "review_requirement_verdict",
                    "review_evidence_refs_json",
                    "review_reason",
                    "reviewer_participant_id",
                    "reviewed_at_ms",
                    "proposed_operability_verdict",
                    "proposed_requirement_verdict",
                }.issubset(review_columns)
            )
            self.assertIsNotNone(
                conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'agent_room_partner_dispatches'"
                ).fetchone()
            )

    def test_0185_expands_run_kind_without_losing_runs_or_diffs(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-ime-production-0184-"
        ) as temporary, closing(sqlite3.connect(":memory:")) as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            migrations_0184 = Path(temporary) / "migrations"
            migrations_0184.mkdir()
            for migration in load_migrations():
                if migration.version <= 184:
                    shutil.copy2(migration.path, migrations_0184 / migration.path.name)
            initial = apply_database_migrations(
                conn,
                migrations_dir=migrations_0184,
                applied_at_ms=184,
            )
            self.assertEqual(initial.current_version, 184)
            conn.execute(
                """
                INSERT INTO memory_cleanup_runs(
                    run_id, created_at_ms, provider, model, status, summary,
                    metadata_json, owner_kind, owner_id, run_kind
                ) VALUES (
                    'memory_book_preserved', 1, 'test', 'test', 'draft',
                    'preserved', '{}', 'user', 'default', 'manual_curation'
                )
                """
            )
            conn.execute(
                """
                INSERT INTO memory_cleanup_diffs(
                    run_id, op, target_memory_id, payload_json, status,
                    created_at_ms, rollback_json
                ) VALUES (
                    'memory_book_preserved', 'noop', '', '{}', 'pending', 1, '{}'
                )
                """
            )
            conn.commit()

            upgraded = apply_database_migrations(conn, applied_at_ms=185)

            self.assertEqual(upgraded.applied_versions, (185, 186, 187, 188, 189, 190, 191, 192, 193, 194, 195, 196, 197, 198, 199, 200, 201, 202))
            self.assertEqual(
                conn.execute(
                    "SELECT run_kind FROM memory_cleanup_runs "
                    "WHERE run_id = 'memory_book_preserved'"
                ).fetchone()[0],
                "manual_curation",
            )
            self.assertEqual(
                conn.execute(
                    "SELECT COUNT(*) FROM memory_cleanup_diffs "
                    "WHERE run_id = 'memory_book_preserved'"
                ).fetchone()[0],
                1,
            )
            conn.execute(
                """
                INSERT INTO memory_cleanup_runs(
                    run_id, created_at_ms, metadata_json,
                    owner_kind, owner_id, run_kind
                ) VALUES (
                    'memory_book_catalog', 2, '{}',
                    'user', 'default', 'catalog_consolidation'
                )
                """
            )
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(conn.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_0160_resume_hook_does_not_swallow_other_sql_errors(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="rag-ime-migration-0160-error-"
        ) as temporary, closing(sqlite3.connect(":memory:")) as conn:
            migrations = Path(temporary) / "migrations"
            migrations.mkdir()
            (migrations / "0160_agent_room_work_item_review_verdicts.sql").write_text(
                "ALTER TABLE missing_table ADD COLUMN should_fail TEXT;\n",
                encoding="utf-8",
            )
            conn.execute("CREATE TABLE agent_room_work_items(id TEXT PRIMARY KEY)")

            with self.assertRaisesRegex(sqlite3.OperationalError, "missing_table"):
                apply_database_migrations(conn, migrations_dir=migrations)

            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = 160"
                ).fetchone()
            )
            review_columns = [
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(agent_room_work_items)"
                )
                if str(row[1]).startswith("review")
            ]
            self.assertEqual(
                review_columns,
                [
                    "review_operability_verdict",
                    "review_requirement_verdict",
                    "review_evidence_refs_json",
                    "review_reason",
                    "reviewer_participant_id",
                    "reviewed_at_ms",
                ],
            )
            with self.assertRaisesRegex(sqlite3.OperationalError, "missing_table"):
                apply_database_migrations(conn, migrations_dir=migrations)
            self.assertIsNone(
                conn.execute(
                    "SELECT 1 FROM schema_migrations WHERE version = 160"
                ).fetchone()
            )

    def test_checksum_change_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rag-ime-migrations-") as tmp:
            root = Path(tmp)
            migration = root / "0001_initial.sql"
            migration.write_text("CREATE TABLE sample(id INTEGER PRIMARY KEY);\n", encoding="utf-8")
            with closing(sqlite3.connect(":memory:")) as conn, conn:
                apply_database_migrations(conn, migrations_dir=root)
                migration.write_text("CREATE TABLE changed(id INTEGER PRIMARY KEY);\n", encoding="utf-8")

                with self.assertRaises(MigrationChecksumError):
                    apply_database_migrations(conn, migrations_dir=root)


if __name__ == "__main__":
    unittest.main()
