from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from rag_ime.knowledge_library.store import KnowledgeStore
from rag_ime.knowledge_library.vault import MarkdownVault
from rag_ime.knowledge_library.models import KnowledgeLibraryError


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name)
        self.root = base / "notes"
        self.root.mkdir()
        self.vault = MarkdownVault(
            KnowledgeStore(base / "control" / "knowledge.sqlite")
        )
        self.space = self.vault.dispatch({"action": "connect", "root": str(self.root)})[
            "space"
        ]

    def call(self, action, **kwargs):
        return self.vault.dispatch(
            {"action": action, "vaultId": self.space["id"], **kwargs}
        )

    def write(self, name, text):
        p = self.root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        return p

    def test_readthrough_search_links_and_external_edit(self):
        self.write(
            "知识/缓存.md",
            "---\naliases: [cache, 缓存策略]\n---\n# 缓存\n完整正文不是摘要\n[[索引#失效]]",
        )
        self.write("知识/索引.md", "# 索引\n## 生效\n版本一")
        snap = self.call("snapshot")
        self.assertEqual(len(snap["notes"]), 2)
        self.assertEqual(snap["edges"][0]["kind"], "explicit_link")
        self.assertFalse(snap["edges"][0]["locatorValid"])
        found = self.call("snapshot", query="CACHE")["notes"]
        self.assertEqual(len(found), 1)
        note = found[0]
        first = self.call("read", noteId=note["id"])
        self.assertIn("完整正文不是摘要", first["markdown"])
        self.write(note["path"], "# 缓存\n用户在外部编辑器更新的完整正文")
        second = self.call("read", noteId=note["id"])
        self.assertNotEqual(first["revision"], second["revision"])
        self.assertIn("外部编辑器", second["markdown"])
        self.assertEqual(len(list(self.root.rglob("*.md"))), 2)

    def test_exclusions_never_leak_into_search_graph_or_counts(self):
        self.write("public.md", "# 公开\n[[秘密]]")
        self.write("私人/秘密.md", "# 秘密\n保密")
        self.write("日记/2026-09-20.md", "# 本人日记")
        snap = self.call("snapshot")
        self.assertEqual(snap["total"], 1)
        self.assertEqual(snap["edges"], [])
        self.assertEqual(self.call("snapshot", query="保密")["notes"], [])

    def test_rename_preserves_identity_but_rename_plus_edit_is_not_guessed(self):
        p = self.write("a.md", "# A")
        a = self.call("snapshot")["notes"][0]
        p.rename(self.root / "b.md")
        b = self.call("snapshot")["notes"][0]
        self.assertEqual(a["id"], b["id"])
        (self.root / "b.md").rename(self.root / "c.md")
        self.write("c.md", "# changed")
        c = self.call("snapshot")["notes"][0]
        self.assertNotEqual(c["id"], b["id"])
        self.assertEqual(c["identityState"], "new_unlinked")

    def test_explicit_identity_and_duplicate_detection(self):
        p = self.write("a.md", "---\npaw_note_id: stable\n---\n# A")
        a = self.call("snapshot")["notes"][0]
        p.rename(self.root / "b.md")
        self.write("b.md", "---\npaw_note_id: stable\n---\n# B")
        b = self.call("snapshot")["notes"][0]
        self.assertEqual(a["id"], b["id"])
        self.write("c.md", "---\npaw_note_id: stable\n---\n# Copied")
        notes = self.call("snapshot")["notes"]
        self.assertEqual(len({n["id"] for n in notes}), 2)
        self.assertTrue(all(n["identityState"] == "duplicate_id" for n in notes))

    def test_symlink_pause_and_resume_scope(self):
        outside = self.root.parent / "outside.md"
        outside.write_text("not permitted")
        (self.root / "leak.md").symlink_to(outside)
        self.write("a.md", "# A")
        snap = self.call("snapshot")
        self.assertEqual(snap["total"], 1)
        note = snap["notes"][0]
        self.call("pause", paused=True)
        for action in ["snapshot", "read"]:
            with self.assertRaises(KnowledgeLibraryError):
                self.call(action, noteId=note["id"])
        self.call("pause", paused=False)
        self.assertEqual(self.call("snapshot")["total"], 1)
        (self.root / "a.md").unlink()
        (self.root / "a.md").symlink_to(outside)
        with self.assertRaises(KnowledgeLibraryError):
            self.call("read", noteId=note["id"])

    def test_reconnect_preserves_authorization_and_no_metadata_writes(self):
        p = self.write("a.md", "# A")
        before = p.read_bytes()
        self.call("snapshot")
        again = self.vault.dispatch(
            {"action": "connect", "root": str(self.root), "excluded": []}
        )["space"]
        self.assertEqual(again["excluded"], self.space["excluded"])
        self.assertEqual(p.read_bytes(), before)
        self.assertFalse((self.root / ".paw").exists())


if __name__ == "__main__":
    unittest.main()
