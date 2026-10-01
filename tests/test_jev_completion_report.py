import unittest
from types import SimpleNamespace
from rag_ime.jev_tasks.completion_report import completion_report


def task(id='task', parent='', artifacts=(), state='done'):
    return SimpleNamespace(id=id, parent_id=parent, artifacts=artifacts, state=state, objective='交付可玩游戏')


class CompletionReportTests(unittest.TestCase):
    def test_artifacts_and_real_verdicts_are_published(self):
        report = completion_report('游戏已交付，打开 index.html。', [task(artifacts=('/project/index.html',))],
            {'task': {'operabilityVerdict': 'passed', 'requirementVerdict': 'satisfied', 'reason': '浏览器交互已检查'}}, success=True)
        for text in ['成果报告', '/project/index.html', '浏览器交互已检查', '未完成与未验证项']:
            self.assertIn(text, report)

    def test_missing_verification_is_not_reported_as_passed(self):
        report = completion_report('已保存代码。', [task(), task('child', 'task', state='failed')], {}, success=False)
        self.assertIn('尚未全部完成', report)
        self.assertIn('未完成或验证不完整', report)
        self.assertNotIn('已验收', report)

    def test_report_uses_readable_current_paths_without_changing_verdicts(self):
        old = '/project/report.md'
        active = '/project/docs/agent/work/active/report.md'
        current = '/project/docs/agent/work/archive/report.md'
        report = completion_report('保留真实失败。', [task(artifacts=(old, active), state='failed')], {},
            success=False, artifact_revisions={'task': [
                {'sourceRef': old, 'status': 'available', 'resolvedRef': current, 'relocationReceiptId': 'receipt:register'},
                {'sourceRef': active, 'status': 'available', 'resolvedRef': current, 'relocationReceiptId': 'receipt:archive'},
            ]})
        self.assertEqual(report.count('- ' + current), 1)
        self.assertNotIn('- ' + old, report)
        self.assertNotIn('- ' + active, report)
        self.assertIn('尚未全部完成', report)
        self.assertNotIn('已验收', report)

    def test_unavailable_or_other_task_revision_cannot_rewrite_delivery_reference(self):
        old = '/project/report.md'
        for revisions in [
            {'task': [{'sourceRef': old, 'status': 'unavailable', 'resolvedRef': '/wrong', 'relocationReceiptId': 'receipt'}]},
            {'other-task': [{'sourceRef': old, 'status': 'available', 'resolvedRef': '/wrong', 'relocationReceiptId': 'receipt'}]},
            {'task': [{'sourceRef': old, 'status': 'available', 'resolvedRef': '/wrong'}]},
        ]:
            report = completion_report('报告。', [task(artifacts=(old,))], {}, success=False, artifact_revisions=revisions)
            self.assertIn('- ' + old, report)
            self.assertNotIn('/wrong', report)

    def test_greeting_remains_a_direct_answer(self):
        self.assertEqual(completion_report('Hi!', [task()], {}, success=True), 'Hi!')

    def test_substantial_text_deliverable_also_gets_a_report(self):
        report = completion_report('研究结论。' * 100, [task()], {}, success=True)
        self.assertIn('成果报告', report)
        self.assertIn('文字成果见上方答复', report)

    def test_long_narrative_keeps_receipts_and_post_budget(self):
        report = completion_report('内容' * 8000, [task(artifacts=('/project/result.html',))], {}, success=False)
        self.assertLessEqual(len(report), 16000)
        self.assertIn('/project/result.html', report)
        self.assertIn('完整答复保留在执行记录中', report)

    def test_large_graph_reports_remaining_counts_without_exceeding_post_limit(self):
        tasks = [task(str(i), 'root', artifacts=(f'/project/{i}-' + 'x' * 500,), state='failed')
                 for i in range(200)]
        for item in tasks:
            item.objective = '任务' * 100
        report = completion_report('内容' * 8000, tasks,
            {item.id: {'reason': '尚未验证' * 200} for item in tasks}, success=False)
        self.assertLessEqual(len(report), 16000)
        self.assertIn('194 项未关闭', report)
        self.assertIn('完整记录见“任务与分派”', report)
