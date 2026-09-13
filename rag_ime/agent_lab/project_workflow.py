"""Bounded, read-only workflow projection of existing Lab owner snapshots.

This module neither schedules work nor interprets Agent-authored progress as a
run receipt. Public nodes use an allowlist; cases, prompts, labels and source
contents never cross this projection into the project Guide.
"""
from __future__ import annotations

import math
import time
from collections.abc import Mapping
from typing import Any

MAX_NODES = 1000
_STATES = {'pending', 'queued', 'running', 'completed', 'failed', 'cancelled', 'interrupted', 'unavailable'}
_LABELS = {'pending': '尚未开始', 'queued': '等待执行', 'running': '正在执行', 'completed': '执行完成',
           'failed': '执行失败', 'cancelled': '已停止', 'interrupted': '等待恢复原任务', 'unavailable': '记录暂时无法核对'}
_KNOWLEDGE_KINDS = {'import_corpus': 'corpus', 'connect_base': 'corpus', 'index': 'index',
                    'restore_index': 'index', 'import_dataset': 'dataset', 'evaluate': 'experiment', 'search': 'job'}
_TITLES = {'materials': '项目材料', 'corpus': '导入语料', 'index': '建立索引', 'dataset': '准备评测集',
           'calibration': '校准评审', 'experiment': '对照实验', 'job': '执行任务'}


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in value if isinstance(row, Mapping)] if isinstance(value, list) else []


def _id(value: Any) -> str:
    return value if isinstance(value, str) and 0 < len(value) <= 1000 else ''


def _number(value: Any) -> int | float | None:
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _dependencies(*records: Mapping[str, Any]) -> list[str]:
    return list(dict.fromkeys(identifier for record in records
        for key in ('corpusId', 'indexId', 'datasetId', 'materialSetId', 'artifactId')
        if (identifier := _id(record.get(key)))))


def _job_summary(job: Mapping[str, Any]) -> str:
    # These are execution-owner progress fields, never the project's authored
    # progress artifact. Terminal state still comes only from the owner state.
    progress = job.get('progress')
    return progress[:500] if job.get('state') in {'queued', 'running'} and isinstance(progress, str) else ''


def _metrics(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    metrics = []
    for key, label in (('development', '开发集'), ('holdout', '验证集')):
        phase = _mapping(result.get(key))
        if not phase:
            continue
        baseline = _mapping(phase.get('baselineMetrics'))
        candidate = _mapping(phase.get('candidateMetrics'))
        metrics.append({'label': label + '通过率', 'baseline': _number(baseline.get('passRate')),
                        'candidate': _number(candidate.get('passRate')), 'unit': 'ratio'})
        cost = _mapping(phase.get('businessCost'))
        if cost.get('basis') in {'actual', 'model_catalog_estimate'}:
            metrics.append({'label': label + ('费用' if cost['basis'] == 'actual' else '费用估算'),
                            'baseline': _number(cost.get('baselineUsd')),
                            'candidate': _number(cost.get('candidateUsd')), 'unit': 'USD'})
    return metrics


def _model_label(value: Any) -> str:
    model = _mapping(value)
    return ' / '.join(str(model[key])[:120] for key in ('provider', 'model', 'thinkingLevel')
                      if isinstance(model.get(key), str) and model[key])


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 2**53 - 1 else None


def _retrieval_result(node: dict[str, Any], result: Mapping[str, Any]) -> None:
    """One retrieval configuration is a single observation, never an A/B pair."""
    if node['status'] != 'completed' or result.get('partial'):
        return
    report = _mapping(result.get('report'))
    aggregate = _mapping(report.get('metrics'))
    values = _mapping(aggregate.get('metrics'))
    count = _count(aggregate.get('queryCount'))
    metrics = []

    def metric(label: str, value: Any) -> None:
        number = _number(value)
        if number is None or not 0 <= number <= 1:
            return
        row = {'label': label, 'value': number, 'baseline': None, 'candidate': None, 'unit': 'ratio'}
        if count is not None:
            row['sampleCount'] = count
        metrics.append(row)

    metric('MRR', values.get('mrr'))
    for key, label in (('recallAtK', 'Recall'), ('ndcgAtK', 'nDCG')):
        # Keep only bounded numeric cutoffs, never arbitrary result keys.
        cutoffs = [(int(k), value) for k, value in _mapping(values.get(key)).items()
                   if isinstance(k, str) and k.isascii() and k.isdecimal() and len(k) <= 5 and int(k) > 0]
        for cutoff, value in sorted(cutoffs, key=lambda item: item[0])[:16]:
            metric(f'{label}@{cutoff}', value)
    if metrics:
        node['metrics'] = metrics
    profile = _mapping(result.get('profile')) or _mapping(report.get('config'))
    factors = []
    for key in ('mode', 'topK', 'candidateDepth', 'contextChars', 'rerank', 'threshold'):
        value = profile.get(key)
        if ((key == 'mode' and value in ('lexical', 'dense', 'hybrid')) or
                (key == 'rerank' and type(value) is bool) or
                (key not in {'mode', 'rerank'} and _number(value) is not None)):
            factors.append({'name': key, 'before': '', 'after': str(value).lower(), 'reason': '本次检索配置'})
    if factors:
        node['factors'] = factors
    planned = _count(result.get('plannedCount'))
    denominator = f'{count} / {planned}' if count is not None and planned is not None else str(count) if count is not None else ''
    node['summary'] = (f'已完成 {denominator} 题检索评测。' if denominator else '已保存本次检索评测结果。') + '仅衡量来源检索，未构成配置对照或回答质量结论。'


def project_workflow(project: Mapping[str, Any], *, knowledge: Any = None,
                     executions: Mapping[str, Any] | None = None,
                     app_calls: Any = None,
                     history_artifact: Any = None,
                     progress_artifact: Any = None,
                     unavailable_owners: list[str] | None = None,
                     observed_at_ms: int | None = None) -> dict[str, Any]:
    """Project real version/job identities without running or rereading owners.

    Counts include unique top-level runtime jobs. Nested model-call receipts
    remain visible but do not count a second time as parallel Lab jobs.
    """
    nodes: dict[str, dict[str, Any]] = {}
    unavailable = list(unavailable_owners or [])
    executions = executions or {}
    app_calls = _mapping(app_calls)

    def limited(value: Any) -> list[Mapping[str, Any]]:
        rows = _rows(value)
        if len(rows) > MAX_NODES:
            unavailable.append('workflow:limit')
        return rows[:MAX_NODES]

    def add(identifier: Any, kind: str, state: Any, *, ref_kind: str,
            title: str = '', summary: str = '', dependencies: list[str] | None = None,
            source: str = 'runtime', version: Any = None, updated: Any = None,
            parent: str = '') -> dict[str, Any] | None:
        identifier = _id(identifier)
        if not identifier:
            return None
        if identifier in nodes:
            return nodes[identifier]
        if len(nodes) >= MAX_NODES:
            unavailable.append('workflow:limit')
            return None
        status = state if isinstance(state, str) and state in _STATES else 'unavailable'
        node = {'id': identifier, 'kind': kind, 'title': title[:240] or _TITLES.get(kind, '项目记录'),
                'status': status, 'summary': summary[:500] or _LABELS[status],
                'dependencies': list(dict.fromkeys(d for d in (dependencies or []) if d != identifier)),
                'ref': {'kind': ref_kind, 'id': identifier}, 'source': source}
        if status == 'unavailable':
            unavailable.append(identifier)
        if type(version) is int and version > 0:
            node['ref']['version'] = version
        if _number(updated) is not None:
            node['updatedAtMs'] = updated
        if parent:
            node['parentId'] = parent
        nodes[identifier] = node
        return node

    material_set = _mapping(project.get('materialSet'))
    material_id = _id(project.get('materialSetId'))
    material_count = len(_rows(material_set.get('materials')))
    if material_id:
        add(material_id, 'materials', 'completed' if material_count else 'pending', ref_kind='material_set',
            source='artifact', version=material_set.get('version'),
            summary=f'已保存 {material_count} 项材料；材料保存不代表实验已完成。')
    for version in limited(project.get('materialVersions')):
        add(version.get('materialSetId'), 'materials', 'completed', ref_kind='material_set', source='artifact',
            version=version.get('version'), updated=version.get('createdAtMs'), summary='已保存的材料版本。')

    knowledge = _mapping(knowledge)
    if knowledge.get('unavailable') or knowledge.get('status') == 'unavailable':
        unavailable.append('knowledge')
    # Jobs precede resources: a conflicting completed resource cannot overwrite
    # the execution owner's cancelled, interrupted or failed terminal state.
    for job in limited(knowledge.get('jobs')):
        spec, result = _mapping(job.get('publicSpec')), _mapping(job.get('result'))
        kind = _KNOWLEDGE_KINDS.get(spec.get('operation'), 'job')
        node = add(job.get('jobId'), kind, job.get('state'), ref_kind='knowledge_job',
            dependencies=_dependencies(spec, result), updated=job.get('updatedAtMs'),
            summary=_job_summary(job),
            title={'evaluate': '检索评测', 'restore_index': '恢复已有索引'}.get(spec.get('operation'), _TITLES[kind]))
        if node and spec.get('operation') == 'evaluate':
            _retrieval_result(node, result)
    for collection, kind in (('corpora', 'corpus'), ('indexes', 'index'), ('datasets', 'dataset'), ('evaluations', 'experiment')):
        for resource in limited(knowledge.get(collection)):
            identifier = resource.get('datasetId') if collection == 'datasets' else resource.get('jobId')
            number = _number(resource.get('documentCount' if kind in {'corpus', 'index'} else 'caseCount'))
            summary = f'已保存 {number:g} ' + ('篇正文' if kind in {'corpus', 'index'} else '条评测题') if number is not None else '已保存的执行结果，可复用原任务记录。'
            node = add(identifier, kind, 'completed', ref_kind='knowledge_job', dependencies=_dependencies(resource), summary=summary)
            if node and node['status'] == 'completed':
                node['summary'] = summary
                if collection == 'evaluations':
                    node['title'] = '检索评测'
                    _retrieval_result(node, resource)

    for binding in limited(project.get('bindings')):
        binding_id = _id(binding.get('bindingId'))
        owner = _mapping(binding.get('ownerRef'))
        execution = _mapping(executions.get(binding_id))
        suite = _mapping(execution.get('suite'))
        jobs_value = suite.get('jobs') if suite else execution.get('jobs')
        jobs = limited(jobs_value)
        deps = _dependencies(binding, _mapping(binding.get('input')))
        if not isinstance(jobs_value, list):
            unavailable.append(binding_id or 'execution')
            add(binding_id, 'experiment', 'unavailable', ref_kind=str(owner.get('kind') or 'binding'),
                dependencies=deps, summary='执行记录暂时无法读取；保留原绑定，等待刷新。')
        elif not jobs:
            node = add(binding_id, 'experiment', 'pending', ref_kind=str(owner.get('kind') or 'binding'),
                       dependencies=deps, summary='已建立执行绑定，尚无实际实验任务。')
            if node and _id(owner.get('id')):
                node['ref']['id'] = owner['id']
        for job in jobs:
            golden = owner.get('kind') == 'golden_suite'
            kind = {'draft': 'dataset', 'review': 'dataset', 'calibrate': 'calibration', 'experiment': 'experiment'}.get(job.get('kind'), 'job') if golden else 'experiment'
            result = _mapping(job.get('result'))
            node = add(job.get('jobId'), kind, job.get('state'), ref_kind='golden_job' if golden else 'trial_job',
                       dependencies=list(dict.fromkeys(deps + _dependencies(_mapping(result.get('knowledge'))))),
                       updated=job.get('updatedAtMs'), summary=_job_summary(job),
                       title='Agent 核对题集' if golden and job.get('kind') == 'review' else '')
            if not node:
                continue
            if golden and job.get('kind') == 'review' and node['status'] == 'completed':
                reviewed, approved = _count(result.get('reviewedCount')), _count(result.get('approvedCount'))
                node['summary'] = (f'Agent 已核对 {reviewed} 题' if reviewed is not None else 'Agent 核对任务已完成')
                if approved is not None:
                    node['summary'] += f'，其中 {approved} 题通过标准核对'
                node['summary'] += '；这是题集核对记录，尚不代表应用质量已提升。'
            if kind == 'experiment' and node['status'] == 'completed' and result.get('partial'):
                node['status'] = 'unavailable'
                node['summary'] = '任务终态与部分结果不一致，需要核对原回执。'
                unavailable.append(node['id'])
            scope = result.get('optimizationScope')
            if kind == 'experiment' and scope in {'model', 'prompt', 'model_and_prompt', 'repeat', 'skill',
                                                   'model_and_skill', 'prompt_and_skill', 'model_and_prompt_and_skill'}:
                baseline, candidate = _mapping(result.get('baseline')), _mapping(result.get('candidate'))
                node['optimization'] = {'scope': scope, 'baselineModel': _model_label(baseline),
                                        'candidateModel': _model_label(candidate)}
                if isinstance(baseline.get('prompt'), str) and isinstance(candidate.get('prompt'), str):
                    node['optimization']['promptChanged'] = baseline['prompt'] != candidate['prompt']
                selected = _mapping(result.get('optimization')).get('selectedCandidateIndex')
                if type(selected) is int and selected >= 0:
                    node['optimization']['selectedCandidateIndex'] = selected
            if _id(result.get('snapshotId')):
                node['evidenceRefs'] = [{'kind': 'golden_snapshot', 'id': result['snapshotId']}]
            if golden and _id(owner.get('id')):
                node.setdefault('evidenceRefs', []).append({'kind': 'golden_suite', 'id': owner['id']})
            method = _mapping(result.get('applicationMethodComparison'))
            if kind == 'experiment' and method.get('scope') == 'application_skill_body' and type(method.get('changed')) is bool:
                identities = {}
                for variant in ('baseline', 'candidate'):
                    identity = _mapping(method.get(variant))
                    identities[variant] = {key: identity[key] for key in ('kind', 'title', 'sha256') if key in identity} if identity else None
                    if identities[variant] is not None:
                        source = _mapping(identity.get('source'))
                        identities[variant]['source'] = {key: source[key] for key in ('kind', 'projectId', 'artifactId', 'artifactRevision') if key in source}
                node['applicationMethodComparison'] = {'scope': 'application_skill_body', 'changed': method['changed'],
                    **identities, 'diff': method.get('diff', '')[:128000] if isinstance(method.get('diff'), str) else ''}
            if _id(job.get('sessionId')):
                node.setdefault('evidenceRefs', []).append({'kind': 'runtime_session', 'id': job['sessionId']})
            if kind == 'experiment' and node['status'] == 'completed' and not result.get('partial'):
                decision = _mapping(result.get('comparison')).get('decision')
                if decision in {'improved', 'no_improvement', 'inconclusive', 'keep', 'reject', 'unknown', 'Keep', 'Reject'}:
                    node['decision'] = decision
                    node['summary'] = f'对照实验已完成，判定为 {decision}；可复用原任务结果。'
                metrics = _metrics(result)
                if metrics:
                    node['metrics'] = metrics
                reasons = _mapping(result.get('comparison')).get('reasons')
                if isinstance(reasons, list):
                    node['reasons'] = [reason[:500] for reason in reasons[:8] if isinstance(reason, str)]
            children = list(node.get('children', []))
            for record in limited(result.get('receipts')):
                receipt = _mapping(record.get('receipt'))
                status = receipt.get('status', receipt.get('state'))
                status = 'completed' if status == 'succeeded' else 'cancelled' if status == 'aborted' else status
                stage = {'answer': '回答调用', 'judge': '评审调用', 'optimize': '优化调用', 'draft': '起草调用', 'review': '题目核对调用', 'calibration': '校准调用'}.get(record.get('stage'), '模型调用')
                child = add(record.get('requestId'), 'job', status, ref_kind='runtime_request', title=stage,
                            parent=node['id'], dependencies=[node['id']], updated=receipt.get('settledAtMs'))
                if child:
                    child['evidenceRefs'] = [{'kind': kind, 'id': identifier} for key, kind in
                                            (('sessionId', 'runtime_session'), ('turnId', 'runtime_turn'))
                                            if (identifier := _id(record.get(key)))]
                    if golden and _id(owner.get('id')):
                        child['evidenceRefs'].append({'kind': 'golden_suite', 'id': owner['id']})
                if child and child.get('parentId') == node['id'] and child['id'] not in children:
                    children.append(child['id'])
            pending = _id(result.get('pendingRequestId'))
            if pending and pending not in nodes:
                child = add(pending, 'job', 'running' if node['status'] == 'running' else 'interrupted',
                            ref_kind='runtime_request', title='待确认的模型调用', parent=node['id'], dependencies=[node['id']])
                if child:
                    children.append(child['id'])
            if children:
                node['children'] = children

    history = _mapping(history_artifact)
    content = _mapping(history.get('content'))
    if content.get('schemaVersion') == 'paw.lab-imported-experiments.v1':
        records = limited(content.get('experiments'))
        previous: dict[str, list[str]] = {}
        for record in records:
            successor = _id(record.get('supersededBy'))
            identifier = _id(record.get('experimentId'))
            if successor and identifier:
                previous.setdefault(successor, []).append(identifier)
        for record in records:
            identifier = _id(record.get('experimentId'))
            node = add(identifier, 'experiment', 'completed', ref_kind='experiment_record', source='artifact',
                       title=str(record.get('title') or '已有实验'), dependencies=previous.get(identifier, []),
                       updated=record.get('importedAtMs'), summary='已保存的历史实验结果；没有因本次读取重新运行或改变原结论。')
            if not node:
                continue
            comparison = _mapping(record.get('comparison'))
            if isinstance(comparison.get('decision'), str):
                node['decision'] = comparison['decision'][:100]
            reason = comparison.get('decisionReason')
            boundary = _mapping(record.get('claim')).get('forbidden')
            node['reasons'] = [text[:500] for text in (reason, boundary) if isinstance(text, str) and text]
            node['factors'] = [{key: str(factor.get(key) or '')[:500] for key in ('name', 'before', 'after', 'reason')}
                               for factor in limited(record.get('factors'))[:16]]
            baseline, candidate = _mapping(record.get('baseline')), _mapping(record.get('candidate'))
            left, right = _mapping(baseline.get('metrics')), _mapping(candidate.get('metrics'))
            node['metrics'] = [{'label': str(key)[:120], 'baseline': _number(left.get(key)), 'candidate': _number(right.get(key))}
                               for key in sorted(set(left) | set(right))[:32]]
            refs = [{'kind': 'artifact', 'id': str(history.get('artifactId') or '')}]
            if type(history.get('revision')) is int:
                refs[0]['version'] = history['revision']
            for variant in (baseline, candidate):
                refs.extend({'kind': 'evidence', 'id': ref} for ref in variant.get('evidenceRefs', [])[:32]
                            if isinstance(ref, str) and 0 < len(ref) <= 1000)
            if _id(record.get('revisionSha256')):
                refs.append({'kind': 'experiment_revision', 'id': record['revisionSha256']})
            node['evidenceRefs'] = [dict(values) for values in dict.fromkeys(tuple(ref.items()) for ref in refs if ref['id'])]

    for artifact in limited(project.get('artifacts')):
        add(artifact.get('artifactId'), 'artifact', 'completed', ref_kind='artifact', source='artifact',
            title=str(artifact.get('title') or '项目成果'), version=artifact.get('revision'),
            updated=artifact.get('updatedAtMs'), summary='已保存的项目成果；内容描述不作为运行完成或质量通过的依据。')
    for app in limited(project.get('applications')):
        active, latest = app.get('activeVersion'), app.get('latestVersion')
        add(app.get('appId'), 'application', 'completed', ref_kind='application', source='artifact',
            title=str(app.get('title') or '应用版本'), version=latest, updated=app.get('updatedAtMs'),
            summary=f'已准备版本 {latest}；' + (f'当前启用版本 {active}。' if active else '尚未启用。') + '准备或启用不等于当前运行验收通过。')

    if app_calls.get('truncated'):
        unavailable.append('application_calls:limit')
    visible_calls = limited(app_calls.get('calls'))
    calls_by_id = {_id(call.get('callId')): call for call in visible_calls}
    for call in visible_calls:
        app_id = _id(call.get('appId'))
        if not app_id:
            continue
        progress = _mapping(call.get('progress'))
        stage = progress.get('stage')
        stage_label = {'researching': '正在按需取证', 'retrieving': '正在检索', 'sources_ready': '已取得来源',
                       'model_starting': '正在连接模型', 'model_wait': '等待模型', 'thinking': '模型处理中',
                       'answering': '正在回答'}.get(stage, '')
        summary = stage_label if call.get('state') in {'running', 'queued'} else ''
        tools, reads = _count(progress.get('executedToolCalls')), _count(progress.get('executedSourceReadCalls'))
        if tools is not None:
            summary += f'；已执行 {tools} 次知识工具'
        if reads is not None:
            summary += f'，其中 {reads} 次文内定位或读取'
        reuse = _mapping(call.get('evidenceReuse'))
        source_call_id = _id(reuse.get('sourceCallId'))
        source_call = calls_by_id.get(source_call_id)
        reused_windows = _count(reuse.get('windowCount'))
        source_version = _count(reuse.get('sourceAppVersion'))
        # The App owner projects only verified same-App completed sources.
        # Also reject a conflicting visible record rather than drawing a
        # misleading dependency. A bounded history may omit the older node.
        reuse_known = bool(source_call_id and source_call_id != call.get('callId') and reused_windows
                           and (source_call is None or (source_call.get('appId') == app_id
                                                        and source_call.get('state') == 'completed')))
        if reuse_known:
            summary += f'；复用前次 {reused_windows} 个原文窗口'
        if call.get('state') == 'completed':
            summary = '应用调用已完成' + summary + '；完成不代表研究质量通过。'
        title = str(call.get('title') or '应用') + (f" v{call['version']}" if _count(call.get('version')) else '')
        title += ' · ' + str(call.get('actionTitle') or call.get('actionId') or '调用')
        node = add(call.get('callId'), 'job', call.get('state'), ref_kind='application_call', title=title,
                   version=call.get('version'), updated=call.get('updatedAtMs'), summary=summary.lstrip('；'),
                   dependencies=[app_id] if app_id in nodes else [])
        if node:
            node['evidenceRefs'] = [{'kind': 'application', 'id': app_id, **({'version': call['version']} if type(call.get('version')) is int else {})}]
            if reuse_known:
                if source_call is not None:
                    node['dependencies'].append(source_call_id)
                    source_version = _count(source_call.get('version'))
                node['evidenceRefs'].append({'kind': 'application_call', 'id': source_call_id,
                                             **({'version': source_version} if source_version else {})})
            if _id(call.get('sessionId')):
                node['evidenceRefs'].append({'kind': 'runtime_session', 'id': call['sessionId']})
            node['progress'] = {key: value for key, value in progress.items() if
                (key == 'stage' and isinstance(value, str) and value in _STATES | {'context_ready', 'retrieving', 'researching', 'sources_ready', 'model_starting', 'model_wait', 'thinking', 'answering', 'unconfirmed'}) or
                (key == 'operation' and isinstance(value, str) and value in {'discover', 'find', 'open', 'search'}) or
                (key in {'executedToolCalls', 'executedSearchCalls', 'executedSourceReadCalls', 'contextChars'} and _count(value) is not None)}

    planned_node_ids: set[str] = set()
    progress = _mapping(progress_artifact)
    progress_content = _mapping(progress.get('content'))
    if progress_content.get('schemaVersion') == 'paw.lab-project-progress.v1':
        steps = limited(progress_content.get('steps'))
        aliases = {}
        for step in steps:
            step_id = _id(step.get('id'))
            if not step_id:
                continue
            references = step.get('evidenceRefs') if isinstance(step.get('evidenceRefs'), list) else []
            candidates = [_id(step.get('jobId')), *[_id(ref.get('id')) if isinstance(ref, Mapping) else _id(ref)
                                                   for ref in references[:32]]]
            matched = next((candidate for candidate in candidates if candidate in nodes and nodes[candidate]['source'] == 'runtime'
                            and nodes[candidate]['ref']['kind'] in {'knowledge_job', 'golden_job', 'trial_job', 'application_call'}), '')
            aliases[step_id] = matched or step_id
        for step in steps:
            step_id = _id(step.get('id'))
            if step_id not in aliases:
                continue
            identifier = aliases[step_id]
            dependencies = step.get('dependsOn') if isinstance(step.get('dependsOn'), list) else []
            dependencies = [aliases.get(dep, dep) for value in dependencies[:64] if (dep := _id(value))]
            title = str(step.get('title') or '项目步骤')[:240]
            if identifier != step_id:
                node = nodes[identifier]
                node['title'] = title
                node['dependencies'] = list(dict.fromkeys(node['dependencies'] + [dep for dep in dependencies if dep != identifier]))
            else:
                state = {'active': 'running', 'blocked': 'unavailable'}.get(step.get('state'), step.get('state'))
                node = add(identifier, 'step', state, ref_kind='progress_step', source='artifact', title=title,
                           dependencies=dependencies, updated=progress.get('updatedAtMs'),
                           summary='已保存的项目步骤：' + str(step.get('summary') or '')[:440])
            if not node:
                continue
            planned_node_ids.add(node['id'])
            owner_ref = {'kind': 'artifact', 'id': str(progress.get('artifactId') or '')}
            if type(progress.get('revision')) is int:
                owner_ref['version'] = progress['revision']
            refs = list(node.get('evidenceRefs', []))
            if owner_ref['id'] and owner_ref not in refs:
                refs.append(owner_ref)
            for ref in step.get('evidenceRefs', [])[:32] if isinstance(step.get('evidenceRefs'), list) else []:
                if isinstance(ref, Mapping) and _id(ref.get('id')) and _id(ref.get('kind')):
                    item = {'kind': ref['kind'], 'id': ref['id']}
                    if type(ref.get('version')) is int:
                        item['version'] = ref['version']
                    if item not in refs:
                        refs.append(item)
                elif _id(ref):
                    refs.append({'kind': 'evidence', 'id': ref})
            node['evidenceRefs'] = refs

    for node in list(nodes.values()):
        for dependency in node['dependencies']:
            if dependency not in nodes:
                unavailable.append('dependency:' + dependency)
                add(dependency, 'job', 'unavailable', ref_kind='knowledge_job',
                    summary='原依赖记录未出现在本次读取中；不能视为完成。')
    edges: list[dict[str, str]] = []
    adjacency: dict[str, list[str]] = {}
    for node in nodes.values():
        accepted = []
        for dependency in node['dependencies']:
            if dependency not in nodes:
                continue
            seen, remaining = set(), [node['id']]
            while remaining:
                current = remaining.pop()
                if current not in seen:
                    seen.add(current)
                    remaining.extend(adjacency.get(current, []))
            if dependency in seen:
                unavailable.append('workflow:cycle')
                continue
            adjacency.setdefault(dependency, []).append(node['id'])
            edges.append({'source': dependency, 'target': node['id']})
            accepted.append(dependency)
        node['dependencies'] = accepted
    countable = [node for node in nodes.values() if node['source'] == 'runtime' and not node.get('parentId')
                 and node['ref']['kind'] in {'knowledge_job', 'golden_job', 'trial_job', 'application_call'}]
    counts = {state: sum(node['status'] == state for node in countable) for state in ('running', 'queued', 'completed', 'failed')}
    application_counts = {state: _count(_mapping(app_calls.get('counts')).get(state)) or 0
                          for state in ('running','queued','completed','failed','cancelled','interrupted')}
    if isinstance(app_calls.get('counts'), Mapping):
        # The App owner counts all calls even when old displayed history is bounded.
        for state in counts:
            counts[state] = sum(node['status'] == state and node['ref']['kind'] != 'application_call' for node in countable) + application_counts[state]
    application_counts['active'] = application_counts['running'] + application_counts['queued']
    application_counts['terminal'] = sum(application_counts[state] for state in ('completed','failed','cancelled'))
    priorities = {'running': 0, 'queued': 1, 'interrupted': 2, 'failed': 3, 'unavailable': 4, 'pending': 5, 'completed': 6, 'cancelled': 7}
    focus_nodes = [node for node in nodes.values() if not node.get('parentId')]
    planned_nodes = [node for node in focus_nodes if node['id'] in planned_node_ids]
    if planned_nodes:
        # A saved plan can select the current work, but cannot hide live owner
        # jobs or rewrite historical failures as successful/superseded runs.
        active_jobs = [node for node in countable if node['status'] in {'running', 'queued'}]
        focus_nodes = active_jobs or planned_nodes
    current_nodes = sorted(focus_nodes,
                           key=lambda node: (priorities[node['status']], node['source'] != 'runtime', -(node.get('updatedAtMs') or 0), node['id']))
    return {'schemaVersion': 'paw.lab-project-workflow.v1',
            'observedAtMs': observed_at_ms if observed_at_ms is not None else int(time.time() * 1000),
            'nodes': list(nodes.values()), 'edges': edges, 'counts': counts,
            'applicationCalls': {'counts': application_counts, 'totalCount': _count(app_calls.get('totalCount')) or 0,
                                 'truncated': bool(app_calls.get('truncated'))},
            'currentNodeId': current_nodes[0]['id'] if current_nodes else None,
            'complete': not unavailable, 'unavailableOwners': list(dict.fromkeys(unavailable))}
