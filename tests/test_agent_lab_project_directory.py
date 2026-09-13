from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rag_ime.agent_lab.project_application import AgentLabProjectApplication
from rag_ime.agent_lab.projects import AgentLabProjectStore


class ProjectDirectoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'lab.sqlite'
        self.app = AgentLabProjectApplication(self.db, session_application=Mock(), current_model=lambda: {})
        self.app.store = AgentLabProjectStore(self.db)
        self.serial = 0
        self.project = None

    def command(self, action, value):
        self.serial += 1
        result = self.app.command({'action': action, 'projectId': self.project['projectId'] if self.project else '',
            'expectedRevision': self.project['revision'] if self.project else 0,
            'clientRequestId': f'directory-{self.serial}', 'input': value})
        self.project = result['project']
        return result

    def create(self):
        return self.command('create', {'title': '冰架论文比较', 'description': '使用已有语料比较两篇论文，保留已完成的结果。'})

    def test_creation_has_real_directory_without_starting_guide(self):
        self.create()
        directory = self.project['directory']
        root = Path(directory['path'])
        self.assertEqual(directory['status'], 'ready')
        self.assertTrue((root / 'AGENTS.md').is_file())
        self.assertTrue((root / 'docs/README.md').is_file())
        self.assertIn('使用已有语料', (root / 'docs/requirements.md').read_text())
        self.assertTrue((root / 'docs/CONTINUE.md').is_file())
        self.assertFalse((root / 'SKILL.md').exists())
        self.assertFalse(self.project['guideSessionId'])
        self.app.sessions.create_in_transaction.assert_not_called()

    def test_reopening_legacy_history_allocates_folder_without_guide_or_rerun(self):
        prepared = {'sceneId': 'cloudops', 'sourceHash': 'source-one', 'experimentCount': 1,
            'project': {'title': '事故诊断', 'description': '保留已完成实验'},
            'artifacts': [{'title': '已有记录', 'view': 'json', 'content': {
                'schemaVersion': 'paw.lab-imported-experiments.v1', 'experiments': []}}]}
        store = AgentLabProjectStore(self.db, bind_execution=lambda *_: {'ownerRef': {'kind': 'scene_trial', 'id': 'cloudops'}, 'summary': '已有记录'})
        project = store.command({'action': 'import_history', 'projectId': '', 'expectedRevision': 0,
            'clientRequestId': 'history', 'input': {}}, history_import=lambda _: prepared)['project']
        self.app.read_trials = lambda: {'schemaVersion': 'trial.v1', 'jobs': [], 'registeredSceneIds': []}
        reopened = self.app.read({'projectId': project['projectId']})['project']
        self.assertTrue(Path(reopened['directory']['path']).is_dir())
        self.assertEqual(reopened['directory']['status'], 'ready')
        self.assertEqual(reopened['guideSessionId'], '')
        self.app.sessions.create_in_transaction.assert_not_called()

    def test_material_artifact_versions_and_real_method_are_projected(self):
        self.create()
        self.command('import_materials', {'materials': [{'title': '论文目录', 'kind': 'document', 'text': '两篇已有论文'}]})
        first = self.command('publish_artifact', {'title': '比较结果', 'view': 'markdown', 'content': '# 原比较结果'})['artifact']
        self.command('publish_artifact', {'artifactId': first['artifactId'], 'expectedArtifactRevision': 1, 'content': '# 更正后的结果'})
        self.command('publish_artifact', {'title': '论文比较方法', 'kind': 'project_method', 'view': 'markdown',
            'content': '---\nname: polar-comparison\ndescription: 比较已有论文\n---\n先检查已保存来源，再比较机制。'})
        root = Path(self.project['directory']['path'])
        self.assertIn('检查已保存来源', (root / 'SKILL.md').read_text())
        files = list((root / 'records/artifacts').rglob('*.md'))
        text = '\n'.join(file.read_text() for file in files)
        self.assertIn('# 原比较结果', text)
        self.assertIn('# 更正后的结果', text)
        self.assertIn('两篇已有论文', '\n'.join(file.read_text() for file in (root / 'records/materials').rglob('*.json')))

    def test_manual_files_are_preserved_and_not_adopted_for_later_overwrite(self):
        self.create()
        root = Path(self.project['directory']['path'])
        (root / 'AGENTS.md').write_text('人工维护的入口')
        (root / 'docs/requirements.md').write_text('人工补充的需求')
        (root / 'manual.txt').write_text('不要删除')
        for title in ('新版标题', '再次更新'):
            self.command('update_brief', {'title': title})
            self.assertEqual(self.project['directory']['status'], 'partial')
            self.assertEqual((root / 'docs/requirements.md').read_text(), '人工补充的需求')
        self.assertEqual((root / 'AGENTS.md').read_text(), '人工维护的入口')
        self.assertEqual((root / 'manual.txt').read_text(), '不要删除')

    def test_interrupted_file_write_keeps_committed_artifact_and_recovers_on_read(self):
        from rag_ime.agent_lab.project_directory import ProjectDirectoryProjection
        self.create()
        with patch.object(ProjectDirectoryProjection, '_atomic_write', side_effect=OSError('disk unavailable')):
            result = self.command('publish_artifact', {'title': '真实结果', 'view': 'markdown', 'content': '已保存且不用重跑'})
        self.assertIn(result['project']['directory']['status'], {'partial', 'unavailable'})
        artifact_id = result['artifact']['artifactId']
        recreated = AgentLabProjectApplication(self.db, session_application=Mock(), current_model=lambda: {})
        project = recreated.read({'projectId': self.project['projectId']})['project']
        self.assertEqual(project['directory']['status'], 'ready')
        self.assertEqual(len(project['artifacts']), 1)
        self.assertEqual(project['artifacts'][0]['artifactId'], artifact_id)
        self.assertIn('已保存且不用重跑', '\n'.join(p.read_text() for p in Path(project['directory']['path']).rglob('*.md')))
        recreated.sessions.create_in_transaction.assert_not_called()

    def test_restart_is_idempotent_and_private_evaluation_fields_are_not_mirrored(self):
        self.create()
        self.command('publish_artifact', {'title': '实验摘要', 'view': 'json', 'content': {
            'metric': .9, 'cases': [{'referenceAnswer': 'SECRET_GOLD'}],
            'prompt': 'SECRET_PROMPT', 'thinking': 'SECRET_THINKING', 'evidenceRefs': ['run-one']}})
        first = self.app.read({'projectId': self.project['projectId']})['project']['directory']
        second = self.app.read({'projectId': self.project['projectId']})['project']['directory']
        self.assertEqual(first, second)
        root = Path(first['path'])
        contents = '\n'.join(p.read_text() for p in root.rglob('*') if p.is_file())
        self.assertNotIn('SECRET_', contents)
        self.assertIn('run-one', contents)

    def test_symlink_in_generated_directory_is_not_followed(self):
        self.create()
        root = Path(self.project['directory']['path'])
        outside = Path(self.temp.name) / 'outside'
        outside.mkdir()
        target = root / 'records/materials'
        target.rename(root / 'original-materials')
        target.symlink_to(outside, target_is_directory=True)
        self.command('import_materials', {'materials': [{'title': '材料', 'kind': 'document', 'text': '内容'}]})
        self.assertEqual(self.project['directory']['status'], 'partial')
        self.assertEqual(list(outside.iterdir()), [])

    def test_stale_response_cannot_overwrite_newer_committed_projection(self):
        self.create()
        stale = dict(self.project)
        self.command('update_brief', {'description': '新版已确认要求'})
        directory = self.app.directory.sync(stale)
        self.assertEqual(directory['sourceRevision'], self.project['revision'])
        self.assertIn('新版已确认要求', (Path(directory['path']) / 'docs/requirements.md').read_text())

    def test_invalid_manifest_can_be_rebuilt_without_losing_saved_results(self):
        self.create()
        root = Path(self.project['directory']['path'])
        (root / '.paw/project-projection.json').write_text('[]')
        directory = self.app.directory.sync(self.project)
        self.assertEqual(directory['status'], 'ready')
        self.assertEqual(json.loads((root / '.paw/project-projection.json').read_text())['projectId'], self.project['projectId'])


if __name__ == '__main__':
    unittest.main()
