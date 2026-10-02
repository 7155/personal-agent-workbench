from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from rag_ime.activity_timeline_curation import (
    ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
    activity_organization_output_schema,
    build_activity_organization_packet,
)
from rag_ime.activity_timeline_evaluation import (
    ActivityOrganizationCandidateRun,
    LunaStructuredRun,
    _write_private_exclusive,
    baseline_activity_summary,
    evaluation_timezone,
    load_luna_structured_run,
    load_frozen_activity_timeline,
    redacted_organization_summary,
    run_luna_structured,
    validate_activity_candidate_with_one_contract_repair,
)


class ActivityTimelineEvaluationTests(unittest.TestCase):
    def test_historical_audit_resumes_original_model_receipt_without_admitting_another_call(self) -> None:
        from scripts import audit_historical_memory_luna as audit
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "completed"
            root.mkdir(mode=0o700)
            phase = "historical-catalog-audit"
            prompt, schema, output = "frozen audit prompt", {"type":"object"}, {}
            def encode(value):
                return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output_text = json.dumps(output)
            receipt = {"phase":phase,"model":"gpt-5.6-luna","thinking":"max","exitCode":0,"elapsedSeconds":1,
                "promptSha256":hashlib.sha256(prompt.encode()).hexdigest(),
                "schemaSha256":hashlib.sha256(encode(schema).encode()).hexdigest(),
                "outputSha256":hashlib.sha256(output_text.encode()).hexdigest(),"stdoutSha256":"d"*64,"stderrSha256":"e"*64}
            for name, value in ((f"{phase}-output.json", output_text), (f"{phase}-receipt.json", json.dumps(receipt))):
                path = root / name
                path.write_text(value)
                path.chmod(0o600)
            with patch.object(audit, "run_luna_structured", side_effect=AssertionError("must not admit a new call")) as writer:
                resumed = audit._run_or_resume_audit(prompt=prompt, schema=schema, artifact_base=root, timeout_seconds=1, codex_bin="unused")
            writer.assert_not_called()
            self.assertEqual(resumed.model, "gpt-5.6-luna")
            self.assertEqual(sorted(path.name for path in Path(temporary).iterdir()), ["completed"])

    def test_exclusive_writer_does_not_close_a_descriptor_after_fdopen_owns_it(
        self,
    ) -> None:
        class FailingOwnedHandle:
            closed = False

            def __enter__(self):
                return self

            def __exit__(self, _error_type, _error, _traceback):
                self.closed = True
                return False

            @staticmethod
            def write(_text: str) -> None:
                raise OSError("simulated write failure")

        handle = FailingOwnedHandle()
        with (
            patch(
                "rag_ime.activity_timeline_evaluation.os.open",
                return_value=73,
            ),
            patch(
                "rag_ime.activity_timeline_evaluation.os.fdopen",
                return_value=handle,
            ),
            patch(
                "rag_ime.activity_timeline_evaluation.os.close",
            ) as close_descriptor,
        ):
            with self.assertRaisesRegex(
                OSError,
                "simulated write failure",
            ):
                _write_private_exclusive(
                    Path("/private/activity-evaluation.txt"),
                    "payload",
                )

        self.assertTrue(handle.closed)
        close_descriptor.assert_not_called()

    def test_legacy_timezone_abbreviation_uses_explicit_evaluation_fallback(self) -> None:
        self.assertEqual(evaluation_timezone("CST", fallback="Asia/Shanghai"), "Asia/Shanghai")
        self.assertEqual(evaluation_timezone("UTC", fallback="Asia/Shanghai"), "UTC")

    def _database(self, root: Path) -> Path:
        path = root / "timeline.sqlite"
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE input_events(
                    id INTEGER PRIMARY KEY,
                    created_at_ms INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    committed_text TEXT NOT NULL,
                    recent_context TEXT NOT NULL,
                    preedit TEXT NOT NULL,
                    app TEXT NOT NULL,
                    project TEXT NOT NULL,
                    context_group_id TEXT NOT NULL,
                    context_group_level TEXT NOT NULL
                );
                CREATE TABLE daily_activity_timelines(
                    timeline_id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    timeline_date TEXT NOT NULL,
                    timezone TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source_event_ids_json TEXT NOT NULL,
                    source_event_hash TEXT NOT NULL,
                    segments_json TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    event_count INTEGER NOT NULL,
                    segment_count INTEGER NOT NULL,
                    created_at_ms INTEGER NOT NULL,
                    updated_at_ms INTEGER NOT NULL
                );
                """
            )
            rows = [
                (7, 1_754_035_200_000, "squirrel_input_segment", "修复时间线", "", "", "Ghostty", "p", "lane-a", "app"),
                (8, 1_754_035_260_000, "pi_agent_user", "验证上下文", "", "", "RagImeControl", "p", "lane-b", "session"),
                (9, 1_754_035_320_000, "squirrel_input_segment", "好", "", "", "Ghostty", "p", "lane-a", "app"),
            ]
            conn.executemany(
                "INSERT INTO input_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            segments = [
                {
                    "segmentId": "s1",
                    "sourceEventIds": [7, 8],
                    "title": "私密标题不应进入公开摘要",
                    "summary": "私密原始内容不应进入公开摘要",
                    "startMs": rows[0][1],
                    "endMs": rows[1][1],
                    "apps": ["Ghostty", "RagImeControl"],
                },
                {
                    "segmentId": "s2",
                    "sourceEventIds": [9],
                    "title": "另一个私密标题",
                    "summary": "另一个私密摘要",
                    "startMs": rows[2][1],
                    "endMs": rows[2][1],
                    "apps": ["Ghostty"],
                },
            ]
            conn.execute(
                "INSERT INTO daily_activity_timelines VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "timeline:test",
                    "p",
                    "2026-08-01",
                    "Asia/Shanghai",
                    "approved",
                    json.dumps([7, 8, 9]),
                    "a" * 64,
                    json.dumps(segments, ensure_ascii=False),
                    json.dumps({"segmentationMode": "semantic_task_v5"}),
                    3,
                    2,
                    1,
                    2,
                ),
            )
        path.chmod(0o600)
        return path

    def test_loads_exact_frozen_timeline_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._database(Path(tmp))
            before = hashlib.sha256(path.read_bytes()).hexdigest()

            snapshot = load_frozen_activity_timeline(path, timeline_id="timeline:test")

            after = hashlib.sha256(path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(snapshot.event_ids, (7, 8, 9))
            self.assertEqual([row["id"] for row in snapshot.event_rows], [7, 8, 9])
            self.assertEqual(snapshot.status, "approved")
            self.assertEqual(snapshot.source_event_hash, "a" * 64)

    def test_baseline_summary_is_reference_complete_and_raw_text_free(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = load_frozen_activity_timeline(
                self._database(Path(tmp)),
                timeline_id="timeline:test",
            )

            summary = baseline_activity_summary(snapshot)
            serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True)

            self.assertTrue(summary["exactEventCoverage"])
            self.assertEqual(summary["eventCounts"], [2, 1])
            self.assertEqual(summary["segmentCount"], 2)
            self.assertNotIn("私密标题", serialized)
            self.assertNotIn("私密原始内容", serialized)
            self.assertEqual(len(summary["titleSha256"]), 2)

    def test_luna_runner_sends_prompt_on_stdin_and_hardens_private_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            artifact_dir = Path(tmp) / "private"
            prompt = "PRIVATE SOURCE TEXT"
            output = {
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [],
                "unclassified": [],
            }
            calls: list[tuple[list[str], str]] = []

            def fake_run(command, **kwargs):
                calls.append((list(command), str(kwargs["input"])))
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(json.dumps(output), encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="receipt")

            run = run_luna_structured(
                prompt=prompt,
                schema=activity_organization_output_schema(),
                artifact_dir=artifact_dir,
                phase="organizer",
                command_runner=fake_run,
            )

            self.assertIsInstance(run, LunaStructuredRun)
            self.assertEqual(calls[0][1], prompt)
            self.assertNotIn(prompt, calls[0][0])
            self.assertIn("--ephemeral", calls[0][0])
            self.assertIn("gpt-6.1-sol", calls[0][0])
            self.assertIn('model_reasoning_effort="max"', calls[0][0])
            self.assertEqual(run.output, output)
            self.assertEqual(stat.S_IMODE(artifact_dir.stat().st_mode), 0o700)
            for path in artifact_dir.iterdir():
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, path.name)

    def test_redacted_organization_summary_contains_metrics_not_model_text(self) -> None:
        run = LunaStructuredRun(
            phase="organizer",
            model="gpt-5.6-luna",
            thinking="max",
            command=("codex", "exec"),
            elapsed_seconds=1.25,
            exit_code=0,
            prompt_sha256="b" * 64,
            schema_sha256="c" * 64,
            output_sha256="d" * 64,
            stdout_sha256="e" * 64,
            stderr_sha256="f" * 64,
            output={"private": "not exposed"},
        )
        result = {
            "activities": [
                {
                    "activityId": "activity:a",
                    "title": "私密活动标题",
                    "summary": "私密活动摘要",
                    "eventRefs": ["e1", "e2"],
                    "confidence": 0.9,
                    "boundaryBasis": "私密边界解释",
                }
            ],
            "unclassified": [{"eventRef": "e3", "reason": "私密原因"}],
        }

        summary = redacted_organization_summary(result, run=run)
        serialized = json.dumps(summary, ensure_ascii=False, sort_keys=True)

        self.assertEqual(summary["activityCount"], 1)
        self.assertEqual(summary["activityEventCounts"], [2])
        self.assertEqual(summary["unclassifiedCount"], 1)
        self.assertNotIn("私密活动标题", serialized)
        self.assertNotIn("私密活动摘要", serialized)
        self.assertNotIn("私密原因", serialized)

    def test_invalid_candidate_receives_exactly_one_bounded_contract_repair(self) -> None:
        packet = build_activity_organization_packet(
            [
                {
                    "id": 1,
                    "created_at_ms": 1_754_035_200_000,
                    "source": "squirrel_input_segment",
                    "committed_text": "整理时间线",
                    "recent_context": "",
                    "app": "Ghostty",
                    "context_group_id": "app:ghostty",
                },
                {
                    "id": 2,
                    "created_at_ms": 1_754_035_260_000,
                    "source": "pi_agent_user",
                    "committed_text": "检查边界",
                    "recent_context": "",
                    "app": "RagImeControl",
                    "context_group_id": "session:timeline",
                },
            ],
            timeline_id="timeline:test-contract-repair",
            project="p",
            timeline_date="2026-08-01",
            timezone_name="Asia/Shanghai",
        )
        rejected = self._run_with_output(
            {
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [
                    {
                        "title": "整理时间线",
                        "summary": "整理时间线活动。",
                        "eventRefs": ["e1"],
                        "confidence": 0.9,
                        "boundaryBasis": "当前输入明确。",
                    }
                ],
                "unclassified": [],
            },
            phase="organizer",
        )
        repaired = self._run_with_output(
            {
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [
                    {
                        "title": "整理时间线",
                        "summary": "整理并检查时间线边界。",
                        "eventRefs": ["e1", "e2"],
                        "confidence": 0.9,
                        "boundaryBasis": "两条输入推进同一整理工作。",
                    }
                ],
                "unclassified": [],
            },
            phase="contract-repair",
        )
        calls = []

        def fake_structured_runner(**kwargs):
            calls.append(kwargs)
            return repaired

        candidate = validate_activity_candidate_with_one_contract_repair(
            packet=packet,
            initial_run=rejected,
            artifact_dir=Path("/unused/private-root"),
            timeout_seconds=10,
            codex_bin="codex",
            structured_runner=fake_structured_runner,
        )

        self.assertIsInstance(candidate, ActivityOrganizationCandidateRun)
        self.assertIs(candidate.run, repaired)
        self.assertEqual(candidate.result.activities[0].event_refs, ("e1", "e2"))
        self.assertEqual(len(candidate.contract_repairs), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["phase"], "contract-repair")
        self.assertIn("requiredRefLedger", calls[0]["prompt"])
        self.assertNotIn("contractError", candidate.contract_repairs[0])

    def test_resume_luna_run_requires_matching_private_receipt_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repair"
            root.mkdir(mode=0o700)
            output = {
                "schemaVersion": ACTIVITY_ORGANIZATION_OUTPUT_VERSION,
                "activities": [],
                "unclassified": [],
            }
            output_text = json.dumps(output, ensure_ascii=False, sort_keys=True)
            output_path = root / "repair-output.json"
            output_path.write_text(output_text, encoding="utf-8")
            output_path.chmod(0o600)
            receipt = {
                "phase": "repair",
                "model": "gpt-5.6-luna",
                "thinking": "max",
                "elapsedSeconds": 12.5,
                "exitCode": 0,
                "promptSha256": "a" * 64,
                "schemaSha256": "b" * 64,
                "outputSha256": hashlib.sha256(output_text.encode()).hexdigest(),
                "stdoutSha256": "d" * 64,
                "stderrSha256": "e" * 64,
            }
            receipt_path = root / "repair-receipt.json"
            receipt_path.write_text(
                json.dumps(receipt),
                encoding="utf-8",
            )
            receipt_path.chmod(0o600)

            run = load_luna_structured_run(root, phase="repair")

            self.assertEqual(run.output, output)
            self.assertEqual(run.model, "gpt-5.6-luna")
            with self.assertRaises(ValueError):
                load_luna_structured_run(root, phase="repair", expected_model="gpt-6.1-sol")
            self.assertEqual(run.elapsed_seconds, 12.5)
            output_path.write_text("{}", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_luna_structured_run(root, phase="repair")

    def _run_with_output(self, output, *, phase: str) -> LunaStructuredRun:
        return LunaStructuredRun(
            phase=phase,
            model="gpt-5.6-luna",
            thinking="max",
            command=("codex", "exec"),
            elapsed_seconds=1.0,
            exit_code=0,
            prompt_sha256="a" * 64,
            schema_sha256="b" * 64,
            output_sha256="c" * 64,
            stdout_sha256="d" * 64,
            stderr_sha256="e" * 64,
            output=output,
        )


if __name__ == "__main__":
    unittest.main()
