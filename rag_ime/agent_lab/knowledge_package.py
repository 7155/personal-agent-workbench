"""Read frozen App projections as data; never load or execute package code."""
from __future__ import annotations

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path
from collections.abc import Mapping

from .knowledge_data import KnowledgeIntakeError, content_identities

_LIMIT = 80 * 1024 * 1024


def read_index_package(path: Path) -> dict:
    def read(name: str) -> bytes:
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise KnowledgeIntakeError('索引包的快照路径无效。')
        if path.is_dir():
            target = path / relative
            if any((path / part).is_symlink() for part in [relative, *relative.parents]):
                raise KnowledgeIntakeError('索引包快照不能通过符号链接读取。')
            if not target.is_file() or target.stat().st_size > _LIMIT:
                raise KnowledgeIntakeError('索引包快照缺失或超过读取预算。')
            return target.read_bytes()
        with zipfile.ZipFile(path) as archive:
            info = archive.getinfo(name)
            if info.file_size > _LIMIT or stat.S_ISLNK(info.external_attr >> 16):
                raise KnowledgeIntakeError('索引包快照类型或大小无效。')
            return archive.read(name)
    try:
        app = json.loads(read('app.json'))
        knowledge = app['knowledge']
        search_bytes = read(knowledge['snapshotFile'])
        if hashlib.sha256(search_bytes).hexdigest() != knowledge['snapshotSha256']:
            raise KnowledgeIntakeError('索引包检索快照与冻结哈希不一致。')
        search = json.loads(search_bytes)
        dense = None
        hashes = {'search': knowledge['snapshotSha256']}
        if knowledge.get('dense', {}).get('enabled'):
            meta = knowledge['dense']; dense_bytes = read(meta['snapshotFile'])
            if hashlib.sha256(dense_bytes).hexdigest() != meta['snapshotSha256']:
                raise KnowledgeIntakeError('索引包向量快照与冻结哈希不一致。')
            dense = json.loads(dense_bytes)
            hashes['dense'] = meta['snapshotSha256']
            provider = dense.get('provider', {})
            if (provider.get('provider') != 'sentence-transformers'
                    or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', str(provider.get('model', '')))
                    or not re.fullmatch(r'[a-f0-9]{40}', str(provider.get('modelRevision', '')))
                    or dense.get('modelRevision') != provider['modelRevision']
                    or provider.get('modelReference') != provider.get('model')
                    or dense.get('dimension') != provider.get('dimensions')
                    or dense.get('fingerprint') != meta.get('providerFingerprint', meta.get('fingerprint'))
                    or dense.get('vectorCount') != knowledge.get('chunkCount')):
                raise KnowledgeIntakeError('索引包没有完整且固定版本的本地语义投影。')
        if knowledge.get('profile', {}).get('mode') in {'dense', 'hybrid'} and dense is None:
            raise KnowledgeIntakeError('语义索引包缺少向量，不能回退到关键词。')
        if (knowledge.get('profile', {}).get('rerank') or search['base']['retrievalConfig'].get('graphEnabled')
                or search['base']['retrievalConfig'].get('rerankEnabled')):
            raise KnowledgeIntakeError('此恢复入口尚不支持重排或图检索投影。')
        hashes['manifest'] = hashlib.sha256(json.dumps(knowledge, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        return {'knowledge': knowledge, 'search': search, 'dense': dense, 'hashes': hashes}
    except (KeyError, TypeError, OSError, ValueError, zipfile.BadZipFile) as exc:
        if isinstance(exc, KnowledgeIntakeError): raise
        raise KnowledgeIntakeError('请选择完整的已导出 App 目录或 ZIP；索引文件缺失或格式无效。') from exc


def match_corpus(package: Mapping, documents: list[dict], corpus_hash: str) -> dict[str, str]:
    """Require the entire corpus and original source mapping, not only titles."""
    knowledge, search = package['knowledge'], package['search']
    if knowledge.get('corpusHash') != corpus_hash:
        raise KnowledgeIntakeError('索引包与当前项目的完整语料哈希不一致，不能复用向量。')
    canonical, _aliases = content_identities(documents)
    by_source = {row['sourceId']: row for row in documents}
    expected_hashes = {hashlib.sha256(row['text'].encode()).hexdigest() for row in canonical}
    sources_by_hash = {}
    for row in documents:
        sources_by_hash.setdefault(hashlib.sha256(row['text'].encode()).hexdigest(), set()).add(row['sourceId'])
    if (len(search['documents']) != len(canonical) or knowledge.get('documentCount') != len(canonical)
            or knowledge.get('sourceCount') != len(documents) or knowledge.get('chunkCount') != len(search['chunks'])
            or {d['sha256'] for d in search['documents']} != expected_hashes):
        raise KnowledgeIntakeError('索引包必须覆盖当前完整正文与全部来源，不能用片段或样例替代。')
    mapping, normalized = {}, {}
    for document in search['documents']:
        meta = search['sources'].get(document['id'], {})
        source = by_source.get(meta.get('originalSourceId') or meta.get('sourceId'))
        if (source is None or hashlib.sha256(source['text'].encode()).hexdigest() != document['sha256']
                or len(source['text'].encode()) != document['byteSize']):
            raise KnowledgeIntakeError('索引包来源身份或全文哈希与当前语料不一致。')
        declared_aliases = {a if isinstance(a, str) else a.get('sourceId') for a in meta.get('sourceAliases', [])}
        expected_aliases = sources_by_hash[document['sha256']] - {source['sourceId']}
        if declared_aliases != expected_aliases:
            raise KnowledgeIntakeError('索引包未保留当前完整来源别名映射。')
        mapping[document['id']] = source['externalId']
        normalized[document['id']] = ' '.join(source['text'].split())
    for chunk in search['chunks']:
        text = chunk.get('content')
        if (not isinstance(text, str) or hashlib.sha256(text.encode()).hexdigest() != chunk.get('contentHash')
                or ' '.join(text.split()) not in normalized.get(chunk.get('documentId'), '')):
            raise KnowledgeIntakeError('索引包切片不属于当前来源正文，不能恢复此投影。')
    return mapping
