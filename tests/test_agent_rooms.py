from __future__ import annotations

import base64
import json
import sqlite3
import stat
import threading
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import patch

from rag_ime.rooms import store as agent_rooms_module
from rag_ime.agent_context_runtime import RUNTIME_PROMPT_ENVELOPE_PREFIX
from rag_ime.rooms.store import AgentRoomEventHub, AgentRoomStore
from rag_ime.agent_service import AgentService
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.pi.config import PiRuntimeConfig

PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class AgentRoomTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-rooms-")
        self.root = Path(self.tmp.name)
        self.db_path = self.root / "rag-ime.sqlite"
        self.sessions = AgentSessionStore(self.db_path)
        self.sessions.initialize()
        self.store = AgentRoomStore(self.db_path, room_dir=self.root / "rooms")
        self.store.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_room_owns_independent_participant_sessions_and_jsonl_timeline(self) -> None:
        room = self.store.create(
            title="  方案   讨论  ",
            routing_policy="manual_mentions",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
                self._participant("companion-future-v1", "VCP"),
            ],
            created_at_ms=10,
        )

        self.assertEqual(room["title"], "方案 讨论")
        self.assertEqual(room["routingPolicy"], "manual_mentions")
        self.assertEqual(len(room["participants"]), 3)
        self.assertEqual(len({item["sessionId"] for item in room["participants"]}), 3)
        target = self.store.route_target(str(room["id"]), "@Hermes 先检查当前状态")
        self.assertEqual(target["roleId"], "companion-firstlight-v1")
        fallback = self.store.route_target(str(room["id"]), "先检查当前状态")
        self.assertEqual(fallback["roleId"], "companion-present-v1")
        fan_out = self.store.plan_routes(
            str(room["id"]), "@澄 和 @VCP 一起回答"
        )
        self.assertEqual(
            [decision["targetDisplayName"] for decision in fan_out],
            ["澄", "VCP"],
        )
        self.assertTrue(
            all(
                decision["selectedParticipantIds"]
                == [room["participants"][0]["id"], room["participants"][2]["id"]]
                for decision in fan_out
            )
        )

        event = self.store.append_event(
            room_id=str(room["id"]),
            event_type="user_message",
            payload={"text": "@Hermes 先检查当前状态"},
            turn_id="room-turn:1",
            participant_id=str(target["id"]),
            created_at_ms=20,
        )
        self.assertEqual(event["sequence"], 1)
        self.assertEqual(self.store.get(str(room["id"]))["lastEventSequence"], 1)

        room_files = list((self.root / "rooms").glob("*.jsonl"))
        self.assertEqual(len(room_files), 1)
        stored = json.loads(room_files[0].read_text(encoding="utf-8").strip())
        self.assertEqual(stored["eventId"], event["eventId"])
        self.assertEqual(stat.S_IMODE(room_files[0].stat().st_mode), 0o600)

    def test_directory_page_reads_bounded_room_projection(self) -> None:
        room = self.store.create(
            title="快速 Room 目录",
            routing_policy="manual_mentions",
            workspace_roots=[self.root],
            participants=[
                self._participant("companion-present-v1", "Agent A"),
                self._participant("companion-future-v1", "Agent B"),
            ],
            created_at_ms=10,
        )

        item = self.store.list_directory_page(limit=100)["items"][0]

        self.assertEqual(item["id"], room["id"])
        self.assertEqual(item["title"], "快速 Room 目录")
        self.assertEqual(item["status"], "active")
        self.assertEqual(item["workspaceRoots"], [str(self.root.resolve())])
        self.assertEqual([value["displayName"] for value in item["participants"]], ["Agent A", "Agent B"])
        self.assertNotIn("scenarioPrompt", item)
        self.assertNotIn("topics", item)
        self.assertNotIn("artifacts", item)

    def test_room_storage_contract_accepts_four_context_roots_plus_system_root(
        self,
    ) -> None:
        context_roots = [
            self.root / f"context-{index}"
            for index in range(4)
        ]
        room = self.store.create(
            title="四个上下文加系统根",
            routing_policy="manual_mentions",
            workspace_roots=[
                *(str(path) for path in context_roots),
                "/",
            ],
            participants=[
                self._participant("companion-present-v1", "Agent A"),
                self._participant("companion-future-v1", "Agent B"),
            ],
        )

        self.assertEqual(
            room["workspaceRoots"],
            [*(str(path.resolve()) for path in context_roots), "/"],
        )

    def test_parallel_routing_starts_every_peer_and_keeps_work_owner_first(
        self,
    ) -> None:
        room = self.store.create(
            title="平级并行",
            routing_policy="parallel",
            participants=[
                self._participant("companion-present-v1", "澄·今"),
                self._participant("companion-firstlight-v1", "澄·初"),
                self._participant("companion-future-v1", "澄·远"),
            ],
        )
        owner = room["participants"][2]

        decisions = self.store.plan_routes(
            str(room["id"]),
            "三位伙伴各做不同部分，最后一起检查",
            authoritative_participant_id=str(owner["id"]),
        )

        self.assertEqual(
            [decision["targetParticipantId"] for decision in decisions],
            [
                owner["id"],
                room["participants"][0]["id"],
                room["participants"][1]["id"],
            ],
        )
        self.assertTrue(
            all(decision["reason"] == "parallel" for decision in decisions)
        )
        self.assertTrue(
            all(decision["dispatchCount"] == 3 for decision in decisions)
        )
        with self.assertRaisesRegex(
            ValueError,
            "parallel room routing requires plan_room_routes",
        ):
            self.store.plan_route(str(room["id"]), "不要退化成单人工作")
        with self.assertRaisesRegex(ValueError, "conflicts with the WorkItem"):
            self.store.plan_routes(
                str(room["id"]),
                "只叫另一位伙伴",
                requested_participant_ids=[
                    str(room["participants"][0]["id"])
                ],
                authoritative_participant_id=str(owner["id"]),
            )


    def test_parallel_managed_routing_treats_review_as_a_task_duty_not_a_rank(
        self,
    ) -> None:
        room = self.store.create(
            title="实现后独立复核",
            routing_policy="parallel",
            participants=[
                {
                    **self._participant("companion-present-v1", "主持者"),
                    "collaborationRole": "coordinator",
                },
                {
                    **self._participant("companion-firstlight-v1", "实施者"),
                    "collaborationRole": "implementer",
                },
                {
                    **self._participant("companion-future-v1", "审查者"),
                    "collaborationRole": "reviewer",
                },
            ],
        )
        facilitator, implementer, reviewer = room["participants"]

        decisions = self.store.plan_routes(
            str(room["id"]),
            "并行完成实现，集成后再独立复核。",
            authoritative_participant_id=str(facilitator["id"]),
        )
        self.assertEqual(
            [decision["targetParticipantId"] for decision in decisions],
            [facilitator["id"], implementer["id"], reviewer["id"]],
        )
        reviewer_owned = self.store.plan_routes(
            str(room["id"]),
            "直接开始自己负责的功能切片。",
            authoritative_participant_id=str(reviewer["id"]),
        )
        self.assertEqual(
            [decision["targetParticipantId"] for decision in reviewer_owned],
            [reviewer["id"], facilitator["id"], implementer["id"]],
        )

    def test_parallel_unaddressed_conversation_selects_one_responder(
        self,
    ) -> None:
        room = self.store.create(
            title="普通对话不隐式并行",
            routing_policy="parallel",
            participants=[
                self._participant("companion-present-v1", "澄·今"),
                self._participant("companion-firstlight-v1", "澄·初"),
                self._participant("companion-future-v1", "澄·远"),
            ],
        )

        decisions = self.store.plan_routes(
            str(room["id"]),
            "请先回答这个普通问题",
            conversation_only=True,
        )

        self.assertEqual(len(decisions), 1)
        self.assertEqual(len(decisions[0]["selectedParticipantIds"]), 1)
        self.assertEqual(decisions[0]["reason"], "facilitator")
        self.assertEqual(
            decisions[0]["targetParticipantId"],
            room["participants"][0]["id"],
        )

    def test_room_never_persists_or_projects_retired_builtin_role_ids(
        self,
    ) -> None:
        room = self.store.create(
            title="身份迁移",
            routing_policy="natural",
            participants=[
                self._participant("vcp-v1", "未来"),
                self._participant("zhiyou-v1", "此刻"),
            ],
        )

        self.assertEqual(
            [item["roleId"] for item in room["participants"]],
            ["companion-future-v1", "companion-present-v1"],
        )
        with sqlite3.connect(self.db_path) as conn:
            stored = conn.execute(
                """
                SELECT role_id
                FROM agent_room_participants
                WHERE room_id = ?
                ORDER BY ordinal
                """,
                (room["id"],),
            ).fetchall()
        self.assertEqual(
            stored,
            [
                ("companion-future-v1",),
                ("companion-present-v1",),
            ],
        )

    def test_moderator_routes_unaddressed_message_and_sse_replays_once(self) -> None:
        room = self.store.create(
            title="主持讨论",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-future-v1", "VCP"),
            ],
            moderator_ordinal=1,
            created_at_ms=100,
        )
        target = self.store.route_target(str(room["id"]), "请先整理线索")
        self.assertEqual(target["displayName"], "VCP")
        self.assertEqual(room["moderatorParticipantId"], target["id"])

        hub = AgentRoomEventHub(self.store)
        first = hub.publish(
            room_id=str(room["id"]),
            event_type="route_decision",
            payload={"reason": "moderator_default"},
            participant_id=str(target["id"]),
            source_session_id=str(target["sessionId"]),
            turn_id="room-turn:2",
            created_at_ms=110,
        )
        stream = hub.subscribe(str(room["id"]), heartbeat_seconds=0.01)
        self.assertEqual(next(stream), b": connected\n\n")
        replay = next(stream)
        self.assertIn(b"event: route_decision", replay)
        self.assertIn(str(first["eventId"]).encode("utf-8"), replay)
        stream.close()

        resumed = hub.subscribe(str(room["id"]), after_event_id=str(first["eventId"]), heartbeat_seconds=0.01)
        self.assertEqual(next(resumed), b": connected\n\n")
        self.assertEqual(next(resumed), b": heartbeat\n\n")
        resumed.close()

        gap = hub.subscribe(str(room["id"]), after_event_id=f"{room['id']}:999", heartbeat_seconds=0.01)
        self.assertEqual(next(gap), b": connected\n\n")
        snapshot_required = next(gap)
        self.assertIn(b"event: snapshot_required", snapshot_required)
        self.assertIn(b"room_event_replay_gap", snapshot_required)
        gap.close()

    def test_public_projection_receipt_deduplicates_fanout_and_rejects_rebinding(self) -> None:
        room = self.store.create(
            title="公开投影去重",
            routing_policy="natural",
            participants=[
                self._participant("companion-present-v1", "澄·今"),
                self._participant("companion-firstlight-v1", "澄·初"),
            ],
            created_at_ms=100,
        )
        room_id = str(room["id"])
        hub = AgentRoomEventHub(self.store)
        observed: list[dict[str, object]] = []
        hub.add_observer(lambda event: observed.append(dict(event)))
        values = {
            "room_id": room_id,
            "event_type": "user_message",
            "payload": {"messageId": "post:user:1", "text": "只显示一次"},
            "turn_id": "root:1",
            "created_at_ms": 110,
        }

        first = hub.publish_projection(projection_key="room-post:post:user:1", **values)
        replay = hub.publish_projection(projection_key="room-post:post:user:1", **values)

        self.assertTrue(hub.has_projection("room-post:post:user:1"))
        self.assertFalse(hub.has_projection("room-post:missing"))

        self.assertIsNotNone(first)
        self.assertEqual(replay, first)
        self.assertEqual(len(observed), 1)
        self.assertEqual(
            [event["eventType"] for event in self.store.list_events(room_id)],
            ["user_message"],
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                "DELETE FROM agent_room_events WHERE event_id = ?",
                (str(first["eventId"]),),  # type: ignore[index]
            )
            conn.commit()
        finally:
            conn.close()
        self.assertTrue(hub.has_projection("room-post:post:user:1"))
        self.assertIsNone(
            hub.publish_projection(
                projection_key="room-post:post:user:1",
                **values,
            )
        )
        self.assertEqual(len(observed), 1)
        with self.assertRaisesRegex(ValueError, "projection key was rebound"):
            hub.publish_projection(
                projection_key="room-post:post:user:1",
                **{
                    **values,
                    "payload": {
                        "messageId": "post:user:1",
                        "text": "偷偷改写",
                    },
                },
            )

    def test_child_terminal_adopts_legacy_event_and_deduplicates_after_retention(self) -> None:
        room = self.store.create(
            title="历史终态恢复", routing_policy="natural",
            participants=[
                self._participant("companion-present-v1", "主持"),
                self._participant("companion-firstlight-v1", "伙伴"),
            ],
        )
        target = room["participants"][1]
        room_id = str(room["id"])
        scope = {
            "room_id": room_id, "event_type": "participant_activity",
            "turn_id": "root:one", "participant_id": target["id"],
            "source_session_id": target["sessionId"],
        }
        hub = AgentRoomEventHub(self.store)
        legacy = hub.publish(**scope, payload={
            "sourceEventId": "runtime:event:one", "sourceEventType": "turn_completed",
            "data": {"activityKind": "child", "phase": "completed", "dispatchId": "child:one"},
        })
        observed: list[dict[str, object]] = []
        restarted = AgentRoomEventHub(self.store)
        restarted.add_observer(lambda event: observed.append(dict(event)))
        recovery = {
            **scope, "runtime_event_id": "runtime:event:one", "dispatch_id": "child:one",
            "payload": {
                "activityKind": "child", "phase": "completed", "dispatchId": "child:one",
                "sourceRuntimeEventId": "runtime:event:one", "summary": "恢复的摘要",
            },
        }
        self.assertEqual(restarted.publish_child_terminal(**recovery), legacy)
        self.assertEqual(len(self.store.list_events(room_id)), 1)
        self.assertEqual(observed, [])
        with self.assertRaisesRegex(ValueError, "projection key was rebound"):
            restarted.publish_child_terminal(**{
                **recovery, "payload": {**recovery["payload"], "phase": "aborted"},
            })
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM agent_room_events WHERE event_id = ?", (legacy["eventId"],))
        self.assertIsNone(AgentRoomEventHub(self.store).publish_child_terminal(**recovery))
        self.assertEqual(self.store.list_events(room_id), [])
        self.assertEqual(observed, [])

    def test_live_and_recovered_child_terminal_share_atomic_publication(self) -> None:
        room = self.store.create(
            title="并发终态投影", routing_policy="natural",
            participants=[
                self._participant("companion-present-v1", "主持"),
                self._participant("companion-firstlight-v1", "伙伴"),
            ],
        )
        target = room["participants"][1]
        scope = {
            "room_id": str(room["id"]), "event_type": "participant_activity",
            "turn_id": "root:concurrent", "participant_id": target["id"],
            "source_session_id": target["sessionId"],
            "runtime_event_id": "runtime:event:concurrent", "dispatch_id": "child:concurrent",
        }
        data = {"activityKind": "child", "phase": "completed", "dispatchId": "child:concurrent"}
        payloads = (
            {"sourceEventId": "runtime:event:concurrent", "sourceEventType": "turn_completed", "data": data},
            {**data, "sourceRuntimeEventId": "runtime:event:concurrent", "summary": "恢复的摘要"},
        )
        barrier = threading.Barrier(2)
        observed: list[dict[str, object]] = []
        results: list[object] = []
        errors: list[Exception] = []

        def publish(payload: dict[str, object]) -> None:
            hub = AgentRoomEventHub(self.store)
            hub.add_observer(lambda event: observed.append(dict(event)))
            try:
                barrier.wait(timeout=5)
                results.append(hub.publish_child_terminal(**scope, payload=payload))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=publish, args=(payload,)) for payload in payloads]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(len(observed), 1)
        self.assertEqual(len(self.store.list_events(str(room["id"]))), 1)

    def test_route_target_uses_exact_longest_mention_and_rejects_real_ambiguity(self) -> None:
        room = self.store.create(
            title="时间线讨论",
            routing_policy="manual_mentions",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("zhiyou-sol-v1", "澄·远"),
            ],
        )
        room_id = str(room["id"])

        current = self.store.route_target(room_id, "@澄 检查当前状态")
        future = self.store.route_target(room_id, "@澄·远 做长期规划")

        self.assertEqual(current["roleId"], "companion-present-v1")
        self.assertEqual(future["roleId"], "zhiyou-sol-v1")

        ambiguous = self.store.create(
            title="重名讨论",
            routing_policy="manual_mentions",
            participants=[
                self._participant("custom-a", "同名角色"),
                self._participant("custom-b", "同名角色"),
            ],
        )
        with self.assertRaisesRegex(ValueError, "exactly one"):
            self.store.route_target(str(ambiguous["id"]), "@同名角色 请回答")

    def test_snapshot_returns_atomic_room_metadata_and_retained_event_window(self) -> None:
        room = self.store.create(
            title="快照房间",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
            created_at_ms=1,
        )
        room_id = str(room["id"])
        for sequence in range(1, 103):
            self.store.append_event(
                room_id=room_id,
                event_type="participant_status",
                payload={"status": f"step-{sequence}"},
                created_at_ms=sequence + 1,
                retain_per_room=100,
            )

        snapshot = self.store.snapshot(room_id)

        self.assertEqual(snapshot["schemaVersion"], "rag-ime.agent-room-snapshot.v1")
        self.assertEqual(snapshot["room"]["lastEventSequence"], 102)
        self.assertEqual(len(snapshot["room"]["participants"]), 2)
        self.assertEqual(snapshot["firstSequence"], 3)
        self.assertEqual(snapshot["lastSequence"], 102)
        self.assertEqual(snapshot["resumeToken"], f"{room_id}:102")
        self.assertTrue(snapshot["truncated"])
        self.assertEqual(
            [event["sequence"] for event in snapshot["events"]],
            list(range(3, 103)),
        )

    def test_turn_event_lookup_does_not_parse_unrelated_room_events(self) -> None:
        room = self.store.create(
            title="定向恢复",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )
        room_id = str(room["id"])
        for index in range(20):
            self.store.append_event(
                room_id=room_id,
                event_type="participant_status",
                turn_id=f"unrelated:{index}",
                payload={"status": "working"},
            )
        expected = self.store.append_event(
            room_id=room_id,
            event_type="participant_status",
            turn_id="root:target",
            payload={"status": "completed"},
        )
        self.store.append_event(
            room_id=room_id,
            event_type="user_message",
            turn_id="root:target",
            payload={"text": "same turn, unrelated event type"},
        )

        with patch(
            "rag_ime.rooms.store._room_event_payload",
            wraps=agent_rooms_module._room_event_payload,
        ) as parse_event:
            events = self.store.list_events_for_turn(
                room_id,
                "root:target",
                event_types=("participant_status",),
                limit=2_000,
            )

        self.assertEqual(events, [expected])
        self.assertEqual(parse_event.call_count, 1)

    def test_typed_result_lookup_only_parses_matching_result_candidate(self) -> None:
        room = self.store.create(
            title="结果恢复",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )
        room_id = str(room["id"])
        for index in range(20):
            self.store.append_event(
                room_id=room_id,
                event_type="room_post",
                turn_id=f"unrelated:{index}",
                payload={"post": {"kind": "result"}},
            )
        self.store.append_event(
            room_id=room_id,
            event_type="room_post",
            turn_id="root:target",
            payload={"post": {"kind": "progress"}},
        )
        self.store.append_event(
            room_id=room_id,
            event_type="room_post",
            turn_id="root:target",
            payload={"post": {"kind": "result"}},
        )

        with patch(
            "rag_ime.rooms.store._room_event_payload",
            wraps=agent_rooms_module._room_event_payload,
        ) as parse_event:
            present = self.store.has_typed_result(
                room_id,
                "root:target",
            )

        self.assertTrue(present)
        self.assertEqual(parse_event.call_count, 1)

    def test_recovery_dispatch_lookup_does_not_parse_same_turn_unrelated_activity(
        self,
    ) -> None:
        room = self.store.create(
            title="恢复派发",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )
        room_id = str(room["id"])
        for index in range(100):
            self.store.append_event(
                room_id=room_id,
                event_type="participant_activity",
                turn_id="root:target",
                payload={
                    "activityKind": "child",
                    "phase": "completed",
                    "childDispatchId": f"unrelated:{index}",
                },
            )
        expected_post = self.store.append_event(
            room_id=room_id,
            event_type="room_post",
            turn_id="root:target",
            payload={
                "post": {
                    "kind": "work_result",
                    "dispatchId": "room-child:expected",
                }
            },
        )
        expected_message = self.store.append_event(
            room_id=room_id,
            event_type="participant_message",
            turn_id="root:target",
            payload={"data": {"dispatchId": "room-child:expected"}},
        )
        expected_activity = self.store.append_event(
            room_id=room_id,
            event_type="participant_activity",
            turn_id="root:target",
            payload={
                "activityKind": "child",
                "phase": "completed",
                "data": {"childDispatchId": "room-child:expected"},
            },
        )

        with patch(
            "rag_ime.rooms.store._room_event_payload",
            wraps=agent_rooms_module._room_event_payload,
        ) as parse_event:
            events = self.store.list_recovery_events_for_dispatches(
                room_id,
                "root:target",
                dispatch_ids=("room-child:expected",),
                limit=2_000,
            )

        self.assertEqual(events, [expected_post, expected_message, expected_activity])
        self.assertEqual(parse_event.call_count, 3)

    def test_history_page_walks_the_retained_prefix_without_unbounded_snapshot(self) -> None:
        room = self.store.create(
            title="分页房间",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )
        room_id = str(room["id"])
        for sequence in range(1, 261):
            self.store.append_event(
                room_id=room_id,
                event_type="participant_status",
                payload={"status": f"step-{sequence}"},
                created_at_ms=sequence,
                retain_per_room=250,
            )

        snapshot = self.store.snapshot(room_id)
        first_page = self.store.history_page(
            room_id,
            before_sequence=int(snapshot["firstSequence"]),
            limit=25,
        )
        second_page = self.store.history_page(
            room_id,
            before_sequence=int(first_page["firstSequence"]),
            limit=25,
        )

        self.assertEqual(len(snapshot["events"]), 200)
        self.assertEqual(snapshot["firstSequence"], 61)
        self.assertEqual(
            [event["sequence"] for event in first_page["items"]],
            list(range(36, 61)),
        )
        self.assertTrue(first_page["hasMore"])
        self.assertEqual(first_page["nextBeforeSequence"], 36)
        self.assertEqual(
            [event["sequence"] for event in second_page["items"]],
            list(range(11, 36)),
        )
        self.assertFalse(second_page["hasMore"])
        self.assertEqual(second_page["nextBeforeSequence"], 0)
        self.assertEqual(second_page["retainedFirstSequence"], 11)
        self.assertTrue(second_page["retainedPrefixTruncated"])

    def test_snapshot_expands_a_tail_that_starts_inside_one_room_turn(self) -> None:
        room = self.store.create(
            title="完整回合快照",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )
        room_id = str(room["id"])
        turn_id = "room-turn:long"
        self.store.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "请完成一次长回合"},
            turn_id=turn_id,
            created_at_ms=1,
        )
        for sequence in range(2, 252):
            self.store.append_event(
                room_id=room_id,
                event_type="participant_delta",
                payload={"data": {"delta": f"chunk-{sequence}"}},
                turn_id=turn_id,
                created_at_ms=sequence,
            )
        self.store.append_event(
            room_id=room_id,
            event_type="participant_message",
            payload={"data": {"message": {"id": "message:final"}}},
            turn_id=turn_id,
            created_at_ms=252,
        )
        self.store.append_event(
            room_id=room_id,
            event_type="turn_completed",
            payload={"status": "completed"},
            turn_id=turn_id,
            created_at_ms=253,
        )

        snapshot = self.store.snapshot(room_id)

        self.assertEqual(snapshot["firstSequence"], 1)
        self.assertEqual(snapshot["lastSequence"], 253)
        self.assertEqual(len(snapshot["events"]), 253)
        self.assertEqual(snapshot["events"][0]["eventType"], "user_message")
        self.assertEqual(snapshot["events"][-1]["eventType"], "turn_completed")
        self.assertFalse(snapshot["truncated"])

    def test_participant_public_cursor_prevents_reinjecting_old_room_messages(self) -> None:
        room = self.store.create(
            title="游标房间",
            routing_policy="moderator",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )
        participant_id = str(room["participants"][1]["id"])
        room_id = str(room["id"])
        topic_id = str(room["activeTopicId"])
        for sequence in range(3):
            self.store.append_event(
                room_id=room_id,
                event_type="user_message",
                payload={"text": f"消息 {sequence}"},
                turn_id=f"turn:{sequence}",
                topic_id=topic_id,
            )

        unread = self.store.unread_public_messages(
            room_id,
            participant_id,
            topic_id=topic_id,
            limit=2,
        )
        self.assertEqual(unread["omittedCount"], 1)
        self.assertEqual(
            [item["payload"]["text"] for item in unread["items"]],
            ["消息 1", "消息 2"],
        )
        self.store.advance_delivery_cursor(
            room_id,
            participant_id,
            topic_id=topic_id,
            through_sequence=int(unread["throughSequence"]),
        )
        self.store.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "新消息"},
            turn_id="turn:new",
            topic_id=topic_id,
        )

        next_unread = self.store.unread_public_messages(
            room_id,
            participant_id,
            topic_id=topic_id,
        )
        self.assertEqual(
            [item["payload"]["text"] for item in next_unread["items"]],
            ["新消息"],
        )

    def test_joining_participant_starts_at_current_room_watermark(self) -> None:
        room = self.store.create(
            title="长期协作",
            routing_policy="manual_mentions",
            participants=[
                self._participant("companion-present-v1", "澄·今"),
                self._participant("companion-firstlight-v1", "澄·初"),
            ],
        )
        room_id = str(room["id"])
        topic_id = str(room["activeTopicId"])
        for sequence in range(30):
            self.store.append_event(
                room_id=room_id,
                event_type="user_message",
                payload={"text": f"加入前的消息 {sequence}"},
                turn_id=f"turn:old:{sequence}",
                topic_id=topic_id,
            )
        session = self.sessions.create(
            title="澄·远 room session",
            role_id="companion-future-v1",
            role_version="1",
        )

        participant = self.store.add_participant(
            room_id,
            session_id=str(session["id"]),
            role_id="companion-future-v1",
            role_version="1",
            display_name="澄·远",
        )

        unread = self.store.unread_public_messages(
            room_id,
            str(participant["id"]),
            topic_id=topic_id,
        )
        self.assertEqual(unread["items"], [])
        self.assertEqual(unread["omittedCount"], 0)
        self.store.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"text": "加入后的第一条消息"},
            turn_id="turn:new",
            topic_id=topic_id,
        )
        next_unread = self.store.unread_public_messages(
            room_id,
            str(participant["id"]),
            topic_id=topic_id,
        )
        self.assertEqual(
            [item["payload"]["text"] for item in next_unread["items"]],
            ["加入后的第一条消息"],
        )

    def test_empty_room_snapshot_has_zero_cursor(self) -> None:
        room = self.store.create(
            title="空房间",
            routing_policy="manual_mentions",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )

        snapshot = self.store.snapshot(str(room["id"]))

        self.assertEqual(snapshot["events"], [])
        self.assertEqual(snapshot["firstSequence"], 0)
        self.assertEqual(snapshot["lastSequence"], 0)
        self.assertEqual(snapshot["resumeToken"], "")
        self.assertFalse(snapshot["truncated"])

    def test_conversation_snapshot_defers_activity_payloads_without_losing_cursor(self) -> None:
        room = self.store.create(
            title="消息优先房间",
            routing_policy="manual_mentions",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "Hermes"),
            ],
        )
        room_id = str(room["id"])
        first = self.store.append_event(
            room_id=room_id,
            event_type="user_message",
            payload={"messageId": "message:user", "text": "先显示消息"},
            turn_id="turn:conversation",
        )
        for index in range(50):
            self.store.append_event(
                room_id=room_id,
                event_type="participant_activity",
                payload={
                    "activityKind": "tool",
                    "summary": f"工具回执 {index}",
                    "result": {"raw": "x" * 4_096},
                },
                turn_id="turn:conversation",
            )
        final = self.store.append_event(
            room_id=room_id,
            event_type="turn_completed",
            payload={"status": "completed"},
            turn_id="turn:conversation",
        )

        conversation = self.store.conversation_snapshot(room_id)
        full = self.store.snapshot(room_id)

        self.assertEqual(
            [event["eventType"] for event in conversation["events"]],
            ["user_message", "turn_completed"],
        )
        self.assertEqual(conversation["firstEventSequence"], first["sequence"])
        self.assertEqual(conversation["cursorSequence"], final["sequence"])
        self.assertEqual(conversation["resumeToken"], final["resumeToken"])
        self.assertEqual(conversation["deferredEventCount"], 50)
        self.assertFalse(conversation["truncated"])
        self.assertLess(
            len(json.dumps(conversation, ensure_ascii=False)),
            len(json.dumps(full, ensure_ascii=False)) // 10,
        )

    def test_room_limits_and_participant_session_ownership_fail_closed(self) -> None:
        participants = [
            self._participant("companion-present-v1", "澄"),
            self._participant("companion-firstlight-v1", "Hermes"),
        ]
        self.store.create(
            title="第一个房间",
            routing_policy="manual_mentions",
            participants=participants,
        )
        with self.assertRaisesRegex(Exception, "UNIQUE constraint failed"):
            self.store.create(
                title="重复会话",
                routing_policy="manual_mentions",
                participants=participants,
            )
        with self.assertRaisesRegex(ValueError, "between 2 and 8"):
            self.store.create(
                title="人数过少",
                routing_policy="manual_mentions",
                participants=[self._participant("companion-future-v1", "VCP")],
            )

    def test_room_accepts_eight_participants_and_rejects_nine(self) -> None:
        eight = [
            self._participant(f"room-role-{index}", f"伙伴 {index}")
            for index in range(8)
        ]

        room = self.store.create(
            title="八位伙伴",
            routing_policy="manual_mentions",
            participants=eight,
        )

        self.assertEqual(len(room["participants"]), 8)
        with self.assertRaisesRegex(ValueError, "between 2 and 8"):
            self.store.create(
                title="九位伙伴",
                routing_policy="manual_mentions",
                participants=[
                    self._participant(f"overflow-role-{index}", f"额外伙伴 {index}")
                    for index in range(9)
                ],
            )

    def test_sequential_and_natural_routing_are_structured_and_deterministic(self) -> None:
        room = self.store.create(
            title="轮流讨论",
            room_kind="roleplay",
            routing_policy="sequential",
            participants=[
                self._participant("companion-present-v1", "此刻"),
                self._participant("companion-firstlight-v1", "初识"),
                self._participant("companion-future-v1", "未来"),
            ],
        )
        room_id = str(room["id"])
        first = self.store.plan_route(room_id, "先说说看")
        self.assertEqual(first["reason"], "sequential")
        self.assertEqual(first["selectedParticipantIds"], [room["participants"][0]["id"]])
        self.store.commit_route(room_id, first)
        second = self.store.plan_route(room_id, "继续")
        self.assertEqual(second["selectedParticipantIds"], [room["participants"][1]["id"]])

        updated = self.store.update_config(
            room_id,
            {"routingPolicy": "natural", "routingConfig": {"naturalJitter": 0}},
        )
        profiles = {
            str(updated["participants"][0]["id"]): {"traits": ["执行", "实现"]},
            str(updated["participants"][1]["id"]): {"traits": ["调研", "证据"]},
            str(updated["participants"][2]["id"]): {"traits": ["规划", "协调"]},
        }
        decision = self.store.plan_route(room_id, "请调研证据", profiles=profiles)
        self.assertEqual(decision["reason"], "descriptor_match")
        self.assertEqual(decision["targetDisplayName"], "初识")
        self.assertEqual(decision["schemaVersion"], "rag-ime.room-route-decision.v1")
        self.assertTrue(decision["candidates"][0]["signals"])
        with self.assertRaisesRegex(ValueError, "cannot be changed"):
            self.store.update_config(room_id, {"roomKind": "collaboration"})

    def test_topics_and_artifacts_are_incremental_and_workspace_scoped(self) -> None:
        room = self.store.create(
            title="资料讨论",
            routing_policy="moderator",
            workspace_roots=[str(self.root)],
            participants=[
                self._participant("companion-present-v1", "此刻"),
                self._participant("companion-future-v1", "未来"),
            ],
        )
        room_id = str(room["id"])
        original_topic = str(room["activeTopicId"])
        topic = self.store.create_topic(room_id, title="发布风险", summary="核对上线边界")
        activated = self.store.update_topic(room_id, str(topic["id"]), {"activate": True})
        self.assertEqual(activated["title"], "发布风险")
        self.assertEqual(self.store.get(room_id)["activeTopicId"], topic["id"])
        archived = self.store.update_topic(room_id, original_topic, {"archived": True})
        self.assertEqual(archived["status"], "archived")

        artifact_path = self.root / "report.md"
        artifact_path.write_text("# report\n", encoding="utf-8")
        artifact = self.store.add_artifact(
            room_id,
            path=str(artifact_path),
            topic_id=str(topic["id"]),
            display_name="风险报告",
            media_type="text/markdown",
        )
        self.assertEqual(artifact["path"], str(artifact_path.resolve()))
        self.assertEqual(
            self.store.list_artifacts(room_id, topic_id=str(topic["id"]))[0]["displayName"],
            "风险报告",
        )
        outside = Path(tempfile.gettempdir()) / "rag-ime-outside-room-artifact.txt"
        outside.write_text("outside", encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        with self.assertRaisesRegex(ValueError, "authorized workspace"):
            self.store.add_artifact(room_id, path=str(outside))

    def test_participant_collaboration_role_is_editable_without_rewriting_identity(
        self,
    ) -> None:
        room = self.store.create(
            title="交付岗位",
            routing_policy="natural",
            participants=[
                self._participant("companion-present-v1", "澄"),
                self._participant("companion-firstlight-v1", "初识"),
            ],
            created_at_ms=10,
        )
        participant = room["participants"][1]

        updated = self.store.update_participant_role(
            str(room["id"]),
            str(participant["id"]),
            "reviewer",
            updated_at_ms=20,
        )

        self.assertEqual(updated["id"], participant["id"])
        self.assertEqual(updated["sessionId"], participant["sessionId"])
        self.assertEqual(updated["collaborationRole"], "reviewer")
        self.assertEqual(self.store.get(str(room["id"]))["updatedAtMs"], 20)
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.store.update_participant_role(
                str(room["id"]),
                str(participant["id"]),
                "observer",
            )
        # `specialist` stays readable for historical members but is no longer
        # assignable: it carries no domain contract, so offering it would
        # promise expertise the runtime cannot supply.
        with self.assertRaisesRegex(ValueError, "history only"):
            self.store.update_participant_role(
                str(room["id"]),
                str(participant["id"]),
                "specialist",
            )
        self.assertEqual(
            self.store.participant(str(participant["id"]))["collaborationRole"],
            "reviewer",
        )

    def _participant(self, role_id: str, display_name: str) -> dict[str, str]:
        session = self.sessions.create(
            title=f"{display_name} room session",
            role_id=role_id,
            role_version="1",
        )
        return {
            "sessionId": str(session["id"]),
            "roleId": role_id,
            "roleVersion": "1",
            "displayName": display_name,
        }


class AgentRoomServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-room-service-")
        self.root = Path(self.tmp.name)
        self.service = AgentService(
            db_path=self.root / "rag-ime.sqlite",
            runtime_config=PiRuntimeConfig(
                enabled=False,
                executable=None,
                agent_dir=self.root / "agent-config",
                session_dir=self.root / "sessions",
                logs_dir=self.root / "logs",
            ),
        )

    def tearDown(self) -> None:
        self.service.close()
        self.tmp.cleanup()

    def test_room_directory_projection_skips_full_room_payload(self) -> None:
        room = self.service.create_room(
            {
                "title": "快速 Room 目录",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]

        item = self.service.list_rooms({"projectionOnly": True})["items"][0]

        self.assertEqual(item["id"], room["id"])
        self.assertNotIn("scenarioPrompt", item)
        self.assertNotIn("topics", item)
        self.assertNotIn("artifacts", item)

    def test_room_defaults_to_system_wide_full_trust_without_project_workspace(self) -> None:
        room = self.service.create_room(
            {
                "title": "没有项目的协作",
                "workspaceRoots": [],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        self.assertEqual(room["executionMode"], "full_trust")
        self.assertEqual(room["workspaceRoots"], ["/"])
        sessions = self.service.list_sessions()["items"]
        self.assertEqual(len(sessions), 2)
        for session in sessions:
            self.assertEqual(session["executionMode"], "full_trust")
            self.assertEqual(
                session["toolProfileVersion"],
                "control-center-auto-approve-v1",
            )
            self.assertEqual(session["workspaceRoots"], ["/"])

    def test_agent_lab_read_only_room_is_app_owned_and_hidden_from_ordinary_room_list(self) -> None:
        room = self.service.create_room(
            {
                "title": "Agent Lab · 评测向导",
                "roomKind": "collaboration",
                "workspaceRoots": [],
                "executionMode": "read_only",
                "ownerAppId": "extension:agent-lab",
                "surfaceKey": "wizard",
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1", "collaborationRole": "coordinator"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1", "collaborationRole": "reviewer"},
                ],
            }
        )["room"]

        self.assertEqual(room["ownerAppId"], "extension:agent-lab")
        self.assertEqual(room["surfaceKey"], "wizard")
        self.assertEqual(room["workspaceRoots"], [])
        participant_session = self.service.sessions.get(str(room["participants"][0]["sessionId"]))
        self.assertEqual(participant_session["surfaceKind"], "extension_app")
        self.assertEqual(participant_session["ownerAppId"], "extension:agent-lab")
        self.assertEqual(participant_session["surfaceKey"], "wizard")

        self.assertEqual(self.service.list_rooms({"ownerAppId": ""})["items"], [])
        owned = self.service.list_rooms({"ownerAppId": "extension:agent-lab"})["items"]
        self.assertEqual([item["id"] for item in owned], [room["id"]])
        self.assertEqual(owned[0]["surfaceKey"], "wizard")

    def test_full_trust_room_keeps_unavailable_workspace_as_optional_context(self) -> None:
        missing = self.root / "removed-workspace"
        room = self.service.create_room(
            {
                "title": "外部上下文不限制全权限 Room",
                "workspaceRoots": [str(missing)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        expected_roots = [str(missing.resolve()), "/"]
        self.assertEqual(room["workspaceRoots"], expected_roots)
        self.assertEqual(room["executionMode"], "full_trust")
        for participant in room["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["workspaceRoots"], expected_roots)
            self.assertEqual(session["executionMode"], "full_trust")

    def test_idle_room_can_replace_an_unavailable_optional_context_for_every_participant(self) -> None:
        room = self.service.create_room(
            {
                "title": "可恢复工作目录 Room",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        missing = self.root / "removed-workspace"
        missing_update = self.service.update_room(
            str(room["id"]),
            {
                "workspaceRoots": [str(missing)],
                "workspaceScopeConfirmation": "APPROVE_WORKSPACE_SCOPE",
            },
        )["room"]
        self.assertEqual(
            missing_update["workspaceRoots"],
            [str(missing.resolve()), "/"],
        )

        replacement = self.root / "replacement"
        replacement.mkdir()
        resolved_replacement = str(replacement.resolve())
        updated = self.service.update_room(
            str(room["id"]),
            {
                "workspaceRoots": [str(replacement)],
                "workspaceScopeConfirmation": "APPROVE_WORKSPACE_SCOPE",
            },
        )["room"]
        self.assertEqual(updated["workspaceRoots"], [resolved_replacement, "/"])
        participant_sessions = [
            self.service.sessions.get(str(participant["sessionId"]))
            for participant in updated["participants"]
            if participant["status"] == "active"
        ]
        self.assertTrue(participant_sessions)
        self.assertTrue(
            all(
                session["workspaceRoots"] == [resolved_replacement, "/"]
                for session in participant_sessions
            )
        )
        self.assertTrue(
            all(
                session["workspaceScopeGranted"] is True
                for session in participant_sessions
            )
        )

    def test_existing_room_can_add_future_without_replaying_old_history(self) -> None:
        room = self.service.create_room(
            {
                "title": "长期项目 Room",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        for sequence in range(40):
            self.service.rooms.append_event(
                room_id=str(room["id"]),
                event_type="user_message",
                payload={"text": f"不可重放的旧消息 {sequence} " + "旧" * 500},
                turn_id=f"turn:old:{sequence}",
                topic_id=str(room["activeTopicId"]),
            )

        added = self.service.add_room_participant(
            str(room["id"]),
            {"roleId": "companion-future-v1", "roleVersion": "1"},
        )
        future = added["participant"]
        self.assertEqual(future["displayName"], "Agent 3")
        self.assertEqual(len([p for p in added["room"]["participants"] if p["status"] == "active"]), 3)

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:future:first"},
        ) as prompt:
            self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "@澄·远 请从现在开始接手规划",
                    "participantIds": [str(future["id"])],
                },
            )

        prompt_payload = prompt.call_args.args[1]
        self.assertEqual(
            prompt_payload["message"],
            "@澄·远 请从现在开始接手规划",
        )
        rendered = prompt_payload["_transientContext"]
        self.assertNotIn("不可重放的旧消息", rendered)
        self.assertLessEqual(len(rendered), 24_000)

    def test_facilitator_can_resize_active_room_and_release_removed_work(self) -> None:
        roles = [
            self.service.personas.create(
                {
                    "displayName": f"测试角色 {index}",
                    "tagline": "协作测试",
                    "summary": "用于 Room 动态成员测试",
                    "traits": ["执行"],
                    "timelineModel": "terra",
                    "selectableModes": ["coordinator"],
                    "suitableTasks": ["Room 协作"],
                    "unsuitableTasks": ["无"],
                }
            )
            for index in range(7)
        ]
        room = self.service.create_room(
            {
                "title": "动态行星 Room",
                "routingPolicy": "natural",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        facilitator = room["participants"][0]
        self.service._begin_room_turn(
            str(facilitator["sessionId"]),
            "room-turn:resize",
            str(room["activeTopicId"]),
            dispatch_id="room-dispatch:resize",
        )

        added = []
        for index, role in enumerate(roles[:6]):
            result = self.service.execute_room_partner_tool(
                str(facilitator["sessionId"]),
                {
                    "op": "add_participant",
                    "roleId": role.role_id,
                    "roleVersion": role.version,
                },
                tool_call_id=f"tool:resize:add:{index}",
            )
            added.append(result["participant"])

        self.assertEqual(
            len([value for value in result["room"]["participants"] if value["status"] == "active"]),
            8,
        )
        self.assertEqual(len({value["sessionId"] for value in added}), 6)
        listed = self.service.execute_room_partner_tool(
            str(facilitator["sessionId"]),
            {"op": "list"},
            tool_call_id="tool:resize:list",
        )
        self.assertEqual(len(listed["partners"]), 7)
        self.assertTrue(
            {value["participantId"] for value in listed["partners"]}
            >= {value["id"] for value in added}
        )

        with self.assertRaisesRegex(ValueError, "at most eight"):
            self.service.execute_room_partner_tool(
                str(facilitator["sessionId"]),
                {
                    "op": "add_participant",
                    "roleId": roles[6].role_id,
                    "roleVersion": roles[6].version,
                },
                tool_call_id="tool:resize:add:overflow",
            )

        removed_target = added[0]
        work = self.service.create_room_work_item(
            str(room["id"]),
            {
                "objective": "交回被移除成员的任务",
                "expectedOutput": "待重新分配的工作卡片",
                "acceptanceCriteria": ["保留原始证据"],
                "currentOwnerParticipantId": removed_target["id"],
                "accountableParticipantId": removed_target["id"],
                "createdByParticipantId": removed_target["id"],
                "clientMessageId": "resize-release-work",
            },
        )["workItem"]
        removed = self.service.execute_room_partner_tool(
            str(facilitator["sessionId"]),
            {
                "op": "remove_participant",
                "participantId": removed_target["id"],
                "reason": "Room 规模调整",
            },
            tool_call_id="tool:resize:remove",
        )
        self.assertEqual(removed["participant"]["status"], "removed")
        self.assertEqual(
            len([value for value in removed["room"]["participants"] if value["status"] == "active"]),
            7,
        )
        self.assertEqual(removed["releasedWorkItems"][0]["id"], work["id"])
        self.assertEqual(removed["releasedWorkItems"][0]["state"], "blocked")
        self.assertTrue(removed["releasedWorkItems"][0]["blocker"]["needsReassignment"])
        self.assertEqual(
            self.service.sessions.get(str(removed_target["sessionId"]))["status"],
            "archived",
        )
        self.assertEqual(
            [event["eventType"] for event in self.service.room_work.list_events(str(work["id"]))],
            ["assigned", "reassigned"],
        )

    def test_existing_member_room_context_is_bounded_by_count_and_characters(self) -> None:
        room = self.service.create_room(
            {
                "title": "上下文预算 Room",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        target = room["participants"][0]
        for sequence in range(30):
            self.service.rooms.append_event(
                room_id=str(room["id"]),
                event_type="user_message",
                payload={"text": f"历史编号-{sequence:02d}-" + "长" * 700},
                turn_id=f"turn:history:{sequence}",
                topic_id=str(room["activeTopicId"]),
            )

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:bounded"},
        ) as prompt:
            self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "@澄·今 继续当前任务",
                    "participantIds": [str(target["id"])],
                },
            )

        prompt_payload = prompt.call_args.args[1]
        self.assertEqual(prompt_payload["message"], "@澄·今 继续当前任务")
        rendered = prompt_payload["_transientContext"]
        self.assertLessEqual(len(rendered), 24_000)
        self.assertIn("历史编号-29", rendered)
        self.assertNotIn("历史编号-00", rendered)
        self.assertIn("另有 22 条较早消息未注入", rendered)
        self.assertIn("按需读取公开摘要、产物或状态", rendered)

    def test_room_text_file_routes_without_image_support_and_is_readable_only_by_members(self) -> None:
        room = self.service.create_room({
            "title": "长文本 Room", "routingPolicy": "manual_mentions", "workspaceRoots": [str(self.root)],
            "participants": [{"roleId": "companion-present-v1", "roleVersion": "1"}, {"roleId": "companion-firstlight-v1", "roleVersion": "1"}],
        })["room"]
        target = room["participants"][0]
        body = "需要按段阅读的中文资料\n" * 600
        media = self.service.import_media(room_id=str(room["id"]), data=body.encode(), mime_type="text/plain", file_name="资料.txt")["media"]
        with (
            patch.object(self.service.runtime, "model_catalog", return_value={"selected": {"supportsImages": False}}),
            patch.object(self.service, "prompt", return_value={"turnId": "turn:room-text"}) as prompt,
        ):
            self.service.post_room_message(str(room["id"]), {"message": "核对资料", "participantIds": [str(target["id"])], "attachmentIds": [str(media["mediaId"])], "clientMessageId": "room-text-file"})
        self.assertEqual(prompt.call_args.args[1]["attachments"], [media["mediaId"]])
        self.assertEqual(self.service.read_media_resource(str(media["mediaId"]), session_id=str(target["sessionId"]))[1].decode(), body)
        preview = self.service.file_preview(str(media["mediaId"]), session_id=str(target["sessionId"]))
        self.assertEqual(preview["content"], body)
        self.assertIn("sessionId=", preview["descriptor"]["contentUrl"])
        self.assertEqual(self.service.media_content(str(media["mediaId"]), session_id=str(target["sessionId"]))[1].decode(), body)
        other = self.service.create_session({"title": "Room 外"})["session"]
        with self.assertRaises(KeyError):
            self.service.read_media_resource(str(media["mediaId"]), session_id=str(other["id"]))

    def test_room_image_is_delivered_to_the_authorized_participant_prompt(self) -> None:
        room = self.service.create_room(
            {
                "title": "图片协作 Room",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        target = room["participants"][0]
        media = self.service.import_media(
            room_id=str(room["id"]),
            data=PNG_1X1,
            mime_type="image/png",
            file_name="room.png",
        )["media"]
        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={"selected": {"supportsImages": True}},
            ),
            patch.object(
                self.service,
                "prompt",
                return_value={"turnId": "turn:room-image"},
            ) as prompt,
        ):
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "请看图片",
                    "participantIds": [str(target["id"])],
                    "attachmentIds": [str(media["mediaId"])],
                    "clientMessageId": "room-image-1",
                },
            )
        prompt_payload = prompt.call_args.args[1]
        self.assertEqual(prompt_payload["attachments"], [media["mediaId"]])
        self.assertEqual(prompt_payload["_mediaOwnerRoomId"], room["id"])
        user_event = next(
            event for event in accepted["timelineEvents"]
            if event["eventType"] == "user_message"
        )
        self.assertEqual(
            user_event["payload"]["attachmentReceipts"][0]["mediaId"],
            media["mediaId"],
        )

    def test_room_image_rejects_an_unsupported_target_model_before_dispatch(self) -> None:
        room = self.service.create_room(
            {
                "title": "文本模型 Room",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        target = room["participants"][0]
        media = self.service.import_media(
            room_id=str(room["id"]),
            data=PNG_1X1,
            mime_type="image/png",
        )["media"]
        with (
            patch.object(
                self.service.runtime,
                "model_catalog",
                return_value={"selected": {"supportsImages": False}},
            ),
            patch.object(self.service, "prompt") as prompt,
        ):
            with self.assertRaisesRegex(ValueError, "当前模型不支持图片"):
                self.service.post_room_message(
                    str(room["id"]),
                    {
                        "message": "请看图片",
                        "participantIds": [str(target["id"])],
                        "attachmentIds": [str(media["mediaId"])],
                    },
                )
        prompt.assert_not_called()

    def test_room_rejects_oversized_message_instead_of_silently_truncating_it(self) -> None:
        room = self.service.create_room(
            {
                "title": "消息边界 Room",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        target = room["participants"][0]

        with self.assertRaisesRegex(ValueError, "must not exceed 8000"):
            self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "@澄·今 " + "超" * 8_000,
                    "participantIds": [str(target["id"])],
                },
            )

        events = self.service.rooms.list_events(str(room["id"]))
        self.assertFalse(any(value["eventType"] == "user_message" for value in events))

    def test_active_room_repairs_a_missing_participant_session(self) -> None:
        room = self.service.create_room(
            {
                "title": "旧 Room Session 修复",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        old_session_id = str(participant["sessionId"])
        # Simulate a legacy/manual database mutation that bypassed FK checks.
        with sqlite3.connect(self.service.sessions.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("DELETE FROM agent_sessions WHERE id = ?", (old_session_id,))

        snapshot = self.service.room_snapshot(str(room["id"]))
        repaired = next(
            value
            for value in snapshot["room"]["participants"]
            if value["id"] == participant["id"]
        )
        self.assertNotEqual(repaired["sessionId"], old_session_id)
        repaired_session = self.service.sessions.get(str(repaired["sessionId"]))
        self.assertEqual(repaired_session["roleId"], participant["roleId"])
        self.assertEqual(
            repaired_session["toolProfileVersion"],
            "control-center-auto-approve-v1",
        )
        self.assertEqual(repaired_session["status"], "idle")

    def test_active_room_restores_legacy_archived_participant_sessions(self) -> None:
        room = self.service.create_room(
            {
                "title": "旧 Room 归档修复",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        session_id = str(room["participants"][0]["sessionId"])
        self.service.sessions.archive(session_id, archived=True)

        self.service.room_snapshot(str(room["id"]))

        self.assertEqual(self.service.sessions.get(session_id)["status"], "idle")

    def test_active_room_restores_the_complete_participant_tool_surface(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room 权限归属",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        session_id = str(room["participants"][0]["sessionId"])
        self.assertTrue(
            self.service.sessions.get(session_id)["projectContextEnabled"]
        )
        self.service.sessions.set_runtime_policy(
            session_id,
            mode="assistant",
            tool_profile_version="subagent-readonly-v1",
            project_context_enabled=False,
            allowed_tools=["overview"],
            workspace_roots=[],
        )

        self.service.room_snapshot(str(room["id"]))

        session = self.service.sessions.get(session_id)
        self.assertEqual(session["mode"], "coordinator")
        self.assertEqual(
            session["toolProfileVersion"],
            "control-center-auto-approve-v1",
        )
        self.assertEqual(session["toolAllowlistMode"], "profile")
        self.assertEqual(session["allowedTools"], [])
        self.assertEqual(
            session["workspaceRoots"],
            [str(self.root.resolve()), "/"],
        )
        self.assertTrue(session["workspaceScopeGranted"])
        self.assertTrue(session["projectContextEnabled"])
        self.assertTrue(session["piSkillsEnabled"])
        self.assertTrue(session["codexSkillsEnabled"])

    def test_room_snapshot_does_not_revoke_an_active_task_workspace(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room 进度轮询不改变任务授权",
                "routingPolicy": "parallel",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        session_id = str(room["participants"][1]["sessionId"])
        task_workspace = self.root / "managed-room-task-worktree"
        task_workspace.mkdir()
        session = self.service.sessions.get(session_id)
        self.service.sessions.set_runtime_policy(
            session_id,
            mode=str(session["mode"]),
            tool_profile_version=str(session["toolProfileVersion"]),
            execution_mode=str(session["executionMode"]),
            grant_workspace_scope=True,
            allowed_tools=None,
            workspace_roots=[str(task_workspace)],
        )
        self.service.room_turns.pending_turn_by_session[session_id] = (
            "room-turn:active-task"
        )

        snapshot = self.service.room_snapshot(str(room["id"]))

        self.assertTrue(snapshot["ok"])
        self.assertEqual(
            self.service.sessions.get(session_id)["workspaceRoots"],
            [str(task_workspace.resolve()), "/"],
        )

    def test_archived_room_releases_policy_and_restore_reclaims_it(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room 权限生命周期",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        session_id = str(room["participants"][0]["sessionId"])

        with self.assertRaisesRegex(
            ValueError,
            "runtime permissions are managed by the Room",
        ):
            self.service.update_session(
                session_id,
                {
                    "mode": "assistant",
                    "toolProfileVersion": "subagent-readonly-v1",
                },
            )

        self.service.update_room(str(room["id"]), {"archived": True})
        changed = self.service.update_session(
            session_id,
            {
                "mode": "assistant",
                "toolProfileVersion": "subagent-readonly-v1",
                "allowedTools": ["overview"],
            },
        )["session"]
        self.assertEqual(changed["mode"], "assistant")
        self.assertEqual(changed["toolProfileVersion"], "subagent-readonly-v1")

        self.service.update_room(str(room["id"]), {"archived": False})
        restored = self.service.sessions.get(session_id)
        self.assertEqual(restored["mode"], "coordinator")
        self.assertEqual(
            restored["toolProfileVersion"],
            "control-center-auto-approve-v1",
        )
        self.assertEqual(restored["toolAllowlistMode"], "profile")
        self.assertEqual(restored["workspaceRoots"], [str(self.root.resolve()), "/"])

    def test_removed_member_is_archived_and_not_restored_by_room_snapshot(self) -> None:
        room = self.service.create_room(
            {
                "title": "成员生命周期",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        future = next(value for value in room["participants"] if value["roleId"] == "companion-future-v1")

        removed = self.service.remove_room_participant(
            str(room["id"]),
            {"participantId": future["id"]},
        )
        self.assertEqual(removed["participant"]["status"], "removed")
        self.assertEqual(self.service.sessions.get(str(future["sessionId"]))["status"], "archived")
        snapshot = self.service.room_snapshot(str(room["id"]))
        self.assertTrue(snapshot["ok"])
        self.assertEqual(self.service.sessions.get(str(future["sessionId"]))["status"], "archived")
        self.assertEqual(
            [value["roleId"] for value in snapshot["room"]["participants"] if value["status"] == "active"],
            ["companion-present-v1", "companion-firstlight-v1"],
        )

    def test_removing_member_clears_inactive_routing_pointers(self) -> None:
        room = self.service.create_room(
            {
                "title": "路由指针生命周期",
                "routingPolicy": "moderator",
                "moderatorRoleId": "companion-future-v1",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        future = next(value for value in room["participants"] if value["roleId"] == "companion-future-v1")
        self.service.update_room(
            str(room["id"]),
            {
                "routingPolicy": "natural",
                "routingConfig": {
                    "naturalJitter": 0.04,
                    "fallbackParticipantId": future["id"],
                },
            },
        )

        removed = self.service.remove_room_participant(
            str(room["id"]),
            {"participantId": future["id"]},
        )

        self.assertEqual(removed["room"]["moderatorParticipantId"], "")
        self.assertEqual(removed["room"]["routingConfig"]["fallbackParticipantId"], "")
        with self.assertRaisesRegex(ValueError, "requires an active moderator"):
            self.service.update_room(
                str(room["id"]),
                {"routingPolicy": "moderator"},
            )

    def test_permanent_room_delete_requires_archive_and_exact_title(self) -> None:
        room = self.service.create_room(
            {
                "title": "可永久删除 Room",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        room_id = str(room["id"])
        session_ids = [str(value["sessionId"]) for value in room["participants"]]
        with self.assertRaisesRegex(ValueError, "archive the Room"):
            self.service.delete_room(room_id, {"confirmTitle": room["title"]})
        self.service.update_room(room_id, {"archived": True})
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.service.delete_room(room_id, {"confirmTitle": "写错了"})

        deleted = self.service.delete_room(room_id, {"confirmTitle": room["title"]})

        self.assertEqual(deleted["deletedSessionIds"], session_ids)
        self.assertEqual(self.service.list_rooms({"includeArchived": True})["items"], [])
        self.assertEqual(self.service.list_sessions({"includeArchived": True, "includeInternal": True})["items"], [])

    def test_permanent_room_delete_tolerates_a_legacy_missing_session(self) -> None:
        room = self.service.create_room(
            {
                "title": "缺失 Session 的旧 Room",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        room_id = str(room["id"])
        missing_session_id = str(room["participants"][0]["sessionId"])
        self.service.update_room(room_id, {"archived": True})
        with sqlite3.connect(self.service.sessions.db_path) as conn:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute(
                "DELETE FROM agent_sessions WHERE id = ?",
                (missing_session_id,),
            )

        deleted = self.service.delete_room(
            room_id,
            {"confirmTitle": room["title"]},
        )

        missing_cleanup = next(
            item
            for item in deleted["sessionCleanup"]
            if item["sessionId"] == missing_session_id
        )
        self.assertTrue(missing_cleanup["alreadyMissing"])
        self.assertEqual(
            self.service.list_rooms({"includeArchived": True})["items"],
            [],
        )

    def test_unaddressed_room_ignores_optional_persona_profile(self) -> None:
        role = self.service.personas.resolve("companion-firstlight-v1", "1")
        self.service.role_books.ensure_seeded(
            role.role_id,
            role.version,
            role.display_name,
            role.summary,
            role.version,
        )
        draft = self.service.role_books.propose_revision(
            "companion-firstlight-v1",
            "1",
            {
                "capabilities": [
                    {
                        "itemId": "capability:timeseries",
                        "text": "时序数据库性能",
                        "provenance": {
                            "sourceType": "work-receipt",
                            "sourceId": "work:timeseries",
                            "observedAtMs": 100,
                        },
                        "evidenceIds": ["receipt:timeseries"],
                    }
                ]
            },
        )
        self.service.role_books.activate_revision(draft["revisionId"])
        room = self.service.create_room(
            {
                "title": "自然路由",
                "routingPolicy": "natural",
                "routingConfig": {"naturalJitter": 0},
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        hermes = next(
            item for item in room["participants"] if item["roleId"] == "companion-firstlight-v1"
        )
        facilitator = room["participants"][0]
        self.assertEqual(
            self.service.sessions.get(str(hermes["sessionId"]))[
                "roleBookRevisionId"
            ],
            "",
        )

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:role-book"},
        ) as prompt:
            accepted = self.service.post_room_message(
                str(room["id"]),
                {"message": "时序数据库性能"},
            )

        self.assertEqual(accepted["participant"]["id"], facilitator["id"])
        self.assertEqual(accepted["routeDecision"]["reason"], "facilitator")
        self.assertEqual(
            self.service.sessions.get(str(hermes["sessionId"]))[
                "roleBookRevisionId"
            ],
            "",
        )
        facilitator_context = str(
            prompt.call_args.args[1].get("_transientContext") or ""
        )
        self.assertIn("skill_load 加载 facilitate-room", facilitator_context)
        self.assertIn("room_partner", facilitator_context)
        self.assertIn("当前职责：Room Facilitator", facilitator_context)
        self.assertIn("普通对话或一个连贯动作直接处理", facilitator_context)
        self.assertIn("不要为了展示多 Agent 机械委派", facilitator_context)
        self.assertIn(
            "Partner 交付只是 submission，不是 acceptance",
            facilitator_context,
        )
        self.assertIn("当前没有结构化 WorkItem", facilitator_context)
        self.assertNotIn("当前阶段：普通对话", facilitator_context)
        evidence = self.service.memory_evidence.list(
            role_id=str(facilitator["roleId"]),
            session_id=str(facilitator["sessionId"]),
        )
        self.assertEqual(evidence[0]["sourceKind"], "room_event")
        self.assertFalse(evidence[0]["metadata"]["accepted"])

    def test_active_work_item_owner_overrides_role_book_and_explicit_conflicts(self) -> None:
        role = self.service.personas.resolve("companion-firstlight-v1", "1")
        self.service.role_books.ensure_seeded(
            role.role_id,
            role.version,
            role.display_name,
            role.summary,
            role.version,
        )
        draft = self.service.role_books.propose_revision(
            "companion-firstlight-v1",
            "1",
            {
                "capabilities": [
                    {
                        "itemId": "capability:timeseries-authority",
                        "text": "时序数据库性能专项负责人",
                        "provenance": {
                            "sourceType": "work-receipt",
                            "sourceId": "work:timeseries-authority",
                            "observedAtMs": 100,
                        },
                        "evidenceIds": ["receipt:timeseries-authority"],
                    }
                ]
            },
        )
        self.service.role_books.activate_revision(draft["revisionId"])
        room = self.service.create_room(
            {
                "title": "WorkItem 权威路由",
                "routingPolicy": "natural",
                "routingConfig": {"naturalJitter": 0},
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        hermes = next(
            item for item in room["participants"] if item["roleId"] == "companion-firstlight-v1"
        )
        vcp = next(
            item for item in room["participants"] if item["roleId"] == "companion-future-v1"
        )
        work_item = self.service.room_work.create(
            room_id=str(room["id"]),
            objective="完成时序数据库性能诊断",
            expected_output="给出诊断报告",
            acceptance_criteria=["诊断报告已生成"],
            current_owner_participant_id=str(vcp["id"]),
            created_by_participant_id=str(vcp["id"]),
            client_message_id="work-item-authority-1",
            topic_id=str(room["activeTopicId"]),
            state="active",
        )

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:work-vcp"},
        ):
            routed = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "请处理时序数据库性能问题",
                    "workItemId": work_item["id"],
                },
            )

        self.assertEqual(routed["participant"]["id"], vcp["id"])
        self.assertEqual(routed["routeDecision"]["reason"], "work_item_owner")
        self.assertEqual(routed["workItem"]["id"], work_item["id"])

        with self.assertRaisesRegex(ValueError, "conflicts with the WorkItem"):
            self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "请处理时序数据库性能问题",
                    "participantIds": [str(hermes["id"])],
                    "workItemId": work_item["id"],
                },
            )

        reassigned = self.service.room_work.reassign(
            str(work_item["id"]),
            actor_participant_id=str(vcp["id"]),
            current_owner_participant_id=str(hermes["id"]),
            reason="VCP 正式移交给 Hermes",
        )
        self.assertEqual(
            reassigned["currentOwnerParticipantId"],
            hermes["id"],
        )
        work_events = self.service.room_work.list_events(str(work_item["id"]))
        self.assertEqual(
            [event["eventType"] for event in work_events],
            ["assigned", "accepted", "assigned"],
        )
        self.assertEqual(
            work_events[-1]["payload"]["previousOwnerParticipantId"],
            vcp["id"],
        )
        self.assertEqual(
            work_events[-1]["payload"]["currentOwnerParticipantId"],
            hermes["id"],
        )

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:work-hermes"},
        ):
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "请处理时序数据库性能问题",
                    "participantIds": [str(hermes["id"])],
                    "workItemId": work_item["id"],
                },
            )

        self.assertEqual(accepted["participant"]["id"], hermes["id"])
        self.assertEqual(
            accepted["workItem"]["currentOwnerParticipantId"],
            hermes["id"],
        )

    def test_service_creates_fresh_sessions_routes_one_speaker_and_mirrors_events(self) -> None:
        created = self.service.create_room(
            {
                "title": "产品讨论",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )
        room = created["room"]
        self.assertEqual(room["workspaceRoots"], [str(self.root.resolve()), "/"])
        self.assertEqual(room["executionMode"], "full_trust")
        self.assertEqual(room["lastEventSequence"], 1)
        self.assertEqual(len(room["participants"]), 3)
        hermes = next(item for item in room["participants"] if item["roleId"] == "companion-firstlight-v1")
        current = next(item for item in room["participants"] if item["roleId"] == "companion-present-v1")
        future = next(item for item in room["participants"] if item["roleId"] == "companion-future-v1")
        self.assertEqual(hermes["collaborationRole"], "implementer")
        self.assertEqual(current["collaborationRole"], "coordinator")
        self.assertEqual(future["collaborationRole"], "implementer")
        hermes_session = self.service.sessions.get(str(hermes["sessionId"]))
        current_session = self.service.sessions.get(str(current["sessionId"]))
        future_session = self.service.sessions.get(str(future["sessionId"]))
        self.assertEqual(hermes_session["mode"], "coordinator")
        self.assertEqual(hermes_session["toolProfileVersion"], "control-center-auto-approve-v1")
        self.assertEqual(hermes_session["executionMode"], "full_trust")
        self.assertTrue(hermes_session["workspaceScopeGranted"])
        self.assertEqual(current_session["toolProfileVersion"], "control-center-auto-approve-v1")
        self.assertEqual(current_session["executionMode"], "full_trust")
        self.assertTrue(current_session["workspaceScopeGranted"])
        self.assertEqual(future_session["toolProfileVersion"], "control-center-auto-approve-v1")
        self.assertEqual(future_session["executionMode"], "full_trust")
        self.assertTrue(future_session["workspaceScopeGranted"])
        self.assertEqual(
            future_session["modelProfile"],
            "openai-codex/gpt-5.6-sol",
        )
        self.assertEqual(future_session["thinkingLevel"], "high")
        self.assertEqual(
            future_session["workspaceRoots"],
            [str(self.root.resolve()), "/"],
        )

        with patch.object(self.service, "prompt", return_value={"turnId": "turn:hermes"}) as prompt:
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "@Agent 2 请先诊断状态",
                    "clientMessageId": "room-client-1",
                    "retryOfRootId": "room-turn:prior-failed",
                },
            )
            replay = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "@Agent 2 请先诊断状态",
                    "clientMessageId": "room-client-1",
                    "retryOfRootId": "room-turn:prior-failed",
                },
            )
        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.args[0], str(hermes["sessionId"]))
        prompt_payload = prompt.call_args.args[1]
        self.assertEqual(prompt_payload["message"], "@Agent 2 请先诊断状态")
        room_context = prompt_payload["_transientContext"]
        self.assertIn("<room-context>", room_context)
        self.assertIn("当前角色：实施者", room_context)
        self.assertIn("当前职责：Room Partner", room_context)
        self.assertNotIn(str(self.root.resolve()), room_context)
        self.assertNotIn("@Agent 2 请先诊断状态", room_context)
        self.assertEqual(accepted["participant"]["id"], hermes["id"])
        self.assertEqual(accepted["clientMessageId"], "room-client-1")
        self.assertEqual(accepted["retryOfRootId"], "room-turn:prior-failed")
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(replay["roomTurnId"], accepted["roomTurnId"])

        self.service.events.publish(
            str(hermes["sessionId"]),
            "text_delta",
            {"delta": "正在检查"},
            turn_id="turn:hermes",
            created_at_ms=200,
        )
        self.service.events.publish(
            str(hermes["sessionId"]),
            "turn_completed",
            {"status": "completed"},
            turn_id="turn:hermes",
            created_at_ms=210,
        )
        self.assertTrue(self.service.events.flush())
        events = self.service.rooms.list_events(str(room["id"]))
        self.assertEqual(
            [item["eventType"] for item in events],
            [
                "participant_status",
                "user_message",
                "route_decision",
                "participant_delta",
                "turn_completed",
            ],
        )
        self.assertEqual(events[-2]["participantId"], hermes["id"])
        self.assertEqual(events[-2]["sourceSessionId"], hermes["sessionId"])
        self.assertEqual(events[1]["payload"]["clientMessageId"], "room-client-1")
        self.assertEqual(
            events[1]["payload"]["retryOfRootId"],
            "room-turn:prior-failed",
        )
        self.assertNotEqual(accepted["roomTurnId"], accepted["sessionTurnId"])
        self.assertEqual(
            {item["turnId"] for item in events[1:]},
            {accepted["roomTurnId"]},
        )

        snapshot = self.service.room_snapshot(str(room["id"]))
        self.assertEqual(snapshot["lastSequence"], events[-1]["sequence"])
        self.assertEqual(snapshot["resumeToken"], events[-1]["resumeToken"])
        self.assertEqual(snapshot["room"]["id"], room["id"])

        with self.assertRaisesRegex(
            ValueError,
            "runtime permissions are managed by the Room",
        ):
            self.service.update_session(
                str(hermes["sessionId"]),
                {
                    "mode": "assistant",
                    "toolProfileVersion": "subagent-readonly-v1",
                    "allowedTools": ["overview", "memory"],
                },
            )

        with self.assertRaisesRegex(ValueError, "cannot be deleted directly"):
            self.service.delete_session(str(hermes["sessionId"]))
        self.assertEqual(len(self.service.list_rooms()["items"]), 1)

    def test_room_retry_recovers_faulted_participant_session_before_dispatch(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "失败后继续同一 Room",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        target = room["participants"][0]
        session_id = str(target["sessionId"])
        self.service.sessions.set_status(session_id, "faulted")

        def recover(value: str) -> dict[str, object]:
            self.assertEqual(value, session_id)
            self.service.sessions.set_status(value, "idle")
            return {"reused": True}

        with (
            patch.object(
                self.service.runtime,
                "ensure",
                side_effect=recover,
            ) as ensure,
            patch.object(
                self.service,
                "prompt",
                return_value={"turnId": "turn:recovered-retry"},
            ) as prompt,
        ):
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "继续完成刚才失败的回合",
                    "clientMessageId": "room-retry-after-fault",
                    "retryOfRootId": "room-turn:failed",
                },
            )

        ensure.assert_called_once_with(session_id)
        prompt.assert_called_once()
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["participant"]["id"], target["id"])
        self.assertEqual(accepted["retryOfRootId"], "room-turn:failed")

    def test_room_retry_root_recovers_the_single_returned_work_item(self) -> None:
        room = self.service.create_room(
            {
                "title": "退回后继续原 WorkItem",
                "routingPolicy": "natural",
                "routingConfig": {"naturalJitter": 0},
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        facilitator, partner = room["participants"]
        prior_root_id = "room-turn:returned-work"
        work_item = self.service.room_work.create(
            room_id=str(room["id"]),
            objective="补齐浏览器真实验收证据",
            expected_output="可操作性和需求证据齐全的修订交付",
            acceptance_criteria=["真实浏览器路径已验证"],
            current_owner_participant_id=str(partner["id"]),
            created_by_participant_id=str(facilitator["id"]),
            accountable_participant_id=str(facilitator["id"]),
            client_message_id="returned-work-retry",
            topic_id=str(room["activeTopicId"]),
            root_turn_id=prior_root_id,
            state="active",
        )
        submitted = self.service.room_work.submit(
            str(partner["sessionId"]),
            {
                "workId": work_item["id"],
                "resultSummary": "首轮没有完成真实浏览器验收。",
                "evidenceRefs": ["test:first-pass"],
                "proposedOperabilityVerdict": "unverified",
                "proposedRequirementVerdict": "not_satisfied",
            },
        )
        returned = self.service.room_work.return_for_revision(
            str(facilitator["sessionId"]),
            {
                "workId": work_item["id"],
                "expectedRevision": 0,
                "operabilityVerdict": "unverified",
                "requirementVerdict": "not_satisfied",
                "evidenceRefs": ["test:first-pass"],
                "reason": "请补齐同窗 Browser 的真实可操作性证据。",
            },
        )
        self.assertEqual(submitted["state"], "review")
        self.assertEqual(returned["state"], "active")
        self.assertEqual(returned["revision"], 1)

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:returned-work-retry"},
        ) as prompt:
            resumed = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "继续",
                    "clientMessageId": "retry-returned-work-from-old-ui",
                    "retryOfRootId": prior_root_id,
                },
            )

        prompt.assert_called_once()
        self.assertEqual(prompt.call_args.args[0], str(partner["sessionId"]))
        self.assertEqual(resumed["participant"]["id"], partner["id"])
        self.assertEqual(resumed["routeDecision"]["reason"], "work_item_owner")
        self.assertEqual(resumed["workItem"]["id"], work_item["id"])
        self.assertEqual(resumed["workItem"]["revision"], 1)
        self.assertEqual(len(self.service.room_work.list(room_id=str(room["id"]))), 1)
        retry_dispatch = self.service.room_partner_dispatches.get_by_work(
            str(work_item["id"])
        )
        self.assertEqual(
            retry_dispatch["childDispatchId"],
            resumed["routeDecision"]["dispatchId"],
        )
        self.assertEqual(retry_dispatch["rootId"], resumed["roomTurnId"])
        self.assertEqual(
            retry_dispatch["sourceParticipantId"],
            facilitator["id"],
        )
        self.assertEqual(retry_dispatch["targetParticipantId"], partner["id"])
        self.assertEqual(retry_dispatch["status"], "dispatched")
        self.assertEqual(
            retry_dispatch["targetSessionTurnId"],
            "turn:returned-work-retry",
        )

    def test_room_retry_root_does_not_guess_between_returned_work_items(self) -> None:
        room = self.service.create_room(
            {
                "title": "多个退回项需要明确选择",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        facilitator, partner = room["participants"]
        prior_root_id = "room-turn:ambiguous-returned-work"
        for index in range(2):
            work_item = self.service.room_work.create(
                room_id=str(room["id"]),
                objective=f"修订轨道 {index + 1}",
                expected_output="修订交付",
                acceptance_criteria=["补齐证据"],
                current_owner_participant_id=str(partner["id"]),
                created_by_participant_id=str(facilitator["id"]),
                accountable_participant_id=str(facilitator["id"]),
                client_message_id=f"ambiguous-returned-work:{index}",
                topic_id=str(room["activeTopicId"]),
                root_turn_id=prior_root_id,
            )
            self.service.room_work.submit(
                str(partner["sessionId"]),
                {
                    "workId": work_item["id"],
                    "resultSummary": "仍需修订。",
                    "evidenceRefs": [f"test:ambiguous:{index}"],
                    "proposedOperabilityVerdict": "unverified",
                    "proposedRequirementVerdict": "not_satisfied",
                },
            )
            self.service.room_work.return_for_revision(
                str(facilitator["sessionId"]),
                {
                    "workId": work_item["id"],
                    "expectedRevision": 0,
                    "operabilityVerdict": "unverified",
                    "requirementVerdict": "not_satisfied",
                    "evidenceRefs": [f"test:ambiguous:{index}"],
                    "reason": "补齐证据后重交。",
                },
            )

        with self.assertRaisesRegex(ValueError, "provide workItemId"):
            self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "继续",
                    "clientMessageId": "ambiguous-retry-from-old-ui",
                    "retryOfRootId": prior_root_id,
                },
            )

    def test_user_room_message_resumes_paused_goal_before_dispatch(self) -> None:
        """An explicit user Room message is the only conversation entry a
        returning user has; it must resume a paused target Goal before the
        Root and user event are persisted, then deliver normally."""

        room = self.service.create_room(
            {
                "title": "暂停后从会话入口继续",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        lead = room["participants"][0]
        lead_session = str(lead["sessionId"])
        goal = self.service.sessions.mutate_agent_goal(
            lead_session,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "objective": "完成小游戏项目",
                "expectedRevision": 0,
            },
        )["workflow"]["goal"]
        self.service.sessions.mutate_agent_goal(
            lead_session,
            {"action": "pause", "expectedRevision": goal["revision"]},
        )

        statuses_at_dispatch: list[str] = []

        def observe_prompt(
            session_id: str,
            payload: dict[str, object],
        ) -> dict[str, object]:
            statuses_at_dispatch.append(
                str(self.service.sessions.agent_goal(session_id)["status"])
            )
            return {"turnId": "turn:resumed"}

        with patch.object(
            self.service,
            "prompt",
            side_effect=observe_prompt,
        ) as prompt:
            accepted = self.service.post_room_message(
                str(room["id"]),
                {"message": "继续完成", "clientMessageId": "room-goal-resume-1"},
            )

        prompt.assert_called_once()
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["participant"]["id"], lead["id"])
        self.assertEqual(statuses_at_dispatch, ["active"])
        resumed = self.service.sessions.agent_goal(lead_session)
        self.assertEqual(resumed["status"], "active")
        event_types = [
            event["eventType"]
            for event in self.service.room_snapshot(str(room["id"]))["events"]
        ]
        self.assertNotIn("turn_failed", event_types)

    def test_user_room_message_does_not_resume_cancelled_goal_and_projects_cause(
        self,
    ) -> None:
        """Only a paused Goal is resumed. Terminal Goals keep the existing
        rejection, and the goal cause code must survive the command receipt
        and reach the durable turn_failed event."""

        room = self.service.create_room(
            {
                "title": "已取消 Goal 不自动恢复",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        lead = room["participants"][0]
        lead_session = str(lead["sessionId"])
        goal = self.service.sessions.mutate_agent_goal(
            lead_session,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "objective": "已经放弃的目标",
                "expectedRevision": 0,
            },
        )["workflow"]["goal"]
        self.service.sessions.mutate_agent_goal(
            lead_session,
            {
                "action": "cancel",
                "expectedRevision": goal["revision"],
                "reason": "用户放弃",
            },
        )

        with self.assertRaises(ValueError) as blocked:
            self.service.post_room_message(
                str(room["id"]),
                {"message": "继续完成", "clientMessageId": "room-goal-cancelled-1"},
            )

        self.assertEqual(
            getattr(blocked.exception, "cause_code", ""),
            "goal_cancelled",
        )
        self.assertEqual(
            str(self.service.sessions.agent_goal(lead_session)["status"]),
            "cancelled",
        )
        failures = [
            event
            for event in self.service.room_snapshot(str(room["id"]))["events"]
            if event["eventType"] == "turn_failed"
        ]
        self.assertEqual(len(failures), 1)
        # Room durable events uppercase the same canonical lowercase error_code.
        self.assertEqual(
            failures[0]["payload"].get("causeCode"),
            "GOAL_CANCELLED",
        )

    def test_room_messages_always_use_participant_sessions_even_with_retired_kernel_mode(self) -> None:
        """A stale install flag must not resurrect the retired Room runtime."""

        room = self.service.create_room(
            {
                "title": "Session 组合唯一入口",
                "routingPolicy": "parallel",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        facilitator = room["participants"][0]
        self.assertFalse(hasattr(self.service, "room_kernel"))

        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:session-room-only"},
        ) as prompt:
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "请先理解目标，再自行决定是否需要伙伴。",
                    "clientMessageId": "room-session-only-1",
                },
            )

        prompt.assert_called_once()
        self.assertEqual(accepted["executionOwner"], "session")
        self.assertEqual(accepted["participant"]["id"], facilitator["id"])
        self.assertEqual(accepted["dispatches"][0]["sessionTurnId"], "turn:session-room-only")

    def test_room_steer_is_recorded_before_the_active_pi_session_receives_it(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room Steer",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        facilitator = room["participants"][0]
        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:room-steer"},
        ):
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "先检查当前实现。",
                    "clientMessageId": "room-steer-start",
                },
            )

        observed_event_types: list[str] = []

        def accept_steer(_session_id: str, payload: Mapping[str, object]) -> dict[str, object]:
            observed_event_types.extend(
                str(event["eventType"])
                for event in self.service.rooms.list_events(str(room["id"]))
            )
            self.assertEqual(payload["delivery"], "steer")
            self.assertEqual(payload["message"], "先停止旧方向，只验证 Stop。")
            return {"turnId": "turn:room-steer", "queued": True}

        with patch.object(self.service, "prompt", side_effect=accept_steer) as prompt:
            receipt = self.service.steer_room_participant(
                str(room["id"]),
                {
                    "action": "steer_participant",
                    "rootId": accepted["roomTurnId"],
                    "participantId": facilitator["id"],
                    "message": "先停止旧方向，只验证 Stop。",
                    "clientActionId": "room-steer-action-1",
                },
            )

        prompt.assert_called_once()
        self.assertEqual(observed_event_types[-1], "user_message")
        self.assertTrue(receipt["accepted"])
        self.assertEqual(receipt["delivery"], "steer")
        self.assertEqual(receipt["participantId"], facilitator["id"])

    def test_pending_room_turn_rejects_queued_prior_turn_events_until_runtime_acceptance(
        self,
    ) -> None:
        room = self.service.create_room(
            {
                "title": "Room runtime turn fence",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {
                        "roleId": "companion-present-v1",
                        "roleVersion": "1",
                    },
                    {
                        "roleId": "companion-firstlight-v1",
                        "roleVersion": "1",
                    },
                ],
            }
        )["room"]
        room_id = str(room["id"])
        participant = room["participants"][0]
        session_id = str(participant["sessionId"])
        root_id = "room-root:new"
        dispatch_id = "room-dispatch:new"
        prior_runtime_turn_id = "runtime-turn:prior"
        accepted_runtime_turn_id = "runtime-turn:new"
        baseline_count = len(self.service.rooms.list_events(room_id))

        # Keep the projection worker behind the registry lock so an old delta
        # crosses begin(), then both the old terminal and the real turn's
        # complete event sequence arrive before prompt() returns its ACK.
        with self.service.room_turns.lock:
            old_delta = self.service.events.publish(
                session_id,
                "text_delta",
                {"delta": "stale content"},
                turn_id=prior_runtime_turn_id,
                created_at_ms=100,
            )
            self.service._begin_room_turn(
                session_id,
                root_id,
                str(room["activeTopicId"]),
                dispatch_id=dispatch_id,
            )
            old_terminal = self.service.events.publish(
                session_id,
                "turn_completed",
                {"status": "completed"},
                turn_id=prior_runtime_turn_id,
                created_at_ms=110,
            )
            real_delta = self.service.events.publish(
                session_id,
                "text_delta",
                {"delta": "current content"},
                turn_id=accepted_runtime_turn_id,
                created_at_ms=120,
            )
            real_terminal = self.service.events.publish(
                session_id,
                "turn_completed",
                {"status": "completed"},
                turn_id=accepted_runtime_turn_id,
                created_at_ms=130,
            )

        self.assertTrue(self.service.events.flush())
        self.assertEqual(
            self.service.room_turns.pending_turn_by_session.get(session_id),
            root_id,
        )
        self.assertNotIn(
            (session_id, prior_runtime_turn_id),
            self.service.room_turns.turn_by_session_turn,
        )
        before_accept = self.service.rooms.list_events(room_id)[baseline_count:]
        self.assertFalse(
            {
                old_delta.event_id,
                old_terminal.event_id,
                real_delta.event_id,
                real_terminal.event_id,
            }
            & {
                str(event["payload"].get("sourceEventId") or "")
                for event in before_accept
            }
        )

        self.service._accept_room_turn(
            session_id,
            accepted_runtime_turn_id,
            root_id,
        )
        self.assertFalse(
            self.service.room_turns.session_turn_active(session_id)
        )
        self.assertNotIn(
            (session_id, accepted_runtime_turn_id),
            self.service.room_turns.turn_by_session_turn,
        )
        self.assertEqual(
            self.service.room_turns.pending_events_by_session_turn,
            {},
        )

        projected = self.service.rooms.list_events(room_id)[baseline_count:]
        self.assertEqual(
            [event["eventType"] for event in projected],
            ["participant_delta", "turn_completed"],
        )
        self.assertEqual(
            {
                str(event["payload"].get("sourceEventId") or "")
                for event in projected
            },
            {real_delta.event_id, real_terminal.event_id},
        )
        self.assertEqual(
            {event["turnId"] for event in projected},
            {root_id},
        )
        self.assertEqual(
            projected[0]["payload"]["data"]["delta"],
            "current content",
        )
        self.assertEqual(
            projected[0]["payload"]["data"]["dispatchId"],
            dispatch_id,
        )

    def test_unrestricted_collaboration_profiles_keep_optional_roots_and_system_access(self) -> None:
        context_roots = [
            self.root / f"optional-context-{index}"
            for index in range(4)
        ]
        caller_contexts = [str(path) for path in context_roots]
        participants = [
            {"roleId": "companion-present-v1", "roleVersion": "1"},
            {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
        ]
        for execution_mode, profile in (
            ("per_action", "control-center-full-access-v1"),
            ("full_trust", "control-center-auto-approve-v1"),
        ):
            room = self.service.create_room(
                {
                    "title": f"Room {execution_mode}",
                    "roomKind": "collaboration",
                    "executionMode": execution_mode,
                    "workspaceRoots": caller_contexts,
                    "participants": participants,
                }
            )["room"]
            expected_roots = [
                *(str(path.resolve()) for path in context_roots),
                "/",
            ]
            self.assertEqual(room["workspaceRoots"], expected_roots)
            for participant in room["participants"]:
                session = self.service.sessions.get(str(participant["sessionId"]))
                self.assertEqual(session["mode"], "coordinator")
                self.assertEqual(session["executionMode"], execution_mode)
                self.assertEqual(session["toolProfileVersion"], profile)
                self.assertEqual(session["toolAllowlistMode"], "profile")
                self.assertEqual(session["allowedTools"], [])
                self.assertEqual(session["workspaceRoots"], expected_roots)
                self.assertTrue(session["projectContextEnabled"])
                self.assertTrue(session["piSkillsEnabled"])
                self.assertTrue(session["codexSkillsEnabled"])

    def test_unrestricted_collaboration_participant_lifecycle_uses_shared_policy(
        self,
    ) -> None:
        for execution_mode, profile in (
            ("per_action", "control-center-full-access-v1"),
            ("full_trust", "control-center-auto-approve-v1"),
        ):
            with self.subTest(execution_mode=execution_mode):
                context_roots = [
                    self.root / f"{execution_mode}-context-{index}"
                    for index in range(4)
                ]
                expected_roots = [
                    *(str(path.resolve()) for path in context_roots),
                    "/",
                ]
                room = self.service.create_room(
                    {
                        "title": f"生命周期 {execution_mode}",
                        "roomKind": "collaboration",
                        "executionMode": execution_mode,
                        "workspaceRoots": [
                            str(path) for path in context_roots
                        ],
                        "participants": [
                            {
                                "roleId": "companion-present-v1",
                                "roleVersion": "1",
                            },
                            {
                                "roleId": "companion-firstlight-v1",
                                "roleVersion": "1",
                            },
                        ],
                    }
                )["room"]
                room_id = str(room["id"])

                added = self.service.add_room_participant(
                    room_id,
                    {
                        "roleId": "companion-future-v1",
                        "roleVersion": "1",
                    },
                )
                added_session = self.service.sessions.get(
                    str(added["participant"]["sessionId"])
                )
                self.assertEqual(
                    added_session["toolProfileVersion"],
                    profile,
                )
                self.assertEqual(
                    added_session["workspaceRoots"],
                    expected_roots,
                )
                self.assertTrue(
                    added_session["projectContextEnabled"]
                )
                self.assertTrue(added_session["piSkillsEnabled"])
                self.assertTrue(added_session["codexSkillsEnabled"])

                missing_participant = room["participants"][0]
                missing_session_id = str(
                    missing_participant["sessionId"]
                )
                with sqlite3.connect(self.service.sessions.db_path) as conn:
                    conn.execute("PRAGMA foreign_keys = OFF")
                    conn.execute(
                        "DELETE FROM agent_sessions WHERE id = ?",
                        (missing_session_id,),
                    )

                stale_participant = room["participants"][1]
                stale_session_id = str(
                    stale_participant["sessionId"]
                )
                self.service.sessions.set_runtime_policy(
                    stale_session_id,
                    mode="coordinator",
                    tool_profile_version="control-center-v1",
                    execution_mode=execution_mode,
                    grant_workspace_scope=(
                        execution_mode == "full_trust"
                    ),
                    allowed_tools=["workspace_read"],
                    project_context_enabled=False,
                    pi_skills_enabled=False,
                    codex_skills_enabled=False,
                    workspace_roots=[str(self.root)],
                )

                snapshot = self.service.room_snapshot(room_id)
                repaired_room = snapshot["room"]
                for participant in repaired_room["participants"]:
                    session = self.service.sessions.get(
                        str(participant["sessionId"])
                    )
                    self.assertEqual(session["mode"], "coordinator")
                    self.assertEqual(
                        session["executionMode"],
                        execution_mode,
                    )
                    self.assertEqual(
                        session["toolProfileVersion"],
                        profile,
                    )
                    self.assertEqual(
                        session["toolAllowlistMode"],
                        "profile",
                    )
                    self.assertEqual(session["allowedTools"], [])
                    self.assertEqual(
                        session["workspaceRoots"],
                        expected_roots,
                    )
                    self.assertTrue(
                        session["workspaceScopeGranted"]
                    )
                    self.assertTrue(
                        session["projectContextEnabled"]
                    )
                    self.assertTrue(session["piSkillsEnabled"])
                    self.assertTrue(session["codexSkillsEnabled"])


    def test_room_execution_mode_updates_all_participants_atomically(self) -> None:
        room = self.service.create_room(
            {
                "title": "Room 执行权限",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        room_id = str(room["id"])

        read_only = self.service.update_room(
            room_id,
            {"executionMode": "read_only"},
        )["room"]
        self.assertEqual(read_only["executionMode"], "read_only")
        for participant in read_only["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["executionMode"], "read_only")
            self.assertEqual(session["toolProfileVersion"], "subagent-readonly-v1")
            self.assertFalse(session["workspaceScopeGranted"])
        for participant in read_only["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.service.sessions.set_runtime_policy(
                str(session["id"]),
                mode="coordinator",
                tool_profile_version="subagent-readonly-v1",
                execution_mode="read_only",
                allowed_tools=["workspace_read"],
                project_context_enabled=True,
                workspace_roots=session["workspaceRoots"],
            )

        per_action = self.service.update_room(
            room_id,
            {"executionMode": "per_action"},
        )["room"]
        self.assertEqual(per_action["executionMode"], "per_action")
        for participant in per_action["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["executionMode"], "per_action")
            self.assertEqual(session["toolProfileVersion"], "control-center-full-access-v1")
            self.assertEqual(session["toolAllowlistMode"], "profile")
            self.assertEqual(session["allowedTools"], [])
            self.assertEqual(session["workspaceRoots"], [str(self.root.resolve()), "/"])
            self.assertTrue(session["workspaceScopeGranted"])
            self.assertTrue(session["projectContextEnabled"])
            self.assertTrue(session["piSkillsEnabled"])
            self.assertTrue(session["codexSkillsEnabled"])

        with self.assertRaisesRegex(ValueError, "workspace scope confirmation"):
            self.service.update_room(
                room_id,
                {"executionMode": "workspace_managed"},
            )
        managed = self.service.update_room(
            room_id,
            {
                "executionMode": "workspace_managed",
                "workspaceScopeConfirmation": "APPROVE_WORKSPACE_SCOPE",
            },
        )["room"]
        self.assertEqual(managed["executionMode"], "workspace_managed")
        for participant in managed["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["executionMode"], "workspace_managed")
            self.assertEqual(session["toolProfileVersion"], "control-center-v1")
            self.assertTrue(session["workspaceScopeGranted"])

        full_trust = self.service.update_room(
            room_id,
            {"executionMode": "full_trust"},
        )["room"]
        self.assertEqual(full_trust["executionMode"], "full_trust")
        for participant in full_trust["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["executionMode"], "full_trust")
            self.assertEqual(session["toolProfileVersion"], "control-center-auto-approve-v1")
            self.assertEqual(session["toolAllowlistMode"], "profile")
            self.assertEqual(session["allowedTools"], [])
            self.assertEqual(session["workspaceRoots"], [str(self.root.resolve()), "/"])
            self.assertTrue(session["workspaceScopeGranted"])
            self.assertTrue(session["projectContextEnabled"])
            self.assertTrue(session["piSkillsEnabled"])
            self.assertTrue(session["codexSkillsEnabled"])

        changed_events = [
            event
            for event in self.service.rooms.list_events(room_id)
            if event["eventType"] == "room_config_changed"
            and event["payload"].get("status")
            in {
                "room_execution_mode_updated",
                "room_config_and_execution_mode_updated",
            }
        ]
        self.assertEqual(
            [event["payload"]["executionMode"] for event in changed_events],
            ["read_only", "per_action", "workspace_managed", "full_trust"],
        )

    def test_room_configuration_and_execution_mode_share_one_transaction(self) -> None:
        room = self.service.create_room(
            {
                "title": "原始 Room 名称",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        room_id = str(room["id"])
        original_revision = int(room["configRevision"])

        with self.assertRaisesRegex(
            ValueError,
            "fallbackParticipantId",
        ):
            self.service.update_room(
                room_id,
                {
                    "title": "不应半保存",
                    "routingConfig": {
                        "maxResponders": 1,
                        "naturalJitter": 0,
                        "fallbackParticipantId": "missing-participant",
                    },
                    "executionMode": "read_only",
                },
            )

        unchanged = self.service.rooms.get(room_id)
        self.assertEqual(unchanged["title"], "原始 Room 名称")
        self.assertEqual(unchanged["executionMode"], "full_trust")
        self.assertEqual(unchanged["configRevision"], original_revision)
        for participant in unchanged["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["executionMode"], "full_trust")
            self.assertTrue(session["workspaceScopeGranted"])

        updated = self.service.update_room(
            room_id,
            {
                "title": "统一保存后的名称",
                "description": "配置与权限一次提交",
                "executionMode": "per_action",
            },
        )["room"]
        self.assertEqual(updated["title"], "统一保存后的名称")
        self.assertEqual(updated["description"], "配置与权限一次提交")
        self.assertEqual(updated["executionMode"], "per_action")
        self.assertEqual(updated["workspaceRoots"], [str(self.root.resolve()), "/"])
        self.assertEqual(updated["configRevision"], original_revision + 1)
        for participant in updated["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["executionMode"], "per_action")
            self.assertEqual(session["toolProfileVersion"], "control-center-full-access-v1")
            self.assertEqual(session["toolAllowlistMode"], "profile")
            self.assertEqual(session["workspaceRoots"], [str(self.root.resolve()), "/"])
            self.assertTrue(session["workspaceScopeGranted"])
            self.assertTrue(session["projectContextEnabled"])
            self.assertTrue(session["piSkillsEnabled"])
            self.assertTrue(session["codexSkillsEnabled"])

        changed = [
            event
            for event in self.service.rooms.list_events(room_id)
            if event["eventType"] == "room_config_changed"
        ][-1]
        self.assertEqual(
            changed["payload"]["status"],
            "room_config_and_execution_mode_updated",
        )
        self.assertEqual(
            changed["payload"]["changedFields"],
            ["description", "executionMode", "permissionPolicy", "title", "workspaceRoots"],
        )

    def test_full_auto_room_reuses_its_single_mode_confirmation(self) -> None:
        room = self.service.create_room(
            {
                "title": "一次确认",
                "executionMode": "full_trust",
                "dangerousModeConfirmation": "ENABLE_FULL_TRUST",
                "workspaceRoots": [],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]

        updated = self.service.update_room(
            str(room["id"]),
            {
                "title": "无需重复确认",
                "executionMode": "full_trust",
                "workspaceRoots": [str(self.root)],
            },
        )["room"]

        self.assertEqual(updated["title"], "无需重复确认")
        self.assertEqual(updated["executionMode"], "full_trust")
        self.assertEqual(updated["workspaceRoots"], [str(self.root.resolve()), "/"])

    def test_multi_mentions_share_one_root_but_keep_independent_dispatches(self) -> None:
        room = self.service.create_room(
            {
                "title": "双 Agent 核对",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        current, firstlight, _future = room["participants"]
        turns = {
            str(current["sessionId"]): "turn:multi:current",
            str(firstlight["sessionId"]): "turn:multi:firstlight",
        }

        dispatch_barrier = threading.Barrier(2)

        def accept(session_id: str, _payload: dict[str, object]) -> dict[str, object]:
            dispatch_barrier.wait(timeout=2)
            return {"turnId": turns[session_id]}

        with patch.object(self.service, "prompt", side_effect=accept) as prompt:
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": (
                        f"@{current['displayName']} @{firstlight['displayName']} "
                        "分别核对实现和证据"
                    ),
                    "clientMessageId": "room-multi-1",
                },
            )

        self.assertTrue(accepted["accepted"])
        self.assertEqual(prompt.call_count, 2)
        self.assertEqual(
            {item["participantId"] for item in accepted["dispatches"]},
            {current["id"], firstlight["id"]},
        )
        self.assertEqual(
            len({item["dispatchId"] for item in accepted["dispatches"]}),
            2,
        )
        root_id = str(accepted["roomTurnId"])
        initial_events = self.service.rooms.list_events(str(room["id"]))
        self.assertEqual(
            [event["eventType"] for event in initial_events],
            ["participant_status", "user_message", "route_decision", "route_decision"],
        )
        self.assertEqual(
            initial_events[1]["payload"]["targetParticipantIds"],
            [current["id"], firstlight["id"]],
        )
        self.assertEqual(
            {event["turnId"] for event in initial_events[1:]},
            {root_id},
        )
        route_dispatches = {
            event["participantId"]: event["payload"]["dispatchId"]
            for event in initial_events
            if event["eventType"] == "route_decision"
        }
        self.assertEqual(len(set(route_dispatches.values())), 2)

        self.service.events.publish(
            str(current["sessionId"]),
            "turn_completed",
            {"status": "completed"},
            turn_id=turns[str(current["sessionId"])],
            created_at_ms=200,
        )
        self.assertEqual(
            self.service._room_topic_for_turn(root_id),
            room["activeTopicId"],
        )
        self.service.events.publish(
            str(firstlight["sessionId"]),
            "text_delta",
            {"delta": "证据已核对"},
            turn_id=turns[str(firstlight["sessionId"])],
            created_at_ms=205,
        )
        self.service.events.publish(
            str(firstlight["sessionId"]),
            "turn_completed",
            {"status": "completed"},
            turn_id=turns[str(firstlight["sessionId"])],
            created_at_ms=210,
        )
        self.assertTrue(self.service.events.flush())

        mirrored = self.service.rooms.list_events(str(room["id"]))[-3:]
        self.assertEqual(
            [event["eventType"] for event in mirrored],
            ["turn_completed", "participant_delta", "turn_completed"],
        )
        for event in mirrored:
            data = event["payload"]["data"]
            self.assertEqual(data["rootId"], root_id)
            self.assertEqual(
                data["dispatchId"],
                route_dispatches[event["participantId"]],
            )
        self.assertEqual(self.service._room_topic_for_turn(root_id), "")

    def test_root_abort_cancels_all_participants_and_fences_late_room_events(self) -> None:
        room = self.service.create_room(
            {
                "title": "整轮停止",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        first, second = room["participants"]
        turns = {
            str(first["sessionId"]): "turn:abort:first",
            str(second["sessionId"]): "turn:abort:second",
        }

        with patch.object(
            self.service,
            "prompt",
            side_effect=lambda session_id, _payload: {"turnId": turns[session_id]},
        ):
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": (
                        f"@{first['displayName']} @{second['displayName']} "
                        "并行检查后统一交付"
                    ),
                    "clientMessageId": "room-root-abort-message",
                },
            )

        def typed_abort(session_id: str) -> dict[str, object]:
            return {
                "schemaVersion": "rag-ime.agent-abort.v1",
                "ok": True,
                "sessionId": session_id,
                "runtimeReceipt": {
                    "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                    "sessionId": session_id,
                    "turnId": turns[session_id],
                    "cancelledDecisionIds": [],
                    "cancelledUIRequestIds": [],
                    "lifecycle": {
                        "schemaVersion": "pi.agent-abort-receipt.v1",
                        "scopeId": session_id,
                        "generation": 1,
                        "reason": "user_abort",
                        "cancelledContinuationIds": [
                            f"continuation:{session_id}"
                        ],
                        "cancelledOperationIds": [
                            f"provider:{session_id}",
                            f"tool:{session_id}",
                        ],
                        "failedOperationIds": [],
                        "operations": [
                            {
                                "operationId": f"provider:{session_id}",
                                "kind": "provider",
                            },
                            {
                                "operationId": f"tool:{session_id}",
                                "kind": "tool",
                            },
                        ],
                        "pendingOperations": [],
                        "drained": True,
                        "idle": True,
                    },
                },
            }

        with patch.object(self.service, "abort", side_effect=typed_abort) as abort:
            receipt = self.service.abort_room_turn(
                str(room["id"]),
                {
                    "roomTurnId": accepted["roomTurnId"],
                    "clientRequestId": "room-root-abort-1",
                },
            )
            replay = self.service.abort_room_turn(
                str(room["id"]),
                {
                    "roomTurnId": accepted["roomTurnId"],
                    "clientRequestId": "room-root-abort-1",
                },
            )

        self.assertEqual(
            {call.args[0] for call in abort.call_args_list},
            {str(first["sessionId"]), str(second["sessionId"])},
        )
        self.assertEqual(receipt["status"], "terminated")
        self.assertEqual(receipt["pendingTargets"], [])
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(
            replay["cancellationReceiptId"],
            receipt["cancellationReceiptId"],
        )

        events_before_late = self.service.rooms.list_events(str(room["id"]))
        terminal = [
            event
            for event in events_before_late
            if event["eventType"] == "turn_completed"
        ]
        self.assertEqual(
            {event["participantId"] for event in terminal},
            {first["id"], second["id"]},
        )
        self.assertTrue(
            all(event["payload"]["status"] == "aborted" for event in terminal)
        )

        self.service.events.publish(
            str(first["sessionId"]),
            "text_delta",
            {"delta": "不应重新出现在 Room"},
            turn_id=turns[str(first["sessionId"])],
            created_at_ms=999,
        )
        self.service.events.publish(
            str(first["sessionId"]),
            "turn_completed",
            {"status": "completed"},
            turn_id=turns[str(first["sessionId"])],
            created_at_ms=1000,
        )
        self.assertEqual(
            self.service.rooms.list_events(str(room["id"])),
            events_before_late,
        )

    def test_root_abort_never_marks_final_while_a_runtime_surface_is_unverified(self) -> None:
        room = self.service.create_room(
            {
                "title": "停止未确认",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        participant = room["participants"][0]
        with patch.object(
            self.service,
            "prompt",
            return_value={"turnId": "turn:abort:pending"},
        ):
            accepted = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": f"@{participant['displayName']} 执行长任务",
                    "clientMessageId": "room-root-abort-pending-message",
                },
            )
        unverified = {
            "schemaVersion": "rag-ime.agent-abort.v1",
            "ok": True,
            "sessionId": str(participant["sessionId"]),
            "runtimeReceipt": {
                "schemaVersion": "rag-ime.pi-session-abort-receipt.v1",
                "sessionId": str(participant["sessionId"]),
                "turnId": "turn:abort:pending",
                "lifecycle": {
                    "schemaVersion": "pi.agent-abort-receipt.v1",
                    "scopeId": str(participant["sessionId"]),
                    "generation": 1,
                    "reason": "user_abort",
                    "cancelledContinuationIds": [],
                    "cancelledOperationIds": [],
                    "failedOperationIds": [],
                    "operations": [
                        {"operationId": "provider:pending", "kind": "provider"},
                    ],
                    "pendingOperations": [
                        {"operationId": "provider:pending", "kind": "provider"},
                    ],
                    "drained": False,
                    "idle": False,
                },
            },
        }

        with patch.object(self.service, "abort", return_value=unverified):
            receipt = self.service.abort_room_turn(
                str(room["id"]),
                {
                    "roomTurnId": accepted["roomTurnId"],
                    "clientRequestId": "room-root-abort-pending-1",
                },
            )

        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["status"], "cancellation_pending")
        self.assertIn("provider", receipt["pendingTargets"])
        self.assertIn("session", receipt["pendingTargets"])
        turn_events = [
            event
            for event in self.service.rooms.list_events(str(room["id"]))
            if event["turnId"] == accepted["roomTurnId"]
        ]
        self.assertFalse(
            any(event["eventType"] == "turn_completed" for event in turn_events)
        )
        self.assertEqual(
            turn_events[-1]["payload"]["status"],
            "cancellation_pending",
        )

        self.service.events.publish(
            str(participant["sessionId"]),
            "turn_completed",
            {"status": "aborted", "aborted": True},
            turn_id="turn:abort:pending",
            created_at_ms=1_000,
        )
        self.assertTrue(self.service.events.flush())
        terminal_events = [
            event
            for event in self.service.rooms.list_events(str(room["id"]))
            if event["turnId"] == accepted["roomTurnId"]
            and event["eventType"] == "turn_completed"
        ]
        self.assertEqual(len(terminal_events), 1)
        terminal_payload = terminal_events[0]["payload"]
        self.assertEqual(
            terminal_payload["data"]["status"],
            "aborted",
        )
        self.assertEqual(
            terminal_payload["data"]["cancellationReceiptId"],
            receipt["cancellationReceiptId"],
        )

        self.service.events.publish(
            str(participant["sessionId"]),
            "turn_completed",
            {"status": "aborted", "aborted": True},
            turn_id="turn:abort:pending",
            created_at_ms=1_001,
        )
        self.assertTrue(self.service.events.flush())
        terminal_events = [
            event
            for event in self.service.rooms.list_events(str(room["id"]))
            if event["turnId"] == accepted["roomTurnId"]
            and event["eventType"] == "turn_completed"
        ]
        self.assertEqual(len(terminal_events), 1)

    def test_room_input_stays_a_user_message_and_room_delta_is_provider_only(self) -> None:
        created = self.service.create_room(
            {
                "title": "Provider-only Room",
                "routingPolicy": "manual_mentions",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        target = created["participants"][0]
        first_text = "@澄·今 检查增量上下文"
        with patch.object(
            self.service.runtime,
            "prompt",
            return_value={
                "accepted": True,
                "turnId": "turn:provider-only:1",
                "piEntryId": "entry:provider-only:1",
                "response": {"success": True},
            },
        ) as runtime_prompt:
            accepted = self.service.post_room_message(
                str(created["id"]),
                {
                    "message": first_text,
                    "participantIds": [str(target["id"])],
                },
            )

        runtime_message = runtime_prompt.call_args.args[1]
        envelope = json.loads(
            runtime_message.removeprefix(RUNTIME_PROMPT_ENVELOPE_PREFIX)
        )
        self.assertEqual(envelope["message"], first_text)
        self.assertIn("<room-context>", envelope["transientContext"])
        self.assertNotIn(first_text, envelope["transientContext"])
        self.assertNotIn("受管 Room 上下文", runtime_message)

        session_events, _ = self.service.events.replay(str(target["sessionId"]))
        user_messages = [
            event.payload["message"]
            for event in session_events
            if event.event_type == "message_completed"
            and isinstance(event.payload.get("message"), dict)
            and event.payload["message"].get("role") == "user"
        ]
        self.assertEqual(len(user_messages), 1)
        self.assertEqual(
            user_messages[0]["blocks"][0]["data"]["text"],
            first_text,
        )
        self.assertNotIn(
            "room-context",
            json.dumps(user_messages[0], ensure_ascii=False),
        )

        self.service.events.publish(
            str(target["sessionId"]),
            "turn_completed",
            {"status": "completed"},
            turn_id=str(accepted["sessionTurnId"]),
        )

    def test_room_user_priority_is_released_when_turn_preparation_fails(self) -> None:
        created = self.service.create_room(
            {
                "title": "异常恢复",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )
        room = created["room"]
        target = room["participants"][0]
        with patch.object(
            self.service.room_events,
            "publish",
            side_effect=RuntimeError("event store unavailable"),
        ):
            with self.assertRaisesRegex(RuntimeError, "event store unavailable"):
                self.service.post_room_message(
                    str(room["id"]),
                    {
                        "message": f"@{target['displayName']} 请检查状态",
                        "participantIds": [str(target["id"])],
                    },
                )
        self.assertNotIn(
            str(target["sessionId"]),
            self.service.room_turns.user_priority_sessions,
        )

    def test_room_rejects_duplicate_roles_and_archives_without_deleting_sessions(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be unique"):
            self.service.create_room(
                {
                    "title": "重复角色",
                    "workspaceRoots": [str(self.root)],
                    "participants": [
                        {"roleId": "companion-future-v1", "roleVersion": "1"},
                        {"roleId": "companion-future-v1", "roleVersion": "1"},
                    ],
                }
            )
        self.assertEqual(self.service.list_sessions()["items"], [])

        created = self.service.create_room(
            {
                "title": "主持房间",
                "routingPolicy": "moderator",
                "moderatorRoleId": "companion-future-v1",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )
        future = next(
            item for item in created["room"]["participants"] if item["roleId"] == "companion-future-v1"
        )
        self.assertEqual(future["collaborationRole"], "coordinator")
        room_id = str(created["room"]["id"])
        with patch.object(self.service, "prompt", return_value={"turnId": "turn:future"}) as prompt:
            accepted = self.service.post_room_message(
                room_id,
                {"message": "请协调大家检查当前项目"},
            )
        self.assertEqual(accepted["participant"]["id"], future["id"])
        prompt_payload = prompt.call_args.args[1]
        self.assertEqual(prompt_payload["message"], "请协调大家检查当前项目")
        moderator_context = prompt_payload["_transientContext"]
        self.assertIn("当前角色：主持整合者", moderator_context)
        self.assertIn("当前职责：Room Facilitator", moderator_context)
        self.assertNotIn("agents.room_ask", moderator_context)
        self.assertNotIn("role=researcher", moderator_context)
        self.assertNotIn("role=implementer", moderator_context)

        updated = self.service.update_room(room_id, {"archived": True})
        self.assertEqual(updated["room"]["status"], "archived")
        self.assertEqual(self.service.list_rooms()["items"], [])
        self.assertEqual(len(self.service.list_rooms({"includeArchived": True})["items"]), 1)
        self.assertEqual(len(self.service.list_sessions()["items"]), 3)
        archived_member = updated["room"]["participants"][0]
        with self.assertRaisesRegex(ValueError, "cannot be deleted directly"):
            self.service.delete_session(str(archived_member["sessionId"]))

    def test_room_projection_keeps_public_output_but_drops_private_tool_payloads(self) -> None:
        room = self.service.create_room(
            {
                "title": "安全投影",
                "workspaceRoots": [str(self.root)],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-firstlight-v1", "roleVersion": "1"},
                ],
            }
        )["room"]
        hermes = next(item for item in room["participants"] if item["roleId"] == "companion-firstlight-v1")
        session_id = str(hermes["sessionId"])
        room_turn_id = "room-turn:safe"
        self.service._begin_room_turn(
            session_id,
            room_turn_id,
            str(room["activeTopicId"]),
            dispatch_id="room-dispatch:safe",
        )
        self.service._accept_room_turn(
            session_id,
            "turn:safe",
            room_turn_id,
        )

        self.service.events.publish(
            session_id,
            "tool_finished",
            {
                "toolCallId": "tool:secret",
                "toolName": "memory",
                "args": {"apiKey": "never-copy-this"},
                "result": {
                    "details": {
                        "result": {
                            "summary": "查询到一份可公开摘要",
                            "items": [
                                {"kind": "book", "title": "输入法项目"},
                                {"kind": "tag", "title": "Pi"},
                            ],
                        }
                    }
                },
                "isError": False,
            },
            turn_id="turn:safe",
        )
        self.assertTrue(self.service.events.flush())
        projected = self.service.rooms.list_events(str(room["id"]))[-1]
        data = projected["payload"]["data"]
        serialized = json.dumps(projected, ensure_ascii=False)
        self.assertEqual(projected["eventType"], "participant_activity")
        self.assertNotIn("arguments", data)
        self.assertEqual(
            data["summary"],
            "查询到一份可公开摘要",
        )
        self.assertEqual(data["books"], ["输入法项目"])
        self.assertEqual(data["tags"], ["Pi"])
        self.assertNotIn("result", data)
        self.assertNotIn("never-copy-this", serialized)

        self.service.events.publish(
            session_id,
            "message_completed",
            {
                "message": {
                    "schemaVersion": "rag-ime.agent-message.v1",
                    "id": "message:public",
                    "sessionId": session_id,
                    "turnId": "turn:safe",
                    "role": "assistant",
                    "status": "completed",
                    "blocks": [
                        {
                            "id": "text:1",
                            "type": "text",
                            "status": "completed",
                            "presentationKind": "markdown",
                            "data": {"text": "这是可以进入群聊的回答"},
                        },
                        {
                            "id": "tool:1",
                            "type": "tool_call",
                            "status": "completed",
                            "presentationKind": "tool_call",
                            "data": {"arguments": {"token": "private-token"}},
                        },
                    ],
                    "attachments": [],
                    "citations": [],
                    "createdAtMs": 10,
                    "completedAtMs": 11,
                }
            },
            turn_id="turn:safe",
        )
        self.assertTrue(self.service.events.flush())
        public_message = self.service.rooms.list_events(str(room["id"]))[-1]
        serialized = json.dumps(public_message, ensure_ascii=False)
        self.assertEqual(public_message["eventType"], "participant_message")
        self.assertIn("这是可以进入群聊的回答", serialized)
        self.assertNotIn("private-token", serialized)
        self.assertNotIn("tool_call", serialized)

        self.service.events.publish(
            session_id,
            "message_completed",
            {
                "message": {
                    "schemaVersion": "rag-ime.agent-message.v1",
                    "id": "message:tool-only",
                    "sessionId": session_id,
                    "turnId": "turn:safe",
                    "role": "tool",
                    "status": "completed",
                    "blocks": [
                        {
                            "id": "tool-result:1",
                            "type": "tool_result",
                            "status": "completed",
                            "presentationKind": "tool_result",
                            "data": {"secret": "tool-result-secret"},
                        }
                    ],
                    "attachments": [],
                    "citations": [],
                    "createdAtMs": 12,
                    "completedAtMs": 13,
                }
            },
            turn_id="turn:safe",
        )
        self.assertTrue(self.service.events.flush())
        hidden_tool_message = self.service.rooms.list_events(str(room["id"]))[-1]
        serialized = json.dumps(hidden_tool_message, ensure_ascii=False)
        self.assertEqual(hidden_tool_message["eventType"], "participant_activity")
        self.assertIn("内部工具步骤", serialized)
        self.assertNotIn("tool-result-secret", serialized)

    def test_roleplay_room_uses_invites_scenario_and_topic_scoped_recent_context(self) -> None:
        created = self.service.create_room(
            {
                "title": "深夜茶话会",
                "roomKind": "roleplay",
                "description": "两个角色聊近况",
                "scenarioPrompt": "场景在安静的茶室，交流要克制自然。",
                "routingPolicy": "invite_only",
                "workspaceRoots": [],
                "participants": [
                    {"roleId": "companion-present-v1", "roleVersion": "1"},
                    {"roleId": "companion-future-v1", "roleVersion": "1"},
                ],
            }
        )
        room = created["room"]
        self.assertEqual(room["roomKind"], "roleplay")
        self.assertEqual(room["workspaceRoots"], [])
        self.assertEqual(room["executionMode"], "per_action")
        self.assertTrue(all(
            self.service.sessions.get(str(item["sessionId"]))["mode"] == "assistant"
            for item in room["participants"]
        ))
        self.assertTrue(all(
            self.service.sessions.get(str(item["sessionId"]))["executionMode"]
            == "per_action"
            for item in room["participants"]
        ))
        for participant in room["participants"]:
            session = self.service.sessions.get(str(participant["sessionId"]))
            self.assertEqual(session["toolProfileVersion"], "control-center-v1")
            self.assertFalse(session["projectContextEnabled"])
            self.assertFalse(session["piSkillsEnabled"])
            self.assertFalse(session["codexSkillsEnabled"])
        with self.assertRaisesRegex(
            ValueError,
            "roleplay Rooms cannot create managed work",
        ):
            self.service.create_room_work_item(
                str(room["id"]),
                {
                    "objective": "不应进入受管执行",
                    "expectedOutput": "无",
                    "currentOwnerParticipantId": str(room["participants"][0]["id"]),
                    "clientMessageId": "roleplay-work-item-rejected",
                    "acceptanceCriteria": ["角色群聊不得创建受管任务"],
                },
            )
        with self.assertRaisesRegex(ValueError, "roleplay Rooms cannot use"):
            self.service.create_room(
                {
                    "title": "不允许托管的角色群聊",
                    "roomKind": "roleplay",
                    "executionMode": "workspace_managed",
                    "workspaceRoots": [],
                    "participants": [
                        {"roleId": "companion-present-v1", "roleVersion": "1"},
                        {"roleId": "companion-future-v1", "roleVersion": "1"},
                    ],
                }
            )
        current, future = room["participants"]
        with patch.object(self.service, "prompt", return_value={"turnId": "turn:first"}):
            first = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "今天有点累",
                    "participantIds": [str(current["id"])],
                },
            )
        self.assertEqual(first["routeDecision"]["reason"], "explicit_invite")
        self.service.rooms.append_event(
            room_id=str(room["id"]),
            event_type="participant_message",
            payload={
                "data": {
                    "message": {
                        "role": "assistant",
                        "blocks": [
                            {
                                "type": "text",
                                "data": {"text": "先坐一会儿，慢慢说。"},
                            }
                        ],
                    }
                }
            },
            participant_id=str(current["id"]),
            topic_id=str(room["activeTopicId"]),
        )
        with patch.object(self.service, "prompt", return_value={"turnId": "turn:second"}) as prompt:
            second = self.service.post_room_message(
                str(room["id"]),
                {
                    "message": "你怎么看？",
                    "participantIds": [str(future["id"])],
                },
            )
        self.assertEqual(second["participant"]["id"], future["id"])
        prompt_payload = prompt.call_args.args[1]
        self.assertEqual(prompt_payload["message"], "你怎么看？")
        materialized = prompt_payload["_transientContext"]
        self.assertIn("安静的茶室", materialized)
        self.assertIn("今天有点累", materialized)
        self.assertIn("先坐一会儿，慢慢说", materialized)
        self.assertNotIn("受管 Room 上下文", materialized)
        self.assertNotIn("[此刻的发言]", materialized)


if __name__ == "__main__":
    unittest.main()
