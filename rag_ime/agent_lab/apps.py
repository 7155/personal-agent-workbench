"""Portable application versions, activation and actual Pi invocation receipts."""
from __future__ import annotations

import base64
import copy
import hashlib
import importlib.util
import json
import re
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Mapping

from .app_runtime import AppInputError, advance_progress, build_prompt, provided_context, validate_action, validate_model, agent_ui_html
from .app_sources import export_zip, freeze_source
from .projects import (AgentLabProjectConflict, AgentLabProjectNotFound,
                                 AgentLabProjectValidationError, _integer, _json, _now, _object, _text)
from ..db import apply_database_migrations, sqlite_connection


class AgentLabAppStore:
    def __init__(self, db_path: str | Path, *, scope_id: str = 'local', freeze_knowledge: Callable | None = None) -> None:
        self.db_path = Path(db_path)
        self.scope_id = scope_id
        self.freeze_knowledge = freeze_knowledge
        self._lock = threading.RLock()
        self._initialized = False

    def initialize(self) -> None:
        with self._lock:
            if self._initialized: return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with sqlite_connection(self.db_path) as conn: apply_database_migrations(conn)
            self._initialized = True

    def _app(self, conn: sqlite3.Connection, app_id: str) -> dict:
        row = conn.execute('SELECT * FROM agent_lab_apps WHERE app_id=? AND scope_id=?', (app_id,self.scope_id)).fetchone()
        if row is None: raise AgentLabProjectNotFound('应用不存在，或不属于当前工作空间。')
        return {**json.loads(row['payload_json']), 'revision':row['revision'], 'latestVersion':row['latest_version'],
                'activeVersion':row['active_version'], 'updatedAtMs':row['updated_at_ms']}

    @staticmethod
    def _version(conn: sqlite3.Connection, app_id: str, version: int) -> dict:
        row = conn.execute('SELECT payload_json FROM agent_lab_app_versions WHERE app_id=? AND version=?', (app_id,version)).fetchone()
        if row is None: raise AgentLabProjectNotFound('此应用版本不存在。')
        return json.loads(row[0])

    @staticmethod
    def _public_version(version: dict, *, include_html: bool = True) -> dict:
        result = {key:value for key,value in version.items() if key not in {'files','uiFiles','bootstrapFiles','resourceFiles','resourceManifest','assetKey','sourceDirectory','runtimeSource','exports'}}
        files = {**version['files'], **version.get('resourceFiles',{}), **version.get('uiFiles',{}), **version.get('bootstrapFiles',{})}
        result['fileCount'] = len(files)
        result['byteSize'] = sum(len(value.encode()) for value in files.values())
        if include_html: result['html'] = agent_ui_html(version.get('uiFiles', {}))+version['files'][version['spec']['html']]
        result['sourceFiles'] = [{'path':path,'byteSize':len(value.encode()),'sha256':hashlib.sha256(value.encode()).hexdigest()} for path,value in files.items()]
        result['sourceFiles'].extend(version.get('resourceManifest', []))
        result['fileCount'] = len(result['sourceFiles'])
        result['byteSize'] = sum(item['byteSize'] for item in result['sourceFiles'])
        return result

    def prepare(self, conn: sqlite3.Connection, project: dict, value: dict, default_model: dict[str,str]) -> dict:
        value = _object(value, {'directory','appId','evaluationSelection'}, '应用准备')
        if 'evaluationSelection' in value and not isinstance(value['evaluationSelection'], dict):
            raise AgentLabProjectValidationError('应用评测选择需要套件、实验和基线或候选标识。')
        directory = _text(value.get('directory'), '应用目录', 500)
        workspace = project.get('executionWorkspace') or {}
        if workspace.get('kind') != 'managed' or not workspace.get('path'):
            raise AgentLabProjectValidationError('此项目没有可用的托管执行目录。')
        try: frozen = freeze_source(Path(workspace['path']), directory, default_model,
                                   freeze_knowledge=(lambda value:self.freeze_knowledge(project['projectId'],value)) if self.freeze_knowledge else None,
                                   select_evaluation=lambda value: self._evaluation_selection(project, value, connection=conn),
                                   evaluation_selection=value.get('evaluationSelection'))
        except ValueError as exc:
            if isinstance(exc, AgentLabProjectValidationError): raise
            raise AgentLabProjectValidationError(str(exc)) from exc
        app_id = _text(value.get('appId',''), '应用标识', optional=True)
        previous = self._app(conn, app_id) if app_id else None
        if previous and previous['projectId'] != project['projectId']:
            raise AgentLabProjectValidationError('应用不属于当前项目。')
        if previous:
            latest = self._version(conn, app_id, previous['latestVersion'])
            if latest['contentHash'] == frozen['contentHash']:
                return {**previous,'version':self._public_version(latest)}
        now = _now(); app_id = app_id or f'extension:lab-{uuid.uuid4().hex}'
        version_number = previous['latestVersion'] + 1 if previous else 1
        version = {**frozen, 'appId':app_id, 'version':version_number,'projectRevision':project['revision'],
                   'materialSetId':project['materialSetId'],'createdAtMs':now}
        # Freeze the entire distributable once. Later product runner, manifest,
        # or README changes cannot silently rewrite an accepted App version.
        version['exports'] = {}
        if version.get('resourceFiles'):
            from .app_assets import freeze_assets
            freeze_assets(self.db_path, version, export_zip)
        else:
            for target in ('paw','standalone'):
                filename,archive = export_zip(version,target)
                version['exports'][target] = {'filename':filename,'mimeType':'application/zip',
                    'base64':base64.b64encode(archive).decode(),'byteSize':len(archive),'sha256':hashlib.sha256(archive).hexdigest()}
        app = {'appId':app_id,'projectId':project['projectId'],'title':frozen['spec']['title'],
               'description':frozen['spec']['description'],'revision':previous['revision']+1 if previous else 1,
               'latestVersion':version_number,'activeVersion':previous['activeVersion'] if previous else None,
               'createdAtMs':previous['createdAtMs'] if previous else now,'updatedAtMs':now}
        conn.execute('INSERT INTO agent_lab_apps(app_id,project_id,scope_id,revision,latest_version,active_version,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?,?,?,?) '
                     'ON CONFLICT(app_id) DO UPDATE SET revision=excluded.revision,latest_version=excluded.latest_version,payload_json=excluded.payload_json,updated_at_ms=excluded.updated_at_ms',
                     (app_id,project['projectId'],self.scope_id,app['revision'],version_number,app['activeVersion'],_json(app),app['createdAtMs'],now))
        conn.execute('INSERT INTO agent_lab_app_versions(app_id,version,payload_json,created_at_ms) VALUES(?,?,?,?)', (app_id,version_number,_json(version),now))
        return {**app,'version':self._public_version(version)}

    def _evaluation_selection(self, project: dict, value: Any, *, connection: sqlite3.Connection) -> dict:
        """Select one completed owner receipt, never an Agent-written claim."""
        from .golden import AgentLabGoldenStore
        value = _object(value, {'suiteId', 'jobId', 'variant'}, '应用评测选择')
        suite_id = _text(value.get('suiteId'), '评测集标识', 240)
        job_id = _text(value.get('jobId'), '实验标识', 240)
        variant = value.get('variant')
        if variant not in {'baseline', 'candidate'}:
            raise AgentLabProjectValidationError('请选择基线或候选的已完成配置。')
        if not any(binding.get('ownerRef') == {'kind': 'golden_suite', 'id': suite_id}
                   for binding in project.get('bindings', [])):
            raise AgentLabProjectValidationError('所选实验不属于此项目的评测绑定。')
        selected = AgentLabGoldenStore(self.db_path).application_configuration(suite_id, job_id, variant, connection=connection)
        knowledge = selected.get('knowledge')
        if knowledge and knowledge.get('projectId') != project['projectId']:
            raise AgentLabProjectValidationError('所选实验的知识库不属于此项目。')
        return selected

    def read(self, payload: Mapping[str, Any] | None = None) -> dict:
        value = _object(payload or {}, {'appId','projectId','version','callId'}, '应用读取')
        self.initialize()
        app_id = _text(value.get('appId',''), '应用标识', optional=True)
        project_id = _text(value.get('projectId',''), '项目标识', optional=True)
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            conn.execute('BEGIN')
            rows = conn.execute('SELECT app_id FROM agent_lab_apps WHERE scope_id=? AND (?="" OR project_id=?) ORDER BY updated_at_ms DESC', (self.scope_id,project_id,project_id))
            items = [self._app(conn,row[0]) for row in rows]
            for item in items:
                installed = item['activeVersion']
                if installed is not None:
                    item['installation'] = self.manifest(item,self._version(conn,item['appId'],installed))
            app = self._app(conn,app_id) if app_id else None
            if app and project_id and app['projectId'] != project_id: raise AgentLabProjectNotFound('应用不属于当前项目。')
            result = {'ok':True,'items':items,'app':app}
            if app:
                selected = value.get('version') or app['latestVersion']
                if isinstance(selected,str) and selected.isdecimal(): selected = int(selected)
                selected = _integer(selected,1)
                result['version'] = self._public_version(self._version(conn,app_id,selected))
                result['versions'] = [self._public_version(json.loads(row[0]),include_html=False) for row in conn.execute('SELECT payload_json FROM agent_lab_app_versions WHERE app_id=? ORDER BY version DESC',(app_id,))]
                result['calls'] = [self._call(row) for row in conn.execute(
                    "SELECT * FROM agent_lab_app_calls WHERE app_id=? AND (state IN ('queued','running') OR call_id IN "
                    '(SELECT call_id FROM agent_lab_app_calls WHERE app_id=? ORDER BY created_at_ms DESC LIMIT 30)) ORDER BY created_at_ms DESC',
                    (app_id,app_id))]
            if value.get('callId'):
                if not app: raise AgentLabProjectValidationError('读取调用需要指定应用。')
                row = conn.execute('SELECT * FROM agent_lab_app_calls WHERE app_id=? AND call_id=?',(app_id,_text(value['callId'],'调用标识'))).fetchone()
                if row is None: raise AgentLabProjectNotFound('应用调用不存在。')
                result['call'] = self._call(row)
            return result

    @staticmethod
    def manifest(app: dict, version: dict) -> dict:
        # Generic desktop identity; it is a Lab-hosted application, not a claim
        # that a compiled native Pi Package has been installed.
        spec = version['spec']; slug = app['appId'].removeprefix('extension:')
        appearance = spec.get('appearance', {'accent':'green','icon':{'symbol':'assistant','background':'#22876A'}})
        return {'schemaVersion':'pawos.lab-app.v1','id':app['appId'],'version':f"0.{version['version']}.0",
                'label':spec['title'],'shortLabel':spec['title'][:12],'tagline':spec['description'],
                'route':f'/extensions/{slug}','presentation':'workspace','accent':appearance['accent'],
                'icon':appearance['icon'],'packageId':f'lab-app-{slug}',
                'bindingSha256':version['contentHash'],'skillRef':spec['skill'],
                'skillSha256':hashlib.sha256(version['files'][spec['skill']].encode()).hexdigest(),
                'verticalSuiteId':app['projectId'],'verticalSuiteRevision':str(version['projectRevision']),
                'hosting':{'kind':'lab-html','appId':app['appId'],'projectId':app['projectId'],'version':version['version']}}

    def project_calls(self, project_id: str, *, limit: int = 200) -> dict:
        """Small read projection; never hydrate versions, answers or journals.

        The selected project owner has initialized the schema. Do not migrate,
        recover calls or construct an execution worker from this polling path.
        Counts cover the whole project; displayed history is explicitly bounded.
        """
        project_id = _text(project_id, '项目标识', 500)
        limit = _integer(limit, 1, 1000)
        joins = ' FROM agent_lab_app_calls c JOIN agent_lab_apps a ON a.app_id=c.app_id '
        scope = ' WHERE a.scope_id=? AND a.project_id=? '
        with sqlite_connection(self.db_path, row_factory=sqlite3.Row) as conn:
            conn.execute('BEGIN')
            counts = {row['state']: row['count'] for row in conn.execute(
                'SELECT c.state,COUNT(*) AS count' + joins + scope + 'GROUP BY c.state', (self.scope_id, project_id))}
            rows = conn.execute("SELECT c.call_id,c.app_id,c.app_version,c.action_id,c.state,c.session_id,c.updated_at_ms,"
                "substr(json_extract(v.payload_json,'$.spec.title'),1,240) AS title,"
                "(SELECT substr(json_extract(value,'$.title'),1,120) FROM json_each(v.payload_json,'$.spec.actions') "
                "WHERE json_extract(value,'$.id')=c.action_id LIMIT 1) AS action_title,"
                "json_extract(c.progress_json,'$.stage') AS stage,"
                "json_extract(c.progress_json,'$.runtime.operation') AS operation,"
                "json_extract(c.progress_json,'$.research.budget.executedToolCalls') AS tools,"
                "json_extract(c.progress_json,'$.research.budget.executedSearchCalls') AS searches,"
                "json_extract(c.progress_json,'$.research.budget.executedSourceReadCalls') AS reads,"
                "json_extract(c.progress_json,'$.research.budget.contextChars') AS chars,"
                "d.call_id AS reuse_call,d.app_version AS reuse_version,"
                "json_extract(c.progress_json,'$.research.evidenceReuse.sourceCount') AS reuse_sources,"
                "json_extract(c.progress_json,'$.research.evidenceReuse.windowCount') AS reuse_windows,"
                "json_extract(c.progress_json,'$.research.evidenceReuse.contextChars') AS reuse_chars"
                + joins + 'JOIN agent_lab_app_versions v ON v.app_id=c.app_id AND v.version=c.app_version'
                + " LEFT JOIN agent_lab_app_calls d ON d.call_id=json_extract(c.progress_json,'$.research.evidenceReuse.sourceCallId') "
                "AND d.app_id=c.app_id AND d.state='completed' AND d.cancel_requested=0 "
                "AND json_extract(c.progress_json,'$.research.evidenceReuse.schemaVersion')='paw.app-evidence-reuse.v1' "
                "AND json_extract(c.progress_json,'$.research.evidenceReuse.snapshotSha256')=json_extract(v.payload_json,'$.spec.knowledge.snapshotSha256') "
                + scope + "ORDER BY CASE c.state WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,c.updated_at_ms DESC,c.call_id LIMIT ?",
                (self.scope_id, project_id, limit)).fetchall()
        calls = []
        stages = {'queued','context_ready','retrieving','researching','sources_ready','model_starting','model_wait',
                  'thinking','answering','completed','failed','cancelled','interrupted','unconfirmed'}
        for row in rows:
            progress = {'stage': row['stage']} if row['stage'] in stages else {}
            if row['operation'] in {'discover','find','open','search'}:
                progress['operation'] = row['operation']
            for column, name in [('tools','executedToolCalls'), ('searches','executedSearchCalls'),
                                 ('reads','executedSourceReadCalls'), ('chars','contextChars')]:
                if type(row[column]) is int and row[column] >= 0:
                    progress[name] = row[column]
            calls.append({'callId': row['call_id'], 'appId': row['app_id'], 'version': row['app_version'],
                          'title': row['title'] or '', 'actionId': row['action_id'], 'actionTitle': row['action_title'] or row['action_id'],
                          'state': row['state'], 'sessionId': row['session_id'], 'updatedAtMs': row['updated_at_ms'], 'progress': progress})
            if row['reuse_call'] and all(type(row[key]) is int and row[key] >= 0 for key in ('reuse_sources', 'reuse_windows', 'reuse_chars')):
                calls[-1]['evidenceReuse'] = {'sourceCallId': row['reuse_call'], 'sourceAppVersion': row['reuse_version'],
                    'sourceCount': row['reuse_sources'], 'windowCount': row['reuse_windows'], 'contextChars': row['reuse_chars']}
        total = sum(counts.values())
        return {'projectId': project_id, 'calls': calls, 'counts': counts, 'totalCount': total,
                'returnedCount': len(calls), 'truncated': len(calls) < total}

    def download(self, payload: Mapping[str, Any]) -> dict:
        value = _object(payload, {'appId','version','target'}, '应用导出')
        app_id = _text(value.get('appId'),'应用标识'); selected = value.get('version')
        if isinstance(selected,str) and selected.isdecimal(): selected = int(selected)
        selected = _integer(selected,1); target = _text(value.get('target'),'导出目标')
        self.initialize()
        with sqlite_connection(self.db_path,row_factory=sqlite3.Row) as conn:
            self._app(conn,app_id)
            version = self._version(conn,app_id,selected)
        if target not in {'paw','standalone'}: raise AgentLabProjectValidationError('不支持此导出目标。')
        exported = version['exports'][target]
        if version.get('assetKey'):
            from .app_assets import download_asset
            try: exported = download_asset(self.db_path, version, target)
            except (OSError, ValueError) as exc: raise AgentLabProjectValidationError(str(exc)) from exc
        return {'ok':True,**exported,'contentHash':version['contentHash'],
                'target':target,'appId':app_id,'version':selected}

    def command(self, payload: Mapping[str, Any]) -> dict:
        value = _object(payload, {'action','appId','expectedRevision','clientRequestId','input'}, '应用命令')
        action = _text(value.get('action'),'应用操作')
        if action not in {'activate','deactivate','invoke','cancel','resume'}: raise AgentLabProjectValidationError('应用操作尚不支持。')
        app_id = _text(value.get('appId'),'应用标识'); client_id = _text(value.get('clientRequestId'),'请求标识')
        expected = _integer(value.get('expectedRevision'),1); body = value.get('input')
        if not isinstance(body,dict): raise AgentLabProjectValidationError('应用操作内容需要对象。')
        request = _json({key:item for key,item in value.items() if key != 'clientRequestId'})
        self.initialize()
        with sqlite_connection(self.db_path,row_factory=sqlite3.Row,foreign_keys=True) as conn:
            conn.execute('BEGIN IMMEDIATE')
            receipt = conn.execute('SELECT request_json,response_json FROM agent_lab_app_commands WHERE scope_id=? AND client_request_id=?',(self.scope_id,client_id)).fetchone()
            if receipt:
                if receipt[0] != request: raise AgentLabProjectConflict('此应用请求标识已绑定其他内容。')
                return {**json.loads(receipt[1]),'replayed':True}
            app = self._app(conn,app_id)
            if app['revision'] != expected: raise AgentLabProjectConflict('应用已经更新，请保留输入并重新读取。')
            result = {'ok':True,'clientRequestId':client_id,'replayed':False}
            if action in {'activate','deactivate'}:
                _object(body, {'version'} if action == 'activate' else set(), '应用启用')
                selected = _integer(body.get('version'),1) if action == 'activate' else None
                if selected is not None: self._version(conn,app_id,selected)
                app.update(activeVersion=selected,revision=app['revision']+1,updatedAtMs=_now())
                conn.execute('UPDATE agent_lab_apps SET active_version=?,revision=?,updated_at_ms=? WHERE app_id=?',(selected,app['revision'],app['updatedAtMs'],app_id))
            elif action == 'invoke':
                _object(body,{'version','actionId','values','model'},'应用输入')
                selected = _integer(body.get('version'),1); version = self._version(conn,app_id,selected)
                action_id = _text(body.get('actionId'),'应用操作')
                try:
                    validate_action(version['spec'],action_id,body.get('values'))
                    model = validate_model(body.get('model',version['spec']['model']))
                except AppInputError as exc: raise AgentLabProjectValidationError(str(exc)) from exc
                self._ensure_capacity(conn,app_id)
                call_id = f'lab-app-call-{uuid.uuid4().hex}'; now = _now()
                conn.execute('INSERT INTO agent_lab_app_calls(call_id,app_id,app_version,action_id,input_json,state,created_at_ms,updated_at_ms,model_json) VALUES(?,?,?,?,?,?,?,?,?)',
                             (call_id,app_id,selected,action_id,_json(body['values']),'queued',now,now,_json(model)))
                result['call'] = self._call(conn.execute('SELECT * FROM agent_lab_app_calls WHERE call_id=?',(call_id,)).fetchone())
            else:
                _object(body,{'callId'},'调用控制')
                row = conn.execute('SELECT * FROM agent_lab_app_calls WHERE app_id=? AND call_id=?',(app_id,_text(body.get('callId'),'调用标识'))).fetchone()
                if row is None: raise AgentLabProjectNotFound('应用调用不存在。')
                if action == 'cancel' and row['state'] in {'queued','running','interrupted'}:
                    state = 'cancelled' if row['state'] == 'queued' and not row['session_id'] else row['state']
                    conn.execute('UPDATE agent_lab_app_calls SET cancel_requested=1,state=?,updated_at_ms=? WHERE call_id=?',(state,_now(),row['call_id']))
                elif action == 'resume':
                    if row['state'] != 'interrupted' or row['cancel_requested']: raise AgentLabProjectValidationError('此调用不需要恢复，或已经请求取消。')
                    self._ensure_capacity(conn,app_id)
                    conn.execute('UPDATE agent_lab_app_calls SET state="queued",error="",updated_at_ms=? WHERE call_id=?',(_now(),row['call_id']))
                result['call'] = self._call(conn.execute('SELECT * FROM agent_lab_app_calls WHERE call_id=?',(row['call_id'],)).fetchone())
            result['app'] = app
            conn.execute('INSERT INTO agent_lab_app_commands(scope_id,client_request_id,request_json,response_json,created_at_ms) VALUES(?,?,?,?,?)',(self.scope_id,client_id,request,_json(result),_now()))
            return result

    @staticmethod
    def _ensure_capacity(conn: sqlite3.Connection, app_id: str) -> None:
        active_count = conn.execute("SELECT COUNT(*) FROM agent_lab_app_calls WHERE app_id=? AND state IN ('queued','running')",(app_id,)).fetchone()[0]
        if active_count >= 4: raise AgentLabProjectConflict('此应用已有 4 项处理中调用，请等待完成或停止后再试。')

    @staticmethod
    def _call(row: sqlite3.Row) -> dict:
        return {'callId':row['call_id'],'appId':row['app_id'],'version':row['app_version'],'actionId':row['action_id'],
                'input':json.loads(row['input_json']),'state':row['state'],'sessionId':row['session_id'],
                'model':json.loads(row['model_json']),
                'result':json.loads(row['result_json']),'error':row['error'],'cancelRequested':bool(row['cancel_requested']),
                'progress':{**json.loads(row['progress_json']), **({'stage':row['state']} if row['state'] not in {'queued','running'} else {})},
                'createdAtMs':row['created_at_ms'],'updatedAtMs':row['updated_at_ms']}

    def call_input(self, call_id: str) -> tuple[dict,dict]:
        self.initialize()
        with sqlite_connection(self.db_path,row_factory=sqlite3.Row) as conn:
            row = conn.execute('SELECT c.* FROM agent_lab_app_calls c JOIN agent_lab_apps a ON a.app_id=c.app_id WHERE call_id=? AND a.scope_id=?',(call_id,self.scope_id)).fetchone()
            if row is None: raise AgentLabProjectNotFound('应用调用不存在。')
            return self._call(row),self._version(conn,row['app_id'],row['app_version'])

    def update_call(self, call_id: str, **fields: Any) -> dict:
        if set(fields) - {'state','session_id','result_json','error'}: raise ValueError('invalid call fields')
        with sqlite_connection(self.db_path,row_factory=sqlite3.Row) as conn:
            conn.execute('BEGIN IMMEDIATE')
            if fields.get('state') in {'completed','failed','cancelled','interrupted'}:
                row = conn.execute('SELECT progress_json FROM agent_lab_app_calls WHERE call_id=?',(call_id,)).fetchone()
                fields['progress_json'] = _json(advance_progress(json.loads(row[0]),{'stage':fields['state']}))
            conn.execute(f"UPDATE agent_lab_app_calls SET {','.join(f'{key}=?' for key in fields)},updated_at_ms=? WHERE call_id=? AND state NOT IN ('completed','failed','cancelled')",
                         (*fields.values(),_now(),call_id))
            return self._call(conn.execute('SELECT * FROM agent_lab_app_calls WHERE call_id=?',(call_id,)).fetchone())

    def update_progress(self, call_id: str, update: dict) -> None:
        with sqlite_connection(self.db_path) as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT progress_json FROM agent_lab_app_calls WHERE call_id=? AND state IN ('queued','running')",(call_id,)).fetchone()
            if row is not None:
                progress = advance_progress(json.loads(row[0]),update)
                conn.execute('UPDATE agent_lab_app_calls SET progress_json=?,updated_at_ms=? WHERE call_id=?',
                             (_json(progress),progress['updatedAtMs'],call_id))


class AgentLabAppApplication:
    def __init__(self, store: AgentLabAppStore, *, complete: Callable[...,dict], abort: Callable[[str],Any], start_workers: bool = True, recover: bool = True) -> None:
        self.store,self.complete,self.abort = store,complete,abort
        self._pool = ThreadPoolExecutor(max_workers=2,thread_name_prefix='paw-lab-app') if start_workers else None
        self._lock = threading.Lock(); self._active: set[str] = set(); self._closed = False
        self._knowledge_runtimes: dict[str, tuple[Path, Any]] = {}
        self._research_locks: dict[str, threading.RLock] = {}
        store.initialize()
        if recover:
            with sqlite_connection(store.db_path) as conn:
                conn.execute("UPDATE agent_lab_app_calls SET state='interrupted',error='执行进程已离线，请恢复原调用。',updated_at_ms=? WHERE state IN ('queued','running') AND app_id IN (SELECT app_id FROM agent_lab_apps WHERE scope_id=?)",(_now(),store.scope_id))

    def command(self, payload: Mapping[str,Any]) -> dict:
        result = self.store.command(payload); call = result.get('call')
        if call and payload['action'] == 'cancel':
            # A replay reconciles the original cancellation. A process restart
            # can leave no worker to consume cancel_requested on its own.
            current,_ = self.store.call_input(call['callId'])
            if current['state'] not in {'completed','failed','cancelled'}:
                if current['sessionId']:
                    try:
                        self.abort(current['sessionId'])
                        self.store.update_call(current['callId'],state='cancelled',error='应用调用已停止。')
                    except Exception:
                        self.store.update_call(current['callId'],state='interrupted',error='停止尚未确认，请重试停止；原调用已保留。')
                else:
                    self.store.update_call(current['callId'],state='cancelled',error='应用调用已停止。')
        elif call and call['state'] == 'queued' and self._pool:
            with self._lock:
                if self._closed or call['callId'] in self._active: return result
                self._active.add(call['callId'])
            self._pool.submit(self.run_call,call['callId'])
        return result

    def session_identity(self, request_id: str) -> dict:
        call,version = self.store.call_input(request_id)
        return {'title':version['spec']['title'],'owner_app_id':call['appId'],'surface_key':f"application.{call['callId']}",
                'allowed_tools': ['lab_research'] if version['spec'].get('workflow') else []}

    def _research_seed(self, call_id: str, version: dict):
        call, _ = self.store.call_input(call_id)
        field = version['spec']['workflow'].get('historyField', 'conversation')
        raw = call['input'].get(field)
        if not raw:
            return None, None
        try:
            history = json.loads(raw) if isinstance(raw, str) else raw
        except (ValueError, TypeError):
            return None, None
        if not isinstance(history, list) or not history or not isinstance(history[-1], dict):
            return None, None
        reference = history[-1].get('callId')
        if reference is None:
            return None, None
        if not isinstance(reference, str) or not re.fullmatch(r'lab-app-call-[a-f0-9]{32}', reference) or reference == call_id:
            raise AppInputError('追问证据需要有效的原 App 调用标识。')
        donor, donor_version = self.store.call_input(reference)
        if (donor['appId'] != call['appId'] or donor['state'] != 'completed' or donor['cancelRequested']
                or donor['updatedAtMs'] > call['createdAtMs']
                or donor_version['spec'].get('knowledge', {}).get('snapshotSha256') != version['spec']['knowledge']['snapshotSha256']):
            raise AppInputError('追问只能复用同一 App、同一知识快照的先前已完成调用。')
        query_field = donor_version['spec']['knowledge'].get('queryField', 'question')
        if history[-1].get('question') != donor['input'].get(query_field):
            raise AppInputError('追问历史问题与原调用不一致。')
        sources = donor.get('result', {}).get('sources')
        if not isinstance(sources, list):
            raise AppInputError('原调用没有可核对的原文窗口。')
        return copy.deepcopy(sources), {'sourceCallId': reference, 'sourceAppVersion': donor['version']}

    def _research_reader(self, call_id: str, version: dict):
        root, runtime = self._knowledge_runtime(version)
        path = root / 'app_research.py'
        runtime_path = root / 'knowledge_runtime.py'
        if version.get('assetKey'):
            record = next((row for row in version['resourceManifest'] if row['path'] == 'app_research.py'), None)
            expected = record['sha256'] if record else ''
            runtime_record = next((row for row in version['resourceManifest'] if row['path'] == 'knowledge_runtime.py'), None)
            runtime_expected = runtime_record['sha256'] if runtime_record else ''
        else:
            expected = hashlib.sha256(version.get('resourceFiles', {}).get('app_research.py', '').encode()).hexdigest()
            runtime_expected = hashlib.sha256(version.get('resourceFiles', {}).get('knowledge_runtime.py', '').encode()).hexdigest()
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise AppInputError('冻结的研究读取组件不存在或已变化。')
        if runtime_path.is_symlink() or not runtime_path.is_file() or hashlib.sha256(runtime_path.read_bytes()).hexdigest() != runtime_expected:
            raise AppInputError('冻结的知识读取组件不存在或已变化。')
        spec = importlib.util.spec_from_file_location('paw_app_research_' + version['contentHash'], path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sources, context = self._research_seed(call_id, version)
        seed = {}
        if sources is not None:
            if getattr(module, 'EVIDENCE_REUSE_PROTOCOL', None) != 'paw.app-evidence-reuse.v1':
                raise AppInputError('此冻结研究版本尚不支持原调用证据复用，请使用更新的 App 版本。')
            seed = {'seed_sources': sources, 'seed_context': context}
        return module.FrozenResearchReader(root, version['spec']['knowledge'], version['spec']['workflow'],
                                          call_id=call_id, search=runtime.retrieve, **seed)

    def research_tool(self, session: Mapping[str, Any], operation: str, args: Mapping[str, Any]) -> dict:
        """One native Pi tool execution, bound to the original App/model call.

        The App journal CAS commits before the result is returned to Pi. An
        uncertain delivery can replay the same toolCallId without another read.
        No model loop or alternate Session is started here.
        """
        session_id = session.get('id')
        tool_call_id = args.get('_toolCallId')
        if not isinstance(session_id, str) or not session_id or args.get('_sessionId') != session_id or not isinstance(tool_call_id, str) or not tool_call_id:
            raise AppInputError('研究工具缺少 Gateway 注入的原 Session/Tool 身份。')
        if self._closed:
            raise AppInputError('研究执行 owner 已停止。')
        with sqlite_connection(self.store.db_path, row_factory=sqlite3.Row) as conn:
            rows = conn.execute('SELECT m.request_id FROM agent_lab_golden_model_calls m '
                'JOIN agent_lab_app_calls c ON c.call_id=m.request_id '
                'JOIN agent_lab_apps a ON a.app_id=c.app_id '
                'WHERE m.session_id=? AND c.session_id=? AND a.scope_id=?',
                (session_id, session_id, self.store.scope_id)).fetchall()
        if len(rows) != 1:
            raise AppInputError('此 Session 没有唯一的冻结 App 研究调用。')
        call_id = rows[0]['request_id']
        with self._lock:
            lock = self._research_locks.setdefault(call_id, threading.RLock())
        with lock:
            call, version = self.store.call_input(call_id)
            if call['state'] != 'running' or call['cancelRequested'] or call['sessionId'] != session_id or not version['spec'].get('workflow'):
                raise AppInputError('此 App 调用未在执行研究，或已经停止。')
            reader = self._research_reader(call_id, version)
            journal = call.get('progress', {}).get('research', {}).get('journal', [])
            values = {key: value for key, value in args.items() if key not in {'op', 'operation', '_sessionId', '_toolCallId'}}
            if args.get('op', operation) != operation or args.get('operation', operation) != operation:
                raise AppInputError('研究操作与 Gateway 路由不一致。')
            transition = reader.execute({'op': operation, **values}, tool_call_id=tool_call_id, journal=journal)
            next_journal = [*journal, transition['journalEntry']] if transition['journalEntry'] else journal
            summary = reader.summarize(next_journal)
            with sqlite_connection(self.store.db_path, row_factory=sqlite3.Row) as conn:
                conn.execute('BEGIN IMMEDIATE')
                row = conn.execute('SELECT * FROM agent_lab_app_calls WHERE call_id=?', (call_id,)).fetchone()
                if row is None or row['state'] != 'running' or row['cancel_requested'] or row['session_id'] != session_id:
                    raise AppInputError('研究调用已停止，未交付新的证据。')
                current = json.loads(row['progress_json'])
                entries = current.get('research', {}).get('journal', [])
                head = entries[-1]['entryHash'] if entries else ''
                if head != transition['expectedJournalHead']:
                    raise AgentLabProjectConflict('研究回执已更新，请用原 Tool 身份重试。')
                if transition['journalEntry']:
                    progress = advance_progress(current, {'stage': 'researching', 'research': {**summary, 'journal': next_journal},
                        'sources': summary['sources'], 'knowledge': self._research_knowledge(version, summary)})
                    conn.execute('UPDATE agent_lab_app_calls SET progress_json=?,updated_at_ms=? WHERE call_id=?',
                                 (_json(progress), progress['updatedAtMs'], call_id))
            return transition['result']

    @staticmethod
    def _research_knowledge(version: dict, summary: dict) -> dict:
        knowledge = version['spec']['knowledge']
        return {'snapshotSha256': knowledge['snapshotSha256'], 'indexId': knowledge['sourceIndexId'],
                'profile': knowledge['profile'], 'documentCount': knowledge['documentCount'],
                'chunkCount': knowledge['chunkCount'], 'contextChars': summary['contextChars'],
                **({'evidenceReuse': copy.deepcopy(summary['evidenceReuse'])} if summary.get('evidenceReuse') else {}),
                'workflow': {key: value for key, value in summary.items() if key != 'sources'}}

    def run_call(self, call_id: str) -> None:
        try:
            call,version = self.store.call_input(call_id)
            if call['state'] != 'queued' or call['cancelRequested']: return
            self.store.update_call(call_id,state='running')
            def cancelled():
                return self._closed or self.store.call_input(call_id)[0]['cancelRequested']
            action, values = validate_action(version['spec'],call['actionId'],call['input'])
            model = call.get('model') or version['spec']['model']
            def progress(update):
                # Feedback is optional projection. A transient display write
                # must not interrupt Pi or change its settlement authority.
                try: self.store.update_progress(call_id, update)
                except (OSError, ValueError, sqlite3.Error): pass
            evidence = None
            research = bool(version['spec'].get('workflow')) and action.get('kind') != 'retrieval'
            initial_research = None
            if research:
                reader = self._research_reader(call_id, version)
                # Rebuild the same admission prompt on recovery. Current Tool
                # results belong to Pi's durable transcript, not a new prompt.
                initial_research = reader.summarize([])
                journal = call.get('progress', {}).get('research', {}).get('journal', [])
                summary = reader.summarize(journal)
                progress({'research': {**summary, 'journal': journal}, 'sources': summary['sources'],
                          'knowledge': self._research_knowledge(version, summary)})
            context = provided_context(version['spec'], version['files'])
            if context:
                progress({'stage':'context_ready', **context})
            if version['spec'].get('knowledge') and not research:
                progress({'stage':'retrieving','model':model})
                try:
                    resource_root, runtime = self._knowledge_runtime(version)
                    evidence = runtime.retrieve(resource_root, version['spec']['knowledge'], values)
                except (OSError, ValueError, KeyError, sqlite3.Error) as exc:
                    raise AppInputError('知识库检索未完成；尚未调用回答模型。' + str(exc)[:300]) from exc
                progress({'stage':'sources_ready','sources':evidence['sources'],
                          'knowledge':{key:value for key,value in evidence.items() if key != 'sources'}})
            if action.get('kind') == 'retrieval':
                result = runtime.retrieval_result(evidence)
            else:
                progress({'stage':'model_starting','model':model})
                result = self.complete(request_id=call_id,model=model,
                                       prompt=build_prompt(version['spec'],version['files'],call['actionId'],values,
                                                           knowledge_sources=initial_research['sources'] if initial_research else evidence['sources'] if evidence else None,
                                                           research_state=initial_research),
                                       on_session=lambda session_id:self.store.update_call(call_id,session_id=session_id),cancelled=cancelled,
                                       on_progress=progress)
                if research:
                    latest, _ = self.store.call_input(call_id)
                    journal = latest.get('progress', {}).get('research', {}).get('journal', [])
                    summary = self._research_reader(call_id, version).summarize(journal)
                    result.update(sources=summary['sources'], research=summary,
                                  knowledge=self._research_knowledge(version, summary))
                elif evidence:
                    result.update(sources=evidence['sources'],knowledge={key:value for key,value in evidence.items() if key != 'sources'})
                elif context:
                    result.update(context)
            if self._closed: self.store.update_call(call_id,state='interrupted',error='应用执行已中断，请恢复原调用。')
            elif cancelled(): self.store.update_call(call_id,state='cancelled',error='应用调用已停止。')
            else: self.store.update_call(call_id,state='completed',result_json=_json(result),error='')
        except Exception as exc:
            call,_ = self.store.call_input(call_id)
            cancelled = call['cancelRequested']
            state = 'cancelled' if cancelled else 'interrupted' if self._closed or getattr(exc,'interrupted',False) or isinstance(exc,(TimeoutError,ConnectionError,OSError)) else 'failed'
            completion = getattr(exc,'completion',None)
            self.store.update_call(call_id,state=state,error='应用调用已停止。' if cancelled else str(exc)[:500] or '应用调用未完成。',
                                   result_json=_json(completion or {}))
        finally:
            with self._lock: self._active.discard(call_id)

    def _knowledge_runtime(self, version: dict) -> tuple[Path, Any]:
        from .app_knowledge_runtime import materialize
        with self._lock:
            if version['contentHash'] not in self._knowledge_runtimes:
                if version.get('assetKey'):
                    from .app_assets import asset_root
                    root = asset_root(self.store.db_path, version['assetKey'])
                    runtime_record = next(row for row in version['resourceManifest'] if row['path'] == 'knowledge_runtime.py')
                    if hashlib.sha256((root / 'knowledge_runtime.py').read_bytes()).hexdigest() != runtime_record['sha256']:
                        raise ValueError('冻结的知识库应用运行器发生变化。')
                else:
                    root = self.store.db_path.parent / 'lab-app-resources' / version['contentHash']
                    materialize(root, version['resourceFiles'])
                spec = importlib.util.spec_from_file_location('paw_app_resources_' + version['contentHash'],root / 'knowledge_runtime.py')
                runtime = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(runtime)
                if len(self._knowledge_runtimes) >= 4:
                    self._knowledge_runtimes.pop(next(iter(self._knowledge_runtimes)))
                self._knowledge_runtimes[version['contentHash']] = (root,runtime)
            return self._knowledge_runtimes[version['contentHash']]

    def close(self) -> None:
        with self._lock:
            self._closed = True; calls = list(self._active)
        for call_id in calls:
            call,_ = self.store.call_input(call_id)
            self.store.update_call(call_id,state='interrupted',error='应用执行已中断，请恢复原调用。')
            if call['sessionId']:
                try: self.abort(call['sessionId'])
                except Exception: pass
        if self._pool: self._pool.shutdown(wait=False,cancel_futures=True)
