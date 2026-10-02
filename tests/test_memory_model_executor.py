from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from rag_ime.agent_events import AgentEventHub
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.memory_model_executor import (
    MEMORY_CURATION_PROFILE,
    GovernedMemoryModelExecutor,
    MemoryModelTimeout,
    MemoryModelUnavailable,
    build_governed_memory_model_executor,
    memory_curation_model_status,
    reconcile_stale_memory_runtime_sessions,
)
from rag_ime.pi.values import (
    PiRuntimeSettlementLookupTimeout,
    PiRuntimeTurnConflict,
)


class FakeMemoryRuntime:
    def __init__(
        self,
        sessions: AgentSessionStore,
        events: AgentEventHub,
        *,
        settle: bool = True,
        emit_terminal_events: bool = True,
        models: list[dict[str, object]] | None = None,
    ) -> None:
        self.sessions = sessions
        self.events = events
        self.settle = settle
        self.emit_terminal_events = emit_terminal_events
        self.models = models or [
            {
                "provider": "openai-codex",
                "id": "gpt-5.6-luna",
                "thinkingLevels": ["off", "high", "max"],
                "contextWindow": 372_000,
                "maxTokens": 128_000,
            }
        ]
        self.prompts: list[dict[str, object]] = []
        self.model_requests: list[dict[str, object]] = []
        self.thinking_requests: list[dict[str, str]] = []
        self.aborted: list[str] = []
        self.closed: list[str] = []
        self.close_error: Exception | None = None
        self.settlement_calls: list[dict[str, object]] = []
        self.settlements: dict[str, dict[str, object]] = {}
        self.snapshots: dict[str, dict[str, object]] = {}

    def available_models(self) -> list[dict[str, object]]:
        return [dict(model) for model in self.models]

    def set_model(
        self,
        session_id: str,
        *,
        provider: str,
        model_id: str,
        max_tokens: int | None = None,
    ) -> dict[str, object]:
        self.model_requests.append(
            {
                "sessionId": session_id,
                "provider": provider,
                "modelId": model_id,
                "maxTokens": max_tokens,
            }
        )
        self.sessions.set_model_profile(session_id, f"{provider}/{model_id}")
        return {
            "selected": {
                "provider": provider,
                "id": model_id,
                "thinkingLevels": ["off", "high", "max"],
                "contextWindow": 372_000,
                "maxTokens": max_tokens or 128_000,
            }
        }

    def set_thinking_level(
        self,
        session_id: str,
        *,
        level: str,
    ) -> dict[str, object]:
        self.thinking_requests.append({"sessionId": session_id, "level": level})
        self.sessions.set_thinking_level(session_id, level)
        return {"thinkingLevel": level}

    def prompt(
        self,
        session_id: str,
        message: str,
        *,
        images: list[dict[str, str]] | None = None,
        client_message_id: str = "",
        delivery: str = "prompt",
    ) -> dict[str, object]:
        turn_id = f"turn-{len(self.prompts) + 1}"
        self.prompts.append(
            {
                "sessionId": session_id,
                "message": message,
                "images": list(images or []),
                "clientMessageId": client_message_id,
                "delivery": delivery,
                "turnId": turn_id,
            }
        )
        if self.settle:
            self.settlements[turn_id] = {
                "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                "sessionId": session_id,
                "runtimeSessionId": f"pi-{session_id}",
                "turnId": turn_id,
                "clientMessageId": client_message_id,
                "receipt": {
                    "schemaVersion": "pi.agent-settled.v2",
                    "receiptId": f"pi-settled:{turn_id}",
                    "sessionId": f"pi-{session_id}",
                    "runId": turn_id,
                    "scopeId": f"pi-{session_id}:{turn_id}",
                    "generation": 1,
                    "disposition": "completed",
                    "stopReason": "stop",
                    "settledAtMs": 100,
                    "aborted": False,
                    "pendingOperations": 0,
                    "operations": {
                        "pending": 0,
                        "pendingByKind": {},
                        "registeredByKind": {},
                    },
                    "operationCounts": {},
                    "finalMessage": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "private chain"},
                            {"type": "text", "text": '{"decisions":[]}'},
                        ],
                        "usage": {"input": len(message), "output": 4},
                    },
                },
            }
        if self.settle and self.emit_terminal_events:
            self.events.publish(
                session_id,
                "message_completed",
                {
                    "message": {
                        "role": "assistant",
                        "blocks": [
                            {
                                "type": "text",
                                "data": {"text": '{"decisions":[]}'},
                            }
                        ],
                    },
                    "usage": {"input": len(message), "output": 4},
                },
                turn_id=turn_id,
            )
            self.events.publish(
                session_id,
                "turn_completed",
                {"terminalEvent": "agent_settled"},
                turn_id=turn_id,
            )
        return {"accepted": True, "turnId": turn_id}

    def await_turn_settled(
        self,
        session_id: str,
        turn_id: str,
        *,
        client_message_id: str,
        timeout_seconds: float,
    ) -> dict[str, object]:
        self.settlement_calls.append(
            {
                "sessionId": session_id,
                "turnId": turn_id,
                "clientMessageId": client_message_id,
                "timeoutSeconds": timeout_seconds,
            }
        )
        settlement = self.settlements.get(turn_id)
        if settlement is None:
            raise TimeoutError("Memory Session turn timed out")
        return dict(settlement)

    def session_snapshot(self, session_id: str) -> dict[str, object]:
        return dict(self.snapshots.get(session_id) or {"messages": []})

    def abort(self, session_id: str) -> dict[str, object]:
        self.aborted.append(session_id)
        return {"cancelled": True, "sessionId": session_id}

    def close_session(self, session_id: str) -> bool:
        if self.close_error is not None:
            raise self.close_error
        self.closed.append(session_id)
        self.sessions.set_status(session_id, "idle")
        return True


class GovernedMemoryModelExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="rag-ime-memory-model-executor-"
        )
        self.db_path = Path(self.temporary.name) / "rag-ime.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.events = AgentEventHub()

    def tearDown(self) -> None:
        self.events.close()
        self.temporary.cleanup()

    def _executor(
        self,
        runtime: FakeMemoryRuntime,
        *,
        timeout_seconds: float = 1.0,
    ) -> GovernedMemoryModelExecutor:
        return build_governed_memory_model_executor(
            runtime,
            "openai-codex/gpt-5.6-luna",
            "max",
            timeout_seconds=timeout_seconds,
            db_path=self.db_path,
        )

    def _leave_accepted_request_running_without_durable_identity(
        self,
        executor: GovernedMemoryModelExecutor,
        *,
        messages: list[dict[str, str]],
    ) -> None:
        def unavailable_bind(*, request_id: str, turn_id: str) -> None:
            del request_id, turn_id
            raise sqlite3.OperationalError("unable to open database file")

        def unavailable_terminal_write(
            *,
            request_id: str,
            turn_id: str,
            output_text: str,
            receipt: dict[str, object],
        ) -> sqlite3.Row:
            del request_id, turn_id, output_text, receipt
            raise sqlite3.OperationalError("unable to open database file")

        def unavailable_resumable_write(
            *,
            request_id: str,
            error: str,
            receipt: dict[str, object] | None = None,
            turn_id: str = "",
        ) -> None:
            del request_id, error, receipt, turn_id
            raise sqlite3.OperationalError("unable to open database file")

        executor._bind_turn = unavailable_bind  # type: ignore[method-assign]
        executor._complete_request = (  # type: ignore[method-assign]
            unavailable_terminal_write
        )
        executor._mark_request_resumable = (  # type: ignore[method-assign]
            unavailable_resumable_write
        )
        with self.assertRaisesRegex(sqlite3.OperationalError, "unable to open"):
            executor.complete(messages=messages)

    def test_default_timeout_covers_the_verified_luna_production_lease(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)

        executor = build_governed_memory_model_executor(
            runtime,
            "openai-codex/gpt-5.6-luna",
            "max",
            db_path=self.db_path,
        )

        self.assertEqual(executor.timeout_seconds, 1_200.0)

    def test_selected_model_profile_and_thinking_reach_session_prompt(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        frozen = hashlib.sha256(b"batch-1").hexdigest()
        run = executor.begin_run("memory_book_user_1", frozen_input_sha256=frozen)

        response = executor.complete(
            messages=[
                {"role": "system", "content": "classify exact refs"},
                {"role": "user", "content": '{"v":2,"e":[]}'},
            ],
            max_tokens=256,
        )

        session = self.sessions.get(str(run["sessionId"]))
        self.assertEqual(session["sessionKind"], "subagent_runtime")
        self.assertEqual(session["toolProfileVersion"], "memory-curation-v1")
        self.assertEqual(session["allowedTools"], [])
        self.assertFalse(session["projectContextEnabled"])
        self.assertFalse(session["piSkillsEnabled"])
        self.assertFalse(session["codexSkillsEnabled"])
        self.assertEqual(runtime.model_requests[0]["provider"], "openai-codex")
        self.assertEqual(runtime.model_requests[0]["modelId"], "gpt-5.6-luna")
        self.assertEqual(runtime.model_requests[0]["maxTokens"], 16_384)
        self.assertEqual(runtime.thinking_requests[0]["level"], "max")
        self.assertEqual(response["profile"], MEMORY_CURATION_PROFILE)
        self.assertEqual(response["thinkingLevel"], "max")
        self.assertEqual(
            response["receipt"]["transport"],
            "gateway_internal_session",
        )
        self.assertEqual(response["receipt"]["contextWindow"], 372_000)
        self.assertEqual(response["receipt"]["maxTokens"], 16_384)
        self.assertEqual(response["receipt"]["catalogMaxTokens"], 128_000)
        self.assertNotIn('"messages"', str(runtime.prompts[0]["message"])[:300])

    def test_configured_gpt6_luna_uses_the_verified_live_catalog_profile(self) -> None:
        runtime = FakeMemoryRuntime(
            self.sessions,
            self.events,
            models=[{
                "provider": "openai-codex",
                "id": "gpt-6-luna",
                "thinkingLevels": ["off", "high", "max"],
                "contextWindow": 272_000,
                "maxTokens": 128_000,
            }],
        )
        executor = build_governed_memory_model_executor(
            runtime,
            "openai-codex/gpt-6-luna",
            "max",
            db_path=self.db_path,
        )
        executor.begin_run("memory_book_configured_gpt6")
        response = executor.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}],
        )

        self.assertEqual(runtime.model_requests[0]["modelId"], "gpt-6-luna")
        self.assertEqual(runtime.thinking_requests[0]["level"], "max")
        self.assertEqual(response["receipt"]["modelId"], "gpt-6-luna")
        self.assertEqual(response["receipt"]["contextWindow"], 272_000)
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            status = memory_curation_model_status(
                conn,
                required_model="openai-codex/gpt-6-luna",
            )
        self.assertEqual(status["requiredModel"], "openai-codex/gpt-6-luna")

    def test_provider_output_budget_is_capped_by_catalog_and_runtime_protocol(self) -> None:
        runtime = FakeMemoryRuntime(
            self.sessions,
            self.events,
            models=[
                {
                    "provider": "openai-codex",
                    "id": "gpt-5.6-luna",
                    "thinkingLevels": ["max"],
                    "contextWindow": 372_000,
                    "maxTokens": 500_000,
                }
            ],
        )
        executor = self._executor(runtime)
        executor.begin_run("memory_book_protocol_budget")

        response = executor.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}],
            max_tokens=500_000,
        )

        self.assertEqual(runtime.model_requests[0]["maxTokens"], 262_144)
        self.assertEqual(response["receipt"]["maxTokens"], 262_144)
        self.assertEqual(response["receipt"]["catalogMaxTokens"], 500_000)

    def test_authoritative_settlement_completes_when_product_terminal_events_are_lost(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(
            self.sessions,
            self.events,
            emit_terminal_events=False,
        )
        executor = self._executor(runtime)
        run = executor.begin_run("memory_book_settlement_reconciliation")

        response = executor.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
        )

        self.assertEqual(response["choices"][0]["message"]["content"], '{"decisions":[]}')
        self.assertEqual(
            runtime.settlement_calls,
            [
                {
                    "sessionId": run["sessionId"],
                    "turnId": "turn-1",
                    "clientMessageId": runtime.prompts[0]["clientMessageId"],
                    "timeoutSeconds": 1.0,
                }
            ],
        )

    def test_transient_database_open_failure_does_not_lose_accepted_turn_identity(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_transient_turn_binding")
        original_bind_turn = executor._bind_turn
        bind_attempts = 0

        def transient_bind_turn(*, request_id: str, turn_id: str) -> None:
            nonlocal bind_attempts
            bind_attempts += 1
            if bind_attempts == 1:
                raise sqlite3.OperationalError("unable to open database file")
            original_bind_turn(request_id=request_id, turn_id=turn_id)

        executor._bind_turn = transient_bind_turn  # type: ignore[method-assign]

        response = executor.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
        )

        self.assertEqual(response["turnId"], "turn-1")
        self.assertEqual(bind_attempts, 2)
        self.assertEqual(len(runtime.prompts), 1)
        self.assertEqual(runtime.aborted, [])
        request = executor.run_status(
            "memory_book_transient_turn_binding"
        )["requests"][0]
        self.assertEqual(request["state"], "completed")
        self.assertEqual(request["turnId"], "turn-1")

    def test_recovery_after_two_bind_failures_reuses_the_accepted_turn(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_persist_accepted_turn_on_failure")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        original_bind_turn = executor._bind_turn
        original_complete_request = executor._complete_request
        binding_available = False
        bind_attempts = 0
        complete_attempts = 0

        def fail_both_initial_bind_attempts(
            *,
            request_id: str,
            turn_id: str,
        ) -> None:
            nonlocal bind_attempts
            bind_attempts += 1
            if not binding_available:
                raise sqlite3.OperationalError("unable to open database file")
            original_bind_turn(request_id=request_id, turn_id=turn_id)

        executor._bind_turn = (  # type: ignore[method-assign]
            fail_both_initial_bind_attempts
        )

        def fail_first_terminal_write(
            *,
            request_id: str,
            turn_id: str,
            output_text: str,
            receipt: dict[str, object],
        ) -> sqlite3.Row:
            nonlocal complete_attempts
            complete_attempts += 1
            if complete_attempts == 1:
                raise sqlite3.OperationalError("unable to open database file")
            return original_complete_request(
                request_id=request_id,
                turn_id=turn_id,
                output_text=output_text,
                receipt=receipt,
            )

        executor._complete_request = (  # type: ignore[method-assign]
            fail_first_terminal_write
        )

        with self.assertRaises(MemoryModelUnavailable):
            executor.complete(messages=messages)

        self.assertEqual(bind_attempts, 2)
        self.assertEqual(len(runtime.prompts), 1)
        resumable = executor.run_status(
            "memory_book_persist_accepted_turn_on_failure"
        )["requests"][0]
        self.assertEqual(resumable["state"], "resumable")
        self.assertEqual(resumable["turnId"], "turn-1")
        self.assertEqual(
            resumable["receipt"]["admission"],
            {
                "schemaVersion": "rag-ime.memory-admission.v1",
                "accepted": True,
                "sessionId": runtime.prompts[0]["sessionId"],
                "turnId": "turn-1",
                "clientMessageId": runtime.prompts[0]["clientMessageId"],
            },
        )

        binding_available = True
        recovered = executor.complete(messages=messages)

        self.assertEqual(recovered["turnId"], "turn-1")
        self.assertTrue(recovered["receipt"]["recoveredSettlement"])
        self.assertEqual(len(runtime.prompts), 1)
        self.assertEqual(
            executor.run_status(
                "memory_book_persist_accepted_turn_on_failure"
            )["requests"][0]["attemptCount"],
            1,
        )

    def test_two_bind_failures_do_not_interrupt_terminal_settlement(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_deferred_turn_binding")
        bind_attempts = 0

        def unavailable_bind(*, request_id: str, turn_id: str) -> None:
            del request_id, turn_id
            nonlocal bind_attempts
            bind_attempts += 1
            raise sqlite3.OperationalError("unable to open database file")

        executor._bind_turn = unavailable_bind  # type: ignore[method-assign]

        response = executor.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
        )

        self.assertEqual(response["turnId"], "turn-1")
        self.assertEqual(bind_attempts, 2)
        self.assertEqual(len(runtime.prompts), 1)
        self.assertEqual(runtime.aborted, [])
        request = executor.run_status(
            "memory_book_deferred_turn_binding"
        )["requests"][0]
        self.assertEqual(request["state"], "completed")
        self.assertEqual(request["turnId"], "turn-1")

    def test_same_process_persists_admission_when_database_returns_later(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_late_database_recovery")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        original_complete_request = executor._complete_request
        original_mark_request_resumable = executor._mark_request_resumable
        complete_attempts = 0
        resumable_attempts = 0

        def unavailable_bind(*, request_id: str, turn_id: str) -> None:
            del request_id, turn_id
            raise sqlite3.OperationalError("unable to open database file")

        def fail_first_terminal_write(
            *,
            request_id: str,
            turn_id: str,
            output_text: str,
            receipt: dict[str, object],
        ) -> sqlite3.Row:
            nonlocal complete_attempts
            complete_attempts += 1
            if complete_attempts == 1:
                raise sqlite3.OperationalError("unable to open database file")
            return original_complete_request(
                request_id=request_id,
                turn_id=turn_id,
                output_text=output_text,
                receipt=receipt,
            )

        def fail_first_resumable_write(
            *,
            request_id: str,
            error: str,
            receipt: dict[str, object] | None = None,
            turn_id: str = "",
        ) -> None:
            nonlocal resumable_attempts
            resumable_attempts += 1
            if resumable_attempts == 1:
                raise sqlite3.OperationalError("unable to open database file")
            original_mark_request_resumable(
                request_id=request_id,
                error=error,
                receipt=receipt,
                turn_id=turn_id,
            )

        executor._bind_turn = unavailable_bind  # type: ignore[method-assign]
        executor._complete_request = (  # type: ignore[method-assign]
            fail_first_terminal_write
        )
        executor._mark_request_resumable = (  # type: ignore[method-assign]
            fail_first_resumable_write
        )

        with self.assertRaisesRegex(sqlite3.OperationalError, "unable to open"):
            executor.complete(messages=messages)
        before_recovery = executor.run_status(
            "memory_book_late_database_recovery"
        )["requests"][0]
        self.assertEqual(before_recovery["state"], "running")
        self.assertEqual(before_recovery["turnId"], "")

        recovered = executor.complete(messages=messages)

        self.assertEqual(recovered["turnId"], "turn-1")
        self.assertTrue(recovered["receipt"]["recoveredSettlement"])
        self.assertEqual(len(runtime.prompts), 1)
        self.assertEqual(resumable_attempts, 2)
        persisted = executor.run_status(
            "memory_book_late_database_recovery"
        )["requests"][0]
        self.assertEqual(persisted["state"], "completed")
        self.assertEqual(persisted["turnId"], "turn-1")
        self.assertEqual(persisted["attemptCount"], 1)

    def test_new_executor_never_replays_ambiguous_running_admission(self) -> None:
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        first.begin_run("memory_book_ambiguous_running_admission")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        self._leave_accepted_request_running_without_durable_identity(
            first,
            messages=messages,
        )
        ambiguous = first.run_status(
            "memory_book_ambiguous_running_admission"
        )["requests"][0]
        self.assertEqual(ambiguous["state"], "running")
        self.assertEqual(ambiguous["turnId"], "")
        self.assertEqual(ambiguous["attemptCount"], 1)
        self.assertEqual(len(first_runtime.prompts), 1)
        first.close()

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        second.begin_run("memory_book_ambiguous_running_admission")

        with self.assertRaisesRegex(
            MemoryModelUnavailable,
            "ambiguous.*not replayed",
        ):
            second.complete(messages=messages)

        self.assertEqual(second_runtime.prompts, [])
        preserved = second.run_status(
            "memory_book_ambiguous_running_admission"
        )["requests"][0]
        self.assertEqual(preserved["state"], "resumable")
        self.assertEqual(preserved["turnId"], "")
        self.assertEqual(preserved["attemptCount"], 1)
        self.assertEqual(
            preserved["lastError"],
            "memory_admission_ambiguous_not_replayed",
        )
        self.assertEqual(preserved["receipt"]["admission"]["status"], "unknown")

    def test_new_executor_recovers_turn_from_durable_runtime_snapshot(self) -> None:
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        first.begin_run("memory_book_snapshot_admission_recovery")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        self._leave_accepted_request_running_without_durable_identity(
            first,
            messages=messages,
        )
        ambiguous = first.run_status(
            "memory_book_snapshot_admission_recovery"
        )["requests"][0]
        original_session_id = str(ambiguous["sessionId"])
        request_id = str(first_runtime.prompts[0]["clientMessageId"])
        first.close()

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second_runtime.snapshots[original_session_id] = {
            "messages": [
                {
                    "role": "user",
                    "turnId": "turn-1",
                    "clientMessageId": request_id,
                    "blocks": [{"type": "text", "data": {"text": "private"}}],
                }
            ]
        }
        second_runtime.settlements["turn-1"] = dict(
            first_runtime.settlements["turn-1"]
        )
        second = self._executor(second_runtime)
        second.begin_run("memory_book_snapshot_admission_recovery")

        recovered = second.complete(messages=messages)

        self.assertEqual(recovered["turnId"], "turn-1")
        self.assertTrue(recovered["receipt"]["recoveredSettlement"])
        self.assertEqual(second_runtime.prompts, [])
        persisted = second.run_status(
            "memory_book_snapshot_admission_recovery"
        )["requests"][0]
        self.assertEqual(persisted["state"], "completed")
        self.assertEqual(persisted["turnId"], "turn-1")
        self.assertEqual(persisted["attemptCount"], 1)

    def test_new_executor_never_replays_when_prompt_ack_is_lost(self) -> None:
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        first.begin_run("memory_book_lost_prompt_ack")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        original_prompt = first_runtime.prompt

        def accept_then_lose_ack(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            original_prompt(*args, **kwargs)
            raise TimeoutError("prompt acknowledgement timed out")

        first_runtime.prompt = accept_then_lose_ack  # type: ignore[method-assign]

        with self.assertRaises(MemoryModelTimeout):
            first.complete(messages=messages)
        ambiguous = first.run_status("memory_book_lost_prompt_ack")["requests"][0]
        self.assertEqual(ambiguous["state"], "resumable")
        self.assertEqual(ambiguous["turnId"], "")
        self.assertEqual(ambiguous["attemptCount"], 1)
        self.assertEqual(ambiguous["lastError"], "memory_curation_timeout")
        self.assertEqual(len(first_runtime.prompts), 1)
        first.close()

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        second.begin_run("memory_book_lost_prompt_ack")

        with self.assertRaisesRegex(
            MemoryModelUnavailable,
            "ambiguous.*not replayed",
        ):
            second.complete(messages=messages)

        self.assertEqual(second_runtime.prompts, [])
        preserved = second.run_status("memory_book_lost_prompt_ack")["requests"][0]
        self.assertEqual(preserved["state"], "resumable")
        self.assertEqual(preserved["turnId"], "")
        self.assertEqual(preserved["attemptCount"], 1)

    def test_near_budget_packet_is_not_truncated_or_nested_as_json_messages(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_near_budget")
        sentinels = "BEGIN-SENTINEL" + ("甲" * 700_000) + "END-SENTINEL"

        response = executor.complete(
            messages=[
                {"role": "system", "content": "return JSON"},
                {"role": "user", "content": sentinels},
            ]
        )

        transported = str(runtime.prompts[0]["message"])
        self.assertIn("BEGIN-SENTINEL", transported)
        self.assertIn("END-SENTINEL", transported)
        self.assertGreater(len(transported), 700_000)
        self.assertEqual(response["receipt"]["inputChars"], len(transported))
        status = executor.run_status("memory_book_near_budget")
        self.assertEqual(status["requests"][0]["state"], "completed")
        self.assertGreater(status["requests"][0]["inputChars"], 700_000)

    def test_isolated_request_uses_and_retires_a_fresh_internal_session(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        run = executor.begin_run("memory_book_independent_verifier")

        first = executor.complete(
            phase="evidence-adjudication",
            messages=[{"role": "user", "content": '{"v":2,"e":[["E1"]]}'}],
        )
        verifier = executor.complete(
            phase="independent-verifier",
            isolated=True,
            messages=[{"role": "user", "content": '{"v":2,"verify":1}'}],
        )

        self.assertEqual(first["receipt"]["sessionId"], run["sessionId"])
        self.assertNotEqual(
            verifier["receipt"]["sessionId"],
            run["sessionId"],
        )
        status = executor.run_status("memory_book_independent_verifier")
        self.assertEqual(
            [request["sessionId"] for request in status["requests"]],
            [first["receipt"]["sessionId"], verifier["receipt"]["sessionId"]],
        )

        executor.finish_run(state="completed")

        self.assertCountEqual(
            runtime.closed,
            [first["receipt"]["sessionId"], verifier["receipt"]["sessionId"]],
        )
        for session_id in runtime.closed:
            self.assertEqual(self.sessions.get(session_id)["status"], "archived")

    def test_public_status_reports_three_pass_receipts_without_contents(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        run = executor.begin_run("memory_book_public_status")
        for phase, isolated in (
            ("evidence-adjudication", False),
            ("atom-adjudication", False),
            ("independent-verifier", True),
        ):
            executor.complete(
                phase=phase,
                isolated=isolated,
                messages=[{"role": "user", "content": f"private-{phase}"}],
            )
        executor.finish_run(state="completed")

        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            status = memory_curation_model_status(conn)

        self.assertEqual(status["requiredModel"], "openai-codex/gpt-6.1-sol")
        self.assertEqual(status["requiredThinkingLevel"], "max")
        self.assertEqual(status["minimumContextTokens"], 272_000)
        latest = status["runs"][0]
        self.assertEqual(latest["runId"], "memory_book_public_status")
        self.assertEqual(latest["state"], "completed")
        requests = latest["requests"]
        self.assertEqual(
            [request["phase"] for request in requests],
            ["evidence-adjudication", "atom-adjudication", "independent-verifier"],
        )
        self.assertEqual(requests[0]["sessionId"], run["sessionId"])
        self.assertEqual(requests[1]["sessionId"], run["sessionId"])
        self.assertNotEqual(requests[2]["sessionId"], run["sessionId"])
        for request in requests:
            self.assertNotIn("messagesJson", request)
            self.assertNotIn("outputText", request)
            self.assertEqual(request["receipt"]["modelId"], "gpt-5.6-luna")
            self.assertEqual(request["receipt"]["thinkingLevel"], "max")
            self.assertEqual(request["receipt"]["contextWindow"], 372_000)
            self.assertNotIn("private-", str(request))

    def test_completed_request_is_reused_from_database_after_executor_restart(self) -> None:
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        frozen = hashlib.sha256(b"stable-batch").hexdigest()
        first.begin_run("memory_book_resume", frozen_input_sha256=frozen)
        messages = [{"role": "user", "content": '{"v":2,"e":[["e1"]]}'}]
        initial = first.complete(messages=messages)

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        second.begin_run("memory_book_resume", frozen_input_sha256=frozen)
        resumed = second.complete(messages=messages)

        self.assertEqual(resumed["requestId"], initial["requestId"])
        self.assertEqual(second_runtime.prompts, [])
        self.assertEqual(
            second.run_status("memory_book_resume")["requests"][0]["attemptCount"],
            1,
        )

    def test_failed_run_keeps_receipt_and_retries_in_stable_successor_attempt(self) -> None:
        frozen = hashlib.sha256(b"failed-batch").hexdigest()
        messages = [{"role": "user", "content": '{"v":2,"e":[["e1"]]}'}]
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        original = first.begin_run(
            "memory_book_failed_retry",
            frozen_input_sha256=frozen,
        )
        first.complete(messages=messages)
        first.fail_run(RuntimeError("invalid delivery contract"))

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        successor = second.begin_run(
            "memory_book_failed_retry",
            frozen_input_sha256=frozen,
        )

        self.assertEqual(successor["runId"], "memory_book_failed_retry:attempt:2")
        self.assertEqual(successor["state"], "prepared")
        self.assertNotEqual(successor["sessionId"], original["sessionId"])
        self.assertEqual(
            second.run_status("memory_book_failed_retry")["state"],
            "failed",
        )
        second.complete(messages=messages)
        second.finish_run(state="completed")
        self.assertEqual(len(second_runtime.prompts), 1)

        third_runtime = FakeMemoryRuntime(self.sessions, self.events)
        third = self._executor(third_runtime)
        resumed = third.begin_run(
            "memory_book_failed_retry",
            frozen_input_sha256=frozen,
        )
        third.complete(messages=messages)

        self.assertEqual(resumed["runId"], successor["runId"])
        self.assertEqual(resumed["state"], "completed")
        self.assertEqual(third_runtime.prompts, [])

    def test_model_change_uses_successor_without_replaying_old_frozen_run(self) -> None:
        frozen = hashlib.sha256(b"activity-day-under-old-model").hexdigest()
        old_runtime = FakeMemoryRuntime(self.sessions, self.events)
        old = self._executor(old_runtime)
        old_run = old.begin_run(
            "activity-organization:model-change",
            frozen_input_sha256=frozen,
        )
        old.complete(messages=[{"role": "user", "content": '{"v":2,"e":[]}'}])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_curation_model_runs SET state = 'running' WHERE run_id = ?",
                (old_run["runId"],),
            )

        new_runtime = FakeMemoryRuntime(
            self.sessions,
            self.events,
            models=[{
                "provider": "openai-codex",
                "id": "gpt-6-luna",
                "thinkingLevels": ["off", "high", "max"],
                "contextWindow": 272_000,
                "maxTokens": 128_000,
            }],
        )
        changed = build_governed_memory_model_executor(
            new_runtime,
            "openai-codex/gpt-6-luna",
            "max",
            db_path=self.db_path,
        )
        with self.assertRaisesRegex(MemoryModelUnavailable, "active under a different"):
            changed.begin_run(
                "activity-organization:model-change",
                frozen_input_sha256=frozen,
            )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE memory_curation_model_runs SET state = 'resumable' WHERE run_id = ?",
                (old_run["runId"],),
            )
        successor = changed.begin_run(
            "activity-organization:model-change",
            frozen_input_sha256=frozen,
        )
        response = changed.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}],
        )

        self.assertEqual(successor["runId"], f"{old_run['runId']}:attempt:2")
        self.assertNotEqual(successor["sessionId"], old_run["sessionId"])
        self.assertEqual(response["receipt"]["modelId"], "gpt-6-luna")
        self.assertEqual(old.run_status(old_run["runId"])["state"], "resumable")
        self.assertEqual(old.run_status(old_run["runId"])["requests"][0]["state"], "completed")
        self.assertEqual(len(old_runtime.prompts), 1)
        self.assertEqual(len(new_runtime.prompts), 1)

    def test_interrupted_running_run_is_recovered_as_resumable(self) -> None:
        frozen = hashlib.sha256(b"interrupted-batch").hexdigest()
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        original = first.begin_run(
            "memory_book_interrupted",
            frozen_input_sha256=frozen,
        )
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET state = 'running', updated_at_ms = updated_at_ms - 60000
                WHERE run_id = ?
                """,
                (original["runId"],),
            )

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        recovered = second.begin_run(
            "memory_book_interrupted",
            frozen_input_sha256=frozen,
        )

        self.assertEqual(recovered["runId"], original["runId"])
        self.assertEqual(recovered["state"], "resumable")
        self.assertEqual(second_runtime.aborted, [original["sessionId"]])
        second.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
        )
        self.assertEqual(len(second_runtime.prompts), 1)

    def test_timeout_aborts_turn_but_keeps_frozen_run_resumable(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        executor = self._executor(runtime, timeout_seconds=0.01)
        run = executor.begin_run("memory_book_timeout")

        with self.assertRaisesRegex(MemoryModelTimeout, "remains resumable"):
            executor.complete(
                messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
            )

        self.assertEqual(runtime.aborted, [run["sessionId"]])
        status = executor.run_status("memory_book_timeout")
        self.assertEqual(status["state"], "resumable")
        self.assertEqual(status["requests"][0]["state"], "resumable")
        self.assertEqual(self.sessions.get(str(run["sessionId"]))["status"], "idle")
        self.assertEqual(runtime.closed, [])

    def test_retry_replays_only_a_confirmed_timeout_abort(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        executor = self._executor(runtime, timeout_seconds=0.01)
        executor.begin_run("memory_book_confirmed_timeout_abort")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        original_abort = runtime.abort

        def abort_with_settlement(session_id: str) -> dict[str, object]:
            original_abort(session_id)
            prompt = runtime.prompts[-1]
            turn_id = str(prompt["turnId"])
            runtime.settlements[turn_id] = {
                "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                "sessionId": session_id,
                "runtimeSessionId": f"pi-{session_id}",
                "turnId": turn_id,
                "clientMessageId": prompt["clientMessageId"],
                "receipt": {
                    "schemaVersion": "pi.agent-settled.v2",
                    "sessionId": f"pi-{session_id}",
                    "disposition": "aborted",
                    "stopReason": "aborted",
                },
            }
            return {
                "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                "sessionId": session_id,
                "turnId": turn_id,
                "lifecycle": {"drained": True, "idle": True},
            }

        runtime.abort = abort_with_settlement  # type: ignore[method-assign]
        with self.assertRaises(MemoryModelTimeout):
            executor.complete(messages=messages)
        first = executor.run_status("memory_book_confirmed_timeout_abort")["requests"][0]
        self.assertEqual(first["state"], "resumable")
        self.assertEqual(len(runtime.prompts), 1)

        runtime.settle = True
        completed = executor.complete(messages=messages)
        self.assertEqual(len(runtime.prompts), 2)
        self.assertEqual(
            runtime.prompts[1]["clientMessageId"], first["requestId"]
        )
        self.assertNotEqual(
            runtime.prompts[1]["sessionId"], runtime.prompts[0]["sessionId"]
        )
        self.assertEqual(completed["receipt"]["replayRecovery"]["reasonCode"], "own_timeout_abort")
        self.assertEqual(
            executor.run_status("memory_book_confirmed_timeout_abort")["requests"][0]["attemptCount"],
            2,
        )

    def test_aborted_turn_without_matching_abort_receipt_is_not_replayed(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        executor = self._executor(runtime, timeout_seconds=0.01)
        executor.begin_run("memory_book_unproven_abort")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]

        with self.assertRaises(MemoryModelTimeout):
            executor.complete(messages=messages)
        prompt = runtime.prompts[0]
        runtime.settlements[str(prompt["turnId"])] = {
            "schemaVersion": "rag-ime.pi-turn-settlement.v1",
            "sessionId": prompt["sessionId"],
            "runtimeSessionId": f"pi-{prompt['sessionId']}",
            "turnId": prompt["turnId"],
            "clientMessageId": prompt["clientMessageId"],
            "receipt": {
                "schemaVersion": "pi.agent-settled.v2",
                "sessionId": f"pi-{prompt['sessionId']}",
                "disposition": "aborted",
                "stopReason": "aborted",
            },
        }

        with self.assertRaisesRegex(MemoryModelUnavailable, "not replayed: aborted"):
            executor.complete(messages=messages)
        self.assertEqual(len(runtime.prompts), 1)

    def test_settlement_read_timeout_keeps_the_accepted_turn_live_and_resumable(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        run = executor.begin_run("memory_settlement_read_timeout")
        messages = [{"role": "user", "content": '{"fixture":true}'}]
        original_await = runtime.await_turn_settled

        def unavailable_settlement(*args, **kwargs):
            raise PiRuntimeSettlementLookupTimeout("Pi Session settlement lookup timed out")

        runtime.await_turn_settled = unavailable_settlement
        with self.assertRaisesRegex(MemoryModelUnavailable, "settlement lookup timed out.*not replayed") as caught:
            executor.complete(messages=messages)
        self.assertNotIsInstance(caught.exception, MemoryModelTimeout)
        self.assertEqual(executor.fail_run(caught.exception)["state"], "resumable")
        self.assertEqual(runtime.aborted, [])
        self.assertEqual(runtime.closed, [])
        request = executor.run_status(run["runId"])["requests"][0]
        self.assertEqual(request["state"], "resumable")
        self.assertEqual(request["lastError"], "memory_settlement_lookup_timeout")
        self.assertEqual(request["turnId"], "turn-1")
        self.assertEqual(request["attemptCount"], 1)

        runtime.await_turn_settled = original_await
        recovered = executor.complete(messages=messages)
        self.assertEqual(recovered["turnId"], "turn-1")
        self.assertEqual(len(runtime.prompts), 1)

    def test_timeout_cancels_only_the_memory_session(self) -> None:
        ordinary = self.sessions.create(
            title="ordinary conversation",
            session_kind="conversation",
        )
        ordinary_session_id = str(ordinary["id"])
        self.sessions.bind_runtime_session(
            ordinary_session_id,
            driver_id="pi-runtime-v2",
            runtime_kind="pi",
            external_session_id="pi-ordinary-live",
        )
        self.sessions.set_status(ordinary_session_id, "busy")
        runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        executor = self._executor(runtime, timeout_seconds=0.01)
        memory_run = executor.begin_run("memory_book_timeout_isolation")

        with self.assertRaises(MemoryModelTimeout):
            executor.complete(
                messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
            )

        self.assertEqual(runtime.aborted, [memory_run["sessionId"]])
        self.assertEqual(self.sessions.get(ordinary_session_id)["status"], "busy")
        self.assertEqual(
            self.sessions.runtime_binding(ordinary_session_id)["externalSessionId"],
            "pi-ordinary-live",
        )

    def test_retry_reuses_late_durable_settlement_without_reprompting(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        executor = self._executor(runtime, timeout_seconds=0.01)
        executor.begin_run("memory_book_late_settlement")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]

        with self.assertRaises(MemoryModelTimeout):
            executor.complete(messages=messages)
        first_prompt = runtime.prompts[0]
        runtime.settlements["turn-1"] = {
            "schemaVersion": "rag-ime.pi-turn-settlement.v1",
            "sessionId": first_prompt["sessionId"],
            "runtimeSessionId": f"pi-{first_prompt['sessionId']}",
            "turnId": "turn-1",
            "clientMessageId": first_prompt["clientMessageId"],
            "receipt": {
                "schemaVersion": "pi.agent-settled.v2",
                "receiptId": "pi-settled:turn-1",
                "sessionId": f"pi-{first_prompt['sessionId']}",
                "runId": "turn-1",
                "scopeId": f"pi-{first_prompt['sessionId']}:turn-1",
                "generation": 1,
                "disposition": "completed",
                "stopReason": "stop",
                "settledAtMs": 200,
                "aborted": False,
                "pendingOperations": 0,
                "operations": {
                    "pending": 0,
                    "pendingByKind": {},
                    "registeredByKind": {},
                },
                "operationCounts": {},
                "finalMessage": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": '{"decisions":[]}'}],
                    "usage": {"input": 12, "output": 4},
                },
            },
        }
        runtime.settle = True

        recovered = executor.complete(messages=messages)

        self.assertEqual(recovered["turnId"], "turn-1")
        self.assertTrue(recovered["receipt"]["recoveredSettlement"])
        self.assertEqual(len(runtime.prompts), 1)
        request = executor.run_status("memory_book_late_settlement")["requests"][0]
        self.assertEqual(request["state"], "completed")
        self.assertEqual(request["attemptCount"], 1)

    def test_timeout_reconciles_completion_that_settles_during_cancellation(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        executor = self._executor(runtime, timeout_seconds=10.0)
        run = executor.begin_run("memory_book_settles_during_cancel")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        original_abort = runtime.abort

        def settle_during_abort(session_id: str) -> dict[str, object]:
            prompt = runtime.prompts[0]
            runtime.settlements["turn-1"] = {
                "schemaVersion": "rag-ime.pi-turn-settlement.v1",
                "sessionId": session_id,
                "runtimeSessionId": f"pi-{session_id}",
                "turnId": "turn-1",
                "clientMessageId": prompt["clientMessageId"],
                "receipt": {
                    "schemaVersion": "pi.agent-settled.v2",
                    "receiptId": "pi-settled:turn-1",
                    "sessionId": f"pi-{session_id}",
                    "runId": "turn-1",
                    "scopeId": f"pi-{session_id}:turn-1",
                    "generation": 1,
                    "disposition": "completed",
                    "stopReason": "stop",
                    "settledAtMs": 200,
                    "aborted": False,
                    "pendingOperations": 0,
                    "operations": {
                        "pending": 0,
                        "pendingByKind": {},
                        "registeredByKind": {},
                    },
                    "operationCounts": {},
                    "finalMessage": {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": '{"decisions":[]}'}
                        ],
                        "usage": {"input": 12, "output": 4},
                    },
                },
            }
            runtime.settle = True
            original_abort(session_id)
            raise RuntimeError(
                "Pi Runtime Host command timed out: session.abort"
            )

        runtime.abort = settle_during_abort  # type: ignore[method-assign]

        completed = executor.complete(messages=messages)

        self.assertEqual(completed["turnId"], "turn-1")
        self.assertTrue(completed["receipt"]["recoveredSettlement"])
        self.assertEqual(
            completed["receipt"]["cancellation"]["error"],
            "Pi Runtime Host command timed out: session.abort",
        )
        self.assertEqual(runtime.aborted, [run["sessionId"]])
        self.assertEqual(len(runtime.prompts), 1)
        self.assertEqual(
            [call["timeoutSeconds"] for call in runtime.settlement_calls],
            [10.0, 5.0],
        )
        request = executor.run_status(
            "memory_book_settles_during_cancel"
        )["requests"][0]
        self.assertEqual(request["state"], "completed")
        self.assertEqual(request["attemptCount"], 1)

    def test_unresolved_accepted_turn_is_not_reprompted(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        executor = self._executor(runtime, timeout_seconds=0.01)
        executor.begin_run("memory_book_unresolved_accepted_turn")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]

        with self.assertRaises(MemoryModelTimeout):
            executor.complete(messages=messages)
        with self.assertRaisesRegex(
            MemoryModelTimeout,
            "not replayed",
        ):
            executor.complete(messages=messages)

        self.assertEqual(len(runtime.prompts), 1)
        request = executor.run_status(
            "memory_book_unresolved_accepted_turn"
        )["requests"][0]
        self.assertEqual(request["state"], "resumable")
        self.assertEqual(request["turnId"], "turn-1")
        self.assertEqual(request["attemptCount"], 1)

    def test_restart_replays_frozen_request_after_old_session_is_archived(self) -> None:
        first_runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        first = self._executor(first_runtime, timeout_seconds=0.01)
        first.begin_run("memory_book_restart_after_admission")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]

        with self.assertRaises(MemoryModelTimeout):
            first.complete(messages=messages)
        persisted = first.run_status(
            "memory_book_restart_after_admission"
        )["requests"][0]
        self.assertEqual(persisted["turnId"], "turn-1")
        original_session_id = str(persisted["sessionId"])
        reconcile_stale_memory_runtime_sessions(
            self.sessions,
            db_path=self.db_path,
        )

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime, timeout_seconds=0.01)
        resumed = second.begin_run("memory_book_restart_after_admission")
        self.assertNotEqual(resumed["sessionId"], original_session_id)

        completed = second.complete(messages=messages)

        self.assertEqual(len(second_runtime.prompts), 1)
        self.assertNotEqual(
            second_runtime.prompts[0]["sessionId"],
            original_session_id,
        )
        self.assertEqual(
            second_runtime.prompts[0]["clientMessageId"],
            persisted["requestId"],
        )
        self.assertEqual(
            completed["receipt"]["replayRecovery"],
            {
                "schemaVersion": "rag-ime.memory-request-replay-recovery.v1",
                "reasonCode": "runtime_session_archived",
                "previousSessionId": original_session_id,
                "previousTurnId": "turn-1",
                "previousAdmission": persisted["receipt"]["admission"],
            },
        )
        after_restart = second.run_status(
            "memory_book_restart_after_admission"
        )["requests"][0]
        self.assertEqual(after_restart["state"], "completed")
        self.assertNotEqual(after_restart["sessionId"], original_session_id)
        self.assertEqual(after_restart["turnId"], "turn-1")
        self.assertEqual(after_restart["attemptCount"], 2)

    def test_restart_replays_when_idle_accepted_session_lost_its_transcript(
        self,
    ) -> None:
        first_runtime = FakeMemoryRuntime(self.sessions, self.events, settle=False)
        first = self._executor(first_runtime, timeout_seconds=0.01)
        first.begin_run("memory_book_restart_without_transcript")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]

        with self.assertRaises(MemoryModelTimeout):
            first.complete(messages=messages)
        persisted = first.run_status(
            "memory_book_restart_without_transcript"
        )["requests"][0]
        original_session_id = str(persisted["sessionId"])
        missing_transcript = self.temporary.name + "/missing-memory-session.jsonl"
        self.sessions.bind_runtime_session(
            original_session_id,
            driver_id="managed-pi",
            runtime_kind="pi_rpc",
            external_session_id="pi-memory-missing-transcript",
            transcript_ref=missing_transcript,
        )
        self.sessions.set_status(original_session_id, "idle")

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime, timeout_seconds=0.01)
        second.begin_run("memory_book_restart_without_transcript")

        completed = second.complete(messages=messages)

        self.assertEqual(len(second_runtime.prompts), 1)
        self.assertNotEqual(
            second_runtime.prompts[0]["sessionId"],
            original_session_id,
        )
        self.assertEqual(
            completed["receipt"]["replayRecovery"]["reasonCode"],
            "runtime_transcript_missing",
        )
        replayed = second.run_status(
            "memory_book_restart_without_transcript"
        )["requests"][0]
        self.assertEqual(replayed["state"], "completed")
        self.assertEqual(replayed["attemptCount"], 2)

    def test_explicit_admission_without_turn_id_is_never_blindly_replayed(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_admission_without_turn_id")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        original_prompt = runtime.prompt

        def accepted_without_turn_id(
            *args: object,
            **kwargs: object,
        ) -> dict[str, object]:
            original_prompt(*args, **kwargs)
            return {"accepted": True}

        runtime.prompt = accepted_without_turn_id  # type: ignore[method-assign]

        with self.assertRaisesRegex(MemoryModelUnavailable, "turn id"):
            executor.complete(messages=messages)
        resumable = executor.run_status(
            "memory_book_admission_without_turn_id"
        )["requests"][0]
        self.assertEqual(resumable["turnId"], "")
        self.assertTrue(resumable["receipt"]["admission"]["accepted"])

        with self.assertRaisesRegex(MemoryModelUnavailable, "not replayed"):
            executor.complete(messages=messages)

        self.assertEqual(len(runtime.prompts), 1)
        self.assertEqual(
            executor.run_status(
                "memory_book_admission_without_turn_id"
            )["requests"][0]["attemptCount"],
            1,
        )

    def test_failed_accepted_turn_is_reported_and_not_reprompted(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_failed_accepted_turn")
        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        original_await_turn_settled = runtime.await_turn_settled

        def failed_settlement(
            session_id: str,
            turn_id: str,
            *,
            client_message_id: str,
            timeout_seconds: float,
        ) -> dict[str, object]:
            settlement = original_await_turn_settled(
                session_id,
                turn_id,
                client_message_id=client_message_id,
                timeout_seconds=timeout_seconds,
            )
            receipt = dict(settlement["receipt"])
            receipt["disposition"] = "failed"
            receipt["stopReason"] = "provider_terminal_failure"
            settlement["receipt"] = receipt
            return settlement

        runtime.await_turn_settled = (  # type: ignore[method-assign]
            failed_settlement
        )

        with self.assertRaisesRegex(
            MemoryModelUnavailable,
            "provider_terminal_failure",
        ):
            executor.complete(messages=messages)
        with self.assertRaisesRegex(
            MemoryModelUnavailable,
            "not replayed.*provider_terminal_failure",
        ):
            executor.complete(messages=messages)

        self.assertEqual(len(runtime.prompts), 1)
        request = executor.run_status(
            "memory_book_failed_accepted_turn"
        )["requests"][0]
        self.assertEqual(request["state"], "resumable")
        self.assertEqual(request["turnId"], "turn-1")
        self.assertIn("provider_terminal_failure", request["lastError"])

    def test_resumable_isolated_request_retries_in_a_fresh_internal_session(self) -> None:
        messages = [{"role": "user", "content": '{"v":2,"activity":[]}'}]
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        first.begin_run("memory_book_isolated_active_turn")
        original_prompt = first_runtime.prompt

        def reject_active_turn(*args: object, **kwargs: object) -> dict[str, object]:
            raise PiRuntimeTurnConflict("Session already has an active turn")

        first_runtime.prompt = reject_active_turn  # type: ignore[method-assign]
        with self.assertRaisesRegex(MemoryModelUnavailable, "active turn"):
            first.complete(
                phase="activity-organizer",
                isolated=True,
                messages=messages,
            )
        failed_request = first.run_status(
            "memory_book_isolated_active_turn"
        )["requests"][0]
        failed_session_id = str(failed_request["sessionId"])
        self.assertEqual(failed_request["state"], "resumable")
        self.assertEqual(
            failed_request["receipt"]["admission"],
            {
                "schemaVersion": "rag-ime.memory-admission.v1",
                "status": "rejected",
                "reasonCode": "AGENT_TURN_CONFLICT",
                "sessionId": failed_session_id,
                "clientMessageId": failed_request["requestId"],
            },
        )
        first_runtime.prompt = original_prompt  # type: ignore[method-assign]

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        second.begin_run("memory_book_isolated_active_turn")
        completed = second.complete(
            phase="activity-organizer",
            isolated=True,
            messages=messages,
        )

        self.assertNotEqual(completed["receipt"]["sessionId"], failed_session_id)
        self.assertEqual(first_runtime.aborted, [failed_session_id])
        self.assertEqual(second_runtime.closed, [failed_session_id])
        self.assertEqual(self.sessions.get(failed_session_id)["status"], "archived")
        self.assertEqual(len(second_runtime.prompts), 1)
        retried = second.run_status(
            "memory_book_isolated_active_turn"
        )["requests"][0]
        self.assertEqual(retried["state"], "completed")
        self.assertEqual(retried["attemptCount"], 2)

    def test_resumable_primary_request_retries_in_a_fresh_internal_session(self) -> None:
        """A cancelled Pi turn may remain active after the Gateway lease ends.

        Retrying the frozen owner-curation request in that same Session would
        fail forever with ``Session already has an active turn``.  Recovery
        must preserve the frozen request identity while moving transport to a
        fresh internal Session.
        """

        messages = [{"role": "user", "content": '{"v":2,"e":[]}'}]
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        run = first.begin_run("memory_book_primary_active_turn")
        original_session_id = str(run["sessionId"])

        def reject_active_turn(*args: object, **kwargs: object) -> dict[str, object]:
            raise PiRuntimeTurnConflict("Session already has an active turn")

        first_runtime.prompt = reject_active_turn  # type: ignore[method-assign]
        with self.assertRaisesRegex(MemoryModelUnavailable, "active turn"):
            first.complete(phase="evidence-adjudication", messages=messages)
        failed = first.run_status("memory_book_primary_active_turn")["requests"][0]
        self.assertEqual(failed["state"], "resumable")
        self.assertEqual(failed["sessionId"], original_session_id)
        self.assertEqual(
            failed["receipt"]["admission"]["reasonCode"],
            "AGENT_TURN_CONFLICT",
        )

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        second.begin_run("memory_book_primary_active_turn")
        completed = second.complete(
            phase="evidence-adjudication",
            messages=messages,
        )

        self.assertNotEqual(completed["receipt"]["sessionId"], original_session_id)
        self.assertEqual(first_runtime.aborted, [original_session_id])
        self.assertEqual(second_runtime.closed, [original_session_id])
        self.assertEqual(self.sessions.get(original_session_id)["status"], "archived")
        self.assertEqual(len(second_runtime.prompts), 1)
        retried = second.run_status("memory_book_primary_active_turn")["requests"][0]
        self.assertEqual(retried["state"], "completed")
        self.assertEqual(retried["attemptCount"], 2)

    def test_gateway_restart_retires_only_memory_sessions_and_is_idempotent(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        run = executor.begin_run("memory_book_gateway_restart")
        memory_session_id = str(run["sessionId"])
        ordinary = self.sessions.create(
            title="ordinary conversation",
            session_kind="conversation",
        )
        ordinary_session_id = str(ordinary["id"])
        orphaned_messages = [
            {"role": "user", "content": '{"v":2,"activity":[]}'},
        ]
        orphaned_messages_json = json.dumps(
            orphaned_messages,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        orphaned_input_sha256 = hashlib.sha256(
            orphaned_messages_json.encode("utf-8")
        ).hexdigest()
        self.sessions.set_status(memory_session_id, "busy")
        self.sessions.bind_runtime_session(
            memory_session_id,
            driver_id="pi-runtime-v2",
            runtime_kind="pi",
            external_session_id="pi-memory-stale",
        )
        self.sessions.bind_runtime_session(
            ordinary_session_id,
            driver_id="pi-runtime-v2",
            runtime_kind="pi",
            external_session_id="pi-ordinary-live",
        )
        self.sessions.set_status(ordinary_session_id, "busy")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET state = 'running'
                WHERE run_id = ?
                """,
                (run["runId"],),
            )
            conn.execute(
                """
                INSERT INTO memory_curation_model_requests(
                    request_id, run_id, phase, ordinal, input_sha256,
                    messages_json, input_chars, session_id, state,
                    created_at_ms, updated_at_ms
                ) VALUES (?, ?, 'activity-organizer', 1, ?, '[]', 2, ?,
                          'running', 1, 1)
                """,
                (
                    "memory-request:gateway-restart",
                    run["runId"],
                    "0" * 64,
                    memory_session_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO memory_curation_model_runs(
                    run_id, session_id, profile, provider, model_id,
                    thinking_level, frozen_input_sha256, state,
                    created_at_ms, updated_at_ms
                ) VALUES ('memory_book_orphaned_run', NULL, 'MEMORY_CURATION',
                          'openai-codex', 'gpt-5.6-luna', 'max', '', 'running',
                          1, 1)
                """
            )
            conn.execute(
                """
                INSERT INTO memory_curation_model_requests(
                    request_id, run_id, phase, ordinal, input_sha256,
                    messages_json, input_chars, session_id, state,
                    created_at_ms, updated_at_ms
                ) VALUES ('memory-request:orphaned', 'memory_book_orphaned_run',
                          'activity-organizer', 1, ?, '[]', 2,
                          'agent:missing-memory-session', 'running', 1, 1)
                """,
                (orphaned_input_sha256,),
            )
            conn.execute(
                """
                UPDATE memory_curation_model_requests
                SET messages_json = ?, input_chars = ?
                WHERE request_id = 'memory-request:orphaned'
                """,
                (orphaned_messages_json, len(orphaned_messages_json)),
            )

        receipt = reconcile_stale_memory_runtime_sessions(
            self.sessions,
            db_path=self.db_path,
        )

        self.assertEqual(receipt["recoveredSessionCount"], 1)
        self.assertEqual(receipt["resumableRequestCount"], 2)
        self.assertEqual(receipt["resumableRunCount"], 2)
        status = executor.run_status(str(run["runId"]))
        self.assertEqual(status["state"], "resumable")
        self.assertEqual(status["lastError"], "memory_gateway_restart")
        self.assertEqual(status["requests"][0]["state"], "resumable")
        self.assertEqual(
            status["requests"][0]["lastError"],
            "memory_gateway_restart",
        )
        self.assertEqual(
            self.sessions.get(memory_session_id)["status"],
            "archived",
        )
        self.assertIsNone(self.sessions.runtime_binding(memory_session_id))
        self.assertEqual(self.sessions.get(ordinary_session_id)["status"], "busy")
        self.assertEqual(
            self.sessions.runtime_binding(ordinary_session_id)["externalSessionId"],
            "pi-ordinary-live",
        )
        orphaned = executor.run_status("memory_book_orphaned_run")
        self.assertEqual(orphaned["state"], "resumable")
        self.assertEqual(orphaned["lastError"], "memory_gateway_restart")
        self.assertEqual(orphaned["requests"][0]["state"], "resumable")

        repeated = reconcile_stale_memory_runtime_sessions(
            self.sessions,
            db_path=self.db_path,
        )
        self.assertEqual(repeated["recoveredSessionCount"], 0)
        self.assertEqual(repeated["resumableRequestCount"], 0)
        self.assertEqual(repeated["resumableRunCount"], 0)

        recovery_runtime = FakeMemoryRuntime(self.sessions, self.events)
        recovery = self._executor(recovery_runtime)
        recovered_run = recovery.begin_run("memory_book_orphaned_run")
        recovered_response = recovery.complete(
            phase="activity-organizer",
            messages=orphaned_messages,
        )
        self.assertTrue(str(recovered_run["sessionId"]).startswith("agent:"))
        self.assertNotEqual(
            recovered_response["receipt"]["sessionId"],
            "agent:missing-memory-session",
        )
        self.assertEqual(
            recovery.run_status("memory_book_orphaned_run")["requests"][0]["state"],
            "completed",
        )

    def test_gateway_restart_reopens_terminal_run_with_unfinished_request(
        self,
    ) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        run = executor.begin_run("memory_book_split_terminal")
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET state = 'completed', completed_at_ms = 2
                WHERE run_id = ?
                """,
                (run["runId"],),
            )
            conn.execute(
                """
                INSERT INTO memory_curation_model_requests(
                    request_id, run_id, phase, ordinal, input_sha256,
                    messages_json, input_chars, session_id, state,
                    created_at_ms, updated_at_ms
                ) VALUES ('memory-request:split-terminal', ?, 'model-call', 1,
                          ?, '[]', 2, ?, 'running', 1, 1)
                """,
                (run["runId"], "2" * 64, run["sessionId"]),
            )

        receipt = reconcile_stale_memory_runtime_sessions(
            self.sessions,
            db_path=self.db_path,
        )

        self.assertEqual(receipt["resumableRunCount"], 1)
        self.assertEqual(receipt["resumableRequestCount"], 1)
        status = executor.run_status(str(run["runId"]))
        self.assertEqual(status["state"], "resumable")
        self.assertEqual(status["completedAtMs"], 0)
        self.assertEqual(status["requests"][0]["state"], "resumable")

    def test_gateway_restart_replaces_archived_run_session_before_new_phase(
        self,
    ) -> None:
        first_runtime = FakeMemoryRuntime(self.sessions, self.events)
        first = self._executor(first_runtime)
        run = first.begin_run("memory_book_gateway_restart_before_request")
        stale_session_id = str(run["sessionId"])
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                UPDATE memory_curation_model_runs
                SET state = 'running'
                WHERE run_id = ?
                """,
                (run["runId"],),
            )
        reconcile_stale_memory_runtime_sessions(
            self.sessions,
            db_path=self.db_path,
        )

        second_runtime = FakeMemoryRuntime(self.sessions, self.events)
        second = self._executor(second_runtime)
        resumed = second.begin_run(str(run["runId"]))
        response = second.complete(
            phase="activity-organizer",
            messages=[{"role": "user", "content": '{"v":2,"activity":[]}'}],
        )

        self.assertEqual(resumed["state"], "resumable")
        self.assertNotEqual(response["receipt"]["sessionId"], stale_session_id)
        self.assertEqual(
            second_runtime.prompts[0]["sessionId"],
            response["receipt"]["sessionId"],
        )
        self.assertEqual(self.sessions.get(stale_session_id)["status"], "archived")

    def test_terminal_run_closes_only_its_session_and_retires_projection(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        run = executor.begin_run("memory_book_terminal")
        executor.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
        )

        terminal = executor.finish_run(state="completed")

        self.assertEqual(terminal["state"], "completed")
        self.assertEqual(runtime.closed, [run["sessionId"]])
        archived = self.sessions.get(str(run["sessionId"]))
        self.assertEqual(archived["status"], "archived")
        self.assertEqual(archived["sessionFile"], "")

    def test_retirement_failure_keeps_run_resumable_instead_of_completed(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events)
        executor = self._executor(runtime)
        executor.begin_run("memory_book_retirement_failure")
        executor.complete(
            messages=[{"role": "user", "content": '{"v":2,"e":[]}'}]
        )
        runtime.close_error = RuntimeError("close failed")

        with self.assertRaisesRegex(MemoryModelUnavailable, "remains resumable"):
            executor.finish_run(state="completed")

        status = executor.run_status("memory_book_retirement_failure")
        self.assertEqual(status["state"], "resumable")
        self.assertIn("memory_session_retirement_failed", status["lastError"])

    def test_unavailable_or_short_context_model_fails_closed_without_session(self) -> None:
        runtime = FakeMemoryRuntime(
            self.sessions,
            self.events,
            models=[
                {
                    "provider": "openai-codex",
                    "id": "gpt-5.6-luna",
                    "thinkingLevels": ["max"],
                    "contextWindow": 128_000,
                    "maxTokens": 32_000,
                }
            ],
        )

        with self.assertRaisesRegex(MemoryModelUnavailable, "below 272000"):
            self._executor(runtime)

        self.assertEqual(
            self.sessions.list(include_internal=True, include_archived=True),
            [],
        )

    def test_model_with_too_small_output_budget_fails_closed_without_session(self) -> None:
        runtime = FakeMemoryRuntime(
            self.sessions,
            self.events,
            models=[
                {
                    "provider": "openai-codex",
                    "id": "gpt-5.6-luna",
                    "thinkingLevels": ["max"],
                    "contextWindow": 372_000,
                    "maxTokens": 8_192,
                }
            ],
        )

        with self.assertRaisesRegex(MemoryModelUnavailable, "below 16384"):
            self._executor(runtime)

        self.assertEqual(
            self.sessions.list(include_internal=True, include_archived=True),
            [],
        )

    def test_legacy_gpt_alias_resolves_to_live_canonical_provider(self) -> None:
        runtime = FakeMemoryRuntime(self.sessions, self.events, models=[{
            "provider": "openai-codex", "id": "gpt-6.1-sol", "thinkingLevels": ["max"],
            "contextWindow": 372_000, "maxTokens": 128_000,
        }])

        executor = build_governed_memory_model_executor(
            runtime,
            "gpt/gpt-5.6-luna",
            "max",
            db_path=self.db_path,
        )

        self.assertEqual(executor.reference, "openai-codex/gpt-6.1-sol")


if __name__ == "__main__":
    unittest.main()
