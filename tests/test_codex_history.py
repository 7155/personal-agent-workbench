from __future__ import annotations

import io
import json
import os
import plistlib
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from rag_ime.cli import _launchctl_field, _sidecar_lane_timeout_checks, main
from rag_ime.adapter import InputMethodAdapter
from rag_ime.codex_history import (
    evaluate_suggestions,
    input_event_from_codex_record,
    load_codex_history_records,
    load_eval_cases,
)
from rag_ime.input_quality import MEMORY_CONTEXT_OPT_IN_TAG
from rag_ime.local_sqlite_core import LocalSqliteCoreClient
from rag_ime.models import InputEvent, InputSuggestion
from rag_ime.predictor import NullPredictionProvider


class _MockComparisonPredictionHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        length = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(length)
        body = json.dumps(
            {
                "choices": [
                    {
                        "message": {
                            "content": "Squirrel本地记忆",
                        }
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _MockMlxCapabilityHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path == "/health":
            body = json.dumps(
                {
                    "ok": True,
                    "provider": "mlx-lm",
                    "model": "mlx-qwen3.5-0.8b",
                    "modelLoaded": True,
                    "promptCache": {
                        "enabled": True,
                        "prepared": True,
                        "cacheFileReady": True,
                        "usedForGeneration": True,
                        "hits": 2,
                    },
                    "capabilities": {
                        "streaming": True,
                        "residentModel": True,
                        "promptCache": True,
                        "sequenceFork": False,
                        "batchCandidates": False,
                        "serverTiming": True,
                    },
                },
                ensure_ascii=False,
            ).encode("utf-8")
        elif self.path == "/v1/models":
            body = json.dumps({"data": [{"id": "mlx-qwen3.5-0.8b"}]}, ensure_ascii=False).encode("utf-8")
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        length = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(length)
        body = json.dumps(
            {
                "ok": True,
                "candidates": ["PROJECT_MEMORY_BLOCK"],
                "rawText": '["PROJECT_MEMORY_BLOCK"]',
                "totalMs": 12,
                "promptCache": {
                    "enabled": True,
                    "prepared": True,
                    "cacheFileReady": True,
                    "usedForGeneration": True,
                    "hits": 2,
                },
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class _MockTryoutSidecarHandler(BaseHTTPRequestHandler):
    rime_suggest_calls = 0

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if self.path != "/health":
            self.send_error(404)
            return
        self._send_json(
            {
                "ok": True,
                "eventCount": 7,
                "predictor": {
                    "configured": True,
                    "providerName": "local-ollama",
                    "model": "qwen3.5:0.8b-mlx",
                    "streamFirstCandidate": True,
                },
            }
        )

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        if self.path != "/rime-suggest":
            self.send_error(404)
            return
        _MockTryoutSidecarHandler.rime_suggest_calls += 1
        length = int(self.headers.get("Content-Length") or "0")
        self.rfile.read(length)
        self._send_json(
            {
                "schemaVersion": "rag-ime.rime-sidecar.v1",
                "queryBasis": "rimeCandidates",
                "displayCandidates": [
                    {
                        "label": "1",
                        "text": "本地记忆",
                        "sourceType": "rime",
                        "selectionAction": "select_rime_candidate",
                    }
                ],
                "cache": {"hit": False},
            }
        )

    def _send_json(self, payload: dict[str, object]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        return


class CodexHistoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="rag-ime-codex-history-")
        self.root = Path(self.tmp.name)
        self.history = self.root / "codex.jsonl"
        self.history.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "response_item",
                            "created_at": "2026-06-30T10:00:00Z",
                            "payload": {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "我想把 RAG 输入法接入 Squirrel, 让候选词使用本地记忆。",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    "{not valid json",
                    json.dumps(
                        {
                            "type": "response_item",
                            "created_at": "2026-06-30T10:02:00Z",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "FTS5 排序和 local-first 记忆要能服务 Codex 历史评测。",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "event_msg": {
                                "message": "Working (44m 30s) esc to interrupt",
                                "timestamp_ms": 1782813600000,
                            }
                        },
                        ensure_ascii=False,
                    ),
                ]
            ),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_fake_squirrel_app(self, name: str) -> Path:
        app = self.root / f"{name}.app"
        contents = app / "Contents"
        executable_dir = contents / "MacOS"
        executable_dir.mkdir(parents=True)
        executable = executable_dir / "Squirrel"
        executable.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        with (contents / "Info.plist").open("wb") as fh:
            plistlib.dump({"CFBundleIdentifier": "im.rime.inputmethod.Squirrel"}, fh)
        return app

    def _write_fake_squirrel_config(
        self,
        name: str,
        *,
        db_path: Path,
        project: str,
        sidecar_url: str = "http://127.0.0.1:8766/api",
        create_build: bool = True,
        primary_schema: str = "luna_pinyin_simp",
        page_size: int = 8,
    ) -> Path:
        rime_dir = self.root / f"{name}-rime"
        rime_dir.mkdir(parents=True, exist_ok=True)
        config = rime_dir / "squirrel.custom.yaml"
        config.write_text(
            "\n".join(
                [
                    "patch:",
                    "# >>> RAG-IME managed block",
                    '  "rag_ime/enabled": true',
                    f'  "rag_ime/sidecar_url": "{sidecar_url}"',
                    f'  "rag_ime/db_path": "{str(db_path)}"',
                    f'  "rag_ime/project": "{project}"',
                    "# <<< RAG-IME managed block",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        (rime_dir / "default.custom.yaml").write_text(
            "\n".join(
                [
                    "patch:",
                    "# >>> RAG-IME default managed block",
                    f'  "menu/page_size": {page_size}',
                    "  schema_list:",
                    f"    - schema: {primary_schema}",
                    "    - schema: luna_pinyin",
                    "# <<< RAG-IME default managed block",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        if create_build:
            build_dir = rime_dir / "build"
            build_dir.mkdir()
            (build_dir / "default.yaml").write_text(
                f"menu:\n  page_size: {page_size}\nschema_list:\n  - schema: {primary_schema}\n  - schema: luna_pinyin\n",
                encoding="utf-8",
            )
            (build_dir / "luna_pinyin.schema.yaml").write_text("schema:\n  schema_id: luna_pinyin\n", encoding="utf-8")
            (build_dir / "luna_pinyin.table.bin").write_bytes(b"table")
        return config

    def test_load_codex_history_records_skips_invalid_jsonl_and_extracts_text(self) -> None:
        records = load_codex_history_records(self.history, limit=10)
        self.assertEqual(len(records), 2)
        self.assertIn("RAG 输入法", records[0].text)
        self.assertIn("FTS5 排序", records[1].text)
        self.assertNotIn("Working", "\n".join(item.text for item in records))
        self.assertIn("codex-history", records[0].tags)
        self.assertEqual(records[0].role, "user")
        self.assertGreater(records[0].created_at_ms, 0)

    def test_load_codex_history_records_can_filter_to_user_role(self) -> None:
        mixed_history = self.root / "mixed-codex.jsonl"
        mixed_history.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "用户真正输入的 RAG 输入法需求"}],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"summary": "没有 role 的系统摘要不应该默认进 IME 记忆候选"}, ensure_ascii=False),
                ]
            ),
            encoding="utf-8",
        )

        all_records = load_codex_history_records(mixed_history, limit=10)
        user_records = load_codex_history_records(mixed_history, limit=10, roles=("user",))

        self.assertEqual([item.role for item in all_records], ["user", ""])
        self.assertEqual([item.text for item in user_records], ["用户真正输入的 RAG 输入法需求"])

    def test_record_can_be_converted_to_input_event(self) -> None:
        record = load_codex_history_records(self.history, limit=1)[0]
        event = input_event_from_codex_record(record, project="wisdom-weasel-rag-ime")
        self.assertEqual(event.source, "codex_history")
        self.assertEqual(event.schema_id, "codex_history")
        self.assertEqual(event.app, "codex")
        self.assertIn("codex_history:codex.jsonl:1", event.recent_context)
        self.assertNotIn("curated", event.tags)
        curated = input_event_from_codex_record(
            record,
            project="wisdom-weasel-rag-ime",
            curated=True,
        )
        self.assertIn("curated", curated.tags)
        self.assertNotIn(MEMORY_CONTEXT_OPT_IN_TAG, curated.tags)
        opted_in = input_event_from_codex_record(
            record,
            project="wisdom-weasel-rag-ime",
            curated=True,
            memory_context_opt_in=True,
        )
        self.assertIn(MEMORY_CONTEXT_OPT_IN_TAG, opted_in.tags)

    def test_directory_import_prefers_recent_sessions_and_skips_runtime_noise(self) -> None:
        history_dir = self.root / "history-dir"
        history_dir.mkdir()
        old_file = history_dir / "old.jsonl"
        old_file.write_text(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "旧会话里的普通记忆可以作为回退"}],
                    },
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        new_file = history_dir / "new.jsonl"
        new_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "developer",
                                "content": [{"type": "input_text", "text": "You are Codex, system text"}],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "file:///Volumes/undo%204t/git/learnA/wisdom-weasel-rag-ime",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "[317] tool update_plan result: Plan updated",
                                    },
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "[461] tool write_stdin result: Chunk ID: abc "
                                            "Wall time: 30.0030 seconds Output: ........"
                                        ),
                                    },
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "Assess the exact planned action below. Use read-only tool checks when local state matters.",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "Welcome to fish, the friendly interactive shell "
                                            "❯ cd \"/Volumes/example/langchain\" "
                                            "curl -s -H \"x-scoutpi-browser-token: $TOKEN\" http://127.0.0.1:17391/doctor"
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "<turn_aborted> The user interrupted the previous turn on purpose. "
                                            "Any running unified exec processes may still be running."
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "ScoutPi BrowserBridge Bridge URL ws://127.0.0.1:17391/extension "
                                            "Token ••••••••••••••••••••••••••••••••••••••••••• Connect Connected"
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": '{"key":"sk-real-looking-secret-token","url":"https://api.example.com"}',
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "<personality_spec> The user has requested a new communication style. "
                                            "Future messages should adhere to the following personality."
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "# Files mentioned by the user:\n"
                                            "## codex-clipboard-demo.png: /var/folders/tmp/codex-clipboard-demo.png\n"
                                            "## My request for Codex:"
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "<image name=[Image #1] path=\"/tmp/codex-clipboard-demo.png\">",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "<skills_instructions>\n"
                                            "## Skills\n"
                                            "skills_instructions / SKILL.md / tool_search "
                                            "这些是运行时技能清单, 不应该成为用户记忆。"
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "<apps_instructions>\n"
                                            "apps_instructions / connector_id / codex_apps "
                                            "这些 connector 上下文也不应该被导入。"
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": (
                                            "<collaboration_mode>\n"
                                            "collaboration_mode / request_user_input "
                                            "这是 Codex 运行模式注入, 不是项目记忆。"
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [
                                    {
                                        "type": "input_text",
                                        "text": "# AGENTS.md instructions for /Volumes/example/git/learnA",
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "function_call_output",
                                "output": "Chunk ID: noisy tool output",
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": (
                                            "The following is the Codex agent history added "
                                            "since your last approval assessment."
                                        ),
                                    }
                                ],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "最新 RAG-IME 记忆应该先导入"}],
                            },
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        os.utime(old_file, (1000, 1000))
        os.utime(new_file, (2000, 2000))

        records = load_codex_history_records(history_dir, limit=2, path_order="mtime-desc")

        self.assertEqual([item.text for item in records], ["最新 RAG-IME 记忆应该先导入", "旧会话里的普通记忆可以作为回退"])

    def test_cli_import_dry_run_does_not_write_database(self) -> None:
        db_path = self.root / "dry-run.sqlite"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "--db-path",
                    str(db_path),
                    "import-codex-history",
                    "--curated",
                    "--path",
                    str(self.history),
                    "--dry-run",
                ]
            )
        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["schemaVersion"], "rag-ime.codex-history-import.v1")
        self.assertEqual(payload["records"], 2)
        self.assertEqual(payload["imported"], 0)
        self.assertFalse(payload["memoryContextOptIn"])
        if db_path.exists():
            with closing(sqlite3.connect(db_path)) as conn, conn:
                table = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'input_events'"
                ).fetchone()
            self.assertIsNone(table)

    def test_cli_import_defaults_to_user_role(self) -> None:
        mixed_history = self.root / "mixed-cli-codex.jsonl"
        mixed_history.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "response_item",
                            "payload": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "用户输入应该进入本地记忆候选"}],
                            },
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps({"summary": "无角色摘要只在显式 --roles any 时导入"}, ensure_ascii=False),
                ]
            ),
            encoding="utf-8",
        )

        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(
                [
                    "--db-path",
                    str(self.root / "user-default.sqlite"),
                    "import-codex-history",
                    "--curated",
                    "--path",
                    str(mixed_history),
                    "--dry-run",
                    "--sample-size",
                    "5",
                ]
            )

        self.assertEqual(code, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["roles"], ["user"])
        self.assertEqual(payload["records"], 1)
        self.assertEqual(payload["samples"][0]["text"], "用户输入应该进入本地记忆候选")

        any_stdout = io.StringIO()
        with redirect_stdout(any_stdout):
            any_code = main(
                [
                    "--db-path",
                    str(self.root / "any-role.sqlite"),
                    "import-codex-history",
                    "--curated",
                    "--path",
                    str(mixed_history),
                    "--dry-run",
                    "--roles",
                    "any",
                    "--sample-size",
                    "5",
                ]
            )

        self.assertEqual(any_code, 0)
        any_payload = json.loads(any_stdout.getvalue())
        self.assertEqual(any_payload["roles"], "any")
        self.assertEqual(any_payload["records"], 2)

    def test_cli_import_and_eval_cases(self) -> None:
        db_path = self.root / "codex-history.sqlite"
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            import_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "import-codex-history",
                    "--curated",
                    "--memory-context-opt-in",
                    "--path",
                    str(self.history),
                    "--project",
                    "wisdom-weasel-rag-ime",
                ]
            )
        self.assertEqual(import_code, 0)
        import_payload = json.loads(stdout.getvalue())
        self.assertEqual(import_payload["imported"], 2)
        self.assertEqual(import_payload["duplicateSkipped"], 0)
        self.assertTrue(import_payload["memoryContextOptIn"])

        second_stdout = io.StringIO()
        with redirect_stdout(second_stdout):
            second_import_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "import-codex-history",
                    "--curated",
                    "--memory-context-opt-in",
                    "--path",
                    str(self.history),
                    "--project",
                    "wisdom-weasel-rag-ime",
                ]
            )
        self.assertEqual(second_import_code, 0)
        second_payload = json.loads(second_stdout.getvalue())
        self.assertEqual(second_payload["imported"], 0)
        self.assertEqual(second_payload["duplicateSkipped"], 2)

        cases_file = self.root / "cases.jsonl"
        repeated_case = {
            "query": "Squirrel RAG 输入法候选",
            "expectedTerms": ["Squirrel", "本地记忆"],
        }
        cases_file.write_text(
            "\n".join(
                [
                    json.dumps({"id": "squirrel-rag", **repeated_case}, ensure_ascii=False),
                    json.dumps({"id": "squirrel-rag-repeat", **repeated_case}, ensure_ascii=False),
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        eval_stdout = io.StringIO()
        with redirect_stdout(eval_stdout):
            eval_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "eval-codex-history",
                    "--cases-file",
                    str(cases_file),
                    "--match",
                    "all",
                ]
            )
        self.assertEqual(eval_code, 0)
        report = json.loads(eval_stdout.getvalue())
        self.assertEqual(report["schemaVersion"], "rag-ime.codex-history-eval.v1")
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["passRate"], 1.0)
        self.assertEqual(report["metrics"]["hitRate"], 1.0)
        self.assertEqual(report["metrics"]["top1Accuracy"], 1.0)
        self.assertEqual(report["metrics"]["meanReciprocalRank"], 1.0)
        self.assertEqual(report["cases"][0]["firstMatchRank"], 1)
        self.assertTrue(report["cases"][0]["top1Passed"])
        self.assertGreaterEqual(report["cacheStats"]["hits"], 1)
        self.assertEqual(report["latency"]["caseCount"], 2)
        self.assertGreaterEqual(report["latency"]["totalMs"], 0)
        self.assertIn("elapsedMs", report["cases"][0])

    def test_cli_eval_repeat_measures_warm_cache(self) -> None:
        db_path = self.root / "repeat-eval.sqlite"
        with redirect_stdout(io.StringIO()):
            import_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "import-codex-history",
                    "--curated",
                    "--memory-context-opt-in",
                    "--path",
                    str(self.history),
                    "--project",
                    "wisdom-weasel-rag-ime",
                ]
            )
        self.assertEqual(import_code, 0)

        cases_file = self.root / "repeat-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "squirrel-rag",
                    "query": "Squirrel RAG 输入法候选",
                    "expectedTerms": ["Squirrel", "本地记忆"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        eval_stdout = io.StringIO()
        with redirect_stdout(eval_stdout):
            eval_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "eval-codex-history",
                    "--cases-file",
                    str(cases_file),
                    "--repeat",
                    "3",
                    "--match",
                    "all",
                ]
            )
        self.assertEqual(eval_code, 0)
        report = json.loads(eval_stdout.getvalue())
        self.assertEqual(report["total"], 3)
        self.assertEqual(report["repeat"]["requested"], 3)
        self.assertEqual(report["repeat"]["baseCaseCount"], 1)
        self.assertEqual(report["repeat"]["effectiveCaseCount"], 3)
        self.assertEqual(report["latency"]["caseCount"], 3)
        self.assertEqual(
            [item["caseId"] for item in report["cases"]],
            ["squirrel-rag#r1", "squirrel-rag#r2", "squirrel-rag#r3"],
        )
        self.assertGreaterEqual(report["cacheStats"]["hits"], 2)
        self.assertIn("elapsedMs", report["cases"][2])

    def test_cli_eval_rime_sidecar_scores_display_side_candidates_and_cache(self) -> None:
        db_path = self.root / "rime-sidecar-eval.sqlite"
        with redirect_stdout(io.StringIO()):
            import_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "import-codex-history",
                    "--curated",
                    "--path",
                    str(self.history),
                    "--project",
                    "wisdom-weasel-rag-ime",
                ]
            )
        self.assertEqual(import_code, 0)
        InputMethodAdapter(LocalSqliteCoreClient(db_path), project="wisdom-weasel-rag-ime").commit_text(
            "Squirrel 本地记忆",
            recent_context="结构化 RAG 候选用于 sidecar eval, raw Codex history 不直接作为输入法候选。",
            project="wisdom-weasel-rag-ime",
            tags=("structure", "rag-ime", "curated"),
            privacy_disposition="allowed",
        )

        cases_file = self.root / "rime-sidecar-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "squirrel-rag-sidecar",
                    "query": "Squirrel RAG 输入法候选",
                    "expectedTerms": ["Squirrel", "本地记忆"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        eval_stdout = io.StringIO()
        with redirect_stdout(eval_stdout):
            eval_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "eval-rime-sidecar",
                    "--cases-file",
                    str(cases_file),
                    "--match",
                    "all",
                    "--repeat",
                    "2",
                ]
            )
        self.assertEqual(eval_code, 0)
        report = json.loads(eval_stdout.getvalue())
        self.assertEqual(report["schemaVersion"], "rag-ime.rime-sidecar-eval.v1")
        self.assertEqual(report["repeat"]["effectiveCaseCount"], 2)
        self.assertEqual(report["passed"], 2)
        self.assertEqual(report["metrics"]["top1Accuracy"], 1.0)
        self.assertEqual(report["sidecar"]["triggerRefreshCount"], 2)
        self.assertGreaterEqual(report["sidecar"]["totalRagCandidates"], 1)
        self.assertGreaterEqual(report["sidecar"]["totalSideCandidates"], 1)
        self.assertEqual(report["sidecar"]["totalModelCandidates"], 0)
        self.assertEqual(report["sidecar"]["ragLaneCalledCount"], 2)
        self.assertEqual(report["sidecar"]["ragLaneTimeoutCount"], 0)
        self.assertEqual(report["sidecar"]["ragLaneTimeoutRate"], 0.0)
        self.assertEqual(report["sidecar"]["modelLaneTimeoutCount"], 0)
        self.assertEqual(report["sidecar"]["modelLaneTimeoutRate"], 0.0)
        self.assertGreaterEqual(report["sidecar"]["rimeSuggestCache"]["hits"], 1)
        self.assertEqual(report["sidecar"]["rimeSuggestCache"]["misses"], 1)
        self.assertIn("elapsedMs", report["cases"][0])
        self.assertIn("本地记忆", " ".join(report["cases"][0]["topSurfaces"]))

    def test_cli_quality_gate_aggregates_eval_sidecar_acceptance_and_cache(self) -> None:
        db_path = self.root / "quality-gate.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "quality-gate-cases.jsonl"
        cases_file.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "id": "agent-hook",
                            "query": "首次运行自动注入背景记忆",
                            "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                        },
                        ensure_ascii=False,
                    ),
                    json.dumps(
                        {
                            "id": "memory-actions",
                            "query": "本地记忆 action 如何支持 pin downrank delete",
                            "expectedTerms": ["pin", "downrank", "delete"],
                        },
                        ensure_ascii=False,
                    ),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "quality-gate",
                    "--cases-file",
                    str(cases_file),
                    "--min-rag-pass-rate",
                    "1",
                    "--min-sidecar-pass-rate",
                    "1",
                    "--max-sidecar-rag-timeout-rate",
                    "0",
                    "--max-sidecar-model-timeout-rate",
                    "1",
                    "--force-side-candidates",
                    "--require-suggestion-cache",
                ]
            )
        self.assertEqual(gate_code, 0)
        report = json.loads(gate_stdout.getvalue())
        self.assertEqual(report["schemaVersion"], "rag-ime.quality-gate.v1")
        self.assertTrue(report["passed"])
        self.assertTrue(all(item["passed"] for item in report["checks"]))
        self.assertEqual(report["rag"]["passRate"], 1.0)
        self.assertEqual(report["rimeSidecar"]["passRate"], 1.0)
        self.assertNotIn("cases", report["rag"])
        self.assertNotIn("cases", report["rimeSidecar"])
        self.assertTrue(report["cacheProbe"]["summary"]["suggestionCachePassed"])
        self.assertTrue(report["cacheProbe"]["summary"]["rimeCachePassed"])
        self.assertIn("predictor", report)
        self.assertEqual(report["thresholds"]["requiredPredictorCapabilities"], [])
        self.assertEqual(report["thresholds"]["minRagTop1Accuracy"], 0.0)
        self.assertEqual(report["thresholds"]["minSidecarMeanReciprocalRank"], 0.0)
        self.assertEqual(report["thresholds"]["maxRagNoiseRate"], 1.0)
        self.assertEqual(report["thresholds"]["maxSidecarRagTimeoutRate"], 0.0)
        self.assertEqual(report["thresholds"]["maxSidecarModelTimeoutRate"], 1.0)
        check_names = {item["name"] for item in report["checks"]}
        self.assertIn("rag-top1-accuracy", check_names)
        self.assertIn("rag-mean-reciprocal-rank", check_names)
        self.assertIn("rag-noise-rate", check_names)
        self.assertIn("rime-sidecar-top1-accuracy", check_names)
        self.assertIn("rime-sidecar-mean-reciprocal-rank", check_names)
        self.assertIn("rime-sidecar-noise-rate", check_names)
        self.assertIn("rime-sidecar-rag-timeout-rate", check_names)
        self.assertIn("rime-sidecar-model-timeout-rate", check_names)

    def test_sidecar_lane_timeout_checks_fail_when_rates_exceed_threshold(self) -> None:
        checks = _sidecar_lane_timeout_checks(
            {
                "sidecar": {
                    "ragLaneCalledCount": 3,
                    "ragLaneTimeoutCount": 1,
                    "ragLaneTimeoutRate": 1 / 3,
                    "modelLaneCalledCount": 3,
                    "modelLaneTimeoutCount": 0,
                    "modelLaneTimeoutRate": 0.0,
                }
            },
            max_rag_timeout_rate=0.0,
            max_model_timeout_rate=0.0,
        )
        by_name = {item["name"]: item for item in checks}
        self.assertFalse(by_name["rime-sidecar-rag-timeout-rate"]["passed"])
        self.assertTrue(by_name["rime-sidecar-model-timeout-rate"]["passed"])
        self.assertEqual(by_name["rime-sidecar-rag-timeout-rate"]["timeoutCount"], 1)
        self.assertEqual(by_name["rime-sidecar-rag-timeout-rate"]["calledCount"], 3)

    def test_launchctl_field_parses_running_launch_agent_status(self) -> None:
        output = """
        path = /Users/example/Library/LaunchAgents/com.rag-ime.sidecar.plist
        state = running
        pid = 13454
        program = /usr/local/bin/python3
        """

        self.assertEqual(_launchctl_field(output, "state"), "running")
        self.assertEqual(_launchctl_field(output, "pid"), "13454")
        self.assertEqual(
            _launchctl_field(output, "path"),
            "/Users/example/Library/LaunchAgents/com.rag-ime.sidecar.plist",
        )
        self.assertEqual(_launchctl_field(output, "program"), "/usr/local/bin/python3")
        self.assertEqual(_launchctl_field(output, "missing"), "")

    def test_cli_quality_gate_can_enforce_noise_thresholds(self) -> None:
        db_path = self.root / "quality-gate-noise.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "quality-gate-noise-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook-noise",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                    "forbiddenTerms": ["Agent"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "quality-gate",
                    "--cases-file",
                    str(cases_file),
                    "--min-rag-pass-rate",
                    "0",
                    "--min-sidecar-pass-rate",
                    "0",
                    "--max-rag-noise-rate",
                    "0",
                    "--max-sidecar-noise-rate",
                    "0",
                    "--force-side-candidates",
                ]
            )
        self.assertEqual(gate_code, 1)
        report = json.loads(gate_stdout.getvalue())
        self.assertFalse(report["passed"])
        noise_checks = {item["name"]: item for item in report["checks"] if item["name"].endswith("noise-rate")}
        self.assertFalse(noise_checks["rag-noise-rate"]["passed"])
        self.assertFalse(noise_checks["rime-sidecar-noise-rate"]["passed"])
        self.assertEqual(noise_checks["rag-noise-rate"]["expectedAtMost"], 0.0)
        self.assertEqual(report["rag"]["metrics"]["noiseRate"], 1.0)

    def test_cli_quality_gate_fails_when_recent_context_candidates_enabled_for_production(self) -> None:
        db_path = self.root / "quality-gate-production-governance.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "quality-gate-production-governance-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        gate_stdout = io.StringIO()
        with patch.dict(os.environ, {"RAG_IME_RECENT_CONTEXT_CANDIDATES": "1"}):
            with redirect_stdout(gate_stdout):
                gate_code = main(
                    [
                        "--db-path",
                        str(db_path),
                        "quality-gate",
                        "--cases-file",
                        str(cases_file),
                        "--min-rag-pass-rate",
                        "0",
                        "--min-sidecar-pass-rate",
                        "0",
                        "--max-sidecar-rag-timeout-rate",
                        "1",
                        "--max-sidecar-model-timeout-rate",
                        "1",
                        "--force-side-candidates",
                        "--skip-acceptance-check",
                        "--require-production-rag-governance",
                    ]
                )
        self.assertEqual(gate_code, 1)
        report = json.loads(gate_stdout.getvalue())
        self.assertFalse(report["passed"])
        self.assertTrue(report["thresholds"]["requireProductionRagGovernance"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertFalse(checks["production-rag-no-recent-context-candidates"]["passed"])
        self.assertTrue(checks["production-rag-no-recent-context-candidates"]["debugOnlyFallbackEnabled"])
        self.assertEqual(checks["production-rag-no-recent-context-candidates"]["actual"], "1")

    def test_cli_quality_gate_can_require_predictor_capabilities(self) -> None:
        db_path = self.root / "quality-gate-capability.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "quality-gate-capability-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "quality-gate",
                    "--cases-file",
                    str(cases_file),
                    "--min-rag-pass-rate",
                    "1",
                    "--min-sidecar-pass-rate",
                    "1",
                    "--force-side-candidates",
                    "--require-predictor-capability",
                    "sequenceFork",
                ]
            )
        self.assertEqual(gate_code, 1)
        report = json.loads(gate_stdout.getvalue())
        self.assertFalse(report["passed"])
        self.assertEqual(report["thresholds"]["requiredPredictorCapabilities"], ["sequenceFork"])
        capability_checks = [item for item in report["checks"] if item["name"] == "predictor-capability:sequenceFork"]
        self.assertEqual(len(capability_checks), 1)
        self.assertFalse(capability_checks[0]["passed"])
        self.assertIs(capability_checks[0]["actual"], False)

    def test_cli_quality_gate_can_require_selected_input_source(self) -> None:
        db_path = self.root / "quality-gate-input-source.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "quality-gate-input-source-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        check_script = self.root / "selected-input-source.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=true current=im.rime.inputmethod.Squirrel.Hans hitoolboxEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        check_script.chmod(0o755)

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "quality-gate",
                    "--cases-file",
                    str(cases_file),
                    "--min-rag-pass-rate",
                    "1",
                    "--min-sidecar-pass-rate",
                    "1",
                    "--force-side-candidates",
                    "--require-input-source-ready",
                    "--input-source-check-script",
                    str(check_script),
                ]
            )

        self.assertEqual(gate_code, 0)
        report = json.loads(gate_stdout.getvalue())
        self.assertTrue(report["passed"])
        self.assertTrue(report["thresholds"]["requireInputSourceReady"])
        self.assertEqual(report["inputSource"]["schemaVersion"], "rag-ime.debug-input-source.v1")
        checks = {item["name"]: item for item in report["checks"]}
        self.assertTrue(checks["input-source-installed"]["passed"])
        self.assertTrue(checks["input-source-selected"]["passed"])

    def test_cli_quality_gate_fails_when_input_source_is_not_selected(self) -> None:
        db_path = self.root / "quality-gate-input-source-fail.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "quality-gate-input-source-fail-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        check_script = self.root / "unselected-input-source.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=false current=com.apple.keylayout.ABC hitoolboxEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        check_script.chmod(0o755)

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "quality-gate",
                    "--cases-file",
                    str(cases_file),
                    "--min-rag-pass-rate",
                    "1",
                    "--min-sidecar-pass-rate",
                    "1",
                    "--force-side-candidates",
                    "--require-input-source-ready",
                    "--input-source-check-script",
                    str(check_script),
                ]
            )

        self.assertEqual(gate_code, 1)
        report = json.loads(gate_stdout.getvalue())
        checks = {item["name"]: item for item in report["checks"]}
        self.assertTrue(checks["input-source-installed"]["passed"])
        self.assertFalse(checks["input-source-selected"]["passed"])
        self.assertEqual(checks["input-source-selected"]["current"], "com.apple.keylayout.ABC")

    def test_cli_squirrel_tryout_gate_fails_fast_when_input_source_is_not_selected(self) -> None:
        db_path = self.root / "squirrel-tryout-input-source-fail.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "squirrel-tryout-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        check_script = self.root / "tryout-unselected-input-source.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=false current=com.apple.keylayout.ABC hitoolboxEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        check_script.chmod(0o755)
        fake_app = self._write_fake_squirrel_app("tryout-unselected")
        config_path = self._write_fake_squirrel_config(
            "tryout-unselected",
            db_path=db_path,
            project="wisdom-weasel-rag-ime",
        )
        report_path = self.root / "squirrel-tryout-report.json"

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "squirrel-tryout-gate",
                    "--cases-file",
                    str(cases_file),
                    "--input-source-check-script",
                    str(check_script),
                    "--squirrel-app",
                    str(fake_app),
                    "--squirrel-config-path",
                    str(config_path),
                    "--skip-launch-agent",
                    "--skip-sidecar-health",
                    "--report-path",
                    str(report_path),
                ]
            )

        self.assertEqual(gate_code, 1)
        report = json.loads(gate_stdout.getvalue())
        self.assertEqual(report["schemaVersion"], "rag-ime.squirrel-tryout-gate.v1")
        self.assertFalse(report["passed"])
        self.assertEqual(report["inputSource"]["readinessState"], "switch")
        self.assertIsNone(report["inputSourceAudit"])
        self.assertTrue(report["installedBundle"]["ok"])
        self.assertTrue(report["installedRimeConfig"]["ok"])
        self.assertTrue(report["installedRimeDefaults"]["ok"])
        self.assertEqual(report["installedRimeDefaults"]["primarySchema"], "luna_pinyin_simp")
        self.assertEqual(report["installedRimeDefaults"]["pageSize"], 8)
        self.assertTrue(report["installedRimeBuild"]["ok"])
        self.assertIsNone(report["qualityGate"])
        self.assertIn("foreground editor typing verification", report["manualRequired"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertTrue(checks["installed-bundle"]["passed"])
        self.assertTrue(checks["installed-rime-config"]["passed"])
        self.assertTrue(checks["installed-rime-defaults"]["passed"])
        self.assertTrue(checks["installed-rime-build"]["passed"])
        self.assertFalse(checks["input-source-ready"]["passed"])
        self.assertTrue(checks["launch-agent"]["passed"])
        self.assertTrue(checks["launch-agent"]["skipped"])
        self.assertTrue(checks["sidecar-health"]["passed"])
        self.assertTrue(checks["sidecar-health"]["skipped"])
        self.assertTrue(checks["quality-gate"]["skipped"])
        self.assertIn("macOS input menu -> Squirrel - Simplified", report["manualRequired"])
        self.assertIn("scripts/wait_squirrel_typing_ready.sh", report["manualRequired"])
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["schemaVersion"], report["schemaVersion"])

    def test_cli_squirrel_tryout_gate_runs_quality_gate_when_input_source_is_selected(self) -> None:
        db_path = self.root / "squirrel-tryout-selected.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "squirrel-tryout-selected-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        check_script = self.root / "tryout-selected-input-source.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=true current=im.rime.inputmethod.Squirrel.Hans hitoolboxEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        check_script.chmod(0o755)
        fake_app = self._write_fake_squirrel_app("tryout-selected")
        config_path = self._write_fake_squirrel_config(
            "tryout-selected",
            db_path=db_path,
            project="wisdom-weasel-rag-ime",
        )

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "squirrel-tryout-gate",
                    "--cases-file",
                    str(cases_file),
                    "--min-rag-pass-rate",
                    "1",
                    "--min-sidecar-pass-rate",
                    "1",
                    "--input-source-check-script",
                    str(check_script),
                    "--squirrel-app",
                    str(fake_app),
                    "--squirrel-config-path",
                    str(config_path),
                    "--skip-launch-agent",
                    "--skip-sidecar-health",
                ]
            )

        self.assertEqual(gate_code, 0)
        report = json.loads(gate_stdout.getvalue())
        self.assertTrue(report["passed"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertTrue(checks["installed-bundle"]["passed"])
        self.assertTrue(checks["installed-rime-config"]["passed"])
        self.assertTrue(checks["installed-rime-defaults"]["passed"])
        self.assertTrue(checks["installed-rime-build"]["passed"])
        self.assertTrue(checks["input-source-ready"]["passed"])
        self.assertTrue(checks["launch-agent"]["passed"])
        self.assertTrue(checks["launch-agent"]["skipped"])
        self.assertTrue(checks["quality-gate"]["passed"])
        self.assertEqual(report["qualityGate"]["schemaVersion"], "rag-ime.quality-gate.v1")
        self.assertTrue(report["qualityGate"]["thresholds"]["requireInputSourceReady"])

    def test_cli_squirrel_tryout_gate_fails_fast_when_rime_build_is_missing(self) -> None:
        db_path = self.root / "squirrel-tryout-missing-build.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "squirrel-tryout-missing-build-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        check_script = self.root / "tryout-selected-missing-build-input-source.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=true current=im.rime.inputmethod.Squirrel.Hans hitoolboxEnabled=true thirdPartyEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        check_script.chmod(0o755)
        fake_app = self._write_fake_squirrel_app("tryout-missing-build")
        config_path = self._write_fake_squirrel_config(
            "tryout-missing-build",
            db_path=db_path,
            project="wisdom-weasel-rag-ime",
            create_build=False,
        )

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "squirrel-tryout-gate",
                    "--cases-file",
                    str(cases_file),
                    "--input-source-check-script",
                    str(check_script),
                    "--squirrel-app",
                    str(fake_app),
                    "--squirrel-config-path",
                    str(config_path),
                    "--skip-launch-agent",
                    "--skip-sidecar-health",
                ]
            )

        self.assertEqual(gate_code, 1)
        report = json.loads(gate_stdout.getvalue())
        self.assertFalse(report["passed"])
        self.assertFalse(report["installedRimeBuild"]["ok"])
        self.assertIn("build/default.yaml", report["installedRimeBuild"]["missingFiles"])
        self.assertIsNone(report["qualityGate"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertFalse(checks["installed-rime-build"]["passed"])
        self.assertTrue(checks["quality-gate"]["skipped"])

    def test_cli_squirrel_tryout_gate_fails_when_default_rime_schema_is_not_simplified(self) -> None:
        db_path = self.root / "squirrel-tryout-wrong-default.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "squirrel-tryout-wrong-default-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        check_script = self.root / "tryout-selected-wrong-default-input-source.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=true current=im.rime.inputmethod.Squirrel.Hans hitoolboxEnabled=true thirdPartyEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        check_script.chmod(0o755)
        fake_app = self._write_fake_squirrel_app("tryout-wrong-default")
        config_path = self._write_fake_squirrel_config(
            "tryout-wrong-default",
            db_path=db_path,
            project="wisdom-weasel-rag-ime",
            primary_schema="luna_pinyin",
            page_size=5,
        )

        gate_stdout = io.StringIO()
        with redirect_stdout(gate_stdout):
            gate_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "squirrel-tryout-gate",
                    "--cases-file",
                    str(cases_file),
                    "--input-source-check-script",
                    str(check_script),
                    "--squirrel-app",
                    str(fake_app),
                    "--squirrel-config-path",
                    str(config_path),
                    "--skip-launch-agent",
                    "--skip-sidecar-health",
                ]
            )

        self.assertEqual(gate_code, 1)
        report = json.loads(gate_stdout.getvalue())
        self.assertFalse(report["passed"])
        self.assertFalse(report["installedRimeDefaults"]["ok"])
        self.assertEqual(report["installedRimeDefaults"]["primarySchema"], "luna_pinyin")
        self.assertFalse(report["installedRimeDefaults"]["primarySchemaMatches"])
        self.assertEqual(report["installedRimeDefaults"]["pageSize"], 5)
        self.assertFalse(report["installedRimeDefaults"]["pageSizeMatches"])
        self.assertIsNone(report["qualityGate"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertFalse(checks["installed-rime-defaults"]["passed"])
        self.assertTrue(checks["quality-gate"]["skipped"])

    def test_cli_squirrel_tryout_gate_probes_sidecar_rime_suggest(self) -> None:
        db_path = self.root / "squirrel-tryout-sidecar.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "squirrel-tryout-sidecar-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        check_script = self.root / "tryout-sidecar-selected-input-source.sh"
        check_script.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    "echo 'id=im.rime.inputmethod.Squirrel.Hans name=Squirrel - Simplified enabled=true selectable=true selected=true current=im.rime.inputmethod.Squirrel.Hans hitoolboxEnabled=true'",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        check_script.chmod(0o755)
        fake_app = self._write_fake_squirrel_app("tryout-sidecar")
        _MockTryoutSidecarHandler.rime_suggest_calls = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockTryoutSidecarHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            sidecar_base = f"http://127.0.0.1:{server.server_port}"
            config_path = self._write_fake_squirrel_config(
                "tryout-sidecar",
                db_path=db_path,
                project="wisdom-weasel-rag-ime",
                sidecar_url=f"{sidecar_base}/api",
            )
            gate_stdout = io.StringIO()
            with redirect_stdout(gate_stdout):
                gate_code = main(
                    [
                        "--db-path",
                        str(db_path),
                        "squirrel-tryout-gate",
                        "--cases-file",
                        str(cases_file),
                        "--min-rag-pass-rate",
                        "1",
                        "--min-sidecar-pass-rate",
                        "1",
                        "--input-source-check-script",
                        str(check_script),
                        "--squirrel-app",
                        str(fake_app),
                        "--squirrel-config-path",
                        str(config_path),
                        "--skip-launch-agent",
                        "--sidecar-url",
                        sidecar_base,
                    ]
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(gate_code, 0)
        report = json.loads(gate_stdout.getvalue())
        self.assertTrue(report["passed"])
        checks = {item["name"]: item for item in report["checks"]}
        self.assertTrue(checks["launch-agent"]["skipped"])
        self.assertTrue(report["sidecar"]["healthOk"])
        self.assertEqual(report["sidecar"]["rimeSuggest"]["schemaVersion"], "rag-ime.rime-sidecar.v1")
        self.assertEqual(report["sidecar"]["rimeSuggest"]["displayCandidateCount"], 1)
        self.assertEqual(_MockTryoutSidecarHandler.rime_suggest_calls, 1)

    def test_cli_quality_gate_probes_mlx_capability_requirements(self) -> None:
        db_path = self.root / "quality-gate-mlx-capability.sqlite"
        with redirect_stdout(io.StringIO()):
            seed_code = main(["--db-path", str(db_path), "seed-demo", "--reset"])
        self.assertEqual(seed_code, 0)

        cases_file = self.root / "quality-gate-mlx-capability-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "agent-hook",
                    "query": "首次运行自动注入背景记忆",
                    "expectedTerms": ["PROJECT_MEMORY_BLOCK"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockMlxCapabilityHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            gate_stdout = io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "RAG_IME_PREDICTOR_PROVIDER": "mlx",
                    "RAG_IME_PREDICTOR_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "RAG_IME_PREDICTOR_MODEL": "mlx-qwen3.5-0.8b",
                    "RAG_IME_PREDICTOR_TIMEOUT_MS": "1000",
                    "RAG_IME_PREDICTOR_FAILURE_COOLDOWN_MS": "0",
                },
                clear=False,
            ):
                with redirect_stdout(gate_stdout):
                    gate_code = main(
                        [
                            "--db-path",
                            str(db_path),
                            "quality-gate",
                            "--cases-file",
                            str(cases_file),
                            "--min-rag-pass-rate",
                            "1",
                            "--min-sidecar-pass-rate",
                            "1",
                            "--force-side-candidates",
                            "--require-predictor-capability",
                            "promptCache",
                        ]
                    )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(gate_code, 0)
        report = json.loads(gate_stdout.getvalue())
        self.assertTrue(report["passed"])
        self.assertTrue(report["thresholds"]["probePredictorCapabilities"])
        self.assertTrue(report["predictor"]["capabilityProbe"]["ok"])
        self.assertTrue(report["predictor"]["capabilities"]["promptCache"])
        capability_checks = [item for item in report["checks"] if item["name"] == "predictor-capability:promptCache"]
        self.assertEqual(len(capability_checks), 1)
        self.assertTrue(capability_checks[0]["passed"])

    def test_cli_eval_comparison_runs_rag_and_model_on_same_cases(self) -> None:
        db_path = self.root / "comparison.sqlite"
        with redirect_stdout(io.StringIO()):
            import_code = main(
                [
                    "--db-path",
                    str(db_path),
                    "import-codex-history",
                    "--curated",
                    "--memory-context-opt-in",
                    "--path",
                    str(self.history),
                    "--project",
                    "wisdom-weasel-rag-ime",
                ]
            )
        self.assertEqual(import_code, 0)

        cases_file = self.root / "comparison-cases.jsonl"
        cases_file.write_text(
            json.dumps(
                {
                    "id": "squirrel-rag",
                    "query": "Squirrel RAG 输入法候选",
                    "expectedTerms": ["Squirrel", "本地记忆"],
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

        server = ThreadingHTTPServer(("127.0.0.1", 0), _MockComparisonPredictionHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            eval_stdout = io.StringIO()
            with patch.dict(
                os.environ,
                {
                    "RAG_IME_PREDICTOR_PROVIDER": "openai-compatible",
                    "RAG_IME_PREDICTOR_BASE_URL": f"http://127.0.0.1:{server.server_port}",
                    "RAG_IME_PREDICTOR_MODEL": "mock-qwen",
                    "RAG_IME_PREDICTOR_TIMEOUT_MS": "1000",
                },
                clear=False,
            ):
                with redirect_stdout(eval_stdout):
                    eval_code = main(
                        [
                            "--db-path",
                            str(db_path),
                            "eval-comparison",
                            "--cases-file",
                            str(cases_file),
                            "--match",
                            "all",
                            "--repeat",
                            "2",
                        ]
                    )
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()

        self.assertEqual(eval_code, 0)
        report = json.loads(eval_stdout.getvalue())
        self.assertEqual(report["schemaVersion"], "rag-ime.eval-comparison.v1")
        self.assertEqual(report["repeat"]["effectiveCaseCount"], 2)
        self.assertEqual(report["rag"]["passed"], 2)
        self.assertEqual(report["model"]["passed"], 2)
        self.assertTrue(report["model"]["prediction"]["providerConfigured"])
        self.assertEqual(report["model"]["prediction"]["providerName"], "local-openai-compatible")
        self.assertEqual(report["comparison"]["bothPassed"], 2)
        self.assertEqual(report["comparison"]["neitherPassed"], 0)
        self.assertEqual(report["comparison"]["winnerByPassRate"], "tie")
        self.assertEqual(report["comparison"]["cases"][0]["ragPassed"], True)
        self.assertEqual(report["comparison"]["cases"][0]["modelPassed"], True)
        self.assertGreaterEqual(report["rag"]["cacheStats"]["hits"], 1)

    def test_load_eval_cases_and_match_results(self) -> None:
        cases_file = self.root / "manual-cases.jsonl"
        cases_file.write_text(
            json.dumps({"query": "FTS5", "expected_terms": "local-first"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        cases = load_eval_cases(cases_file)
        self.assertEqual(cases[0].expected_terms, ("local-first",))
        core = LocalSqliteCoreClient(self.root / "manual.sqlite")
        core.record_event(
            input_event_from_codex_record(
                load_codex_history_records(self.history, limit=2)[1],
                project="wisdom-weasel-rag-ime",
                curated=True,
                memory_context_opt_in=True,
            )
        )
        suggestions = core.suggest_for_input(current_input="FTS5", project="wisdom-weasel-rag-ime")
        result = evaluate_suggestions(cases[0], suggestions, match="any")
        self.assertTrue(result.passed)
        self.assertEqual(result.first_match_rank, 1)

    def test_candidate_level_all_match_does_not_merge_terms_across_suggestions(self) -> None:
        case = load_eval_cases(
            self._write_cases(
                [
                    {
                        "id": "split-all",
                        "query": "Squirrel 本地记忆",
                        "expectedTerms": ["Squirrel", "本地记忆"],
                    }
                ]
            )
        )[0]
        suggestions = [
            self._suggestion("Squirrel 候选面板", "只提到了 Squirrel"),
            self._suggestion("本地记忆写回", "只提到了本地记忆"),
            self._suggestion("Squirrel 和本地记忆", "同一候选命中两个词"),
        ]
        result = evaluate_suggestions(case, suggestions, match="all")
        self.assertTrue(result.passed)
        self.assertEqual(result.first_match_rank, 3)
        self.assertAlmostEqual(result.reciprocal_rank, 1 / 3)
        self.assertFalse(result.top1_passed)
        self.assertEqual(dict(result.term_first_ranks), {"Squirrel": 1, "本地记忆": 2})

    def test_forbidden_terms_mark_noise_even_when_expected_terms_match(self) -> None:
        case = load_eval_cases(
            self._write_cases(
                [
                    {
                        "id": "noise",
                        "query": "脏拼音",
                        "expectedTerms": ["Rime"],
                        "forbiddenTerms": ["让大模型直接解析脏拼音"],
                    }
                ]
            )
        )[0]
        suggestions = [
            self._suggestion("Rime 候选", "不要让大模型直接解析脏拼音"),
        ]
        result = evaluate_suggestions(case, suggestions, match="any")
        self.assertFalse(result.passed)
        self.assertEqual(result.first_match_rank, 1)
        self.assertEqual(result.forbidden_matched_terms, ("让大模型直接解析脏拼音",))

    def _write_cases(self, items: list[dict[str, object]]) -> Path:
        path = self.root / f"cases-{len(items)}-{id(items)}.jsonl"
        path.write_text(
            "\n".join(json.dumps(item, ensure_ascii=False) for item in items) + "\n",
            encoding="utf-8",
        )
        return path

    def _suggestion(self, surface: str, evidence: str = "") -> InputSuggestion:
        return InputSuggestion(
            suggestion_id=f"sug-{surface}",
            surface_text=surface,
            suggestion_type="phrase",
            source_event_id=1,
            evidence_preview=evidence,
            confidence=1.0,
            expanded_evidence=evidence,
            metadata={"insert_text": surface},
        )


class CodexCliLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(tempfile.TemporaryDirectory(prefix="paw-cli-lifecycle-"))
        self.root = Path(temporary)
        self.db_path = self.root / "rag-ime.sqlite"
        self.core = LocalSqliteCoreClient(self.db_path)
        self.addCleanup(self.core.close)
        self.core.initialize()
        self.predictor = NullPredictionProvider()

    def test_cli_stages_release_workers_and_keep_borrowed_core_usable_on_success_and_error(self) -> None:
        from rag_ime.cli import run_cache_probe, run_rime_sidecar_eval
        from rag_ime.debug_server import DebugImeService

        baseline = set(threading.enumerate())
        cases = self.root / "cases.jsonl"
        cases.write_text(json.dumps({"id": "empty-local-case", "query": "local-only", "expectedTerms": ["local-only"]}) + "\n")
        with patch.object(self.core, "close", wraps=self.core.close) as close:
            report = run_rime_sidecar_eval(
                self.core, self.predictor, db_path=self.db_path, cases_file=cases,
                project="test", match="any", repeat=1, max_visible_candidates=3,
                max_side_candidates=3, sidecar_latency_budget_ms=100,
                rime_cache_ttl_ms=400, force_side_candidates=False,
            )
            self.assertEqual(report["schemaVersion"], "rag-ime.rime-sidecar-eval.v1")
            self.assertEqual(set(threading.enumerate()) - baseline, set())
            cache = run_cache_probe(
                self.core, self.predictor, db_path=self.db_path, project="test",
                current_input="local-only", recent_context="", top_k=3, repeat=1,
                rime_candidates=["本地"], rime_cache_ttl_ms=400, force_side_candidates=False,
            )
            self.assertIn("schemaVersion", cache)
            self.assertEqual(set(threading.enumerate()) - baseline, set())
            self.core.record_event(InputEvent(
                event_id=None, created_at_ms=1, source="voice_final",
                committed_text="真实后续读写", privacy_disposition="allowed", project="test",
            ))
            self.assertGreater(self.core.event_count(), 0)
            with patch.object(DebugImeService, "cache_probe", side_effect=RuntimeError("CLI lifecycle sentinel")):
                with self.assertRaisesRegex(RuntimeError, "CLI lifecycle sentinel"):
                    run_cache_probe(
                        self.core, self.predictor, db_path=self.db_path, project="test",
                        current_input="local-only", recent_context="", top_k=3, repeat=1,
                        rime_candidates=[], rime_cache_ttl_ms=400, force_side_candidates=False,
                    )
            self.assertEqual(set(threading.enumerate()) - baseline, set())
            self.assertIsNotNone(self.core._read_connection)
            self.assertGreater(self.core._read_connection.execute("SELECT COUNT(*) FROM input_events").fetchone()[0], 0)
            self.assertGreater(self.core.event_count(), 0)
            close.assert_not_called()

    def test_default_service_close_adopts_core_and_owned_core_ignores_borrow_flag(self) -> None:
        from rag_ime.debug_server import DebugImeService, DebugServerConfig

        baseline = set(threading.enumerate())
        with patch.object(self.core, "close", wraps=self.core.close) as close:
            service = DebugImeService(DebugServerConfig(
                db_path=self.db_path, core=self.core, predictor=self.predictor, seed_if_empty=False,
            ))
            service.close()
            close.assert_called_once()
        self.assertEqual(set(threading.enumerate()) - baseline, set())
        service = DebugImeService(DebugServerConfig(
            db_path=self.root / "owned.sqlite", predictor=self.predictor,
            seed_if_empty=False, close_core_on_close=False,
        ))
        with patch.object(service.core, "close", wraps=service.core.close) as close:
            service.close()
            close.assert_called_once()
        self.assertEqual(set(threading.enumerate()) - baseline, set())


if __name__ == "__main__":
    unittest.main()
