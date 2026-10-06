"""Calendar/read projections only; no provider or Runtime is started."""
from datetime import datetime, timezone
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo
from rag_ime.knowledge_workbench import KnowledgeWorkbenchRequest, build_knowledge_workbench_messages

class WorkbenchCalendarAlignmentTests(unittest.TestCase):
    def test_prompt_relative_ranges_and_current_date_share_the_configured_dst_calendar(self):
        instant = datetime(2026, 3, 8, 4, 30, tzinfo=timezone.utc)
        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant
        with patch.dict(os.environ, {'RAG_IME_TIMEZONE': 'America/New_York'}), patch('rag_ime.knowledge_workbench.datetime', FrozenDatetime):
            messages = build_knowledge_workbench_messages(KnowledgeWorkbenchRequest(question='今天做了什么', mode='recall'), evidence=())
        payload = json.loads(messages[1]['content'])
        self.assertEqual(payload['currentLocalDate'], '2026-03-07')
        zone = ZoneInfo('America/New_York')
        self.assertEqual(payload['requestedTimeRanges'][0]['startMs'], int(datetime(2026, 3, 7, tzinfo=zone).timestamp() * 1000))
        self.assertEqual(payload['requestedTimeRanges'][0]['endMs'], int(datetime(2026, 3, 8, tzinfo=zone).timestamp() * 1000))

    def test_retrieval_uses_the_timeline_calendar_instead_of_the_parser_default(self):
        from rag_ime.debug_server import DebugImeService
        from rag_ime.local_sqlite_core import LocalSqliteCoreClient
        service = DebugImeService.__new__(DebugImeService)
        service.core = LocalSqliteCoreClient.__new__(LocalSqliteCoreClient)
        service.activity_timelines = SimpleNamespace(timezone_name='America/New_York')
        service.runtime_config_snapshot = lambda: SimpleNamespace(hybrid_rag=SimpleNamespace(enabled=True), memory=SimpleNamespace(enabled=True))
        service._temporal_knowledge_evidence = Mock(return_value=())
        service._knowledge_workbench_evidence(KnowledgeWorkbenchRequest(question='昨天做了什么', mode='recall'))
        temporal = service._temporal_knowledge_evidence.call_args.args[1]
        self.assertEqual(temporal.timezone, 'America/New_York')
