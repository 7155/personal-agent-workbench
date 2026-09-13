"""Recoverable, one-way Lab database to project-directory projection.

Project/Knowledge/Golden/App/Pi stores retain authority. This owner creates no
sessions or jobs and never imports edited generated files back into those stores.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import threading
import time
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

_LOCK = threading.RLock()
_SCHEMA = 'paw.lab-project-directory.v1'
_MAX_FILES = 1500
_MAX_BYTES = 32 * 1024 * 1024
_PRIVATE = {'cases', 'privatecases', 'references', 'referenceanswer', 'referenceanswers', 'expectedanswer',
            'gold', 'goldanswer', 'hiddenlabels', 'humanverdict', 'judgments', 'prompt', 'systemprompt',
            'thinking', 'reasoning', 'apikey', 'accesstoken', 'refreshtoken', 'password', 'secret'}


def _json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _public(item) for key, item in value.items()
                if re.sub(r'[^a-z]', '', key.lower()) not in _PRIVATE}
    if isinstance(value, list):
        return [_public(item) for item in value]
    return value


def _filename(identifier: Any) -> str:
    text = str(identifier)
    cleaned = re.sub(r'[^A-Za-z0-9_.-]', '-', text)[:150].strip('.') or 'record'
    return cleaned if cleaned == text else cleaned + '-' + _hash(text.encode())[:10]


def managed_workspace(db_path: Path, scope_id: str, project: Mapping[str, Any]) -> dict[str, Any]:
    """Stable mechanical binding, independent of whether a Guide exists."""
    identifier = str(project['projectId'])
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,239}', identifier):
        raise ValueError('项目目录标识无效。')
    scope = _hash(scope_id.encode())[:16]
    existing = project.get('executionWorkspace')
    if isinstance(existing, Mapping) and existing.get('path'):
        root = Path(str(existing['path']))
        if (existing.get('kind') != 'managed' or not root.is_absolute() or root.name != identifier
                or root.parent.name != scope or root.parent.parent.name != 'lab-workspaces'):
            raise ValueError('已有项目目录绑定不符合托管目录结构，保留原绑定等待核对。')
        created = existing.get('createdAtMs', project.get('createdAtMs', 0))
    else:
        root = db_path.expanduser().resolve().parent / 'lab-workspaces' / scope / identifier
        created = project.get('createdAtMs', 0)
    return {'kind': 'managed', 'path': str(root), 'createdAtMs': created}


def ensure_workspace(root: Path) -> None:
    # The database/storage parent is configured by the host. The managed tree
    # itself cannot redirect writes through a symlink.
    for directory in (root.parent.parent, root.parent, root):
        if directory.is_symlink():
            raise ValueError('项目目录不可通过符号链接重定向。')
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)


class ProjectDirectoryProjection:
    def __init__(self, db_path: str | Path, *, scope_id: str = 'local') -> None:
        self.db_path = Path(db_path)
        self.scope_id = scope_id

    @staticmethod
    def _path(root: Path, relative: str) -> Path:
        value = Path(relative)
        if value.is_absolute() or '..' in value.parts:
            raise ValueError('生成文件路径无效。')
        current = root
        for part in value.parts:
            current /= part
            if current.is_symlink():
                raise ValueError('保留符号链接，不向链接目标写入生成文件。')
        return current

    @staticmethod
    def _atomic_write(path: Path, value: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix='.paw-writing-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'wb') as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _snapshot(self, project_id: str) -> dict[str, Any]:
        with closing(sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute('BEGIN')
            row = conn.execute('SELECT payload_json FROM agent_lab_projects WHERE project_id=? AND scope_id=?',
                               (project_id, self.scope_id)).fetchone()
            if row is None:
                raise ValueError('项目不存在于当前工作空间。')
            project = json.loads(row[0])
            def records(query: str, args: tuple = (project_id,)) -> list[dict]:
                return [json.loads(value[0]) for value in conn.execute(query, args)]
            snapshot = {'project': project,
                'briefs': [dict(row) for row in conn.execute('SELECT version,payload_json FROM agent_lab_project_brief_versions WHERE project_id=? ORDER BY version', (project_id,))],
                'materials': records('SELECT payload_json FROM agent_lab_project_material_sets WHERE project_id=? ORDER BY version'),
                'artifacts': records('SELECT v.payload_json FROM agent_lab_project_artifact_versions v JOIN agent_lab_project_artifacts a USING(artifact_id) WHERE a.project_id=? ORDER BY v.artifact_id,v.revision'),
                'applications': [], 'runs': []}
            for row in conn.execute('SELECT a.app_id,a.active_version,v.version,v.payload_json FROM agent_lab_apps a JOIN agent_lab_app_versions v USING(app_id) WHERE a.project_id=? ORDER BY a.app_id,v.version', (project_id,)):
                version = json.loads(row['payload_json'])
                snapshot['applications'].append({'appId': row['app_id'], 'version': row['version'],
                    'activeVersion': row['active_version'], 'contentHash': version.get('contentHash'),
                    'sourceDirectory': version.get('sourceDirectory'), 'assetKey': version.get('assetKey'),
                    'exports': {target: {key: value for key, value in export.items() if key != 'base64'}
                                for target, export in version.get('exports', {}).items()},
                    'knowledge': {key: value for key, value in version.get('spec', {}).get('knowledge', {}).items()
                                  if key in {'sourceIndexId', 'documentCount', 'sourceCount', 'chunkCount', 'corpusHash', 'snapshotSha256'}}})
            suite_ids = list(dict.fromkeys(binding.get('ownerRef', {}).get('id') for binding in project.get('bindings', [])
                                          if binding.get('ownerRef', {}).get('kind') == 'golden_suite'))
            for suite_id in suite_ids:
                for row in conn.execute('SELECT job_id,kind,state,updated_at_ms,payload_json FROM agent_lab_golden_jobs WHERE suite_id=? ORDER BY created_at_ms', (suite_id,)):
                    payload = json.loads(row['payload_json'])
                    result = payload.get('result') or {}
                    snapshot['runs'].append({'jobId': row['job_id'], 'owner': 'golden', 'suiteId': suite_id,
                        'kind': row['kind'], 'state': row['state'], 'updatedAtMs': row['updated_at_ms'],
                        'sessionId': payload.get('sessionId'), 'progress': payload.get('progress'),
                        'comparison': _public(result.get('comparison')), 'usage': _public(result.get('usage')),
                        'applicationMethodComparison': _public(result.get('applicationMethodComparison'))})
            for row in conn.execute("SELECT job_id,state,updated_at_ms,public_spec_json,result_json FROM agent_lab_trials WHERE json_extract(public_spec_json,'$.projectId')=? ORDER BY created_at_ms", (project_id,)):
                spec = json.loads(row['public_spec_json'])
                result = json.loads(row['result_json']) if row['result_json'] else {}
                snapshot['runs'].append({'jobId': row['job_id'], 'owner': 'knowledge', 'state': row['state'],
                    'updatedAtMs': row['updated_at_ms'], 'spec': _public(spec),
                    'result': {key: result.get(key) for key in ('kind', 'corpusId', 'indexId', 'datasetId', 'corpusHash',
                               'documentCount', 'sourceCount', 'chunkCount', 'configHash') if key in result}})
            return snapshot

    def _files(self, snapshot: dict[str, Any], root: Path) -> dict[str, tuple[str, str, bytes]]:
        project = snapshot['project']
        files: dict[str, tuple[str, str, bytes]] = {}
        def add(path: str, title: str, kind: str, content: Any) -> None:
            files[path] = (title, kind, content.encode() if isinstance(content, str) else _json(_public(content)))
        add('AGENTS.md', '项目入口', 'entry', f"# {project['title']}\n\n"
            '先阅读 docs/README.md、docs/requirements.md 与 docs/CONTINUE.md。若 SKILL.md 已存在，读取项目已保存的方法。\n\n'
            '项目状态与版本由 lab_project、Knowledge、Golden、App 和 Pi 的既有 owner 管理。'
            'records 与 outputs 是来源可查的文件投影，不是第二套运行状态。继续前查询原 jobId；复用已完成结果，未知调用恢复原任务，不重新提交。\n\n'
            '项目源码保留在现有应用/源码目录。修改生成文档请通过项目的 publish_artifact、update_brief 或 import_materials 保存新版本；'
            '系统保留手工修改，不会自动反向覆盖数据库。活跃工作文档仍通过现有 WorkDocument 绑定真实 Session goal/work item。\n')
        requirement_lines = [f"# {project['title']} · 项目要求", '', '以下为已保存的项目描述版本；不把 Agent 整理的描述冒充用户原话。', '']
        for brief in snapshot['briefs']:
            body = json.loads(brief['payload_json'])
            requirement_lines += [f"## 描述版本 {brief['version']}", '', str(body.get('description', '')), '']
        add('docs/requirements.md', '项目要求', 'requirements', '\n'.join(requirement_lines))
        artifact_index = []
        methods = []
        latest: dict[str, dict] = {}
        for artifact in snapshot['artifacts']:
            identifier, revision = artifact['artifactId'], artifact['revision']
            latest[identifier] = artifact
            view, content = artifact['view'], artifact['content']
            suffix = {'markdown': 'md', 'html': 'html'}.get(view, 'json')
            relative = f'records/artifacts/{_filename(identifier)}/v{revision}.{suffix}'
            add(relative, f"{artifact['title']} · v{revision}", 'artifact', content)
            artifact_index.append({'artifactId': identifier, 'revision': revision, 'title': artifact['title'], 'file': relative,
                                   'view': view, 'source': 'agent_lab_project_artifact_versions'})
            if artifact.get('kind') == 'project_method' and view == 'markdown' and isinstance(content, str):
                methods.append(artifact)
            if isinstance(content, dict) and content.get('schemaVersion') == 'paw.lab-imported-experiments.v1':
                for experiment in content.get('experiments', []):
                    if not isinstance(experiment, dict) or not experiment.get('experimentId'):
                        continue
                    path = f"records/experiments/{_filename(experiment['experimentId'])}/{_filename(experiment.get('revisionSha256', 'original'))}.json"
                    add(path, str(experiment.get('title') or experiment['experimentId']), 'experiment',
                        {'artifactRef': {'artifactId': identifier, 'revision': revision}, 'experiment': experiment,
                         'executionPerformedByProjection': False})
        add('records/artifacts/index.json', '成果与原版本索引', 'index', artifact_index)
        if methods:
            method = max(methods, key=lambda value: (value.get('updatedAtMs', 0), value['revision'], value['artifactId']))
            add('SKILL.md', method['title'], 'skill', method['content'])
        material_index = []
        for material_set in snapshot['materials']:
            version = material_set['version']
            path = f'records/materials/v{version}.json'
            add(path, f'材料版本 {version}', 'materials', material_set)
            material_index.append({'materialSetId': material_set['materialSetId'], 'version': version, 'file': path})
        add('records/materials/index.json', '材料来源索引', 'index', material_index)
        add('records/runtime/jobs.json', '已保存运行记录', 'runtime', snapshot['runs'])
        add('records/runtime/bindings.json', '原执行 owner 与绑定', 'runtime', [
            {key: binding[key] for key in ('bindingId', 'adapterId', 'ownerRef', 'materialSetId', 'artifactId', 'artifactRevision') if key in binding}
            for binding in project.get('bindings', [])])
        applications = []
        for app in snapshot['applications']:
            value = {**app}
            directory = app.get('sourceDirectory')
            if isinstance(directory, str) and not Path(directory).is_absolute() and '..' not in Path(directory).parts:
                value['sourcePath'] = str(root / directory)
                value['sourceExists'] = (root / directory).is_dir()
            if app.get('assetKey'):
                value['packagePaths'] = {target: str(self.db_path.resolve().parent / 'lab-app-packages' / app['assetKey'] / (target + '.zip'))
                                         for target in app['exports']}
            else:
                value['packageOwner'] = 'lab_project App download: immutable bytes stored in version exports'
            applications.append(value)
        add('outputs/index.json', '应用、源码和导出来源', 'outputs', applications)
        continuation = [f"# {project['title']} · 继续工作", '', f"项目：`{project['projectId']}`，描述/成果修订：{project['revision']}。", '',
            '此文件来自已保存记录。当前执行状态以原任务 owner 的读取为准；目录重建不会运行模型、创建 Guide 或重新评测。', '', '## 已保存成果', '']
        continuation += [f"- [{artifact['title']}](../records/artifacts/{_filename(artifact['artifactId'])}/v{artifact['revision']}.{'md' if artifact['view']=='markdown' else 'html' if artifact['view']=='html' else 'json'}) · v{artifact['revision']}" for artifact in latest.values()]
        continuation += ['', '## 原运行与恢复', '']
        continuation += [f"- `{run['jobId']}` · {run['owner']} · {run['state']}。完成结果复用；中断或未知状态查询并恢复原调用。" for run in snapshot['runs']]
        continuation += ['', '## 下一步', '', '先查看最新项目步骤成果及 [应用与源码索引](../outputs/index.json)，核对既有结果和阻塞；按已确认的项目方法继续，不重新开始。', '']
        add('docs/CONTINUE.md', '接手与结果恢复', 'continuation', '\n'.join(continuation))
        add('docs/README.md', '项目文件索引', 'index', f"# {project['title']}\n\n"
            '- [项目要求](requirements.md)\n- [接手与结果恢复](CONTINUE.md)\n'
            '- [材料及原版本](../records/materials/index.json)\n- [成果及原版本](../records/artifacts/index.json)\n'
            '- [原运行记录](../records/runtime/jobs.json)\n- [执行 owner 与绑定](../records/runtime/bindings.json)\n- [应用、源码与导出](../outputs/index.json)\n\n'
            + ('[项目方法 Skill](../SKILL.md) 已由真实 project_method 成果生成。\n' if methods else '尚无已保存的 project_method 成果；不自动编造领域方法。\n'))
        return files

    def sync(self, project: Mapping[str, Any]) -> dict[str, Any]:
        response = {'path': '', 'status': 'unavailable', 'sourceRevision': int(project.get('revision', 0)),
                    'generatedAtMs': 0, 'files': [], 'warnings': []}
        try:
            workspace = managed_workspace(self.db_path, self.scope_id, project)
            root = Path(workspace['path'])
            response['path'] = str(root)
            if not self.db_path.is_file():
                raise ValueError('项目数据库暂时无法读取。')
            with _LOCK:
                ensure_workspace(root)
                self._path(root, '.paw').mkdir(parents=True, exist_ok=True, mode=0o700)
                setup_warnings = []
                for relative in ('docs', 'records/materials', 'records/artifacts', 'records/experiments', 'records/runtime', 'outputs'):
                    try:
                        self._path(root, relative).mkdir(parents=True, exist_ok=True, mode=0o700)
                    except (OSError, ValueError):
                        setup_warnings.append(f'目录暂不可用：{relative}；保留原内容。')
                lock_path = self._path(root, '.paw/project-directory.lock')
                with os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600), 'a') as lock:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                    # Reread committed state under the per-directory OS lock;
                    # an older command response cannot project over newer data.
                    snapshot = self._snapshot(str(project['projectId']))
                    if managed_workspace(self.db_path, self.scope_id, snapshot['project'])['path'] != str(root):
                        raise ValueError('项目目录绑定已更新，请重新读取。')
                    response = self._reconcile(root, snapshot)
                    if setup_warnings:
                        response['warnings'].extend(setup_warnings)
                        response['status'] = 'partial'
        except (OSError, ValueError, sqlite3.Error):
            response['warnings'].append('项目文件暂时未能完整生成；数据库结果仍保留，重新打开可重试，已有文件不会被清理。')
        return response

    def _reconcile(self, root: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
        manifest_path = self._path(root, '.paw/project-projection.json')
        warnings = []
        try:
            previous = json.loads(manifest_path.read_text()) if manifest_path.is_file() else {}
            if not isinstance(previous, dict) or previous.get('schemaVersion') != _SCHEMA:
                previous = {}
        except (OSError, ValueError):
            previous = {}
            warnings.append('生成清单需要重建；无法核对的已有内容予以保留。')
        old_files = previous.get('files', {})
        old_files = {path: {'sha256': str(value.get('sha256', '')), 'title': str(value.get('title', path)),
                            'kind': str(value.get('kind', 'artifact'))}
                     for path, value in old_files.items() if isinstance(value, dict)} if isinstance(old_files, dict) else {}
        files = self._files(snapshot, root)
        source_hash = _hash(_json(snapshot))
        generated = {}
        total = 0
        for relative, (title, kind, content) in files.items():
            total += len(content)
            if len(generated) >= _MAX_FILES or total > _MAX_BYTES:
                warnings.append('本次目录投影达到文件/体积上限；原始版本仍在项目数据库中。')
                break
            expected = _hash(content)
            old = old_files.get(relative, {})
            try:
                path = self._path(root, relative)
                if path.exists() and not path.is_file():
                    raise ValueError('已有路径不是普通文件，保留原内容。')
                exists = path.is_file()
                actual = _hash(path.read_bytes()) if exists else ''
                if relative == 'AGENTS.md' and exists:
                    # AGENTS is a scaffold, intentionally human-maintained.
                    generated[relative] = {'sha256': old.get('sha256', actual), 'title': title, 'kind': kind}
                    continue
                if exists and actual != expected and actual != old.get('sha256'):
                    warnings.append(f'保留手工修改：{relative}；请通过项目保存操作接入该修改。')
                    generated[relative] = old or {'sha256': '', 'title': title, 'kind': kind}
                    continue
                if actual != expected:
                    self._atomic_write(path, content)
                generated[relative] = {'sha256': expected, 'title': title, 'kind': kind}
            except (OSError, ValueError):
                warnings.append(f'暂未生成：{relative}；原数据库版本仍保留。')
                if old:
                    generated[relative] = old
        now = int(time.time() * 1000)
        manifest = {'schemaVersion': _SCHEMA, 'projectId': snapshot['project']['projectId'],
            'sourceRevision': snapshot['project']['revision'], 'sourceHash': source_hash,
            'generatedAtMs': previous.get('generatedAtMs', now) if previous.get('sourceHash') == source_hash and previous.get('files') == generated else now,
            'files': generated}
        try:
            encoded = _json(manifest)
            if not manifest_path.is_file() or manifest_path.read_bytes() != encoded:
                self._atomic_write(manifest_path, encoded)
        except OSError:
            warnings.append('生成清单尚未写入；下次读取会核对已有文件并继续。')
        return {'path': str(root), 'status': 'partial' if warnings else 'ready',
            'sourceRevision': manifest['sourceRevision'], 'generatedAtMs': manifest['generatedAtMs'],
            'files': [{'path': str(root / path), 'title': value['title'], 'kind': value['kind']} for path, value in generated.items()],
            'warnings': warnings}
