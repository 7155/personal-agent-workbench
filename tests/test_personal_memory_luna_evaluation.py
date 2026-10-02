from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from rag_ime.activity_timeline_evaluation import FrozenActivityTimeline, LunaStructuredRun
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.embeddings import HashingEmbeddingProvider
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.memory_book_compiler import (
    apply_stored_memory_book_run,
    memory_book_plan_from_compile_output,
    rollback_memory_book_run,
    store_memory_book_plan,
)
from rag_ime.memory_evidence_admission import transition_evidence_admission
from rag_ime.personal_memory_luna_evaluation import (
    PERSONAL_MEMORY_LUNA_TRANSPORT,
    SYNTHETIC_PERSONAL_MEMORY_RAG_CASES,
    PrivateCodexLunaMemoryExecutor,
    atom_first_memory_state_summary,
    build_personal_memory_semantic_evaluation_bundle,
    evaluate_synthetic_personal_memory_rag,
    personal_memory_phase_schema,
    redacted_personal_memory_rag_summary,
    redacted_owner_run_summary,
    redacted_semantic_curation_summary,
    redacted_luna_request_summary,
    redacted_synthetic_seed_summary,
    seed_synthetic_personal_memory_rag_cases,
    _evaluation_prompt,
)


class PersonalMemoryLunaEvaluationTests(unittest.TestCase):
    def test_new_private_executor_freezes_current_model_in_runner_prompt_and_receipt(self) -> None:
        calls = []
        def runner(**kwargs):
            calls.append(kwargs)
            return LunaStructuredRun(phase=kwargs["phase"], model=kwargs["model"], thinking="max", command=(),
                elapsed_seconds=0.1, exit_code=0, prompt_sha256="a"*64, schema_sha256="b"*64,
                output_sha256="c"*64, stdout_sha256="d"*64, stderr_sha256="e"*64, output={"v":2,"d":[]})
        with tempfile.TemporaryDirectory() as temporary:
            executor = PrivateCodexLunaMemoryExecutor(temporary, structured_runner=runner)
            executor.begin_run("latest", frozen_input_sha256="f"*64)
            response = executor.complete(phase="evidence-adjudication", messages=[{"role":"system","content":"classify"},{"role":"user","content":"[]"}])
            executor.finish_run()
        self.assertEqual(executor.model_id, "gpt-6.1-sol")
        self.assertEqual(calls[0]["model"], "gpt-6.1-sol")
        self.assertIn("Required model: gpt-6.1-sol", calls[0]["prompt"])
        self.assertEqual(response["receipt"]["model"], "gpt-6.1-sol")

    def test_concise_prompt_contract_keeps_full_packet_and_bounds_output(self) -> None:
        messages = [
            {"role": "system", "content": "authoritative contract"},
            {"role": "user", "content": '{"b": 2, "a": [1, 2]}'},
        ]
        standard_prompt, standard_projection = _evaluation_prompt(
            "atom-first-curation",
            messages,
            requested_output_tokens=None,
            required_model="gpt-5.6-sol",
            context_profile="full-json-v1",
            prompt_contract="standard-v1",
        )
        concise_prompt, concise_projection = _evaluation_prompt(
            "atom-first-curation",
            messages,
            requested_output_tokens=None,
            required_model="gpt-5.6-sol",
            context_profile="full-json-v1",
            prompt_contract="concise-json-v1",
        )
        concise_schema = personal_memory_phase_schema(
            "atom-first-curation",
            prompt_contract="concise-json-v1",
        )

        self.assertEqual(standard_projection["sourcePacketSha256"], concise_projection["sourcePacketSha256"])
        self.assertEqual(standard_projection["sourcePacketChars"], concise_projection["projectedPacketChars"])
        self.assertIn('{"b": 2, "a": [1, 2]}', concise_prompt)
        self.assertLess(len(concise_prompt), len(standard_prompt))
        self.assertIn("reasoning target <= 384 tokens", concise_prompt)
        reason = concise_schema["properties"]["create"]["items"]["properties"]["reason"]
        self.assertEqual(reason["maxLength"], 48)

    def test_executor_can_freeze_sol_and_compact_only_packet_whitespace(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-sol-compact-") as temporary:
            calls: list[dict[str, object]] = []

            def runner(**kwargs: object) -> LunaStructuredRun:
                calls.append(dict(kwargs))
                prompt = str(kwargs["prompt"])
                schema_text = json.dumps(
                    kwargs["schema"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                output = {"v": 2, "d": []}
                encoded = json.dumps(output, ensure_ascii=False, sort_keys=True)
                return LunaStructuredRun(
                    phase=str(kwargs["phase"]),
                    model="gpt-5.6-sol",
                    thinking="max",
                    command=(),
                    elapsed_seconds=0.1,
                    exit_code=0,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    schema_sha256=hashlib.sha256(schema_text.encode()).hexdigest(),
                    output_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                    stdout_sha256="7" * 64,
                    stderr_sha256="8" * 64,
                    output=output,
                )

            executor = PrivateCodexLunaMemoryExecutor(
                temporary,
                model_id="gpt-5.6-sol",
                context_profile="compact-json-v1",
                structured_runner=runner,
            )
            executor.begin_run("run:sol", frozen_input_sha256="f" * 64)
            response = executor.complete(
                phase="evidence-adjudication",
                isolated=False,
                messages=[
                    {"role": "system", "content": "classify"},
                    {"role": "user", "content": '{"b": 2, "a": [1, 2]}'},
                ],
            )
            executor.finish_run()

            prompt = str(calls[0]["prompt"])
            self.assertIn("Required model: gpt-5.6-sol", prompt)
            self.assertIn('{"a":[1,2],"b":2}', prompt)
            self.assertNotIn('{"b": 2, "a": [1, 2]}', prompt)
            self.assertEqual("compact-json-v1", response["receipt"]["contextProfile"])
            self.assertGreater(
                response["receipt"]["sourcePacketChars"],
                response["receipt"]["projectedPacketChars"],
            )

    def test_synthetic_capture_to_rag_and_rollback_is_retrieval_complete(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-rag-eval-") as temporary:
            root = Path(temporary)
            db_path = root / "rag-ime.sqlite"
            provider = HashingEmbeddingProvider()
            core = LocalSqliteCoreClient(db_path, embedding_provider=provider)
            core.initialize()
            cases = seed_synthetic_personal_memory_rag_cases(
                core,
                project="personal-agent-workbench",
            )
            run_id = "memory-run:synthetic-rag-evaluation"
            session = AgentSessionStore(db_path).create(
                title="Synthetic Memory RAG evaluation",
                created_at_ms=100,
            )
            fixtures = {
                str(item["caseId"]): dict(item)
                for item in SYNTHETIC_PERSONAL_MEMORY_RAG_CASES
            }
            positive = [dict(item) for item in cases if bool(item["expectMemory"])]
            source_ids: list[str] = []
            with core._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO memory_curation_model_runs(
                        run_id, session_id, provider, model_id, thinking_level,
                        frozen_input_sha256, state, created_at_ms, updated_at_ms
                    ) VALUES (?, ?, 'openai-codex', 'gpt-5.6-luna', 'max', ?,
                              'completed', 100, 100)
                    """,
                    (run_id, session["id"], hashlib.sha256(run_id.encode()).hexdigest()),
                )
                for case in positive:
                    transition_evidence_admission(
                        conn,
                        str(case["evidenceId"]),
                        new_state="admitted",
                        reason_code="luna_personal_memory_confirmed",
                        actor_kind="luna",
                        created_at_ms=200,
                        run_id=run_id,
                    )
                    source = conn.execute(
                        "SELECT source_id FROM agent_memory_sources WHERE input_event_id = ?",
                        (int(case["eventId"]),),
                    ).fetchone()
                    self.assertIsNotNone(source)
                    source_ids.append(str(source["source_id"]))
                atoms = []
                kinds = (
                    "durable_preference",
                    "personal_habit",
                    "personal_principle",
                    "project_requirement",
                )
                for ordinal, (case, kind) in enumerate(zip(positive, kinds, strict=True), start=1):
                    fixture = fixtures[str(case["caseId"])]
                    personal_kind = kind != "project_requirement"
                    atoms.append(
                        {
                            "atomId": f"atom:synthetic-rag:{ordinal}",
                            "operation": "create",
                            "kind": kind,
                            "claimKey": f"user:synthetic-rag:{ordinal}",
                            "canonicalText": str(fixture["text"]),
                            "sourceEventIds": [int(case["eventId"])],
                            "evidenceIds": [str(case["evidenceId"])],
                            "tags": ["工作方式"],
                            "confidence": 0.97,
                            "qualityScore": 0.97,
                            "project": "" if personal_kind else "personal-agent-workbench",
                            "app": "",
                            "ownerKind": "user",
                            "ownerId": "default",
                            "knowledgeDomain": (
                                "personal_memory" if personal_kind else "legacy"
                            ),
                            "scopeKind": "user" if personal_kind else "legacy",
                            "scopeId": "default" if personal_kind else "",
                            "visibility": "private" if personal_kind else "legacy",
                            "authorizationRevision": (
                                "memory-atom-v2" if personal_kind else ""
                            ),
                            "bindingId": (
                                "personal-memory:user:default" if personal_kind else ""
                            ),
                            "scopeMode": "authoritative" if personal_kind else "legacy",
                            "curationRunId": run_id,
                            "curationArchitecture": "atom-first-v1",
                        }
                    )
                bundle = {
                    "bundleHash": hashlib.sha256(b"synthetic-rag-bundle").hexdigest(),
                    "legalSourceEventIds": [int(item["eventId"]) for item in positive],
                    "inputs": [
                        {
                            "sourceRef": f"S{index}",
                            "sourceEventIds": [int(item["eventId"])],
                            "evidenceIds": [str(item["evidenceId"])],
                            "createdAtMs": index,
                            "text": str(fixtures[str(item["caseId"])]["text"]),
                        }
                        for index, item in enumerate(positive, start=1)
                    ],
                    "cursor": {"fromEventId": 0, "toEventId": max(int(item["eventId"]) for item in positive)},
                }
                plan = memory_book_plan_from_compile_output(
                    {
                        "memoryAtoms": atoms,
                        "topicBooks": [
                            {
                                "bookId": "book:topic:room-map",
                                "bookKey": "room-map",
                                "title": "Room 地图",
                                "summary": "Room 地图的稳定产品要求。",
                                "memoryAtomIds": ["atom:synthetic-rag:4"],
                                "sourceEventIds": [int(positive[3]["eventId"])],
                                "confidence": 0.97,
                                "qualityScore": 0.97,
                            }
                        ],
                    },
                    project="personal-agent-workbench",
                    provider="openai-codex",
                    model="gpt-5.6-luna",
                    source_bundle=bundle,
                    owner_kind="user",
                    owner_id="default",
                    run_kind="manual_curation",
                )
                plan["runId"] = run_id
                plan["metadata"] = {
                    **dict(plan.get("metadata") or {}),
                    "ownerKind": "user",
                    "ownerId": "default",
                    "project": "personal-agent-workbench",
                    "runKind": "manual_curation",
                    "sourceIds": source_ids,
                }
                store_memory_book_plan(conn, plan)
                apply_stored_memory_book_run(conn, run_id=run_id)

            schema_view = root / "schema-view"
            schema_view.mkdir(mode=0o700)
            applied = evaluate_synthetic_personal_memory_rag(
                core,
                cases,
                project="personal-agent-workbench",
                migrations_dir=schema_view,
            )

            self.assertTrue(applied["passed"], applied)
            self.assertEqual(applied["discoveredAtomCount"], 4)
            self.assertEqual(
                [item["passed"] for item in applied["cases"]],
                [True, True, True, True, True],
            )
            state = atom_first_memory_state_summary(db_path)
            self.assertEqual(state["governedCurrentAtomCount"], 4)
            self.assertTrue(state["allGovernedCurrentAtomsHaveLegalLineage"])
            self.assertTrue(state["bookProjection"]["inSync"])
            with core._connect() as conn:
                rollback_memory_book_run(conn, run_id=run_id)
            rolled_back = evaluate_synthetic_personal_memory_rag(
                core,
                cases,
                project="personal-agent-workbench",
                expected_atom_ids=applied["atomIds"],
                expect_present=False,
                migrations_dir=schema_view,
            )
            self.assertTrue(rolled_back["passed"], rolled_back)
            redacted = redacted_personal_memory_rag_summary(applied)
            self.assertNotIn("atomIds", redacted)
            self.assertNotIn(
                str(fixtures["durable-explanation-preference"]["text"]),
                json.dumps(redacted, ensure_ascii=False),
            )

    def test_synthetic_seed_summary_is_raw_text_free(self) -> None:
        summary = redacted_synthetic_seed_summary(
            [
                {
                    "caseId": "preference",
                    "contentSha256": "a" * 64,
                    "expectMemory": True,
                    "outcome": "stored",
                    "evidenceState": "candidate",
                    "query": "private query",
                }
            ]
        )

        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("private query", encoded)
        self.assertTrue(summary["allStored"])
        self.assertTrue(summary["allCandidateEvidence"])

    def test_semantic_bundle_keeps_recoverable_expressions_without_admitting_them(self) -> None:
        shared = {
            "source": "squirrel_input_segment",
            "app": "com.example.Editor",
            "project": "personal-agent-workbench",
            "context_group_id": "field:one",
            "context_group_level": "field",
            "preedit": "",
        }
        snapshot = FrozenActivityTimeline(
            timeline_id="timeline:test",
            project="personal-agent-workbench",
            timeline_date="2026-08-01",
            timezone="Asia/Shanghai",
            status="approved",
            source_event_hash="f" * 64,
            event_ids=(1, 2, 3, 4),
            event_rows=(
                {
                    **shared,
                    "id": 1,
                    "created_at_ms": 1_000,
                    "committed_text": "简洁自然",
                    "recent_context": "我长期偏好回答简洁自然并避免机器化措辞",
                    "tags_json": "[]",
                },
                {
                    **shared,
                    "id": 2,
                    "created_at_ms": 2_000,
                    "committed_text": "简洁自然",
                    "recent_context": "我长期偏好回答简洁自然并避免机器化措辞",
                    "tags_json": "[]",
                },
                {
                    **shared,
                    "id": 3,
                    "created_at_ms": 3_000,
                    "committed_text": "然后",
                    "recent_context": "",
                    "tags_json": "[]",
                },
                {
                    **shared,
                    "id": 4,
                    "created_at_ms": 4_000,
                    "committed_text": "我习惯在上午安排需要专注的工作。",
                    "recent_context": "",
                    "tags_json": '["finalized","complete-input"]',
                },
            ),
            baseline_segments=(),
            metadata={},
        )

        bundle, summary = build_personal_memory_semantic_evaluation_bundle(snapshot)

        self.assertTrue(bundle["semanticEvaluationOnly"])
        self.assertFalse(bundle["legalEvidenceAdmissionAllowed"])
        self.assertEqual(summary["rawEventCount"], 4)
        self.assertEqual(summary["modelInputCount"], 2)
        self.assertEqual(summary["representedSourceEventCount"], 3)
        self.assertEqual(summary["contextRecoveredCandidateCount"], 2)
        self.assertEqual(summary["deduplicatedCandidateCount"], 1)
        self.assertEqual(summary["ambiguousExcludedCount"], 1)
        self.assertTrue(
            all(
                item["boundaryKind"] == "semantic_evaluation_only"
                for item in bundle["inputs"]
            )
        )

    def test_semantic_summary_contains_counts_and_hashes_but_no_atom_text(self) -> None:
        result = {
            "sourceDecisions": [
                {
                    "sourceRef": "S1",
                    "disposition": "remember",
                    "evidenceAdmissionState": "admitted",
                }
            ],
            "memoryAtoms": [
                {
                    "kind": "durable_preference",
                    "operation": "create",
                    "canonicalText": "private personal preference",
                }
            ],
            "memoryRetractions": [],
            "personalCurationV2": {"independentlyVerified": True},
            "modelBundleStats": {"sourceCount": 1},
            "elapsedMs": 100,
        }

        summary = redacted_semantic_curation_summary(
            result,
            expected_source_refs=["S1"],
        )
        replay_summary = redacted_semantic_curation_summary(
            {**result, "elapsedMs": 999_999, "modelDiagnostics": {"resumed": True}},
            expected_source_refs=["S1"],
        )

        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("private personal preference", encoded)
        self.assertTrue(summary["allSourceRefsCoveredExactlyOnce"])
        self.assertEqual(summary["memoryAtomKindCounts"], {"durable_preference": 1})
        self.assertEqual(summary["resultSha256"], replay_summary["resultSha256"])

    def test_phase_schemas_reject_extra_top_level_keys(self) -> None:
        evidence = personal_memory_phase_schema("evidence-adjudication")
        atom = personal_memory_phase_schema("atom-adjudication")
        verifier = personal_memory_phase_schema("independent-verifier")
        atom_first = personal_memory_phase_schema("atom-first-curation")
        atom_first_verifier = personal_memory_phase_schema("atom-first-verifier")
        role_book = personal_memory_phase_schema("role-book-curation")

        self.assertFalse(evidence["additionalProperties"])
        self.assertEqual(evidence["required"], ["v", "d"])
        self.assertEqual(atom["required"], ["v", "o"])
        self.assertEqual(verifier["required"], ["v", "ok", "r", "o", "errors"])
        self.assertIn("retract", atom_first["required"])
        create_e = atom_first["properties"]["create"]["items"]["properties"]["e"]
        self.assertEqual(create_e["anyOf"][0], {"type": "string"})
        self.assertEqual(create_e["anyOf"][1]["type"], "array")
        self.assertEqual(
            atom_first_verifier["required"],
            [
                "v",
                "ok",
                "coveredEvidenceRefs",
                "checkedActionCount",
                "decisionDigest",
                "findings",
                "errors",
            ],
        )
        self.assertEqual(
            role_book["required"],
            [
                "traitProposals",
                "capabilityProposals",
                "lessonProposals",
                "commitmentProposals",
                "warnings",
            ],
        )
        self.assertFalse(
            role_book["properties"]["lessonProposals"]["items"][
                "additionalProperties"
            ]
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            personal_memory_phase_schema("book-generation")

    def test_executor_records_real_model_contract_without_private_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-luna-") as temporary:
            root = Path(temporary)
            calls: list[dict[str, object]] = []

            def runner(**kwargs: object) -> LunaStructuredRun:
                calls.append(dict(kwargs))
                phase = str(kwargs["phase"])
                artifact_dir = Path(kwargs["artifact_dir"])
                artifact_dir.mkdir(mode=0o700)
                output = {"v": 2, "d": [["E1", "n", 0.99, "temporary"]]}
                if phase == "atom-adjudication":
                    output = {"v": 2, "o": []}
                elif phase == "independent-verifier":
                    output = {
                        "v": 2,
                        "ok": 1,
                        "r": [["E1", 1]],
                        "o": [],
                        "errors": [],
                    }
                prompt = str(kwargs["prompt"])
                schema_text = json.dumps(
                    kwargs["schema"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                encoded = json.dumps(output, ensure_ascii=False, sort_keys=True)
                return LunaStructuredRun(
                    phase=phase,
                    model="gpt-5.6-luna",
                    thinking="max",
                    command=(),
                    elapsed_seconds=1.25,
                    exit_code=0,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    schema_sha256=hashlib.sha256(schema_text.encode()).hexdigest(),
                    output_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                    stdout_sha256="1" * 64,
                    stderr_sha256="2" * 64,
                    output=output,
                )

            executor = PrivateCodexLunaMemoryExecutor(root, structured_runner=runner, model_id="gpt-5.6-luna")
            executor.begin_run("run:one", frozen_input_sha256="a" * 64)
            response = executor.complete(
                phase="evidence-adjudication",
                isolated=False,
                max_tokens=24_000,
                messages=[
                    {"role": "system", "content": "classify"},
                    {"role": "user", "content": '{"private":"secret phrase"}'},
                ],
            )
            executor.finish_run()

            self.assertEqual(len(calls), 1)
            self.assertEqual(response["receipt"]["transport"], PERSONAL_MEMORY_LUNA_TRANSPORT)
            self.assertEqual(response["receipt"]["model"], "gpt-5.6-luna")
            summary = redacted_luna_request_summary(executor.receipts)
            self.assertNotIn("secret phrase", json.dumps(summary, ensure_ascii=False))
            self.assertEqual(summary[0]["thinking"], "max")
            self.assertFalse(summary[0]["isolated"])

    def test_verifier_must_be_isolated(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-luna-") as temporary:
            executor = PrivateCodexLunaMemoryExecutor(
                temporary,
                structured_runner=lambda **_kwargs: self.fail("runner must not execute"),
            )
            executor.begin_run("run:two", frozen_input_sha256="b" * 64)
            with self.assertRaisesRegex(ValueError, "isolation"):
                executor.complete(
                    phase="independent-verifier",
                    isolated=False,
                    messages=[
                        {"role": "system", "content": "verify"},
                        {"role": "user", "content": "{}"},
                    ],
                )
            with self.assertRaisesRegex(ValueError, "isolation"):
                executor.complete(
                    phase="atom-first-verifier",
                    isolated=False,
                    messages=[
                        {"role": "system", "content": "verify"},
                        {"role": "user", "content": "{}"},
                    ],
                )

    def test_executor_records_auditable_private_session_and_request(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-luna-audit-") as temporary:
            root = Path(temporary)
            db_path = root / "rag-ime.sqlite"
            LocalSqliteCoreClient(db_path).initialize()

            def runner(**kwargs: object) -> LunaStructuredRun:
                artifact_dir = Path(kwargs["artifact_dir"])
                artifact_dir.mkdir(mode=0o700)
                output = {"v": 2, "d": [["E1", "n", 0.99, "temporary"]]}
                prompt = str(kwargs["prompt"])
                schema_text = json.dumps(
                    kwargs["schema"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                encoded = json.dumps(output, ensure_ascii=False, sort_keys=True)
                return LunaStructuredRun(
                    phase=str(kwargs["phase"]),
                    model="gpt-5.6-luna",
                    thinking="max",
                    command=(),
                    elapsed_seconds=0.1,
                    exit_code=0,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    schema_sha256=hashlib.sha256(schema_text.encode()).hexdigest(),
                    output_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                    stdout_sha256="3" * 64,
                    stderr_sha256="4" * 64,
                    output=output,
                )

            executor = PrivateCodexLunaMemoryExecutor(
                root / "luna",
                audit_db_path=db_path,
                structured_runner=runner, model_id="gpt-5.6-luna",
            )
            executor.begin_run("run:audited", frozen_input_sha256="c" * 64)
            executor.complete(
                phase="evidence-adjudication",
                isolated=False,
                messages=[
                    {"role": "system", "content": "classify"},
                    {"role": "user", "content": '{"private":"test"}'},
                ],
            )
            executor.finish_run()

            with sqlite3.connect(db_path) as conn:
                run = conn.execute(
                    """
                    SELECT session_id, state FROM memory_curation_model_runs
                    WHERE run_id = 'run:audited'
                    """
                ).fetchone()
                request = conn.execute(
                    """
                    SELECT state, phase, session_id
                    FROM memory_curation_model_requests
                    WHERE run_id = 'run:audited'
                    """
                ).fetchone()
                session_status = conn.execute(
                    "SELECT status FROM agent_sessions WHERE id = ?",
                    (str(run[0]),),
                ).fetchone()[0]

            self.assertEqual(run[1], "completed")
            self.assertEqual(request[:2], ("completed", "evidence-adjudication"))
            self.assertEqual(request[2], run[0])
            self.assertEqual(session_status, "archived")

    def test_executor_replayed_request_is_audited_idempotently(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-luna-replay-") as temporary:
            root = Path(temporary)
            db_path = root / "rag-ime.sqlite"
            LocalSqliteCoreClient(db_path).initialize()

            def runner(**kwargs: object) -> LunaStructuredRun:
                artifact_dir = Path(kwargs["artifact_dir"])
                artifact_dir.mkdir(mode=0o700)
                output = {"v": 2, "d": [["E1", "n", 0.99, "temporary"]]}
                prompt = str(kwargs["prompt"])
                schema_text = json.dumps(
                    kwargs["schema"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                encoded = json.dumps(output, ensure_ascii=False, sort_keys=True)
                return LunaStructuredRun(
                    phase=str(kwargs["phase"]),
                    model="gpt-5.6-luna",
                    thinking="max",
                    command=(),
                    elapsed_seconds=0.1,
                    exit_code=0,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    schema_sha256=hashlib.sha256(schema_text.encode()).hexdigest(),
                    output_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                    stdout_sha256="7" * 64,
                    stderr_sha256="8" * 64,
                    output=output,
                )

            executor = PrivateCodexLunaMemoryExecutor(
                root / "luna",
                audit_db_path=db_path,
                structured_runner=runner, model_id="gpt-5.6-luna",
            )
            executor.begin_run("run:replayed", frozen_input_sha256="f" * 64)
            request = {
                "phase": "evidence-adjudication",
                "isolated": False,
                "messages": [
                    {"role": "system", "content": "classify"},
                    {"role": "user", "content": "{}"},
                ],
            }
            executor.complete(**request)
            executor.complete(**request)
            executor.finish_run()

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT state, attempt_count
                    FROM memory_curation_model_requests
                    WHERE run_id = 'run:replayed'
                    """
                ).fetchall()

            self.assertEqual(rows, [("completed", 2)])

    def test_executor_close_cancels_an_interrupted_audit_run(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-luna-close-") as temporary:
            root = Path(temporary)
            db_path = root / "rag-ime.sqlite"
            LocalSqliteCoreClient(db_path).initialize()
            executor = PrivateCodexLunaMemoryExecutor(
                root / "luna",
                audit_db_path=db_path,
                structured_runner=lambda **_kwargs: self.fail("runner must not execute"),
            )
            executor.begin_run("run:interrupted", frozen_input_sha256="e" * 64)

            executor.close()

            with sqlite3.connect(db_path) as conn:
                run = conn.execute(
                    "SELECT session_id, state FROM memory_curation_model_runs "
                    "WHERE run_id = 'run:interrupted'"
                ).fetchone()
                session_status = conn.execute(
                    "SELECT status FROM agent_sessions WHERE id = ?",
                    (str(run[0]),),
                ).fetchone()[0]
            self.assertEqual(run[1], "cancelled")
            self.assertEqual(session_status, "archived")

    def test_executor_waits_for_a_short_audit_database_writer(self) -> None:
        with tempfile.TemporaryDirectory(prefix="personal-memory-luna-lock-") as temporary:
            root = Path(temporary)
            db_path = root / "rag-ime.sqlite"
            LocalSqliteCoreClient(db_path).initialize()

            def runner(**kwargs: object) -> LunaStructuredRun:
                artifact_dir = Path(kwargs["artifact_dir"])
                artifact_dir.mkdir(mode=0o700)
                output = {"v": 2, "d": []}
                prompt = str(kwargs["prompt"])
                schema_text = json.dumps(
                    kwargs["schema"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                encoded = json.dumps(output, ensure_ascii=False, sort_keys=True)
                return LunaStructuredRun(
                    phase=str(kwargs["phase"]),
                    model="gpt-5.6-luna",
                    thinking="max",
                    command=(),
                    elapsed_seconds=0.1,
                    exit_code=0,
                    prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),
                    schema_sha256=hashlib.sha256(schema_text.encode()).hexdigest(),
                    output_sha256=hashlib.sha256(encoded.encode()).hexdigest(),
                    stdout_sha256="5" * 64,
                    stderr_sha256="6" * 64,
                    output=output,
                )

            executor = PrivateCodexLunaMemoryExecutor(
                root / "luna",
                audit_db_path=db_path,
                structured_runner=runner, model_id="gpt-5.6-luna",
            )
            executor.begin_run("run:lock-wait", frozen_input_sha256="d" * 64)
            writer_ready = threading.Event()

            def hold_writer() -> None:
                with sqlite3.connect(db_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    writer_ready.set()
                    # SQLite's default five-second busy timeout would reject
                    # the already-completed Luna result before this writer exits.
                    time.sleep(5.5)
                    conn.commit()

            writer = threading.Thread(target=hold_writer, daemon=True)
            writer.start()
            self.assertTrue(writer_ready.wait(timeout=2.0))
            executor.complete(
                phase="evidence-adjudication",
                isolated=False,
                messages=[
                    {"role": "system", "content": "classify"},
                    {"role": "user", "content": "{}"},
                ],
            )
            writer.join(timeout=2.0)
            self.assertFalse(writer.is_alive())
            executor.finish_run()

            with sqlite3.connect(db_path) as conn:
                state = conn.execute(
                    "SELECT state FROM memory_curation_model_requests "
                    "WHERE run_id = 'run:lock-wait'"
                ).fetchone()
            self.assertEqual(tuple(state or ()), ("completed",))

    def test_owner_run_summary_drops_source_and_evidence_ids(self) -> None:
        summary = redacted_owner_run_summary(
            {
                "schemaVersion": "run.v1",
                "ok": True,
                "manual": True,
                "ranScopeCount": 1,
                "results": [
                    {
                        "ok": True,
                        "runId": "private-run-id",
                        "runStatus": "applied",
                        "sourceCount": 2,
                        "modelDecisions": [
                            {
                                "sourceId": "private-source-id",
                                "evidenceId": "private-evidence-id",
                                "disposition": "remember",
                            },
                            {"disposition": "not_for_memory"},
                        ],
                    }
                ],
            }
        )

        encoded = json.dumps(summary, ensure_ascii=False)
        self.assertNotIn("private-source-id", encoded)
        self.assertNotIn("private-evidence-id", encoded)
        self.assertNotIn("private-run-id", encoded)
        self.assertEqual(
            summary["results"][0]["modelDispositionCounts"],
            {"not_for_memory": 1, "remember": 1},
        )


if __name__ == "__main__":
    unittest.main()
