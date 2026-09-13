"""Frozen App search adapter; shipped with the unchanged KnowledgeStore owner.

Only a package-local cache is writable. Query encoding uses only the pinned local encoder; documents are never re-embedded.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import types
from pathlib import Path

_LOCK = threading.RLock()
_READY: dict[str, tuple[object, dict, object | None]] = {}


def read_snapshot(root: Path, knowledge: dict) -> dict:
    """Read the exact portable text snapshot without opening a DB or encoder."""
    relative = Path(str(knowledge.get('snapshotFile') or ''))
    if relative.is_absolute() or '..' in relative.parts or not relative.parts:
        raise ValueError('冻结快照路径无效。')
    path = root / relative
    if any((root / Path(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
        raise ValueError('冻结快照不能是符号链接。')
    if not path.is_file() or path.stat().st_size > 80 * 1024 * 1024:
        raise ValueError('知识库应用缺少完整的检索快照。')
    encoded = path.read_bytes()
    return decode_snapshot(encoded, knowledge)


def decode_snapshot(encoded: bytes, knowledge: dict) -> dict:
    """Also usable with an already-read archive member; never extracts files."""
    if len(encoded) > 80 * 1024 * 1024 or hashlib.sha256(encoded).hexdigest() != knowledge.get('snapshotSha256'):
        raise ValueError('知识库快照与此应用版本不一致。')
    try:
        snapshot = json.loads(encoded)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('知识库快照格式无效。') from exc
    if not isinstance(snapshot, dict) or snapshot.get('schemaVersion') != 'paw.knowledge-search-snapshot.v1':
        raise ValueError('知识库快照格式无效。')
    return snapshot


def materialize(root: Path, files: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, text in files.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or relative.parts[0] not in {'knowledge', 'knowledge_owner', 'knowledge_runtime.py', 'app_research.py'}:
            raise ValueError('知识库应用资源路径无效。')
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.is_symlink():
            raise ValueError('知识库应用资源不能是符号链接。')
        if not path.exists():
            with path.open('x', encoding='utf-8') as out:
                os.chmod(path, 0o600)
                out.write(text)
        elif path.read_text(encoding='utf-8') != text:
            raise ValueError('冻结的知识库应用资源发生变化，请重新准备此版本。')


def _open(root: Path, knowledge: dict) -> tuple[object, dict, object | None]:
    path = root / knowledge['snapshotFile']
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 80 * 1024 * 1024:
        raise ValueError('知识库应用缺少完整的检索快照。')
    encoded = path.read_bytes()
    if hashlib.sha256(encoded).hexdigest() != knowledge['snapshotSha256']:
        raise ValueError('知识库快照与此应用版本不一致。')
    dense_meta = knowledge.get('dense') if isinstance(knowledge.get('dense'), dict) else None
    dense_enabled = bool(dense_meta and dense_meta.get('enabled'))
    dense_encoded = b''
    dense_snapshot = None
    if dense_enabled:
        dense_path = root / str(dense_meta.get('snapshotFile') or '')
        if dense_path.is_symlink() or not dense_path.is_file() or dense_path.stat().st_size > 80 * 1024 * 1024:
            raise ValueError('知识库应用缺少完整的 dense 快照。')
        dense_encoded = dense_path.read_bytes()
        if hashlib.sha256(dense_encoded).hexdigest() != str(dense_meta.get('snapshotSha256') or ''):
            raise ValueError('知识库 dense 快照与此应用版本不一致。')
        try:
            dense_snapshot = json.loads(dense_encoded)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('知识库 dense 快照格式无效。') from exc
    dense_suffix = ''
    if dense_enabled:
        dense_suffix = ':' + ':'.join(
            str(dense_meta.get(name) or '')
            for name in ('snapshotSha256', 'providerFingerprint', 'modelRevision', 'denseOwnerSha256')
        )
        provider_payload = dense_meta.get('provider') if isinstance(dense_meta.get('provider'), dict) else knowledge.get('embedding')
        dense_suffix += ':' + hashlib.sha256(
            json.dumps(provider_payload or {}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
        ).hexdigest()
    key = str(root.resolve()) + ':' + knowledge['snapshotSha256'] + ':' + str(knowledge.get('ownerSha256') or '') + dense_suffix
    with _LOCK:
        try:
            snapshot = json.loads(encoded)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError('知识库快照格式无效。') from exc
        owner_root = root / 'knowledge_owner'
        names = ('store.py', 'models.py', 'permissions.py')
        try:
            source_hash = hashlib.sha256(b''.join((owner_root / name).read_bytes() for name in names)).hexdigest()
        except OSError as exc:
            raise ValueError('冻结的 Knowledge 检索组件缺失。') from exc
        if source_hash != knowledge['ownerSha256']:
            raise ValueError('冻结的 Knowledge 检索组件发生变化。')
        if dense_enabled:
            try:
                dense_source_hash = hashlib.sha256(
                    b''.join((owner_root / name).read_bytes() for name in ('dense.py', 'embeddings.py', 'text_utils.py'))
                ).hexdigest()
            except OSError as exc:
                raise ValueError('冻结的 dense Knowledge 组件缺失。') from exc
            if dense_source_hash != str(dense_meta.get('denseOwnerSha256') or ''):
                raise ValueError('冻结的 dense Knowledge 组件发生变化。')
        if key in _READY:
            return _READY[key]
        namespace = 'paw_app_knowledge_' + source_hash + (('_' + dense_source_hash) if dense_enabled else '')
        if namespace not in sys.modules:
            package = types.ModuleType(namespace)
            package.__path__ = [str(owner_root)]
            sys.modules[namespace] = package
        store_type = importlib.import_module(namespace + '.store').KnowledgeStore
        dense_type = None
        provider = None
        if dense_enabled:
            embedding = dense_meta.get('provider') if isinstance(dense_meta.get('provider'), dict) else knowledge.get('embedding')
            if not isinstance(embedding, dict):
                raise ValueError('dense 应用缺少 Embedding Provider 配置。')
            provider_name = str(embedding.get('provider') or '').strip().lower()
            model = str(embedding.get('model') or '').strip()
            model_revision = str(embedding.get('modelRevision') or '').strip()
            model_reference = str(embedding.get('modelReference') or '').strip()
            if provider_name != 'sentence-transformers':
                raise ValueError('portable dense 应用缺少可用的本地 Embedding Provider。')
            if provider_name in {'sentence-transformers', 'mlx-bert'} and not model:
                raise ValueError('portable dense 应用缺少 Embedding 模型。')
            if provider_name == 'sentence-transformers' and not re.fullmatch(r'[0-9a-f]{40}', model_revision):
                raise ValueError('portable dense 应用缺少固定的 Embedding 模型版本。')
            if (
                Path(model).expanduser().is_absolute()
                or model.startswith(("./", "../", "~/", ".\\", "..\\"))
                or bool(re.match(r"^[A-Za-z]:[\\/]", model))
                or model.startswith("\\\\")
                or (bool(model) and Path(model).exists())
                or (bool(model) and Path(model).suffix.lower() in {".gguf", ".bin", ".safetensors", ".mlmodelc"})
                or (bool(model) and model.startswith(("models/", "model/", "checkpoints/", "weights/", "models\\", "model\\")))
            ):
                raise ValueError('portable dense 应用不能读取机器专属的模型路径。')
            embedding_module = importlib.import_module(namespace + '.embeddings')
            environment = {
                'RAG_IME_EMBEDDING_PROVIDER': provider_name,
                'RAG_IME_EMBEDDING_MODEL': model,
                'RAG_IME_EMBEDDING_MODEL_REFERENCE': model_reference,
                'RAG_IME_EMBEDDING_MODEL_REVISION': model_revision,
                'RAG_IME_EMBEDDING_QUERY_PREFIX': str(embedding.get('queryPrefix') or ''),
                'RAG_IME_EMBEDDING_DOCUMENT_PREFIX': str(embedding.get('documentPrefix') or ''),
                'RAG_IME_EMBEDDING_DIMENSIONS': str(int(embedding.get('dimensions') or 0)),
                'RAG_IME_KNOWLEDGE_DENSE_BACKEND': 'sqlite-exact',
            }
            provider = embedding_module.embedding_provider_from_env(environment)
            provider_info = embedding_module.embedding_provider_info(provider)
            if provider_info.get('configured') is not True or str(provider_info.get('fingerprint') or '') != str(dense_meta.get('providerFingerprint') or dense_meta.get('fingerprint') or ''):
                raise ValueError('portable dense 应用的 Embedding Provider 或模型版本不匹配。')
            dense_type = importlib.import_module(namespace + '.dense').SqliteDenseIndex
        if key in _READY:
            return _READY[key]
        cache_parent = root / '.knowledge-cache'
        cache_parent.mkdir(mode=0o700, exist_ok=True)
        cache_name = knowledge['snapshotSha256'] + (('-' + str(dense_meta['snapshotSha256'])[:16]) if dense_enabled else '')
        cache = cache_parent / cache_name
        if not (cache / 'ready').is_file():
            temporary = Path(tempfile.mkdtemp(prefix='building-', dir=cache_parent))
            try:
                store = store_type(temporary / 'knowledge.sqlite')
                store.import_search_snapshot(snapshot)
                dense_index = None
                if dense_enabled and dense_type is not None and provider is not None:
                    dense_index = dense_type(temporary / 'knowledge.sqlite', provider)
                    document_hashes = {str(row['id']): str(row['sha256']) for row in snapshot.get('documents', [])}
                    dense_index.import_snapshot(
                        dense_snapshot,
                        expected_base_id=str(snapshot['base']['id']),
                        expected_document_hashes=document_hashes,
                        expected_fingerprint=str(dense_meta.get('providerFingerprint') or dense_meta.get('fingerprint') or ''),
                        expected_model_revision=str(dense_meta.get('modelRevision') or ''),
                    )
                with store.connection() as connection:
                    connection.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                (temporary / 'ready').write_text(cache_name)
                try:
                    temporary.rename(cache)
                except FileExistsError:
                    if not (cache / 'ready').is_file():
                        raise ValueError('知识库应用缓存未完成，请稍后重试。')
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)
        store = store_type(cache / 'knowledge.sqlite')
        dense_index = None
        if dense_enabled and dense_type is not None and provider is not None:
            dense_index = dense_type(cache / 'knowledge.sqlite', provider)
            expected_count = int(dense_meta.get('vectorCount') or 0)
            observed_count = int(dense_index.status().get('vectorCount') or 0)
            if observed_count != expected_count:
                raise ValueError('portable dense 应用的向量缓存不完整。')
        snapshot.pop('chunks', None)
        snapshot.pop('documents', None)
        if len(_READY) >= 4:
            _READY.pop(next(iter(_READY)))
        _READY[key] = (store, snapshot, dense_index)
        return _READY[key]


def _conversation_query(question: str, conversation: object) -> str:
    # The App sends up to four completed {question, answer} turns. A new chat
    # clears this field; do not guess topic switches from language keywords.
    if not isinstance(conversation, str) or not conversation or len(conversation) > 16000:
        return question
    try:
        turns = json.loads(conversation)
    except (ValueError, RecursionError):
        return question
    if not isinstance(turns, list) or not 1 <= len(turns) <= 4 or not all(
        isinstance(turn, dict) and isinstance(turn.get('question'), str)
        and turn['question'].strip() and isinstance(turn.get('answer'), str)
        for turn in turns
    ):
        return question
    # Keep the current question intact and first. Only user questions provide
    # topic context: old answers and their citation numbers are not evidence.
    query, seen = question, {question.strip()}
    for turn in (turns[0], turns[-1]):
        previous = turn['question'].strip()
        if previous in seen:
            continue
        seen.add(previous)
        remaining = min(1500, 20000 - len(query) - 1)
        if remaining <= 0:
            break
        query += '\n' + previous[:remaining]
    return query


def retrieve(root: Path, knowledge: dict, values: dict) -> dict:
    include_candidate_text = False
    query = values.get(knowledge['queryField'])
    if not isinstance(query, str) or not query.strip() or len(query) > 20000:
        raise ValueError('请输入有效的知识库问题。')
    query = _conversation_query(query, values.get('conversation'))
    profile = knowledge['profile']
    if profile['rerank']:
        raise ValueError('此独立检索包不支持未冻结的重排配置。')
    requested_mode = str(profile.get('mode') or 'lexical')
    if requested_mode not in {'lexical', 'dense', 'hybrid'}:
        raise ValueError('此独立检索包的检索模式无效。')
    if requested_mode in {'dense', 'hybrid'} and not isinstance(knowledge.get('dense'), dict):
        raise ValueError('此独立检索包缺少冻结的 dense projection，不能回退到关键词检索。')
    store, snapshot, dense_index = _open(root, knowledge)
    top_k = profile['topK']
    retrieval_config = snapshot['base'].get('retrievalConfig') if isinstance(snapshot['base'], dict) else {}
    retrieval_config = retrieval_config if isinstance(retrieval_config, dict) else {}
    if retrieval_config.get('graphEnabled') or retrieval_config.get('rerankEnabled'):
        raise ValueError('此独立包未冻结图检索或重排执行器，不能改变实际检索配置。')
    multiplier = max(1, min(20, int(retrieval_config.get('candidateMultiplier') or 4)))
    candidate_limit = min(100, top_k * multiplier)
    lexical_hits = store.search(
        query,
        base_ids=[snapshot['base']['id']],
        limit=candidate_limit,
        agent_only=True,
    ) if requested_mode in {'lexical', 'hybrid'} else []
    dense_hits = []
    if requested_mode in {'dense', 'hybrid'}:
        if dense_index is None:
            raise ValueError('此独立检索包缺少可用的 dense projection。')
        # Dense failures are surfaced to the caller. A semantic App must never
        # silently turn a model/provider failure into lexical evidence.
        scored = dense_index.search(
            query,
            base_ids=[snapshot['base']['id']],
            limit=candidate_limit,
        )
        if not scored:
            raise ValueError('冻结的 dense Provider 没有返回向量候选；此次检索不能回退到关键词。')
        dense_hits = store.hydrate_dense_hits(
            scored,
            base_ids=[snapshot['base']['id']],
            agent_only=True,
        )
        if not dense_hits:
            raise ValueError('冻结的 dense projection 与当前知识库切片不一致；此次检索不能回退到关键词。')
    owner_module = importlib.import_module(type(store).__module__)
    ranker = getattr(owner_module, 'rank_retrieval_hits', None)
    if not callable(ranker):
        raise ValueError('冻结的 Knowledge 检索组件缺少混合排序 owner。')
    hits, effective_mode = ranker(
        lexical_hits,
        dense_hits,
        requested_mode=requested_mode,
        config=retrieval_config,
    )
    hits = [hit for hit in hits if hit.score >= profile['threshold']]
    if requested_mode == 'lexical':
        # Knowledge service admits the lexical top K before its stable
        # tie-break. Preserve that boundary for equal-score FTS candidates.
        hits = hits[:top_k]
        hits.sort(key=lambda hit: (-hit.score, hit.base_id, hit.chunk_id))
    else:
        hits = hits[:top_k]
    sources, remaining = [], profile['contextChars']
    for hit in hits:
        if remaining <= 0:
            break
        source = snapshot['sources'][hit.document_id]
        content = hit.content[:remaining]
        sources.append({**source, 'chunkId': hit.chunk_id, 'text': content, 'score': hit.score,
                        'citationNumber': len(sources) + 1,
                        'citation': _citation(knowledge, source, hit.chunk_id, hit.page, hit.heading, hit.ordinal)})
        remaining -= len(content)
    selected = {source['chunkId'] for source in sources}
    retrieval_hits = [{**snapshot['sources'][hit.document_id], 'chunkId':hit.chunk_id,
                       'preview':hit.content[:180], 'score':hit.score, 'usedInContext':hit.chunk_id in selected,
                       **({'text': hit.content, 'citation': _citation(knowledge, snapshot['sources'][hit.document_id], hit.chunk_id, hit.page, hit.heading, hit.ordinal)} if include_candidate_text else {})} for hit in hits]
    return {'query': query, 'sources': sources, 'retrievalHits':retrieval_hits, 'snapshotSha256': knowledge['snapshotSha256'],
            'indexId': knowledge['sourceIndexId'], 'profile': profile,
            'documentCount': knowledge['documentCount'], 'retrievedChunks': len(hits),
            'contextChars': sum(len(source['text']) for source in sources), 'modelCalls': 0,
            'retrieval': {'mode': requested_mode, 'effectiveMode': effective_mode,
                          'candidateLimit': candidate_limit, 'lexicalCandidates': len(lexical_hits),
                          'denseCandidates': len(dense_hits), 'config': retrieval_config},
            **({'retrievalHitBoundary': 'per_query_top_k_before_context'} if include_candidate_text else {})}


def retrieval_result(value: dict) -> dict:
    sources = value['sources']
    text = '\n\n'.join(f"[{row['sourceId']}] {row['title']}\n{row['text']}\n{row['uri']}" for row in sources)
    return {'text': text or '未检索到符合条件的知识库证据。', 'sources': sources,
            'knowledge': {key: item for key, item in value.items() if key != 'sources'},
            'usage': {'calls': 0, 'source': 'local_knowledge_retrieval'}, 'transport': 'knowledge_snapshot'}


def _citation(knowledge: dict, source: dict, chunk_id: str, page: int | None, heading: str, ordinal: int) -> dict:
    return {'sourceId': source['sourceId'], 'chunkId': chunk_id, 'snapshotSha256': knowledge['snapshotSha256'],
            'page': page, 'heading': heading, 'ordinal': ordinal}
