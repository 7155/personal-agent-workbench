from __future__ import annotations

from pathlib import Path
import time
import unittest
import json

from rag_ime.agent_background_jobs import AgentBackgroundJobService
from rag_ime.agent_tools import ControlToolGateway
from tests import test_agent_tools as tool_fixtures


class _RecordingQuickActionJobs:
    def __init__(self, existing: list[dict[str, object]] | None = None) -> None:
        self.items = list(existing or [])
        self.started: list[tuple[str, object, dict[str, object]]] = []

    def list(self, session_id: str, *, limit: object = 50, status: object = "") -> dict[str, object]:
        return {"items": list(self.items), "sessionId": session_id, "activeCount": len(self.items), "ok": True}

    def start(self, session_id: str, prepared: object, **kwargs: object) -> dict[str, object]:
        self.started.append((session_id, prepared, dict(kwargs)))
        job = {
            "jobId": "bg_0123456789abcdef0123456789abcdef",
            "sessionId": session_id,
            "status": "running",
            "label": kwargs.get("label", ""),
            "command": prepared.command,
            "cwd": str(prepared.cwd),
        }
        self.items = [job]
        return {"schemaVersion": "rag-ime.agent-background-job-start-receipt.v1", "ok": True, "summary": "started", "job": job}


class WorkspaceJobIdempotencyGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = tool_fixtures.ControlToolGatewayTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.workspace = Path(self.fixture.tmp.name) / "idempotent-command"
        self.workspace.mkdir()
        self.session = self.fixture.store.create(
            title="job identity contract",
            mode="coordinator",
            tool_profile_version="control-center-auto-approve-v1",
            execution_mode="full_trust",
            workspace_roots=[str(self.workspace), "/"],
        )
        self.fixture._start_todo(str(self.session["id"]))
        self.jobs = AgentBackgroundJobService(
            Path(self.fixture.tmp.name) / "rag-ime.sqlite",
            events=lambda *args, **kwargs: None,
        )
        self.jobs.initialize()
        self.addCleanup(self.jobs.close)
        self.gateway = ControlToolGateway(
            sessions=self.fixture.store,
            management=self.fixture.management,
            core=tool_fixtures._Core(),
            project="personal-agent-workbench",
            facade=tool_fixtures._Facade(),
            workspace_harness=self.jobs.workspace_harness,
            background_jobs=self.jobs,
        )

    def _start(self):
        prepared = self.gateway._prepare_background_job_start(
            session_id=str(self.session["id"]),
            args={
                "command": "printf 'effect\\n' >> effect.txt",
                "cwd": str(self.workspace),
                "timeoutSeconds": 5,
                "idempotencyKey": "same-start-after-lost-reply",
            },
            risk_level="R2",
        )
        approval = prepared["approval"]
        self.assertEqual(
            approval["preview"]["actionPayload"].get("idempotencyKey"),
            "same-start-after-lost-reply",
        )
        decided = self.fixture.store.decide_approval(
            approval["approvalId"], approved=True, payload_sha256=approval["payloadSha256"]
        )
        return self.gateway.apply_approval(decided)

    def test_start_identity_survives_preview_and_replays_without_new_mutation(self) -> None:
        first = self._start()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            job = self.jobs.status(str(self.session["id"]), first["job"]["jobId"])["job"]
            if job["status"] not in {"queued", "running", "cancelling"}:
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed")
        replay = self._start()
        self.assertEqual(first["job"]["jobId"], replay["job"]["jobId"])
        self.assertTrue(replay["replayed"])
        self.assertFalse(replay["mutationApplied"])
        self.assertEqual((self.workspace / "effect.txt").read_text().splitlines(), ["effect"])

    def test_project_quick_action_maps_only_declared_scripts_and_reuses_active_job(self) -> None:
        project_id = "project-quick-action"
        self.workspace.joinpath("package.json").write_text(
            json.dumps({"scripts": {"start": "node scripts/serve.mjs", "test": "node --test tests/*.test.mjs"}}),
            encoding="utf-8",
        )
        session = self.fixture.store.create(
            title="project quick action",
            mode="coordinator",
            execution_mode="workspace_managed",
            workspace_roots=[str(self.workspace)],
        )
        jobs = _RecordingQuickActionJobs()
        gateway = ControlToolGateway(
            sessions=self.fixture.store,
            management=self.fixture.management,
            core=tool_fixtures._Core(),
            project="personal-agent-workbench",
            facade=tool_fixtures._Facade(),
            workspace_harness=self.jobs.workspace_harness,
            background_jobs=jobs,
        )

        first = gateway.start_project_quick_action(
            str(session["id"]),
            {
                "action": "preview",
                "projectId": project_id,
                "cwd": str(self.workspace),
                "previewUrl": "http://127.0.0.1:5392/dev/ui.html",
            },
        )
        self.assertEqual(first["quickAction"]["script"], "start")
        self.assertEqual(jobs.started[0][1].command, "npm run start")
        self.assertEqual(first["quickAction"]["previewUrl"], "http://127.0.0.1:5392/dev/ui.html")

        root_preview = gateway.start_project_quick_action(
            str(session["id"]),
            {
                "action": "preview",
                "projectId": project_id,
                "cwd": str(self.workspace),
                "previewUrl": "http://127.0.0.1:5392/",
            },
        )
        self.assertEqual(root_preview["quickAction"]["previewUrl"], "http://127.0.0.1:5392/")

        replay = gateway.start_project_quick_action(
            str(session["id"]),
            {"action": "preview", "projectId": project_id, "cwd": str(self.workspace)},
        )
        self.assertTrue(replay["deduplicated"])
        self.assertEqual(replay["job"]["jobId"], first["job"]["jobId"])
        self.assertEqual(len(jobs.started), 1)

        with self.assertRaisesRegex(ValueError, "preview or checks"):
            gateway.start_project_quick_action(
                str(session["id"]),
                {"action": "unknown", "projectId": project_id, "cwd": str(self.workspace)},
            )


if __name__ == "__main__":
    unittest.main()
