"""Proposal disclosure and recoverable validation, without installed/model calls."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator
from rag_ime.agent_tools import ControlToolGateway, _runtime_tool_parameter_schema
from rag_ime.jev_tasks.decider import JevChoices
from rag_ime.jev_tasks.types import GraphConflict, GraphError
from rag_ime.jev_tasks.submission_contracts import submission_contract, validate_submission
from tests.test_jev_host_application import JevHostFixture
from tests import test_jev_failure_lifecycle as failure


VALID = {
    'verification_submit': {'operabilityVerdict': 'unverified', 'requirementVerdict': 'unverified',
                            'reason': 'Evidence unavailable', 'evidenceRefs': ['fixture:check']},
    'result_submit': {'resultSummary': 'Observed result', 'artifactRefs': [], 'evidenceRefs': ['fixture:check']},
    'final_submit': {'content': 'Unresolved items remain', 'evidenceRefs': ['fixture:check']},
}
GENERIC = {key: 'private-body-probe' for key in ('verdict', 'summary', 'operability',
    'requirementSatisfaction', 'requirementFindings', 'qualityFindings', 'evidence',
    'requiredFixes', 'optionalImprovements', 'residualRisk', 'reviewDocumentUpdateReceipt')}


class JevSubmissionSchemaTests(unittest.TestCase):
    def test_submission_contracts_import_without_development_dependencies(self):
        root = str(Path(__file__).resolve().parents[1])
        result = subprocess.run([sys.executable, '-I', '-S', '-c',
            'import sys; sys.path.insert(0, sys.argv[1]); '
            'from rag_ime.jev_tasks.submission_contracts import validate_submission; '
            'validate_submission("result_submit", {"resultSummary":"Observed", "evidenceRefs":["fixture:check"]})',
            root], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def validator(self, op):
        return Draft202012Validator(_runtime_tool_parameter_schema('room_partner', ['list', op]))

    def test_current_runtime_schema_rejects_generic_agent_result_for_every_submission(self):
        for op, proposal in VALID.items():
            with self.subTest(op=op):
                validator = self.validator(op)
                validator.validate({'op': op, 'proposal': proposal})
                self.assertFalse(validator.is_valid({'op': op, 'proposal': GENERIC}))
                self.assertFalse(validator.is_valid({'op': op, 'proposal': {}}))
                self.assertFalse(validator.is_valid({'op': op, 'proposal': {**proposal, 'verdict': 'passed'}}))
                self.assertTrue(validator.is_valid({'op': 'list'}))

    def test_runtime_schema_discloses_host_limits_and_enums(self):
        invalid = [
            ('verification_submit', {'operabilityVerdict': 'success'}),
            ('verification_submit', {'requirementVerdict': 'passed'}),
            ('verification_submit', {'reason': 'x' * 2001}),
            ('result_submit', {'resultSummary': 'x' * 4001}),
            ('result_submit', {'artifactRefs': ['x'] * 17}),
            ('result_submit', {'artifactRefs': ['x' * 1001]}),
            ('final_submit', {'content': 'x' * 16001}),
        ]
        invalid += [(op, change) for op in VALID for change in (
            {'evidenceRefs': []}, {'evidenceRefs': ['x'] * 25}, {'evidenceRefs': ['x' * 1001]},
            {'evidenceRefs': ['   ']}, {'evidenceRefs': ['x\0y']})]
        for op, change in invalid:
            with self.subTest(op=op, field=next(iter(change))):
                self.assertFalse(self.validator(op).is_valid({'op': op, 'proposal': {**VALID[op], **change}}))
                with self.assertRaises(GraphError):
                    validate_submission(op, {**VALID[op], **change})

    def test_host_and_disclosed_contract_agree_on_json_shapes(self):
        values = [None, False, 1, 1.5, {}, [], ['fixture:check'], '', ' ', '\0', 'observed',
                  'passed', 'failed', 'satisfied', 'not_satisfied', 'unverified']
        for op, valid in VALID.items():
            schema = Draft202012Validator(submission_contract(op)['proposalSchema'])
            candidates = [valid, GENERIC, None, [], False, {**valid, 'unknown': 'private-body-probe'}]
            for field in valid:
                candidates.append({key: value for key, value in valid.items() if key != field})
                candidates.extend({**valid, field: value} for value in values)
            for proposal in candidates:
                with self.subTest(op=op, proposal=proposal):
                    try:
                        validate_submission(op, proposal)
                        accepted = True
                    except GraphError:
                        accepted = False
                    self.assertEqual(accepted, schema.is_valid(proposal))


    def test_host_errors_explain_limits_without_echoing_values(self):
        for op, field, limit in (('verification_submit', 'reason', 2000),
                                ('result_submit', 'resultSummary', 4000),
                                ('final_submit', 'content', 16000)):
            with self.subTest(op=op):
                valid = {**VALID[op], field: 'x' * limit}
                validate_submission(op, valid)
                self.validator(op).validate({'op': op, 'proposal': valid})
                for invalid in (GENERIC, None, {**valid, field: 'private-body-probe' * limit}):
                    with self.assertRaises(GraphError) as error:
                        validate_submission(op, invalid)
                    self.assertIn(field, str(error.exception))
                    self.assertIn(str(limit), str(error.exception))
                    self.assertIn('64000', str(error.exception))
                    self.assertNotIn('private-body-probe', str(error.exception))
                    self.assertLess(len(str(error.exception)), 2000)
        # Legal character lengths can still exceed the existing UTF-8 budget.
        over_bytes = {**VALID['final_submit'], 'content': '界' * 16000,
                      'evidenceRefs': ['界' * 1000] * 6}
        self.validator('final_submit').validate({'op': 'final_submit', 'proposal': over_bytes})
        with self.assertRaisesRegex(GraphError, 'byte limit'):
            validate_submission('final_submit', over_bytes)

    def test_contract_projections_cannot_mutate_other_sessions_or_acceptance_rules(self):
        contract = submission_contract('verification_submit')
        contract['proposalSchema']['properties']['operabilityVerdict']['enum'].append('success')
        self.assertNotIn('success', submission_contract('verification_submit')['proposalSchema']['properties']['operabilityVerdict']['enum'])
        self.assertFalse(self.validator('verification_submit').is_valid({'op': 'verification_submit',
            'proposal': {**VALID['verification_submit'], 'operabilityVerdict': 'success'}}))
        validate_submission('result_submit', {'resultSummary': 'Observed', 'evidenceRefs': ['fixture:check']})


class JevSubmissionRepairTests(JevHostFixture):
    effects = failure.JevFailureLifecycleTests.effects
    active_effect = failure.JevFailureLifecycleTests.active_effect
    submit = failure.JevFailureLifecycleTests.submit
    finish = failure.JevFailureLifecycleTests.finish

    def setUp(self):
        super().setUp()
        self.gateway = ControlToolGateway(sessions=self.service.sessions, management=object(), core=object(),
            project=self.service.project, collaboration=self.service, background_jobs=self.service.background_jobs,
            delegation=self.service.delegation, work_documents=self.service.work_documents)
        self.service.bind_tool_manifest_provider(self.gateway.runtime_manifests)
        self.app.driver.controller.decider = JevChoices(failure.choose_valid_progress)
        self.terminals = {}
        original = self.app.execution_terminal
        p = patch.object(self.app, 'execution_terminal', side_effect=lambda effect, **kw:
            self.terminals.get(effect['effectId']) or original(effect, **kw))
        p.start(); self.addCleanup(p.stop)

    def verifier(self):
        created = self.create()
        worker = self.active_effect(created, 'execute')
        self.submit(worker, 'result_submit', VALID['result_submit'])
        self.finish(worker)
        return created, self.active_effect(created, 'verify')

    def test_invalid_verification_is_correctable_in_same_turn_without_accepting_prose(self):
        created, effect = self.verifier()
        with self.assertRaises(GraphError) as error:
            self.submit(effect, 'verification_submit', GENERIC)
        message = str(error.exception)
        self.assertIn('operabilityVerdict', message)
        self.assertIn('not_satisfied', message)
        self.assertIn('2000', message)
        self.assertNotIn('private-body-probe', message)
        with self.app.ledger.connection() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM agent_jev_execution_outputs WHERE dispatch_id=?',
                                          (effect['effectId'],)).fetchone())
        with self.assertRaises(GraphConflict):
            self.submit(effect, 'final_submit', VALID['final_submit'])
        listing = self.app.tool_operation(effect['request']['sessionId'], {'op': 'list'}, tool_call_id='recover-contract')
        contract = listing['submissionContract']
        self.assertEqual(contract['operation'], 'verification_submit')
        self.assertEqual(contract['maxEncodedBytes'], 64000)
        Draft202012Validator(contract['proposalSchema']).validate(VALID['verification_submit'])
        manifest = next(t for t in self.gateway.runtime_manifests(self.service.sessions.get(effect['request']['sessionId']))
                        if t['name'] == 'room_partner')
        branch = next(b for b in manifest['parameters']['oneOf']
                      if b['properties']['op']['const'] == 'verification_submit')
        self.assertEqual(branch['properties']['proposal'], contract['proposalSchema'])
        accepted = self.submit(effect, 'verification_submit', VALID['verification_submit'])
        self.assertEqual(accepted['status'], 'submitted')
        self.assertTrue(self.submit(effect, 'verification_submit', VALID['verification_submit'])['replayed'])
        with self.app.ledger.connection() as conn:
            output = json.loads(conn.execute('SELECT payload_json FROM agent_jev_execution_outputs WHERE dispatch_id=?',
                                            (effect['effectId'],)).fetchone()[0])
        self.assertEqual(output, VALID['verification_submit'])
        self.assertEqual(self.snapshot(created).tasks[0].state, 'review')
        self.assertEqual(len(self.calls), 2)
