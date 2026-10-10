from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os
import sqlite3
import unittest
from unittest.mock import patch
from rag_ime.knowledge_library.store import KnowledgeStore
from rag_ime.knowledge_library.vault import MarkdownVault
from rag_ime.knowledge_library.models import (
    KnowledgeLibraryError,
    KnowledgeConflictError,
)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "notes"
        self.root.mkdir()
        self.vault = MarkdownVault(
            KnowledgeStore(Path(self.temp.name) / "control" / "knowledge.sqlite")
        )
        self.space = self.vault.dispatch({"action": "connect", "root": str(self.root)})[
            "space"
        ]
        self.call("configure", inbox="收件箱")
        (self.root / "concept.md").write_text("# 原笔记\n旧理解。\n")
        self.target = self.call("snapshot")["notes"][0]

    def call(self, action, **args):
        return self.vault.dispatch(
            {"action": action, "vaultId": self.space["id"], **args}
        )

    def saved(self, body="准备做实验，还没有结果。", request="one"):
        return self.call("save", markdown=body, requestId=request, project="demo")

    def proposal(self):
        saved = self.saved()
        source = self.call("read", noteId=saved["noteId"])
        return self.call(
            "prepare",
            noteId=self.target["id"],
            baseRevision=self.target["revision"],
            sourceRefs=[{"noteId": source["noteId"], "revision": source["revision"]}],
            before="旧理解。",
            after="新理解，仍待实验验证。",
            reason="原始材料补充",
        )

    def editor(self, token, action, **args):
        return self.vault.workflow.editor(
            token,
            {
                "action": action,
                "vaultId": self.space["id"],
                "root": str(self.root),
                **args,
            },
        )

    def test_renamed_diary_keeps_derived_identity_and_cannot_be_evidence(self):
        self.saved("GraphRAG 实验计划，尚未执行。")
        exported = self.call("export_day", date="2026-09-20", timezone="Asia/Shanghai")
        original = self.root / exported["path"]
        self.call("configure", captureFolder="captured", captureProject="demo")
        (self.root / "captured").mkdir()
        renamed = self.root / "captured/renamed-review.md"
        original.rename(renamed)
        self.call("snapshot")
        note = next(n for n in self.call("snapshot")["notes"] if n["path"] == "captured/renamed-review.md")
        source = self.call("read", noteId=note["id"])
        with self.assertRaises(KnowledgeLibraryError) as error:
            self.call("suggest_targets", sourceRefs=[{"noteId":note["id"],"revision":source["revision"]}])
        self.assertEqual(error.exception.code, "derived_source")
        self.assertIn("paw_derived_kind: work_diary", renamed.read_text())
        self.assertIn("paw_source_refs:", renamed.read_text())
        with self.vault.store.connection() as db:
            count = db.execute("SELECT COUNT(*) FROM knowledge_vault_materials WHERE note_id=?",(note["id"],)).fetchone()[0]
        self.assertEqual(count, 0)

    def test_target_discovery_searches_beyond_page_limit_without_private_or_self(self):
        for index in range(205):(self.root/f"a{index:03d}.md").write_text("# Unrelated topic")
        (self.root/"z-target.md").write_text("---\naliases: [GraphRAG]\n---\n# 图检索\n关系查询的旧笔记")
        (self.root/"private").mkdir()
        (self.root/"private/secret.md").write_text("GraphRAG private must stay excluded")
        saved=self.saved("GraphRAG 关系查询发现新方法")
        source=self.call("read",noteId=saved["noteId"])
        result=self.call("suggest_targets",sourceRefs=[{"noteId":source["noteId"],"revision":source["revision"]}])
        self.assertEqual(result["candidates"][0]["path"],"z-target.md")
        self.assertFalse(result["remoteProcessing"])
        self.assertNotIn(source["noteId"],[n["id"] for n in result["candidates"]])
        self.assertFalse(any(n["path"].startswith("private/") for n in result["candidates"]))

    def test_ime_preparation_binds_actual_target_and_expires_after_accept(self):
        from types import SimpleNamespace
        from rag_ime.knowledge_control import KnowledgeControlFacade
        facade=object.__new__(KnowledgeControlFacade)
        facade.worker=SimpleNamespace(management_call=lambda method,payload:self.vault.dispatch(payload))
        facade._ime_reference=None
        facade._ime_project_provider=lambda:"actual-input-workspace"
        result=facade.vault({"action":"ime_prepare","vaultId":self.space["id"],"noteIds":[self.target["id"]],"project":"note-origin-project"})
        self.assertEqual(result["targetProject"],"actual-input-workspace")
        self.assertIsNone(facade.ime_reference("note-origin-project"))
        reference=facade.ime_reference("actual-input-workspace")
        self.assertTrue(facade.validate_ime_reference(reference["id"],"actual-input-workspace"))
        self.assertFalse(facade.validate_ime_reference(reference["id"],"actual-input-workspace"))
        facade.vault({"action":"ime_prepare","vaultId":self.space["id"],"noteIds":[self.target["id"]]})
        (self.root/"concept.md").write_text("Changed original")
        with self.assertRaises(ValueError):facade.ime_reference("actual-input-workspace")

    def test_activity_originals_feed_both_paths_and_revoke_before_apply(self):
        source={"id":"evidence:one","text":"计划实验，未执行", "occurredAtMs":1789900000000,
                "origin":{"namespace":"paw:input","id":"one"}}
        packet={"evidence":[source],"admissionRevisions":{"evidence:one":1}}
        self.vault.activity_provider=lambda project,day,timezone:packet
        self.assertEqual(self.call("day",date="2026-09-20",project="demo")["sources"],[])
        self.call("configure",activityProject="demo",timezone="Asia/Shanghai",remoteProcessing=True)
        day=self.call("day",date="2026-09-20",project="demo")
        material=day["sources"][0]
        self.assertEqual(material["text"],source["text"])
        refs=[{"activityId":material["activityId"],"revision":material["revision"]}]
        context=self.call("organize_context",sourceRefs=refs)
        self.assertEqual(context["sources"][0]["markdown"],source["text"])
        proposal=self.call("prepare",noteId=self.target["id"],baseRevision=self.target["revision"],
            sourceRefs=refs,before="旧理解。",after="新理解。",reason="原始来源")
        self.call("store_diary",sourceRefs=refs,date="2026-09-20",project="demo",markdown="仍是计划。",generator="test")
        self.call("configure",activityProject="")
        with self.assertRaises(KnowledgeConflictError):
            self.call("approve",proposalId=proposal["id"],proposalRevision=proposal["revision"])
        self.assertFalse(self.call("day",date="2026-09-20",project="demo")["modelDrafts"][0]["sourcesCurrent"])
        with self.vault.store.connection() as db:
            row=db.execute("SELECT * FROM knowledge_vault_activity_refs").fetchone()
            self.assertNotIn(source["text"],str(tuple(row)))

    def test_authorized_directory_capture_is_incremental_and_stops(self):
        folder = self.root / "材料"
        folder.mkdir()
        (folder / "one.md").write_text("first")
        self.call("configure", captureFolder="材料", captureProject="demo")
        self.call("snapshot")
        self.call("snapshot")
        with self.vault.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM knowledge_vault_materials").fetchone()[0],1)
        (folder / "one.md").write_text("second")
        self.call("snapshot")
        self.call("configure", captureFolder="")
        (folder / "two.md").write_text("not authorized")
        self.call("snapshot")
        with self.vault.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM knowledge_vault_materials").fetchone()[0],2)

    def test_model_diary_survives_restart_and_flags_stale_sources(self):
        saved = self.saved()
        source = self.call("read", noteId=saved["noteId"])
        args = dict(sourceRefs=[{"noteId":source["noteId"],"revision":source["revision"]}],
                    date="2026-09-20", timezone="Asia/Shanghai", project="demo", markdown="机器回顾", generator="test")
        self.call("configure", remoteProcessing=True)
        before_day = self.call("day", date="2026-09-20",timezone="Asia/Shanghai",project="demo")
        first = self.call("store_diary", **args)
        self.assertEqual(first, self.call("store_diary", **args))
        self.vault = MarkdownVault(self.vault.store)
        day = self.call("day", date="2026-09-20", timezone="Asia/Shanghai", project="demo")
        self.assertGreater(day["revision"],before_day["revision"])
        self.assertEqual(day["modelDrafts"][0]["markdown"], "机器回顾")
        self.assertTrue(day["modelDrafts"][0]["sourcesCurrent"])
        (self.root / source["path"]).write_text("changed")
        day = self.call("day", date="2026-09-20", timezone="Asia/Shanghai", project="demo")
        self.assertFalse(day["modelDrafts"][0]["sourcesCurrent"])

    def test_graph_uses_explicit_project_and_readable_sources(self):
        saved = self.saved()
        graph = self.call("graph_business", graphMode="project", project="demo")
        self.assertTrue(any(n["id"] == saved["noteId"] for n in graph["nodes"]))
        self.assertTrue(any(e["label"] == "用于项目" for e in graph["edges"]))
        self.assertEqual(self.call("graph_business", project="other")["nodes"], [])
        growth = self.call("graph_business", graphMode="growth")
        self.assertTrue(any(e["label"] == "来自主动保存" for e in growth["edges"]))
        (self.root / saved["path"]).unlink()
        self.assertEqual(self.call("graph_business")["nodes"], [])

    @patch("rag_ime.knowledge_library.vault_workflow.now_ms",
           return_value=int(datetime(2026, 10, 5, 20, tzinfo=timezone.utc).timestamp() * 1000))
    def test_save_replay_short_question_and_day(self, clock):
        a = self.saved("为什么？")
        b = self.saved("为什么？")
        self.assertEqual(a["noteId"], b["noteId"])
        with self.assertRaises(KnowledgeConflictError):
            self.saved("不同内容")
        day = self.call(
            "day",
            date=datetime.fromtimestamp(clock.return_value / 1000, ZoneInfo("Asia/Shanghai")).date().isoformat(),
            timezone="Asia/Shanghai",
            project="demo",
        )
        self.assertIn("为什么", day["markdown"])
        self.assertEqual(len(day["sources"]), 1)
        self.saved(request="two")
        later = self.call(
            "day", date=day["date"], timezone=day["timezone"], project="demo"
        )
        self.assertEqual(later["id"], day["id"])
        self.assertGreater(later["revision"], day["revision"])
        self.assertIn("准备做实验", later["markdown"])
        self.assertNotIn("已完成实验", later["markdown"])

    @patch("rag_ime.knowledge_library.vault_workflow.now_ms",
           return_value=int(datetime(2026, 10, 5, 20, tzinfo=timezone.utc).timestamp() * 1000))
    def test_current_day_uses_requested_and_configured_calendar(self, _clock):
        with patch.dict(os.environ, {"TZ": "UTC", "RAG_IME_TIMEZONE": "America/Los_Angeles"}):
            # A newly connected vault starts in the configured calendar; saved
            # per-vault choices continue to override later host/env changes.
            self.vault = MarkdownVault(KnowledgeStore(Path(self.temp.name) / "configured" / "knowledge.sqlite"))
            self.space = self.vault.dispatch({"action": "connect", "root": str(self.root)})["space"]
            self.call("configure", inbox="收件箱")
            self.saved("跨日保存，仍是原始材料。")
            default = self.call("day", project="demo")
            self.assertEqual((default["date"], default["timezone"]), ("2026-10-05", "America/Los_Angeles"))
            self.assertEqual(len(default["sources"]), 1)
            requested = self.call("day", timezone="Asia/Shanghai", project="demo")
            self.assertEqual(requested["date"], "2026-10-06")
            self.assertEqual(len(requested["sources"]), 1)
            self.call("configure", activityProject="demo", timezone="Pacific/Auckland")
            configured = self.call("day", project="demo")
            self.assertEqual((configured["date"], configured["timezone"]), ("2026-10-06", "Pacific/Auckland"))
            self.assertEqual(len(configured["sources"]), 1)
            self.call("configure", activityProject="other")
            self.assertEqual(self.call("settings")["policy"]["activityTimezone"], "Pacific/Auckland")
            explicit = self.call("day", timezone="UTC", project="demo")
            self.assertEqual((explicit["date"], explicit["timezone"]), ("2026-10-05", "UTC"))
            self.assertEqual(len(explicit["sources"]), 1)
            historical = self.call("day", date="2026-10-05", timezone="Asia/Shanghai", project="demo")
            self.assertEqual(historical["date"], "2026-10-05")
            self.assertEqual(historical["sources"], [])

    @patch("rag_ime.knowledge_library.vault_workflow.now_ms",
           return_value=int(datetime(2026, 10, 5, 20, tzinfo=timezone.utc).timestamp() * 1000))
    def test_diary_and_receipt_defaults_share_the_requested_date(self, _clock):
        self.call("configure", remoteProcessing=True)
        saved = self.saved("准备验证跨日整理。")
        source = self.call("read", noteId=saved["noteId"])
        request = {"sourceRefs": [{"noteId": source["noteId"], "revision": source["revision"]}],
                   "timezone": "Asia/Shanghai", "project": "demo"}
        diary = self.call("store_diary", **request, markdown="仅覆盖所选计划。", generator="fixture")
        self.assertEqual(diary["date"], "2026-10-06")
        view = self.call("day", date="2026-10-06", timezone="Asia/Shanghai", project="demo")
        self.assertEqual(view["modelDrafts"][0]["id"], diary["id"])
        self.call("organize_receipt", **request,
                  result={"diaryRecord": diary, "diary": "仅覆盖所选计划。", "proposal": None})
        self.vault = MarkdownVault(self.vault.store)
        replay = self.call("organize_lookup", **request, date="2026-10-06")
        self.assertEqual(replay["result"]["diaryRecord"]["id"], diary["id"])
        self.assertTrue(replay["result"]["replayed"])

    def test_private_diary_is_separate_and_export_preserves_edits(self):
        (self.root / "日记").mkdir()
        (self.root / "日记/2026-09-20.md").write_text("私人感受")
        self.call("configure", personalDiary="日记/{date}.md")
        day = self.call("day", date="2026-09-20", timezone="Asia/Shanghai")
        self.assertEqual(day["personalDiary"]["markdown"], "私人感受")
        self.assertNotIn("私人感受", day["markdown"])
        self.assertEqual(self.call("snapshot", query="私人感受")["total"], 0)
        export = self.call("export_day", date="2026-09-20", timezone="Asia/Shanghai")
        (self.root / export["path"]).write_text("用户接管了正文")
        with self.assertRaises(KnowledgeConflictError):
            self.call("export_day", date="2026-09-20", timezone="Asia/Shanghai")
        self.assertEqual((self.root / export["path"]).read_text(), "用户接管了正文")

    def test_no_plugin_approval_and_draft_never_changes_original(self):
        p = self.proposal()
        before = (self.root / "concept.md").read_text()
        accepted = self.call("approve", proposalId=p["id"], proposalRevision=1)
        self.assertEqual(accepted["state"], "waiting_editor")
        result = self.call("draft", proposalId=p["id"], proposalRevision=1)
        self.assertFalse(result["existingNoteUpdated"])
        self.assertEqual((self.root / "concept.md").read_text(), before)

    def test_editor_authenticated_intent_receipt_and_restart_replay(self):
        p = self.proposal()
        token = self.call("pair")["pairingToken"]
        with self.assertRaises(KnowledgeLibraryError):
            self.editor("wrong", "pending")
        with self.assertRaises(KnowledgeConflictError):
            self.editor(token, "begin", proposalId=p["id"])
        self.call("approve", proposalId=p["id"], proposalRevision=1)
        intent = self.editor(token, "begin", proposalId=p["id"])
        with self.assertRaises(KnowledgeConflictError):
            self.editor(
                token,
                "receipt",
                proposalId=p["id"],
                applicationId=intent["applicationId"],
            )
        # Simulate the editor's only write; native editor behavior is tested separately.
        (self.root / intent["path"]).write_text(intent["markdown"])
        self.vault = MarkdownVault(self.vault.store)
        result = self.editor(
            token, "receipt", proposalId=p["id"], applicationId=intent["applicationId"]
        )
        self.assertEqual(result["state"], "saved")
        self.assertEqual(
            self.editor(
                token,
                "receipt",
                proposalId=p["id"],
                applicationId=intent["applicationId"],
            )["state"],
            "saved",
        )
        self.assertEqual((self.root / "concept.md").read_text().count("新理解"), 1)
        self.call("revoke")
        with self.assertRaises(KnowledgeLibraryError):
            self.editor(token, "pending")

    def test_conflict_and_stale_sources_are_not_applied(self):
        p = self.proposal()
        (self.root / "concept.md").write_text("用户同时改了正文")
        with self.assertRaises(KnowledgeConflictError):
            self.call("approve", proposalId=p["id"], proposalRevision=1)
        self.assertEqual((self.root / "concept.md").read_text(), "用户同时改了正文")

    def test_pause_blocks_editor_and_remote_default_off(self):
        token = self.call("pair")["pairingToken"]
        with self.assertRaises(KnowledgeLibraryError):
            self.call("organize_context")
        self.call("pause", paused=True)
        with self.assertRaises(KnowledgeLibraryError):
            self.editor(token, "pending")
        with self.assertRaises(KnowledgeLibraryError):
            self.saved()

    def test_index_failure_records_saved_body(self):
        p = self.proposal()
        token = self.call("pair")["pairingToken"]
        self.call("approve", proposalId=p["id"], proposalRevision=1)
        intent = self.editor(token, "begin", proposalId=p["id"])
        (self.root / intent["path"]).write_text(intent["markdown"])
        with patch.object(self.vault, "snapshot", side_effect=OSError("unavailable")):
            result = self.editor(
                token,
                "receipt",
                proposalId=p["id"],
                applicationId=intent["applicationId"],
            )
        self.assertEqual(result["state"], "saved_index_pending")
        self.assertTrue(result["bodySaved"])

    def test_organizer_replays_same_materials_after_restart_without_provider(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from rag_ime.vault_organizer import organize
        self.call("configure", remoteProcessing=True)
        saved = self.saved()
        source = self.call("read", noteId=saved["noteId"])
        request = {"vaultId":self.space["id"], "sourceRefs":[{"noteId":source["noteId"],"revision":source["revision"]}], "project":"demo", "date":"2026-09-20", "timezone":"Asia/Shanghai"}
        runtime = Mock()
        runtime.complete_once.return_value = {"text":'{"diary":"仅覆盖计划，尚未执行。","action":"none","before":"","after":"","reason":"无目标"}'}
        facade = SimpleNamespace(worker=SimpleNamespace(management_call=lambda method,p:self.vault.dispatch(p)), runtime_provider=lambda:runtime)
        first = organize(facade, request)
        self.vault = MarkdownVault(self.vault.store)
        facade.runtime_provider = None
        second = organize(facade, request)
        self.assertEqual(second["diaryRecord"]["id"], first["diaryRecord"]["id"])
        self.assertTrue(second["replayed"])
        self.assertEqual(runtime.complete_once.call_count, 1)
        self.call("configure", remoteProcessing=False)
        with self.assertRaises(KnowledgeLibraryError):
            organize(facade, request)

    def test_dismissed_organization_is_not_recreated_but_new_material_runs(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from rag_ime.vault_organizer import organize
        self.call("configure", remoteProcessing=True)
        saved = self.saved()
        source = self.call("read", noteId=saved["noteId"])
        request = {"vaultId":self.space["id"], "noteId":self.target["id"], "sourceRefs":[{"noteId":source["noteId"],"revision":source["revision"]}], "date":"2026-09-20"}
        runtime = Mock()
        runtime.complete_once.return_value = {"text":'{"diary":"仅覆盖计划。","action":"append","before":"","after":"计划实验，还未执行。","reason":"新材料补充"}'}
        facade = SimpleNamespace(worker=SimpleNamespace(management_call=lambda method,p:self.vault.dispatch(p)), runtime_provider=lambda:runtime)
        first = organize(facade, request)
        self.call("dismiss", proposalId=first["proposal"]["id"], proposalRevision=first["proposal"]["revision"])
        replay = organize(facade, request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["proposal"]["state"], "dismissed")
        self.assertEqual(runtime.complete_once.call_count, 1)
        (self.root / source["path"]).write_text("实验完成，有新结果。")
        current = self.call("read",noteId=source["noteId"])
        request["sourceRefs"][0]["revision"] = current["revision"]
        organize(facade, request)
        self.assertEqual(runtime.complete_once.call_count, 2)

    def test_forget_stops_recapture_and_invalidates_prepared_ime(self):
        from types import SimpleNamespace
        from rag_ime.knowledge_control import KnowledgeControlFacade
        self.call("configure", captureFolder="captured", captureProject="demo", activityProject="demo")
        (self.root / "captured").mkdir()
        (self.root / "captured/source.md").write_text("Original input")
        self.call("snapshot")
        facade = object.__new__(KnowledgeControlFacade)
        facade.worker = SimpleNamespace(management_call=lambda method,payload:self.vault.dispatch(payload))
        facade._ime_reference = None
        facade._ime_project_provider = lambda:"demo"
        facade.vault({"action":"ime_prepare","vaultId":self.space["id"],"noteIds":[self.target["id"]]})
        facade.vault({"action":"pause","vaultId":self.space["id"],"paused":True})
        facade.vault({"action":"pause","vaultId":self.space["id"],"paused":False})
        self.assertIsNone(facade.ime_reference("demo"))
        facade.vault({"action":"ime_prepare","vaultId":self.space["id"],"noteIds":[self.target["id"]]})
        facade.vault({"action":"forget","vaultId":self.space["id"],"confirm":True})
        self.assertIsNone(facade.ime_reference("demo"))
        self.call("snapshot")
        with self.vault.store.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM knowledge_vault_materials").fetchone()[0], 0)
        policy = self.call("settings")["policy"]
        self.assertEqual(policy["captureFolder"], "")
        self.assertEqual(policy["activityProject"], "")
        self.assertTrue((self.root / "captured/source.md").exists())

    def test_forget_preserves_markdown_and_revokes_pending(self):
        p = self.proposal()
        token = self.call("pair")["pairingToken"]
        self.call("forget", confirm=True)
        self.assertEqual(self.call("proposals")["items"], [])
        self.assertTrue((self.root / "concept.md").exists())
        with self.assertRaises(KnowledgeLibraryError):
            self.editor(token, "pending")

    def test_memory_adoption_uses_real_owner_and_rechecks_changed_statement(self):
        from types import SimpleNamespace
        from rag_ime.local_sqlite_core import LocalSqliteCoreClient
        from rag_ime.vault_memory import adopt, reconcile
        from rag_ime.db import sqlite_connection

        path = Path(self.temp.name) / "core.sqlite"
        core = LocalSqliteCoreClient(path)
        core.initialize(perform_maintenance=False)
        worker = SimpleNamespace(
            management_call=lambda method, payload: self.vault.dispatch(payload)
        )
        facade = SimpleNamespace(
            worker=worker, work_contract=SimpleNamespace(db_path=path)
        )
        args = {
            "vaultId": self.space["id"],
            "noteId": self.target["id"],
            "baseRevision": self.target["revision"],
            "statement": "旧理解。",
            "project": "demo",
            "confirm": True,
        }
        result = adopt(facade, args)
        self.assertTrue(result["adopted"])
        self.assertEqual(adopt(facade, args)["atomId"], result["atomId"])
        recalled = core.build_agent_context(project="demo", query="旧理解", top_k=5)
        self.assertIn("旧理解", str(recalled))
        with sqlite_connection(path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM memory_atoms WHERE id=?", (result["atomId"],)
                ).fetchone()[0],
                "approved",
            )
        (self.root / "concept.md").write_text("# 原笔记\n修正了原先理解。")
        checked = reconcile(facade, {"vaultId": self.space["id"]})
        self.assertEqual(checked["items"][0]["state"], "needs_review")
        with sqlite_connection(path) as db:
            self.assertEqual(
                db.execute(
                    "SELECT status FROM memory_atoms WHERE id=?", (result["atomId"],)
                ).fetchone()[0],
                "disabled",
            )

    def test_export_respects_scope_and_backup_does_not_restore_approval(self):
        import base64, io, zipfile
        from rag_ime.knowledge_library.vault_backup import (
            export_records,
            restore_records,
        )

        p = self.proposal()
        self.call("approve", proposalId=p["id"], proposalRevision=1)
        token = self.call("pair")["pairingToken"]
        (self.root / "私人").mkdir()
        (self.root / "私人/secret.md").write_text("private")
        archive = self.call("export_notes")
        with zipfile.ZipFile(io.BytesIO(base64.b64decode(archive["base64"]))) as z:
            self.assertIn("concept.md", z.namelist())
            self.assertNotIn("私人/secret.md", z.namelist())
        packet = export_records(self.vault.store.database_path.resolve())
        restore_records(self.vault.store.database_path, packet)
        self.assertTrue(self.vault.dispatch({"action": "list"})["spaces"][0]["paused"])
        with self.assertRaises(KnowledgeLibraryError):
            self.editor(token, "pending")
        self.call("pause", paused=False)
        self.assertEqual(self.call("proposals")["items"][0]["state"], "prepared")
        self.assertEqual(self.call("settings")["policy"]["inbox"], "")

    def test_new_file_symlink_escape_blocked(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (self.root / "收件箱").symlink_to(outside)
        with self.assertRaises(OSError):
            self.saved()
        self.assertEqual(list(outside.iterdir()), [])


class VaultBackupExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "knowledge.sqlite"

    def tracked_export(self, *, has_vault=True, fail_read=False):
        from rag_ime.knowledge_library.vault_backup import export_records

        with sqlite3.connect(self.database) as db:
            if has_vault:
                db.execute("CREATE TABLE knowledge_vaults (id TEXT, title TEXT)")
                db.execute("INSERT INTO knowledge_vaults VALUES ('original', '原笔记')")
        db.close()
        original_connect = sqlite3.connect
        connections = []
        close_calls = []
        statements = []

        class TrackedConnection(sqlite3.Connection):
            def close(self):
                close_calls.append(self)
                super().close()

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs, factory=TrackedConnection)
            connections.append(connection)
            self.addCleanup(sqlite3.Connection.close, connection)
            connection.set_trace_callback(statements.append)
            if fail_read:
                connection.set_authorizer(
                    lambda action, table, *_: sqlite3.SQLITE_DENY
                    if action == sqlite3.SQLITE_READ and table == "knowledge_vaults"
                    else sqlite3.SQLITE_OK
                )
            return connection

        with patch("sqlite3.connect", side_effect=connect) as connector:
            if fail_read:
                with self.assertRaises(sqlite3.DatabaseError):
                    export_records(self.database)
                packet = None
            else:
                packet = export_records(self.database)
        connector.assert_called_once_with(self.database.as_uri() + "?mode=ro", uri=True)
        self.assertEqual(len(connections), 1)
        self.assertEqual(close_calls, connections)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        self.assertIn("BEGIN", statements)
        return packet

    def test_export_closes_original_readonly_connection_and_keeps_packet(self):
        self.assertEqual(
            self.tracked_export(),
            {
                "schemaVersion": "paw.vault-control-backup.v1",
                "tables": {"knowledge_vaults": [{"id": "original", "title": "原笔记"}]},
                "restoresAuthority": False,
            },
        )

    def test_export_without_vault_table_closes_connection_before_return(self):
        self.assertIsNone(self.tracked_export(has_vault=False))

    def test_export_read_failure_closes_connection_and_propagates_error(self):
        self.tracked_export(fail_read=True)


if __name__ == "__main__":
    unittest.main()
