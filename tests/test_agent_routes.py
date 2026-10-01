from __future__ import annotations

import unittest
from http import HTTPStatus
from types import SimpleNamespace

from rag_ime.agent_routes import (
    agent_background_job_route,
    agent_approval_route,
    agent_artifact_route,
    agent_context_item_route,
    agent_context_trace_route,
    agent_media_route,
    agent_room_route,
    agent_room_work_route,
    agent_session_route,
    agent_wake_schedule_route,
    observability_trace_diagnostic_report_route,
)
from rag_ime.debug_server import DebugRequestHandler


class AgentRouteTests(unittest.TestCase):
    def test_codemode_route_decodes_the_existing_session_identity(self) -> None:
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent%3Aptc/codemode"),
            ("agent:ptc", "codemode"),
        )

    def test_codemode_http_post_reaches_the_session_policy_owner(self) -> None:
        calls: list[tuple[str, object]] = []

        def select_mode(session_id, payload):
            calls.append((session_id, payload))
            return {"ok": True, "sessionId": session_id, "codemodeMode": "only"}

        handler = DebugRequestHandler.__new__(DebugRequestHandler)
        handler.service = SimpleNamespace(
            agent=SimpleNamespace(select_codemode_mode=select_mode)
        )
        handler._authorize_gateway_request = lambda _method, _parsed: True
        handler._management_post_security_error = lambda _path, require_json=True: None
        handler._read_json = lambda: {"mode": "only"}
        written: list[tuple[HTTPStatus, dict[str, object]]] = []
        handler._write_json = lambda status, body: written.append((status, body))
        handler.path = "/api/agent/sessions/agent%3Aptc/codemode"
        handler.do_POST()

        self.assertEqual(calls, [("agent:ptc", {"mode": "only"})])
        self.assertEqual(written[0][0], HTTPStatus.OK)
        self.assertEqual(written[0][1]["codemodeMode"], "only")

    def test_trace_diagnostic_report_routes_are_strict_and_url_decoded(self) -> None:
        report_id = "trace-report:" + "a" * 32
        encoded = report_id.replace(":", "%3A")
        self.assertEqual(
            observability_trace_diagnostic_report_route(
                "/api/observability/trace-diagnostic-reports"
            ),
            ("", "collection"),
        )
        self.assertEqual(
            observability_trace_diagnostic_report_route(
                f"/api/observability/trace-diagnostic-reports/{encoded}"
            ),
            (report_id, "get"),
        )
        self.assertEqual(
            observability_trace_diagnostic_report_route(
                f"/api/observability/trace-diagnostic-reports/{encoded}/finalize"
            ),
            (report_id, "finalize"),
        )
        self.assertEqual(
            observability_trace_diagnostic_report_route(
                f"/api/observability/trace-diagnostic-reports/{encoded}/repair-authorize"
            ),
            (report_id, "repair-authorize"),
        )
        self.assertEqual(
            observability_trace_diagnostic_report_route(
                f"/api/observability/trace-diagnostic-reports/{encoded}/repair-verify"
            ),
            (report_id, "repair-verify"),
        )
        for path in (
            "/api/observability/trace-diagnostic-reports/",
            "/control/v1/observability/trace-diagnostic-reports",
            "/control/v1/observability/trace-diagnostic-reports/trace-report%3A" + "a" * 32,
            "/api/observability/trace-diagnostic-reports/a/b/c",
            "/api/observability/trace-diagnostic-reports//finalize",
        ):
            with self.subTest(path=path):
                self.assertEqual(
                    observability_trace_diagnostic_report_route(path),
                    ("", ""),
                )

    def test_trace_diagnostic_report_http_handlers_use_persisted_report_authority(self) -> None:
        report_id = "trace-report:" + "b" * 32
        calls: list[tuple[str, object]] = []

        class AgentRecorder:
            def list_trace_diagnostic_reports(self, payload):
                calls.append(("list", payload))
                return {"items": []}

            def trace_diagnostic_report(self, requested_report_id):
                calls.append(("get", requested_report_id))
                return {"reportId": requested_report_id}

            def create_trace_diagnostic_report(self, payload):
                calls.append(("create", payload))
                return {"reportId": report_id, "status": "generating"}

            def finalize_trace_diagnostic_report(self, requested_report_id, payload):
                calls.append(("finalize", (requested_report_id, payload)))
                return {"reportId": requested_report_id, "status": "completed"}

            def authorize_trace_diagnostic_repair(self, requested_report_id, payload):
                calls.append(("repair-authorize", (requested_report_id, payload)))
                return {"reportId": requested_report_id, "status": "completed"}

            def verify_trace_diagnostic_repair(self, requested_report_id, payload):
                calls.append(("repair-verify", (requested_report_id, payload)))
                return {"reportId": requested_report_id, "status": "completed"}

        handler = DebugRequestHandler.__new__(DebugRequestHandler)
        handler.service = SimpleNamespace(agent=AgentRecorder())
        handler._authorize_gateway_request = lambda _method, _parsed: True
        handler._serve_gateway_static = lambda _path: False
        handler._management_post_security_error = lambda _path, require_json=True: None
        handler._trace_repair_loopback_allowed = lambda: True
        written: list[tuple[HTTPStatus, dict[str, object]]] = []
        handler._write_json = lambda status, body: written.append((status, body))

        handler.path = "/api/observability/trace-diagnostic-reports?limit=10"
        handler.do_GET()
        handler.path = "/api/observability/trace-diagnostic-reports/" + report_id.replace(":", "%3A")
        handler.do_GET()
        handler._read_json = lambda: {"diagnosticSessionId": "agent:1", "title": "报告", "targets": []}
        handler.path = "/api/observability/trace-diagnostic-reports"
        handler.do_POST()
        handler._read_json = lambda: {"expectedRevision": 1}
        handler.path = (
            "/api/observability/trace-diagnostic-reports/"
            + report_id.replace(":", "%3A")
            + "/finalize"
        )
        handler.do_POST()

        handler._read_json = lambda: {"expectedRevision": 2, "repairSessionId": "agent:repair"}
        handler.path = (
            "/api/observability/trace-diagnostic-reports/"
            + report_id.replace(":", "%3A")
            + "/repair-authorize"
        )
        handler.do_POST()

        handler._read_json = lambda: {"expectedRevision": 3, "repairReceiptId": "repair-receipt:1"}
        handler.path = (
            "/api/observability/trace-diagnostic-reports/"
            + report_id.replace(":", "%3A")
            + "/repair-verify"
        )
        handler.do_POST()

        self.assertEqual(
            [item[0] for item in calls],
            ["list", "get", "create", "finalize", "repair-authorize", "repair-verify"],
        )
        self.assertEqual([int(item[0]) for item in written], [200, 200, 201, 200, 200, 200])

    def test_session_routes_are_strict_and_url_decoded(self) -> None:
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent%3A123/messages"),
            ("agent:123", "messages"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/prompt"),
            ("agent:123", "prompt"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/forks"),
            ("agent:123", "forks"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123"),
            ("agent:123", ""),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/models"),
            ("agent:123", "models"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/commands"),
            ("agent:123", "commands"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/model"),
            ("agent:123", "model"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/thinking"),
            ("agent:123", "thinking"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/intercom"),
            ("agent:123", "intercom"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/review"),
            ("agent:123", "review"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/workspace"),
            ("agent:123", "workspace"),
        )
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent:123/workspace-file"),
            ("agent:123", "workspace-file"),
        )

    def test_ui_response_session_route_is_strict_and_url_decoded(self) -> None:
        self.assertEqual(
            agent_session_route("/api/agent/sessions/agent%3A123/ui-response"),
            ("agent:123", "ui-response"),
        )
        self.assertEqual(
            agent_session_route(
                "/api/agent/sessions/agent%3A123/ui-response/extra"
            ),
            ("", ""),
        )

    def test_unknown_or_nested_routes_do_not_fall_through(self) -> None:
        for path in (
            "/api/agent/sessions",
            "/api/agent/sessions/",
            "/api/agent/sessions/agent:123/unknown",
            "/api/agent/sessions/agent:123/messages/extra",
            "/api/not-agent/sessions/agent:123/messages",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_session_route(path), ("", ""))

    def test_workspace_get_handlers_forward_decoded_session_and_query(self) -> None:
        calls: list[tuple[str, str, dict[str, object]]] = []

        class WorkspaceRecorder:
            def workspace_list(
                self,
                session_id: str,
                request: dict[str, object],
            ) -> dict[str, object]:
                calls.append(("list", session_id, request))
                return {"ok": True, "items": []}

            def workspace_read(
                self,
                session_id: str,
                request: dict[str, object],
            ) -> dict[str, object]:
                calls.append(("read", session_id, request))
                return {"ok": True, "content": "hello"}

        written: list[tuple[HTTPStatus, dict[str, object]]] = []
        handler = DebugRequestHandler.__new__(DebugRequestHandler)
        handler.service = SimpleNamespace(agent_tools=WorkspaceRecorder())
        handler._authorize_gateway_request = lambda _method, _parsed: True
        handler._serve_gateway_static = lambda _path: False
        handler._write_json = lambda status, body: written.append((status, body))

        handler.path = "/api/agent/sessions/agent%3A123/workspace?path=%2Fwork&depth=1&limit=40"
        handler.do_GET()
        handler.path = "/api/agent/sessions/agent%3A123/workspace-file?path=%2Fwork%2FREADME.md&offset=0&limit=65536"
        handler.do_GET()

        self.assertEqual(
            calls,
            [
                ("list", "agent:123", {"path": "/work", "depth": "1", "limit": "40"}),
                ("read", "agent:123", {"path": "/work/README.md", "offset": "0", "limit": "65536"}),
            ],
        )
        self.assertEqual(
            written,
            [
                (HTTPStatus.OK, {"ok": True, "items": []}),
                (HTTPStatus.OK, {"ok": True, "content": "hello"}),
            ],
        )

    def test_ui_response_handler_receives_decoded_session_id_and_payload(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []
        payload: dict[str, object] = {
            "requestId": "ui-request:456",
            "value": "approved",
        }

        class AgentRecorder:
            def resolve_ui_request(
                self,
                session_id: str,
                request: dict[str, object],
            ) -> dict[str, object]:
                calls.append((session_id, request))
                return {"ok": True, "requestId": request["requestId"]}

        handler = DebugRequestHandler.__new__(DebugRequestHandler)
        handler.service = SimpleNamespace(agent=AgentRecorder())
        handler.path = "/api/agent/sessions/agent%3A123/ui-response"
        handler._authorize_gateway_request = lambda _method, _parsed: True
        handler._management_post_security_error = (
            lambda _path, require_json=True: None
        )
        handler._read_json = lambda: payload
        written: list[tuple[HTTPStatus, dict[str, object]]] = []
        handler._write_json = lambda status, body: written.append((status, body))

        handler.do_POST()

        self.assertEqual(calls, [("agent:123", payload)])
        self.assertEqual(
            written,
            [(HTTPStatus.OK, {"ok": True, "requestId": "ui-request:456"})],
        )

    def test_background_job_routes_are_strict_and_url_decoded(self) -> None:
        job_id = "bg_0123456789abcdef0123456789abcdef"
        self.assertEqual(
            agent_background_job_route(
                f"/api/agent/sessions/agent%3A123/background-jobs/{job_id}"
            ),
            ("agent:123", job_id, "status"),
        )
        self.assertEqual(
            agent_background_job_route(
                f"/api/agent/sessions/agent%3A123/background-jobs/{job_id}/logs"
            ),
            ("agent:123", job_id, "logs"),
        )
        self.assertEqual(
            agent_background_job_route(
                f"/api/agent/sessions/agent%3A123/background-jobs/{job_id}/cancel"
            ),
            ("agent:123", job_id, "cancel"),
        )
        self.assertEqual(
            agent_background_job_route(
                "/api/agent/sessions/agent%3A123/background-jobs"
            ),
            ("agent:123", "", "collection"),
        )
        self.assertEqual(
            agent_background_job_route(
                "/api/agent/sessions/agent%3A123/background-jobs/start"
            ),
            ("agent:123", "", "start"),
        )
        for path in (
            f"/api/agent/sessions/agent:123/background-jobs/{job_id}/unknown",
            "/api/agent/sessions/agent:123/background-jobs/not-a-job",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_background_job_route(path), ("", "", ""))

    def test_context_routes_are_strict_and_url_decoded(self) -> None:
        self.assertEqual(
            agent_context_item_route("/api/agent/sessions/agent%3A123/context-items"),
            ("agent:123", "", "list"),
        )
        self.assertEqual(
            agent_context_item_route(
                "/api/agent/sessions/agent%3A123/context-items/context-item%3A456/ack"
            ),
            ("agent:123", "context-item:456", "ack"),
        )
        self.assertEqual(
            agent_context_trace_route("/api/agent/sessions/agent%3A123/context-traces"),
            ("agent:123", ""),
        )
        self.assertEqual(
            agent_context_trace_route(
                "/api/agent/sessions/agent%3A123/context-traces/context-trace%3A456"
            ),
            ("agent:123", "context-trace:456"),
        )
        for path in (
            "/api/agent/sessions/agent:123/context-items/item:1",
            "/api/agent/sessions/agent:123/context-items/item:1/delete",
            "/api/agent/sessions/agent:123/context-traces/trace:1/extra",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_context_item_route(path), ("", "", ""))
                self.assertEqual(agent_context_trace_route(path), ("", ""))

    def test_approval_routes_are_strict_and_url_decoded(self) -> None:
        self.assertEqual(
            agent_approval_route("/api/agent/approvals/approval%3A123/decision"),
            ("approval:123", "decision"),
        )
        self.assertEqual(
            agent_approval_route("/api/agent/approvals/approval%3A123/external-result"),
            ("approval:123", "external-result"),
        )
        self.assertEqual(
            agent_approval_route("/api/agent/approvals/approval:123"),
            ("approval:123", ""),
        )
        for path in (
            "/api/agent/approvals",
            "/api/agent/approvals/approval:123/unknown",
            "/api/agent/approvals/approval:123/decision/extra",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_approval_route(path), ("", ""))

    def test_media_routes_only_expose_receipt_or_content(self) -> None:
        self.assertEqual(
            agent_media_route("/api/agent/media/media_abc123456789/receipt"),
            ("media_abc123456789", "receipt"),
        )
        self.assertEqual(
            agent_media_route("/api/agent/media/media_abc123456789/content"),
            ("media_abc123456789", "content"),
        )
        self.assertEqual(
            agent_media_route("/api/agent/media/media_abc123456789/preview"),
            ("media_abc123456789", "preview"),
        )
        self.assertEqual(
            agent_media_route("/api/agent/media/media_abc123456789"),
            ("media_abc123456789", "receipt"),
        )
        for path in (
            "/api/agent/media",
            "/api/agent/media/media_abc123456789/open",
            "/api/agent/media/media_abc123456789/content/extra",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_media_route(path), ("", ""))

    def test_room_routes_are_strict_and_participant_aware(self) -> None:
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room%3A123/events"),
            ("room:123", "events"),
        )
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room:123/messages"),
            ("room:123", "messages"),
        )
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room%3A123/start-gate"),
            ("room:123", "start-gate"),
        )
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room%3A123/start-execution"),
            ("", ""),
        )
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room%3A123/steer"),
            ("room:123", "steer"),
        )
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room%3A123/snapshot"),
            ("room:123", "snapshot"),
        )
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room%3A123/conversation"),
            ("room:123", "conversation"),
        )
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room%3A123/history"),
            ("room:123", "history"),
        )
        self.assertEqual(agent_room_route("/api/agent/rooms/room:123"), ("room:123", ""))
        for path in (
            "/api/agent/rooms",
            "/api/agent/rooms/room:123/unknown",
            "/api/agent/rooms/room:123/events/extra",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_room_route(path), ("", ""))

    def test_room_work_routes_are_strict_and_url_decoded(self) -> None:
        self.assertEqual(
            agent_room_work_route(
                "/api/agent/rooms/room%3A123/work-items"
            ),
            ("room:123", "", "collection"),
        )

    def test_room_kernel_routes_are_not_part_of_the_public_api(self) -> None:
        self.assertEqual(
            agent_room_route("/api/agent/rooms/room:123/kernel/snapshot"),
            ("", ""),
        )
        self.assertEqual(
            agent_room_work_route(
                "/api/agent/rooms/room%3A123/work-items/work%3A456"
            ),
            ("room:123", "work:456", "get"),
        )
        self.assertEqual(
            agent_room_work_route(
                "/api/agent/rooms/room%3A123/work-items/work%3A456/reassign"
            ),
            ("room:123", "work:456", "reassign"),
        )
        for path in (
            "/api/agent/rooms/room:123/work-items//reassign",
            "/api/agent/rooms/room:123/work-items/work:456/unknown",
            "/api/agent/rooms/room:123/work-items/work:456/extra/path",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_room_work_route(path), ("", "", ""))

    def test_artifact_route_accepts_one_opaque_identifier_only(self) -> None:
        self.assertEqual(
            agent_artifact_route("/api/agent/artifacts/artifact%3Aabc123"),
            "artifact:abc123",
        )
        for path in (
            "/api/agent/artifacts",
            "/api/agent/artifacts/",
            "/api/agent/artifacts/artifact:abc/extra",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_artifact_route(path), "")

    def test_wake_schedule_routes_are_strict_and_url_decoded(self) -> None:
        self.assertEqual(
            agent_wake_schedule_route("/api/agent/wake-schedules/wake%3A123/runs"),
            ("wake:123", "runs"),
        )
        self.assertEqual(
            agent_wake_schedule_route("/api/agent/wake-schedules/wake%3A123/action"),
            ("wake:123", "action"),
        )
        self.assertEqual(
            agent_wake_schedule_route("/api/agent/wake-schedules/wake%3A123"),
            ("wake:123", ""),
        )
        for path in (
            "/api/agent/wake-schedules",
            "/api/agent/wake-schedules/wake:123/unknown",
            "/api/agent/wake-schedules/wake:123/runs/extra",
        ):
            with self.subTest(path=path):
                self.assertEqual(agent_wake_schedule_route(path), ("", ""))


if __name__ == "__main__":
    unittest.main()
