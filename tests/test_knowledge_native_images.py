from __future__ import annotations

import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

from rag_ime.embeddings import SentenceTransformerEmbeddingProvider, embedding_provider_info
from rag_ime.knowledge_library import KnowledgeLibraryConfig, KnowledgeLibraryService
from rag_ime.knowledge_library.dense import SqliteDenseIndex
from rag_ime.knowledge_library.models import ParsedAsset, ParsedBlock, ParsedDocument
from rag_ime.knowledge_library.visual import attach_visual_evidence, visual_spans


class _ImageEncoder:
    fingerprint = "fixture:native-image:v1"
    supports_images = True

    def embed(self, text):
        return [0.0, 0.0, 1.0]

    def embed_many(self, texts, *, batch_size=32):
        return [self.embed(text) for text in texts]

    def embed_query(self, text):
        return [0.0, 1.0, 0.0] if "blue" in text else [1.0, 0.0, 0.0]

    def embed_images(self, paths, *, batch_size=1):
        from PIL import Image
        result = []
        for path in paths:
            with Image.open(path) as image:
                pixel = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
            result.append([0.0, 1.0, 0.0] if pixel[2] > pixel[0] else [1.0, 0.0, 0.0])
        return result


@unittest.skipUnless(importlib.util.find_spec("PIL"), "native image fixtures require optional Pillow")
class NativeImageKnowledgeTests(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.red = self.root / "a.png"
        self.blue = self.root / "b.png"
        Image.new("RGB", (96, 96), "red").save(self.red)
        Image.new("RGB", (96, 96), "blue").save(self.blue)
        self.service = KnowledgeLibraryService(KnowledgeLibraryConfig(self.root / "Knowledge"))
        self.addCleanup(self.service.close)
        self.service.dense_index = SqliteDenseIndex(self.service.config.database_path, _ImageEncoder())
        self.base = self.service.create_base("Synthetic visual sources", retrieval_config={"mode": "dense", "topK": 1})

    def test_native_image_hit_retains_original_asset_and_no_ocr_claim(self):
        document = self.service.import_document(self.base["id"], self.red)
        self.assertEqual(document["status"], "ready")
        result = self.service.search("red shape", base_ids=[self.base["id"]])
        hit = result["hits"][0]
        self.assertEqual(hit["documentId"], document["documentId"])
        self.assertEqual(hit["citation"]["modality"], "image")
        digest = hashlib.sha256(self.red.read_bytes()).hexdigest()
        self.assertEqual(hit["citation"]["assetSha256"], digest)
        self.assertIsNone(hit["citation"]["page"])
        self.assertFalse(hit["citation"]["sourceBlocks"][0]["metadata"]["ocrApplied"])
        asset = self.service.read_document_asset(self.base["id"], document["documentId"], digest)
        self.assertEqual(asset.data, self.red.read_bytes())
        self.assertIn(digest, hit["citation"]["assetReadPath"])
        preview = self.service.preview_chunking(self.base["id"], document["documentId"], {"strategy": "fixed"})
        self.assertTrue(any(item["provenance"].get("assetSha256") == digest for item in preview["items"]))

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PDF visual fixture requires optional PyMuPDF")
    def test_scanned_pdf_query_returns_the_exact_page_and_local_asset(self):
        import pymupdf
        source = self.root / "visual.pdf"
        with pymupdf.open() as pdf:
            for path in [self.red, self.blue]:
                page = pdf.new_page(width=200, height=200)
                page.insert_image(page.rect, filename=str(path))
            pdf.save(source)
        document = self.service.import_document(self.base["id"], source)
        self.assertEqual(document["status"], "ready")
        hit = self.service.search("blue shape", base_ids=[self.base["id"]])["hits"][0]
        self.assertEqual(hit["citation"]["page"], 2)
        self.assertEqual(hit["citation"]["modality"], "image")
        asset = self.service.read_document_asset(self.base["id"], document["documentId"], hit["citation"]["assetSha256"])
        self.assertEqual(hashlib.sha256(asset.data).hexdigest(), hit["citation"]["assetSha256"])
        original = self.service.read_document_source(self.base["id"], document["documentId"])
        self.assertEqual(original.data, source.read_bytes())

    def test_text_profile_keeps_the_existing_ocr_requirement(self):
        self.service.dense_index = SqliteDenseIndex(self.service.config.database_path, SentenceTransformerEmbeddingProvider(model="text-only-fixture"))
        document = self.service.import_document(self.base["id"], self.red)
        self.assertEqual(document["status"], "failed")
        self.assertEqual(document["error"]["code"], "mineru_disabled")

    def test_projection_rejects_mixed_dimensions_before_replacing_vectors(self):
        encoder = _ImageEncoder()
        encoder.embed_images = lambda paths, **kwargs: [[1.0, 0.0]]
        index = SqliteDenseIndex(self.root / "projection.sqlite3", encoder)
        with self.assertRaisesRegex(RuntimeError, "incompatible"):
            index.replace_document("d", [{"id": "t", "base_id": "b", "content": "text"},
                {"id": "i", "base_id": "b", "content": "image source", "image_path": str(self.red)}])
        self.assertEqual(index.status()["vectorCount"], 0)


class NativeImageProviderTests(unittest.TestCase):
    def test_embeddinggemma2_defaults_to_official_role_prefixes_and_image_capability(self):
        provider = SentenceTransformerEmbeddingProvider(model="google/embeddinggemma-2")
        self.assertEqual(provider.query_prefix, "task: search result | query: ")
        self.assertEqual(provider.document_prefix, "title: none | text: ")
        self.assertEqual(embedding_provider_info(provider)["modalities"], ["text", "image", "audio"])
        self.assertFalse(SentenceTransformerEmbeddingProvider(model="BAAI/bge-base-zh-v1.5").supports_images)

    def test_remote_and_unsupported_image_references_do_not_become_native_vectors(self):
        digest = "a" * 64
        parsed = ParsedDocument(text="caption", provider="fixture", assets=(ParsedAsset("vector.svg", "image/svg+xml", digest, b"<svg/>"),),
            blocks=(ParsedBlock("image", "remote caption", metadata={"imagePath": "https://example.invalid/image.png"}),
                ParsedBlock("image", "vector caption", metadata={"assetSha256": digest})))
        attached = attach_visual_evidence(parsed, Path("fixture.docx"))
        self.assertEqual(visual_spans(attached), [])
        self.assertEqual(attached.text, "caption")

    def test_visual_unit_limit_is_explicit_and_retains_page_provenance(self):
        assets = tuple(ParsedAsset(f"{i}.png", "image/png", f"{i:064x}", b"fixture") for i in range(65))
        blocks = tuple(ParsedBlock("image", "", page=i + 1, metadata={"assetSha256": asset.sha256}) for i, asset in enumerate(assets))
        attached = attach_visual_evidence(ParsedDocument(text="source", provider="fixture", assets=assets, blocks=blocks), Path("fixture.docx"))
        self.assertTrue(attached.metadata["visualUnitsTruncated"])
        self.assertEqual(attached.metadata["visualUnitCount"], 65)
        spans = visual_spans(attached)
        self.assertEqual(len(spans), 64)
        self.assertEqual(spans[-1]["page"], 64)


if __name__ == "__main__":
    unittest.main()
