"""App composition for general Lab projects; domain adapters stay optional."""
from __future__ import annotations

import sqlite3
import copy
import json
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .golden import AgentLabGoldenStore
from .projects import AgentLabProjectStore, AgentLabProjectValidationError, _project_work_summary
from .apps import AgentLabAppStore
from .project_directory import ProjectDirectoryProjection, ensure_workspace, managed_workspace


class AgentLabProjectApplication:
    def __init__(self, db_path: str | Path, *, session_application: Any,
                 current_model: Callable[[], dict[str, str]], scope_id: str = "local",
                 read_golden: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
                 command_golden: Callable[[Mapping[str, Any]], dict[str, Any]] | None = None,
                 knowledge: Any = None, start_knowledge: Callable | None = None,
                 cancel_knowledge: Callable | None = None, read_experiments: Callable | None = None,
                 read_trials: Callable | None = None, command_app: Callable | None = None) -> None:
        self.db_path = Path(db_path)
        self.sessions = session_application
        self.current_model = current_model
        self.read_golden = read_golden
        self.command_golden = command_golden
        self.knowledge, self.start_knowledge, self.cancel_knowledge = knowledge, start_knowledge, cancel_knowledge
        self.read_experiments = read_experiments
        self.read_trials = read_trials
        self.command_app = command_app
        self.apps = AgentLabAppStore(self.db_path,scope_id=scope_id,freeze_knowledge=getattr(knowledge, 'app_resources', None))
        self.directory = ProjectDirectoryProjection(self.db_path, scope_id=scope_id)
        self.store = AgentLabProjectStore(self.db_path, scope_id=scope_id,
                                         bind_execution=self._bind, create_guide=self._guide,
                                         prepare_app=lambda conn,project,value:self.apps.prepare(conn,project,value,self.current_model()))

    def read(self, payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        if not isinstance(payload, Mapping) or set(payload) - {"projectId", "materialSetId", "artifactId", "artifactRevision"}:
            raise AgentLabProjectValidationError("项目读取参数无效。")
        revision = payload.get("artifactRevision")
        if revision == "":
            revision = None
        if isinstance(revision, str) and revision.isascii() and revision.isdecimal():
            revision = int(revision)
        result = self.store.read(payload.get("projectId", ""), material_set_id=payload.get("materialSetId", ""),
                                 artifact_id=payload.get("artifactId", ""), artifact_revision=revision,
                                 knowledge_resources=self._knowledge_resources())
        if not payload.get("projectId") and self.read_experiments is not None:
            from .history import public_history_collections
            try:
                result["historyCollections"] = public_history_collections(self.read_experiments())
            except (OSError, ValueError, sqlite3.Error):
                result["historyUnavailable"] = True
        # Project summaries intentionally stay cheap.  A selected project gets
        # a second, read-only execution projection so the UI can distinguish a
        # real queued/running/completed model job from a published artifact or
        # an Agent's proposed next step.  This never starts a worker.
        workflow_executions: dict[str, Any] = {}
        workflow_unavailable: list[str] = []
        owner_reads: dict[tuple[str, str], Any] = {}
        if isinstance(result.get("project"), Mapping):
            project = dict(result["project"])
            bindings = []
            for binding in project.get("bindings", []):
                item = dict(binding) if isinstance(binding, Mapping) else binding
                if isinstance(item, dict):
                    try:
                        owner = item.get('ownerRef') if isinstance(item.get('ownerRef'), Mapping) else {}
                        key = (str(owner.get('kind', '')), str(owner.get('id', '')))
                        if key not in owner_reads:
                            if len(owner_reads) >= 64:
                                raise AgentLabProjectValidationError('本次运行投影达到读取上限。')
                            # Cache failures as well as successes so duplicate
                            # bindings cannot multiply expensive owner reads.
                            owner_reads[key] = None
                            if key[0] == 'golden_suite' and self.read_golden:
                                observed = self.read_golden({'suiteId': key[1]})
                                suite = observed.get('suite') if isinstance(observed, Mapping) else None
                                if not isinstance(suite, Mapping) or suite.get('suiteId') != key[1]:
                                    raise AgentLabProjectValidationError('绑定的执行记录暂未读到。')
                                owner_reads[key] = {'suite': suite}
                            else:
                                observed = self._execution(project, 'execution_read', {'bindingId': item.get('bindingId', '')})
                                owner_reads[key] = observed.get('execution')
                        execution = owner_reads[key]
                        if not isinstance(execution, Mapping):
                            raise AgentLabProjectValidationError('执行记录暂时无法读取。')
                        # Raw owner data is local to this read. Only the
                        # allowlisted workflow and continuation summary leave
                        # this method; the Guide never receives held-out cases.
                        workflow_executions[item.get('bindingId', '')] = execution
                        item["execution"] = self._execution_summary(execution)
                    except (AgentLabProjectValidationError, OSError, ValueError, RuntimeError, sqlite3.Error):
                        # A temporary read failure must not erase the durable
                        # binding.  The run surface will explain that it needs
                        # a refresh and remains able to open the owner UI.
                        item["execution"] = {"status": "unavailable", "label": "运行状态待核对",
                                             "reason": "执行回执暂时无法读取；原绑定仍保留。", "latestJob": None,
                                             "canContinue": False}
                        workflow_unavailable.append(str(item.get('bindingId') or 'execution'))
                bindings.append(item)
            project["bindings"] = bindings
            result["project"] = project
        result["availableAdapters"] = [{"adapterId": "golden.context_qa", "title": "资料问答评测",
                                        "description": "从选定文本材料建立标准，由现有 Golden/Pi 执行。创建绑定本身不启动模型。",
                                        'input':{'targetCount':'1–100 之间的整数，默认 12','sourceIds':'可选；此项目 document/history/failure 来源的 sourceId 列表','scenario':'可选；本次评测的任务说明'}}]
        if self.knowledge is not None:
            result["availableAdapters"].append({"adapterId": "golden.knowledge_qa", "title": "知识库回答评测",
                "description": "绑定已完成的 Knowledge 索引和可选的原始评测集；回答前真实检索，参考答案只交给评审。",
                "input": {"indexId": "本项目已完成的索引任务 ID", "datasetId": "可选的原始评测集任务 ID；不传则起草待审核标准",
                          "targetCount": "2–100，默认 12", "profile": "mode,topK,threshold,rerank,candidateDepth,contextChars"}})
            if result.get("project"):
                try:
                    result["knowledge"] = self.knowledge.read(result["project"]["projectId"])
                    if not isinstance(result["knowledge"], Mapping):
                        raise AgentLabProjectValidationError('知识库记录暂时无法读取。')
                except (AgentLabProjectValidationError, OSError, ValueError, RuntimeError, sqlite3.Error):
                    result["knowledge"] = {"status": "unavailable", "unavailable": True,
                                           "corpora": [], "indexes": [], "datasets": [], "evaluations": [], "jobs": []}
                    workflow_unavailable.append('knowledge')
        if result.get('project'):
            from .project_workflow import project_workflow
            app_calls = None
            try:
                app_calls = self.apps.project_calls(result['project']['projectId'])
            except (OSError, ValueError, RuntimeError, sqlite3.Error):
                workflow_unavailable.append('application_calls')
            history_artifact = None
            progress_artifact = None
            history = result['project'].get('historyOrigin') or {}
            if history.get('snapshotArtifactId'):
                try:
                    # One pinned artifact read, not another catalog scan. This
                    # preserves the imported experiment IDs, factors and source
                    # revisions instead of replacing accepted history with new
                    # evaluations or Agent-written progress claims.
                    with self.store._connection() as conn:
                        history_artifact = self.store._artifact(conn, result['project']['projectId'],
                            history['snapshotArtifactId'], history.get('snapshotArtifactRevision'))
                except (AgentLabProjectValidationError, OSError, ValueError, RuntimeError, sqlite3.Error):
                    workflow_unavailable.append('history:' + str(history['snapshotArtifactId']))
            if result['project'].get('artifacts'):
                try:
                    with self.store._connection() as conn:
                        row = conn.execute("SELECT payload_json FROM agent_lab_project_artifacts WHERE project_id=? "
                            "AND json_extract(payload_json,'$.content.schemaVersion')='paw.lab-project-progress.v1' "
                            "ORDER BY updated_at_ms DESC, rowid DESC LIMIT 1", (result['project']['projectId'],)).fetchone()
                        progress_artifact = json.loads(row[0]) if row else None
                except (OSError, ValueError, RuntimeError, sqlite3.Error):
                    workflow_unavailable.append('project_progress')
            result['project']['workflow'] = project_workflow(result['project'], knowledge=result.get('knowledge'),
                executions=workflow_executions, app_calls=app_calls, history_artifact=history_artifact,
                progress_artifact=progress_artifact, unavailable_owners=workflow_unavailable)
            result['project']['directory'] = self.directory.sync(result['project'])
            result['commandGuide'] = {
                'publish_artifact':{'new':'直接提供 title,kind,view,content；不包在 artifact 对象里。','update':'artifactId + expectedArtifactRevision，附要修改的字段；只改内容可只给 content。','views':{'markdown':'content 是正文字符串','html':'content 是自包含 HTML 字符串','code':'content={source,language,filename?}','table':'content={columns:[{key,label}],rows:[{列key:值}],caption?}','form':'content={fields:[{key,label,type,required?,options?}],values:{字段key:值},description?}','json':'任意有效 JSON 内容'},'actions':'可选 [{actionId,label,prompt}]；点击会把项目输入发给当前 Guide。'},
                'bind_execution':'input={adapterId, input:适配器参数, artifactId?,artifactRevision?}；绑定不启动模型。',
                'knowledge':{'read':'knowledge_read 返回当前项目资源与任务，可附 jobId 精确查看；不执行。保留集仅返回汇总指标。',
                    'command':'knowledge_command 使用 expectedRevision,clientRequestId,input；input.operation 选择下列操作。未知结果用完全相同的请求核对；完成且输入未改变的任务直接复用。',
                    'operations':{'import_corpus':'path 或 uploadId，可选 fields','connect_base':'kbId：用户接入的知识库','import_dataset':'corpusId + path 或 uploadId，可选 fields；Agent 可将生成题集写入项目目录后导入，保留来源与分组',
                        'index':'corpusId, chunking:{strategy,size,overlap}, embedding:none|configured','restore_index':'corpusId,packagePath：已导出完整索引包，不重新 Embedding',
                        'search':'indexId,query,profile','evaluate':'indexId,datasetId,profile,split:development|holdout','cancel':'jobId'},
                    'profile':'mode:lexical|dense|hybrid,topK,threshold,rerank,candidateDepth,contextChars；candidateDepth 控制重排候选，rerank=false 时不能宣称实际候选池就是该值。',
                    'paths':'path/packagePath 必须是当前 Session 工作目录内的绝对路径；外部资料由用户材料入口接入。'},
                'execution':'execution_read 先取绑定 suite 的 revision 和 jobs；draft 生成题集，review 用独立 Pi 调用核对尚未核对的题目并标注样本，记录 author=agent；已完成核对不重复。开发集可用 review_case/label_sample 修正，必须保留 Agent 作者，不能编辑或读取保留题。calibrate 通过后 freeze。experiment 真实调用基线与候选模型，返回逐题回执、指标和用量；baseline/candidate.applicationMethod 可绑定 {artifactId,artifactRevision} 或 {body,title}，比较应用方法正文，不代表原生 Skill 路由评测。只有 completed 回执允许发布本轮指标或 Keep/Reject；Tool、MCP/Workflow、检索必须使用对应执行器。',
                'workflow':{'read':'project.workflow 来自实际执行与已保存的项目流程。打开、选择或移动节点不重新执行。project.directory 提供项目文件夹与延续记录。',
                    'publish':'用已有 publish_artifact 保存一个 project_progress 成果，view=json，content.schemaVersion=paw.lab-project-progress.v1；steps:[{id,title,state,summary,dependsOn?,jobId?,evidenceRefs?}]。只声明本项目实际需要的依赖，真实任务附 jobId，Runtime 回执覆盖计划状态。',
                    'reuse':'读现有绑定、输入版本与已完成回执；相同输入复用记录，变化只创建受影响工作的候选版本。保留被淘汰结果和原因，不为每次刷新新建任务。'},
                'read_app':'op=read，提供 appId，可选 appVersion、appCallId。默认返回本项目应用版本与调用摘要；appCallId 按需读取实际输入、输出、用量和回执。不启动调用。',
                'app_command':'op=app_command，appId,action:invoke|cancel|resume,expectedRevision:应用revision,clientRequestId,input。invoke input={version,actionId,values}；cancel/resume input={callId}。只调用当前项目的不可变应用版本；等待原回执后再比较，未知结果以相同请求核对。研究型应用的 Tool/Workflow 效果以实际调用与来源读取回执为准。',
                'prepare_app':{'input':'{directory:相对 executionWorkspace.path 的应用目录, appId?:已有应用标识, evaluationSelection?:{suiteId,jobId,variant:baseline|candidate}}',
                    'evaluatedSelection':'app.json 可声明 evaluationSelection:{suiteId,jobId,variant:baseline|candidate}；必须属于当前项目且实验已完成。准备器直接采用该次冻结的应用方法、Prompt、模型与检索配置，保留原评测引用。SKILL 的伴随材料与 App 运行仍需验收。',
                    'sourceFile':'app.json','schemaVersion':'paw.lab-app-source.v1','requiredFields':['schemaVersion','title','html','skill','context','actions'],
                    'fields':{'description':'可选的应用说明','html':'自包含 HTML 文件相对路径','skill':'本应用 SKILL.md 方法文件相对路径','context':'要随应用冻结的文本材料相对路径数组','model':'可选 {provider,model,thinkingLevel}；不提供时冻结当前项目默认模型','knowledge':'可选 {indexId,profile?,queryField?}，冻结本项目已完成的 Knowledge 索引。支持 lexical；固定版本的本地 sentence-transformers 索引还支持 dense/hybrid。当前要求 rerank=false，不支持未冻结的图检索；原始评测问题和答案不会进入应用。','actions':'[{id,title,prompt,kind?:completion|retrieval,inputSchema:{type:object,properties:{字段:{type:string|number|integer|boolean,title?,enum?,maxLength?,minimum?,maximum?}},required:[字段]}}]'},
                    'browserApi':'HTML 调用 await window.pawApp.invoke(actionId, values, {onProgress,signal})，获得 {text,usage,receipt?,sources?,knowledge?}。可选 AbortSignal 请求停止，仍等待原调用回执。capabilities.cancel 表示支持停止；ready() 声明应用自己呈现进度。旧两参数调用仍可用。调用失败会 reject 带 state/requestId 的 Error。',
                    'interaction':{'required':'生成 App 时同时设计输入确认、运行中、完成、无结果和失败恢复。不能只放 loading 后等待最终答案。采用 agent-lab-project/assets/portable-app.html 的反馈行为并适配领域界面。',
                        'progress':'onProgress({stage,events?,sources?,knowledge?,text?,streamPartial?,startedAtMs?,updatedAtMs?})；stage: queued/context_ready/retrieving/sources_ready/model_starting/model_wait/thinking/answering/completed/failed/cancelled/interrupted/unconfirmed。events 只记录实际阶段。此回调是公开进度投影，只有 invoke resolve 才能确认完成。',
                        'sources':'实际召回后立刻展示 sources 的 title/uri/text，可展开原文；knowledge.retrievedChunks 是召回数，sources.length 是采用数。context_ready/sourceKind=provided_context 表示直接提供的应用资料，不能说成召回。回答流更新时保持来源展开状态。',
                        'conversation':'对话型 App 默认用紧凑消息流、底部输入、可展开过程和引用，不堆等高结果卡片。真实追问需声明可选 conversation 字符串字段，并把已完成前文作为不可信上下文发送；原问题与新问题分开。研究阶段必须来自实际工具或执行器，不能编排假动画。',
                        'feedback':'按真实 stage 显示检索、连接、等待、思考、输出。只有收到 thinking 才显示思考中。text 标记为尚未完成；streamPartial 时说明过程不完整。不得编造百分比、思考内容、相关度。',
                        'recovery':'保留输入与证据，运行时防重复点击，中断时停止动画并核对原调用。history 恢复结果不触发新模型调用、不覆盖进行中的结果。',
                        'acceptance':'PAW 与独立导出各检查慢调用期间来源可见、阶段变化、输出、失败及历史恢复。区分真实模型与受控测试。'},
                    'runtime':'自定义 HTML + 声明的知识检索或文本模型操作；PAW 通过普通 Pi Session 执行，独立包通过配置的 OpenAI-compatible 服务执行。此封装没有外部业务写入工具，不能声称支持未接入的订单、支付等操作。',
                    'externalWorkspace':'可选 {title,url}，连接已存在的浏览器工作台。URL 仅支持无凭据的 HTTPS 或本机 HTTP，不含查询参数和片段。工作台在独立页面中运行，与问答共享 App 入口；不会获得问答桥接权限或自动执行。服务源码和登录态不随应用导出，必须另行启动。',
                    'output':'返回 application 与不可变版本；前端“应用交付”可试用、添加至 PAW、导出独立或 PAW 应用包。准备不等于安装或效果验证。'},
            }
        return result

    def _knowledge_resources(self) -> Mapping[str, Mapping[str, Any]]:
        reader = getattr(self.knowledge, 'project_resources', None)
        if not callable(reader):
            return {}
        try:
            resources = reader()
            return resources if isinstance(resources, Mapping) else {}
        except (OSError, ValueError, RuntimeError, sqlite3.Error):
            return {}

    @staticmethod
    def _execution_summary(execution: Any) -> dict[str, Any]:
        """Reduce an owner read to truthful project-level continuation state."""
        if not isinstance(execution, Mapping):
            return {"status": "unavailable", "label": "运行状态待核对",
                    "reason": "执行器没有返回可核对的状态。", "latestJob": None,
                    "canContinue": False}
        jobs = execution.get("jobs")
        if not isinstance(jobs, list):
            suite = execution.get("suite")
            jobs = suite.get("jobs") if isinstance(suite, Mapping) else None
        jobs = [job for job in jobs if isinstance(job, Mapping)] if isinstance(jobs, list) else []
        stamp = lambda job: job.get("updatedAtMs") if isinstance(job.get("updatedAtMs"), (int, float)) else job.get("createdAtMs") if isinstance(job.get("createdAtMs"), (int, float)) else 0
        jobs.sort(key=lambda job: (stamp(job),
                                   str(job.get("jobId") or "")), reverse=True)
        latest = jobs[0] if jobs else None
        state = str(latest.get("state") or "") if latest else "not_started"
        labels = {"queued": "排队中", "running": "模型运行中", "completed": "已完成",
                  "failed": "运行失败", "cancelled": "已停止", "interrupted": "待恢复"}
        if state == "not_started":
            return {"status": state, "label": "尚未运行", "reason": "执行绑定已建立，可以进入评测页面开始真实模型运行。",
                    "latestJob": None, "canContinue": True}
        if state in {"queued", "running"}:
            return {"status": state, "label": labels[state],
                    "reason": str(latest.get("progress") or "等待真实执行回执。"),
                    "latestJob": {"jobId": str(latest.get("jobId") or ""), "kind": str(latest.get("kind") or ""),
                                   "state": state, "progress": str(latest.get("progress") or "")},
                    "canContinue": False}
        if state == "completed":
            kind = str(latest.get("kind") or "")
            if kind != "experiment":
                return {"status": state, "label": "评测准备完成", "canContinue": True,
                        "reason": "评测准备步骤已完成；核对题集和标准后再开始基线与候选实验。",
                        "latestJob": {"jobId": str(latest.get("jobId") or ""),
                                      "kind": kind, "state": state,
                                      "progress": str(latest.get("progress") or "")}}
            result = latest.get("result") if isinstance(latest.get("result"), Mapping) else {}
            comparison = result.get("comparison") if isinstance(result, Mapping) and isinstance(result.get("comparison"), Mapping) else {}
            decision = str(comparison.get("decision") or "")
            reason = "模型运行已完成；可以核对逐题回执后继续下一轮优化。"
            if decision:
                reason = f"模型运行已完成，当前判定为 {decision}；可以核对逐题回执后继续下一轮优化。"
            return {"status": state, "label": labels[state], "reason": reason,
                    "latestJob": {"jobId": str(latest.get("jobId") or ""), "kind": str(latest.get("kind") or ""),
                                   "state": state, "progress": str(latest.get("progress") or ""), **({"decision": decision} if decision else {})},
                    "canContinue": True}
        return {"status": state if state in labels else "failed", "label": labels.get(state, "运行未完成"),
                "reason": str(latest.get("error") or latest.get("progress") or "本次运行没有形成完整回执，请核对原始记录后继续。"),
                "latestJob": {"jobId": str(latest.get("jobId") or ""), "kind": str(latest.get("kind") or ""),
                               "state": state}, "canContinue": state in {"failed", "cancelled", "interrupted"}}

    def command(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(payload, Mapping) and payload.get("action") == "knowledge":
            result = self._knowledge_command(payload)
        elif isinstance(payload, Mapping) and payload.get("action") == "import_history":
            from .history import prepare_history_import
            # Snapshot before entering the project transaction. The callback is
            # evaluated only after checking the durable original receipt.
            try:
                experiments = self.read_experiments() if self.read_experiments else []
            except (OSError, ValueError, sqlite3.Error):
                experiments = []
            result = self.store.command(payload, history_import=lambda value: prepare_history_import(value, experiments))
        else:
            result = self.store.command(payload)
        # The command journal has committed before touching generated files.
        # A projection failure never converts a committed action into a retry.
        if isinstance(result.get('project'), Mapping):
            project = result['project']
            project.update(_project_work_summary(project, project.get('artifacts'),
                self._knowledge_resources().get(project['projectId'])))
            result['project']['directory'] = self.directory.sync(result['project'])
        return result

    def _knowledge_command(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        from .knowledge_data import KnowledgeIntakeError
        from .trials import AgentLabTrialConflict, AgentLabTrialServiceUnavailable
        from .projects import AgentLabProjectConflict, AgentLabProjectUnavailable
        if (self.knowledge is None or self.start_knowledge is None or self.cancel_knowledge is None
                or set(payload) != {"action", "projectId", "expectedRevision", "clientRequestId", "input"}
                or type(payload["expectedRevision"]) is not int or payload["expectedRevision"] < 1
                or not isinstance(payload["clientRequestId"], str) or not 1 <= len(payload["clientRequestId"]) <= 240
                or not isinstance(payload["input"], Mapping)):
            raise AgentLabProjectValidationError("知识库操作需要当前项目和有效参数。")
        project = self.store.read(payload["projectId"])["project"]
        value = dict(payload["input"])
        try:
            if value.get("operation") in {"upload_begin", "upload_chunk", "upload_seal"}:
                result = self.knowledge.upload(project["projectId"], payload["clientRequestId"], value)
            elif value.get("operation") == "cancel":
                if set(value) != {"operation", "jobId"} or not any(job["jobId"] == value["jobId"] for job in self.knowledge._jobs(project["projectId"])):
                    raise AgentLabProjectValidationError("只能停止此项目的知识库任务。")
                result = self.cancel_knowledge(value["jobId"])
            else:
                if "projectId" in value:
                    raise AgentLabProjectValidationError("知识库操作的项目身份不能由输入替换。")
                # Resources have immutable job identities, independent of brief
                # edits. A stale project revision cannot retarget these inputs.
                result = self.start_knowledge(payload["clientRequestId"], {**value, "projectId": project["projectId"]})
        except KnowledgeIntakeError as exc:
            raise AgentLabProjectValidationError(str(exc)) from exc
        except AgentLabTrialConflict as exc:
            raise AgentLabProjectConflict("此请求已用于另一项知识库操作。请核对原操作。") from exc
        except AgentLabTrialServiceUnavailable as exc:
            raise AgentLabProjectUnavailable() from exc
        return {"ok": True, "project": project, **({"job": result["job"]} if "job" in result else {"upload": result["upload"]}), "clientRequestId": payload["clientRequestId"],
                "replayed": bool(result.get("replayed", False))}

    def _bind(self, conn: sqlite3.Connection, project: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
        if request["adapterId"] == "scene.trial":
            from .history import SCENES
            value = request["input"]
            if set(value) != {"sceneId"} or value["sceneId"] not in {row[0] for row in SCENES}:
                raise AgentLabProjectValidationError("请选择已登记的场景。")
            return {"ownerRef": {"kind": "scene_trial", "id": value["sceneId"]},
                    "summary": "历史证据已经保留；执行是否可用以 Trial 服务当前登记的环境为准。新运行单独记录。"}
        if request["adapterId"] == "golden.knowledge_qa" and self.knowledge is not None:
            from .knowledge_data import KnowledgeIntakeError
            try:
                value = self.knowledge.golden_inputs(project, request["input"])
            except KnowledgeIntakeError as exc:
                raise AgentLabProjectValidationError(str(exc)) from exc
            suite = AgentLabGoldenStore(self.db_path, default_model=self.current_model()).create_in_transaction(conn, value)
            return {"ownerRef": {"kind": "golden_suite", "id": suite["suiteId"]},
                    "summary": "已冻结知识库索引与检索配置；题目等待核对，尚未调用模型。"}
        if request["adapterId"] != "golden.context_qa":
            raise AgentLabProjectValidationError("此执行适配器尚未接入。项目成果仍可使用自己的结构和展示形式。")
        value = request["input"]
        if set(value) - {"targetCount", "sourceIds", "scenario"}:
            raise AgentLabProjectValidationError("资料问答绑定参数无效。")
        materials = project["materialSet"]["materials"]
        ids = value.get("sourceIds")
        available = {item["sourceId"] for item in materials if item["kind"] in {"document", "history", "failure"}}
        if ids is not None and (not isinstance(ids, list) or not ids
                                or any(not isinstance(item, str) for item in ids) or set(ids) - available):
            raise AgentLabProjectValidationError("请选择此项目中的业务文档、历史任务或失败材料。")
        selected = set(ids) if ids is not None else available
        sources = [{key: item[key] for key in ("sourceId", "title", "kind", "uri", "text")}
                   for item in materials if item["sourceId"] in selected]
        if not sources:
            raise AgentLabProjectValidationError("请先添加可供问答标准引用的业务材料。")
        suite = AgentLabGoldenStore(self.db_path, default_model=self.current_model()).create_in_transaction(conn, {
            "title": project["title"], "scenario": value.get("scenario", project["description"]),
            "sources": sources, "targetCount": value.get("targetCount", 12),
        })
        return {"ownerRef": {"kind": "golden_suite", "id": suite["suiteId"]}, "summary": "已绑定材料版本；尚未执行评测。"}

    def _guide(self, conn: sqlite3.Connection, project: dict[str, Any]) -> dict[str, Any]:
        # Candidate files belong to a managed project workspace. Imported source
        # paths remain material connections, not writable Session roots.
        binding = managed_workspace(self.db_path, self.store.scope_id, project)
        workspace = Path(binding['path'])
        ensure_workspace(workspace)
        session = self.sessions.create_in_transaction({
            "title": f'Lab · {project["title"][:90]}', "mode": "coordinator",
            "toolProfileVersion": "control-center-v1", "executionMode": "workspace_managed",
            "workspaceRoots": [str(workspace)], "_internalWorkspaceScopeGrant": True,
            "projectContextEnabled": True, "piSkillsEnabled": True, "codexSkillsEnabled": False,
            "surfaceKind": "extension_app", "ownerAppId": "extension:agent-lab",
            "surfaceKey": f'project.{project["projectId"]}.guide',
        }, conn)
        return {"sessionId": str(session["id"]), "workspace": {"kind": "managed", "path": str(workspace), "createdAtMs": int(time.time() * 1000)}}

    def tool(self, session: Mapping[str, Any], operation: str, args: Mapping[str, Any]) -> dict[str, Any]:
        key = str(session.get("surfaceKey", ""))
        if (session.get("surfaceKind") != "extension_app" or session.get("ownerAppId") != "extension:agent-lab"
                or not key.startswith("project.") or not key.endswith(".guide")):
            raise AgentLabProjectValidationError("此工具需要由对应 Lab 项目的 Agent 调用。")
        project_id = key[len("project."):-len(".guide")]
        project = self.store.read(project_id)["project"]
        if project["guideSessionId"] != session.get("id"):
            raise AgentLabProjectValidationError("引导会话与当前项目绑定不一致。")
        value = {key: item for key, item in args.items() if key not in {"op", "_sessionId"}}
        if operation == 'app_command':
            if (self.command_app is None or set(value) != {'appId', 'action', 'expectedRevision', 'clientRequestId', 'input'}
                    or value['action'] not in {'invoke', 'cancel', 'resume'} or not value.get('appId')):
                raise AgentLabProjectValidationError('当前应用操作需要绑定的应用、版本及调用参数。')
            self.apps.read({'projectId': project_id, 'appId': value['appId']})
            return self.command_app(value)
        if operation == "read":
            if set(value) - {"artifactId", "artifactRevision", "materialSetId", "appId", "appVersion", "appCallId"}:
                raise AgentLabProjectValidationError("项目读取参数无效。")
            if set(value) & {"appId", "appVersion", "appCallId"}:
                if not value.get('appId') or set(value) & {"artifactId", "artifactRevision", "materialSetId"}:
                    raise AgentLabProjectValidationError("应用读取需要 appId；请与成果或材料读取分别执行。")
                observed = self.apps.read({'projectId':project_id,'appId':value['appId'],
                    **({'version':value['appVersion']} if 'appVersion' in value else {}),
                    **({'callId':value['appCallId']} if 'appCallId' in value else {})})
                observed['version'] = {key:item for key,item in observed['version'].items() if key != 'html'}
                observed['calls'] = [{key:item for key,item in call.items() if key != 'result'} for call in observed.get('calls',[])]
                return {'ok':True,'projectId':project_id,'application':observed}
            observed = self.read({"projectId": project_id, **value})
            # The UI owns the user's project catalog. A bound Guide receives
            # only its current project, including in the response summaries.
            observed['items'] = [item for item in observed.get('items', []) if item['projectId'] == project_id]
            if isinstance(observed.get('knowledge'), Mapping):
                observed['knowledge'] = self._guide_knowledge(observed['knowledge'])
            return observed
        if operation in {"knowledge_read", "knowledge_command"}:
            if self.knowledge is None:
                raise AgentLabProjectValidationError('当前项目没有接入 Knowledge 执行器。')
            if operation == 'knowledge_read':
                if set(value) - {'jobId'}:
                    raise AgentLabProjectValidationError('知识库读取参数无效。')
                observed = self.knowledge.read(project_id)
                if value.get('jobId'):
                    job = next((job for job in self.knowledge._jobs(project_id) if job['jobId'] == value['jobId']), None)
                    if job is None:
                        raise AgentLabProjectValidationError('知识库任务不属于当前项目。')
                    observed = {'jobs': [job]}
                return {'ok': True, 'projectId': project_id, 'knowledge': self._guide_knowledge(observed)}
            if set(value) != {'expectedRevision', 'clientRequestId', 'input'} or not isinstance(value['input'], Mapping):
                raise AgentLabProjectValidationError('知识库命令需要版本、请求标识与操作输入。')
            self._workspace_paths(session, value['input'], ('path', 'packagePath'))
            result = self._knowledge_command({'action': 'knowledge', 'projectId': project_id, **value})
            if isinstance(result.get('job'), Mapping):
                result['job'] = self._guide_knowledge({'jobs': [result['job']]})['jobs'][0]
            return result
        if operation in {"execution_read", "execution_command"}:
            return self._execution(project, operation, value)
        if operation != "command" or value.get("action") in {"create", "import_history", "ensure_guide", "knowledge"}:
            raise AgentLabProjectValidationError("此操作不属于当前项目的成果工作。")
        if "projectId" in value:
            raise AgentLabProjectValidationError("项目身份由当前 Agent 绑定，不能由工具参数替换。")
        command_input = value.get("input")
        if value.get("action") == "import_materials" and isinstance(command_input, Mapping) and "path" in command_input:
            self._workspace_paths(session, command_input, ('path',))
        return self.command({"projectId": project_id, **value})

    @staticmethod
    def _workspace_paths(session: Mapping, value: Mapping, fields: tuple[str, ...]) -> None:
        roots = [Path(str(root)).expanduser().resolve() for root in session.get('workspaceRoots', [])]
        for field in fields:
            if field not in value:
                continue
            if not isinstance(value[field], str) or not value[field]:
                raise AgentLabProjectValidationError('本地材料路径无效。')
            path = Path(value[field]).expanduser()
            if not path.is_absolute() or not any(path.resolve().is_relative_to(root) for root in roots):
                raise AgentLabProjectValidationError('此材料位于当前 Session 的工作空间之外，请通过项目接入添加。')

    @staticmethod
    def _guide_knowledge(observed: Mapping) -> dict:
        result = copy.deepcopy(dict(observed))
        def safe_evaluation(value):
            if not isinstance(value, dict) or value.get('split') != 'holdout':
                return value
            report = value.get('report', {})
            value['report'] = {key: report[key] for key in ('metrics', 'queryCount', 'config', 'configHash', 'schemaVersion') if key in report}
            value['holdoutReferencesVisible'] = False
            return value
        result['evaluations'] = [safe_evaluation(value) for value in result.get('evaluations', [])]
        for job in result.get('jobs', []):
            if isinstance(job.get('result'), dict):
                job['result'] = safe_evaluation(job['result'])
        return result

    @staticmethod
    def _guide_job(job: Mapping) -> dict:
        public = copy.deepcopy(dict(job))
        result = public.pop('result', None)
        if isinstance(result, Mapping):
            safe = {key: copy.deepcopy(result[key]) for key in ('usage', 'usageByScope', 'reviewAuthor', 'reviewedCount', 'approvedCount') if key in result}
            if job.get('kind') == 'experiment':
                safe.update({key: copy.deepcopy(result[key]) for key in
                    ('snapshotId', 'suiteId', 'executionMode', 'optimizationScope', 'baseline', 'candidate',
                     'applicationMethodComparison', 'development', 'comparison', 'knowledge') if key in result})
                phase = result.get('holdout')
                if isinstance(phase, Mapping):
                    safe['holdout'] = {key: copy.deepcopy(phase[key]) for key in
                        ('baselineMetrics', 'candidateMetrics', 'businessCost') if key in phase}
            public['result'] = safe
        return public

    @classmethod
    def _guide_suite(cls, suite: Mapping) -> dict:
        public = copy.deepcopy(dict(suite))
        public['holdoutCaseCount'] = sum(item.get('split') == 'holdout' for item in public.get('cases', []))
        public['cases'] = [item for item in public.get('cases', []) if item.get('split') == 'development']
        public['jobs'] = [cls._guide_job(job) for job in public.get('jobs', [])]
        public['holdoutReferencesVisible'] = False
        return public

    def _execution(self, project: dict[str, Any], operation: str, value: dict[str, Any]) -> dict[str, Any]:
        fields = {"bindingId"} if operation == "execution_read" else {"bindingId", "action", "expectedRevision", "clientRequestId", "input"}
        if set(value) - fields:
            raise AgentLabProjectValidationError("执行操作参数无效。")
        binding = next((item for item in project["bindings"] if item["bindingId"] == value.get("bindingId")), None)
        if binding is None:
            raise AgentLabProjectValidationError("执行绑定不属于当前项目。")
        if binding["adapterId"] == "scene.trial" and binding["ownerRef"]["kind"] == "scene_trial":
            if operation != "execution_read" or self.read_trials is None:
                raise AgentLabProjectValidationError("请在项目运行页选择本次场景配置并启动验证。")
            observed = self.read_trials()
            scene_id = binding["ownerRef"]["id"]
            return {"ok": True, "binding": binding, "execution": {
                "schemaVersion": observed["schemaVersion"],
                "registered": scene_id in observed.get("registeredSceneIds", []),
                "jobs": [job for job in observed.get("jobs", []) if job.get("sceneId") == scene_id],
            }}
        if binding["adapterId"] not in {"golden.context_qa", "golden.knowledge_qa"} or binding["ownerRef"]["kind"] != "golden_suite":
            raise AgentLabProjectValidationError("此绑定的执行适配器尚未接入。")
        suite_id = binding["ownerRef"]["id"]
        if operation == "execution_read" and self.read_golden:
            observed = self.read_golden({"suiteId": suite_id})
            suite = observed.get("suite")
            if not isinstance(suite, Mapping) or suite.get("suiteId") != suite_id:
                raise AgentLabProjectValidationError("绑定的执行记录暂未读到。")
            suite = self._guide_suite(suite)
            # The legacy catalog can include unrelated suites; a project Tool
            # returns only its own binding, never that cross-project catalog.
            return {"ok": True, "binding": binding, "execution": {"ok": True, "suite": suite, "items": [suite]}}
        if operation == "execution_command" and self.command_golden:
            action = value.get('action')
            if action not in {"draft", "review", "review_case", "label_sample", "judge_config", "calibrate", "freeze", "experiment", "cancel", "resume"}:
                raise AgentLabProjectValidationError('此操作未接入当前项目的评测执行器。')
            if action in {'review_case', 'label_sample'}:
                inputs = value.get('input')
                if not isinstance(inputs, Mapping) or self.read_golden is None:
                    raise AgentLabProjectValidationError('评测核对输入无效。')
                suite = self.read_golden({'suiteId': suite_id}).get('suite', {})
                case = next((case for case in suite.get('cases', []) if case['caseId'] == inputs.get('caseId')), None)
                if case is None or case.get('split') != 'development' or inputs.get('split', 'development') != 'development':
                    raise AgentLabProjectValidationError('优化 Agent 只能编辑开发集；保留集由独立 review 任务核对。')
                author = 'reviewAuthor' if action == 'review_case' else 'labelAuthor'
                if inputs.get(author, 'agent') != 'agent':
                    raise AgentLabProjectValidationError('Agent 核对必须记录真实 Agent 作者，不能声称人工审核。')
                value = {**value, 'input': {**inputs, author: 'agent'}}
            observed = self.command_golden({key: item for key, item in {**value, "suiteId": suite_id}.items() if key != "bindingId"})
            # Commands return the same owner receipt but must not bypass the
            # read projection and leak held-out questions through their suite.
            observed = dict(observed)
            if isinstance(observed.get('suite'), Mapping):
                observed['suite'] = self._guide_suite(observed['suite'])
            if isinstance(observed.get('job'), Mapping):
                observed['job'] = self._guide_job(observed['job'])
            return observed
        raise AgentLabProjectValidationError("当前运行环境没有接入此执行服务。")
