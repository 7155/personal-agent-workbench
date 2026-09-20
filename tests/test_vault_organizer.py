import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from rag_ime.vault_organizer import organize
from rag_ime.knowledge_library.models import KnowledgeLibraryError


class OrganizerTests(unittest.TestCase):
    def test_disabled_policy_never_calls_any_provider(self):
        runtime = Mock()
        worker = Mock()
        worker.management_call.side_effect = KnowledgeLibraryError(
            "off", code="remote_disabled"
        )
        with patch("rag_ime.jev.evaluate") as jev:
            with self.assertRaises(KnowledgeLibraryError):
                organize(
                    SimpleNamespace(worker=worker, runtime_provider=runtime),
                    {"vaultId": "test"},
                )
            runtime.assert_not_called()
            jev.assert_not_called()

    def test_no_target_is_valid_and_shared_sources_are_sent_once(self):
        context = {
            "sources": [{"noteId": "source", "revision": "v1", "markdown": "准备实验"}],
            "target": None,
            "policy": {"remoteProcessing": True, "jevEnabled": False},
        }
        worker = Mock()
        worker.management_call.return_value = context
        runtime = Mock()
        runtime.complete_once.return_value = {
            "text": json.dumps(
                {
                    "diary": "保存了实验计划，尚未执行。",
                    "action": "none",
                    "before": "",
                    "after": "",
                    "reason": "没有目标",
                }
            )
        }
        result = organize(
            SimpleNamespace(worker=worker, runtime_provider=lambda: runtime),
            {"vaultId": "v", "sourceRefs": [{"noteId": "source", "revision": "v1"}]},
        )
        self.assertIsNone(result["proposal"])
        self.assertEqual(result["state"], "draft_only")
        self.assertEqual(runtime.complete_once.call_count, 1)
        self.assertEqual(
            runtime.complete_once.call_args.kwargs["message"].count("准备实验"), 1
        )

    def test_model_result_cannot_approve_or_invoke_tools(self):
        context = {
            "sources": [],
            "target": None,
            "policy": {"remoteProcessing": True, "jevEnabled": False},
        }
        worker = Mock()
        worker.management_call.return_value = context
        runtime = Mock()
        runtime.complete_once.return_value = {"text": '{"action":"execute"}'}
        with self.assertRaises(KnowledgeLibraryError):
            organize(
                SimpleNamespace(worker=worker, runtime_provider=lambda: runtime),
                {"vaultId": "v"},
            )
        self.assertFalse(
            any(
                c.args[1].get("action") == "approve"
                for c in worker.management_call.call_args_list
            )
        )
