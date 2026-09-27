from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path

from scripts import build_paw_backend_web_model_package as package_builder


class PawBackendWebModelPackageTests(unittest.TestCase):
    def test_room_jev_scope_contains_live_seams_and_only_product_skills(self) -> None:
        paths = package_builder.collect_room_jev_paths(package_builder.PAW_ROOT)
        selected = {p.relative_to(package_builder.PAW_ROOT).as_posix() for p in paths}
        self.assertIn("rag_ime/rooms/session_dispatch.py", selected)
        self.assertIn("rag_ime/jev_tasks/room_driver.py", selected)
        self.assertIn("rag_ime/jev_tasks/application.py", selected)
        self.assertIn("rag_ime/db/migrations/0208_jev_host_events.sql", selected)
        self.assertIn("tests/test_jev_host_application.py", selected)
        self.assertIn("scripts/canary_jev_room.py", selected)
        self.assertIn("tests/test_pi_exact_turn_cancellation.py", selected)
        self.assertIn("rag_ime/browser_control.py", selected)
        self.assertIn("scripts/build_ego_browser_runtime.py", selected)
        self.assertIn("integrations/ego-browser/upstream/package/ego-browser/src/run.ts", selected)
        self.assertFalse(any("/dist/" in p or "/node_modules/" in p for p in selected))
        self.assertIn("integrations/pi/skills/facilitate-room/references/runtime-operations.md", selected)
        self.assertFalse(any(p.startswith("control-center-web/") for p in selected))
        self.assertFalse(any(p.endswith((".sqlite", ".jsonl", ".zip")) for p in selected))
        self.assertEqual(len(selected), len(paths))

    def test_room_jev_frontend_scope_includes_ui_state_transport_styles_and_imports(self) -> None:
        paths = package_builder.collect_room_jev_frontend_paths(package_builder.PAW_ROOT)
        selected = {p.relative_to(package_builder.PAW_ROOT / "control-center-web").as_posix() for p in paths}
        for expected in (
            "src/paw-os/apps/PawRoomWorkspace.tsx", "src/paw-os/apps/PawAgentApp.tsx",
            "src/features/rooms/state/live-store.ts", "src/features/semantic-workspace/AgentModeSwitch.tsx",
            "src/features/rooms/composer/RoomComposer.test.tsx", "src/platform/http-transport.ts",
            "src/platform/sse.ts", "src/contracts/room-reducer.ts", "src/paw-os/styles/paw-os-room-progress.css",
            "package.json", "src/features/agent/public-error.ts",
            "index.html", "src/main.tsx", "src/app/App.tsx", "src/app/App-loading.test.tsx",
            "src/paw-os/apps/PawJevWorkspace.test.tsx",
            "e2e/fixtures/jev-execution.tsx",
        ):
            self.assertIn(expected, selected)
        self.assertFalse(any("node_modules" in p or p.endswith(".jsonl") for p in selected))
        self.assertEqual(len(paths), len(selected))

    def test_source_filter_uses_repository_relative_private_directory(self) -> None:
        with tempfile.TemporaryDirectory() as raw_temp:
            root = Path(raw_temp) / "private" / "pi"
            source = root / "src" / "host.ts"
            source.parent.mkdir(parents=True)
            source.write_text("export const version = 1;\n")
            staging = Path(raw_temp) / "staging"
            receipts: list[package_builder.SourceReceipt] = []
            package_builder.add_text_source(
                staging, receipts, repository="pi", category="runtime",
                root=root, path=source, target="code/pi/src/host.ts",
                git_status="tracked-clean", roots=(root,),
            )
            self.assertEqual((staging / "code/pi/src/host.ts").read_text(), source.read_text())
            self.assertEqual(receipts[0].source_relative, "src/host.ts")
            secret = root / "private" / "hidden.ts"
            secret.parent.mkdir()
            secret.write_text("private fixture")
            with self.assertRaisesRegex(ValueError, "ineligible required source"):
                package_builder.add_text_source(
                    staging, receipts, repository="pi", category="runtime",
                    root=root, path=secret, target="code/pi/private/hidden.ts",
                    git_status="tracked-clean", roots=(root,),
                )

    def test_agent_lab_index_projects_current_four_project_evidence(self) -> None:
        sources = (
            "eval/interview-metrics/runs/agent-lab-optimal-path-enterpriseops-luna-prompt-20260904.v1.json",
            "eval/interview-metrics/runs/agent-lab-optimal-path-cloudops-luna-prompt-20260904.v1.json",
            "eval/interview-metrics/runs/memory-maintenance-sol-to-luna-model-only-optimization-20260904.r1.json",
            "eval/interview-metrics/runs/enterprise-rag-answer-evidence-sol-max-frozen-v19-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
            "eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-model-only-v19-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
            "eval/interview-metrics/runs/enterprise-rag-answer-evidence-luna-max-coverage-balanced-v4-r4-attention-r6-exact-offline-rescore-20260905.v1.json",
            "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-sol-max-frozen-v19-20260904.r4.v1.json",
            "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-luna-max-model-only-v19-20260904.r4.v1.json",
            "eval/interview-metrics/runs/agent-lab-cost-enterprise-rag-luna-max-coverage-balanced-v4-20260904.r4.v1.json",
        )

        with tempfile.TemporaryDirectory() as raw_temp:
            staging = Path(raw_temp)
            receipts: list[package_builder.SourceReceipt] = []
            for source_relative in sources:
                source = package_builder.PAW_ROOT / source_relative
                target_relative = f"evaluation/agent-lab-results/{source_relative}"
                target = staging / target_relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                receipts.append(
                    package_builder.SourceReceipt(
                        repository="paw",
                        category="agent-lab-evaluation-result",
                        source_relative=source_relative,
                        target=target_relative,
                        git_status="test-fixture",
                        original_bytes=source.stat().st_size,
                        original_sha256=digest,
                        package_bytes=target.stat().st_size,
                        package_sha256=digest,
                        redactions={},
                    )
                )

            rendered = package_builder.render_agent_lab_evidence_index(staging, receipts)

        self.assertIn("Sol baseline -> Luna model-only Reject -> Luna + Prompt", rendered)
        self.assertIn("exact citation facts 7/9 -> 8/9 -> 9/9", rendered)
        self.assertIn("$2.170603 -> $0.1029376; -95.2576% (21.0866x)", rendered)
        self.assertIn("$0.10594896 -> $0.1029376; -2.8423%", rendered)
        self.assertIn("Validation-quality Keep", rendered)
        self.assertIn("rescoredDecision=reject", rendered)
        self.assertIn("rescoredDecision=keep", rendered)
        self.assertIn("candidate-aware", rendered)
        self.assertIn("Held-out remains unopened", rendered)
        self.assertNotIn("Stage 1 preflight only", rendered)
        self.assertNotIn("three-qualified/one-invalid", rendered)

        for source_relative in sources[3:]:
            self.assertIn(source_relative, package_builder.AGENT_LAB_EVIDENCE_FILES)
        self.assertIn(
            "control-center-web/src/features/eval-lab/optimization/OptimizationWorkbench.tsx",
            package_builder.AGENT_LAB_UI_FILES,
        )

        trace_receipt = package_builder.TRACE_TEST_RESULTS_MARKDOWN
        self.assertIn("2026-09-05", trace_receipt)
        self.assertIn("78/78 passed", trace_receipt)
        self.assertIn("45/45", trace_receipt)
        self.assertIn("real loopback HTTP", trace_receipt)
        self.assertNotIn("were not rerun", trace_receipt)
        self.assertNotIn("66/66 passed", trace_receipt)


if __name__ == "__main__":
    unittest.main()
