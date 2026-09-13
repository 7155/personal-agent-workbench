"""Frozen, read-only research tools. Pi owns all model/tool iteration.

execute is a journal transition: callers must serialize per App call and durably
append journalEntry before delivering result to Pi. It never persists business
state, creates a Session, or calls a completion Provider. Search delegates to the
unchanged frozen ranking owner; all reading uses its verified exported chunks.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import re
from pathlib import Path

try:
    from . import app_knowledge_runtime as knowledge_runtime
except ImportError:  # Frozen standalone package.
    _runtime_spec = importlib.util.spec_from_file_location('paw_frozen_research_knowledge', Path(__file__).with_name('knowledge_runtime.py'))
    knowledge_runtime = importlib.util.module_from_spec(_runtime_spec)
    _runtime_spec.loader.exec_module(knowledge_runtime)

EVIDENCE_REUSE_PROTOCOL = 'paw.app-evidence-reuse.v1'


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _integer(value, low, high, name):
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f'{name} must be an integer in {low}–{high}.')
    return value


def normalize_workflow(value: dict, profile: dict) -> dict:
    total = _integer(profile.get('contextChars'), 1000, 60000, 'contextChars')
    defaults = {'kind': 'adaptive_research', 'version': 1, 'maxToolCalls': 8,
                'maxSearchCalls': 4, 'perQueryTopK': profile['topK'],
                'maxReadChars': min(4000, total), 'maxSearchChars': min(3000, total // 2),
                'readReserveChars': total // 2}
    if not isinstance(value, dict) or set(value) - {*defaults, 'historyField'}:
        raise ValueError('Unknown research workflow fields.')
    config = {**defaults, **value}
    if config['kind'] != 'adaptive_research' or type(config['version']) is not int or config['version'] != 1:
        raise ValueError('Unsupported research workflow.')
    for key, low, high in [('maxToolCalls', 1, 20), ('maxSearchCalls', 1, 8),
                           ('perQueryTopK', 1, 100), ('maxReadChars', 1, total),
                           ('maxSearchChars', 1, total), ('readReserveChars', 1, total - 1)]:
        _integer(config[key], low, high, key)
    if config['maxSearchCalls'] > config['maxToolCalls'] or config['perQueryTopK'] != profile['topK']:
        raise ValueError('Research must preserve the frozen retrieval topK and bounded search count.')
    if 'historyField' in config and (not isinstance(config['historyField'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,63}', config['historyField'])):
        raise ValueError('Invalid research history field.')
    return config


class FrozenResearchReader:
    def __init__(self, root: Path, knowledge: dict, workflow: dict, *, call_id: str, search=None, snapshot_bytes: bytes | None = None,
                 seed_sources: list[dict] | None = None, seed_context: dict | None = None):
        if not isinstance(call_id, str) or not call_id or len(call_id) > 500:
            raise ValueError('Research requires an App call identity.')
        self.root, self.knowledge = Path(root), copy.deepcopy(knowledge)
        self.config = normalize_workflow(workflow, knowledge['profile'])
        self.total = knowledge['profile']['contextChars']
        self.snapshot = (knowledge_runtime.decode_snapshot(snapshot_bytes, knowledge) if snapshot_bytes is not None
                         else knowledge_runtime.read_snapshot(self.root, knowledge))
        self.search = search or knowledge_runtime.retrieve
        self.binding = {'callId': call_id, 'knowledgeHash': _hash(self.knowledge),
                        'snapshotSha256': knowledge['snapshotSha256'], 'workflow': self.config}
        self.documents, self.chunks, self.by_document, self.by_source = {}, {}, {}, {}
        self._validate_snapshot()
        self.seed_sources, self.evidence_reuse = self._seed(seed_sources, seed_context)
        if self.evidence_reuse:
            self.binding['evidenceSeedSha256'] = _hash({'sources': self.seed_sources, 'reuse': self.evidence_reuse})
        self.binding_hash = _hash(self.binding)

    def _seed(self, sources, context):
        """Only an owner-supplied completed-call result can reach this seam.

        Revalidate every actually delivered range against the same snapshot;
        never promote an old answer, title, or whole unread chunk to evidence.
        """
        if sources is None and context is None:
            return [], None
        if not isinstance(sources, list) or len(sources) > 1000 or not isinstance(context, dict):
            raise ValueError('Invalid verified evidence seed.')
        source_call = context.get('sourceCallId')
        if not isinstance(source_call, str) or not re.fullmatch(r'lab-app-call-[a-f0-9]{32}', source_call):
            raise ValueError('Evidence reuse requires the original App call identity.')
        version = _integer(context.get('sourceAppVersion'), 1, 2**53 - 1, 'sourceAppVersion')
        seeds, identities, numbers = [], {}, {}
        total = 0
        for source in sources:
            if not isinstance(source, dict):
                raise ValueError('Invalid evidence seed source.')
            key = source.get('chunkId')
            chunk = self.chunks.get(key) if isinstance(key, str) else None
            if not chunk:
                raise ValueError('Evidence seed chunk is outside the frozen snapshot.')
            original = self.snapshot['sources'][chunk['documentId']]
            citation = source.get('citation')
            if (source.get('sourceId') != original['sourceId'] or source.get('documentId', chunk['documentId']) != chunk['documentId']
                    or source.get('contentSha256') != chunk['contentHash'] or not isinstance(citation, dict)
                    or citation.get('snapshotSha256') != self.knowledge['snapshotSha256']
                    or citation.get('chunkId') != key or citation.get('sourceId') != original['sourceId']
                    or citation.get('page') != chunk.get('page') or citation.get('ordinal') != chunk['ordinal']
                    or citation.get('heading', '') != (chunk.get('heading') or '')):
                raise ValueError('Evidence seed identity/page/hash does not match the frozen original.')
            number = _integer(source.get('citationNumber'), 1, 200000, 'citationNumber')
            if (number in numbers and numbers[number] != key) or (key in identities and identities[key] != number):
                raise ValueError('Evidence seed citation identity is inconsistent.')
            identities[key], numbers[number] = number, key
            segments = source.get('segments')
            if segments is None:
                segments = [{'charStart': citation.get('charStart'), 'charEnd': citation.get('charEnd'), 'text': source.get('text')}]
            if not isinstance(segments, list) or not segments or len(segments) > 1000:
                raise ValueError('Evidence seed requires actual delivered windows.')
            verified = []
            previous_end = -1
            for segment in segments:
                if not isinstance(segment, dict):
                    raise ValueError('Invalid evidence seed window.')
                start = _integer(segment.get('charStart'), 0, len(chunk['content']), 'charStart')
                end = _integer(segment.get('charEnd'), start + 1, len(chunk['content']), 'charEnd')
                if start < previous_end or segment.get('text') != chunk['content'][start:end]:
                    raise ValueError('Evidence seed text/range differs from the frozen original.')
                previous_end = end
                verified.append({'charStart': start, 'charEnd': end, 'text': chunk['content'][start:end]})
            if 'charRanges' in citation and citation['charRanges'] != [[v['charStart'], v['charEnd']] for v in verified]:
                raise ValueError('Evidence seed citation ranges disagree with delivered windows.')
            text = ''.join(('\n[…]\n' if i and verified[i - 1]['charEnd'] != v['charStart'] else '') + v['text'] for i, v in enumerate(verified))
            if source.get('text') not in (text, '\n[…]\n'.join(v['text'] for v in verified)):
                raise ValueError('Evidence seed summary text is not its delivered original windows.')
            for segment in verified:
                start, end = segment['charStart'], segment['charEnd']
                if any(row['chunkId'] == key and row['citation']['charStart'] < end and row['citation']['charEnd'] > start for row in seeds):
                    raise ValueError('Evidence seed windows overlap or duplicate.')
                total += end - start
                if total > self.total:
                    raise ValueError('Verified prior evidence exceeds this call context budget.')
                precise = knowledge_runtime._citation(self.knowledge, original, key, chunk.get('page'), chunk.get('heading') or '', chunk['ordinal'])
                precise.update(charStart=start, charEnd=end)
                seeds.append({'sourceId': original['sourceId'], 'documentId': chunk['documentId'], 'chunkId': key,
                    'title': str(original.get('title') or self.documents[chunk['documentId']].get('title') or ''),
                    **({'uri': original['uri']} if isinstance(original.get('uri'), str) else {}),
                    'text': segment['text'], 'contentSha256': chunk['contentHash'], 'citationNumber': number,
                    'citation': precise, 'sourceFormat': 'extracted_text', 'sourceCallId': source_call})
        reuse = {'schemaVersion': EVIDENCE_REUSE_PROTOCOL, 'sourceCallId': source_call, 'sourceAppVersion': version,
                 'snapshotSha256': self.knowledge['snapshotSha256'], 'sourceCount': len({s['sourceId'] for s in seeds}),
                 'chunkCount': len(identities), 'windowCount': len(seeds), 'contextChars': total, 'toolCalls': 0, 'providerCalls': 0}
        return seeds, reuse

    def _validate_snapshot(self):
        snapshot = self.snapshot
        docs, chunks, sources = snapshot.get('documents'), snapshot.get('chunks'), snapshot.get('sources')
        if not isinstance(docs, list) or not isinstance(chunks, list) or not isinstance(sources, dict) or len(docs) > 20000 or len(chunks) > 200000:
            raise ValueError('Invalid frozen document/chunk mapping.')
        for doc in docs:
            key = doc.get('id') if isinstance(doc, dict) else None
            source = sources.get(key)
            if not isinstance(key, str) or not key or key in self.documents or not isinstance(source, dict):
                raise ValueError('Invalid frozen document identity.')
            source_id = source.get('sourceId')
            if not isinstance(source_id, str) or not source_id or source_id in self.by_source:
                raise ValueError('Invalid frozen source identity.')
            self.documents[key], self.by_source[source_id], self.by_document[key] = doc, key, []
        total_bytes = 0
        for chunk in chunks:
            if not isinstance(chunk, dict) or chunk.get('documentId') not in self.documents:
                raise ValueError('Chunk outside the frozen document mapping.')
            key, content = chunk.get('id'), chunk.get('content')
            if not isinstance(key, str) or not key or key in self.chunks or not isinstance(content, str):
                raise ValueError('Invalid frozen chunk identity.')
            _integer(chunk.get('ordinal'), 0, 200000, 'ordinal')
            if chunk.get('page') is not None:
                _integer(chunk['page'], 1, 1000000, 'page')
            total_bytes += len(content.encode())
            if total_bytes > 64 * 1024 * 1024 or hashlib.sha256(content.encode()).hexdigest() != chunk.get('contentHash'):
                raise ValueError('Frozen chunk content hash mismatch.')
            self.chunks[key] = chunk
            self.by_document[chunk['documentId']].append(chunk)
        for key, rows in self.by_document.items():
            rows.sort(key=lambda c: c['ordinal'])
            if len(rows) != self.documents[key].get('chunkCount') or [c['ordinal'] for c in rows] != list(range(len(rows))):
                raise ValueError('Frozen document chunk order/count mismatch.')
        for key, count in [('documentCount', len(docs)), ('chunkCount', len(chunks))]:
            if key in self.knowledge and self.knowledge[key] != count:
                raise ValueError('Frozen snapshot counts do not match the App.')

    def _state(self, journal):
        if not isinstance(journal, list) or len(journal) > 256:
            raise ValueError('Invalid research journal.')
        state = {'head': '', 'seen': {}, 'citations': {}, 'metadata': set(), 'calls': 0,
                 'searchCalls': 0, 'readCalls': 0, 'executedToolCalls': 0,
                 'executedSearchCalls': 0, 'executedReadCalls': 0, 'readChars': 0, 'discoveryChars': 0,
                 'reusedChars': sum(len(s['text']) for s in self.seed_sources)}
        for source in self.seed_sources:
            key = source['chunkId']
            state['citations'][key] = source['citationNumber']
            state['seen'].setdefault(key, []).append((source['citation']['charStart'], source['citation']['charEnd']))
        identities = set()
        for i, entry in enumerate(journal):
            if (not isinstance(entry, dict) or entry.get('bindingHash') != self.binding_hash
                    or entry.get('sequence') != i or entry.get('previousHash') != state['head']
                    or entry.get('entryHash') != _hash({k: v for k, v in entry.items() if k != 'entryHash'})
                    or entry.get('toolCallId') in identities):
                raise ValueError('Research journal identity or chain mismatch.')
            identities.add(entry['toolCallId'])
            result, usage = entry['result'], entry['usage']
            state['calls'] += 1
            state['searchCalls'] += int(result['operation'] == 'search')
            state['readCalls'] += int(result['operation'] in {'find', 'open'})
            if usage['executed']:
                state['executedToolCalls'] += 1
                state['executedSearchCalls'] += int(result['operation'] == 'search')
                state['executedReadCalls'] += int(result['operation'] in {'find', 'open'})
            state['readChars'] += usage['readChars']
            state['discoveryChars'] += usage['discoveryChars']
            for source in result['sources']:
                key = source['chunkId']
                state['citations'][key] = source['citationNumber']
                state['seen'].setdefault(key, []).append((source['citation']['charStart'], source['citation']['charEnd']))
            state['metadata'].update(usage['metadataIds'])
            state['head'] = entry['entryHash']
        if state['readChars'] + state['discoveryChars'] + state['reusedChars'] > self.total:
            raise ValueError('Research journal exceeds its frozen evidence budget.')
        return state

    def _budget(self, state):
        used = state['readChars'] + state['discoveryChars'] + state['reusedChars']
        return {'maxToolCalls': self.config['maxToolCalls'], 'toolCalls': state['calls'],
                'maxSearchCalls': self.config['maxSearchCalls'], 'searchCalls': state['searchCalls'],
                'sourceReadCalls': state['readCalls'], 'executedToolCalls': state['executedToolCalls'],
                'executedSearchCalls': state['executedSearchCalls'], 'executedSourceReadCalls': state['executedReadCalls'],
                'maxContextChars': self.total, 'contextChars': used, 'readContextChars': state['readChars'],
                'discoveryContextChars': state['discoveryChars'], 'reusedContextChars': state['reusedChars'], 'readReserveChars': self.config['readReserveChars'],
                'remainingContextChars': self.total - used,
                'remainingToolCalls': max(0, self.config['maxToolCalls'] - state['calls']),
                'remainingSearchCalls': max(0, self.config['maxSearchCalls'] - state['searchCalls'])}

    def execute(self, arguments: dict, *, tool_call_id: str, journal: list[dict]) -> dict:
        if not isinstance(tool_call_id, str) or not tool_call_id or len(tool_call_id) > 500 or not isinstance(arguments, dict):
            raise ValueError('Research requires toolCallId and structured arguments.')
        args = copy.deepcopy(arguments)
        state = self._state(journal)
        for entry in journal:
            if entry['toolCallId'] == tool_call_id:
                if entry['arguments'] != args:
                    raise ValueError('This toolCallId already belongs to different arguments.')
                return {'result': copy.deepcopy(entry['result']), 'journalEntry': None, 'replayed': True, 'expectedJournalHead': state['head']}
        operation = args.get('op', args.get('operation', ''))
        if not isinstance(operation, str):
            raise ValueError('Research operation must be text.')
        if operation == 'read_source':
            operation = 'open'
        result = {'schemaVersion': 'paw.app-research-result.v1', 'operation': operation, 'status': 'ok',
                  'snapshotSha256': self.knowledge['snapshotSha256'], 'sources': [], 'existingCitations': [], 'untrustedData': True}
        usage = {'executed': False, 'readChars': 0, 'discoveryChars': 0, 'metadataIds': []}
        try:
            if state['calls'] >= self.config['maxToolCalls'] or (operation == 'search' and state['searchCalls'] >= self.config['maxSearchCalls']):
                result.update(status='budget_exhausted', message='Research tool budget exhausted; answer supported parts and retain gaps.')
            else:
                self._dispatch(operation, args, state, result, usage)
        except ValueError as exc:
            result.update(status='invalid_argument', message=str(exc), sources=[], existingCitations=[])
            usage = {'executed': False, 'readChars': 0, 'discoveryChars': 0, 'metadataIds': []}
        except Exception as exc:
            # Tool failure is observable and replayable, never evidence absence.
            result.update(status='tool_error', message=f'Frozen knowledge operation failed ({type(exc).__name__}); no evidence absence is inferred.', sources=[], existingCitations=[])
            usage = {'executed': False, 'readChars': 0, 'discoveryChars': 0, 'metadataIds': []}
        entry = {'schemaVersion': 'paw.app-research-journal.v1', 'sequence': len(journal), 'previousHash': state['head'],
                 'bindingHash': self.binding_hash, 'toolCallId': tool_call_id, 'arguments': args, 'result': result, 'usage': usage}
        updated = copy.deepcopy(state)
        updated['calls'] += 1
        updated['searchCalls'] += int(operation == 'search')
        updated['readCalls'] += int(operation in {'find', 'open'})
        updated['readChars'] += usage['readChars']
        updated['discoveryChars'] += usage['discoveryChars']
        if usage['executed']:
            updated['executedToolCalls'] += 1
            updated['executedSearchCalls'] += int(operation == 'search')
            updated['executedReadCalls'] += int(operation in {'find', 'open'})
        result['budget'] = self._budget(updated)
        entry['entryHash'] = _hash(entry)
        return {'result': copy.deepcopy(result), 'journalEntry': entry, 'replayed': False, 'expectedJournalHead': state['head']}

    def summarize(self, journal: list[dict]) -> dict:
        state = self._state(journal)
        sources = {}
        for rows in [self.seed_sources, *[entry['result']['sources'] for entry in journal]]:
            for row in rows:
                source = sources.setdefault(row['chunkId'], {**copy.deepcopy(row), 'text': '', 'segments': []})
                source['segments'].append({'charStart': row['citation']['charStart'], 'charEnd': row['citation']['charEnd'], 'text': row['text']})
        for source in sources.values():
            segments = sorted(source['segments'], key=lambda v: v['charStart'])
            source['segments'] = segments
            source['text'] = ''.join(('\n[…]\n' if i and segments[i - 1]['charEnd'] != segment['charStart'] else '')
                                     + segment['text'] for i, segment in enumerate(segments))
            source['citation']['charRanges'] = [[s['charStart'], s['charEnd']] for s in segments]
            source['citation'].pop('charStart', None)
            source['citation'].pop('charEnd', None)
        return {'schemaVersion': 'paw.app-research-summary.v1', 'binding': copy.deepcopy(self.binding),
                'execution': 'pi_tool_loop', 'config': copy.deepcopy(self.config), 'budget': self._budget(state),
                'sources': sorted(sources.values(), key=lambda s: s['citationNumber']),
                'toolCallCount': state['calls'], 'searchCount': state['searchCalls'], 'sourceReadCount': state['readCalls'],
                'executedToolCallCount': state['executedToolCalls'], 'executedSearchCallCount': state['executedSearchCalls'],
                'executedSourceReadCallCount': state['executedReadCalls'], 'contextChars': state['readChars'] + state['discoveryChars'] + state['reusedChars'],
                **({'evidenceReuse': copy.deepcopy(self.evidence_reuse)} if self.evidence_reuse else {}),
                'coverageStatus': 'ungraded', 'calls': [{'toolCallId': e['toolCallId'], 'operation': e['result']['operation'],
                    'status': e['result']['status'], 'executed': e['usage']['executed']} for e in journal]}

    def _document(self, args):
        candidates = []
        for field, lookup in [('sourceId', self.by_source), ('documentId', self.documents), ('chunkId', self.chunks)]:
            if field in args:
                value = args[field]
                if not isinstance(value, str) or value not in lookup:
                    raise ValueError('Source, document or chunk is outside this frozen App.')
                candidates.append(lookup[value] if field == 'sourceId' else value if field == 'documentId' else lookup[value]['documentId'])
        if not candidates or len(set(candidates)) != 1:
            raise ValueError('Source/document/chunk identities do not agree.')
        return candidates[0]

    def _metadata(self, doc_id):
        doc, source = self.documents[doc_id], self.snapshot['sources'][doc_id]
        return {'documentId': doc_id, 'sourceId': source['sourceId'], 'title': str(source.get('title') or doc.get('title') or '')[:1000],
                'chunkCount': len(self.by_document[doc_id]), 'pages': sorted({c['page'] for c in self.by_document[doc_id] if c.get('page') is not None}),
                'snapshotSha256': self.knowledge['snapshotSha256'], 'sourceFormat': 'extracted_text'}

    def _dispatch(self, operation, args, state, result, usage):
        fields = {'discover': {'query', 'offset', 'limit'}, 'search': {'query'},
                  'find': {'sourceId', 'documentId', 'patterns', 'offset', 'limit'},
                  'open': {'sourceId', 'documentId', 'chunkId', 'page', 'offset', 'charOffset', 'before', 'after', 'limit', 'maxChars'}}
        if operation not in fields or set(args) - fields[operation] - {'op', 'operation', 'snapshotSha256'}:
            raise ValueError('Unknown research operation or arguments.')
        if 'snapshotSha256' in args and args['snapshotSha256'] != self.knowledge['snapshotSha256']:
            raise ValueError('Citation belongs to a different frozen App snapshot.')
        remaining = self.total - state['readChars'] - state['discoveryChars'] - state['reusedChars']
        reading = operation in {'find', 'open'}
        available = min(remaining, self.config['maxReadChars'] if reading else self.config['maxSearchChars'])
        if not reading:
            available = min(available, self.total - self.config['readReserveChars'] - state['discoveryChars'])
        if available <= 0:
            result.update(status='budget_exhausted', message='This operation has no remaining evidence budget; preserve reading reserve.')
            return
        if operation in {'discover', 'search'}:
            query = args.get('query')
            if not isinstance(query, str) or not query.strip() or len(query) > 2000:
                raise ValueError('Research query must contain 1–2000 characters.')
            if operation == 'discover':
                tokens = query.casefold().split()
                docs = [key for key, doc in self.documents.items() if all(token in
                        (str(doc.get('title', '')) + ' ' + str(self.snapshot['sources'][key].get('title', ''))).casefold() for token in tokens)]
                offset, limit = _integer(args.get('offset', 0), 0, 20000, 'offset'), _integer(args.get('limit', 8), 1, 20, 'limit')
                result['documents'] = []
                for doc_id in docs[offset:offset + limit]:
                    row = self._metadata(doc_id)
                    charge = 0 if doc_id in state['metadata'] else len(row['title'])
                    if charge > available:
                        break
                    available -= charge
                    usage['discoveryChars'] += charge
                    usage['metadataIds'].append(doc_id)
                    result['documents'].append(row)
                next_offset = offset + len(result['documents'])
                result.update(totalDocuments=len(docs), nextCursor={'op': 'discover', 'query': query, 'offset': next_offset, 'limit': limit} if next_offset < len(docs) else None)
                usage['executed'] = True
                return
            packet = self.search(self.root, copy.deepcopy(self.knowledge), {self.knowledge['queryField']: query})
            if packet.get('snapshotSha256', self.knowledge['snapshotSha256']) != self.knowledge['snapshotSha256']:
                raise ValueError('Search returned a different frozen snapshot.')
            ranges = []
            for hit in packet.get('sources', []):
                chunk = self.chunks.get(hit.get('chunkId'))
                if not chunk or ('sourceId' in hit and hit['sourceId'] != self.snapshot['sources'][chunk['documentId']]['sourceId']):
                    raise ValueError('Search hit does not belong to this frozen source mapping.')
                ranges.append((chunk, 0, min(len(chunk['content']), 240)))
            result['retrieval'] = copy.deepcopy(packet.get('retrieval', {}))
            result['retrievalProfile'] = copy.deepcopy(self.knowledge['profile'])
            result['retrievalHitCount'] = len(ranges)
            self._pack(ranges, available, state, result, usage, reading=False)
        elif operation == 'find':
            doc_id = self._document(args)
            patterns = args.get('patterns')
            if not isinstance(patterns, list) or not 1 <= len(patterns) <= 10 or any(not isinstance(p, str) or not p or len(p) > 240 for p in patterns):
                raise ValueError('find requires 1–10 literal patterns, each at most 240 characters.')
            matches = []
            for chunk in self.by_document[doc_id]:
                # Literal, case-insensitive matching; no unbounded user regex.
                positions = [((match.start() if (match := re.search(re.escape(p), chunk['content'], re.IGNORECASE)) else -1), p) for p in patterns]
                found = [(pos, p) for pos, p in positions if pos >= 0]
                if found:
                    matches.append((chunk, min(pos for pos, _ in found), [p for _, p in found]))
            offset, limit = _integer(args.get('offset', 0), 0, 200000, 'offset'), _integer(args.get('limit', 6), 1, 20, 'limit')
            chosen = matches[offset:offset + limit]
            result['matches'] = [{'chunkId': c['id'], 'sourceId': self.snapshot['sources'][doc_id]['sourceId'], 'documentId': doc_id,
                                  'ordinal': c['ordinal'], 'page': c.get('page'), 'charOffset': pos, 'patterns': ps} for c, pos, ps in chosen]
            result['nextCursor'] = {'op': 'find', 'sourceId': self.snapshot['sources'][doc_id]['sourceId'], 'patterns': patterns, 'offset': offset + len(chosen), 'limit': limit} if offset + len(chosen) < len(matches) else None
            self._pack([(c, max(0, pos - 60), min(len(c['content']), pos + 180)) for c, pos, _ in chosen], available, state, result, usage, reading=True)
        else:
            doc_id = self._document(args)
            rows = self.by_document[doc_id]
            offset = _integer(args.get('offset', 0), 0, 200000, 'offset')
            limit = _integer(args.get('limit', 4), 1, 24, 'limit')
            if 'chunkId' in args:
                anchor = self.chunks[args['chunkId']]['ordinal']
                before = _integer(args.get('before', 1), 0, 10, 'before')
                after = _integer(args.get('after', 1), 0, 10, 'after')
                offset = max(0, anchor - before) if 'offset' not in args else offset
                limit = before + after + 1
            if 'page' in args:
                page = _integer(args['page'], 1, 1000000, 'page')
                rows = [c for c in rows if c.get('page') == page]
            rows = [c for c in rows if c['ordinal'] >= offset][:limit]
            start = _integer(args.get('charOffset', 0), 0, 10000000, 'charOffset')
            if rows and start > len(rows[0]['content']):
                raise ValueError('Reading position exceeds this chunk.')
            available = min(available, _integer(args.get('maxChars', self.config['maxReadChars']), 1, self.config['maxReadChars'], 'maxChars'))
            self._pack([(c, start if i == 0 else 0, len(c['content'])) for i, c in enumerate(rows)], available, state, result, usage, reading=True)
            result['document'] = self._metadata(doc_id)
            if result.get('nextCursor') is None and rows:
                following = rows[-1]['ordinal'] + 1
                if any(c['ordinal'] >= following and ('page' not in args or c.get('page') == args['page']) for c in self.by_document[doc_id]):
                    result['nextCursor'] = {'op': 'open', 'sourceId': self.snapshot['sources'][doc_id]['sourceId'], 'offset': following}
            if result.get('nextCursor') and 'page' in args:
                result['nextCursor']['page'] = args['page']
        usage['executed'] = True

    def _pack(self, ranges, available, state, result, usage, *, reading):
        seen = copy.deepcopy(state['seen'])
        citations = dict(state['citations'])
        existing = set()
        result['nextCursor'] = result.get('nextCursor')
        for chunk, start, end in ranges:
            key = chunk['id']
            intervals = [(start, end)]
            for old_start, old_end in sorted(seen.get(key, [])):
                revised = []
                for a, b in intervals:
                    if old_end <= a or old_start >= b:
                        revised.append((a, b))
                    else:
                        existing.add(citations[key])
                        if a < old_start:
                            revised.append((a, old_start))
                        if old_end < b:
                            revised.append((old_end, b))
                intervals = revised
            for a, b in intervals:
                if b <= a:
                    continue
                source = self.snapshot['sources'][chunk['documentId']]
                stop = min(b, a + available)
                if stop > a:
                    number = citations.setdefault(key, max(citations.values(), default=0) + 1)
                    citation = knowledge_runtime._citation(self.knowledge, source, key, chunk.get('page'), chunk.get('heading') or '', chunk['ordinal'])
                    citation.update(charStart=a, charEnd=stop)
                    result['sources'].append({'sourceId': source['sourceId'], 'documentId': chunk['documentId'], 'chunkId': key,
                        'title': str(source.get('title') or self.documents[chunk['documentId']].get('title') or ''),
                        **({'uri': source['uri']} if isinstance(source.get('uri'), str) else {}),
                        'text': chunk['content'][a:stop], 'contentSha256': chunk['contentHash'],
                        'citationNumber': number, 'citation': citation, 'sourceFormat': 'extracted_text'})
                    seen.setdefault(key, []).append((a, stop))
                    available -= stop - a
                    usage['readChars' if reading else 'discoveryChars'] += stop - a
                if stop < b:
                    result.update(status='partial', nextCursor={'op': 'open', 'sourceId': source['sourceId'], 'offset': chunk['ordinal'], 'charOffset': stop})
                    result['existingCitations'] = sorted(existing)
                    return
        result['existingCitations'] = sorted(existing)
        if not result['sources'] and existing:
            result['status'] = 'already_read'
        elif not ranges:
            result['status'] = 'not_found'
