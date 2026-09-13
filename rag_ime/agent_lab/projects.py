"""General Agent-led Lab workspaces, artifacts and execution bindings.

Business structures belong to each artifact and its optional Skill template.
Pi and adapters retain execution authority; the caller owns workspace scope.
"""
from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .project_artifacts import VIEWS, validate_artifact
from .project_materials import empty_intake, read_inline_materials, read_local_materials, validate_material_set
from ..db import apply_database_migrations
from ..db.connection import sqlite_connection


class AgentLabProjectValidationError(ValueError):
    code = "AGENT_LAB_PROJECT_INVALID_REQUEST"
    http_status = 422

    def response_payload(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": str(self)}


class AgentLabProjectConflict(AgentLabProjectValidationError):
    code = "AGENT_LAB_PROJECT_CONFLICT"
    http_status = 409


class AgentLabProjectNotFound(AgentLabProjectValidationError):
    code = "AGENT_LAB_PROJECT_NOT_FOUND"
    http_status = 404


class AgentLabProjectUnavailable(AgentLabProjectValidationError):
    code = "AGENT_LAB_PROJECT_UNAVAILABLE"
    http_status = 503

    def __init__(self) -> None:
        super().__init__("优化项目暂时无法读取或保存，请稍后重试。")


def _now() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise AgentLabProjectValidationError("命令需要有效的 JSON 内容。") from exc


def _text(value: Any, label: str, limit: int = 240, *, optional: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not optional and not value.strip()):
        raise AgentLabProjectValidationError(f"{label}无效。")
    return value.strip()


def _object(value: Any, fields: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) - fields:
        raise AgentLabProjectValidationError(f"{label}字段无效。")
    return dict(value)


def _integer(value: Any, minimum: int = 0, maximum: int = 2**53 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise AgentLabProjectValidationError("版本或数量无效。")
    return value


def _strings(value: Any, label: str, *, maximum: int = 100) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise AgentLabProjectValidationError(f"{label}需要有效的文本列表。")
    return [_text(item, label, 2000) for item in value]


_ARTIFACT_FIELDS = {"title", "kind", "view", "content", "summary", "templateRef", "actions"}


def _project_work_summary(project: Mapping[str, Any], artifacts: list[Mapping[str, Any]] | None = None,
                          knowledge: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a truthful, read-only projection for the Lab home page.

    Project payloads deliberately do not store a guessed execution outcome.
    This projection only uses durable material intake, completed Knowledge
    resource receipts, bindings, history provenance, and published artifacts so the home page can answer
    "what should I do next?" without turning an Agent statement into a system
    status.
    """
    intake = project.get("intake") if isinstance(project.get("intake"), Mapping) else {}
    materials = int(project.get("materialCount") or 0)
    knowledge_ready = bool(knowledge and int(knowledge.get("documentCount") or 0) > 0
                           and (int(knowledge.get("corpusCount") or 0) > 0 or int(knowledge.get("indexCount") or 0) > 0))
    has_materials = materials > 0 or knowledge_ready
    bindings = project.get("bindings") if isinstance(project.get("bindings"), list) else []
    history = project.get("historyOrigin") if isinstance(project.get("historyOrigin"), Mapping) else None
    intake_state = str(intake.get("state") or "needs_materials")
    issues = intake.get("issues") if isinstance(intake.get("issues"), list) else []
    partial = bool(intake.get("partial"))
    missing: list[str] = []
    if intake_state == "unavailable" or partial:
        missing.append("材料路径或文件需重新核对")
    if not has_materials:
        missing.append("至少一份当前材料快照")
    if not bindings:
        missing.append("一个已登记的执行绑定")

    if intake_state == "unavailable" or partial:
        status, label, reason = "blocked", "材料受阻", "材料尚未完整读取，先修复来源后再继续。"
        next_kind, next_label, next_reason = "fix_materials", "修复材料", "核对来源路径、权限或未读取项。"
    elif history and not has_materials:
        status, label, reason = "history_only", "历史结果", "这是迁移的历史记录，可以查看，但没有当前材料快照，不能直接复跑。"
        next_kind, next_label, next_reason = "prepare_rerun", "准备复跑", "先绑定当前数据与执行环境；历史导入本身没有重新运行。"
    elif not has_materials:
        status, label, reason = "draft", "待接入材料", "项目还没有可用于验证的当前材料。"
        next_kind, next_label, next_reason = "add_materials", "添加材料", "上传、粘贴或连接执行器上的材料。"
    elif not bindings:
        status, label, reason = "needs_binding", "待连接执行", "材料已保存，但还没有登记本项目的执行绑定。"
        if knowledge_ready:
            reason = "Knowledge 资料已接入，可继续索引、评测准备或连接本项目的回答评测；没有登记回答执行绑定。"
        next_kind, next_label, next_reason = "bind_execution", "连接执行", "选择已登记的评测或运行适配器。"
    elif artifacts:
        status, label, reason = "active", "可继续", "项目已有成果和执行绑定，可以从最近成果继续核对。"
        next_kind, next_label, next_reason = "review_latest", "查看最近成果", "先核对最近成果与原始回执，再决定是否开始下一轮。"
    else:
        status, label, reason = "ready", "已就绪", "材料和执行绑定都已登记，可以开始一次新的验证。"
        next_kind, next_label, next_reason = "start_validation", "开始验证", "确认范围、标准和预算后再运行。"

    readiness_status = "ready" if not missing else "not_ready"
    readiness_reason = "当前材料、执行绑定和读取状态满足复跑前提。" if not missing else "；".join(missing)
    latest: dict[str, Any] | None = None
    ordered_artifacts = sorted((dict(item) for item in (artifacts or [])), key=lambda item: (int(item.get("updatedAtMs") or 0), str(item.get("artifactId") or "")), reverse=True)
    if history:
        snapshot_id = str(history.get("snapshotArtifactId") or "")
        snapshot = next((item for item in ordered_artifacts if str(item.get("artifactId") or "") == snapshot_id), None)
        latest = {"kind": "history", "status": "historical", "title": str((snapshot or {}).get("title") or "历史实验快照"),
                  "updatedAtMs": int(history.get("importedAtMs") or project.get("updatedAtMs") or 0),
                  "artifactId": snapshot_id}
    elif ordered_artifacts:
        item = ordered_artifacts[0]
        latest = {"kind": "artifact", "status": "available", "title": str(item.get("title") or "最近成果"),
                  "updatedAtMs": int(item.get("updatedAtMs") or 0), "artifactId": str(item.get("artifactId") or "")}
    return {
        **({"knowledgeResources": dict(knowledge)} if knowledge else {}),
        "workState": {"status": status, "label": label, "reason": reason},
        "nextAction": {"kind": next_kind, "label": next_label, "reason": next_reason},
        "rerunReadiness": {"status": readiness_status, "reason": readiness_reason, "missing": missing},
        "latestRecord": latest,
    }


class AgentLabProjectStore:
    def __init__(self, db_path: str | Path, *, scope_id: str = "local",
                 bind_execution: Callable[[sqlite3.Connection, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None,
                 create_guide: Callable[[sqlite3.Connection, dict[str, Any]], str | dict[str, Any]] | None = None,
                 prepare_app: Callable[[sqlite3.Connection, dict[str, Any], dict[str, Any]], dict[str, Any]] | None = None) -> None:
        self.db_path = Path(db_path)
        self.scope_id = _text(scope_id, "工作空间范围")
        self._bind_execution = bind_execution
        self._create_guide = create_guide
        self._prepare_app = prepare_app
        self._initialize_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        try:
            with sqlite_connection(self.db_path, row_factory=sqlite3.Row, foreign_keys=True) as conn:
                yield conn
        except (sqlite3.Error, OSError) as exc:
            raise AgentLabProjectUnavailable() from exc

    def initialize(self) -> None:
        with self._initialize_lock:
            if self._initialized:
                return
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AgentLabProjectUnavailable() from exc
            with self._connection() as conn:
                apply_database_migrations(conn)
            self._initialized = True

    def _project(self, conn: sqlite3.Connection, project_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT payload_json FROM agent_lab_projects WHERE project_id=? AND scope_id=?", (project_id, self.scope_id)).fetchone()
        if row is None:
            raise AgentLabProjectNotFound("优化项目不存在，请从项目列表重新打开。")
        project = json.loads(row[0])
        if not project.get('executionWorkspace'):
            from .project_directory import managed_workspace
            project['executionWorkspace'] = managed_workspace(self.db_path, self.scope_id, project)
        return project

    @staticmethod
    def _material_set(conn: sqlite3.Connection, project_id: str, material_set_id: str) -> dict[str, Any]:
        if not material_set_id:
            return {"materialSetId": "", "version": 0, "materials": [], "createdAtMs": None}
        row = conn.execute("SELECT payload_json FROM agent_lab_project_material_sets WHERE project_id=? AND material_set_id=?",
                           (project_id, material_set_id)).fetchone()
        if row is None:
            raise AgentLabProjectNotFound("此项目的材料版本不存在。")
        return json.loads(row[0])

    @classmethod
    def _public(cls, conn: sqlite3.Connection, project: dict[str, Any], knowledge: Mapping[str, Any] | None = None) -> dict[str, Any]:
        result = copy.deepcopy(project)
        result["materialSet"] = cls._material_set(conn, project["projectId"], project["materialSetId"])
        result["materialVersions"] = [{"materialSetId": row[0], "version": row[1], "createdAtMs": row[2]}
                                      for row in conn.execute("SELECT material_set_id,version,created_at_ms FROM agent_lab_project_material_sets WHERE project_id=? ORDER BY version DESC", (project["projectId"],))]
        result["artifacts"] = []
        result['applications'] = [{**json.loads(row[0]),'revision':row[1],'latestVersion':row[2],'activeVersion':row[3]}
                                  for row in conn.execute('SELECT payload_json,revision,latest_version,active_version FROM agent_lab_apps WHERE project_id=? ORDER BY updated_at_ms DESC',(project['projectId'],))]
        for row in conn.execute("SELECT payload_json FROM agent_lab_project_artifacts WHERE project_id=? ORDER BY created_at_ms,rowid", (project["projectId"],)):
            artifact = json.loads(row[0])
            result["artifacts"].append({key: value for key, value in artifact.items() if key != "content"})
        result.update(_project_work_summary(result, result["artifacts"], knowledge))
        return result

    @staticmethod
    def _artifact(conn: sqlite3.Connection, project_id: str, artifact_id: str, revision: int | None = None) -> dict[str, Any]:
        if revision is None:
            row = conn.execute("SELECT payload_json FROM agent_lab_project_artifacts WHERE project_id=? AND artifact_id=?", (project_id, artifact_id)).fetchone()
        else:
            row = conn.execute("SELECT v.payload_json FROM agent_lab_project_artifact_versions v JOIN agent_lab_project_artifacts a USING(artifact_id) WHERE a.project_id=? AND a.artifact_id=? AND v.revision=?", (project_id, artifact_id, revision)).fetchone()
        if row is None:
            raise AgentLabProjectNotFound("此项目的成果或版本不存在。")
        return json.loads(row[0])

    def read(self, project_id: str = "", *, material_set_id: str = "", artifact_id: str = "", artifact_revision: int | None = None,
             knowledge_resources: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        project_id = _text(project_id, "项目标识", optional=True)
        material_set_id = _text(material_set_id, "材料版本", optional=True)
        artifact_id = _text(artifact_id, "成果标识", optional=True)
        if artifact_revision is not None:
            artifact_revision = _integer(artifact_revision, 1)
            if not artifact_id:
                raise AgentLabProjectValidationError("读取成果版本需要指定成果。")
        if (material_set_id or artifact_id) and not project_id:
            raise AgentLabProjectValidationError("读取成果或材料版本需要指定优化项目。")
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN")
            items = []
            for row in conn.execute("SELECT payload_json FROM agent_lab_projects WHERE scope_id=? ORDER BY updated_at_ms DESC,rowid DESC", (self.scope_id,)):
                item = json.loads(row[0])
                artifact_row = conn.execute(
                    "SELECT payload_json FROM agent_lab_project_artifacts WHERE project_id=? ORDER BY updated_at_ms DESC,rowid DESC LIMIT 1",
                    (item["projectId"],),
                ).fetchone()
                latest_artifact = [json.loads(artifact_row[0])] if artifact_row is not None else []
                snapshot_id = (item.get("historyOrigin") or {}).get("snapshotArtifactId")
                if snapshot_id and not any(value.get("artifactId") == snapshot_id for value in latest_artifact):
                    snapshot_row = conn.execute(
                        "SELECT payload_json FROM agent_lab_project_artifacts WHERE project_id=? AND artifact_id=?",
                        (item["projectId"], snapshot_id),
                    ).fetchone()
                    if snapshot_row is not None:
                        latest_artifact.append(json.loads(snapshot_row[0]))
                summary = {key: item[key] for key in ("projectId", "revision", "title", "materialCount", "artifactCount", "guideSessionId", "createdAtMs", "updatedAtMs")}
                summary.update(_project_work_summary(item, latest_artifact, (knowledge_resources or {}).get(item["projectId"])))
                items.append(summary)
                if "historyOrigin" in item:
                    items[-1]["historyOrigin"] = item["historyOrigin"]
            project = self._public(conn, self._project(conn, project_id), (knowledge_resources or {}).get(project_id)) if project_id else None
            result = {"ok": True, "items": items, "project": project, "supportedViews": sorted(VIEWS)}
            if material_set_id:
                result["materialSet"] = self._material_set(conn, project_id, material_set_id)
            if artifact_id:
                result["artifact"] = self._artifact(conn, project_id, artifact_id, artifact_revision)
            return result

    def command(self, payload: Mapping[str, Any], *, history_import: Callable | None = None) -> dict[str, Any]:
        payload = _object(payload, {"action", "projectId", "expectedRevision", "clientRequestId", "input"}, "命令")
        action = _text(payload.get("action"), "操作")
        if action not in {"create", "import_history", "update_brief", "import_materials", "remove_materials", "publish_artifact", "set_workspace", "bind_execution", "ensure_guide", "prepare_app"}:
            raise AgentLabProjectValidationError("不支持此优化项目操作。")
        project_id = _text(payload.get("projectId", ""), "项目标识", optional=True)
        client_id = _text(payload.get("clientRequestId"), "请求标识")
        revision = _integer(payload.get("expectedRevision"))
        value = payload.get("input")
        if not isinstance(value, dict):
            raise AgentLabProjectValidationError("命令内容需要 JSON 对象。")
        request_json = _json({"action": action, "projectId": project_id, "expectedRevision": revision, "input": value})
        receipt_key = _json([self.scope_id, client_id])
        self.initialize()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            receipt = conn.execute("SELECT request_json,response_json FROM agent_lab_project_commands WHERE client_request_id=?", (receipt_key,)).fetchone()
            if receipt is not None:
                if receipt[0] != request_json:
                    raise AgentLabProjectConflict("此请求标识已用于不同内容，请保留修改后重新操作。")
                return {**json.loads(receipt[1]), "replayed": True}
            extra = {}
            if action in {"create", "import_history"}:
                if project_id or revision != 0:
                    raise AgentLabProjectValidationError("新项目需要版本 0，且不能指定已有项目。")
                if action == "create":
                    project = self._create(conn, value)
                else:
                    if history_import is None:
                        raise AgentLabProjectValidationError("已有实验来源尚未连接。")
                    prepared = history_import(value)
                    existing = next((json.loads(row[0]) for row in conn.execute(
                        "SELECT payload_json FROM agent_lab_projects WHERE scope_id=?", (self.scope_id,))
                        if json.loads(row[0]).get("historyOrigin", {}).get("sceneId") == prepared["sceneId"]), None)
                    if existing:
                        project = existing
                    else:
                        project = self._create(conn, prepared["project"], ensure_guide=False)
                        artifacts = [self._publish(conn, project, artifact) for artifact in prepared["artifacts"]]
                        project["historyOrigin"] = {"sceneId": prepared["sceneId"], "sourceHash": prepared["sourceHash"],
                            "experimentCount": prepared["experimentCount"], "importedAtMs": _now(),
                            "snapshotArtifactId": artifacts[-1]["artifactId"], "snapshotArtifactRevision": 1}
                        self._bind(conn, project, {"adapterId": "scene.trial", "input": {"sceneId": prepared["sceneId"]}})
                        project["workspace"]["layout"] = "focus"
            else:
                project = self._project(conn, project_id)
                if revision != project["revision"]:
                    raise AgentLabProjectConflict("优化项目已经更新，请刷新后保留你的修改重新操作。")
                extra = self._edit(conn, project, action, value)
                project["revision"] += 1
                project["updatedAtMs"] = _now()
            conn.execute("UPDATE agent_lab_projects SET revision=?,payload_json=?,updated_at_ms=? WHERE project_id=? AND scope_id=?",
                         (project["revision"], _json(project), project["updatedAtMs"], project["projectId"], self.scope_id))
            response = {"ok": True, "project": self._public(conn, project), "clientRequestId": client_id, "replayed": False, **extra}
            conn.execute("INSERT INTO agent_lab_project_commands(client_request_id,project_id,request_json,response_json,created_at_ms) VALUES(?,?,?,?,?)",
                         (receipt_key, project["projectId"], request_json, _json(response), _now()))
            return response

    def _create(self, conn: sqlite3.Connection, value: dict[str, Any], *, ensure_guide: bool = True) -> dict[str, Any]:
        value = _object(value, {"title", "description", "path", "materials"}, "新项目")
        description = _text(value.get("description"), "项目描述", 30_000)
        now = _now()
        project = {"schemaVersion": "rag-ime.agent-lab-project.v1", "projectId": f"lab-project-{uuid.uuid4().hex}",
                   "revision": 1, "title": _text(value.get("title", description[:80]), "项目名称", 500),
                   "description": description, "briefVersion": 1, "materialSetId": "", "materialCount": 0,
                   "artifactCount": 0, "intake": empty_intake(), "bindings": [], "guideSessionId": "",
                   "workspace": {"artifactOrder": [], "primaryArtifactId": "", "layout": "split"},
                   "workspaceBinding": None, "executionWorkspace": None, "createdAtMs": now, "updatedAtMs": now}
        from .project_directory import managed_workspace
        project['executionWorkspace'] = managed_workspace(self.db_path, self.scope_id, project)
        conn.execute("INSERT INTO agent_lab_projects(project_id,scope_id,revision,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)",
                     (project["projectId"], self.scope_id, 1, _json(project), now, now))
        self._save_brief(conn, project)
        if "path" in value or "materials" in value:
            self._import(conn, project, {key: value[key] for key in ("path", "materials") if key in value})
        if ensure_guide and self._create_guide is not None:
            self._ensure_guide(conn, project)
        return project

    @staticmethod
    def _save_brief(conn: sqlite3.Connection, project: dict[str, Any]) -> None:
        conn.execute("INSERT INTO agent_lab_project_brief_versions(project_id,version,payload_json,created_at_ms) VALUES(?,?,?,?)",
                     (project["projectId"], project["briefVersion"], _json({"description": project["description"]}), _now()))

    def _set_materials(self, conn: sqlite3.Connection, project: dict[str, Any], materials: list[dict[str, Any]]) -> None:
        try:
            validate_material_set(materials)
        except ValueError as exc:
            raise AgentLabProjectValidationError(str(exc)) from exc
        previous = self._material_set(conn, project["projectId"], project["materialSetId"])
        # Re-reading identical sources updates intake evidence without making a
        # content version or invalidating an existing standards binding.
        content_fields = ("sourceId", "title", "kind", "origin", "uri", "contentHash")
        comparable = lambda values: sorted(tuple(item[key] for key in content_fields) for item in values)
        if comparable(previous["materials"]) == comparable(materials):
            return
        value = {"materialSetId": f"lab-materials-{uuid.uuid4().hex}", "version": previous["version"] + 1,
                 "materials": materials, "createdAtMs": _now()}
        conn.execute("INSERT INTO agent_lab_project_material_sets(material_set_id,project_id,version,payload_json,created_at_ms) VALUES(?,?,?,?,?)",
                     (value["materialSetId"], project["projectId"], value["version"], _json(value), value["createdAtMs"]))
        project.update(materialSetId=value["materialSetId"], materialCount=len(materials))

    def _import(self, conn: sqlite3.Connection, project: dict[str, Any], value: dict[str, Any]) -> None:
        value = _object(value, {"path", "materials"}, "材料接入")
        if len(value) != 1:
            raise AgentLabProjectValidationError("一次接入请选择本地路径或文本材料。")
        try:
            incoming, intake = read_local_materials(value["path"]) if "path" in value else read_inline_materials(value["materials"])
        except ValueError as exc:
            raise AgentLabProjectValidationError(str(exc)) from exc
        project["intake"] = intake
        if intake["resolvedPath"]:
            project["workspaceBinding"] = {"kind": "local", "path": intake["resolvedPath"], "pathKind": intake["pathKind"], "checkedAtMs": intake["checkedAtMs"]}
        if incoming:
            previous = self._material_set(conn, project["projectId"], project["materialSetId"])
            combined = {item["sourceId"]: item for item in previous["materials"]}
            combined.update({item["sourceId"]: item for item in incoming})
            self._set_materials(conn, project, list(combined.values()))

    def _edit(self, conn: sqlite3.Connection, project: dict[str, Any], action: str, value: dict[str, Any]) -> dict[str, Any]:
        if action == "update_brief":
            value = _object(value, {"title", "description"}, "项目更新")
            if not value:
                raise AgentLabProjectValidationError("请提供需要更新的项目内容。")
            if "title" in value:
                project["title"] = _text(value["title"], "项目名称", 500)
            if "description" in value:
                description = _text(value["description"], "项目描述", 30_000)
                if description != project["description"]:
                    project.update(description=description, briefVersion=project["briefVersion"] + 1)
                    self._save_brief(conn, project)
        elif action == "import_materials":
            self._import(conn, project, value)
        elif action == "remove_materials":
            value = _object(value, {"sourceIds"}, "材料移除")
            ids = set(_strings(value.get("sourceIds"), "材料标识"))
            previous = self._material_set(conn, project["projectId"], project["materialSetId"])
            if not ids or ids - {item["sourceId"] for item in previous["materials"]}:
                raise AgentLabProjectValidationError("请选择此项目中的现有材料。")
            remaining = [item for item in previous["materials"] if item["sourceId"] not in ids]
            self._set_materials(conn, project, remaining)
            if not remaining:
                project["intake"] = empty_intake()
        elif action == "publish_artifact":
            return {"artifact": self._publish(conn, project, value)}
        elif action == "set_workspace":
            value = _object(value, {"artifactOrder", "primaryArtifactId", "layout"}, "项目工作区")
            ids = _strings(value.get("artifactOrder"), "成果顺序", maximum=500)
            existing = {row[0] for row in conn.execute("SELECT artifact_id FROM agent_lab_project_artifacts WHERE project_id=?", (project["projectId"],))}
            if set(ids) != existing or len(ids) != len(set(ids)):
                raise AgentLabProjectValidationError("工作区顺序需要保留此项目的全部现有成果。")
            primary = _text(value.get("primaryArtifactId", ""), "主要成果", optional=True)
            if (ids and primary not in existing) or (not ids and primary):
                raise AgentLabProjectValidationError("主要成果需要属于此项目。")
            layout = _text(value.get("layout", project["workspace"]["layout"]), "布局")
            if layout not in {"split", "focus"}:
                raise AgentLabProjectValidationError("不支持此容器布局。")
            project["workspace"] = {"artifactOrder": ids, "primaryArtifactId": primary, "layout": layout}
        elif action == "bind_execution":
            return {"binding": self._bind(conn, project, value)}
        elif action == 'prepare_app':
            if self._prepare_app is None: raise AgentLabProjectUnavailable()
            return {'application':self._prepare_app(conn,self._public(conn,project),value)}
        else:
            _object(value, set(), "引导会话")
            self._ensure_guide(conn, project)
        return {}

    def _ensure_guide(self, conn: sqlite3.Connection, project: dict[str, Any]) -> None:
        if project["guideSessionId"]:
            return
        if self._create_guide is None:
            raise AgentLabProjectUnavailable()
        result = self._create_guide(conn, self._public(conn, project))
        if isinstance(result, str):
            project["guideSessionId"] = _text(result, "引导会话标识")
            return
        result = _object(result, {"sessionId", "workspace"}, "项目执行环境")
        workspace = _object(result.get("workspace"), {"kind", "path", "createdAtMs"}, "项目执行目录")
        project["guideSessionId"] = _text(result.get("sessionId"), "引导会话标识")
        project["executionWorkspace"] = {"kind": _text(workspace.get("kind"), "环境类型"),
                                         "path": _text(workspace.get("path"), "执行目录", 4096),
                                         "createdAtMs": _integer(workspace.get("createdAtMs"))}

    def _publish(self, conn: sqlite3.Connection, project: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        value = _object(value, _ARTIFACT_FIELDS | {"artifactId", "expectedArtifactRevision"}, "成果发布")
        artifact_id = _text(value.get("artifactId", ""), "成果标识", optional=True)
        expected = _integer(value.get("expectedArtifactRevision", 0))
        previous = self._artifact(conn, project["projectId"], artifact_id) if artifact_id else None
        if expected != (previous["revision"] if previous else 0):
            raise AgentLabProjectConflict("成果版本已经更新，请保留草稿并核对新版本。")
        body = {key: previous[key] for key in _ARTIFACT_FIELDS} if previous else {}
        body.update({key: value[key] for key in _ARTIFACT_FIELDS if key in value})
        try:
            body = validate_artifact(body)
        except ValueError as exc:
            raise AgentLabProjectValidationError(str(exc)) from exc
        now = _now()
        artifact = {**body, "artifactId": artifact_id or f"lab-artifact-{uuid.uuid4().hex}", "revision": expected + 1,
                    "createdAtMs": previous["createdAtMs"] if previous else now, "updatedAtMs": now}
        if previous:
            conn.execute("UPDATE agent_lab_project_artifacts SET revision=?,payload_json=?,updated_at_ms=? WHERE artifact_id=? AND project_id=?",
                         (artifact["revision"], _json(artifact), now, artifact_id, project["projectId"]))
        else:
            if project["artifactCount"] >= 500:
                raise AgentLabProjectValidationError("当前项目已有 500 份成果，请将大型资料保存为文件或拆分项目。")
            conn.execute("INSERT INTO agent_lab_project_artifacts(artifact_id,project_id,revision,payload_json,created_at_ms,updated_at_ms) VALUES(?,?,?,?,?,?)",
                         (artifact["artifactId"], project["projectId"], 1, _json(artifact), now, now))
            project["artifactCount"] += 1
            project["workspace"]["artifactOrder"].append(artifact["artifactId"])
            if not project["workspace"]["primaryArtifactId"]:
                project["workspace"]["primaryArtifactId"] = artifact["artifactId"]
        conn.execute("INSERT INTO agent_lab_project_artifact_versions(artifact_id,revision,payload_json,created_at_ms) VALUES(?,?,?,?)",
                     (artifact["artifactId"], artifact["revision"], _json(artifact), now))
        return artifact

    def _bind(self, conn: sqlite3.Connection, project: dict[str, Any], value: dict[str, Any]) -> dict[str, Any]:
        value = _object(value, {"adapterId", "artifactId", "artifactRevision", "input"}, "执行绑定")
        adapter = _text(value.get("adapterId"), "执行适配器")
        artifact_id = _text(value.get("artifactId", ""), "成果标识", optional=True)
        artifact_revision = _integer(value.get("artifactRevision", 0))
        artifact = self._artifact(conn, project["projectId"], artifact_id, artifact_revision) if artifact_id else None
        if not artifact_id and artifact_revision:
            raise AgentLabProjectValidationError("成果版本需要对应成果标识。")
        parameters = value.get("input", {})
        if not isinstance(parameters, dict) or len(_json(parameters)) > 100_000:
            raise AgentLabProjectValidationError("执行绑定参数无效。")
        request = {"adapterId": adapter, "artifactId": artifact_id, "artifactRevision": artifact_revision,
                   "input": parameters, "materialSetId": project["materialSetId"], "briefVersion": project["briefVersion"]}
        key = hashlib.sha256(_json(request).encode()).hexdigest()
        existing = next((item for item in project["bindings"] if item["requestKey"] == key), None)
        if existing:
            return copy.deepcopy(existing)
        if self._bind_execution is None:
            raise AgentLabProjectUnavailable()
        result = self._bind_execution(conn, self._public(conn, project), {**request, "artifact": artifact})
        result = _object(result, {"ownerRef", "summary"}, "执行适配器回执")
        owner = _object(result.get("ownerRef"), {"kind", "id"}, "执行所有者")
        owner = {key: _text(owner.get(key), "执行所有者") for key in ("kind", "id")}
        binding = {**request, "bindingId": f"lab-binding-{uuid.uuid4().hex}", "requestKey": key,
                   "ownerRef": owner, "summary": _text(result.get("summary", ""), "绑定说明", 5000, optional=True), "createdAtMs": _now()}
        project["bindings"].append(binding)
        return copy.deepcopy(binding)
