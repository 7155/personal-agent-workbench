from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rag_ime.agent_background_jobs import AgentBackgroundJobService
from rag_ime.agent_context_runtime import AgentContextRuntime
from rag_ime.agent_execution_policy import ROOM_UNRESTRICTED_EXECUTION_MODE
from rag_ime.agent_media import AgentMediaStore
from rag_ime.agent_memory_sources import AgentMemorySourceStore
from rag_ime.agent_sessions import AgentSessionStore
from rag_ime.agent_tool_artifacts import AgentToolArtifactProjector
from rag_ime.agent_tools import (
    ControlToolGateway,
    _RUNTIME_TOOL_PROJECTIONS,
    _TOOL_SPECS,
    _runtime_tool_parameter_schema,
)
from rag_ime.agent_workspace import WorkspaceHarness, WorkspaceHarnessError
from rag_ime.contracts.json_schema import validate_contract
from rag_ime.work_documents import WorkDocumentService


class _Management:
    def __init__(self):
        self.task_events = {}
        self.runtime_jobs = {}
        self.memory_requests = []
        self.ai_paused = False
        self.runtime_revision = 1
        self.provider_configuration_revision = 1
        self.provider_profiles = {
            "instant": {
                "provider": "mlx",
                "endpoint": "http://127.0.0.1:8767",
                "model": "",
            },
            "knowledge": {
                "provider": "deepseek",
                "endpoint": "https://api.deepseek.com",
                "model": "deepseek-v4-flash",
            },
            "voice": {"provider": "native_streaming"},
        }
        self.tasks = [
            {
                "id": "task:1",
                "date": "2026-07-13",
                "title": "接入 Pi",
                "status": "todo",
                "project": "wisdom-weasel-rag-ime",
                "updatedAtMs": 20,
            },
            {
                "id": "task:2",
                "date": "2026-07-13",
                "title": "协议测试",
                "status": "done",
                "project": "wisdom-weasel-rag-ime",
                "updatedAtMs": 19,
            },
        ]

    def overview(self):
        return {
            "settingsRevision": "settings:1",
            "runtimeRevision": self.runtime_revision,
            "aiPaused": self.ai_paused,
            "components": {"sidecar": {"ok": True}, "predictor": {"ok": False}},
            "memory": {"memoryBookCount": 2},
            "lastPrediction": {"visibleCandidate": "继续实现"},
        }

    def runtime_status(self):
        return {
            "ok": True,
            "runtimeRevision": self.runtime_revision,
            "components": {"sidecar": {"ok": True}, "predictor": {"ok": False}},
        }

    def runtime_components(self):
        return {"ok": True, "items": [{"id": "sidecar", "ok": True}]}

    def start_runtime_action(self, payload):
        if payload["action"] == "restart_sidecar":
            self.runtime_jobs["runtime:1"] = {
                "jobId": "runtime:1",
                "action": payload["action"],
                "status": "external-supervisor-required",
                "error": "external supervisor required",
                "result": {
                    "externalCommand": [
                        "launchctl",
                        "kickstart",
                        "-k",
                        f"gui/{os.getuid()}/com.rag-ime.sidecar",
                    ]
                },
            }
            return {"ok": True, "jobId": "runtime:1", "job": self.runtime_jobs["runtime:1"]}
        if payload["action"] == "stop_ai":
            self.ai_paused = True
        elif payload["action"] == "resume_ai":
            self.ai_paused = False
        self.runtime_revision += 1
        self.runtime_jobs["runtime:1"] = {
            "jobId": "runtime:1",
            "action": payload["action"],
            "status": "succeeded",
            "error": "",
        }
        return {"ok": True, "jobId": "runtime:1", "job": self.runtime_jobs["runtime:1"]}

    def runtime_job(self, job_id):
        return {"ok": True, "jobId": job_id, "job": self.runtime_jobs[job_id]}

    def history_page(self, request):
        return {
            "items": [{"id": 1, "textPreview": "把普通生成和深度检索分开", "rawTextVisible": False}][
                : request.limit
            ],
            "nextCursor": "",
        }

    def portable_backup_export(self, payload):
        target = Path(payload["destination"])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"portable-backup-fixture")
        return {
            "ok": True,
            "path": str(target),
            "sizeBytes": target.stat().st_size,
            "databaseCounts": {"memory_books": 2, "agent_sessions": 1},
            "rimeFileCount": 3,
            "secretsIncluded": False,
            "auditId": 61,
        }

    def portable_restore_preview(self, payload):
        source = Path(payload["path"])
        if not source.is_file():
            raise ValueError("backup not found")
        return {
            "ok": True,
            "valid": True,
            "path": str(source),
            "restoreToken": "7" * 64,
            "createdAtMs": 1_700_000_000_000,
            "databaseCounts": {"memory_books": 2, "agent_sessions": 1},
            "databaseMigrationVersion": 18,
            "rimeFileCount": 3,
            "requiresConfirmation": "RESTORE RAG-IME",
            "requiresRestart": True,
        }

    def provider_configuration(self):
        return {
            "ok": True,
            "configurationHash": f"provider:{self.provider_configuration_revision}",
            "settingsRevision": "settings:1",
            "runtimeRevision": self.runtime_revision,
            "providers": {
                **{slot: dict(profile) for slot, profile in self.provider_profiles.items()},
                "secretsIncluded": False,
            },
        }

    def provider_configuration_apply(self, payload):
        if payload["expectedConfigurationHash"] != f"provider:{self.provider_configuration_revision}":
            raise ValueError("provider configuration changed after the approval preview was created")
        slot = payload["slot"]
        before = dict(self.provider_profiles[slot])
        after = {"provider": payload["provider"]}
        if slot != "voice":
            after.update({"endpoint": payload["endpoint"], "model": payload["model"]})
        self.provider_profiles[slot] = after
        self.provider_configuration_revision += 1
        self.runtime_revision += 1
        return {
            "ok": True,
            "auditId": 62,
            "slot": slot,
            "before": before,
            "after": dict(after),
            "configurationHash": f"provider:{self.provider_configuration_revision}",
            "settingsRevision": "settings:1",
            "runtimeRevision": self.runtime_revision,
            "restartComponent": "predictor" if slot == "instant" else "sidecar",
            "existingSecretPreserved": True,
        }

    def planning_dashboard(self, *, plan_date, project):
        return {
            "date": plan_date or "2026-07-13",
            "project": project,
            "tasks": [dict(task) for task in self.tasks if task["date"] == (plan_date or "2026-07-13")],
            "goals": [],
        }

    def planning_task_action(self, payload):
        target = {
            "complete": "done",
            "start": "in_progress",
            "reopen": "todo",
            "cancel": "cancelled",
        }[payload["action"]]
        task = next(item for item in self.tasks if item["id"] == payload["taskId"])
        previous = task["status"]
        task["status"] = target
        task["updatedAtMs"] += 1
        event_id = f"task-event:{len(self.task_events) + 1}"
        self.task_events[event_id] = {"taskId": task["id"], "previous": previous}
        return {
            "ok": True,
            "auditId": 42,
            "eventId": event_id,
            "task": dict(task),
            "undoAvailable": True,
        }

    def planning_undo_task_event(self, payload):
        event = self.task_events[payload["eventId"]]
        task = next(item for item in self.tasks if item["id"] == event["taskId"])
        task["status"] = event["previous"]
        task["updatedAtMs"] += 1
        return {"ok": True, "auditId": 43, "task": dict(task)}

    def memory_page(self, kind, request):
        self.memory_requests.append((kind, request))
        if kind == "apps":
            return {
                "items": [
                    {
                        "id": "com.openai.codex",
                        "title": "Codex",
                        "eventCount": 12,
                        "atomCount": 3,
                        "bookCount": 1,
                    }
                ],
                "nextCursor": "",
            }
        if kind == "books":
            return {
                "items": [
                    {
                        "id": "book:1",
                        "title": "输入法项目",
                        "summary": "记录输入法 Agent 与 RAG 的设计决定",
                        "tags": ["输入法", "Agent"],
                        "atomCount": 2,
                        "updated_at_ms": 20,
                        "sourceStartMs": 10,
                        "sourceEndMs": 20,
                        "memories": [
                            {
                                "id": "atom:decision:1",
                                "type": "decision",
                                "text": "普通生成不经过 Pi",
                                "updatedAtMs": 20,
                            }
                        ],
                        "ref": {
                            "kind": "book",
                            "id": "book:1",
                            "referenceKind": "book",
                            "referenceId": "book:1",
                        },
                        "evidenceRefs": [
                            {
                                "kind": "event",
                                "id": "7",
                                "referenceKind": "event",
                                "referenceId": "7",
                            }
                        ],
                    }
                ]
            }
        if kind == "groups":
            return {
                "items": [
                    {
                        "title": "Agent Runtime",
                        "note": "Pi RPC 和 Session",
                        "tags": ["Pi"],
                        "event_count": 4,
                        "updated_at_ms": 19,
                    }
                ]
            }
        if kind == "tags":
            return {
                "items": [
                    {
                        "tag": "Pi",
                        "description": "Pi 运行时",
                        "item_count": 3,
                        "updated_at_ms": 18,
                    }
                ]
            }
        if kind == "evidence":
            if request.status != "remember":
                return {"items": []}
            return {
                "items": [
                    {
                        "id": "memory-source:7",
                        "type": "user_final",
                        "transportSource": "rime",
                        "source": {"kind": "input_event", "id": "7"},
                        "ref": {
                            "kind": "evidence",
                            "id": "memory-source:7",
                            "referenceKind": "evidence",
                            "referenceId": "memory-source:7",
                        },
                        "evidenceRefs": [
                            {
                                "kind": "event",
                                "id": "7",
                                "referenceKind": "event",
                                "referenceId": "7",
                            }
                        ],
                        "text": "把普通生成和深度检索分开",
                        "app": "Codex",
                        "project": "wisdom-weasel-rag-ime",
                        "ownerKind": "user",
                        "ownerId": "default",
                        "disposition": "remember",
                        "createdAtMs": 30,
                    },
                    {
                        "id": "memory-source:noise",
                        "type": "voice_final",
                        "source": "voice_streaming_asr",
                        "text": "嗯嗯那个这个",
                        "app": "Voice",
                        "project": "wisdom-weasel-rag-ime",
                        "ownerKind": "user",
                        "ownerId": "default",
                        "disposition": "not_for_memory",
                        "createdAtMs": 31,
                    },
                ]
            }
        raise AssertionError(kind)


class _Core:
    def list_memory_events(self, *, project, query, limit):
        return {
            "items": [
                {
                    "eventId": 7,
                    "createdAtMs": 30,
                    "source": "rime",
                    "text": "把普通生成和深度检索分开",
                    "app": "Codex",
                    "project": project,
                    "tags": ["Agent"],
                }
            ][:limit]
        }

    def retrieve_memories(self, *, current_input, project, top_k):
        return [
            SimpleNamespace(
                memory_id="stable:1",
                text="Pi 只负责 Agent Loop",
                source_ref="event:7",
                score=0.91,
                reason="hybrid retrieval",
                evidence_preview="输入法快速链路不经过 Pi",
                project=project,
                tags=("Pi",),
                source_event_id="7",
                created_at_ms=30,
            )
        ][:top_k]

    def suggestion_cache_stats(self):
        return {"hits": 3, "misses": 1}


class _KnowledgeClient:
    def __init__(self):
        self.calls = []

    def list_bases(self, payload):
        self.calls.append(("list_bases", dict(payload)))
        return {
            "items": [
                {
                    "kbId": "kb:project-docs",
                    "name": "项目资料",
                    "agentEnabled": True,
                    "indexedDocumentCount": 2,
                }
            ]
        }

    def search(self, payload):
        self.calls.append(("search", dict(payload)))
        return {
            "schemaVersion": "rag-ime.document-knowledge-search.v1",
            "kbId": payload["kbId"],
            "query": payload["query"],
            "items": [
                {
                    "chunkId": "chunk:1",
                    "fileId": "file:1",
                    "fileName": "architecture.md",
                    "content": "Pi 只负责 Agent Loop；输入法热路径保持独立。",
                    "sourcePath": "/private/project/architecture.md",
                    "citation": {"page": None, "startLine": 41, "endLine": 42},
                }
            ],
        }

    def find(self, payload):
        self.calls.append(("find", dict(payload)))
        return {"items": [{"line": 41, "content": "输入法热路径保持独立。"}]}

    def open(self, payload):
        self.calls.append(("open", dict(payload)))
        return {
            "fileId": payload.get("fileId", "file:1"),
            "chunkId": payload.get("chunkId"),
            "startLine": 41,
            "content": "引用窗口",
        }

    def status(self, payload):
        self.calls.append(("status", dict(payload)))
        return {"available": True, "state": "ready", "workerUrl": "http://127.0.0.1:8769"}


class _Facade:
    def __init__(self):
        self.memory_run_status = "draft"
        self.memory_prepare_no_run = False
        self.memory_prepare_pending_count = 0
        self.memory_prepare_batch_count = 1
        self.memory_prepare_drain_limited = False
        self.memory_maintenance_requests = []
        self.memory_run_owner = ("user", "default")
        self.settings_revision = 1
        self.settings_payload = {
            "interaction": {"postCommit": {"enabled": True, "idleTriggerMs": 420}},
            "display": {"fadeAnimation": True},
            "activeRag": {"enabled": True, "localOnlyDefault": True},
            "pinyin": {"fuzzyProfile": "sichuan-mild", "pairs": {"nL": False}},
            "privacy": {"allowRemoteModelForActiveRag": False},
            "providers": {"knowledge": {"apiKey": "must-not-leak"}},
        }
        self.lexicon_applied = False

    def settings(self):
        return {
            "settings": self.settings_payload,
            "settingsHash": f"settings:{self.settings_revision}",
            "runtimeConfig": {"profile": "default"},
        }

    def settings_update(self, payload):
        changed = []
        for key, value in payload.items():
            if key == "updatedBy":
                continue
            cursor = self.settings_payload
            parts = key.split(".")
            for part in parts[:-1]:
                cursor = cursor.setdefault(part, {})
            if cursor.get(parts[-1]) != value:
                cursor[parts[-1]] = value
                changed.append(key)
        self.settings_revision += 1
        return {
            "ok": True,
            "auditId": 51,
            "changedKeys": sorted(changed),
            "settingsRevision": f"settings:{self.settings_revision}",
            "runtimeRevision": self.settings_revision,
        }

    def frontend_capabilities(self):
        return {"frontend": "squirrel", "postCommit": True}

    def input_source_status(self):
        return {"ok": True, "selected": True}

    def candidate_explain(self, payload):
        return {"ok": True, "queryPreview": payload["query"], "candidates": [{"text": "继续"}]}

    def rime_lexicon_review(self, payload):
        entries = [] if self.lexicon_applied else [
            {
                "text": "派会话",
                "pinyin": "pai hui hua",
                "reviewKey": "派会话\tpai hui hua",
                "selected": True,
            }
        ]
        return {
            "ok": True,
            "reviewToken": "must-not-reach-pi",
            "confirmText": "must-not-reach-pi",
            "entries": entries,
        }

    def rime_lexicon_apply(self, payload):
        if payload["reviewToken"] != "must-not-reach-pi":
            return {"ok": False, "reason": "review_token_stale"}
        self.lexicon_applied = True
        return {
            "ok": True,
            "applied": True,
            "entryCount": len(payload["selectedKeys"]),
            "rollbackId": "lexicon:1",
            "requiresRedeploy": True,
        }

    def rime_lexicon_rollback(self, payload):
        if payload["rollbackId"] != "lexicon:1":
            return {"ok": False, "reason": "rollback_manifest_missing"}
        self.lexicon_applied = False
        return {"ok": True, "rolledBack": True, "rollbackId": "lexicon:1", "requiresRedeploy": True}

    def memory_optimizer_trace(self, payload):
        return {"ok": True, "traceId": payload["traceId"], "decision": "keep"}

    def agent_memory_maintenance_status(self, payload):
        self.memory_maintenance_requests.append(("status", dict(payload)))
        return {
            "schemaVersion": "rag-ime.agent-memory-maintenance-status.v1",
            "ok": True,
            "policy": "review",
            "autoApply": False,
            "compileState": {"pendingEventCount": 7},
            "pendingDraftCount": 1,
            "runs": [{"runId": "memory_book_draft", "status": "draft", "diffCount": 3}],
            "limit": payload["limit"],
        }

    def agent_memory_maintenance_prepare(self, payload):
        self.memory_maintenance_requests.append(("prepare", dict(payload)))
        if self.memory_prepare_no_run:
            return {
                "ok": True,
                "storedDraft": False,
                "reusedDraft": False,
                "source": {
                    "ownerKind": payload["ownerKind"],
                    "ownerId": payload["ownerId"],
                    "pendingSourceCount": self.memory_prepare_pending_count,
                    "batchCount": self.memory_prepare_batch_count,
                    "drainLimited": self.memory_prepare_drain_limited,
                },
                "validation": {"ok": True, "errors": []},
                "storedRun": {},
                "curation": {
                    "results": [
                        {
                            "ownerKind": payload["ownerKind"],
                            "ownerId": payload["ownerId"],
                            "skipped": True,
                            "reason": "no_sources",
                        }
                    ]
                },
            }
        self.memory_run_owner = (payload["ownerKind"], payload["ownerId"])
        return {
            "ok": True,
            "storedDraft": True,
            "reusedDraft": False,
            "source": {
                "bundleHash": "sha256:bundle",
                "eventCount": 7,
                "pendingSourceCount": self.memory_prepare_pending_count,
                "batchCount": self.memory_prepare_batch_count,
                "drainLimited": self.memory_prepare_drain_limited,
            },
            "validation": {"ok": True, "errors": []},
            "storedRun": {
                "runId": "memory_book_draft",
                "status": "draft",
                "ownerKind": payload["ownerKind"],
                "ownerId": payload["ownerId"],
            },
            "instruction": payload["instruction"],
        }

    def agent_memory_maintenance_run(self, payload):
        status = self.memory_run_status
        diff_count = 0 if status == "empty" else 3
        return {
            "ok": True,
            "revisionHash": f"sha256:{status}",
            "stale": False,
            "canApply": status == "draft",
            "canRollback": status == "applied",
            "run": {
                "runId": payload["runId"],
                "status": status,
                "summary": "Pi 记忆整理草案",
                "ownerKind": self.memory_run_owner[0],
                "ownerId": self.memory_run_owner[1],
                "runKind": "manual_curation",
                "bundleHash": "sha256:bundle",
                "sourceCursor": {"fromEventId": 10, "toEventId": 16},
                "diffCount": diff_count,
                "pendingDiffCount": diff_count if status == "draft" else 0,
                "appliedDiffCount": diff_count if status == "applied" else 0,
                "operationCounts": (
                    {
                        "upsert_memory_book": 1,
                        "upsert_memory_atom": 2,
                    }
                    if diff_count
                    else {}
                ),
                "changes": [
                    {
                        "diffId": 1,
                        "operation": "upsert_memory_book",
                        "operationLabel": "更新工具书",
                        "status": "pending" if status == "draft" else status,
                        "selected": True,
                        "title": "Pi 控制中心",
                        "detail": "记录审批和记忆整理边界",
                        "sourceCount": 3,
                    }
                ],
            },
        }

    def knowledge_workbench_database_apply(self, payload):
        self.memory_run_status = "applied"
        return {"ok": True, "action": "apply", "run": {"runId": payload["runId"]}}

    def knowledge_workbench_database_rollback(self, payload):
        self.memory_run_status = "rolled_back"
        return {"ok": True, "action": "rollback", "run": {"runId": payload["runId"]}}

    def knowledge_workbench_route_status(self):
        return {"ok": True, "deepseekReady": False, "skipReason": "credentials_missing"}

    def models_status(self):
        return {
            "ok": True,
            "predictor": {"ok": True},
            "activeRagRoute": {"remoteReady": False, "skipReason": "credentials_missing"},
        }

    def model_probe(self, payload):
        return {"ok": True, "dryRun": True, "predictor": {"ok": True}}

    def management_audit(self, payload):
        return {"ok": True, "items": [{"action": "settings_update", "apiKey": "must-not-leak"}]}


class ControlToolGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-agent-tools-")
        self.previous_support_dir = os.environ.get("RAG_IME_APP_SUPPORT_DIR")
        os.environ["RAG_IME_APP_SUPPORT_DIR"] = str(Path(self.tmp.name) / "support")
        self.store = AgentSessionStore(Path(self.tmp.name) / "rag-ime.sqlite")
        self.store.initialize()
        self.session = self.store.create(title="tool test", created_at_ms=1)
        self.store.set_disclosure_preferences(
            str(self.session["id"]), {"tool:memory": "enabled"}, updated_at_ms=1,
        )
        self.management = _Management()
        self.facade = _Facade()
        self.knowledge = _KnowledgeClient()
        self.gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            knowledge_client=self.knowledge,
        )

    def tearDown(self) -> None:
        if self.previous_support_dir is None:
            os.environ.pop("RAG_IME_APP_SUPPORT_DIR", None)
        else:
            os.environ["RAG_IME_APP_SUPPORT_DIR"] = self.previous_support_dir
        self.tmp.cleanup()

    def test_catalog_exposes_named_books_groups_and_tags_without_database_access(self) -> None:
        response = self.gateway.execute(self._call("catalog", query="Pi"))
        result = response["result"]

        self.assertEqual(result["counts"], {"books": 1, "groups": 1, "tags": 1})
        self.assertIn("1 本工具书", result["summary"])
        self.assertEqual([item["kind"] for item in result["items"]], ["book", "group", "tag"])
        self.assertEqual(result["items"][0]["ref"]["referenceKind"], "book")
        self.assertEqual(result["items"][0]["ref"]["type"], "book")
        self.assertEqual(result["items"][0]["evidenceRefs"][0]["kind"], "event")
        self.assertNotIn("sourceEventIds", str(response))

    def test_read_and_recent_return_bounded_evidence(self) -> None:
        read = self.gateway.execute(self._call("read", bookId="book:1"))["result"]
        recent = self.gateway.execute(self._call("recent", query="深度检索", limit=8))["result"]

        self.assertEqual(read["book"]["title"], "输入法项目")
        self.assertEqual(read["book"]["memories"][0]["text"], "普通生成不经过 Pi")
        self.assertEqual(read["book"]["ref"]["referenceKind"], "book")
        self.assertEqual(read["book"]["memories"][0]["ref"]["referenceKind"], "atom")
        self.assertEqual(read["book"]["evidenceRefs"][0]["referenceKind"], "event")
        self.assertEqual(recent["count"], 1)
        self.assertEqual(recent["items"][0]["text"], "把普通生成和深度检索分开")
        self.assertEqual(recent["items"][0]["disposition"], "remember")
        self.assertEqual(recent["items"][0]["source"], "rime")
        self.assertEqual(recent["items"][0]["sourceRef"]["kind"], "input_event")
        self.assertEqual(recent["items"][0]["ref"]["referenceKind"], "evidence")
        self.assertEqual(recent["items"][0]["evidenceRefs"][0]["referenceKind"], "event")
        self.assertNotIn("嗯嗯那个这个", str(recent))
        evidence_requests = [
            request
            for kind, request in self.management.memory_requests
            if kind == "evidence"
        ]
        self.assertEqual(
            [request.status for request in evidence_requests],
            ["remember", "consolidated"],
        )
        self.assertTrue(all(request.visible_owners for request in evidence_requests))

    def test_memory_read_compatibility_normalizes_missing_operation_without_enabling_writes(self) -> None:
        recent = self.gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": self.session["id"],
                "tool": "memory",
                "toolCallId": "tool:legacy-recent",
                "args": {
                    "query": "RAG IME 昨天进展",
                    "scope": "recent",
                },
            }
        )["result"]

        self.assertEqual(recent["count"], 1)
        self.assertEqual(recent["items"][0]["text"], "把普通生成和深度检索分开")
        with self.assertRaisesRegex(ValueError, "unsupported memory operation"):
            self.gateway.execute(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": self.session["id"],
                    "tool": "memory",
                    "toolCallId": "tool:legacy-write",
                    "args": {
                        "text": "不能凭缺失 op 猜写操作",
                        "scope": "global",
                    },
                }
            )

    def test_memory_tools_include_the_active_room_owner_in_visibility(self) -> None:
        participant_calls = []

        class _Rooms:
            def participant_for_session(self, session_id):
                participant_calls.append(session_id)
                return {"roomId": "room:architecture"}

        class _Collaboration:
            rooms = _Rooms()

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            collaboration=_Collaboration(),
        )

        gateway.execute(self._call("recent", limit=3))

        evidence_requests = [
            request
            for kind, request in self.management.memory_requests
            if kind == "evidence"
        ]
        self.assertEqual(participant_calls, [self.session["id"]])
        self.assertEqual(len(evidence_requests), 2)
        self.assertTrue(
            all(
                ("room", "room:architecture") in request.visible_owners
                for request in evidence_requests
            )
        )
        self.assertTrue(
            all(
                ("session", self.session["id"]) in request.visible_owners
                for request in evidence_requests
            )
        )

    def test_memory_maintenance_status_exposes_review_only_drafts(self) -> None:
        result = self.gateway.execute(self._call("maintenance_status", limit=8))["result"]

        self.assertEqual(result["summary"], "有 7 条来源待整理、1 份草案待审阅")
        self.assertEqual(result["maintenance"]["policy"], "review")
        self.assertFalse(result["maintenance"]["autoApply"])
        self.assertEqual(result["maintenance"]["runs"][0]["status"], "draft")
        request = self.facade.memory_maintenance_requests[-1][1]
        self.assertEqual(
            (request["ownerKind"], request["ownerId"]),
            ("user", "default"),
        )

    def test_runtime_memory_tool_discloses_operation_specific_schema(self) -> None:
        manifests = self.gateway.runtime_manifests(self.session)
        memory = next(item for item in manifests if item["name"] == "memory")
        parameters = memory["parameters"]
        branches = parameters["oneOf"]
        by_operation = {
            branch["properties"]["op"]["const"]: branch
            for branch in branches
            if "op" in branch.get("properties", {})
        }

        self.assertIn("curation_prepare", by_operation)
        self.assertIn("capture", by_operation)
        self.assertEqual(
            memory["runtimeProjections"],
            [{"name": "memory_capture", "operation": "capture"}],
        )
        self.assertEqual(
            by_operation["capture"]["required"],
            [
                "op",
                "kind",
                "claim",
                "captureScope",
                "basis",
                "futureUse",
            ],
        )
        self.assertEqual(
            parameters["properties"]["basis"]["enum"],
            [
                "explicit_user_request",
                "explicit_user_statement",
                "user_correction",
                "repeated_user_signal",
                "verified_outcome",
            ],
        )
        self.assertEqual(parameters["properties"]["futureUse"]["maxLength"], 300)
        self.assertIn(
            "至少两条独立用户表达",
            parameters["properties"]["basis"]["description"],
        )
        self.assertIn(
            "已应用工具回执",
            parameters["properties"]["basis"]["description"],
        )
        self.assertIn(
            "若只影响当前任务或当前会话",
            parameters["properties"]["futureUse"]["description"],
        )
        self.assertIn("一轮最多三条", parameters["description"])
        self.assertIn("Room 私有过程", parameters["description"])
        self.assertIn("失败不循环重试", parameters["description"])
        self.assertEqual(
            by_operation["curation_prepare"]["required"],
            ["op", "trigger"],
        )
        self.assertEqual(
            parameters["properties"]["trigger"]["enum"],
            ["task_completion", "explicit_request", "idle_batch"],
        )
        self.assertEqual(
            parameters["properties"]["scope"]["enum"],
            [
                "incremental",
                "global",
                "recent",
                "current",
                "historical",
                "change",
            ],
        )
        compatibility = next(
            branch
            for branch in branches
            if "op" not in branch.get("properties", {})
        )
        self.assertEqual(compatibility["required"], ["query"])
        self.assertEqual(compatibility["not"], {"required": ["op"]})
        self.assertEqual(
            compatibility["properties"]["scope"]["enum"],
            ["recent", "current", "historical", "change"],
        )
        self.assertNotIn("。。", memory["description"])

        self.assertEqual(
            parameters["properties"]["policy"]["enum"],
            ["conservative"],
        )
        self.assertEqual(
            by_operation["maintenance_review"]["required"],
            ["op", "runId"],
        )
        self.assertEqual(
            by_operation["remember_preview"]["required"],
            ["op", "text"],
        )

        self.assertEqual(
            by_operation["correct_preview"]["required"],
            ["op", "targetId", "text"],
        )
        self.assertEqual(
            by_operation["forget_preview"]["required"],
            ["op", "targetId", "reason"],
        )
        self.assertEqual(
            by_operation["remember_apply"]["required"],
            ["op", "proposalId"],
        )
        self.assertCountEqual(
            by_operation["get"]["anyOf"],
            [{"required": ["targetId"]}, {"required": ["draftId"]}],
        )
        for field in (
            "targetId",
            "text",
            "reason",
            "memoryKind",
            "evidenceIds",
            "claimKey",
            "idempotencyKey",
            "proposalId",
            "draftId",
            "mode",
        ):
            self.assertIn(field, parameters["properties"])
        self.assertEqual(memory["description"], "个人上下文记忆")
        self.assertIn("工具回执", memory["notFor"][0])
        self.assertNotIn("changes", str(parameters))

        role_book = next(
            item for item in manifests if item["name"] == "agent_role_book"
        )
        self.assertIn("随 Session 固定版本注入系统提示词", role_book["does"])
        self.assertIn("待审草案", role_book["output"])

    def test_memory_master_switch_removes_runtime_operations_and_blocks_stale_calls(self) -> None:
        enabled = False
        self.gateway._memory_enabled_provider = lambda: enabled

        self.assertNotIn(
            "memory",
            {item["name"] for item in self.gateway.runtime_manifests(self.session)},
        )
        public_memory = next(
            item
            for item in self.gateway.manifests(session_id=str(self.session["id"]))["items"]
            if item["id"] == "memory"
        )
        self.assertFalse(public_memory["enabled"])
        self.assertEqual(public_memory["effectiveOperations"], [])
        with self.assertRaisesRegex(ValueError, "memory tool is disabled"):
            self.gateway.execute(self._call("recent", query="关闭时不可召回"))
        self.assertEqual(self.management.memory_requests, [])

        enabled = True
        self.assertIn(
            "memory",
            {item["name"] for item in self.gateway.runtime_manifests(self.session)},
        )
        restored = self.gateway.execute(self._call("recent", limit=1))["result"]
        self.assertEqual(restored["count"], 1)

    def test_session_memory_switch_blocks_stale_calls_and_restores(self) -> None:
        session_id = str(self.session["id"])
        self.store.set_disclosure_preferences(session_id, {"tool:memory": "disabled"})
        self.assertNotIn("memory", {item["name"] for item in self.gateway.runtime_manifests(self.session)})
        memory = next(item for item in self.gateway.manifests(session_id=session_id)["items"] if item["id"] == "memory")
        self.assertEqual(memory["disclosure"]["effective"], "disabled")
        self.assertEqual(memory["effectiveOperations"], [])
        with self.assertRaisesRegex(ValueError, "memory tool is disabled"):
            self.gateway.execute(self._call("recent", query="当前对话已关闭"))
        self.assertEqual(self.management.memory_requests, [])
        self.store.set_disclosure_preferences(session_id, {"tool:memory": "enabled"})
        restored = self.gateway.execute(self._call("recent", limit=1))["result"]
        self.assertEqual(restored["count"], 1)

    def test_overview_tool_describes_the_agent_product_before_input_sources(self) -> None:
        manifests = self.gateway.runtime_manifests(self.session)
        overview = next(item for item in manifests if item["name"] == "overview")

        self.assertEqual(overview["description"], "控制中心概览")
        self.assertNotIn("查看输入法、模型", overview["does"])
        self.assertIn("Agent、模型、记忆、输入", overview["output"])

    def test_session_search_exposes_navigation_anchors_without_transcripts(self) -> None:
        historical = self.store.create(title="Room Launch Digest", created_at_ms=20)
        self.store.set_status(
            str(historical["id"]),
            "idle",
            message_count=4,
            last_message_preview="已确认结构化终止合同",
            updated_at_ms=30,
        )
        self.store.record_runtime_event(
            event_id="event:room-contract",
            session_id=str(historical["id"]),
            turn_id="turn:room-contract",
            sequence=1,
            event_type="message_completed",
            created_at_ms=31,
            redacted_summary="Launch Digest 交付已确认",
        )

        manifests = self.gateway.runtime_manifests(self.session)
        manifest = next(item for item in manifests if item["name"] == "session_search")
        self.assertTrue(manifest["alwaysAvailable"])
        self.assertEqual(
            {
                branch["properties"]["op"]["const"]
                for branch in manifest["parameters"]["oneOf"]
            },
            {"search"},
        )

        response = self.gateway.execute(
            self._tool_call(
                "session_search",
                "search",
                query="Launch Digest",
                limit=5,
            )
        )
        result = response["result"]
        self.assertEqual(result["presentationKind"], "session_history")
        self.assertEqual(len(result["anchors"]), 1)
        self.assertEqual(result["anchors"][0]["sessionId"], historical["id"])
        self.assertEqual(result["anchors"][0]["turnId"], "turn:room-contract")
        self.assertIn("不返回原始 transcript", result["privacyBoundary"])
        self.assertNotIn("sessionFile", str(result))
        self.assertNotIn(str(Path(self.tmp.name)), str(result))

    def test_trace_diagnostics_exposes_bounded_inspection_and_scoped_knowledge_read(self) -> None:
        class _TraceDiagnostics:
            def trace_diagnostic_inspection(self, payload):
                return {
                    "schemaVersion": "rag-ime.trace-diagnostic-inspection.v1",
                    "targets": payload["targets"],
                    "traceIds": ["trace:a", "trace:b"],
                    "scorecard": {"rubricVersion": "trace-score-v1"},
                }

        self.gateway.trace_diagnostics = _TraceDiagnostics()
        manifests = self.gateway.runtime_manifests(self.session)
        manifest = next(item for item in manifests if item["name"] == "trace_diagnostics")
        self.assertEqual(
            {
                branch["properties"]["op"]["const"]
                for branch in manifest["parameters"]["oneOf"]
            },
            {"inspect", "read", "command"},
        )

        result = self.gateway.execute(
            self._tool_call(
                "trace_diagnostics",
                "inspect",
                targets=[
                    {"kind": "session", "id": "session:a", "title": "A"},
                    {"kind": "session", "id": "session:b", "title": "B"},
                ],
            )
        )["result"]
        self.assertEqual(result["traceIds"], ["trace:a", "trace:b"])
        self.assertEqual(len(result["targets"]), 2)

    def test_trace_diagnostics_rejects_invalid_nested_target_instead_of_filtering_it(self) -> None:
        class _TraceDiagnostics:
            def trace_diagnostic_inspection(self, payload):
                return payload

        self.gateway.trace_diagnostics = _TraceDiagnostics()
        with self.assertRaisesRegex(ValueError, "targets.*objects"):
            self.gateway.execute(
                self._tool_call(
                    "trace_diagnostics",
                    "inspect",
                    targets=[
                        {"kind": "session", "id": "session:a", "title": "A"},
                        "not-a-target",
                    ],
                )
            )

    def test_dynamic_structured_output_manifest_terminates_the_child_turn(self) -> None:
        class _Delegation:
            submitted: tuple[str, dict[str, object], str] | None = None

            def structured_output_manifest(self, session_id):
                return {
                    "name": "structured_output",
                    "description": "提交结构化交付",
                    "parameters": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["value"],
                        "properties": {
                            "value": {
                                "type": "object",
                                "required": ["summary"],
                                "properties": {"summary": {"type": "string"}},
                            }
                        },
                    },
                    "alwaysAvailable": True,
                }

            def submit_structured_output(self, session_id, args, *, tool_call_id):
                self.submitted = (session_id, dict(args), tool_call_id)
                return {
                    "schemaVersion": "rag-ime.agent-subagent-structured-output.v1",
                    "summary": "交付合同有效",
                    "runId": "subagent-run:1",
                    "nodeId": "subagent-node:1",
                    "attemptId": "subagent-attempt:1",
                    "contractStatus": "valid",
                    "structuredOutput": dict(args["value"]),
                    "terminate": True,
                }

        delegation = _Delegation()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            knowledge_client=self.knowledge,
            delegation=delegation,
        )
        manifests = gateway.runtime_manifests(self.session)
        structured = next(item for item in manifests if item["name"] == "structured_output")
        self.assertTrue(structured["alwaysAvailable"])
        self.assertEqual(
            structured["parameters"]["properties"]["value"]["required"],
            ["summary"],
        )

        response = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": self.session["id"],
                "tool": "structured_output",
                "toolCallId": "tool:structured-final",
                "args": {"value": {"summary": "done"}},
            }
        )
        self.assertTrue(response["result"]["terminate"])
        self.assertEqual(
            delegation.submitted,
            (
                self.session["id"],
                {"value": {"summary": "done"}},
                "tool:structured-final",
            ),
        )

    def test_native_bash_guidance_routes_edits_and_room_waits_to_their_owners(
        self,
    ) -> None:
        extension = (
            Path(__file__).parents[1] / "integrations" / "pi" / "rag-ime-control.ts"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "Use edit, write, or workspace_patch for file changes; do not wrap edits in shell commands.",
            extension,
        )
        self.assertIn(
            "Do not use sleep or polling to wait for another Session or Room participant; use Agent events and status instead.",
            extension,
        )

    def test_workspace_root_path_accepts_an_omitted_or_empty_value(self) -> None:
        (Path(self.tmp.name) / "root-contract.txt").write_text(
            "root-contract-marker\n",
            encoding="utf-8",
        )
        coordinator = self.store.create(
            title="workspace root schema",
            mode="coordinator",
            workspace_roots=[self.tmp.name],
            created_at_ms=2,
        )
        for path_value in (None, ""):
            path_args = {} if path_value is None else {"path": path_value}
            listed = self.gateway.execute(
                {
                    **self._tool_call("workspace_list", "list", **path_args),
                    "sessionId": coordinator["id"],
                }
            )["result"]
            searched = self.gateway.execute(
                {
                    **self._tool_call(
                        "workspace_search",
                        "search",
                        query="root-contract-marker",
                        **path_args,
                    ),
                    "sessionId": coordinator["id"],
                }
            )["result"]

            self.assertEqual(listed["items"][0]["path"], str(Path(self.tmp.name).resolve()))
            self.assertEqual(searched["matches"][0]["preview"], "root-contract-marker")

    def test_workspace_gateway_targets_are_hidden_behind_native_runtime_tools(
        self,
    ) -> None:
        coordinator = self.store.create(
            title="native workspace projections",
            mode="coordinator",
            workspace_roots=[self.tmp.name],
            created_at_ms=2,
        )
        manifests = {
            item["name"]: item
            for item in self.gateway.runtime_manifests(coordinator)
        }

        projections = {
            "workspace_list": [{"name": "ls", "operation": "list"}],
            "workspace_read": [{"name": "read", "operation": "read"}],
            "workspace_search": [
                {"name": "grep", "operation": "search"},
                {"name": "find", "operation": "search"},
            ],
            "workspace_edit": [{"name": "edit", "operation": "apply"}],
            "workspace_write": [{"name": "write", "operation": "apply"}],
            "workspace_shell": [{"name": "bash", "operation": "run"}],
        }
        for target, expected_projections in projections.items():
            with self.subTest(target=target):
                self.assertIn(target, manifests)
                self.assertIs(manifests[target]["modelVisible"], False)
                self.assertEqual(
                    manifests[target]["runtimeProjections"],
                    expected_projections,
                )
        self.assertIn(
            "workDocument",
            _RUNTIME_TOOL_PROJECTIONS["workspace_write"][0]["parameters"][
                "properties"
            ],
        )
        for reserved_name in (
            "ls",
            "read",
            "grep",
            "find",
            "edit",
            "write",
            "bash",
        ):
            with self.subTest(reserved_name=reserved_name):
                self.assertNotIn(reserved_name, manifests)
        for target in (
            "workspace_list",
            "workspace_read",
            "workspace_search",
            "workspace_edit",
            "workspace_write",
            "workspace_shell",
        ):
            with self.subTest(target=target):
                self.assertIn(target, manifests)
        # These have no differently named always-on native alias. They remain
        # progressive Tools and are disclosed only when selected.
        self.assertIn("workspace_patch", manifests)
        self.assertIn("workspace_lsp", manifests)
        for target in ("workspace_edit", "workspace_write"):
            with self.subTest(contract_target=target):
                validate_contract(
                    {
                        "schemaVersion": "rag-ime.agent-tool-call.v1",
                        "sessionId": coordinator["id"],
                        "tool": target,
                        "toolCallId": f"tool:{target}",
                        "args": {},
                    },
                    "agent-tool-call.v1.json",
                )
                validate_contract(
                    {
                        "schemaVersion": "rag-ime.agent-tool-result.v1",
                        "ok": True,
                        "tool": target,
                        "operation": "apply",
                        "result": {},
                    },
                    "agent-tool-result.v1.json",
                )
        self.assertIs(manifests["workspace_patch"]["modelVisible"], False)
        self.assertNotIn("runtimeProjections", manifests["workspace_patch"])
        self.assertNotIn("modelVisible", manifests["workspace_lsp"])
        self.assertNotIn("runtimeProjections", manifests["workspace_lsp"])
        for operation in (
            "status",
            "symbols",
            "hover",
            "definition",
            "references",
            "diagnostics",
            "rename",
            "code_action_apply",
        ):
            validate_contract(
                {
                    "schemaVersion": "rag-ime.agent-tool-result.v1",
                    "ok": True,
                    "tool": "workspace_lsp",
                    "operation": operation,
                    "result": {},
                },
                "agent-tool-result.v1.json",
            )

    def test_room_facilitator_delegation_requires_active_goal_not_root_document(self) -> None:
        workspace = Path(self.tmp.name) / "room-root-document-gate"
        workspace.mkdir()
        facilitator = self.store.create(
            title="Room facilitator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=3,
        )
        session_id = str(facilitator["id"])
        goal_result = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "Build the Room result",
                "successCriteria": "Real delegated result is reviewed",
                "evidenceExpectations": ["real result evidence"],
            },
            actor="agent-runtime",
            updated_at_ms=4,
        )
        goal = goal_result["workflow"]["goal"]
        participant = {
            "id": "participant:root",
            "sessionId": session_id,
            "collaborationRole": "coordinator",
        }
        calls: list[dict[str, object]] = []
        collaboration = SimpleNamespace(
            rooms=SimpleNamespace(
                participant_for_session=lambda *_args, **_kwargs: participant,
            ),
            execute_room_partner_tool=lambda _session_id, args, **_kwargs: (
                calls.append(dict(args)) or {"operation": str(args["op"])}
            ),
        )
        documents: list[dict[str, object]] = []
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            collaboration=collaboration,
            work_documents=SimpleNamespace(
                list=lambda **_kwargs: {"items": list(documents)}
            ),
        )
        request = {
            **self._tool_call(
                "room_partner",
                "delegate",
                targetParticipantId="participant:worker",
                task="Implement one bounded lane",
                expectedOutput="Evidence",
                acceptanceCriteria=["Provide a receipt"],
            ),
            "sessionId": session_id,
        }

        self.assertEqual(
            gateway.execute(request)["result"]["operation"],
            "delegate",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(documents, [])

    def test_room_partner_catalog_describes_dynamic_fanout_up_to_capacity(self) -> None:
        room_partner = next(
            spec for spec in _TOOL_SPECS if spec["id"] == "room_partner"
        )
        when = " ".join(str(item) for item in room_partner["when"])

        self.assertIn("按任务规模动态选择", when)
        self.assertIn("最多 7 个可见 Partner", when)
        self.assertIn("Room 总参与者最多 8 个", when)
        self.assertNotIn("2–3 个", when)
        self.assertIn("active 且带 reviewFeedback", str(room_partner["description"]))
        self.assertIn("recoverableWorkItems", str(room_partner["description"]))
        self.assertIn("expectedRevision", str(room_partner["description"]))

        schema = _runtime_tool_parameter_schema(
            "room_partner",
            list(room_partner["operations"]),
        )
        self.assertIn("退回后仍为 active", schema["properties"]["op"]["description"])
        self.assertIn(
            "recoverableWorkItems",
            schema["properties"]["op"]["description"],
        )

    def test_room_partner_contract_routes_through_the_room_gateway(self) -> None:
        calls = []

        def execute_room_partner_tool(
            session_id,
            args,
            *,
            tool_call_id,
            source_loop_id="",
        ):
            calls.append(
                {
                    "sessionId": session_id,
                    "args": args,
                    "toolCallId": tool_call_id,
                    "sourceLoopId": source_loop_id,
                }
            )
            return {"operation": "list", "partners": []}

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            knowledge_client=self.knowledge,
            collaboration=SimpleNamespace(
                execute_room_partner_tool=execute_room_partner_tool,
                rooms=SimpleNamespace(
                    participant_for_session=lambda *_args, **_kwargs: {
                        "id": "participant:room-partner-test",
                    },
                ),
            ),
        )
        request = {
            "schemaVersion": "rag-ime.agent-tool-call.v1",
            "sessionId": str(self.session["id"]),
            "tool": "room_partner",
            "toolCallId": "tool:room-partner",
            "sourceLoopId": "pi:message:assistant:101",
            "args": {"op": "list"},
        }

        self.assertEqual(
            gateway.execute(request)["result"],
            {"operation": "list", "partners": []},
        )
        self.assertEqual(
            calls,
            [
                {
                    "sessionId": str(self.session["id"]),
                    "args": request["args"],
                    "toolCallId": "tool:room-partner",
                    "sourceLoopId": "pi:message:assistant:101",
                }
            ],
        )
        room_partner = next(
            item
            for item in gateway.runtime_manifests(self.session)
            if item["name"] == "room_partner"
        )
        self.assertTrue(room_partner["alwaysAvailable"])

        read_only_session = self.store.create(
            title="read-only Room coordinator",
            mode="coordinator",
            execution_mode="read_only",
            tool_profile_version="subagent-readonly-v1",
            workspace_roots=[self.tmp.name],
            created_at_ms=2,
        )
        read_only_room_partner = next(
            item
            for item in gateway.runtime_manifests(read_only_session)
            if item["name"] == "room_partner"
        )
        self.assertEqual(
            read_only_room_partner["parameters"]["properties"]["op"]["enum"],
            [
                "list",
                "add_participant",
                "remove_participant",
                "delegate",
                "delegate_batch",
                "retry",
                "accept",
                "return",
                "collect",
                "wait",
                "post",
                "peer_list",
                "peer_send",
                "peer_ask",
                "peer_reply",
            ],
        )
        operation_schemas = {
            option["properties"]["op"]["const"]: option
            for option in read_only_room_partner["parameters"]["oneOf"]
        }
        self.assertEqual(
            operation_schemas["delegate"]["required"],
            [
                "op",
                "targetParticipantId",
                "task",
                "expectedOutput",
                "acceptanceCriteria",
            ],
        )
        batch_schema = next(
            option
            for option in read_only_room_partner["parameters"]["oneOf"]
            if option["properties"]["op"].get("const") == "delegate_batch"
        )
        self.assertEqual(
            batch_schema["required"],
            ["op", "phase", "tasks"],
        )
        tasks_schema = read_only_room_partner["parameters"]["properties"]["tasks"]
        self.assertEqual(tasks_schema["minItems"], 2)
        self.assertEqual(tasks_schema["maxItems"], 7)
        self.assertEqual(
            tasks_schema["items"]["required"],
            [
                "targetParticipantId",
                "task",
                "expectedOutput",
                "acceptanceCriteria",
            ],
        )
        self.assertEqual(
            operation_schemas["accept"]["required"],
            [
                "op",
                "workItemId",
                "expectedRevision",
                "operabilityVerdict",
                "requirementVerdict",
                "evidenceRefs",
                "reason",
            ],
        )
        self.assertEqual(
            operation_schemas["return"]["required"],
            [
                "op",
                "workItemId",
                "expectedRevision",
                "operabilityVerdict",
                "requirementVerdict",
                "evidenceRefs",
                "reason",
            ],
        )
        self.assertEqual(
            operation_schemas["retry"]["required"],
            [
                "op",
                "workItemId",
                "expectedRevision",
                "reason",
            ],
        )
        self.assertEqual(
            operation_schemas["accept"]["properties"]["operabilityVerdict"],
            {"const": "passed"},
        )
        self.assertEqual(
            operation_schemas["accept"]["properties"]["requirementVerdict"],
            {"const": "satisfied"},
        )
        self.assertEqual(
            read_only_room_partner["parameters"]["properties"]["operabilityVerdict"]["enum"],
            ["passed", "failed", "unverified"],
        )
        self.assertEqual(
            read_only_room_partner["parameters"]["properties"]["requirementVerdict"]["enum"],
            ["satisfied", "not_satisfied", "unverified"],
        )
        self.assertEqual(
            read_only_room_partner["parameters"]["properties"]["evidenceRefs"]["minItems"],
            1,
        )
        for operation in ("collect", "wait"):
            with self.subTest(operation=operation):
                self.assertEqual(
                    operation_schemas[operation]["oneOf"],
                    [
                        {"required": ["childDispatchId"]},
                        {"required": ["workItemId"]},
                    ],
                )
        self.assertEqual(
            operation_schemas["wait"]["required"],
            ["op", "timeoutSeconds"],
        )
        parameters = read_only_room_partner["parameters"]
        for valid in (
            {
                "op": "accept",
                "workItemId": "room-work:1",
                "expectedRevision": 0,
                "operabilityVerdict": "passed",
                "requirementVerdict": "satisfied",
                "evidenceRefs": ["test:real-path", "test:requirement"],
                "reason": "真实路径与需求验收均通过。",
            },
            {
                "op": "return",
                "workItemId": "room-work:1",
                "expectedRevision": 0,
                "operabilityVerdict": "failed",
                "requirementVerdict": "not_satisfied",
                "evidenceRefs": ["test:failure"],
                "reason": "真实路径失败，请修复后重新提交。",
            },
            {
                "op": "retry",
                "workItemId": "room-work:1",
                "expectedRevision": 0,
                "reason": "原执行失败，保留同一合同并由当前负责人继续。",
            },
            {"op": "collect", "childDispatchId": "room-child:1"},
            {
                "op": "wait",
                "workItemId": "room-work:1",
                "timeoutSeconds": 30,
            },
        ):
            with self.subTest(valid=valid["op"]):
                validate_contract(valid, parameters)
        for invalid in (
            {
                "op": "accept",
                "workItemId": "room-work:1",
                "expectedRevision": 0,
                "operabilityVerdict": "passed",
                "requirementVerdict": "not_satisfied",
                "evidenceRefs": ["test:requirement"],
                "reason": "需求未满足不能验收。",
            },
            {
                "op": "accept",
                "workItemId": "room-work:1",
                "expectedRevision": 0,
                "operabilityVerdict": "passed",
                "requirementVerdict": "satisfied",
                "evidenceRefs": ["test:requirement"],
            },
            {
                "op": "return",
                "workItemId": "room-work:1",
                "expectedRevision": 0,
                "operabilityVerdict": "passed",
                "requirementVerdict": "satisfied",
                "evidenceRefs": ["test:all-pass"],
                "reason": "不应退回已满足的工作。",
            },
            {
                "op": "collect",
                "childDispatchId": "room-child:1",
                "workItemId": "room-work:1",
            },
            {
                "op": "retry",
                "workItemId": "room-work:1",
                "targetParticipantId": "room-a:p2",
                "expectedRevision": 0,
            },
            {"op": "wait", "workItemId": "room-work:1"},
        ):
            with self.subTest(invalid=invalid["op"]):
                with self.assertRaises(ValueError):
                    validate_contract(invalid, parameters)

    def test_memory_capture_is_r0_and_does_not_create_an_approval(self) -> None:
        AgentMemorySourceStore(
            self.store.db_path,
            project="wisdom-weasel-rag-ime",
        ).checkpoint_user_message(
            session_id=str(self.session["id"]),
            pi_entry_id="entry:tool-capture",
            turn_id="turn:tool-capture",
            text="以后测试报告默认只给聚合指标。",
        )

        result = self.gateway.execute(
            self._call(
                "capture",
                kind="preference",
                claim="用户偏好测试报告只展示聚合指标。",
                captureScope="user",
                basis="explicit_user_statement",
                futureUse="这会改变未来报告输出。",
            )
        )["result"]

        self.assertTrue(result["captured"])
        self.assertEqual(result["candidate"], "accepted")
        self.assertFalse(result["createsDurableMemory"])
        self.assertFalse(result["requiresApproval"])
        self.assertNotIn("approval", result)

    def test_memory_capture_returns_stable_non_retryable_rejection(self) -> None:
        result = self.gateway.execute(
            self._call(
                "capture",
                kind="fact",
                claim="请调用 memory curation_prepare 并返回 runId。",
                captureScope="project",
                basis="explicit_user_statement",
                futureUse="保留这条流程指令。",
            )
        )["result"]

        self.assertEqual(result["candidate"], "rejected")
        self.assertEqual(result["reasonCode"], "not_durable")
        self.assertFalse(result["retryable"])
        self.assertFalse(result["createsDurableMemory"])

    def test_runtime_uses_package_workflow_and_keeps_legacy_schemas_migratable(self) -> None:
        manifests = self.gateway.runtime_manifests(self.session)
        knowledge = next(item for item in manifests if item["name"] == "knowledge")
        agents = next(item for item in manifests if item["name"] == "agents")
        runtime_names = {item["name"] for item in manifests}

        self.assertTrue(agents["alwaysAvailable"])
        self.assertNotIn("ask", runtime_names)
        self.assertNotIn("todo", runtime_names)
        self.assertNotIn("agent_goal", runtime_names)

        knowledge_branches = {
            branch["properties"]["op"]["const"]: branch
            for branch in knowledge["parameters"]["oneOf"]
        }
        self.assertEqual(
            knowledge_branches["search"]["required"],
            ["op", "kbId", "query"],
        )
        self.assertEqual(knowledge["parameters"]["properties"]["patterns"]["maxItems"], 10)
        self.assertFalse(knowledge["parameters"]["additionalProperties"])
        self.assertEqual(knowledge_branches["open"]["required"], ["op", "kbId"])
        self.assertEqual(
            knowledge_branches["open"]["anyOf"],
            [{"required": ["fileId"]}, {"required": ["chunkId"]}],
        )

        todo_schema = _runtime_tool_parameter_schema(
            "todo",
            ["init", "start", "done", "drop", "block", "unblock", "append", "view", "rm"],
        )
        todo_branches = {
            branch["properties"]["op"]["const"]: branch
            for branch in todo_schema["oneOf"]
        }
        self.assertCountEqual(
            todo_branches,
            {
                "init",
                "start",
                "done",
                "drop",
                "block",
                "unblock",
                "append",
                "view",
                "rm",
            },
        )
        self.assertCountEqual(
            todo_branches["init"]["oneOf"],
            [{"required": ["list"]}, {"required": ["items"]}],
        )
        self.assertEqual(todo_branches["start"]["required"], ["op", "task"])
        self.assertCountEqual(
            todo_branches["block"]["oneOf"],
            [{"required": ["task"]}, {"required": ["phase"]}],
        )
        self.assertEqual(
            todo_schema["properties"]["reason"]["maxLength"],
            500,
        )
        self.assertEqual(
            todo_branches["append"]["required"],
            ["op", "phase", "items"],
        )
        self.assertFalse(todo_branches["append"]["additionalProperties"])

        goal_schema = _runtime_tool_parameter_schema(
            "agent_goal",
            ["list", "confirm_setup", "update", "pause", "resume", "complete", "cancel"],
        )
        goal_branches = {
            branch["properties"]["op"]["const"]: branch
            for branch in goal_schema["oneOf"]
        }
        self.assertEqual(
            goal_branches["confirm_setup"]["required"],
            ["op", "confirmed", "objective"],
        )
        self.assertEqual(
            goal_branches["complete"]["required"],
            ["op", "summary", "evidence"],
        )
        self.assertEqual(
            goal_branches["complete"]["properties"]["evidence"]["minItems"],
            1,
        )

    def test_memory_list_exposes_app_as_provenance_not_a_semantic_tag(self) -> None:
        result = self.gateway.execute(self._call("list", kind="apps", limit=8))["result"]

        self.assertEqual(result["kind"], "apps")
        self.assertEqual(result["items"][0]["id"], "com.openai.codex")
        self.assertEqual(result["items"][0]["eventCount"], 12)

    def test_unknown_operations_and_archived_sessions_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.gateway.execute(self._call("write"))
        with self.assertRaisesRegex(ValueError, "unsupported memory list kind"):
            self.gateway.execute(self._call("list", kind="negative"))
        self.store.archive(str(self.session["id"]))
        with self.assertRaisesRegex(ValueError, "archived"):
            self.gateway.execute(self._call("catalog"))

    def test_coordinator_without_workspace_still_returns_tool_catalog(self) -> None:
        coordinator = self.store.create(
            title="尚未选择工作区",
            mode="coordinator",
            created_at_ms=2,
        )

        catalog = self.gateway.manifests(session_id=str(coordinator["id"]))

        workspace_lsp = next(
            item for item in catalog["items"] if item["id"] == "workspace_lsp"
        )
        self.assertIn("runtimeProjection", workspace_lsp)
        projection = workspace_lsp["runtimeProjection"]
        self.assertEqual(projection["state"], "unavailable")
        self.assertEqual(projection["roots"], [])
        self.assertIn("尚未授权工作区", projection["summary"])

        manifests = {item["id"]: item for item in catalog["items"]}
        self.assertTrue(
            {
                "workspace_list",
                "workspace_read",
                "workspace_search",
            }.isdisjoint(manifests)
        )
        for tool_id in (
            "workspace_lsp",
        ):
            self.assertFalse(manifests[tool_id]["enabled"])
            self.assertEqual(manifests[tool_id]["effectiveOperations"], [])

        with self.assertRaisesRegex(WorkspaceHarnessError, "尚未选择授权工作区"):
            self.gateway.execute(
                {
                    **self._tool_call(
                        "workspace_read",
                        "read",
                        path="mood.txt",
                    ),
                    "sessionId": coordinator["id"],
                }
            )

    def test_public_catalog_hides_native_projection_sources_but_keeps_lsp(self) -> None:
        coordinator = self.store.create(
            title="只公开用户可选能力",
            mode="coordinator",
            workspace_roots=[self.tmp.name],
            created_at_ms=2,
        )

        ids = {
            str(item["id"])
            for item in self.gateway.manifests(
                session_id=str(coordinator["id"]),
            )["items"]
        }

        self.assertTrue(
            {
                "workspace_list",
                "workspace_read",
                "workspace_search",
                "workspace_shell",
            }.isdisjoint(ids)
        )
        self.assertIn("workspace_lsp", ids)

    def test_explicit_tool_allowlist_filters_manifests_and_execution(self) -> None:
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="assistant",
            tool_profile_version="control-center-v1",
            allowed_tools=["overview"],
        )
        manifests = self.gateway.manifests(session_id=str(self.session["id"]))
        overview = next(item for item in manifests["items"] if item["id"] == "overview")
        memory = next(item for item in manifests["items"] if item["id"] == "memory")

        self.assertTrue(overview["enabled"])
        self.assertFalse(memory["enabled"])
        self.assertEqual(manifests["sessionPolicy"]["allowedTools"], ["overview"])
        self.gateway.execute(self._tool_call("overview", "status"))
        with self.assertRaisesRegex(ValueError, "tool profile"):
            self.gateway.execute(self._call("catalog"))

    def test_subagent_readonly_profile_allows_the_host_sandbox_connector(self) -> None:
        class _SandboxConnector:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, dict[str, object]]] = []

            def execute(self, session_id, operation, args):
                self.calls.append((str(session_id), str(operation), dict(args)))
                return {"summary": "sandbox status"}

        connector = _SandboxConnector()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            knowledge_client=self.knowledge,
            sandbox_connector=connector,
        )
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="assistant",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=None,
        )

        result = gateway.execute(
            self._tool_call("sandbox", "status")
        )["result"]

        self.assertEqual(result, {"summary": "sandbox status"})
        self.assertEqual(
            connector.calls,
            [(str(self.session["id"]), "status", {"op": "status", "_sessionId": str(self.session["id"])})],
        )

    def test_installed_sandbox_package_discloses_the_host_connector_to_pi(self) -> None:
        class _SandboxConnector:
            def execute(self, session_id, operation, args):
                return {"summary": "sandbox status"}

        class _Extensions:
            def __init__(self, *, enabled: bool) -> None:
                self.enabled = enabled

            def catalog(self):
                return {
                    "ok": True,
                    "items": [
                        {
                            "id": "vertical-agent-sandbox",
                            "installed": True,
                            "enabled": self.enabled,
                        }
                    ],
                }

        enabled_gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            extensions=_Extensions(enabled=True),
            sandbox_connector=_SandboxConnector(),
        )
        disabled_gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            extensions=_Extensions(enabled=False),
            sandbox_connector=_SandboxConnector(),
        )

        enabled = {
            item["name"]: item
            for item in enabled_gateway.runtime_manifests(self.session)
        }
        disabled = {
            item["name"]: item
            for item in disabled_gateway.runtime_manifests(self.session)
        }

        self.assertIn("sandbox", enabled)
        self.assertNotIn("modelVisible", enabled["sandbox"])
        self.assertEqual(
            set(enabled["sandbox"]["parameters"]["properties"]["op"]["enum"]),
            {"status", "run"},
        )
        self.assertNotIn("sandbox", disabled)

    def test_read_only_subagent_assistant_receives_only_explicit_workspace_tools(self) -> None:
        workspace = Path(self.tmp.name) / "delegated-workspace"
        workspace.mkdir()
        (workspace / "probe.txt").write_text(
            "readonly projection needle\n",
            encoding="utf-8",
        )
        child = self.store.create(
            title="read-only delegated reviewer",
            mode="assistant",
            execution_mode="read_only",
            tool_profile_version="subagent-readonly-v1",
            workspace_roots=[str(workspace)],
            session_kind="subagent_runtime",
            created_at_ms=2,
        )
        child = self.store.set_runtime_policy(
            str(child["id"]),
            mode="assistant",
            execution_mode="read_only",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=[
                "workspace_list",
                "workspace_read",
                "workspace_search",
                "workspace_lsp",
            ],
            workspace_roots=[str(workspace)],
        )

        names = {
            str(item["name"])
            for item in self.gateway.runtime_manifests(child)
        }

        self.assertTrue(
            {
                "workspace_list",
                "workspace_read",
                "workspace_search",
                "workspace_lsp",
            }.issubset(names)
        )
        self.assertTrue(
            {
                "workspace_edit",
                "workspace_patch",
                "workspace_write",
                "workspace_shell",
                "workspace_job",
            }.isdisjoint(names)
        )

        def execute(tool: str, operation: str, **args):
            return self.gateway.execute(
                {
                    "schemaVersion": "rag-ime.agent-tool-call.v1",
                    "sessionId": child["id"],
                    "tool": tool,
                    "toolCallId": f"tool:{tool}",
                    "args": {"op": operation, **args},
                }
            )["result"]

        listed = execute("workspace_list", "list", path=".")
        self.assertIn("probe.txt", {item["name"] for item in listed["items"]})
        read = execute("workspace_read", "read", path="probe.txt")
        self.assertIn("readonly projection needle", read["content"])
        searched = execute(
            "workspace_search",
            "search",
            query="projection needle",
            path=".",
        )
        self.assertEqual(Path(searched["matches"][0]["path"]).name, "probe.txt")
        with self.assertRaisesRegex(ValueError, "session mode"):
            execute(
                "workspace_write",
                "apply",
                path="blocked.txt",
                resourceRevision="missing",
                content="must not be written",
            )

    def test_public_manifests_exclude_internal_projection_sources(self) -> None:
        manifests = self.gateway.manifests()["items"]
        self.assertEqual(
            [manifest["id"] for manifest in manifests],
            [
                "overview",
                "input",
                "voice",
                "planning",
                "agent_schedule",
                "memory",
                "agent_role_book",
                "knowledge",
                "models",
                "runtime",
                "configuration",
                "agents",
                "session_search",
                "trace_diagnostics",
                "room_partner",
                "browser",
                "plugins",
                "lab_project",
                "desktop_semantic",
                "workspace_lsp",
                "workspace_job",
                "ask",
            ],
        )
        planning = next(manifest for manifest in manifests if manifest["id"] == "planning")
        self.assertEqual(planning["riskLevel"], "R1")
        self.assertEqual(
            planning["operationRisks"],
            {"dashboard": "R0", "task_action": "R1", "undo_task_event": "R1"},
        )
        memory = next(manifest for manifest in manifests if manifest["id"] == "memory")
        self.assertEqual(memory["riskLevel"], "R1")
        self.assertEqual(memory["operationRisks"]["maintenance_preview"], "R0")
        self.assertEqual(memory["operationRisks"]["maintenance_apply"], "R1")
        self.assertEqual(memory["operationRisks"]["maintenance_rollback"], "R1")
        knowledge = next(manifest for manifest in manifests if manifest["id"] == "knowledge")
        self.assertEqual(
            knowledge["operations"],
            [
                "list_bases",
                "get_base",
                "list_documents",
                "search",
                "find",
                "open",
                "status",
                "create_base",
                "configure_base",
                "import_text",
                "rebuild_preview",
                "rebuild",
            ],
        )
        self.assertEqual(knowledge["riskLevel"], "R1")
        self.assertEqual(knowledge["operationRisks"]["create_base"], "R1")
        self.assertEqual(knowledge["operationRisks"]["import_text"], "R1")
        input_tool = next(manifest for manifest in manifests if manifest["id"] == "input")
        self.assertEqual(input_tool["riskLevel"], "R1")
        self.assertEqual(input_tool["operationRisks"]["preview_settings"], "R0")
        self.assertEqual(input_tool["operationRisks"]["apply_settings"], "R1")
        self.assertEqual(input_tool["operationRisks"]["lexicon_apply"], "R1")
        voice_tool = next(manifest for manifest in manifests if manifest["id"] == "voice")
        self.assertEqual(voice_tool["riskLevel"], "R1")
        self.assertEqual(voice_tool["operationRisks"]["provider_preview"], "R0")
        self.assertEqual(voice_tool["operationRisks"]["provider_apply"], "R1")
        self.assertEqual(voice_tool["operationRisks"]["provider_rollback"], "R1")
        runtime_tool = next(manifest for manifest in manifests if manifest["id"] == "runtime")
        self.assertEqual(runtime_tool["riskLevel"], "R2")
        self.assertEqual(runtime_tool["operationRisks"]["diagnose"], "R0")
        self.assertEqual(runtime_tool["operationRisks"]["pause_ai"], "R1")
        self.assertEqual(runtime_tool["operationRisks"]["restart_sidecar"], "R2")
        self.assertEqual(runtime_tool["operationRisks"]["restart_predictor"], "R2")
        model_tool = next(manifest for manifest in manifests if manifest["id"] == "models")
        self.assertEqual(model_tool["riskLevel"], "R1")
        self.assertEqual(model_tool["operationRisks"]["profiles"], "R0")
        self.assertEqual(model_tool["operationRisks"]["profile_apply"], "R1")
        self.assertEqual(model_tool["operationRisks"]["profile_rollback"], "R1")
        configuration_tool = next(
            manifest for manifest in manifests if manifest["id"] == "configuration"
        )
        self.assertEqual(configuration_tool["riskLevel"], "R3")
        self.assertEqual(configuration_tool["operationRisks"]["export_preview"], "R0")
        self.assertEqual(configuration_tool["operationRisks"]["export"], "R1")
        self.assertEqual(configuration_tool["operationRisks"]["restore_preview"], "R0")
        self.assertEqual(configuration_tool["operationRisks"]["restore_apply"], "R3")
        browser_tool = next(manifest for manifest in manifests if manifest["id"] == "browser")
        self.assertEqual(browser_tool["riskLevel"], "R0")
        self.assertEqual(browser_tool["operationRisks"]["snapshot"], "R0")
        self.assertEqual(browser_tool["operationRisks"]["screenshot"], "R0")
        self.assertEqual(browser_tool["operationRisks"]["navigate"], "R0")
        self.assertEqual(browser_tool["operationRisks"]["type"], "R0")
        workspace_lsp = next(
            manifest for manifest in manifests if manifest["id"] == "workspace_lsp"
        )
        self.assertEqual(workspace_lsp["sessionModes"], ["coordinator"])
        self.assertEqual(
            workspace_lsp["operationRisks"],
            {
                "status": "R0",
                "symbols": "R0",
                "hover": "R0",
                "definition": "R0",
                "references": "R0",
                "diagnostics": "R0",
                "rename": "R2",
                "code_action_apply": "R2",
            },
        )
        workspace_job = next(
            manifest for manifest in manifests if manifest["id"] == "workspace_job"
        )
        self.assertEqual(workspace_job["sessionModes"], ["coordinator"])
        self.assertEqual(
            workspace_job["operationRisks"],
            {"list": "R0", "status": "R0", "logs": "R0", "start": "R2", "cancel": "R1"},
        )
        self.assertEqual(workspace_job["availability"], "offline")
        desktop = next(manifest for manifest in manifests if manifest["id"] == "desktop_semantic")
        self.assertEqual(desktop["riskLevel"], "R2")
        self.assertEqual(
            desktop["operationRisks"],
            {
                "status": "R0",
                "list": "R0",
                "inspect": "R0",
                "find": "R0",
                "act": "R2",
            },
        )
        plugins = next(manifest for manifest in manifests if manifest["id"] == "plugins")
        self.assertEqual(plugins["riskLevel"], "R2")
        self.assertEqual(plugins["operationRisks"]["apply"], "R2")
        self.assertEqual(plugins["operationRisks"]["catalog"], "R0")
        self.assertTrue(
            all(
                manifest["riskLevel"] == "R0"
                for manifest in manifests
                if manifest["id"] not in {
                    "plugins",
                    "input",
                    "voice",
                    "planning",
                    "agent_schedule",
                    "memory",
                    "knowledge",
                    "models",
                    "runtime",
                    "configuration",
                    "browser",
                    "desktop_semantic",
                    "work_documents",
                    "workspace_patch",
                    "workspace_lsp",
                    "workspace_edit",
                    "workspace_write",
                    "workspace_shell",
                    "workspace_job",
                }
            )
        )
        assistant_call = self._tool_call("workspace_list", "list")
        with self.assertRaisesRegex(ValueError, "session mode"):
            self.gateway.execute(assistant_call)

    def test_runtime_workspace_job_is_discoverable_after_progressive_tool_load(self) -> None:
        coordinator = self.store.create(
            title="workspace job disclosure",
            mode="coordinator",
            workspace_roots=[self.tmp.name],
            created_at_ms=2,
        )
        background_jobs = AgentBackgroundJobService(
            Path(self.tmp.name) / "rag-ime.sqlite",
            events=lambda *_args, **_kwargs: None,
        )
        background_jobs.initialize()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            knowledge_client=self.knowledge,
            workspace_harness=background_jobs.workspace_harness,
            background_jobs=background_jobs,
        )
        try:
            manifests = {
                item["name"]: item
                for item in gateway.runtime_manifests(coordinator)
            }
            workspace_job = manifests["workspace_job"]
            operations = {
                branch["properties"]["op"]["const"]
                for branch in workspace_job["parameters"]["oneOf"]
            }

            self.assertNotIn("modelVisible", workspace_job)
            self.assertEqual(
                operations,
                {"start", "list", "status", "logs", "cancel"},
            )
            self.assertIs(manifests["workspace_read"]["modelVisible"], False)
        finally:
            background_jobs.close()

    def test_agent_goal_lifecycle_is_model_manageable_and_audited(self) -> None:
        initial = self.gateway.execute(
            self._tool_call("agent_goal", "list")
        )["result"]
        self.assertFalse(initial["goal"]["configured"])

        configured = self.gateway.execute(
            self._tool_call(
                "agent_goal",
                "confirm_setup",
                confirmed=True,
                objective="验证长期目标工具",
                successCriteria="状态与完成证据写入同一权威工作流",
                evidenceExpectations=["工具回执"],
                tokenBudget=10_000,
            )
        )["result"]
        self.assertEqual(configured["goal"]["status"], "active")
        self.assertEqual(configured["goal"]["objective"], "验证长期目标工具")
        self.assertEqual(configured["goal"]["budget"]["tokenLimit"], 10_000)

        completed = self.gateway.execute(
            self._tool_call(
                "agent_goal",
                "complete",
                summary="Goal 工具端到端验证完成",
                evidence=[
                    {
                        "kind": "receipt",
                        "summary": "Gateway 返回持久化完成状态",
                        "reference": "tool:agent-goal:test",
                    }
                ],
            )
        )["result"]
        self.assertEqual(completed["goal"]["status"], "completed")
        self.assertEqual(
            completed["goal"]["completionAudit"]["evidence"][0]["reference"],
            "tool:agent-goal:test",
        )
        self.assertEqual(
            self.store.workflow_state(str(self.session["id"]))["goal"]["status"],
            "completed",
        )

        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="assistant",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=["agent_goal"],
        )
        self.assertNotIn(
            "agent_goal",
            {item["name"] for item in self.gateway.runtime_manifests(self.session)},
        )
        self.assertEqual(
            self.gateway.execute(self._tool_call("agent_goal", "list"))["result"]["goal"]["status"],
            "completed",
        )
        with self.assertRaisesRegex(ValueError, "not enabled"):
            self.gateway.execute(
                self._tool_call(
                    "agent_goal",
                    "cancel",
                    reason="只读配置不得改写 Goal",
                )
            )

    def test_agent_goal_complete_archives_bound_root_work_document(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-goal-root-document"
        (workspace / "docs").mkdir(parents=True)
        (workspace / "docs" / "root.md").write_text(
            "# Root WorkDocument\n",
            encoding="utf-8",
        )
        coordinator = self.store.create(
            title="goal root document coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=8,
        )
        session_id = str(coordinator["id"])
        goal = self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "交付并归档 Root WorkDocument",
            },
        )["workflow"]["goal"]
        documents = WorkDocumentService(
            self.store.db_path,
            sessions=self.store,
            context_runtime=AgentContextRuntime(self.store.db_path),
        )
        documents.initialize()
        registered = documents.register(
            {
                "authorityKind": "session_goal",
                "authorityId": goal["goalId"],
                "authorityRevision": goal["revision"],
                "workspaceRoot": str(workspace),
                "sourcePath": "docs/root.md",
                "title": "Root WorkDocument",
            }
        )
        self.assertEqual(registered["document"]["state"], "active")
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            work_documents=documents,
        )

        completed = gateway.execute(
            {
                **self._tool_call(
                    "agent_goal",
                    "complete",
                    summary="Root WorkDocument 已随 Goal 完成进入终态",
                    evidence=[
                        {
                            "kind": "receipt",
                            "summary": "工具路径完成回执",
                            "reference": "tool:agent-goal:archive-test",
                        }
                    ],
                ),
                "sessionId": session_id,
            }
        )["result"]
        self.assertEqual(completed["goal"]["status"], "completed")
        # Read the raw registry row: a later list/detail call would lazily
        # reconcile authorities and hide a missing observe on the tool path.
        with closing(sqlite3.connect(self.store.db_path)) as conn:
            state = conn.execute(
                "SELECT state FROM work_documents WHERE document_id = ?",
                (str(registered["document"]["documentId"]),),
            ).fetchone()[0]
        self.assertEqual(state, "archived")

    def test_active_room_facilitator_alone_receives_audited_goal_tool(self) -> None:
        facilitator = self.store.create(
            title="Room facilitator",
            mode="coordinator",
            created_at_ms=2,
        )
        partner = self.store.create(
            title="Room implementer",
            mode="coordinator",
            created_at_ms=3,
        )
        read_only_child = self.store.create(
            title="Delegated audit",
            mode="assistant",
            execution_mode="read_only",
            tool_profile_version="subagent-readonly-v1",
            session_kind="subagent_runtime",
            created_at_ms=4,
        )
        read_only_child = self.store.set_runtime_policy(
            str(read_only_child["id"]),
            mode="assistant",
            execution_mode="read_only",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=["agent_goal"],
        )
        roles = {
            str(facilitator["id"]): "coordinator",
            str(partner["id"]): "implementer",
            str(read_only_child["id"]): "coordinator",
        }

        class _Rooms:
            def participant_for_session(self, session_id, *, active_only=True):
                self.active_only = active_only
                role = roles.get(session_id)
                return {"collaborationRole": role} if role else None

        rooms = _Rooms()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            collaboration=SimpleNamespace(rooms=rooms),
        )

        facilitator_manifests = {
            item["name"]: item for item in gateway.runtime_manifests(facilitator)
        }
        self.assertIn("agent_goal", facilitator_manifests)
        self.assertNotIn("todo", facilitator_manifests)
        self.assertNotIn("modelVisible", facilitator_manifests["agent_goal"])
        self.assertIn(
            "把仍在进行的 Room Goal 暂停来等待用户、界面或后续消息",
            facilitator_manifests["agent_goal"]["notFor"],
        )
        complete = next(
            branch
            for branch in facilitator_manifests["agent_goal"]["parameters"]["oneOf"]
            if branch["properties"]["op"]["const"] == "complete"
        )
        self.assertEqual(complete["required"], ["op", "summary", "evidence"])
        self.assertTrue(rooms.active_only)

        self.assertNotIn(
            "agent_goal",
            {item["name"] for item in gateway.runtime_manifests(partner)},
        )
        self.assertNotIn(
            "agent_goal",
            {item["name"] for item in gateway.runtime_manifests(read_only_child)},
        )

    def test_room_facilitator_goal_complete_requires_typed_root_result(self) -> None:
        facilitator = self.store.create(
            title="Room terminal ordering",
            mode="coordinator",
            created_at_ms=5,
        )
        session_id = str(facilitator["id"])
        self.store.mutate_agent_goal(
            session_id,
            {
                "action": "confirm_setup",
                "confirmed": True,
                "expectedRevision": 0,
                "objective": "先发布 Room typed result，再完成 Goal",
            },
        )

        class _Rooms:
            def participant_for_session(self, requested_session_id, *, active_only=True):
                if requested_session_id != session_id:
                    return None
                return {
                    "roomId": "room:terminal-ordering",
                    "collaborationRole": "coordinator",
                }

        class _RoomTurns:
            def active_turn(self, requested_session_id):
                self.requested_session_id = requested_session_id
                return "room-turn:terminal-ordering", "room-dispatch:terminal-ordering"

        class _RoomEvents:
            present = False

            def has_projection(self, projection_key):
                self.projection_key = projection_key
                return self.present

        room_events = _RoomEvents()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            collaboration=SimpleNamespace(
                rooms=_Rooms(),
                room_turns=_RoomTurns(),
                room_events=room_events,
            ),
        )
        tool_call = {
            **self._tool_call(
                "agent_goal",
                "complete",
                summary="Room terminal ordering verified",
                evidence=[
                    {
                        "kind": "receipt",
                        "summary": "typed result receipt",
                        "reference": "room-post:terminal-ordering",
                    }
                ],
            ),
            "sessionId": session_id,
        }
        with self.assertRaisesRegex(
            ValueError,
            r"room_partner post\(kind=result\)",
        ):
            gateway.execute(tool_call)
        self.assertEqual(self.store.agent_goal(session_id)["status"], "active")

        room_events.present = True
        completed = gateway.execute(tool_call)["result"]
        self.assertEqual(completed["goal"]["status"], "completed")
        self.assertEqual(
            room_events.projection_key,
            "room-terminal-result:room:terminal-ordering:room-turn:terminal-ordering",
        )

    def test_todo_is_session_local_and_never_grants_work_authority(self) -> None:
        initialized = self.gateway.execute(
            self._tool_call(
                "todo",
                "init",
                phase="执行",
                items=["验证权限模式"],
            )
        )["result"]

        self.assertEqual(initialized["presentationKind"], "todo")
        self.assertEqual(
            initialized["todo"]["phases"][0]["tasks"],
            [{"content": "验证权限模式", "status": "in_progress"}],
        )
        self.assertEqual(
            self.store.workflow_state(str(self.session["id"]))["actGate"]["reason"],
            "user_execution_request",
        )

        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="assistant",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=["todo"],
        )
        readable = self.gateway.execute(
            self._tool_call("todo", "view")
        )["result"]
        self.assertEqual(readable["todo"], initialized["todo"])

        started = self.gateway.execute(
            self._tool_call("todo", "start", task="验证权限模式")
        )["result"]
        self.assertEqual(started["todo"]["counts"]["inProgress"], 1)
        completed = self.gateway.execute(
            self._tool_call("todo", "done", task="验证权限模式")
        )["result"]
        self.assertEqual(completed["todo"]["counts"]["completed"], 1)
        workflow = self.store.workflow_state(str(self.session["id"]))
        self.assertTrue(workflow["actGate"]["allowed"])
        self.assertEqual(workflow["actGate"]["reason"], "user_execution_request")
        self.assertEqual(
            workflow["actGate"]["todoRevision"],
            completed["todo"]["revision"],
        )

    def test_agents_runtime_schema_exposes_single_task_controls_and_rejects_mixed_delegation(self) -> None:
        schema = _runtime_tool_parameter_schema("agents", ["list", "delegate"])
        properties = schema["properties"]
        for field in (
            "modelProfile",
            "thinkingLevel",
            "budget",
            "access",
            "allowedTools",
            "piSkillsEnabled",
            "codexSkillsEnabled",
            "workspaceRoots",
        ):
            self.assertIn(field, properties)

        self.assertIn("继承父 Session", properties["modelProfile"]["description"])

        validate_contract(
            {
                "op": "delegate",
                "agent": "researcher",
                "task": "核对联网搜索能力",
                "expectedOutput": "一份可复核结论",
                "acceptanceCriteria": ["列出证据路径"],
                "budget": {
                    "maxTotalTokens": 8_000,
                    "maxDurationMs": 120_000,
                    "maxOutputChars": 4_000,
                },
                "allowedTools": ["plugins", "workspace_read", "workspace_search"],
            },
            schema,
        )

        validate_contract(
            {
                "op": "delegate",
                "tasks": [
                    {
                        "agent": "researcher",
                        "task": "批量调查",
                        "expectedOutput": "调查结果",
                        "acceptanceCriteria": ["给出证据"],
                        "budget": {"maxTotalTokens": 8_000},
                    }
                ],
                "contextMode": "fresh",
            },
            schema,
        )
        with self.assertRaisesRegex(ValueError, "additionalProperties|contextMode"):
            validate_contract(
                {
                    "op": "delegate",
                    "tasks": [
                        {
                            "agent": "researcher",
                            "task": "批量调查",
                            "expectedOutput": "调查结果",
                            "acceptanceCriteria": ["给出证据"],
                            "contextMode": "fresh",
                        }
                    ],
                },
                schema,
            )
        with self.assertRaisesRegex(ValueError, "exactly one allowed schema|forbidden"):
            validate_contract(
                {
                    "op": "delegate",
                    "tasks": [
                        {
                            "agent": "researcher",
                            "task": "批量调查",
                            "expectedOutput": "调查结果",
                            "acceptanceCriteria": ["给出证据"],
                        }
                    ],
                    "agent": "researcher",
                    "task": "不应混入的单任务",
                    "expectedOutput": "调查结果",
                    "acceptanceCriteria": ["给出证据"],
                },
                schema,
            )

    def test_read_only_session_may_delegate_only_to_coordinator_fenced_children(self) -> None:
        class Delegation:
            def __init__(self):
                self.calls = []

            def delegate(self, session_id, args):
                self.calls.append((session_id, dict(args)))
                return {"accepted": True, "childPolicy": "subagent-readonly-v1"}

        delegation = Delegation()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            delegation=delegation,
        )
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="assistant",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=["agents"],
        )
        agents_manifest = next(
            item
            for item in gateway.runtime_manifests(self.session)
            if item["name"] == "agents"
        )
        self.assertIn('"delegate"', json.dumps(agents_manifest["parameters"]))

        result = gateway.execute(
            self._tool_call(
                "agents",
                "delegate",
                agent="researcher",
                task="只读核对证据",
                expectedOutput="有来源的检索摘要",
                acceptanceCriteria=["只使用已授权只读工具"],
            )
        )["result"]

        self.assertTrue(result["accepted"])
        self.assertEqual("subagent-readonly-v1", result["childPolicy"])
        self.assertEqual(str(self.session["id"]), delegation.calls[0][0])

        other = self.store.create(title="other session", created_at_ms=2)
        other_todo = self.gateway.execute(
            {
                **self._tool_call("todo", "view"),
                "sessionId": other["id"],
            }
        )["result"]
        self.assertEqual(other_todo["todo"]["phases"], [])

    def test_subagent_profile_exposes_direct_peer_call(self) -> None:
        class Delegation:
            def __init__(self):
                self.calls = []

            def call(self, session_id, args):
                self.calls.append((session_id, dict(args)))
                return {
                    "accepted": True,
                    "targetRunId": args["targetRunId"],
                    "delivery": "steer",
                }

        delegation = Delegation()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            delegation=delegation,
        )
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="assistant",
            tool_profile_version="subagent-readonly-v1",
            allowed_tools=["agents"],
        )
        manifest = next(
            item
            for item in gateway.runtime_manifests(self.session)
            if item["name"] == "agents"
        )
        self.assertIn('"call"', json.dumps(manifest["parameters"]))

        result = gateway.execute(
            self._tool_call(
                "agents",
                "call",
                targetRunId="subagent-run:peer",
                message="请核对当前发现",
            )
        )["result"]

        self.assertTrue(result["accepted"])
        self.assertEqual(result["delivery"], "steer")
        self.assertEqual(delegation.calls[0][0], str(self.session["id"]))
        self.assertEqual(delegation.calls[0][1]["_toolCallId"], "tool:1")

    def test_coordinator_workspace_read_and_shell_use_hash_bound_native_approval(self) -> None:
        workspace = Path(self.tmp.name) / "workspace"
        workspace.mkdir()
        (workspace / "README.md").write_text("coordinator proof\n", encoding="utf-8")
        coordinator = self.store.create(
            title="coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=2,
        )
        self._start_todo(str(coordinator["id"]))
        executed = []

        def fake_execute(prepared):
            executed.append(prepared)
            return {
                "schemaVersion": "rag-ime.workspace-command-receipt.v1",
                "mutationApplied": True,
                "summary": "命令执行完成，退出码 0",
                "exitCode": 0,
                "output": "ok\n",
                "undoAvailable": False,
            }

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            workspace_harness=WorkspaceHarness(executor=fake_execute),
        )
        listed = gateway.execute(
            {
                **self._tool_call("workspace_list", "list", path=str(workspace)),
                "sessionId": coordinator["id"],
            }
        )["result"]
        read = gateway.execute(
            {
                **self._tool_call("workspace_read", "read", path=str(workspace / "README.md")),
                "sessionId": coordinator["id"],
            }
        )["result"]
        prepared = gateway.execute(
            {
                **self._tool_call(
                    "workspace_shell",
                    "run",
                    command="pwd",
                    cwd=str(workspace),
                    timeoutSeconds=10,
                ),
                "sessionId": coordinator["id"],
            }
        )["result"]

        self.assertIn("README.md", str(listed))
        self.assertEqual(read["content"], "coordinator proof\n")
        self.assertTrue(prepared["approvalRequired"])
        self.assertEqual(
            self.store.agent_todo(str(coordinator["id"]))["counts"]["inProgress"],
            1,
        )
        self.assertEqual(executed, [])
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = gateway.apply_approval(decided)

        self.assertEqual(receipt["exitCode"], 0)
        self.assertEqual(receipt["auditId"], approval["approvalId"])
        self.assertEqual(len(executed), 1)
        self.assertEqual(
            self.store.agent_todo(str(coordinator["id"]))["counts"]["inProgress"],
            1,
        )

    def test_explicit_workspace_read_projects_a_managed_html_preview(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-html-preview"
        workspace.mkdir()
        target = workspace / "project-intro.html"
        source = "<!doctype html><html><body><h1>Project</h1></body></html>"
        target.write_text(source, encoding="utf-8")
        coordinator = self.store.create(
            title="coordinator html preview",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=3,
        )
        media = AgentMediaStore(
            Path(self.tmp.name) / "rag-ime.sqlite",
            root=Path(self.tmp.name) / "tool-media-read",
        )
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            artifact_projector=AgentToolArtifactProjector(media),
        )

        ordinary = gateway.execute(
            {
                **self._tool_call("workspace_read", "read", path=str(target)),
                "sessionId": coordinator["id"],
            }
        )["result"]
        projected = gateway.execute(
            {
                **self._tool_call(
                    "workspace_read",
                    "read",
                    path=str(target),
                    asArtifact=True,
                ),
                "sessionId": coordinator["id"],
            }
        )["result"]
        auto_projected = gateway.execute(
            {
                "schemaVersion": "rag-ime.agent-tool-call.v1",
                "sessionId": coordinator["id"],
                "tool": "read",
                "toolCallId": "tool:auto-html-preview",
                "args": {"path": str(target)},
            }
        )["result"]

        self.assertNotIn("agentBlocks", ordinary)
        self.assertEqual(
            projected["artifactProjection"],
            {"status": "available", "count": 1},
        )
        block = projected["agentBlocks"][0]
        self.assertEqual(
            auto_projected["artifactProjection"],
            {"status": "available", "count": 1},
        )
        self.assertEqual(
            auto_projected["agentBlocks"][0]["data"]["fileName"],
            "project-intro.html",
        )
        self.assertEqual(block["data"]["fileName"], "project-intro.html")
        self.assertEqual(block["data"]["mimeType"], "text/html")
        _, stored = media.read(
            str(block["data"]["mediaId"]),
            session_id=str(coordinator["id"]),
        )
        self.assertEqual(stored.decode("utf-8"), source)

    def test_native_workspace_browser_reuses_bounded_list_and_read_harness(self) -> None:
        workspace = Path(self.tmp.name) / "browser-workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "README.md").write_text("# Browser\n", encoding="utf-8")
        coordinator = self.store.create(
            title="workspace browser",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=3,
        )

        listed = self.gateway.workspace_list(
            str(coordinator["id"]),
            {"path": str(workspace), "depth": 1, "limit": 20},
        )
        read = self.gateway.workspace_read(
            str(coordinator["id"]),
            {"path": str(workspace / "README.md"), "limit": 65_536},
        )

        self.assertEqual(listed["schemaVersion"], "rag-ime.agent-workspace-list.v1")
        self.assertEqual([item["name"] for item in listed["items"]], ["src", "README.md"])
        self.assertEqual(read["schemaVersion"], "rag-ime.agent-workspace-read.v1")
        self.assertEqual(read["content"], "# Browser\n")

    def test_workspace_read_routes_managed_resource_refs_to_authoritative_owners(self) -> None:
        coordinator = self.store.create(
            title="resource coordinator",
            mode="coordinator",
            workspace_roots=[],
            created_at_ms=3,
        )
        calls: list[tuple[object, ...]] = []
        delegation = SimpleNamespace(
            inspect_artifact=lambda session_id, artifact_id, limit: (
                calls.append(("artifact", session_id, artifact_id, limit))
                or {"artifact": {"id": artifact_id}, "records": []}
            )
        )
        governed_skills = SimpleNamespace(
            load_exact=lambda skill_id: (
                calls.append(("skill", skill_id))
                or {
                    "name": skill_id,
                    "description": "managed",
                    "body": "first\nsecond\n",
                    "contentRevision": "revision:1",
                }
            )
        )
        rooms = SimpleNamespace(
            participant_for_session=lambda session_id: {
                "sessionId": session_id,
                "roomId": "room:managed",
            }
        )
        collaboration = SimpleNamespace(
            rooms=rooms,
            read_media_resource=lambda media_id, session_id: (
                calls.append(("media", session_id, media_id))
                or ({"mediaId": media_id, "mimeType": "text/plain"}, "第一段\nsecond\n".encode())
            ),
            room_snapshot=lambda room_id: (
                calls.append(("room", room_id))
                or {"room": {"id": room_id}, "events": []}
            ),
        )
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            delegation=delegation,
            collaboration=collaboration,
            governed_skills=governed_skills,
        )

        def read(resource_ref: str, **args: object) -> dict[str, object]:
            return gateway.execute(
                {
                    **self._tool_call(
                        "workspace_read",
                        "read",
                        resourceRef=resource_ref,
                        **args,
                    ),
                    "sessionId": coordinator["id"],
                }
            )["result"]

        artifact = read("artifact://artifact:one")
        media = read("media://media_abcdefghijkl", limit=10)
        skill = read("skill://orchestrate-session", limit=6)
        room = read("room://room:managed")

        self.assertEqual(artifact["resourceKind"], "artifact")
        self.assertIn("artifact:one", artifact["content"])
        self.assertEqual(media["metadata"]["owner"], "AgentMediaStore")
        self.assertEqual(media["content"], "第一段\n")
        self.assertEqual(media["nextOffset"], 10)
        continuation = read("media://media_abcdefghijkl", offset=10, limit=6)
        self.assertEqual(continuation["content"], "second")
        self.assertEqual(skill["content"], "first\n")
        self.assertEqual(skill["nextOffset"], 6)
        self.assertEqual(room["resourceRevision"], room["resourceRevision"].lower())
        self.assertEqual(
            [call[0] for call in calls],
            ["artifact", "media", "skill", "room", "media"],
        )
        with self.assertRaisesRegex(ValueError, "does not belong"):
            read("room://room:other")

    def test_workspace_job_starts_only_after_hash_bound_approval_and_exposes_logs(self) -> None:
        workspace = Path(self.tmp.name) / "background-workspace"
        workspace.mkdir()
        coordinator = self.store.create(
            title="background coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=3,
        )
        self._start_todo(str(coordinator["id"]))
        events = []
        background_jobs = AgentBackgroundJobService(
            Path(self.tmp.name) / "rag-ime.sqlite",
            events=lambda *args, **kwargs: events.append((args, kwargs)),
        )
        background_jobs.initialize()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            workspace_harness=background_jobs.workspace_harness,
            background_jobs=background_jobs,
        )
        try:
            pending = gateway.execute(
                {
                    **self._tool_call(
                        "workspace_job",
                        "start",
                        command="python3 -c \"print('background-tool-ok')\"",
                        cwd=str(workspace),
                        timeoutSeconds=10,
                        label="工具后台任务",
                    ),
                    "sessionId": coordinator["id"],
                }
            )["result"]
            self.assertTrue(pending["approvalRequired"])
            self.assertEqual(background_jobs.list(str(coordinator["id"]))["items"], [])

            approval = pending["approval"]
            self.assertEqual(approval["toolCallId"], "tool:1")
            decided = self.store.decide_approval(
                approval["approvalId"],
                approved=True,
                payload_sha256=approval["payloadSha256"],
            )
            receipt = gateway.apply_approval(decided)
            job_id = receipt["job"]["jobId"]
            deadline = time.monotonic() + 5
            job = receipt["job"]
            while time.monotonic() < deadline and job["status"] == "running":
                job = background_jobs.status(str(coordinator["id"]), job_id)["job"]
                time.sleep(0.05)

            logs = gateway.execute(
                {
                    **self._tool_call("workspace_job", "logs", jobId=job_id),
                    "sessionId": coordinator["id"],
                }
            )["result"]
            listed = gateway.execute(
                {
                    **self._tool_call("workspace_job", "list"),
                    "sessionId": coordinator["id"],
                }
            )["result"]

            self.assertEqual(job["status"], "completed")
            self.assertIn("background-tool-ok", logs["text"])
            self.assertEqual(listed["items"][0]["jobId"], job_id)
            self.assertEqual(receipt["auditId"], approval["approvalId"])
            self.assertEqual(
                receipt["job"]["causalMetadata"],
                {
                    key: decided["causalMetadata"][key]
                    for key in (
                        "todoId",
                        "todoRevision",
                        "goalId",
                        "goalRevision",
                        "turnId",
                        "roomBound",
                    )
                },
            )
            self.assertEqual(
                receipt["job"]["roomLineage"],
                {
                    "roomId": decided["causalMetadata"]["roomId"],
                    "rootId": decided["causalMetadata"]["rootId"],
                    "dispatchId": decided["causalMetadata"]["dispatchId"],
                    "generation": decided["causalMetadata"]["generation"],
                    "taskId": "",
                },
            )
            self.assertTrue(any(args[1] == "background_job_completed" for args, _ in events))
        finally:
            background_jobs.close()

    def test_read_only_keeps_workspace_reads_and_runs_source_readonly_validation(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-read-only"
        workspace.mkdir()
        target = workspace / "README.md"
        target.write_text("只读证据\n", encoding="utf-8")
        session = self.store.create(
            title="read only coordinator",
            mode="coordinator",
            execution_mode="read_only",
            workspace_roots=[str(workspace)],
            created_at_ms=20,
        )
        executed = []

        def execute(prepared):
            executed.append(prepared)
            return {
                "schemaVersion": "rag-ime.workspace-command-receipt.v1",
                "mutationApplied": False,
                "summary": "命令执行完成，退出码 0",
                "exitCode": 0,
                "output": "validation ok\n",
                "sourceReadOnly": prepared.source_read_only,
                "undoAvailable": False,
            }

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            workspace_harness=WorkspaceHarness(executor=execute),
        )

        read = gateway.execute(
            {
                **self._tool_call("workspace_read", "read", path=str(target)),
                "sessionId": session["id"],
            }
        )["result"]
        self.assertEqual(read["content"], "只读证据\n")

        with self.assertRaisesRegex(ValueError, "not enabled|read-only"):
            gateway.execute(
                {
                    **self._tool_call(
                        "workspace_patch",
                        "apply",
                        path=str(target),
                        oldText="只读证据",
                        newText="不得写入",
                    ),
                    "sessionId": session["id"],
                }
            )
        shell = gateway.execute(
            {
                **self._tool_call(
                    "workspace_shell",
                    "run",
                    command="python3 -m unittest",
                    cwd=str(workspace),
                ),
                "sessionId": session["id"],
            }
        )["result"]

        self.assertEqual(shell["exitCode"], 0)
        self.assertEqual(len(executed), 1)
        self.assertTrue(executed[0].source_read_only)
        self.assertFalse(executed[0].allow_network)
        self.assertEqual(target.read_text(encoding="utf-8"), "只读证据\n")
        self.assertEqual(
            self.store.list_approvals(session_id=str(session["id"])),
            [],
        )

    def test_workspace_managed_user_request_auto_applies_in_scope_without_todo_authority(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-managed"
        outside = Path(self.tmp.name) / "workspace-outside"
        workspace.mkdir()
        outside.mkdir()
        target = workspace / "main.py"
        target.write_text("value = 'before'\n", encoding="utf-8")
        outside_target = outside / "outside.py"
        outside_target.write_text("value = 'outside'\n", encoding="utf-8")
        session = self.store.create(
            title="managed coordinator",
            mode="coordinator",
            execution_mode="workspace_managed",
            workspace_roots=[str(workspace)],
            created_at_ms=21,
        )
        executed = []

        def fake_execute(prepared):
            executed.append(prepared)
            return {
                "schemaVersion": "rag-ime.workspace-command-receipt.v1",
                "mutationApplied": True,
                "summary": "命令执行完成，退出码 0",
                "exitCode": 0,
                "output": "managed\n",
                "undoAvailable": False,
            }

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            workspace_harness=WorkspaceHarness(executor=fake_execute),
        )
        automatic_approvals = []

        def auto_approve(approval):
            automatic_approvals.append(dict(approval))
            decided = self.store.decide_approval(
                str(approval["approvalId"]),
                approved=True,
                payload_sha256=str(approval["payloadSha256"]),
                decided_by="execution-policy:workspace_managed",
            )
            receipt = gateway.apply_approval(decided)
            return {
                "summary": receipt["summary"],
                "approvalRequired": False,
                "autoApproved": True,
                "approvalId": approval["approvalId"],
                "receipt": receipt,
            }

        gateway.bind_auto_approval_executor(auto_approve)
        patch_result = gateway.execute(
            {
                **self._tool_call(
                    "workspace_patch",
                    "apply",
                    path=str(target),
                    oldText="before",
                    newText="after",
                ),
                "sessionId": session["id"],
            }
        )["result"]
        shell_result = gateway.execute(
            {
                **self._tool_call(
                    "workspace_shell",
                    "run",
                    command="pwd",
                    cwd=str(workspace),
                ),
                "sessionId": session["id"],
            }
        )["result"]

        self.assertTrue(patch_result["autoApproved"])
        self.assertTrue(shell_result["autoApproved"])
        self.assertEqual(target.read_text(encoding="utf-8"), "value = 'after'\n")
        self.assertEqual(len(executed), 1)
        self.assertEqual(len(automatic_approvals), 2)

        with self.assertRaisesRegex(WorkspaceHarnessError, "outside"):
            gateway.execute(
                {
                    **self._tool_call(
                        "workspace_patch",
                        "apply",
                        path=str(outside_target),
                        oldText="outside",
                        newText="escaped",
                    ),
                    "sessionId": session["id"],
                }
            )
        with self.assertRaisesRegex(WorkspaceHarnessError, "system or privilege"):
            gateway.execute(
                {
                    **self._tool_call(
                        "workspace_shell",
                        "run",
                        command="sudo echo escaped",
                        cwd=str(workspace),
                    ),
                    "sessionId": session["id"],
                }
            )
        self.assertEqual(outside_target.read_text(encoding="utf-8"), "value = 'outside'\n")
        self.assertEqual(len(automatic_approvals), 2)

    def test_full_trust_model_arbitrates_every_gate_but_keeps_scope_and_system_fences(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-full-trust"
        outside = Path(self.tmp.name) / "outside-full-trust"
        workspace.mkdir()
        outside.mkdir()
        outside_target = outside / "outside.py"
        outside_target.write_text("value = 'outside'\n", encoding="utf-8")
        session = self.store.create(
            title="full trust coordinator",
            mode="coordinator",
            execution_mode="full_trust",
            workspace_roots=[str(workspace)],
            created_at_ms=22,
        )
        self._start_todo(str(session["id"]))
        auto_approvals = []

        def auto_approve(approval):
            auto_approvals.append(dict(approval))
            decided = self.store.decide_approval(
                str(approval["approvalId"]),
                approved=True,
                payload_sha256=str(approval["payloadSha256"]),
                decided_by="approval-model:test",
            )
            receipt = self.gateway.apply_approval(decided)
            return {
                "summary": receipt["summary"],
                "approvalRequired": False,
                "autoApproved": True,
                "approvalId": approval["approvalId"],
                "receipt": receipt,
                "modelDecided": True,
                "decisionMode": "model",
            }

        self.gateway.bind_auto_approval_executor(auto_approve)
        routine = self.gateway.execute(
            {
                **self._tool_call(
                    "planning",
                    "task_action",
                    taskId="task:1",
                    action="complete",
                    date="2026-07-13",
                ),
                "sessionId": session["id"],
            }
        )["result"]
        protected = self.gateway.execute(
            {
                **self._tool_call("runtime", "restart_sidecar"),
                "sessionId": session["id"],
            }
        )["result"]

        self.assertTrue(routine["autoApproved"])
        self.assertTrue(protected["autoApproved"])
        self.assertEqual(auto_approvals[1]["operation"], "restart_sidecar")
        with self.assertRaisesRegex(WorkspaceHarnessError, "outside"):
            self.gateway.execute(
                {
                    **self._tool_call(
                        "workspace_patch",
                        "apply",
                        path=str(outside_target),
                        oldText="outside",
                        newText="escaped",
                    ),
                    "sessionId": session["id"],
                }
            )
        with self.assertRaisesRegex(WorkspaceHarnessError, "system or privilege"):
            self.gateway.execute(
                {
                    **self._tool_call(
                        "workspace_shell",
                        "run",
                        command="sudo echo escaped",
                        cwd=str(workspace),
                    ),
                    "sessionId": session["id"],
                }
            )
        self.assertEqual(
            outside_target.read_text(encoding="utf-8"),
            "value = 'outside'\n",
        )
        self.assertEqual(len(auto_approvals), 2)
        applied = self.store.get_approval(str(routine["approvalId"]))
        self.assertEqual(applied["state"], "approved")
        self.assertEqual(applied["decidedBy"], "approval-model:test")

    def test_full_access_edits_outside_project_without_waiting_for_approval(self) -> None:
        workspace = Path(self.tmp.name) / "selected-project"
        outside = Path(self.tmp.name) / "outside-project"
        workspace.mkdir()
        outside.mkdir()
        target = outside / "note.txt"
        target.write_text("before\n", encoding="utf-8")
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]), mode="coordinator",
            tool_profile_version="control-center-full-access-v1",
            execution_mode="per_action", allowed_tools=None,
            workspace_roots=[str(workspace)],
        )

        def auto_approve(approval):
            decided = self.store.decide_approval(
                str(approval["approvalId"]), approved=True,
                payload_sha256=str(approval["payloadSha256"]),
                decided_by="execution-policy:full-access",
            )
            receipt = self.gateway.apply_approval(decided)
            return {"approvalRequired": False, "autoApproved": True, "receipt": receipt}

        self.gateway.bind_auto_approval_executor(auto_approve)
        response = self.gateway.execute(self._tool_call(
            "workspace_patch", "apply", path=str(target), oldText="before", newText="after",
        ))["result"]
        self.assertFalse(response["approvalRequired"])
        self.assertTrue(response["autoApproved"])
        self.assertEqual(target.read_text(encoding="utf-8"), "after\n")
        approvals = self.store.list_approvals(session_id=str(self.session["id"]))
        self.assertTrue(approvals)
        self.assertTrue(all(item["state"] != "pending" for item in approvals))

    def test_dangerous_profile_auto_approves_through_the_bound_service_bridge(self) -> None:
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-auto-approve-v1",
            allowed_tools=None,
            workspace_roots=[self.tmp.name],
        )
        received = []

        def auto_approve(approval):
            received.append(dict(approval))
            return {
                "summary": "已自动批准并完成任务",
                "approvalRequired": False,
                "autoApproved": True,
                "approvalId": approval["approvalId"],
            }

        self.gateway.bind_auto_approval_executor(auto_approve)
        response = self.gateway.execute(
            self._tool_call(
                "planning",
                "task_action",
                taskId="task:1",
                action="complete",
                date="2026-07-13",
            )
        )

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["state"], "pending")
        self.assertTrue(response["result"]["autoApproved"])
        self.assertFalse(response["result"]["approvalRequired"])

    def test_room_per_action_tool_uses_the_session_approval_only(self) -> None:
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="per_action",
            allowed_tools=None,
        )
        response = self.gateway.execute(
            {
                **self._tool_call(
                    "planning",
                    "task_action",
                    taskId="task:1",
                    action="complete",
                    date="2026-07-13",
                ),
                "toolCallId": "tool:room-planning",
                "loadReceiptId": "load:room-planning",
            }
        )

        self.assertTrue(response["result"]["approvalRequired"])
        self.assertNotIn("roomInvocationReceipt", response)
        self.assertNotIn("roomExecutionReceipt", response)
        approval = response["result"]["approval"]
        self.assertNotIn(
            "roomInvocationReceiptId",
            approval["preview"]["baseState"],
        )
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.assertTrue(receipt["mutationApplied"])
        self.assertNotIn("roomExecutionReceipt", receipt)

    def test_confirmed_room_unrestricted_executes_without_per_tool_prompt(self) -> None:
        workspace = Path(self.tmp.name) / "room-unrestricted"
        workspace.mkdir()
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="full_trust",
            allowed_tools=None,
            workspace_roots=[str(workspace)],
        )

        class _ConfirmedRoom:
            def _active_room_dispatch_context(
                self,
                _session_id: str,
            ) -> dict[str, object]:
                return {
                    "roomId": "room:confirmed",
                    "rootId": "root:confirmed",
                    "dispatchId": "dispatch:confirmed",
                    "generation": 1,
                }

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            collaboration=_ConfirmedRoom(),
        )

        def auto_approve(approval):
            self.assertTrue(approval["causalMetadata"]["roomBound"])
            self.assertNotIn("approvalArbitration", approval["preview"])
            decided = self.store.decide_approval(
                str(approval["approvalId"]),
                approved=True,
                payload_sha256=str(approval["payloadSha256"]),
                decided_by="execution-policy:room_unrestricted",
            )
            receipt = gateway.apply_approval(decided)
            return {
                "summary": receipt["summary"],
                "approvalRequired": False,
                "autoApproved": True,
                "approvalId": approval["approvalId"],
                "receipt": receipt,
            }

        gateway.bind_auto_approval_executor(auto_approve)
        response = gateway.execute(
            {
                **self._tool_call(
                    "planning",
                    "task_action",
                    taskId="task:1",
                    action="complete",
                    date="2026-07-13",
                ),
                "toolCallId": "tool:room-unrestricted-patch",
            }
        )

        self.assertTrue(response["result"]["autoApproved"])
        self.assertEqual(self.store.get(str(self.session["id"]))["roomExecutionMode"], "")

    def test_room_approval_is_exactly_bound_in_the_creation_transaction(self) -> None:
        """A crash after INSERT must not leave a fake human approval behind."""

        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="full_trust",
            grant_workspace_scope=True,
            allowed_tools=None,
            workspace_roots=[self.tmp.name],
        )
        room_context = {
            "roomId": "room:atomic-create",
            "rootId": "root:atomic-create",
            "dispatchId": "dispatch:atomic-create",
            "generation": 7,
        }

        class _LiveRoom:
            def _active_room_dispatch_context(
                self,
                _session_id: str,
            ) -> dict[str, object]:
                return dict(room_context)

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            collaboration=_LiveRoom(),
        )

        # This is the old crash seam: create_approval committed, then the
        # gateway performed two follow-up binding transactions. Interrupt the
        # very first compatibility bind and inspect the already-durable row.
        with patch.object(
            self.store,
            "bind_approval_tool_call",
            side_effect=RuntimeError("crash after approval insert"),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "crash after approval insert",
            ):
                gateway.execute(
                    {
                        **self._tool_call(
                            "planning",
                            "task_action",
                            taskId="task:1",
                            action="complete",
                            date="2026-07-13",
                        ),
                        "toolCallId": "tool:atomic-create",
                    }
                )

        approvals = self.store.list_approvals(
            session_id=str(self.session["id"]),
        )
        self.assertEqual(len(approvals), 1)
        approval = approvals[0]
        self.assertEqual(approval["toolCallId"], "tool:atomic-create")
        self.assertNotIn("approvalArbitration", approval["preview"])
        self.assertEqual(
            approval["causalMetadata"],
            {
                **approval["causalMetadata"],
                "roomBound": True,
                "roomId": room_context["roomId"],
                "rootId": room_context["rootId"],
                "dispatchId": room_context["dispatchId"],
                "generation": room_context["generation"],
            },
        )

    def test_incomplete_room_dispatch_fails_before_creating_an_approval(self) -> None:
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="per_action",
            allowed_tools=None,
        )

        class _IncompleteRoom:
            def __init__(self, context: dict[str, object]) -> None:
                self.context = context

            def _active_room_dispatch_context(
                self,
                _session_id: str,
            ) -> dict[str, object]:
                return dict(self.context)

        malformed = (
            {
                "roomId": "room:incomplete",
                "rootId": "root:incomplete",
                "dispatchId": "",
                "generation": 1,
            },
            {
                "roomId": "room:incomplete",
                "rootId": "root:incomplete",
                "dispatchId": "dispatch:incomplete",
                "generation": 0,
            },
        )
        for index, context in enumerate(malformed):
            with self.subTest(context=context):
                gateway = ControlToolGateway(
                    sessions=self.store,
                    management=self.management,
                    core=_Core(),
                    project="wisdom-weasel-rag-ime",
                    facade=self.facade,
                    collaboration=_IncompleteRoom(context),
                )
                auto_calls: list[dict[str, object]] = []
                gateway.bind_auto_approval_executor(
                    lambda approval, auto_calls=auto_calls: auto_calls.append(dict(approval)) or {}
                )

                with self.assertRaisesRegex(
                    ValueError,
                    "Room dispatch context is incomplete",
                ):
                    gateway.execute(
                        {
                            **self._tool_call(
                                "planning",
                                "task_action",
                                taskId="task:1",
                                action="complete",
                                date="2026-07-13",
                            ),
                            "toolCallId": f"tool:incomplete-room:{index}",
                        }
                    )

                self.assertEqual(auto_calls, [])
                self.assertEqual(
                    self.store.list_approvals(
                        session_id=str(self.session["id"]),
                    ),
                    [],
                )

    def test_stale_persisted_room_overlay_does_not_authorize_a_direct_session_tool(self) -> None:
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="per_action",
            allowed_tools=None,
        )
        self.store.set_room_execution_mode(
            str(self.session["id"]),
            ROOM_UNRESTRICTED_EXECUTION_MODE,
        )
        self.gateway.bind_auto_approval_executor(
            lambda _approval: (_ for _ in ()).throw(
                AssertionError("a direct Session Tool inherited stale Room authority")
            )
        )

        response = self.gateway.execute(
            {
                **self._tool_call(
                    "planning",
                    "task_action",
                    taskId="task:1",
                    action="complete",
                    date="2026-07-13",
                ),
                "toolCallId": "tool:direct-after-room",
            }
        )

        self.assertTrue(response["result"]["approvalRequired"])
        self.assertFalse(
            response["result"]["approval"]["causalMetadata"]["roomBound"]
        )

    def test_room_workspace_managed_auto_approval_uses_session_receipt(self) -> None:
        workspace = Path(self.tmp.name) / "room-managed"
        workspace.mkdir()
        target = workspace / "room.py"
        target.write_text("state = 'before'\n", encoding="utf-8")
        self.session = self.store.set_runtime_policy(
            str(self.session["id"]),
            mode="coordinator",
            tool_profile_version="control-center-v1",
            execution_mode="workspace_managed",
            grant_workspace_scope=True,
            allowed_tools=None,
            workspace_roots=[str(workspace)],
        )
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
        )

        def auto_approve(approval):
            decided = self.store.decide_approval(
                str(approval["approvalId"]),
                approved=True,
                payload_sha256=str(approval["payloadSha256"]),
                decided_by="execution-policy:workspace_managed",
            )
            receipt = gateway.apply_approval(decided)
            return {
                "summary": receipt["summary"],
                "approvalRequired": False,
                "autoApproved": True,
                "approvalId": approval["approvalId"],
                "receipt": receipt,
            }

        gateway.bind_auto_approval_executor(auto_approve)
        response = gateway.execute(
            {
                **self._tool_call(
                    "workspace_patch",
                    "apply",
                    path=str(target),
                    oldText="before",
                    newText="after",
                ),
                "toolCallId": "tool:room-managed-patch",
                "loadReceiptId": "load:room-managed-patch",
            }
        )

        self.assertTrue(response["result"]["autoApproved"])
        self.assertEqual(target.read_text(encoding="utf-8"), "state = 'after'\n")
        self.assertTrue(response["result"]["receipt"]["mutationApplied"])
        self.assertNotIn("roomExecutionReceipt", response)
        self.assertNotIn("roomInvocationReceipt", response)

    def test_legacy_room_bound_approval_must_be_retried_in_session(self) -> None:
        approval = self.store.create_approval(
            session_id=str(self.session["id"]),
            tool_name="workspace_shell",
            operation="run",
            payload_sha256="a" * 64,
            preview={
                "actionPayload": {},
                "baseState": {
                    "roomInvocationReceiptId": "invoke:legacy-room",
                },
            },
            risk_level="R2",
        )
        decided = self.store.decide_approval(
            str(approval["approvalId"]),
            approved=True,
            payload_sha256=str(approval["payloadSha256"]),
        )
        with self.assertRaisesRegex(ValueError, "legacy Room-bound"):
            self.gateway.apply_approval(decided)

    def test_coordinator_search_and_patch_require_native_hash_bound_approval(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-patch"
        workspace.mkdir()
        target = workspace / "main.py"
        target.write_text("print('before')\n", encoding="utf-8")
        coordinator = self.store.create(
            title="coordinator patch",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=3,
        )
        media = AgentMediaStore(
            Path(self.tmp.name) / "rag-ime.sqlite",
            root=Path(self.tmp.name) / "tool-media",
        )
        self.gateway.artifact_projector = AgentToolArtifactProjector(media)
        self._start_todo(str(coordinator["id"]))
        found = self.gateway.execute(
            {
                **self._tool_call("workspace_search", "search", query="before"),
                "sessionId": coordinator["id"],
            }
        )["result"]
        prepared = self.gateway.execute(
            {
                **self._tool_call(
                    "workspace_patch",
                    "apply",
                    path=str(target),
                    oldText="before",
                    newText="after",
                ),
                "sessionId": coordinator["id"],
            }
        )["result"]

        self.assertEqual(found["matches"][0]["lineNumber"], 1)
        self.assertEqual(target.read_text(encoding="utf-8"), "print('before')\n")
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.assertEqual(receipt["replacementCount"], 1)
        self.assertEqual(receipt["artifactProjection"], {"status": "available", "count": 2})
        self.assertEqual(
            [block["data"]["mimeType"] for block in receipt["agentBlocks"]],
            ["text/plain", "text/x-diff"],
        )
        self.assertEqual(target.read_text(encoding="utf-8"), "print('after')\n")
        self.assertEqual(
            self.store.agent_todo(str(coordinator["id"]))["counts"]["inProgress"],
            1,
        )

    def test_workspace_patch_fails_closed_if_file_changes_after_native_approval(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-stale"
        workspace.mkdir()
        target = workspace / "main.py"
        target.write_text("old\n", encoding="utf-8")
        coordinator = self.store.create(
            title="coordinator stale patch",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=4,
        )
        self._start_todo(str(coordinator["id"]))
        prepared = self.gateway.execute(
            {
                **self._tool_call(
                    "workspace_patch",
                    "apply",
                    path=str(target),
                    oldText="old",
                    newText="new",
                ),
                "sessionId": coordinator["id"],
            }
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        target.write_text("changed\n", encoding="utf-8")

        with self.assertRaisesRegex(WorkspaceHarnessError, "changed"):
            self.gateway.apply_approval(decided)
        self.assertEqual(target.read_text(encoding="utf-8"), "changed\n")
        self.assertEqual(
            self.store.agent_todo(str(coordinator["id"]))["counts"]["inProgress"],
            1,
        )

    def test_user_request_reaches_existing_action_approval_policy_without_todo(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-gated"
        workspace.mkdir()
        (workspace / "README.md").write_text("readable\n", encoding="utf-8")
        coordinator = self.store.create(
            title="coordinator gated",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=5,
        )
        listed = self.gateway.execute(
            {
                **self._tool_call("workspace_list", "list", path=str(workspace)),
                "sessionId": coordinator["id"],
            }
        )["result"]
        self.assertIn("README.md", str(listed))
        prepared = self.gateway.execute(
            {
                **self._tool_call(
                    "workspace_shell",
                    "run",
                    command="pwd",
                    cwd=str(workspace),
                ),
                "sessionId": coordinator["id"],
            }
        )["result"]
        self.assertTrue(prepared["approvalRequired"])
        self.assertEqual(prepared["approval"]["riskLevel"], "R2")
        todo = self.store.agent_todo(str(coordinator["id"]))
        self.assertEqual(todo["revision"], 0)
        self.assertTrue(
            self.store.workflow_state(str(coordinator["id"]))["actGate"]["allowed"]
        )

    def test_workspace_write_receipt_registers_hash_bound_work_document(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-work-document"
        workspace.mkdir()
        coordinator = self.store.create(
            title="work document coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=6,
        )
        session_id = str(coordinator["id"])
        self._start_todo(session_id)
        todo = self.store.agent_todo(session_id)
        documents = WorkDocumentService(
            self.store.db_path,
            sessions=self.store,
            context_runtime=AgentContextRuntime(self.store.db_path),
        )
        documents.initialize()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            work_documents=documents,
        )
        prepared = gateway.execute(
            {
                **self._tool_call(
                    "workspace_write",
                    "apply",
                    path="docs/work.md",
                    resourceRevision="missing",
                    content="# Canonical work\n",
                    workDocument={
                        "authorityKind": "session_todo",
                        "authorityId": session_id,
                        "authorityRevision": todo["revision"],
                        "title": "Canonical work",
                    },
                ),
                "sessionId": session_id,
            }
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        tampered = dict(decided)
        tampered_preview = dict(tampered["preview"])
        tampered_action = dict(tampered_preview["actionPayload"])
        tampered_binding = dict(tampered_action["workDocument"])
        tampered_binding["authorityRevision"] = int(todo["revision"]) + 1
        tampered_action["workDocument"] = tampered_binding
        tampered_preview["actionPayload"] = tampered_action
        tampered["preview"] = tampered_preview
        with self.assertRaisesRegex(ValueError, "payload no longer matches"):
            gateway.apply_approval(tampered)

        receipt = gateway.apply_approval(decided)
        registration = receipt["workDocumentRegistration"]
        self.assertEqual(registration["operation"], "register")
        self.assertEqual(registration["document"]["authorityId"], session_id)
        self.assertEqual(registration["document"]["contentSha256"], receipt["postimageSha256"])
        listed = gateway.execute(
            {
                **self._tool_call("work_documents", "list", limit=10),
                "sessionId": session_id,
            }
        )["result"]
        self.assertEqual(listed["schemaVersion"], "rag-ime.work-document-list.v1")
        self.assertEqual(listed["total"], 1)
        self.assertEqual(
            gateway.execute(
                {
                    **self._tool_call(
                        "work_documents",
                        "get",
                        documentId=registration["document"]["documentId"],
                    ),
                    "sessionId": session_id,
                }
            )["result"]["document"]["documentId"],
            registration["document"]["documentId"],
        )

    def test_workspace_write_apply_rebinds_after_open_authority_advances(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-work-document-rebind"
        workspace.mkdir()
        coordinator = self.store.create(
            title="work document rebind coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=9,
        )
        session_id = str(coordinator["id"])
        todo = self._start_todo(session_id)
        documents = WorkDocumentService(
            self.store.db_path,
            sessions=self.store,
            context_runtime=AgentContextRuntime(self.store.db_path),
        )
        documents.initialize()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            work_documents=documents,
        )
        prepared = gateway.execute(
            {
                **self._tool_call(
                    "workspace_write",
                    "apply",
                    path="docs/work.md",
                    resourceRevision="missing",
                    content="# Canonical work\n",
                    workDocument={
                        "authorityKind": "session_todo",
                        "authorityId": session_id,
                        "authorityRevision": todo["revision"],
                        "title": "Canonical work",
                    },
                ),
                "sessionId": session_id,
            }
        )["result"]
        first = gateway.apply_approval(
            self.store.decide_approval(
                prepared["approval"]["approvalId"],
                approved=True,
                payload_sha256=prepared["approval"]["payloadSha256"],
            )
        )
        canonical = str(first["workDocumentRegistration"]["document"]["path"])
        bound_revision = int(
            first["workDocumentRegistration"]["document"]["authorityRevision"]
        )
        self.store.mutate_agent_todo(
            session_id,
            {"op": "append", "phase": "受控工作区执行", "items": ["继续绑定写回"]},
            actor="test-user",
        )
        live = documents.authority_context("session_todo", session_id)
        self.assertGreater(int(live["authorityRevision"]), bound_revision)
        rewritten = gateway.execute(
            {
                **self._tool_call(
                    "workspace_write",
                    "apply",
                    path=canonical,
                    resourceRevision="sha256:" + str(first["postimageSha256"]),
                    content="# Canonical work\n\nrebound\n",
                    workDocument={
                        "authorityKind": "session_todo",
                        "authorityId": session_id,
                        "authorityRevision": bound_revision,
                        "title": "Canonical work",
                    },
                ),
                "sessionId": session_id,
            }
        )["result"]
        receipt = gateway.apply_approval(
            self.store.decide_approval(
                rewritten["approval"]["approvalId"],
                approved=True,
                payload_sha256=rewritten["approval"]["payloadSha256"],
            )
        )
        self.assertEqual(
            int(receipt["workDocumentRegistration"]["document"]["authorityRevision"]),
            int(live["authorityRevision"]),
        )
        self.assertEqual(
            receipt["workDocumentRegistration"]["document"]["state"],
            "active",
        )

    def test_workspace_write_keeps_real_write_when_work_document_registration_fails(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-work-document-rollback"
        workspace.mkdir()
        coordinator = self.store.create(
            title="work document rollback coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=7,
        )
        session_id = str(coordinator["id"])

        class _FailingWorkDocuments:
            def preflight_register(self, _payload):
                return None

            def register(self, _payload):
                raise RuntimeError("authority advanced after preflight")

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            work_documents=_FailingWorkDocuments(),
        )
        target = workspace / "docs" / "worker.md"
        prepared = gateway.execute(
            {
                **self._tool_call(
                    "workspace_write",
                    "apply",
                    path=str(target),
                    resourceRevision="missing",
                    content="# Worker opening\n",
                    workDocument={
                        "authorityKind": "room_work_item",
                        "authorityId": "room-work:test",
                        "authorityRevision": 2,
                        "title": "Worker",
                    },
                ),
                "sessionId": session_id,
            }
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )

        receipt = gateway.apply_approval(decided)

        self.assertTrue(target.exists())
        self.assertEqual(receipt["documentSync"]["state"], "failed")
        self.assertEqual(receipt["documentSync"]["attemptCount"], 1)
        self.assertFalse(receipt["documentSync"]["retryable"])
        self.assertIn("authority advanced", receipt["documentSync"]["reason"])
        self.assertTrue(str(receipt["documentSync"]["traceId"]).startswith("trace:work-document-sync:"))

    def test_workspace_write_records_archived_document_failure_without_raising(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-work-document-archived"
        workspace.mkdir()
        coordinator = self.store.create(
            title="archived document coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=9,
        )
        session_id = str(coordinator["id"])

        class _ArchivedWorkDocuments:
            def preflight_register(self, _payload):
                raise RuntimeError(
                    "WorkDocumentError: archived document must be reopened through its authority"
                )

            def register(self, _payload):
                raise AssertionError("preflight failure must be the only document attempt")

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            work_documents=_ArchivedWorkDocuments(),
        )
        target = workspace / "docs" / "worker.md"
        prepared = gateway.execute(
            {
                **self._tool_call(
                    "workspace_write",
                    "apply",
                    path=str(target),
                    resourceRevision="missing",
                    content="# Worker opening\n",
                    workDocument={
                        "authorityKind": "room_work_item",
                        "authorityId": "room-work:archived",
                        "authorityRevision": 2,
                    },
                ),
                "sessionId": session_id,
            }
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )

        receipt = gateway.apply_approval(decided)

        self.assertTrue(target.exists())
        self.assertEqual(receipt["documentSync"]["state"], "failed")
        self.assertEqual(receipt["documentSync"]["attemptCount"], 1)
        self.assertIn(
            "archived document must be reopened through its authority",
            receipt["documentSync"]["reason"],
        )

    def test_workspace_write_rejects_non_active_work_document_receipt(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-work-document-rejected"
        workspace.mkdir()
        coordinator = self.store.create(
            title="work document rejected coordinator",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=8,
        )

        class _RejectedWorkDocuments:
            def preflight_register(self, _payload):
                return None

            def register(self, _payload):
                return {
                    "receipt": {"status": "failed"},
                    "document": {"state": "error"},
                }

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            work_documents=_RejectedWorkDocuments(),
        )
        target = workspace / "docs" / "worker.md"
        prepared = gateway.execute(
            {
                **self._tool_call(
                    "workspace_write",
                    "apply",
                    path=str(target),
                    resourceRevision="missing",
                    content="# Worker opening\n",
                    workDocument={
                        "authorityKind": "room_work_item",
                        "authorityId": "room-work:test",
                        "authorityRevision": 2,
                    },
                ),
                "sessionId": coordinator["id"],
            }
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )

        receipt = gateway.apply_approval(decided)

        self.assertTrue(target.exists())
        self.assertEqual(receipt["documentSync"]["state"], "failed")
        self.assertEqual(receipt["documentSync"]["receiptStatus"], "failed")
        self.assertEqual(receipt["documentSync"]["documentState"], "error")

    def test_workspace_harness_failure_keeps_native_approval_out_of_execution(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-failing"
        workspace.mkdir()
        coordinator = self.store.create(
            title="coordinator failing",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=6,
        )
        self._start_todo(str(coordinator["id"]))

        def fail_execute(_prepared):
            raise WorkspaceHarnessError("runner unavailable")

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            workspace_harness=WorkspaceHarness(executor=fail_execute),
        )
        prepared = gateway.execute(
            {
                **self._tool_call(
                    "workspace_shell",
                    "run",
                    command="pwd",
                    cwd=str(workspace),
                ),
                "sessionId": coordinator["id"],
            }
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )

        with self.assertRaisesRegex(WorkspaceHarnessError, "runner unavailable"):
            gateway.apply_approval(decided)
        self.assertEqual(
            self.store.agent_todo(str(coordinator["id"]))["counts"]["inProgress"],
            1,
        )

    def test_failed_timeout_or_limited_shell_receipt_does_not_change_todo_authority(self) -> None:
        cases = (
            {"mutationApplied": False, "exitCode": 1, "timedOut": False, "outputLimited": False},
            {"mutationApplied": False, "exitCode": -15, "timedOut": True, "outputLimited": False},
            {"mutationApplied": False, "exitCode": -15, "timedOut": False, "outputLimited": True},
        )
        for index, receipt_fields in enumerate(cases):
            with self.subTest(receipt=receipt_fields):
                workspace = Path(self.tmp.name) / f"workspace-unsuccessful-{index}"
                workspace.mkdir()
                coordinator = self.store.create(
                    title=f"coordinator unsuccessful {index}",
                    mode="coordinator",
                    workspace_roots=[str(workspace)],
                    created_at_ms=10 + index,
                )
                self._start_todo(str(coordinator["id"]))

                def execute(_prepared, fields=receipt_fields):
                    return {
                        "schemaVersion": "rag-ime.workspace-command-receipt.v1",
                        "summary": "command did not complete successfully",
                        "output": "",
                        "undoAvailable": False,
                        **fields,
                    }

                gateway = ControlToolGateway(
                    sessions=self.store,
                    management=self.management,
                    core=_Core(),
                    project="wisdom-weasel-rag-ime",
                    facade=_Facade(),
                    workspace_harness=WorkspaceHarness(executor=execute),
                )
                prepared = gateway.execute(
                    {
                        **self._tool_call(
                            "workspace_shell",
                            "run",
                            command="false",
                            cwd=str(workspace),
                        ),
                        "sessionId": coordinator["id"],
                    }
                )["result"]
                approval = prepared["approval"]
                decided = self.store.decide_approval(
                    approval["approvalId"],
                    approved=True,
                    payload_sha256=approval["payloadSha256"],
                )

                result = gateway.apply_approval(decided)
                self.assertFalse(result["mutationApplied"])
                self.assertEqual(
                    self.store.agent_todo(str(coordinator["id"]))["counts"]["inProgress"],
                    1,
                )

    def test_task_action_requires_native_approval_then_returns_rollback_receipt(self) -> None:
        prepared = self.gateway.execute(
            self._tool_call(
                "planning",
                "task_action",
                taskId="task:1",
                date="2026-07-13",
                action="complete",
            )
        )["result"]
        approval = prepared["approval"]

        self.assertTrue(prepared["approvalRequired"])
        self.assertEqual(self.management.tasks[0]["status"], "todo")
        self.assertEqual(approval["preview"]["changes"][0]["after"], "已完成")

        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)

        self.assertTrue(receipt["mutationApplied"])
        self.assertTrue(receipt["undoAvailable"])
        self.assertEqual(receipt["rollback"]["operation"], "undo_task_event")
        self.assertEqual(self.management.tasks[0]["status"], "done")

    def test_memory_preview_apply_and_rollback_use_native_approval_and_revision_hash(self) -> None:
        preview = self.gateway.execute(
            self._call(
                "maintenance_preview",
                trigger="explicit_request",
                instruction="整理本次 Pi 会话的最终事实",
            )
        )["result"]
        self.assertTrue(preview["reviewRequired"])
        self.assertTrue(preview["needsReview"])
        self.assertEqual(preview["runId"], "memory_book_draft")
        self.assertEqual(preview["counts"]["upsert_memory_book"], 1)
        self.assertNotIn("run", preview)
        self.assertEqual(self.facade.memory_run_status, "draft")

        review = self.gateway.execute(
            self._call("maintenance_review", runId="memory_book_draft")
        )["result"]
        self.assertTrue(review["reviewRequired"])
        self.assertTrue(review["canApply"])
        self.assertEqual(review["diffCount"], 3)
        self.assertNotIn("changes", review)
        self.assertNotIn("run", review)

        prepared = self.gateway.execute(
            self._call("maintenance_apply", runId="memory_book_draft")
        )["result"]
        approval = prepared["approval"]
        self.assertTrue(prepared["approvalRequired"])
        self.assertEqual(approval["preview"]["changes"][0]["after"], "已应用")
        self.assertIn("Pi 控制中心", [item["label"] for item in approval["preview"]["changes"]])
        self.assertEqual(self.facade.memory_run_status, "draft")

        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        applied = self.gateway.apply_approval(decided)
        self.store.complete_approval(approval["approvalId"], state="applied", receipt=applied)
        self.assertEqual(self.facade.memory_run_status, "applied")
        self.assertTrue(applied["undoAvailable"])
        self.assertEqual(applied["diffCount"], 3)

        rollback = self.gateway.execute(
            self._call("maintenance_rollback", runId="memory_book_draft")
        )["result"]["approval"]
        rollback_decided = self.store.decide_approval(
            rollback["approvalId"],
            approved=True,
            payload_sha256=rollback["payloadSha256"],
        )
        rolled_back = self.gateway.apply_approval(rollback_decided)
        self.assertEqual(self.facade.memory_run_status, "rolled_back")
        self.assertEqual(rolled_back["revertedRunId"], "memory_book_draft")
        self.assertFalse(rolled_back["undoAvailable"])

    def test_memory_curation_prepare_returns_compact_no_change_receipt(self) -> None:
        self.facade.memory_run_status = "empty"

        result = self.gateway.execute(
            self._call(
                "curation_prepare",
                trigger="task_completion",
                scope="incremental",
                policy="conservative",
            )
        )["result"]

        self.assertEqual(result["runId"], "memory_book_draft")
        self.assertEqual(result["counts"], {})
        self.assertEqual(result["diffCount"], 0)
        self.assertFalse(result["needsReview"])
        self.assertFalse(result["reviewRequired"])
        self.assertFalse(result["storedDraft"])
        self.assertNotIn("run", result)
        self.assertNotIn("changes", result)
        request = next(
            payload
            for operation, payload in self.facade.memory_maintenance_requests
            if operation == "prepare"
        )
        self.assertEqual(
            (request["ownerKind"], request["ownerId"]),
            ("user", "default"),
        )
        self.assertEqual(request["trigger"], "task_completion")

    def test_memory_curation_prepare_does_not_report_completion_with_backlog(self) -> None:
        self.facade.memory_run_status = "empty"
        self.facade.memory_prepare_pending_count = 3
        self.facade.memory_prepare_batch_count = 8
        self.facade.memory_prepare_drain_limited = True

        result = self.gateway.execute(
            self._call(
                "curation_prepare",
                trigger="explicit_request",
                scope="incremental",
                policy="conservative",
            )
        )["result"]

        self.assertEqual(result["pendingSourceCount"], 3)
        self.assertEqual(result["batchCount"], 8)
        self.assertTrue(result["drainLimited"])
        self.assertIn("仍有 3 条", result["summary"])
        self.assertNotIn("全部新增证据已完成整理", result["summary"])

    def test_memory_curation_prepare_rejects_ordinary_chat_without_completion_trigger(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "requires trigger"):
            self.gateway.execute(self._call("curation_prepare"))
        with self.assertRaisesRegex(ValueError, "requires trigger"):
            self.gateway.execute(
                self._call("curation_prepare", trigger="ordinary_chat")
            )
        self.assertEqual(self.facade.memory_maintenance_requests, [])

    def test_memory_curation_prepare_treats_no_sources_as_success(self) -> None:
        self.facade.memory_prepare_no_run = True

        result = self.gateway.execute(
            self._call(
                "curation_prepare",
                trigger="explicit_request",
                scope="incremental",
                policy="conservative",
            )
        )["result"]

        self.assertEqual(result["runId"], "")
        self.assertEqual(result["diffCount"], 0)
        self.assertFalse(result["needsReview"])
        self.assertFalse(result["reviewRequired"])
        self.assertFalse(result["storedDraft"])
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "no_sources")

    def test_memory_apply_fails_closed_when_draft_changes_after_preview(self) -> None:
        prepared = self.gateway.execute(
            self._call("maintenance_apply", runId="memory_book_draft")
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        self.facade.memory_run_status = "applied"

        with self.assertRaisesRegex(ValueError, "changed after"):
            self.gateway.apply_approval(decided)

    def test_task_action_fails_closed_when_task_changes_after_preview(self) -> None:
        prepared = self.gateway.execute(
            self._tool_call(
                "planning",
                "task_action",
                taskId="task:1",
                date="2026-07-13",
                action="start",
            )
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        self.management.tasks[0]["updatedAtMs"] += 1

        with self.assertRaisesRegex(ValueError, "changed after"):
            self.gateway.apply_approval(decided)
        self.assertEqual(self.management.tasks[0]["status"], "todo")

    def test_input_settings_preview_apply_and_rollback_are_hash_bound(self) -> None:
        changes = [
            {"key": "interaction.postCommit.idleTriggerMs", "value": 650},
            {"key": "pinyin.pairs.nL", "value": True},
        ]
        preview = self.gateway.execute(
            self._tool_call("input", "preview_settings", changes=changes)
        )["result"]
        self.assertEqual(preview["changeCount"], 2)
        self.assertTrue(preview["approvalRequiredForApply"])

        prepared = self.gateway.execute(
            self._tool_call("input", "apply_settings", changes=changes)
        )["result"]
        approval = prepared["approval"]
        self.assertEqual(self.facade.settings_payload["pinyin"]["pairs"]["nL"], False)
        self.assertEqual(approval["preview"]["changes"][0]["before"], 420)

        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.store.complete_approval(approval["approvalId"], state="applied", receipt=receipt)
        self.assertEqual(self.facade.settings_payload["interaction"]["postCommit"]["idleTriggerMs"], 650)
        self.assertEqual(self.facade.settings_payload["pinyin"]["pairs"]["nL"], True)
        self.assertTrue(receipt["undoAvailable"])

        rollback = self.gateway.execute(
            self._tool_call(
                "input",
                "rollback_settings",
                sourceApprovalId=approval["approvalId"],
            )
        )["result"]["approval"]
        rollback_decided = self.store.decide_approval(
            rollback["approvalId"],
            approved=True,
            payload_sha256=rollback["payloadSha256"],
        )
        rollback_receipt = self.gateway.apply_approval(rollback_decided)
        self.assertEqual(self.facade.settings_payload["interaction"]["postCommit"]["idleTriggerMs"], 420)
        self.assertEqual(self.facade.settings_payload["pinyin"]["pairs"]["nL"], False)
        self.assertEqual(rollback_receipt["revertedSettingsApprovalId"], approval["approvalId"])

    def test_input_settings_apply_fails_closed_after_any_settings_revision_change(self) -> None:
        prepared = self.gateway.execute(
            self._tool_call(
                "input",
                "apply_settings",
                changes=[{"key": "display.fadeAnimation", "value": False}],
            )
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        self.facade.settings_revision += 1

        with self.assertRaisesRegex(ValueError, "changed after"):
            self.gateway.apply_approval(decided)
        self.assertTrue(self.facade.settings_payload["display"]["fadeAnimation"])

    def test_lexicon_apply_hides_review_token_redeploys_and_can_rollback(self) -> None:
        review = self.gateway.execute(
            self._tool_call("input", "lexicon_review")
        )["result"]
        review_key = review["entries"][0]["reviewKey"]
        self.assertNotIn("must-not-reach-pi", str(review))

        prepared = self.gateway.execute(
            self._tool_call(
                "input",
                "lexicon_apply",
                selectedKeys=[review_key],
            )
        )["result"]
        approval = prepared["approval"]
        self.assertNotIn("must-not-reach-pi", str(prepared))
        self.assertFalse(self.facade.lexicon_applied)

        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.store.complete_approval(approval["approvalId"], state="applied", receipt=receipt)
        self.assertTrue(self.facade.lexicon_applied)
        self.assertEqual(receipt["deploymentStatus"], "succeeded")
        self.assertTrue(receipt["undoAvailable"])

        rollback = self.gateway.execute(
            self._tool_call(
                "input",
                "lexicon_rollback",
                sourceApprovalId=approval["approvalId"],
            )
        )["result"]["approval"]
        rollback_decided = self.store.decide_approval(
            rollback["approvalId"],
            approved=True,
            payload_sha256=rollback["payloadSha256"],
        )
        rollback_receipt = self.gateway.apply_approval(rollback_decided)
        self.assertFalse(self.facade.lexicon_applied)
        self.assertEqual(rollback_receipt["revertedLexiconApprovalId"], approval["approvalId"])
        self.assertFalse(rollback_receipt["undoAvailable"])

    def test_runtime_pause_resume_and_restart_require_native_approval(self) -> None:
        pause = self.gateway.execute(
            self._tool_call("runtime", "pause_ai")
        )["result"]["approval"]
        self.assertEqual(pause["riskLevel"], "R1")
        self.assertFalse(self.management.ai_paused)
        pause_decided = self.store.decide_approval(
            pause["approvalId"],
            approved=True,
            payload_sha256=pause["payloadSha256"],
        )
        paused = self.gateway.apply_approval(pause_decided)
        self.assertTrue(paused["mutationApplied"])
        self.assertTrue(paused["aiPaused"])
        self.assertTrue(self.management.ai_paused)

        resume = self.gateway.execute(
            self._tool_call("runtime", "resume_ai")
        )["result"]["approval"]
        resume_decided = self.store.decide_approval(
            resume["approvalId"],
            approved=True,
            payload_sha256=resume["payloadSha256"],
        )
        resumed = self.gateway.apply_approval(resume_decided)
        self.assertTrue(resumed["mutationApplied"])
        self.assertFalse(resumed["aiPaused"])

        restart = self.gateway.execute(
            self._tool_call("runtime", "restart_predictor")
        )["result"]["approval"]
        self.assertEqual(restart["riskLevel"], "R2")
        restart_decided = self.store.decide_approval(
            restart["approvalId"],
            approved=True,
            payload_sha256=restart["payloadSha256"],
        )
        restarted = self.gateway.apply_approval(restart_decided)
        self.assertTrue(restarted["mutationApplied"])
        self.assertEqual(restarted["status"], "succeeded")

        sidecar = self.gateway.execute(
            self._tool_call("runtime", "restart_sidecar")
        )["result"]["approval"]
        self.assertEqual(sidecar["riskLevel"], "R2")
        sidecar_decided = self.store.decide_approval(
            sidecar["approvalId"],
            approved=True,
            payload_sha256=sidecar["payloadSha256"],
        )
        external = self.gateway.apply_approval(sidecar_decided)
        self.assertFalse(external["mutationApplied"])
        self.assertTrue(external["externalActionPending"])
        self.assertEqual(external["externalAction"], "restart_sidecar")
        self.assertEqual(external["status"], "external-supervisor-required")
        self.assertEqual(
            external["externalCommand"],
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.rag-ime.sidecar"],
        )
        self.assertEqual(len(external["externalCommandSha256"]), 64)

    def test_runtime_action_fails_closed_when_runtime_revision_changes(self) -> None:
        prepared = self.gateway.execute(
            self._tool_call("runtime", "restart_predictor")
        )["result"]["approval"]
        decided = self.store.decide_approval(
            prepared["approvalId"],
            approved=True,
            payload_sha256=prepared["payloadSha256"],
        )
        self.management.runtime_revision += 1
        with self.assertRaisesRegex(ValueError, "changed after"):
            self.gateway.apply_approval(decided)

    def test_model_profile_preview_apply_and_rollback_preserve_secrets(self) -> None:
        profiles = self.gateway.execute(
            self._tool_call("models", "profiles")
        )["result"]
        self.assertEqual(profiles["profiles"]["instant"]["provider"], "mlx")
        self.assertFalse(profiles["secretsVisible"])

        requested = {
            "slot": "instant",
            "provider": "ollama",
            "endpoint": "http://127.0.0.1:11434/v1",
            "model": "qwen3:0.6b",
        }
        preview = self.gateway.execute(
            self._tool_call("models", "profile_preview", **requested)
        )["result"]
        self.assertEqual(preview["restartComponent"], "predictor")
        self.assertTrue(preview["secretsPreserved"])
        self.assertEqual(len(preview["changes"]), 3)

        prepared = self.gateway.execute(
            self._tool_call("models", "profile_apply", **requested)
        )["result"]
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.store.complete_approval(approval["approvalId"], state="applied", receipt=receipt)
        self.assertEqual(self.management.provider_profiles["instant"]["provider"], "ollama")
        self.assertEqual(receipt["activationStatus"], "succeeded")
        self.assertTrue(receipt["secretsPreserved"])

        rollback = self.gateway.execute(
            self._tool_call(
                "models",
                "profile_rollback",
                sourceApprovalId=approval["approvalId"],
            )
        )["result"]["approval"]
        rollback_decided = self.store.decide_approval(
            rollback["approvalId"],
            approved=True,
            payload_sha256=rollback["payloadSha256"],
        )
        rolled_back = self.gateway.apply_approval(rollback_decided)
        self.assertEqual(self.management.provider_profiles["instant"]["provider"], "mlx")
        self.assertEqual(rolled_back["revertedModelProfileApprovalId"], approval["approvalId"])

    def test_voice_provider_switch_and_rollback_only_change_the_provider_choice(self) -> None:
        preview = self.gateway.execute(
            self._tool_call(
                "voice",
                "provider_preview",
                provider="realtime_websocket",
            )
        )["result"]
        self.assertTrue(preview["approvalRequiredForApply"])
        self.assertTrue(preview["secretsPreserved"])

        prepared = self.gateway.execute(
            self._tool_call(
                "voice",
                "provider_apply",
                provider="realtime_websocket",
            )
        )["result"]["approval"]
        decided = self.store.decide_approval(
            prepared["approvalId"],
            approved=True,
            payload_sha256=prepared["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.store.complete_approval(prepared["approvalId"], state="applied", receipt=receipt)
        self.assertEqual(self.management.provider_profiles["voice"]["provider"], "realtime_websocket")
        self.assertEqual(receipt["activationStatus"], "pending_external_restart")
        self.assertTrue(receipt["secretsPreserved"])

        rollback = self.gateway.execute(
            self._tool_call(
                "voice",
                "provider_rollback",
                sourceApprovalId=prepared["approvalId"],
            )
        )["result"]["approval"]
        rollback_decided = self.store.decide_approval(
            rollback["approvalId"],
            approved=True,
            payload_sha256=rollback["payloadSha256"],
        )
        rolled_back = self.gateway.apply_approval(rollback_decided)
        self.assertEqual(self.management.provider_profiles["voice"]["provider"], "native_streaming")
        self.assertEqual(rolled_back["revertedVoiceProviderApprovalId"], prepared["approvalId"])

        with self.assertRaisesRegex(ValueError, "unsupported voice provider field"):
            self.gateway.execute(
                self._tool_call(
                    "voice",
                    "provider_preview",
                    provider="http_transcription",
                    endpoint="https://must-not-enter-agent-tool.example/v1",
                )
            )

    def test_knowledge_provider_save_is_honest_about_pending_sidecar_restart(self) -> None:
        requested = {
            "slot": "knowledge",
            "provider": "openai-compatible",
            "endpoint": "https://models.example.test/v1",
            "model": "knowledge-v2",
        }
        prepared = self.gateway.execute(
            self._tool_call("models", "profile_apply", **requested)
        )["result"]["approval"]
        decided = self.store.decide_approval(
            prepared["approvalId"],
            approved=True,
            payload_sha256=prepared["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.assertTrue(receipt["mutationApplied"])
        self.assertEqual(receipt["activationStatus"], "pending_external_restart")
        self.assertIn("外部 Supervisor", receipt["summary"])

        with self.assertRaisesRegex(ValueError, "unsupported model profile field"):
            self.gateway.execute(
                self._tool_call(
                    "models",
                    "profile_preview",
                    **requested,
                    apiKey="must-not-enter-pi-tool",
                )
            )

    def test_model_profile_apply_fails_closed_after_configuration_change(self) -> None:
        prepared = self.gateway.execute(
            self._tool_call(
                "models",
                "profile_apply",
                slot="instant",
                provider="ollama",
                endpoint="http://127.0.0.1:11434/v1",
                model="qwen3:0.6b",
            )
        )["result"]["approval"]
        decided = self.store.decide_approval(
            prepared["approvalId"],
            approved=True,
            payload_sha256=prepared["payloadSha256"],
        )
        self.management.provider_configuration_revision += 1
        with self.assertRaisesRegex(ValueError, "changed after"):
            self.gateway.apply_approval(decided)

    def test_configuration_export_and_restore_are_hash_bound_and_supervised(self) -> None:
        preview = self.gateway.execute(
            self._tool_call("configuration", "export_preview")
        )["result"]
        self.assertFalse(preview["secretsIncluded"])
        self.assertTrue(preview["approvalRequiredForExport"])

        prepared = self.gateway.execute(
            self._tool_call("configuration", "export")
        )["result"]
        approval = prepared["approval"]
        backup_root = Path(os.environ["RAG_IME_APP_SUPPORT_DIR"]) / "Backups"
        self.assertFalse(backup_root.exists())
        self.assertNotIn(str(Path.home()), str(prepared))

        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)
        self.store.complete_approval(approval["approvalId"], state="applied", receipt=receipt)

        target = Path(os.environ["RAG_IME_APP_SUPPORT_DIR"]) / str(
            receipt["managedRelativePath"]
        )
        self.assertTrue(target.is_file())
        self.assertFalse(receipt["secretsIncluded"])
        self.assertNotIn(str(Path.home()), str(receipt))

        restore = self.gateway.execute(
            self._tool_call(
                "configuration",
                "restore_preview",
                sourceApprovalId=approval["approvalId"],
            )
        )["result"]
        self.assertTrue(restore["valid"])
        self.assertTrue(restore["requiresRestart"])
        self.assertTrue(restore["restoreApplyAvailable"])
        self.assertEqual(restore["restoreApplyRisk"], "R3")
        self.assertTrue(restore["externalSupervisorRequired"])
        self.assertNotIn("restoreToken", str(restore))
        self.assertNotIn(str(Path.home()), str(restore))

        restore_prepared = self.gateway.execute(
            self._tool_call(
                "configuration",
                "restore_apply",
                sourceApprovalId=approval["approvalId"],
            )
        )["result"]
        restore_approval = restore_prepared["approval"]
        self.assertEqual(restore_approval["riskLevel"], "R3")
        self.assertNotIn("restoreToken", str(restore_prepared))

        restore_decided = self.store.decide_approval(
            restore_approval["approvalId"],
            approved=True,
            payload_sha256=restore_approval["payloadSha256"],
        )
        restore_receipt = self.gateway.apply_approval(restore_decided)
        self.assertFalse(restore_receipt["mutationApplied"])
        self.assertTrue(restore_receipt["externalActionPending"])
        self.assertEqual(restore_receipt["externalAction"], "restore_backup")
        self.assertEqual(restore_receipt["status"], "external-supervisor-required")

    def test_applied_task_receipt_can_prepare_and_execute_one_approved_undo(self) -> None:
        prepared = self.gateway.execute(
            self._tool_call(
                "planning",
                "task_action",
                taskId="task:1",
                date="2026-07-13",
                action="complete",
            )
        )["result"]
        action_approval = prepared["approval"]
        decided = self.store.decide_approval(
            action_approval["approvalId"],
            approved=True,
            payload_sha256=action_approval["payloadSha256"],
        )
        action_receipt = self.gateway.apply_approval(decided)
        self.store.complete_approval(
            action_approval["approvalId"], state="applied", receipt=action_receipt
        )

        undo = self.gateway.execute(
            self._tool_call(
                "planning",
                "undo_task_event",
                eventId=action_receipt["taskEventId"],
            )
        )["result"]["approval"]
        undo_decided = self.store.decide_approval(
            undo["approvalId"],
            approved=True,
            payload_sha256=undo["payloadSha256"],
        )
        undo_receipt = self.gateway.apply_approval(undo_decided)

        self.assertEqual(self.management.tasks[0]["status"], "todo")
        self.assertEqual(undo_receipt["revertedTaskEventId"], action_receipt["taskEventId"])
        self.assertFalse(undo_receipt["undoAvailable"])

        self.store.complete_approval(undo["approvalId"], state="applied", receipt=undo_receipt)
        with self.assertRaisesRegex(ValueError, "already been rolled back"):
            self.gateway.execute(
                self._tool_call(
                    "planning",
                    "undo_task_event",
                    eventId=action_receipt["taskEventId"],
                )
            )

    def test_overview_planning_document_knowledge_and_models_use_scoped_services(self) -> None:
        overview = self.gateway.execute(self._tool_call("overview", "status"))["result"]
        planning = self.gateway.execute(self._tool_call("planning", "dashboard"))["result"]
        bases = self.gateway.execute(self._tool_call("knowledge", "list_bases"))["result"]
        knowledge = self.gateway.execute(
            self._tool_call(
                "knowledge",
                "search",
                kbId="kb:project-docs",
                query="为什么普通生成不经过 Pi",
                topK=6,
            )
        )["result"]
        models = self.gateway.execute(self._tool_call("models", "status"))["result"]

        self.assertEqual(overview["unhealthyComponents"], ["predictor"])
        self.assertIn("1 个未完成任务", planning["summary"])
        self.assertEqual(bases["items"][0]["kbId"], "kb:project-docs")
        self.assertIn("Pi 只负责 Agent Loop", knowledge["items"][0]["content"])
        self.assertNotIn("/private/project", str(knowledge))
        self.assertEqual(self.knowledge.calls[-1][0], "search")
        self.assertNotIn("mode", self.knowledge.calls[-1][1])
        self.assertEqual(6, self.knowledge.calls[-1][1]["topK"])
        self.assertNotIn("searchMode", self.knowledge.calls[-1][1])
        self.assertIn("深度知识模型当前不可用", models["summary"])

    def test_document_knowledge_search_normalizes_mode_and_preserves_file_name_scope(self) -> None:
        self.gateway.execute(
            self._tool_call(
                "knowledge",
                "search",
                kbId="kb:project-docs",
                query="Agent Loop",
                searchMode="dense",
                fileName="architecture.md",
            )
        )
        operation, payload = self.knowledge.calls[-1]
        self.assertEqual("search", operation)
        self.assertEqual("dense", payload["mode"])
        self.assertEqual("architecture.md", payload["fileName"])

        self.gateway.execute(
            self._tool_call(
                "knowledge",
                "open",
                kbId="kb:project-docs",
                chunkId="chunk:grounding",
                before=2,
                after=3,
            )
        )
        operation, payload = self.knowledge.calls[-1]
        self.assertEqual("open", operation)
        self.assertEqual(
            {
                "kbId": "kb:project-docs",
                "chunkId": "chunk:grounding",
                "before": 2,
                "after": 3,
            },
            payload,
        )

    def test_document_knowledge_read_operations_and_management_manifest_are_separate(self) -> None:
        calls = [
            self._tool_call("knowledge", "list_bases"),
            self._tool_call(
                "knowledge",
                "search",
                kbId="kb:project-docs",
                query="Agent Loop",
            ),
            self._tool_call(
                "knowledge",
                "find",
                kbId="kb:project-docs",
                fileId="file:1",
                patterns=["热路径"],
            ),
            self._tool_call(
                "knowledge",
                "open",
                kbId="kb:project-docs",
                fileId="file:1",
                line=41,
                windowSize=20,
            ),
            self._tool_call("knowledge", "status"),
        ]

        for call in calls:
            result = self.gateway.execute(call)["result"]
            self.assertIsInstance(result, dict)
        self.assertEqual(
            ["list_bases", "search", "find", "open", "status"],
            [operation for operation, _payload in self.knowledge.calls[-5:]],
        )
        manifest = next(
            item for item in self.gateway.manifests()["items"] if item["id"] == "knowledge"
        )
        self.assertEqual(
            (
                "list_bases",
                "get_base",
                "list_documents",
                "search",
                "find",
                "open",
                "status",
                "create_base",
                "configure_base",
                "import_text",
                "rebuild_preview",
                "rebuild",
            ),
            tuple(manifest["operations"]),
        )

    def test_write_operations_knowledge_management_and_secrets_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "not agent-manageable"):
            self.gateway.execute(
                self._tool_call(
                    "input",
                    "apply_settings",
                    changes=[{"key": "privacy.debugIncludeText", "value": True}],
                )
            )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            self.gateway.execute(self._tool_call("knowledge", "import", kbId="kb:project-docs"))
        audit = self.gateway.execute(self._tool_call("configuration", "audit"))["result"]
        lexicon = self.gateway.execute(self._tool_call("input", "lexicon_review"))["result"]
        self.assertNotIn("must-not-leak", str(audit))
        self.assertNotIn("must-not-reach-pi", str(lexicon))

    def test_document_knowledge_never_falls_back_to_personal_memory(self) -> None:
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
        )

        status = gateway.execute(self._tool_call("knowledge", "status"))["result"]
        self.assertFalse(status["available"])
        self.assertEqual(status["reason"], "knowledge_client_not_configured")
        with self.assertRaisesRegex(ValueError, "document knowledge library is unavailable"):
            gateway.execute(
                self._tool_call(
                    "knowledge",
                    "search",
                    kbId="kb:project-docs",
                    query="Pi",
                )
            )

    def test_pi_extension_delegates_coordinator_shell_without_node_escape_hatch(self) -> None:
        extension = (
            Path(__file__).parents[1] / "integrations" / "pi" / "rag-ime-control.ts"
        ).read_text(encoding="utf-8")

        forbidden = [
            "child_process",
            "node:child_process",
            "registerCommand(",
            "execSync(",
            "spawn(",
        ]
        for marker in forbidden:
            self.assertNotIn(marker, extension)
        for tool in (
            "overview",
            "input",
            "voice",
            "planning",
            "agent_schedule",
            "memory",
            "agent_role_book",
            "knowledge",
            "models",
            "runtime",
            "configuration",
            "agents",
            "todo",
            "agent_goal",
            "ls",
            "read",
            "grep",
            "find",
            "edit",
            "write",
            "workspace_patch",
            "workspace_lsp",
            "bash",
        ):
            self.assertEqual(extension.count(f'name: "{tool}"'), 1)
        self.assertIn("RAG_IME_AGENT_TOOL_TOKEN", extension)
        self.assertIn("RAG_IME_AGENT_SESSION_MODE", extension)
        self.assertIn("RAG_IME_AGENT_ROOM_BOUND", extension)
        self.assertIn('if (roomBound) return selectedSpecs.filter((spec) => spec.name !== "ask")', extension)
        self.assertIn('sessionMode === "coordinator"', extension)
        for native_readonly in (
            'ls: ["list"]',
            'read: ["read"]',
            'grep: ["search"]',
            'find: ["search"]',
            'bash: ["run"]',
        ):
            self.assertIn(native_readonly, extension)
        hidden_start = extension.index("const readOnlyHiddenNativeTools")
        hidden_end = extension.index("function specsForToolProfile", hidden_start)
        self.assertNotIn("bash: true", extension[hidden_start:hidden_end])
        self.assertIn("/tool/approval-result", extension)
        self.assertIn('const reviewTitlePrefix = "RAG-IME-REVIEW:"', extension)
        self.assertIn("const memoryParameterSchema", extension)
        self.assertIn('"curation_prepare"', extension)
        self.assertIn("parameterSchema: memoryParameterSchema", extension)
        self.assertIn("parameterSchema: roleBookParameterSchema", extension)
        for operation in (
            "remember_preview",
            "correct_preview",
            "forget_preview",
            "remember_apply",
            "correct_apply",
            "forget_apply",
            "governance_rollback",
            "propose_revision",
        ):
            self.assertIn(f'"{operation}"', extension)
        self.assertIn('"timelines"', extension)
        self.assertIn('"evidence"', extension)
        self.assertNotIn('enum: ["apps", "books", "atoms", "tags", "phrases", "groups", "negative"]', extension)
        self.assertIn("result.reviewRequired === true", extension)
        self.assertIn("resolvedReviewRunIds.has(runId)", extension)
        self.assertIn('executionMode: spec.executionMode ?? "sequential"', extension)
        for read_only_tool in (
            "overview",
            "knowledge",
            "ls",
            "read",
            "grep",
            "find",
        ):
            declaration = extension.index(f'name: "{read_only_tool}"')
            next_tool = extension.find("\n  {", declaration + 1)
            tool_block = extension[
                declaration : next_tool if next_tool >= 0 else len(extension)
            ]
            self.assertIn('executionMode: "parallel"', tool_block)
        for stateful_tool in (
            "memory",
            "todo",
            "workspace_patch",
            "agent_goal",
            "workspace_lsp",
            "edit",
            "write",
            "bash",
            "workspace_job",
        ):
            declaration = extension.index(f'name: "{stateful_tool}"')
            next_tool = extension.find("\n  {", declaration + 1)
            tool_block = extension[
                declaration : next_tool if next_tool >= 0 else len(extension)
            ]
            self.assertNotIn('executionMode: "parallel"', tool_block)
        self.assertIn("class GatewayToolError extends Error", extension)
        self.assertIn("recentNonRetryableFailures", extension)
        self.assertIn("同一工具与参数刚刚已被判定为不可重试", extension)
        self.assertIn(
            'operations: ["init", "start", "done", "drop", "block", "unblock", "append", "view", "rm"]',
            extension,
        )
        self.assertIn("默认省略 modelProfile 并继承父 Session", extension)
        self.assertIn('operations: ["list", "confirm_setup", "update", "pause", "resume", "complete", "cancel"]', extension)
        self.assertIn("不要把仍在进行的 Room Goal 暂停来等待用户、界面或后续消息", extension)
        self.assertIn("Do not reuse a previously remembered bound revision", extension)
        self.assertIn('error.errorCode === "workflow_gate_closed"', extension)
        self.assertIn('requiredAction: "review_workflow_state"', extension)
        self.assertIn(
            "这是当前 Session 唯一的任务状态，不是用户的长期记忆或每日规划，也不构成额外执行许可。",
            extension,
        )
        self.assertNotIn("wait for user approval before calling edit", extension)
        for gateway_name in (
            "workspace_list",
            "workspace_read",
            "workspace_search",
            "workspace_lsp",
            "workspace_edit",
            "workspace_write",
            "workspace_shell",
            "workspace_job",
        ):
            self.assertIn(f'gatewayName: "{gateway_name}"', extension)
        self.assertIn("gatewayParamsFor(spec, params)", extension)
        self.assertIn('pi.on?.("message_start"', extension)
        self.assertIn("activeSourceLoopId", extension)
        self.assertIn("...(sourceLoopId ? { sourceLoopId } : {})", extension)
        self.assertIn("const maxInlineToolResultBytes = 24 * 1024", extension)
        self.assertIn("const maxTurnToolResultBytes = 48 * 1024", extension)
        self.assertIn("turnInlineToolResultBytes", extension)
        self.assertIn('pi.on("before_agent_start"', extension)
        self.assertIn('const toolOutputPrefix = "tool-output://"', extension)
        self.assertIn("function boundedToolResult(", extension)
        self.assertIn("function readStoredToolOutput(", extension)
        self.assertIn("fullOutputRef", extension)
        self.assertIn("writeFileSync(filePath, body, { mode: 0o600 })", extension)
        self.assertIn('spec.name === "read"', extension)
        self.assertIn('{ required: ["resourceRef"] }', extension)
        self.assertIn("selectorCursor: params.selectorCursor", extension)
        self.assertIn("asArtifact: params.asArtifact", extension)
        self.assertIn('asArtifact: { type: "boolean" }', extension)
        self.assertIn("resourceRef,", extension)
        self.assertIn('required: ["path", "resourceRevision", "edits"]', extension)
        self.assertIn('required: ["path", "resourceRevision", "content"]', extension)
        self.assertIn("resourceRevision: params.resourceRevision", extension)
        self.assertIn('authorityKind: "session_goal" | "room_work_item"', extension)
        self.assertIn('enum: ["session_goal", "room_work_item"]', extension)
        self.assertIn("workDocument: params.workDocument", extension)
        self.assertIn(
            'payload.result.failureCode === "automatic_approval_bridge_failed"',
            extension,
        )
        self.assertIn('todoTask: {', extension)
        self.assertIn('可选导航链接；仅在确实需要将子 Agent 工作定位到当前 Todo 时传入。', extension)
        self.assertNotIn('todoPhase?: string;', extension)
        self.assertNotIn('todoPhase: { type: "string", minLength: 1, maxLength: 80 }', extension)
        self.assertIn(
            "Todo 只作为可选导航；未显式传入 todoTask 时，delegate 必须独立启动",
            extension,
        )
        self.assertIn("const delegatedProductToolIds = [", extension)
        self.assertEqual(extension.count("enum: [...delegatedProductToolIds]"), 2)
        self.assertIn(
            "不要把 tool_search、read、grep、find 或 bash 填入 allowedTools",
            extension,
        )
        self.assertIn("主持伙伴必须按验收条件核对结果", extension)

    def test_agents_delegate_defaults_to_background_next_turn(self) -> None:
        extension = (
            Path(__file__).parents[1] / "integrations" / "pi" / "rag-ime-control.ts"
        ).read_text(encoding="utf-8")

        self.assertIn(
            'spec.name === "agents" && params.op === "delegate"',
            extension,
        )
        self.assertIn("wait: params.wait === true", extension)
        self.assertIn('default: false', extension)
        self.assertIn("delegate 默认 wait=false", extension)

    def test_retired_room_operations_cannot_reenter_through_ime_agents(self) -> None:
        calls: list[tuple[str, dict[str, object]]] = []

        class _Collaboration:
            def send_room_intercom(self, session_id, payload):
                calls.append((session_id, dict(payload)))
                return {"ok": True, "message": {"id": "room-message:1"}}

            def list_room_intercom(self, session_id, payload):
                return {"ok": True, "sessionId": session_id, "items": [], **dict(payload)}

            def assign_room_work(self, session_id, payload):
                calls.append((session_id, dict(payload)))
                return {"ok": True, "work": {"id": "room-work:1"}}

        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=self.facade,
            collaboration=_Collaboration(),
        )
        for operation in ("room_send", "room_assign"):
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(
                    ValueError,
                    "unsupported agents operation",
                ):
                    gateway.execute(
                        self._tool_call(
                            "agents",
                            operation,
                            content="retired",
                        )
                    )
        self.assertEqual(calls, [])

    def test_agent_can_search_create_validate_and_propose_a_package(self) -> None:
        calls: list[tuple[str, object]] = []

        class _Extensions:
            def list(self):
                return {"ok": True, "items": []}

            def catalog(self):
                calls.append(("catalog", {}))
                return {"ok": True, "items": [{"id": "bundled.example"}]}

            def create_package_draft(self, payload):
                calls.append(("create_package", dict(payload)))
                return {"ok": True, "draft": {"sourcePath": "/managed/inbox/package-1"}}

            def validate(self, payload):
                calls.append(("validate", dict(payload)))
                return {"ok": True, "validationToken": "validation-1"}

            def preview(self, payload):
                calls.append(("preview", dict(payload)))
                return {"ok": True, "proposalId": "proposal-1", "requiredConfirm": "apply"}

        self.gateway.extensions = _Extensions()
        catalog = self.gateway.execute(self._tool_call("plugins", "catalog"))
        draft = self.gateway.execute(
            self._tool_call(
                "plugins",
                "create_package",
                draftId="package-1",
                packageJson={
                    "name": "@paw/log-helper",
                    "version": "1.0.0",
                    "pi": {"skills": ["skills/log-helper/SKILL.md"]},
                },
                files={"skills/log-helper/SKILL.md": "---\nname: log-helper\ndescription: Help inspect logs.\n---\n"},
            )
        )
        validation = self.gateway.execute(
            self._tool_call(
                "plugins",
                "validate",
                packageSource=draft["result"]["draft"]["sourcePath"],
            )
        )
        proposal = self.gateway.execute(
            self._tool_call(
                "plugins",
                "propose_install",
                validationToken=validation["result"]["validationToken"],
                enable=True,
            )
        )

        self.assertEqual(catalog["result"]["items"][0]["id"], "bundled.example")
        self.assertEqual(proposal["result"]["proposalId"], "proposal-1")
        self.assertEqual(calls[0], ("catalog", {}))
        self.assertEqual(calls[1][0], "create_package")
        self.assertEqual(
            calls[1][1]["packageJson"],
            {
                "name": "@paw/log-helper",
                "version": "1.0.0",
                "pi": {"skills": ["skills/log-helper/SKILL.md"]},
            },
        )
        self.assertEqual(
            calls[2][1]["packageSource"],
            "/managed/inbox/package-1",
        )
        self.assertEqual(
            calls[-1][1],
            {"action": "install", "validationToken": "validation-1", "enable": True},
        )
        plugin_manifest = next(
            item for item in self.gateway.manifests()["items"] if item["id"] == "plugins"
        )
        self.assertIn("apply", plugin_manifest["operations"])
        self.assertEqual(
            plugin_manifest["operations"],
            [
                "catalog",
                "list",
                "create_package",
                "validate",
                "propose_install",
                "propose_enable",
                "propose_disable",
                "propose_update",
                "propose_rollback",
                "propose_uninstall",
                "apply",
            ],
        )
        runtime_plugin = next(
            item
            for item in self.gateway.runtime_manifests(self.session)
            if item["name"] == "plugins"
        )
        runtime_branches = {
            branch["properties"]["op"]["const"]: branch
            for branch in runtime_plugin["parameters"]["oneOf"]
        }
        self.assertEqual(
            runtime_branches["propose_enable"]["required"],
            ["op", "pluginId"],
        )
        self.assertEqual(
            runtime_branches["propose_update"]["required"],
            ["op", "validationToken"],
        )
        schema = runtime_plugin["parameters"]
        for arguments in (
            {"op": "validate", "packageSource": "npm:pi-web-search@1.4.0"},
            {"op": "validate", "packageSource": "git:https://example.com/pi-package.git"},
            {"op": "validate", "packageSource": "/managed/inbox/package-1"},
            {"op": "validate", "catalogId": "@paw/session-workflow", "catalogVersion": "1.0.0"},
            {"op": "validate", "sourcePath": "/managed/inbox/legacy-plugin"},
            {
                "op": "create_package", "draftId": "package-1",
                "packageJson": {"name": "@paw/log-helper", "version": "1.0.0"},
                "files": {"skills/log-helper/SKILL.md": "Help inspect logs."},
            },
        ):
            with self.subTest(arguments=arguments):
                validate_contract(arguments, schema)
        for arguments in (
            {"op": "validate"},
            {"op": "validate", "packageSource": ""},
            {"op": "validate", "sourcePath": "/legacy", "packageSource": "npm:pi-web-search@1.4.0"},
            {"op": "validate", "packageSource": "npm:pi-web-search@1.4.0", "catalogId": "@paw/session-workflow"},
            {"op": "create_package", "draftId": "package-1", "files": {}},
            {"op": "validate", "packageSource": "npm:pi-web-search@1.4.0", "unknownSource": "ignored"},
        ):
            with self.subTest(invalid_arguments=arguments), self.assertRaises(ValueError):
                validate_contract(arguments, schema)

    def test_agent_applies_exact_plugin_preview_through_existing_session_authority(self) -> None:
        applied = []

        class _Extensions:
            def inspect_preview(self, payload):
                return {"action": "uninstall", "pluginId": "example.plugin", "displayName": "Example"}

            def apply(self, payload):
                applied.append(dict(payload))
                return {"ok": True, "receipt": {"action": "uninstall", "receiptId": "plugin:uninstall:1"}}

        self.gateway.extensions = _Extensions()
        prepared = self.gateway.execute(self._tool_call(
            "plugins", "apply", previewToken="preview-exact", payloadSha256="a" * 64,
        ))["result"]
        self.assertEqual(applied, [])
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"], approved=True, payload_sha256=approval["payloadSha256"],
        )
        result = self.gateway.apply_approval(decided)
        self.assertEqual(result["receipt"]["action"], "uninstall")
        self.assertEqual(applied, [{"previewToken": "preview-exact", "payloadSha256": "a" * 64, "confirmText": "apply"}])
        readonly = self.store.create(title="Read only", execution_mode="read_only", created_at_ms=3)
        with self.assertRaises(ValueError):
            self.gateway.execute({**self._tool_call(
                "plugins", "apply", previewToken="preview-exact", payloadSha256="a" * 64,
            ), "sessionId": readonly["id"]})
        self.assertEqual(len(applied), 1)

    def test_agent_can_propose_plugin_lifecycle_previews_without_applying_them(self) -> None:
        calls: list[dict[str, object]] = []

        class _Extensions:
            def preview(self, payload):
                calls.append(dict(payload))
                return {
                    "ok": True,
                    "proposalId": f"proposal-{payload['action']}",
                    "requiredConfirm": "apply",
                }

        self.gateway.extensions = _Extensions()
        requests = (
            ("propose_enable", {"pluginId": "example.plugin"}),
            ("propose_disable", {"pluginId": "example.plugin"}),
            (
                "propose_update",
                {"validationToken": "validation:example.plugin:2"},
            ),
            ("propose_rollback", {"pluginId": "example.plugin"}),
            ("propose_uninstall", {"pluginId": "example.plugin"}),
        )

        for operation, args in requests:
            with self.subTest(operation=operation):
                response = self.gateway.execute(
                    self._tool_call("plugins", operation, **args)
                )
                self.assertTrue(response["result"]["ok"])
                self.assertEqual(
                    response["result"]["proposalId"],
                    f"proposal-{operation.removeprefix('propose_')}",
                )

        self.assertEqual(
            calls,
            [
                {"action": "enable", "pluginId": "example.plugin"},
                {"action": "disable", "pluginId": "example.plugin"},
                {
                    "action": "update",
                    "validationToken": "validation:example.plugin:2",
                    "enable": False,
                },
                {"action": "rollback", "pluginId": "example.plugin"},
                {"action": "uninstall", "pluginId": "example.plugin"},
            ],
        )

    def test_agent_schedule_requires_approval_and_applies_the_bound_preview(self) -> None:
        calls: list[tuple[str, object]] = []

        class _Scheduling:
            def preview_wake_schedule(self, payload, *, requested_by_session_id=""):
                calls.append(("preview", requested_by_session_id))
                return {
                    "ok": True,
                    "schedule": {
                        **dict(payload),
                        "title": "明早复盘",
                        "targetDisplayName": "当前线程",
                        "targetType": "session",
                        "targetSessionId": requested_by_session_id,
                        "targetRoleId": "",
                        "targetRoleVersion": "",
                        "planningTaskId": "",
                        "timezone": "Asia/Shanghai",
                        "recurrenceKind": "once",
                        "recurrenceInterval": 1,
                        "maxRuns": 1,
                    },
                }

            def create_wake_schedule(self, payload, *, created_by_session_id="", require_confirmation=True):
                calls.append(("create", dict(payload)))
                self.created_by_session_id = created_by_session_id
                self.require_confirmation = require_confirmation
                return {
                    "ok": True,
                    "schedule": {"id": "wake:1", "title": payload["title"], "status": "scheduled"},
                }

            def list_wake_schedules(self, payload):
                return {"ok": True, "schedulerActive": True, "items": []}

        scheduling = _Scheduling()
        self.gateway.scheduling = scheduling
        listed = self.gateway.execute(
            self._tool_call("agent_schedule", "list")
        )["result"]
        prepared = self.gateway.execute(
            self._tool_call(
                "agent_schedule",
                "schedule",
                title="明早复盘",
                instruction="整理今天的结果",
                targetType="session",
                targetSessionId=self.session["id"],
                wakeAtMs=1_900_000_000_000,
            )
        )["result"]

        self.assertTrue(listed["schedulerActive"])
        self.assertTrue(prepared["approvalRequired"])
        self.assertEqual(prepared["approval"]["riskLevel"], "R2")
        approval = prepared["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = self.gateway.apply_approval(decided)

        self.assertTrue(receipt["mutationApplied"])
        self.assertEqual(receipt["schedule"]["id"], "wake:1")
        self.assertEqual(scheduling.created_by_session_id, self.session["id"])
        self.assertFalse(scheduling.require_confirmation)

    def test_workspace_lsp_readonly_and_governed_writes_use_canonical_gateway(self) -> None:
        workspace = Path(self.tmp.name) / "workspace-lsp"
        workspace.mkdir()
        target = workspace / "main.py"
        target.write_text("foo = 1\n", encoding="utf-8")

        class _LspHarness:
            def __init__(self) -> None:
                self.read_calls = []
                self.apply_calls = []

            def lsp_status(self, session, args):
                self.read_calls.append(("status", dict(args)))
                return {
                    "schemaVersion": "rag-ime.workspace-lsp-status.v1",
                    "runtimeInstanceId": "workspace-lsp-00000000000000000000000000000001",
                    "runtimeEpoch": 7,
                    "observedAtMs": 1_000,
                    "heartbeatExpiresAtMs": 31_000,
                    "current": True,
                    "summary": "fake LSP available",
                    "state": "available",
                    "roots": [],
                }

            def lsp_read(self, session, operation, args):
                self.read_calls.append((operation, dict(args)))
                return {
                    "schemaVersion": "rag-ime.workspace-lsp-result.v1",
                    "summary": "fake readonly result",
                    "operation": operation,
                    "root": str(workspace),
                    "server": "fake",
                    "items": [],
                }

            def prepare_lsp_mutation(self, session, operation, args):
                return {"operation": operation, "args": dict(args)}

            def lsp_mutation_preview(self, prepared):
                return {
                    "title": "确认重命名符号",
                    "summary": "重命名符号将修改 1 个工作区文件",
                    "operationLabel": "重命名符号",
                    "changes": [
                        {
                            "label": "main.py",
                            "before": "foo = 1",
                            "after": "bar = 1",
                        }
                    ],
                    "actionPayload": {
                        "path": str(target),
                        "newName": prepared["args"]["newName"],
                        "files": [{"path": str(target), "content": "bar = 1\n"}],
                    },
                    "baseState": {
                        "workspaceRoot": str(workspace),
                        "workspaceRootSha256": "a" * 64,
                        "server": "fake",
                        "files": [],
                    },
                }

            def apply_lsp_mutation(
                self,
                session,
                operation,
                action_payload,
                base_state,
            ):
                self.apply_calls.append((operation, dict(action_payload), dict(base_state)))
                preimage = target.read_bytes()
                preimage_sha256 = hashlib.sha256(preimage).hexdigest()
                target.write_text(
                    str(action_payload["files"][0]["content"]),
                    encoding="utf-8",
                )
                postimage_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
                return {
                    "schemaVersion": "rag-ime.workspace-lsp-mutation-receipt.v1",
                    "mutationApplied": True,
                    "summary": "workspace_lsp 已修改 1 个文件",
                    "operation": operation,
                    "root": str(workspace),
                    "server": "fake",
                    "changedFiles": [
                        {
                            "path": str(target),
                            "preimageSha256": preimage_sha256,
                            "postimageSha256": postimage_sha256,
                        }
                    ],
                    **(
                        {
                            "referencesEvidence": {
                                "root": str(workspace),
                                "path": str(target),
                                "relativePath": "main.py",
                                "line": 1,
                                "column": 1,
                                "server": "fake",
                                "resourceRevision": "sha256:" + preimage_sha256,
                                "preimageSha256": preimage_sha256,
                                "count": 0,
                                "truncated": False,
                                "items": [],
                            }
                        }
                        if operation == "rename"
                        else {}
                    ),
                    "undoAvailable": False,
                }

        harness = _LspHarness()
        gateway = ControlToolGateway(
            sessions=self.store,
            management=self.management,
            core=_Core(),
            project="wisdom-weasel-rag-ime",
            facade=_Facade(),
            workspace_harness=harness,
        )
        readonly = self.store.create(
            title="readonly LSP",
            mode="coordinator",
            execution_mode="read_only",
            workspace_roots=[str(workspace)],
            created_at_ms=30,
        )
        catalog = gateway.manifests(session_id=str(readonly["id"]))
        lsp_capability = next(
            item for item in catalog["items"] if item["id"] == "workspace_lsp"
        )
        authoritative = lsp_capability["runtimeProjection"]
        self.assertTrue(authoritative["current"])
        self.assertEqual(authoritative["runtimeEpoch"], 7)
        self.assertEqual(authoritative["heartbeatExpiresAtMs"], 31_000)

        status = gateway.execute(
            {
                **self._tool_call("workspace_lsp", "status"),
                "sessionId": readonly["id"],
            }
        )["result"]
        hover = gateway.execute(
            {
                **self._tool_call(
                    "workspace_lsp",
                    "hover",
                    path=str(target),
                    line=1,
                    column=1,
                ),
                "sessionId": readonly["id"],
            }
        )["result"]
        self.assertEqual(status["state"], "available")
        self.assertEqual(hover["operation"], "hover")
        self.assertEqual(
            self.store.list_approvals(session_id=str(readonly["id"])),
            [],
        )
        with self.assertRaisesRegex(ValueError, "not enabled|read-only"):
            gateway.execute(
                {
                    **self._tool_call(
                        "workspace_lsp",
                        "rename",
                        path=str(target),
                        line=1,
                        column=1,
                        newName="bar",
                    ),
                    "sessionId": readonly["id"],
                }
            )
        self.assertEqual(
            self.store.list_approvals(session_id=str(readonly["id"])),
            [],
        )

        governed = self.store.create(
            title="governed LSP",
            mode="coordinator",
            workspace_roots=[str(workspace)],
            created_at_ms=31,
        )
        self._start_todo(str(governed["id"]))
        pending = gateway.execute(
            {
                **self._tool_call(
                    "workspace_lsp",
                    "rename",
                    path=str(target),
                    line=1,
                    column=1,
                    newName="bar",
                ),
                "sessionId": governed["id"],
            }
        )["result"]
        self.assertTrue(pending["approvalRequired"])
        self.assertEqual(pending["approval"]["riskLevel"], "R2")
        self.assertEqual(target.read_text(encoding="utf-8"), "foo = 1\n")
        approval = pending["approval"]
        decided = self.store.decide_approval(
            approval["approvalId"],
            approved=True,
            payload_sha256=approval["payloadSha256"],
        )
        receipt = gateway.apply_approval(decided)
        self.assertTrue(receipt["mutationApplied"])
        self.assertEqual(receipt["toolId"], "workspace_lsp")
        self.assertEqual(target.read_text(encoding="utf-8"), "bar = 1\n")
        self.assertEqual(len(harness.apply_calls), 1)

    def _call(self, operation: str, **args):
        return self._tool_call("memory", operation, **args)

    def _tool_call(self, tool: str, operation: str, **args):
        return {
            "schemaVersion": "rag-ime.agent-tool-call.v1",
            "sessionId": self.session["id"],
            "tool": tool,
            "toolCallId": "tool:1",
            "args": {"op": operation, **args},
        }

    def _start_todo(self, session_id: str) -> dict[str, object]:
        task = "执行工作区变更"
        self.store.mutate_agent_todo(
            session_id,
            {
                "op": "init",
                "phase": "受控工作区执行",
                "items": [task],
            },
            actor="test-user",
        )
        return self.store.mutate_agent_todo(
            session_id,
            {"op": "start", "task": task},
            actor="test-user",
        )["todo"]


if __name__ == "__main__":
    unittest.main()
