"""Explicit live acceptance adapters over the existing Lab trial owner.

No model, credential discovery or runtime startup occurs on import. The CLI is
responsible for installing and proving the provider guard before run_case.
"""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path

from . import acceptance_fixtures as fixtures
from .acceptance_fixtures import (
    INVOICE_CASE, INVOICE_FILES, INVOICE_PROMPT, INVOICE_SOURCE, INVOICE_FIXTURE_SHA256,
    invoice_materialize, invoice_inspect_files, formatted_json,
    MEMORY_CASES, MEMORY_FIXTURE, MEMORY_FIXTURE_SHA256, MEMORY_QUERY,
    memory_prepare_storage, memory_projection_checks,
    SCOPE_CASE, SCOPE_FIXTURE_SHA256, SCOPE_PROMPT, scope_materialize, scope_create_task,
    scope_gateway_proof, scope_hashes, scope_live_checks,
)
SUPPORTED = {"classic-handoff", "discussion-no-write", "ptc-parallel-barrier-v2", "ptc-error"}

def digest(value):
    return fixtures.canonical_digest(value)


def tree_hashes(root):
    return {str(path.relative_to(root)): 'SYMLINK' if path.is_symlink() else hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(root.rglob('*')) if path.is_symlink() or path.is_file()}

def account_terminal_usage(settled, sid, tid, client, session_file, guard, usage_reader):
    """Bill a proven terminal turn even when its task result failed.

    Never release reservations from partial/foreign settlement or from a mere
    timeout. The budget owner still reconciles transcript calls to HTTP claims.
    """
    receipt = settled.get('receipt') or {}
    if settled.get('schemaVersion') != 'rag-ime.pi-turn-settlement.v1' or settled.get('sessionId') != sid or settled.get('turnId') != tid or (settled.get('clientMessageId') != client) or (receipt.get('schemaVersion') != 'pi.agent-settled.v2') or (not settled.get('runtimeSessionId')) or (receipt.get('sessionId') != settled.get('runtimeSessionId')) or (receipt.get('disposition') not in {'completed', 'failed', 'cancelled', 'aborted'}) or (receipt.get('pendingOperations') != 0):
        raise ValueError('usage settlement identity/terminal state uncertain; retain reservation')
    usage = usage_reader(session_file, tid)
    guard.account_exact_turn(sid, tid, usage)
    return usage

def oracle(task_id, facts, before, after, output_bytes, final_text):
    checks = {'completeTrace': facts.get('complete') is True, 'exactIdentity': facts.get('exactIdentity') is True}
    if task_id == 'classic-handoff':
        checks.update(authorization=facts.get('authorizationBeforeMutation') is True, actualToolChain=facts.get('readWriteRead') is True, oneEffect=facts.get('outputContentMutations') == 1, exactBytes=output_bytes == b'HELLO PAW\n', noCodemode=facts.get('codemodeCalls') == 0, goalCompleted=facts.get('goalCompletionReceipt') is True, unchangedInputs=before == {k: v for k, v in after.items() if k != 'result.txt'}, onlyExpectedFile=set(after) - set(before) == {'result.txt'})
    elif task_id == 'discussion-no-write':
        checks.update(unchanged=before == after, noMutations=facts.get('mutatingAdmissions') == 0, noTasks=facts.get('taskDispatches') == 0, noProcesses=facts.get('processStarts') == 0)
    elif task_id == 'identity-compact-reopen':
        checks.update(sameIdentity=facts.get('samePersistedSession') is True, newTurn=facts.get('distinctContinuationTurn') is True, noReplay=facts.get('originalPromptAdmissions') == 1, compacted=facts.get('actualCompaction') is True, answer=final_text.strip() == 'PAW-CONTINUITY-7319:done')
    elif task_id in {'ptc-parallel', 'ptc-parallel-barrier-v2'}:
        starts, ends = (facts.get('nestedStarts', []), facts.get('nestedEnds', []))
        checks.update(oneOuter=facts.get('codemodeCalls') == 1, calls=facts.get('nestedReadCalls') == 3, overlap=len(starts) == len(ends) == 3 and max(starts) < min(ends), values=facts.get('orderedValues') == [3, 5, 8], sum=facts.get('finalStructuredSum') == 16, unchanged=before == after)
        if task_id == 'ptc-parallel-barrier-v2':
            checks['barrierReached'] = facts.get('barrierArrivals') == [0, 1, 2] and facts.get('barrierReleases') == 3
    elif task_id == 'ptc-error':
        checks.update(oneEach=facts.get('nestedSuccessCalls') == facts.get('nestedErrorCalls') == 1, success=facts.get('successValue') == 7, error=facts.get('errorCode') == 'FIXTURE_E_TRANSIENT', noRetry=facts.get('retryCalls') == 0, truthful=facts.get('finalDistinguishesFailure') is True, unchanged=before == after)
    else:
        raise ValueError('adapter_unimplemented')
    return checks

class AcceptanceAdapter:

    def __init__(self, service, root, plan, budget, observe):
        self.service, self.root, self.plan = (service, Path(root), plan)
        self.budget, self.observe = (budget, observe)

    def prepare(self, spec, job_id):
        if set(spec) != {'taskId'} or spec['taskId'] not in SUPPORTED:
            raise ValueError('adapter_unimplemented')
        task = next((t for t in self.plan['cases'] if t['id'] == spec['taskId']))
        if digest(task['fixture']) != task['fixtureSha256']:
            raise ValueError('fixture_drift')
        return {'publicSpec': {'taskId': task['id'], 'fixtureSha256': task['fixtureSha256'], 'model': self.plan['model']}, 'privateInput': {'task': task, 'jobId': job_id}}

    def execute(self, private, observer, cancelled):
        from rag_ime.agent_session_application import WORKSPACE_SCOPE_CONFIRMATION
        from rag_ime.agent_lab.golden_pi import _settled_output
        from rag_ime.agent_lab.micro import turn_usage
        task, job = (private['task'], private['jobId'])
        cid = task['id']
        if not callable(getattr(self.budget, 'guard', None)) or not callable(self.observe):
            raise ValueError('budget_unknown_or_oracle_missing')
        work = self.root / job / 'workspace'
        work.mkdir(parents=True, exist_ok=False)
        if cid == 'classic-handoff':
            (work / 'input.txt').write_text(task['fixture']['input'])
        elif cid == 'discussion-no-write':
            (work / 'protected.txt').write_text(task['fixture']['existing'])
        service = self.service
        source = service.ensure_primary_assistant({'workspaceRoots': [str(work)]})['session']
        authorization = None
        if cid == 'classic-handoff':
            created = service.create_primary_task({'clientRequestId': job + ':authorize', 'sourceSessionId': source['id'], 'objective': task['input'], 'acceptanceCriteria': ['Write result.txt as uppercase input and verify by real read'], 'workspaceRoots': [str(work)], 'workspaceScopeConfirmation': WORKSPACE_SCOPE_CONFIRMATION})
            session, authorization = (created['session'], created['authorization'])
        else:
            session = source
        sid = session['id']
        prompt = task['input']
        if callable(getattr(self.observe, 'configure', None)):
            prompt = self.observe.configure(task, sid, work, authorization=authorization)
        before = tree_hashes(work)
        if session.get('runtimeEngine', 'classic') != 'classic':
            raise ValueError('capability_unsupported; no engine substitution')
        observer.bind_session(sid, cancel=lambda: service.abort(sid))
        selected = service.runtime.set_model(sid, provider='openai-codex', model_id='gpt-6.1-sol', max_tokens=1024)
        if selected.get('selected', {}).get('id') != 'gpt-6.1-sol' or selected.get('selected', {}).get('provider') != 'openai-codex':
            raise ValueError('model_selection_mismatch')
        effort = service.runtime.set_thinking_level(sid, level='xhigh')
        if effort.get('thinkingLevel') != 'xhigh':
            raise ValueError('thinking_selection_mismatch')
        mode = task.get('codemodeMode', 'off')
        result = service.select_codemode_mode(sid, {'mode': mode})
        if result.get('codemodeMode') != mode:
            raise ValueError('capability_unsupported')
        receipts, admissions, usages = ([], [], [])
        common = {'maxProviderCalls': 12, 'maxToolCalls': 32, 'timeoutSeconds': 180}
        cap = task.get('maxProviderCalls', common['maxProviderCalls'])
        cap = getattr(self.budget, 'max_provider_calls', cap)
        if type(cap) is not int or not 1 <= cap <= 12:
            raise ValueError('invalid bounded provider-call cap')
        tool_cap = getattr(self.budget, 'max_tool_calls', common['maxToolCalls'])
        if type(tool_cap) is not int or not 1 <= tool_cap <= 64:
            raise ValueError('invalid bounded tool-call cap')
        reservation = getattr(self.budget, 'case_limit_usd', None)
        if type(reservation) not in (int, float) or not math.isfinite(reservation) or (not 0 < reservation <= 100):
            raise ValueError('budget_unknown; full per-request reservation required')
        with self.budget.guard(job_id=job, session_id=sid, case_limit_usd=reservation, max_provider_calls=cap, max_tool_calls=tool_cap, max_output_tokens=1024, timeout_seconds=common['timeoutSeconds']) as guard:

            def complete(prompt, suffix):
                if cancelled():
                    raise InterruptedError('cancelled before prompt')
                client = job + ':' + suffix
                admitted = service.prompt(sid, {'message': prompt, 'clientMessageId': client})
                admissions.append(admitted)
                tid = admitted.get('turnId')
                if not tid:
                    raise RuntimeError('receipt_uncertain; no retry')
                observer.bind_session(sid, tid, cancel=lambda: service.abort(sid))
                settled = service.runtime.await_turn_settled(sid, tid, client_message_id=client, timeout_seconds=180)
                usage = account_terminal_usage(settled, sid, tid, client, service.sessions.get(sid)['sessionFile'], guard, turn_usage)
                usages.append(usage)
                (self.root / job / (suffix + '-usage.json')).write_text(json.dumps({'sessionId': sid, 'turnId': tid, 'clientMessageId': client, 'disposition': settled['receipt']['disposition'], **usage}, ensure_ascii=False))
                text, _, receipt_id = _settled_output(settled, sid, tid, client)
                receipts.append({'sessionId': sid, 'turnId': tid, 'clientMessageId': client, 'settlementReceiptId': receipt_id})
                return text
            final = complete(prompt, 'initial')
            compact_receipt = None
            if cid == 'identity-compact-reopen':
                compact_receipt = service.compact(sid, {})
                guard.account_compaction(sid, compact_receipt)
                service.runtime.close_session(sid)
                final = complete('只返回之前任务标记加上 :done，不调用工具。', 'continuation')
            cost = guard.finish()
        if not isinstance(cost, dict) or not math.isfinite(cost.get('estimatedUsd', float('nan'))):
            raise ValueError('budget_unknown')
        facts = self.observe(task=task, session_id=sid, admissions=admissions, receipts=receipts, authorization=authorization, compaction=compact_receipt, workspace=work, final_text=final)
        output = (work / 'result.txt').read_bytes() if (work / 'result.txt').is_file() and (not (work / 'result.txt').is_symlink()) else b''
        checks = oracle(cid, facts, before, tree_hashes(work), output, final)
        return {'status': 'completed', 'qualityVerdict': 'keep' if all(checks.values()) else 'reject', 'evidenceLevel': 'live_runtime', 'nativeAcceptance': False, 'caseId': cid, 'fixtureSha256': task['fixtureSha256'], 'checks': checks, 'receipts': receipts, 'usage': usages, 'cost': cost, 'facts': facts}

def invoice_trace_checks(trace, service, sid, tid, client, work):
    evidence = service.runtime.session_tool_evidence(sid, turn_id=tid, client_message_id=client)
    starts = [e['payload'] for e in evidence['toolHistoryEvents'] if e['eventType'] == 'tool_started']
    rows = [r for r in trace.rows if r['sessionId'] == sid and r['binding'].get('turnId') == tid]
    read_files = set()
    written = set()
    input_write_attempts = []
    for r in rows:
        name = Path(str(r['args'].get('path', ''))).name
        if r['ok'] and r['toolName'] in {'read', 'workspace_read'}:
            read_files.add(name)
        if r['toolName'] in {'write', 'edit', 'workspace_write', 'workspace_edit', 'workspace_patch'}:
            written.add(name)
            if name in INVOICE_FILES:
                input_write_attempts.append(r['toolCallId'])
    goal = service.sessions.agent_goal(sid)
    checks = {'allFiveActuallyRead': set(INVOICE_FILES) <= read_files, 'bothOutputsActuallyWritten': {'reconciliation.csv', 'summary.json'} <= written, 'inputNeverWriteTarget': not input_write_attempts, 'noShellOrCodeExecution': all((p.get('toolName') not in {'bash', 'workspace_shell', 'workspace_job', 'codemode', 'python'} for p in starts)), 'goalCompleted': goal.get('status') == 'completed' and bool((goal.get('completionAudit') or {}).get('auditId')), 'exactToolIdentity': all((e.get('sessionId') == sid and e.get('turnId') == tid for e in evidence['toolHistoryEvents']))}
    return (checks, {'gatewayCalls': rows, 'toolHistoryEvents': evidence['toolHistoryEvents'], 'goalId': goal.get('goalId'), 'goalStatus': goal.get('status')})

class InvoiceAdapter:

    def __init__(self, service, root, budget, trace):
        self.service, self.root, self.budget, self.trace = (service, Path(root), budget, trace)

    def prepare(self, spec, job_id):
        if spec != {'taskId': INVOICE_CASE}:
            raise ValueError('unsupported invoice task')
        return {'publicSpec': {'taskId': INVOICE_CASE, 'fixtureSha256': INVOICE_FIXTURE_SHA256, 'source': INVOICE_SOURCE}, 'privateInput': {'jobId': job_id}}

    def execute(self, private, observer, cancelled):
        try:
            return self._execute(private, observer, cancelled)
        except BaseException as error:
            import traceback
            (self.root / 'invoice-adapter-error.json').write_text(formatted_json({'type': type(error).__name__, 'message': str(error)[:400], 'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(error.__traceback__)]}))
            raise

    def _execute(self, private, observer, cancelled):
        from rag_ime.agent_lab.micro import turn_usage
        from rag_ime.agent_lab.golden_pi import _settled_output
        from rag_ime.agent_session_application import WORKSPACE_SCOPE_CONFIRMATION
        job = private['jobId']
        work = self.root / job / 'workspace'
        invoice_materialize(work)
        service = self.service
        source = service.ensure_primary_assistant({'workspaceRoots': [str(work)]})['session']
        created = service.create_primary_task({'clientRequestId': job + ':authorize', 'sourceSessionId': source['id'], 'objective': INVOICE_PROMPT, 'acceptanceCriteria': ['Read five input files; reconcile duplicates, corrections and exceptions accurately', 'Create only reconciliation.csv and summary.json; never change inputs or execute code'], 'workspaceRoots': [str(work)], 'workspaceScopeConfirmation': WORKSPACE_SCOPE_CONFIRMATION})
        sid = created['session']['id']
        allowed = ['workspace_read', 'workspace_write', 'workspace_edit', 'workspace_list', 'workspace_search', 'agent_goal']
        updated = service.update_session(sid, {'allowedTools': allowed})['session']
        if set(updated.get('allowedTools') or []) != set(allowed) or updated.get('workspaceRoots') != [str(work)]:
            raise ValueError('fixture-only tool policy mismatch')
        self.trace.configure({'id': INVOICE_CASE, 'input': INVOICE_PROMPT}, sid, work, authorization=created['authorization'])
        observer.bind_session(sid, cancel=lambda: service.abort(sid))
        selected = service.runtime.set_model(sid, provider='openai-codex', model_id='gpt-6.1-sol')
        if selected.get('selected', {}).get('id') != 'gpt-6.1-sol':
            raise ValueError('wrong model')
        service.runtime.set_thinking_level(sid, level='xhigh')
        service.select_codemode_mode(sid, {'mode': 'off'})
        with self.budget.guard(job_id=job, session_id=sid, case_limit_usd=self.budget.case_limit_usd, max_provider_calls=self.budget.max_provider_calls, max_tool_calls=self.budget.max_tool_calls, max_output_tokens=4096, timeout_seconds=180) as guard:
            if cancelled():
                raise InterruptedError('cancelled before admission')
            client = job + ':reconcile'
            accepted = service.prompt(sid, {'message': INVOICE_PROMPT, 'clientMessageId': client})
            tid = accepted.get('turnId')
            if not tid:
                raise ValueError('unknown admission; no retry')
            observer.bind_session(sid, tid, cancel=lambda: service.abort(sid))
            settled = service.runtime.await_turn_settled(sid, tid, client_message_id=client, timeout_seconds=180)
            usage = account_terminal_usage(settled, sid, tid, client, service.sessions.get(sid)['sessionFile'], guard, turn_usage)
            (self.root / job / 'usage.json').write_text(formatted_json(usage))
            text, _, receipt = _settled_output(settled, sid, tid, client)
            cost = guard.finish()
        checks = invoice_inspect_files(work)
        actual, trace = invoice_trace_checks(self.trace, service, sid, tid, client, work)
        checks.update(actual)
        result = {'status': 'completed', 'qualityVerdict': 'keep' if all(checks.values()) else 'reject', 'caseId': INVOICE_CASE, 'checks': checks, 'fixtureSha256': INVOICE_FIXTURE_SHA256, 'source': INVOICE_SOURCE, 'sessionId': sid, 'turnId': tid, 'clientMessageId': client, 'settlementReceiptId': receipt, 'usage': usage, 'cost': cost, 'trace': trace, 'nativeAcceptance': False, 'evidenceLevel': 'live_runtime_real_files', 'outputFiles': [str(work / 'reconciliation.csv'), str(work / 'summary.json')]}
        (self.root / job / 'oracle.json').write_text(formatted_json(result))
        return result

class MemoryLifecycleAdapter:

    def __init__(self, service, root, budget, trace):
        self.service, self.root, self.budget, self.trace = (service, Path(root), budget, trace)

    def prepare(self, spec, job_id):
        if set(spec) != {'taskId'} or spec['taskId'] not in MEMORY_CASES:
            raise ValueError('unsupported memory controls')
        return {'publicSpec': {'taskId': spec['taskId'], 'fixtureSha256': MEMORY_FIXTURE_SHA256, 'scope': MEMORY_FIXTURE['scope']}, 'privateInput': {'taskId': spec['taskId'], 'jobId': job_id}}

    def execute(self, private, observer, cancelled):
        try:
            return self._execute(private, observer, cancelled)
        except BaseException as error:
            import re, traceback
            details = {'exceptionType': type(error).__name__, 'message': re.sub('(?:Bearer\\s+\\S+|(?:sk|key|token)[-_A-Za-z0-9.]{8,})', '[redacted]', str(error))[:500], 'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(error.__traceback__)]}
            (self.root / 'memory-adapter-error.json').write_text(json.dumps(details, ensure_ascii=False))
            raise

    def _execute(self, private, observer, cancelled):
        from rag_ime.agent_lab.micro import turn_usage
        from rag_ime.agent_lab.golden_pi import _settled_output
        case, job = (private['taskId'], private['jobId'])
        if not Path(self.service.db_path).resolve().is_relative_to(self.root.resolve().parent):
            raise ValueError('memory fixture requires the isolated sibling state database')
        storage = memory_prepare_storage(self.service.db_path, case)
        work = self.root / job
        work.mkdir(parents=True, exist_ok=False)
        (work / 'storage.json').write_text(json.dumps(storage, ensure_ascii=False, indent=2))
        if not storage['passed']:
            return {'status': 'failed', 'qualityVerdict': 'reject', 'storage': storage}
        sid = self.service.ensure_primary_assistant({'workspaceRoots': []})['session']['id']
        observer.bind_session(sid, cancel=lambda: self.service.abort(sid))
        disclosure_receipts = []

        def disclose(value):
            preferences = dict(self.service.sessions.get(sid).get('capabilityDisclosurePreferences') or {})
            receipt = self.service.update_session(sid, {'capabilityDisclosurePreferences': {**preferences, 'tool:memory': value}})
            disclosure_receipts.append({'value': value, 'policyRevision': receipt.get('policyRevision'), 'effectivePreference': receipt['session'].get('capabilityDisclosurePreferences', {}).get('tool:memory')})
        disclose('disabled')
        disabled_before = self.service.memory_context_application.personal_profile_context(sid)
        disclose('enabled')
        checks, context = memory_projection_checks(self.service, sid, case)
        checks['disabledInitiallyEmpty'] = disabled_before == ''
        disclose('disabled')
        checks['disabledClearsProjection'] = self.service.memory_context_application.personal_profile_context(sid) == ''
        disclose('enabled')
        fresh_checks, context = memory_projection_checks(self.service, sid, case)
        checks.update({'reenabled_' + key: value for key, value in fresh_checks.items()})
        checks['explicitOptInReceipt'] = all((r['value'] == r['effectivePreference'] for r in disclosure_receipts))
        (work / 'projection.json').write_text(json.dumps({'checks': checks, 'context': context, 'disclosureReceipts': disclosure_receipts}, ensure_ascii=False))
        if not all(checks.values()):
            return {'status': 'failed', 'qualityVerdict': 'reject', 'storage': storage, 'checks': checks, 'providerCalls': 0}
        self.trace.configure({'id': case, 'input': MEMORY_QUERY}, sid, work)
        model = self.service.runtime.set_model(sid, provider='openai-codex', model_id='gpt-6.1-sol')
        if model.get('selected', {}).get('id') != 'gpt-6.1-sol':
            raise ValueError('model mismatch')
        self.service.runtime.set_thinking_level(sid, level='xhigh')
        self.service.select_codemode_mode(sid, {'mode': 'off'})
        with self.budget.guard(job_id=job, session_id=sid, case_limit_usd=self.budget.case_limit_usd, max_provider_calls=self.budget.max_provider_calls, max_tool_calls=4, max_output_tokens=1024, timeout_seconds=120) as guard:
            if cancelled():
                raise InterruptedError('cancelled before admission')
            client = job + ':query'
            accepted = self.service.prompt(sid, {'message': MEMORY_QUERY, 'clientMessageId': client})
            tid = accepted.get('turnId')
            if not tid:
                raise ValueError('unknown admission; no retry')
            observer.bind_session(sid, tid, cancel=lambda: self.service.abort(sid))
            settled = self.service.runtime.await_turn_settled(sid, tid, client_message_id=client, timeout_seconds=120)
            usage = account_terminal_usage(settled, sid, tid, client, self.service.sessions.get(sid)['sessionFile'], guard, turn_usage)
            text, _, receipt_id = _settled_output(settled, sid, tid, client)
            cost = guard.finish()
        evidence = self.service.runtime.session_tool_evidence(sid, turn_id=tid, client_message_id=client)
        expected = MEMORY_FIXTURE['expectedNewAnswer' if case == 'personal-profile-update-v1' else 'expectedForgottenAnswer']
        checks.update(exactAnswer=text.strip() == expected, noToolCalls=not evidence['toolHistoryEvents'], queryContainsNoAnswer=MEMORY_FIXTURE['new'] not in MEMORY_QUERY and '苹果' not in MEMORY_QUERY and ('梨' not in MEMORY_QUERY), realSettlement=bool(receipt_id))
        return {'status': 'completed', 'qualityVerdict': 'keep' if all(checks.values()) else 'reject', 'caseId': case, 'fixtureSha256': MEMORY_FIXTURE_SHA256, 'storage': storage, 'checks': checks, 'text': text, 'usage': usage, 'cost': cost, 'sessionId': sid, 'turnId': tid, 'clientMessageId': client, 'settlementReceiptId': receipt_id, 'evidenceLevel': 'live_runtime_with_real_memory_storage', 'nativeAcceptance': False}

class ScopeAdapter:

    def __init__(self, service, root, budget, trace):
        self.service, self.root, self.budget, self.trace = (service, Path(root), budget, trace)

    def prepare(self, spec, job_id):
        if spec != {'taskId': SCOPE_CASE}:
            raise ValueError('unsupported scope case')
        return {'publicSpec': {'taskId': SCOPE_CASE, 'fixtureSha256': SCOPE_FIXTURE_SHA256}, 'privateInput': {'jobId': job_id}}

    def execute(self, private, observer, cancelled):
        try:
            return self._execute(private, observer, cancelled)
        except BaseException as error:
            import traceback
            (self.root / 'scope-adapter-error.json').write_text(json.dumps({'type': type(error).__name__, 'message': str(error)[:400], 'frames': [{'file': f.filename, 'line': f.lineno, 'function': f.name} for f in traceback.extract_tb(error.__traceback__)]}))
            raise

    def _execute(self, private, observer, cancelled):
        from rag_ime.agent_lab.micro import turn_usage
        from rag_ime.agent_lab.golden_pi import _settled_output
        job = private['jobId']
        a, b = scope_materialize(self.root / job)
        created = scope_create_task(self.service, a, job + ':authorize')
        sid = created['session']['id']
        self.trace.configure({'id': SCOPE_CASE, 'input': SCOPE_PROMPT}, sid, a, authorization=created['authorization'])
        proof = scope_gateway_proof(self.trace.gateway, sid, a, b)
        (self.root / job / 'gateway-proof.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2))
        if not proof['passed']:
            return {'status': 'failed', 'qualityVerdict': 'reject', 'proof': proof, 'providerCalls': 0}
        before_a, before_b = (scope_hashes(a), scope_hashes(b))
        observer.bind_session(sid, cancel=lambda: self.service.abort(sid))
        selected = self.service.runtime.set_model(sid, provider='openai-codex', model_id='gpt-6.1-sol')
        if selected.get('selected', {}).get('id') != 'gpt-6.1-sol':
            raise ValueError('model mismatch')
        self.service.runtime.set_thinking_level(sid, level='xhigh')
        self.service.select_codemode_mode(sid, {'mode': 'off'})
        with self.budget.guard(job_id=job, session_id=sid, case_limit_usd=self.budget.case_limit_usd, max_provider_calls=self.budget.max_provider_calls, max_tool_calls=self.budget.max_tool_calls, max_output_tokens=1024, timeout_seconds=180) as guard:
            if cancelled():
                raise InterruptedError('cancelled before admission')
            client = job + ':query'
            accepted = self.service.prompt(sid, {'message': SCOPE_PROMPT, 'clientMessageId': client})
            tid = accepted.get('turnId')
            if not tid:
                raise ValueError('unknown admission; no retry')
            observer.bind_session(sid, tid, cancel=lambda: self.service.abort(sid))
            settled = self.service.runtime.await_turn_settled(sid, tid, client_message_id=client, timeout_seconds=180)
            usage = account_terminal_usage(settled, sid, tid, client, self.service.sessions.get(sid)['sessionFile'], guard, turn_usage)
            (self.root / job / 'usage.json').write_text(json.dumps(usage))
            text, _, receipt = _settled_output(settled, sid, tid, client)
            cost = guard.finish()
        checks, evidence = scope_live_checks(self.service, self.trace, sid, tid, client, text, a, b, before_a, before_b)
        return {'status': 'completed', 'qualityVerdict': 'keep' if all(checks.values()) else 'reject', 'caseId': SCOPE_CASE, 'fixtureSha256': SCOPE_FIXTURE_SHA256, 'proof': proof, 'checks': checks, 'evidence': evidence, 'text': text, 'sessionId': sid, 'turnId': tid, 'clientMessageId': client, 'settlementReceiptId': receipt, 'usage': usage, 'cost': cost, 'nativeAcceptance': False}


def run_case(*, service, root, case_id, budget, trace):
    """Admit one explicit bounded case; unknown effects are never replayed."""
    from .trial_execution import AgentLabTrialApplication
    from .trials import AgentLabTrialStore
    if case_id not in fixtures.CASES:
        raise ValueError("case is not implemented by this acceptance package")
    root=Path(root).resolve()
    root.mkdir(parents=True,exist_ok=False)
    if case_id in SUPPORTED:
        adapter=AcceptanceAdapter(service,root,fixtures.MANIFEST,budget,trace)
    elif case_id in MEMORY_CASES:
        adapter=MemoryLifecycleAdapter(service,root,budget,trace)
    elif case_id==INVOICE_CASE:
        adapter=InvoiceAdapter(service,root,budget,trace)
    else:
        adapter=ScopeAdapter(service,root,budget,trace)
    app=AgentLabTrialApplication(AgentLabTrialStore(root/"trial.sqlite"),{"paw-acceptance":adapter},start_workers=False)
    try:
        admitted=app.start("acceptance:"+root.name+":"+case_id,"paw-acceptance",{"taskId":case_id})
        return app.run_job(admitted["job"]["jobId"])
    finally:
        app.close()
