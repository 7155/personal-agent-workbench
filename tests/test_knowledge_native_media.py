from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rag_ime.embeddings import SentenceTransformerEmbeddingProvider
from rag_ime.knowledge_library import KnowledgeLibraryConfig, KnowledgeLibraryService
from rag_ime.knowledge_library.dense import SqliteDenseIndex
from tests.test_knowledge_native_images import _ImageEncoder


class _MediaEncoder(_ImageEncoder):
    supports_audio = True

    def embed_audio(self, paths, *, batch_size=1):
        return [[0.0, 0.0, 1.0] for _ in paths]

    def embed_query(self, text):
        return [0.0, 0.0, 1.0] if 'sound' in text else super().embed_query(text)


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'local media fixtures require ffmpeg')
class NativeMediaKnowledgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.audio = self.root / 'tone.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=2', str(self.audio)], check=True)
        self.video = self.root / 'red.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=96x96:d=2', '-an', str(self.video)], check=True)
        self.service = KnowledgeLibraryService(KnowledgeLibraryConfig(self.root / 'Knowledge'))
        self.addCleanup(self.service.close)
        self.service.dense_index = SqliteDenseIndex(self.service.config.database_path, _MediaEncoder())
        self.base = self.service.create_base('Public media fixtures', retrieval_config={'mode': 'dense', 'topK': 1})

    def test_audio_import_retains_native_segment_time_hash_and_original(self):
        doc = self.service.import_document(self.base['id'], self.audio)
        self.assertEqual(doc['status'], 'ready', doc)
        hit = self.service.search('sound', base_ids=[self.base['id']])['hits'][0]
        self.assertEqual(hit['documentId'], doc['documentId'])
        self.assertEqual(hit['citation']['modality'], 'audio')
        self.assertEqual(hit['citation']['startSeconds'], 0)
        self.assertAlmostEqual(hit['citation']['endSeconds'], 2, places=2)
        self.assertFalse(hit['citation']['sourceBlocks'][0]['metadata']['transcriptionApplied'])
        digest = hit['citation']['assetSha256']
        blob = self.service.read_document_asset(self.base['id'], doc['documentId'], digest)
        self.assertEqual(hashlib.sha256(blob.data).hexdigest(), digest)
        self.assertEqual(self.service.read_document_source(self.base['id'], doc['documentId']).data, self.audio.read_bytes())
        preview = self.service.preview_chunking(self.base['id'], doc['documentId'], {'strategy': 'fixed'})
        self.assertTrue(any(row['provenance'].get('modality') == 'audio' for row in preview['items']))
        self.service.delete_document(doc['documentId'])
        self.assertEqual(self.service.search('sound', base_ids=[self.base['id']])['hits'], [])

    def test_video_frame_uses_existing_image_encoder_and_time_citation(self):
        doc = self.service.import_document(self.base['id'], self.video)
        self.assertEqual(doc['status'], 'ready', doc)
        hit = self.service.search('red', base_ids=[self.base['id']])['hits'][0]
        self.assertEqual(hit['citation']['modality'], 'image')
        self.assertEqual(hit['citation']['startSeconds'], 0)
        self.assertEqual(hit['citation']['sourceBlocks'][0]['metadata']['sourcePart'], 'video-frame')
        self.assertEqual(self.service.read_document_source(self.base['id'], doc['documentId']).data, self.video.read_bytes())

    def test_audio_without_encoder_fails_explicitly_without_text_substitution(self):
        self.service.dense_index = SqliteDenseIndex(self.service.config.database_path, _ImageEncoder())
        doc = self.service.import_document(self.base['id'], self.audio)
        self.assertEqual(doc['status'], 'failed')
        self.assertEqual(doc['error']['code'], 'audio_embedding_unavailable')
        self.assertEqual(self.service.dense_index.status()['vectorCount'], 0)

    def test_corrupt_media_is_not_a_ready_source_label(self):
        self.audio.write_bytes(b'not a wave file')
        doc = self.service.import_document(self.base['id'], self.audio)
        self.assertEqual(doc['status'], 'failed')
        self.assertEqual(doc['error']['code'], 'invalid_media')
    def test_native_failure_is_failed_not_ready_with_source_label_fallback(self):
        self.service.dense_index.provider.embed_audio = lambda paths, **kw: (_ for _ in ()).throw(RuntimeError('encoder failure'))
        doc = self.service.import_document(self.base['id'], self.audio)
        self.assertEqual(doc['status'], 'failed')
        self.assertEqual(doc['error']['code'], 'media_embedding_failed')
        self.assertEqual(self.service.search('sound', base_ids=[self.base['id']])['hits'], [])

    def test_bounded_audio_leaves_unprocessed_tail_explicit_and_keeps_times(self):
        from rag_ime.knowledge_library.media import parse_native_media, media_spans
        long_audio = self.root / 'long.wav'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=duration=65', str(long_audio)], check=True)
        with patch('rag_ime.knowledge_library.media.MAX_MEDIA_UNITS', 2):
            parsed = parse_native_media(long_audio, supports_images=True, supports_audio=True)
        self.assertEqual(len(parsed.blocks), 2)
        self.assertTrue(parsed.metadata['audioSegmentsTruncated'])
        self.assertEqual([(s['provenance']['startSeconds'], s['provenance']['endSeconds']) for s in media_spans(parsed)], [(0,30),(30,60)])

    def test_video_audio_tracks_share_budget_and_missing_audio_is_explicit(self):
        from rag_ime.knowledge_library.media import parse_native_media
        combined = self.root / 'combined.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.video), '-i', str(self.audio), '-c:v', 'copy', '-c:a', 'aac', '-shortest', str(combined)], check=True)
        parsed = parse_native_media(combined, supports_images=True, supports_audio=True)
        self.assertEqual({b.kind for b in parsed.blocks}, {'image', 'audio'})
        self.assertLessEqual(len(parsed.blocks), 64)
        image_only = parse_native_media(combined, supports_images=True, supports_audio=False)
        self.assertTrue(image_only.metadata['audioOmitted'])
        self.assertEqual({b.kind for b in image_only.blocks}, {'image'})

    def test_cancellation_between_units_discards_parse_result(self):
        from rag_ime.knowledge_library.media import parse_native_media
        calls = []
        def stop():
            calls.append(1)
            return len(calls) > 1
        long_video = self.root / 'long.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=96x96:d=11', str(long_video)], check=True)
        from rag_ime.knowledge_library.models import DocumentParseError
        with self.assertRaises(DocumentParseError) as caught:
            parse_native_media(long_video, supports_images=True, supports_audio=False, should_stop=stop)
        self.assertEqual(caught.exception.code, 'cancelled')

    def test_all_offered_container_formats_decode_and_original_readback(self):
        for suffix in ['.mp3', '.flac', '.ogg', '.m4a', '.mov', '.webm', '.mkv']:
            with self.subTest(suffix=suffix):
                source = self.audio if suffix in {'.mp3', '.flac', '.ogg', '.m4a'} else self.video
                converted = self.root / ('container' + suffix)
                subprocess.run(['ffmpeg', '-v', 'error', '-i', str(source), str(converted)], check=True)
                doc = self.service.import_document(self.base['id'], converted)
                self.assertEqual(doc['status'], 'ready', doc)
                self.assertEqual(self.service.read_document_source(self.base['id'], doc['documentId']).data, converted.read_bytes())

    def test_shorter_audio_track_never_claims_the_full_video_duration(self):
        from rag_ime.knowledge_library.media import parse_native_media
        combined = self.root / 'short-audio.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:s=96x96:d=12', '-i', str(self.audio), '-c:a', 'aac', str(combined)], check=True)
        parsed = parse_native_media(combined, supports_images=True, supports_audio=True)
        audio = [b for b in parsed.blocks if b.kind == 'audio']
        self.assertEqual(len(audio), 1)
        self.assertAlmostEqual(audio[0].metadata['endSeconds'], 2, delta=0.1)
        self.assertGreater(parsed.metadata['durationSeconds'], 11)

    def test_empty_audio_vectors_do_not_publish_a_successful_media_job(self):
        self.service.dense_index.provider.embed_audio = lambda paths, **kw: [[] for _ in paths]
        doc = self.service.import_document(self.base['id'], self.audio)
        self.assertEqual(doc['status'], 'failed')
        self.assertEqual(doc['error']['code'], 'media_embedding_failed')
        self.assertEqual(self.service.dense_index.status()['vectorCount'], 0)

    def test_scope_and_reparse_delete_exclude_old_media_evidence(self):
        doc = self.service.import_document(self.base['id'], self.audio)
        other = self.service.create_base('Unrelated base')
        self.assertEqual(self.service.search('sound', base_ids=[other['id']])['hits'], [])
        retry = self.service.retry_document(doc['documentId'])
        self.assertEqual(retry['revision'], 2)
        self.assertEqual(retry['status'], 'ready')
        self.assertEqual(self.service.dense_index.status()['vectorCount'], 1)



class NativeAudioProviderTests(unittest.TestCase):
    def test_pcm_audio_uses_native_array_and_rejects_wrong_sample_rate(self):
        import wave
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'segment.wav'
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(16000)
                audio.writeframes(b'\0\0' * 16000)
            provider = SentenceTransformerEmbeddingProvider(model='google/embeddinggemma-2')
            class Model:
                def encode(self, inputs, **kwargs):
                    self.inputs = inputs
                    return np.array([[1.0, 0.0]])
            model = Model()
            with patch.object(provider, '_load_model', return_value=model):
                self.assertEqual(provider.embed_audio([str(path)]), [[1.0, 0.0]])
            self.assertEqual(model.inputs[0]['audio']['sampling_rate'], 16000)
            self.assertEqual(len(model.inputs[0]['audio']['array']), 16000)
            with wave.open(str(path), 'wb') as audio:
                audio.setnchannels(1); audio.setsampwidth(2); audio.setframerate(8000)
                audio.writeframes(b'\0\0' * 8000)
            with self.assertRaisesRegex(ValueError, '16 kHz'):
                provider.embed_audio([str(path)])
