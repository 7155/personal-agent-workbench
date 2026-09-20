from __future__ import annotations

import unittest

from rag_ime.control_api import (
    ControlAccessContext,
    ControlApiError,
    ControlErrorCode,
    ControlPathId,
    ControlRequest,
    ControlScope,
    capability_feature_flags,
    default_route_policy,
)


class ControlRoutePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = default_route_policy()

    def test_manifest_covers_every_canonical_path_id_once(self) -> None:
        manifest = self.policy.manifest(include_targets=True)

        self.assertEqual(len(manifest), len(ControlPathId))
        self.assertEqual(
            {item["pathId"] for item in manifest},
            {path_id.value for path_id in ControlPathId},
        )
        self.assertEqual(
            {
                "planning.dashboard",
                "planning.mutation.preview",
                "planning.task.save",
                "planning.goal.save",
                "planning.task.action",
                "planning.taskEvent.undo",
                "planning.mutation.rollback",
                "memory.summary",
                "memory.pages",
                "memory.reference.get",
                "memory.graph.get",
                "memory.entity.get",
                "memory.edit",
                "memory.source.disposition",
                "memory.lifecycle.status",
                "memory.lifecycle.refresh",
                "memory.lifecycle.report",
                "memory.lifecycle.forget.preview",
                "memory.lifecycle.forget.apply",
                "memory.book.archive.preview",
                "memory.book.archive.apply",
                "memory.book.archive.rollback",
                "memory.activityTimeline.get",
                "memory.activityTimeline.calendar",
                "memory.activityTimeline.build",
                "memory.activityTimeline.approve",
                "memory.activityTimeline.reject",
                "history.page",
                "history.detail",
                "history.tombstone.preview",
                "history.tombstone.apply",
                "history.tombstone.rollback",
                "knowledge.start",
                "knowledge.cancel",
                "knowledge.status",
                "knowledge.routeStatus",
                "knowledge.database.apply.preview",
                "knowledge.database.draft.edit",
                "knowledge.database.apply",
                "knowledge.database.rollback",
                "knowledgeBases.list",
                "knowledgeVault.manage",
                "knowledgeBases.create",
                "knowledgeBases.get",
                "knowledgeBases.update",
                "knowledgeBases.delete.preview",
                "knowledgeBases.delete.apply",
                "knowledgeBases.documents.list",
                "knowledgeBases.document.import",
                "knowledgeBases.document.retry",
                "knowledgeBases.document.delete",
                "knowledgeBases.document.get",
                "knowledgeBases.document.source",
                "knowledgeBases.asset.get",
                "knowledgeBases.jobs.list",
                "knowledgeBases.job.cancel",
                "knowledgeBases.chunkPreview",
                "knowledgeBases.search",
                "knowledgeBases.find",
                "knowledgeBases.open",
                "knowledgeBases.graph.get",
                "knowledgeBases.graph.rebuild",
                "knowledgeBases.reindexPreview",
                "knowledgeBases.rebuild",
                "knowledgeWorker.health",
                "knowledgeParsers.list",
                "knowledgeEmbedding.profile",
                "knowledgeEmbedding.probe",
                "knowledgeEmbedding.impact",
                "diagnostics.runtime",
                "diagnostics.predictor",
                "diagnostics.models",
                "diagnostics.action.preview",
                "diagnostics.action.start",
                "diagnostics.action.job",
                "configuration.settings",
                "configuration.schema",
                "configuration.settings.preview",
                "configuration.settings.apply",
                "configuration.settings.rollback",
                "configuration.import.preview",
                "configuration.import.apply",
                "configuration.backup.export",
                "configuration.restore.preview",
                "configuration.restore.apply",
                "input.lexicon.review",
                "input.lexicon.apply",
                "input.lexicon.rollback",
            },
            {
                path_id.value
                for path_id in ControlPathId
                if path_id.name.startswith(
                    (
                        "PLANNING_",
                        "MEMORY_",
                        "HISTORY_",
                        "KNOWLEDGE_",
                        "DIAGNOSTICS_",
                        "CONFIGURATION_",
                        "INPUT_LEXICON_",
                    )
                )
            },
        )

    def test_input_lexicon_work_contract_is_allowlisted_and_local_only(self) -> None:
        review = ControlRequest(
            request_id="request-lexicon-review",
            path_id=ControlPathId.INPUT_LEXICON_REVIEW.value,
            query={"limit": 200, "project": "wisdom-weasel-rag-ime"},
        )
        apply = ControlRequest(
            request_id="request-lexicon-apply",
            path_id=ControlPathId.INPUT_LEXICON_APPLY.value,
            body={
                "reviewToken": "review-token",
                "selectedKeys": ["entry-1"],
                "confirmText": "APPLY_REVIEWED_RIME_LEXICON",
                "project": "wisdom-weasel-rag-ime",
                "limit": 200,
            },
        )
        rollback = ControlRequest(
            request_id="request-lexicon-rollback",
            path_id=ControlPathId.INPUT_LEXICON_ROLLBACK.value,
            body={"rollbackId": "rollback-1"},
        )

        for request in (review, apply, rollback):
            self.policy.authorize(request, ControlAccessContext.native())
            with self.assertRaises(ControlApiError) as raised:
                self.policy.authorize(
                    request,
                    ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
                )
            self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-lexicon-missing-confirm",
                    path_id=ControlPathId.INPUT_LEXICON_APPLY.value,
                    body={"reviewToken": "review-token", "selectedKeys": ["entry-1"]},
                ),
                ControlAccessContext.native(),
            )

    def test_agent_permission_mode_accepts_profile_and_explicit_allowlists(self) -> None:
        for body in (
            {
                "mode": "assistant",
                "toolProfileVersion": "subagent-readonly-v1",
                "toolAllowlistMode": "profile",
                "workspaceRoots": [],
            },
            {
                "mode": "coordinator",
                "toolProfileVersion": "control-center-v1",
                "toolAllowlistMode": "explicit",
                "allowedTools": ["workspace_search"],
                "workspaceRoots": ["/tmp/project"],
            },
            {
                "mode": "coordinator",
                "toolProfileVersion": "control-center-auto-approve-v1",
                "toolAllowlistMode": "profile",
                "workspaceRoots": ["/tmp/project"],
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
            },
        ):
            with self.subTest(body=body):
                self.policy.authorize(
                    ControlRequest(
                        request_id="request-agent-permission",
                        path_id=ControlPathId.AGENT_SESSION_MODE_UPDATE.value,
                        params={"sessionId": "session-1"},
                        body=body,
                    ),
                    ControlAccessContext.native(),
                )

    def test_role_book_review_allows_all_four_governed_proposal_groups(self) -> None:
        selection = {
            "roleId": "companion-present-v1",
            "roleVersion": "1",
            "revisionId": "",
            "draftId": "role-book-draft:1",
            "traitIndexes": [0],
            "capabilityIndexes": [1],
            "lessonIndexes": [2],
            "commitmentIndexes": [3],
        }
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_WRITE.value},
        )
        for path_id, body in (
            (ControlPathId.AGENT_ROLE_BOOK_ACTIVATION_PREVIEW.value, selection),
            (
                ControlPathId.AGENT_ROLE_BOOK_ACTIVATION_APPLY.value,
                {
                    **selection,
                    "previewToken": "preview-token",
                    "payloadSha256": "sha256:payload",
                    "confirmText": "apply",
                },
            ),
        ):
            with self.subTest(path_id=path_id):
                self.policy.authorize(
                    ControlRequest(
                        request_id=f"request-{path_id}",
                        path_id=path_id,
                        body=body,
                    ),
                    context,
                )

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-role-book-injected",
                    path_id=ControlPathId.AGENT_ROLE_BOOK_ACTIVATION_PREVIEW.value,
                    body={**selection, "proposalText": "client-owned"},
                ),
                context,
            )

    def test_configuration_file_migration_routes_are_strict_and_local_only(self) -> None:
        requests = (
            ControlRequest(
                request_id="request-config-preview",
                path_id=ControlPathId.CONFIGURATION_IMPORT_PREVIEW.value,
                body={"path": "/trusted/rag-ime.config.yaml"},
            ),
            ControlRequest(
                request_id="request-config-apply",
                path_id=ControlPathId.CONFIGURATION_IMPORT_APPLY.value,
                body={
                    "path": "/trusted/rag-ime.config.yaml",
                    "expectedRuntimeRevision": 4,
                    "previewToken": "sha256:preview",
                    "confirmText": "IMPORT RAG-IME CONFIGURATION",
                },
            ),
            ControlRequest(
                request_id="request-backup-export",
                path_id=ControlPathId.CONFIGURATION_BACKUP_EXPORT.value,
                body={"destination": "/trusted/Backups"},
            ),
            ControlRequest(
                request_id="request-restore-preview",
                path_id=ControlPathId.CONFIGURATION_RESTORE_PREVIEW.value,
                body={"path": "/trusted/backup.ragime-backup"},
            ),
            ControlRequest(
                request_id="request-restore-apply",
                path_id=ControlPathId.CONFIGURATION_RESTORE_APPLY.value,
                body={
                    "path": "/trusted/backup.ragime-backup",
                    "restoreToken": "a" * 64,
                    "confirmText": "RESTORE RAG-IME",
                    "expectedRuntimeRevision": 4,
                },
            ),
        )

        for request in requests:
            self.policy.authorize(request, ControlAccessContext.native())
            with self.assertRaises(ControlApiError) as raised:
                self.policy.authorize(
                    request,
                    ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
                )
            self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-restore-arbitrary",
                    path_id=ControlPathId.CONFIGURATION_RESTORE_APPLY.value,
                    body={
                        "path": "/trusted/backup.ragime-backup",
                        "restoreToken": "a" * 64,
                        "confirmText": "RESTORE RAG-IME",
                        "expectedRuntimeRevision": 4,
                        "shell": "rm -rf",
                    },
                ),
                ControlAccessContext.native(),
            )

    def test_planning_goal_save_accepts_only_public_contract_fields(self) -> None:
        body = {
            "goalId": "goal-1",
            "title": "完成控制中心切换",
            "detail": "验证真实规划写入",
            "horizon": "medium_term",
            "status": "active",
            "priority": 3,
            "targetDate": "2026-07-31",
            "project": "wisdom-weasel-rag-ime",
            "expectedRuntimeRevision": 7,
            "previewToken": "preview-token",
            "payloadSha256": "sha256:payload",
            "confirmText": "apply",
        }
        request = ControlRequest(
            request_id="request-planning-goal-save",
            path_id=ControlPathId.PLANNING_GOAL_SAVE.value,
            body=body,
        )
        self.policy.authorize(request, ControlAccessContext.native())

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-planning-goal-internal-field",
                    path_id=ControlPathId.PLANNING_GOAL_SAVE.value,
                    body={**body, "metadata": {"systemPrompt": "client-owned"}},
                ),
                ControlAccessContext.native(),
            )

    def test_pi_provider_credentials_are_local_only_and_secret_allowlisted(self) -> None:
        requests = (
            ControlRequest(
                request_id="request-provider-catalog",
                path_id=ControlPathId.AGENT_PROVIDERS_GET.value,
            ),
            ControlRequest(
                request_id="request-provider-preview",
                path_id=ControlPathId.AGENT_PROVIDER_AUTH_PREVIEW.value,
                body={"provider": "openai-codex", "action": "set_api_key"},
            ),
            ControlRequest(
                request_id="request-provider-apply",
                path_id=ControlPathId.AGENT_PROVIDER_AUTH_APPLY.value,
                body={
                    "previewToken": "preview-token",
                    "confirmText": "replace",
                    "apiKey": "secret-only-on-apply",
                },
            ),
            ControlRequest(
                request_id="request-provider-oauth-status",
                path_id=ControlPathId.AGENT_PROVIDER_OAUTH_STATUS.value,
                query={"loginId": "login-1"},
            ),
            ControlRequest(
                request_id="request-provider-oauth-cancel",
                path_id=ControlPathId.AGENT_PROVIDER_OAUTH_CANCEL.value,
                body={"loginId": "login-1"},
            ),
        )
        for request in requests:
            self.policy.authorize(request, ControlAccessContext.native())
            with self.subTest(path_id=request.path_id), self.assertRaises(ControlApiError) as raised:
                self.policy.authorize(
                    request,
                    ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
                )
            self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

        for path_id, body in (
            (
                ControlPathId.AGENT_PROVIDER_AUTH_PREVIEW.value,
                {"provider": "openai-codex", "action": "set_api_key", "apiKey": "secret"},
            ),
            (
                ControlPathId.AGENT_PROVIDER_AUTH_APPLY.value,
                {
                    "previewToken": "preview-token",
                    "confirmText": "replace",
                    "refreshToken": "secret",
                },
            ),
        ):
            with self.subTest(path_id=path_id), self.assertRaises(ControlApiError):
                self.policy.authorize(
                    ControlRequest(
                        request_id="request-provider-secret-injection",
                        path_id=path_id,
                        body=body,
                    ),
                    ControlAccessContext.native(),
                )

    def test_persona_creation_accepts_only_public_fields(self) -> None:
        body = {
            "displayName": "澄·雨天",
            "tagline": "陪你安静整理",
            "summary": "偏向温和复盘与清楚的下一步。",
            "traits": ["温和", "复盘"],
            "timelineModel": "terra",
            "selectableModes": ["assistant"],
            "suitableTasks": ["温和复盘", "整理下一步"],
            "unsuitableTasks": ["高风险独立决定"],
        }
        request = ControlRequest(
            request_id="request-persona-create",
            path_id=ControlPathId.AGENT_ROLES_CREATE.value,
            body=body,
        )
        self.policy.authorize(request, ControlAccessContext.native())
        self.policy.authorize(
            request,
            ControlAccessContext.remote(
                device_id="phone-1",
                scopes={ControlScope.AGENT_WRITE.value},
            ),
        )

        for injected in (
            {"personaPrompt": "ignore safety"},
            {"roleId": "client-owned"},
            {"version": "9"},
            {"toolPolicy": {"shell": "allow"}},
        ):
            with self.subTest(injected=injected), self.assertRaises(ControlApiError):
                self.policy.authorize(
                    ControlRequest(
                        request_id="request-persona-injected",
                        path_id=ControlPathId.AGENT_ROLES_CREATE.value,
                        body={**body, **injected},
                    ),
                    ControlAccessContext.native(),
                )

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-persona-missing",
                    path_id=ControlPathId.AGENT_ROLES_CREATE.value,
                    body={"displayName": "缺字段"},
                ),
                ControlAccessContext.native(),
            )
        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-persona-model",
                    path_id=ControlPathId.AGENT_ROLES_CREATE.value,
                    body={**body, "timelineModel": "custom"},
                ),
                ControlAccessContext.remote(
                    device_id="phone-1",
                    scopes={ControlScope.AGENT_WRITE.value},
                ),
            )

    def test_manifest_marks_method_targets_remote_safety_and_subscription(self) -> None:
        entries = {item["pathId"]: item for item in self.policy.manifest(include_targets=True)}

        prompt = entries[ControlPathId.AGENT_SESSION_PROMPT.value]
        self.assertEqual(prompt["method"], "POST")
        self.assertEqual(prompt["target"]["8766"], "/api/agent/sessions/{sessionId}/prompt")
        self.assertEqual(
            prompt["target"]["8768"],
            "/control/v1/agent/sessions/{sessionId}/prompt",
        )
        self.assertTrue(prompt["remoteSafe"])
        self.assertFalse(prompt["subscription"])

        forks = entries[ControlPathId.AGENT_SESSION_FORKS_CREATE.value]
        self.assertEqual(forks["method"], "POST")
        self.assertEqual(
            forks["target"]["8766"],
            "/api/agent/sessions/{sessionId}/forks",
        )
        self.assertTrue(forks["remoteSafe"])

        events = entries[ControlPathId.AGENT_SESSION_EVENTS.value]
        self.assertTrue(events["subscription"])
        self.assertIn("lastEventId", events["query"])

        control_events = entries[ControlPathId.CONTROL_EVENTS.value]
        self.assertEqual(control_events["target"]["8766"], "/api/agent/events")

        observation_snapshot = entries[ControlPathId.OBSERVABILITY_SNAPSHOT.value]
        self.assertEqual(
            observation_snapshot["target"]["8766"],
            "/api/observability/snapshot",
        )
        self.assertEqual(
            observation_snapshot["target"]["8768"],
            "/control/v1/observability/snapshot",
        )
        self.assertTrue(observation_snapshot["remoteSafe"])
        self.assertIn("runId", observation_snapshot["query"])

        observation_events = entries[ControlPathId.OBSERVABILITY_EVENTS.value]
        self.assertTrue(observation_events["subscription"])
        self.assertIn("lastEventId", observation_events["query"])
        self.assertIn("sessionId", observation_events["query"])
        self.assertIn("runId", observation_events["query"])

        templates = entries[ControlPathId.AGENT_SUBAGENTS_TEMPLATES.value]
        self.assertEqual(
            templates["target"]["8766"],
            "/api/agent/subagents/templates",
        )

        room_snapshot = entries[ControlPathId.AGENT_ROOM_SNAPSHOT.value]
        self.assertEqual(room_snapshot["method"], "GET")
        self.assertEqual(
            room_snapshot["target"]["8766"],
            "/api/agent/rooms/{roomId}/snapshot",
        )
        self.assertTrue(room_snapshot["remoteSafe"])
        room_history = entries[ControlPathId.AGENT_ROOM_HISTORY.value]
        self.assertEqual(
            room_history["target"]["8766"],
            "/api/agent/rooms/{roomId}/history",
        )
        self.assertEqual(room_history["query"], ["beforeSequence", "limit"])
        self.assertTrue(room_history["remoteSafe"])

        wake_action = entries[ControlPathId.AGENT_WAKE_SCHEDULE_ACTION.value]
        self.assertEqual(wake_action["method"], "POST")
        self.assertEqual(
            wake_action["target"]["8768"],
            "/control/v1/agent/wake-schedules/{scheduleId}/action",
        )
        self.assertTrue(wake_action["remoteSafe"])
        self.assertFalse(room_snapshot["subscription"])

        tools = entries[ControlPathId.AGENT_TOOLS_LIST.value]
        self.assertFalse(tools["remoteSafe"])

    def test_trace_diagnostic_reports_are_local_only_and_not_gateway_exposed(self) -> None:
        entries = {item["pathId"]: item for item in self.policy.manifest(include_targets=True)}
        remote_context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_READ.value, "*"},
        )

        for path_id in (
            ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORTS_LIST,
            ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORT_GET,
            ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORT_REPAIR_AUTHORIZE,
            ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORT_REPAIR_VERIFY,
        ):
            entry = entries[path_id.value]
            self.assertFalse(entry["remoteSafe"])
            self.assertIsNone(entry["target"]["8768"])
            with self.assertRaises(ControlApiError) as raised:
                self.policy.authorize(
                    ControlRequest(
                        request_id=f"remote-{path_id.value}",
                        path_id=path_id.value,
                        params=(
                            {"reportId": "trace-report:1"}
                            if path_id is not ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORTS_LIST
                            else {}
                        ),
                        query=(
                            {"limit": 10}
                            if path_id is ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORTS_LIST
                            else {}
                        ),
                        body=(
                            {
                                "expectedRevision": 2,
                                "findingId": "finding:1",
                                "sourceScope": "session:1",
                                "sourceTraceId": "trace:1",
                                "failureRef": "evidence:1",
                                "repairSessionId": "agent:repair",
                            }
                            if path_id is ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORT_REPAIR_AUTHORIZE
                            else {
                                "expectedRevision": 3,
                                "repairReceiptId": "repair-receipt:1",
                            }
                            if path_id is ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORT_REPAIR_VERIFY
                            else {}
                        ),
                    ),
                    remote_context,
                )
            self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

        local_list = self.policy.authorize(
            ControlRequest(
                request_id="local-trace-report-list",
                path_id=ControlPathId.OBSERVABILITY_TRACE_DIAGNOSTIC_REPORTS_LIST.value,
                query={"limit": 10},
            ),
            ControlAccessContext.native(),
        )
        self.assertEqual(local_list.local_8766_path, "/api/observability/trace-diagnostic-reports")
        self.assertIsNone(local_list.gateway_8768_path)

        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize_http(
                method="GET",
                path="/control/v1/observability/trace-diagnostic-reports",
                query={"limit": "10"},
                body={},
                context=remote_context,
            )
        self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_FOUND)

    def test_remote_prompt_accepts_only_explicit_pi_delivery_modes(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_WRITE.value},
        )
        for delivery in ("prompt", "steer", "followUp"):
            with self.subTest(delivery=delivery):
                self.policy.authorize(
                    ControlRequest(
                        request_id=f"request-{delivery}",
                        path_id=ControlPathId.AGENT_SESSION_PROMPT.value,
                        params={"sessionId": "session-1"},
                        body={
                            "message": "继续",
                            "clientMessageId": f"remote-{delivery}",
                            **(
                                {
                                    "retryOfClientMessageId": (
                                        "remote-original"
                                    )
                                }
                                if delivery == "prompt"
                                else {}
                            ),
                            "delivery": delivery,
                        },
                    ),
                    context,
                )
        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-invalid-delivery",
                    path_id=ControlPathId.AGENT_SESSION_PROMPT.value,
                    params={"sessionId": "session-1"},
                    body={
                        "message": "继续",
                        "clientMessageId": "remote-invalid",
                        "delivery": "later",
                    },
                ),
                context,
            )

    def test_unknown_path_id_fails_closed(self) -> None:
        with self.assertRaises(ControlApiError) as raised:
            self.policy.resolve("debug.anything")

        self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_FOUND)

    def test_request_payload_rejects_url_host_and_headers(self) -> None:
        for forbidden in ("url", "host", "headers"):
            with self.subTest(forbidden=forbidden), self.assertRaises(ControlApiError) as raised:
                ControlRequest.from_payload(
                    {
                        "id": "request-1",
                        "pathId": "system.health",
                        forbidden: "http://attacker.invalid",
                    }
                )
            self.assertEqual(raised.exception.code, ControlErrorCode.INVALID_REQUEST)

    def test_request_body_must_be_json_shaped_and_size_bounded(self) -> None:
        with self.assertRaises(ControlApiError) as raised:
            ControlRequest(
                request_id="request-1",
                path_id=ControlPathId.AGENT_SESSION_PROMPT.value,
                params={"sessionId": "session-1"},
                body={"message": object()},
            )
        self.assertEqual(raised.exception.code, ControlErrorCode.INVALID_REQUEST)

        with self.assertRaises(ControlApiError):
            ControlRequest(
                request_id="request-2",
                path_id=ControlPathId.AGENT_SESSION_PROMPT.value,
                params={"sessionId": "session-1"},
                body={"message": "x" * 2_000_001},
            )

    def test_path_parameters_reject_traversal_and_encoded_escape(self) -> None:
        for value in ("../../etc/passwd", "abc/def", "abc%2Fdef", "..", ""):
            request = ControlRequest(
                request_id="request-1",
                path_id=ControlPathId.AGENT_SESSION_SNAPSHOT.value,
                params={"sessionId": value},
            )
            with self.subTest(value=value), self.assertRaises(ControlApiError):
                self.policy.authorize(request, ControlAccessContext.native())

    def test_session_snapshot_accepts_optional_recent_view(self) -> None:
        for query in ({}, {"view": "recent"}):
            with self.subTest(query=query):
                request = ControlRequest(
                    request_id="request-session-snapshot",
                    path_id=ControlPathId.AGENT_SESSION_SNAPSHOT.value,
                    params={"sessionId": "session-1"},
                    query=query,
                )
                route = self.policy.authorize(
                    request,
                    ControlAccessContext.native(),
                )
                self.assertEqual(
                    route.path_id,
                    ControlPathId.AGENT_SESSION_SNAPSHOT,
                )

    def test_memory_page_kind_is_an_enum_not_an_arbitrary_path(self) -> None:
        for kind in ("apps", "books", "timelines"):
            allowed = ControlRequest(
                request_id=f"request-{kind}",
                path_id=ControlPathId.MEMORY_PAGES.value,
                params={"kind": kind},
            )
            self.policy.authorize(allowed, ControlAccessContext.native())
        self.policy.authorize(
            ControlRequest(
                request_id="request-evidence",
                path_id=ControlPathId.MEMORY_PAGES.value,
                params={"kind": "evidence"},
                query={
                    "ownerKind": "user",
                    "ownerId": "default",
                    "status": "not_for_memory",
                },
            ),
            ControlAccessContext.native(),
        )

        rejected = ControlRequest(
            request_id="request-2",
            path_id=ControlPathId.MEMORY_PAGES.value,
            params={"kind": "private-table"},
        )
        with self.assertRaises(ControlApiError):
            self.policy.authorize(rejected, ControlAccessContext.native())

    def test_memory_reference_kind_is_bounded_and_remote_read_only(self) -> None:
        for kind in (
            "event",
            "evidence",
            "atom",
            "book",
            "timeline",
            "role_book_revision",
        ):
            request = ControlRequest(
                request_id=f"request-reference-{kind}",
                path_id=ControlPathId.MEMORY_REFERENCE_GET.value,
                params={"kind": kind, "referenceId": f"{kind}:1"},
            )
            self.policy.authorize(request, ControlAccessContext.native())
            self.policy.authorize(
                request,
                ControlAccessContext.remote(
                    device_id="phone-1",
                    scopes={ControlScope.MEMORY_READ.value},
                ),
            )

        for kind, reference_id in (
            ("segment", "segment:1"),
            ("event", "../event:1"),
        ):
            with self.assertRaises(ControlApiError):
                self.policy.authorize(
                    ControlRequest(
                        request_id="request-reference-invalid",
                        path_id=ControlPathId.MEMORY_REFERENCE_GET.value,
                        params={"kind": kind, "referenceId": reference_id},
                    ),
                    ControlAccessContext.native(),
                )

    def test_memory_edit_is_local_only_and_requires_a_stable_identity(self) -> None:
        route = self.policy.resolve(ControlPathId.MEMORY_EDIT)
        self.assertEqual(route.method.value, "POST")
        self.assertEqual(route.local_8766_path, "/api/memory/edit")
        self.assertEqual(route.required_body, {"kind", "id"})
        self.assertEqual(
            route.body,
            {
                "kind",
                "id",
                "title",
                "text",
                "summary",
                "note",
                "description",
                "tags",
                "aliases",
                "type",
                "color",
                "reason",
                "active",
            },
        )

        allowed = ControlRequest(
            request_id="request-memory-edit",
            path_id=ControlPathId.MEMORY_EDIT.value,
            body={"kind": "books", "id": "book-1", "title": "长期计划"},
        )
        self.policy.authorize(allowed, ControlAccessContext.native())
        with self.assertRaises(ControlApiError) as remote_error:
            self.policy.authorize(
                allowed,
                ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
            )
        self.assertEqual(remote_error.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

        for body in (
            {"kind": "books", "title": "缺少稳定标识"},
            {"kind": "books", "id": "book-1", "schemaVersion": "client-owned"},
        ):
            with self.subTest(body=body), self.assertRaises(ControlApiError):
                self.policy.authorize(
                    ControlRequest(
                        request_id="request-memory-edit-rejected",
                        path_id=ControlPathId.MEMORY_EDIT.value,
                        body=body,
                    ),
                    ControlAccessContext.native(),
                )

    def test_memory_source_disposition_is_local_only_and_allowlisted(self) -> None:
        request = ControlRequest(
            request_id="request-memory-source",
            path_id=ControlPathId.MEMORY_SOURCE_DISPOSITION.value,
            body={
                "sourceId": "input-memory:42",
                "disposition": "not_for_memory",
            },
        )
        self.policy.authorize(request, ControlAccessContext.native())
        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                request,
                ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
            )
        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-memory-source-invalid",
                    path_id=ControlPathId.MEMORY_SOURCE_DISPOSITION.value,
                    body={
                        "sourceId": "input-memory:42",
                        "disposition": "pending",
                        "rawText": "not allowed",
                    },
                ),
                ControlAccessContext.native(),
            )

    def test_memory_maintenance_trigger_is_bounded_and_local_only(self) -> None:
        status = self.policy.resolve(ControlPathId.AGENT_MEMORY_MAINTENANCE_RUN)
        trigger = self.policy.resolve(
            ControlPathId.AGENT_MEMORY_MAINTENANCE_TRIGGER
        )

        self.assertEqual(status.method.value, "GET")
        self.assertEqual(status.required_query, set())
        self.assertEqual(
            status.query,
            {"runId", "jobId", "project", "limit", "projectionOnly"},
        )
        self.assertEqual(trigger.method.value, "POST")
        self.assertEqual(
            trigger.body,
            {
                "project",
                "ownerKind",
                "ownerId",
                "instruction",
                "manual",
                "maxSources",
            },
        )
        request = ControlRequest(
            request_id="request-memory-maintenance",
            path_id=ControlPathId.AGENT_MEMORY_MAINTENANCE_TRIGGER.value,
            body={"project": "sample-project", "manual": False},
        )
        self.policy.authorize(request, ControlAccessContext.native())
        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                request,
                ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
            )

    def test_memory_entity_allows_public_book_summary_but_not_raw_atoms(self) -> None:
        allowed = ControlRequest(
            request_id="request-book",
            path_id=ControlPathId.MEMORY_ENTITY_GET.value,
            params={"kind": "book", "entityId": "book:input"},
        )
        self.policy.authorize(allowed, ControlAccessContext.native())

        rejected = ControlRequest(
            request_id="request-atom",
            path_id=ControlPathId.MEMORY_ENTITY_GET.value,
            params={"kind": "atom", "entityId": "atom:visible"},
        )
        with self.assertRaises(ControlApiError):
            self.policy.authorize(rejected, ControlAccessContext.native())

    def test_subscriptions_require_explicit_last_event_id_even_when_empty(self) -> None:
        missing = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_SESSION_EVENTS.value,
            params={"sessionId": "session-1"},
        )
        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize(missing, ControlAccessContext.native())
        self.assertEqual(raised.exception.code, ControlErrorCode.INVALID_REQUEST)

        fresh = ControlRequest(
            request_id="request-2",
            path_id=ControlPathId.AGENT_SESSION_EVENTS.value,
            params={"sessionId": "session-1"},
            query={"lastEventId": ""},
        )
        self.policy.authorize(fresh, ControlAccessContext.native())

    def test_remote_http_routes_reverse_match_and_require_idempotency_keys(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_WRITE.value},
        )
        route = self.policy.authorize_http(
            method="POST",
            path="/api/agent/sessions/agent%3Aone/prompt",
            query={},
            body={"message": "hello", "clientMessageId": "phone-message-1"},
            context=context,
        )
        self.assertEqual(route.path_id, ControlPathId.AGENT_SESSION_PROMPT)

        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize_http(
                method="POST",
                path="/api/agent/sessions/agent%3Aone/prompt",
                query={},
                body={"message": "hello"},
                context=context,
            )
        self.assertEqual(raised.exception.code, ControlErrorCode.INVALID_REQUEST)

    def test_remote_http_matcher_rejects_local_only_and_unknown_routes(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={scope.value for scope in ControlScope},
        )
        with self.assertRaises(ControlApiError) as local_only:
            self.policy.authorize_http(
                method="GET",
                path="/api/agent/providers",
                query={},
                body={},
                context=context,
            )
        self.assertEqual(local_only.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

        with self.assertRaises(ControlApiError) as unknown:
            self.policy.authorize_http(
                method="GET",
                path="/api/private/debug",
                query={},
                body={},
                context=context,
            )
        self.assertEqual(unknown.exception.code, ControlErrorCode.ROUTE_NOT_FOUND)

    def test_remote_clients_need_a_paired_device_and_each_required_scope(self) -> None:
        request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_SESSIONS_LIST.value,
        )
        contexts = (
            ControlAccessContext.remote(),
            ControlAccessContext.remote(device_id="phone-1"),
            ControlAccessContext.remote(device_id="phone-1", scopes={ControlScope.CONTROL_READ.value}),
        )
        for context in contexts:
            with self.subTest(context=context), self.assertRaises(ControlApiError) as raised:
                self.policy.authorize(request, context)
            self.assertEqual(raised.exception.code, ControlErrorCode.SCOPE_REQUIRED)

        authorized = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_READ.value},
        )
        self.policy.authorize(request, authorized)

    def test_room_message_accepts_retry_work_item_and_attachment_ids(self) -> None:
        request = ControlRequest(
            request_id="request-room-work-item",
            path_id=ControlPathId.AGENT_ROOM_MESSAGE.value,
            params={"roomId": "room-1"},
            body={
                "message": "继续处理",
                "clientMessageId": "message-1",
                "retryOfRootId": "room-root:failed-1",
                "workItemId": "room-work:1",
                "attachmentIds": ["media_room_attachment01"],
            },
        )

        self.policy.authorize(request, ControlAccessContext.native())
        self.policy.authorize(
            request,
            ControlAccessContext.remote(
                device_id="phone-1",
                scopes={ControlScope.AGENT_WRITE.value},
            ),
        )

    def test_running_participant_steer_requires_exact_control_fields(self) -> None:
        body = {
            "action": "steer_participant",
            "rootId": "room-root:1",
            "participantId": "participant:worker",
            "clientActionId": "room-steer:1",
            "message": "补充这一项验收。",
        }
        request = ControlRequest(
            request_id="request-room-steer",
            path_id=ControlPathId.AGENT_ROOM_PARTICIPANT_STEER.value,
            params={"roomId": "room-1"},
            body=body,
        )
        remote = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_WRITE.value},
        )
        self.policy.authorize(request, ControlAccessContext.native())
        self.policy.authorize(request, remote)

        for invalid in (
            {key: value for key, value in body.items() if key != "message"},
            {key: value for key, value in body.items() if key != "clientActionId"},
            {**body, "dispatchId": "client-must-not-select-dispatch"},
            {**body, "action": "start_execution"},
        ):
            with self.subTest(body=invalid), self.assertRaises(ControlApiError):
                self.policy.authorize(
                    ControlRequest(
                        request_id="request-room-steer-invalid",
                        path_id=ControlPathId.AGENT_ROOM_PARTICIPANT_STEER.value,
                        params={"roomId": "room-1"},
                        body=invalid,
                    ),
                    remote,
                )

    def test_room_root_abort_requires_a_root_and_idempotency_identity(self) -> None:
        request = ControlRequest(
            request_id="request-room-root-abort",
            path_id=ControlPathId.AGENT_ROOM_ABORT.value,
            params={"roomId": "room-1"},
            body={
                "roomTurnId": "room-turn:1",
                "clientRequestId": "root-abort:1",
            },
        )

        self.policy.authorize(request, ControlAccessContext.native())
        self.policy.authorize(
            request,
            ControlAccessContext.remote(
                device_id="phone-1",
                scopes={ControlScope.AGENT_WRITE.value},
            ),
        )
        for body in (
            {"roomTurnId": "room-turn:1"},
            {"clientRequestId": "root-abort:1"},
            {
                "roomTurnId": "room-turn:1",
                "clientRequestId": "root-abort:1",
                "sessionId": "must-not-be-client-selected",
            },
        ):
            with self.subTest(body=body), self.assertRaises(ControlApiError):
                self.policy.authorize(
                    ControlRequest(
                        request_id="request-room-root-abort-invalid",
                        path_id=ControlPathId.AGENT_ROOM_ABORT.value,
                        params={"roomId": "room-1"},
                        body=body,
                    ),
                    ControlAccessContext.native(),
                )

    def test_room_work_item_actor_mutations_remain_local_only(self) -> None:
        requests = (
            ControlRequest(
                request_id="request-room-work-create",
                path_id=ControlPathId.AGENT_ROOM_WORK_ITEM_CREATE.value,
                params={"roomId": "room-1"},
                body={
                    "objective": "完成任务",
                    "expectedOutput": "一份结果",
                    "currentOwnerParticipantId": "participant-1",
                    "createdByParticipantId": "participant-1",
                    "clientMessageId": "create-1",
                },
            ),
            ControlRequest(
                request_id="request-room-work-reassign",
                path_id=ControlPathId.AGENT_ROOM_WORK_ITEM_REASSIGN.value,
                params={
                    "roomId": "room-1",
                    "workItemId": "room-work:1",
                },
                body={
                    "actorParticipantId": "participant-1",
                    "targetParticipantId": "participant-2",
                },
            ),
        )
        for request in requests:
            with self.subTest(path_id=request.path_id):
                self.policy.authorize(
                    request,
                    ControlAccessContext.native(),
                )
                with self.assertRaises(ControlApiError) as raised:
                    self.policy.authorize(
                        request,
                        ControlAccessContext.remote(
                            device_id="phone-1",
                            scopes={"*"},
                        ),
                    )
                self.assertEqual(
                    raised.exception.code,
                    ControlErrorCode.ROUTE_NOT_ALLOWED,
                )

    def test_history_detail_requires_event_id_and_history_read_scope(self) -> None:
        request = ControlRequest(
            request_id="request-history-detail",
            path_id=ControlPathId.HISTORY_DETAIL.value,
            query={"eventId": 81},
        )
        self.policy.authorize(request, ControlAccessContext.native())
        self.policy.authorize(
            request,
            ControlAccessContext.remote(
                device_id="phone-1",
                scopes={ControlScope.HISTORY_READ.value},
            ),
        )

        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize(
                request,
                ControlAccessContext.remote(
                    device_id="phone-1",
                    scopes={ControlScope.MEMORY_READ.value},
                ),
            )
        self.assertEqual(raised.exception.code, ControlErrorCode.SCOPE_REQUIRED)

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-history-detail-missing-id",
                    path_id=ControlPathId.HISTORY_DETAIL.value,
                ),
                ControlAccessContext.native(),
            )

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-history-detail-injected-context",
                    path_id=ControlPathId.HISTORY_DETAIL.value,
                    query={"eventId": 81, "includeContext": True},
                ),
                ControlAccessContext.native(),
            )

    def test_remote_clients_cannot_enable_local_only_routes_with_a_wildcard_scope(self) -> None:
        request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_SESSION_DELETE.value,
            params={"sessionId": "session-1"},
        )
        context = ControlAccessContext.remote(device_id="phone-1", scopes={"*", "agent.write"})

        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize(request, context)

        self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

    def test_configuration_settings_mutations_are_local_only(self) -> None:
        request = ControlRequest(
            request_id="request-configuration-preview",
            path_id=ControlPathId.CONFIGURATION_SETTINGS_PREVIEW.value,
            body={"changes": {"display.maxWidth": 640}, "expectedRuntimeRevision": 1},
        )

        self.policy.authorize(request, ControlAccessContext.native())
        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize(
                request,
                ControlAccessContext.remote(
                    device_id="phone-1",
                    scopes={"*"},
                ),
            )

        self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

    def test_activity_timeline_catch_up_flag_is_allowed_by_remote_contract(self) -> None:
        request = ControlRequest(
            request_id="request-activity-catch-up",
            path_id=ControlPathId.MEMORY_ACTIVITY_TIMELINE_BUILD.value,
            body={"date": "2026-08-12", "throughToday": True},
        )

        route = self.policy.authorize(
            request,
            ControlAccessContext.remote(
                device_id="phone-1",
                scopes={ControlScope.MEMORY_WRITE.value},
            ),
        )

        self.assertIn("throughToday", route.remote_body)

    def test_activity_timeline_selected_range_is_allowed_for_every_client(self) -> None:
        request = ControlRequest(
            request_id="request-activity-month",
            path_id=ControlPathId.MEMORY_ACTIVITY_TIMELINE_BUILD.value,
            body={
                "date": "2026-08-12",
                "throughToday": True,
                "rangeStartDate": "2026-08-01",
            },
        )
        for context in (
            ControlAccessContext.native(),
            ControlAccessContext.loopback_web(),
            ControlAccessContext.remote(
                device_id="phone-1",
                scopes={ControlScope.MEMORY_WRITE.value},
            ),
        ):
            with self.subTest(client=context.client_kind):
                self.policy.authorize(request, context)

    def test_remote_body_allowlist_blocks_workspace_paths_and_privileged_modes(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_WRITE.value},
        )
        room_request = ControlRequest(
            request_id="request-room",
            path_id=ControlPathId.AGENT_ROOMS_CREATE.value,
            body={
                "title": "workspace room",
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
                "workspaceRoots": ["/Users/example/project"],
            },
        )
        self.policy.authorize(room_request, ControlAccessContext.native())
        with self.assertRaises(ControlApiError):
            self.policy.authorize(room_request, context)

        workspace_request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_SESSIONS_CREATE.value,
            body={"title": "remote", "workspaceRoots": ["/Users/example"]},
        )
        with self.assertRaises(ControlApiError):
            self.policy.authorize(workspace_request, context)

        privileged_request = ControlRequest(
            request_id="request-2",
            path_id=ControlPathId.AGENT_SESSIONS_CREATE.value,
            body={"title": "remote", "mode": "coding"},
        )
        with self.assertRaises(ControlApiError):
            self.policy.authorize(privileged_request, context)

        assistant_request = ControlRequest(
            request_id="request-3",
            path_id=ControlPathId.AGENT_SESSIONS_CREATE.value,
            body={"title": "remote", "mode": "assistant"},
        )
        self.policy.authorize(assistant_request, context)

        trace_request = ControlRequest(
            request_id="request-trace",
            path_id=ControlPathId.AGENT_SESSIONS_CREATE.value,
            body={
                "title": "Trace diagnostic",
                "mode": "assistant",
                "_modelRoute": "traceDiagnostic",
                "executionMode": "read_only",
            },
        )
        self.policy.authorize(trace_request, ControlAccessContext.native())
        with self.assertRaises(ControlApiError):
            self.policy.authorize(trace_request, context)

    def test_remote_session_list_cannot_request_internal_sessions(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_READ.value},
        )
        request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_SESSIONS_LIST.value,
            query={"includeInternal": True},
        )

        with self.assertRaises(ControlApiError):
            self.policy.authorize(request, context)

    def test_remote_session_and_room_lists_accept_stable_history_cursors(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_READ.value},
        )

        for path_id in (
            ControlPathId.AGENT_SESSIONS_LIST.value,
            ControlPathId.AGENT_ROOMS_LIST.value,
        ):
            request = ControlRequest(
                request_id=f"request-{path_id}",
                path_id=path_id,
                query={
                    "includeArchived": True,
                    "limit": 100,
                    "beforeUpdatedAtMs": 1_725_000_000_000,
                    "beforeId": "history:cursor",
                },
            )

            route = self.policy.authorize(request, context)

            remote_query = route.remote_query if route.remote_query is not None else route.query
            self.assertIn("beforeUpdatedAtMs", remote_query)
            self.assertIn("beforeId", remote_query)

    def test_remote_configuration_update_cannot_spoof_audit_identity(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_WRITE.value},
        )
        request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_CONFIGURATION_UPDATE.value,
            body={
                "expectedRevision": 4,
                "changes": {"defaultModel": "local"},
                "updatedBy": "spoofed-device",
            },
        )

        with self.assertRaises(ControlApiError):
            self.policy.authorize(request, context)

    def test_bounded_artifact_reads_require_session_ownership(self) -> None:
        request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_ARTIFACT_GET.value,
            params={"artifactId": "artifact-1"},
        )

        with self.assertRaises(ControlApiError):
            self.policy.authorize(request, ControlAccessContext.native())

    def test_intercom_rejects_client_supplied_source_identity(self) -> None:
        request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_SESSION_INTERCOM_SEND.value,
            params={"sessionId": "session-1"},
            body={
                "kind": "send",
                "clientMessageId": "message-1",
                "content": "hello",
                "sourceSessionId": "session-2",
            },
        )

        with self.assertRaises(ControlApiError):
            self.policy.authorize(request, ControlAccessContext.native())

    def test_deep_search_remains_local_only(self) -> None:
        request = ControlRequest(
            request_id="request-1",
            path_id=ControlPathId.AGENT_DEEP_SEARCH.value,
            body={"query": "hello", "privacyDisposition": "allowed"},
        )
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.AGENT_WRITE.value},
        )

        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize(request, context)
        self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

    def test_remote_manifest_contains_only_explicit_remote_safe_routes_and_no_targets(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={
                ControlScope.CONTROL_READ.value,
                ControlScope.AGENT_READ.value,
            },
        )
        manifest = self.policy.manifest(context=context)

        self.assertTrue(manifest)
        self.assertTrue(all(item["remoteSafe"] for item in manifest))
        self.assertTrue(all("target" not in item for item in manifest))
        self.assertNotIn(ControlPathId.AGENT_TOOLS_LIST.value, {item["pathId"] for item in manifest})

    def test_remote_manifest_does_not_leak_local_only_feature_flags(self) -> None:
        context = ControlAccessContext.remote(
            device_id="phone-1",
            scopes={ControlScope.CONTROL_READ.value},
        )
        feature_flags = capability_feature_flags(
            self.policy.manifest(context=context, include_targets=False)
        )

        self.assertTrue(feature_flags["subscriptions"])
        self.assertFalse(feature_flags["configurationSettingsWorkContract"])
        self.assertFalse(feature_flags["managementWorkContract"])
        self.assertFalse(feature_flags["planningWorkContract"])

    def test_no_generic_or_privileged_escape_route_is_registered(self) -> None:
        manifest = self.policy.manifest(include_targets=True)
        path_ids = {str(item["pathId"]).lower() for item in manifest}
        targets = {
            str(path).lower()
            for item in manifest
            for path in item["target"].values()
            if path != "facade"
        }

        for forbidden in ("shell", "keychain", "file.read", "file.write"):
            self.assertFalse(any(forbidden in path_id for path_id in path_ids))
        self.assertNotIn("/api/action", targets)
        self.assertIn("/api/configuration/import-apply", targets)
        self.assertNotIn("agent.media.import", path_ids)
        self.assertNotIn("agent.tool.execute", path_ids)
        self.assertNotIn("agent.session.get", path_ids)

        entries = {item["pathId"]: item for item in manifest}
        for path_id in (
            "knowledge.database.apply.preview",
            "knowledge.database.draft.edit",
            "knowledge.database.apply",
            "knowledge.database.rollback",
        ):
            self.assertFalse(entries[path_id]["remoteSafe"])

        apply_route = self.policy.resolve(ControlPathId.KNOWLEDGE_DATABASE_APPLY)
        self.assertEqual(
            apply_route.required_body,
            {"runId", "confirm", "previewToken", "payloadSha256", "expectedRuntimeRevision"},
        )

    def test_document_knowledge_management_routes_are_local_only(self) -> None:
        manifest = {item["pathId"]: item for item in self.policy.manifest(include_targets=True)}
        document_routes = {
            path_id.value
            for path_id in ControlPathId
            if path_id.value.startswith("knowledgeBases.")
            or path_id in {ControlPathId.KNOWLEDGE_WORKER_HEALTH, ControlPathId.KNOWLEDGE_PARSERS_LIST}
        }
        self.assertEqual(len(document_routes), 25)
        for path_id in document_routes:
            self.assertFalse(manifest[path_id]["remoteSafe"])
            self.assertIsNone(manifest[path_id]["target"]["8768"])

        upload = self.policy.authorize(
            ControlRequest(
                request_id="request-knowledge-upload",
                path_id=ControlPathId.KNOWLEDGE_BASES_DOCUMENT_IMPORT.value,
                params={"kbId": "kb_docs"},
                query={"fileName": "manual.pdf", "mimeType": "application/pdf"},
            ),
            ControlAccessContext.native(),
        )
        self.assertEqual(upload.local_8766_path, "/api/knowledge-bases/{kbId}/documents/import")
        asset = self.policy.resolve(ControlPathId.KNOWLEDGE_BASES_ASSET_GET)
        self.assertTrue(asset.binary)
        self.assertIsNone(asset.gateway_8768_path)
        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-invalid-asset",
                    path_id=ControlPathId.KNOWLEDGE_BASES_ASSET_GET.value,
                    params={"kbId": "kb_docs", "fileId": "file_manual", "assetId": "not-a-sha"},
                ),
                ControlAccessContext.native(),
            )

        for request in (
            ControlRequest(
                request_id="request-config-update",
                path_id=ControlPathId.KNOWLEDGE_BASES_UPDATE.value,
                params={"kbId": "kb_docs"},
                body={
                    "chunkingConfig": {"chunkSize": 1200, "overlap": 160},
                    "retrievalConfig": {"mode": "hybrid", "topK": 12, "threshold": 0.2},
                    "expectedRevision": "revision-1",
                },
            ),
            ControlRequest(
                request_id="request-search-threshold",
                path_id=ControlPathId.KNOWLEDGE_BASES_SEARCH.value,
                params={"kbId": "kb_docs"},
                body={"query": "winter velocity", "topK": 8, "mode": "hybrid", "threshold": 0.2},
            ),
            ControlRequest(
                request_id="request-rebuild",
                path_id=ControlPathId.KNOWLEDGE_BASES_REBUILD.value,
                params={"kbId": "kb_docs"},
                body={
                    "previewToken": "preview-token",
                    "payloadSha256": "a" * 64,
                    "expectedRevision": "revision-1",
                    "confirmText": "REBUILD",
                },
            ),
        ):
            self.policy.authorize(request, ControlAccessContext.native())
        with self.assertRaises(ControlApiError) as raised:
            self.policy.authorize(
                ControlRequest(
                    request_id="request-remote-knowledge",
                    path_id=ControlPathId.KNOWLEDGE_BASES_LIST.value,
                ),
                ControlAccessContext.remote(device_id="phone-1", scopes={"*"}),
            )
            self.assertEqual(raised.exception.code, ControlErrorCode.ROUTE_NOT_ALLOWED)

    def test_document_detail_allows_independent_chunk_and_markdown_windows(self) -> None:
        request = ControlRequest(
            request_id="request-knowledge-document-windows",
            path_id=ControlPathId.KNOWLEDGE_BASES_DOCUMENT_GET.value,
            params={"kbId": "kb-docs", "fileId": "file-manual"},
            query={"offset": 400, "limit": 200, "lineOffset": 800, "lineLimit": 200},
        )
        self.policy.authorize(request, ControlAccessContext.native())

        with self.assertRaises(ControlApiError):
            self.policy.authorize(
                ControlRequest(
                    request_id="request-knowledge-document-unknown-window",
                    path_id=ControlPathId.KNOWLEDGE_BASES_DOCUMENT_GET.value,
                    params={"kbId": "kb-docs", "fileId": "file-manual"},
                    query={"lineCursor": 800},
                ),
                ControlAccessContext.native(),
            )


if __name__ == "__main__":
    unittest.main()
