"""Scene-scoped recipe selection for future Enterprise RAG Validation runs.

This owner records a selection; it does not execute an experiment, edit source,
change AgentConfiguration, or rewrite any existing Session/run snapshot.
"""

from __future__ import annotations

import copy
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path

from ..db import apply_database_migrations, sqlite_connection

__all__ = [
    "AgentLabSceneRecipeConflict",
    "AgentLabSceneRecipeServiceUnavailable",
    "AgentLabSceneRecipeStore",
    "AgentLabSceneRecipeUnavailable",
    "ENTERPRISE_RAG_VALIDATION_SCENE_ID",
    "validate_scene_recipe_binding",
]

ENTERPRISE_RAG_VALIDATION_SCENE_ID = "agent-lab.enterprise-rag.validation"
_EXPERIMENT_ID = "enterprise-rag.luna-prompt-v4-standard-r6.v1"
_CANDIDATE_RUN_ID = "enterprise-rag-luna-max-coverage-balanced-v4-20260904-r4"
_EFFECT_SCOPE = "future_validation_runs"

# Model/Prompt defaults are pinned from run_rag_agent_ablation.py's incumbent.
# The flags below belong to this Validation scene, not the global CLI defaults.
_VALIDATION_RECIPE = {
    "provider": "openai-codex",
    "model": "gpt-5.6-sol",
    "thinkingLevel": "max",
    "promptProfile": "incumbent",
    "promptContractVersion": "rag-agent-evidence-state-budget-routing-v19",
    "agenticSupplementalLimit": 6,
    "answerOnly": True,
    "developmentOnly": True,
    "split": "validation",
    "candidateAware": True,
    "unbiasedPromotionClaimAllowed": False,
}
_BUILTIN_VERSION = {
    "schemaVersion": "rag-ime.agent-lab-scene-recipe-version.v1",
    "sceneId": ENTERPRISE_RAG_VALIDATION_SCENE_ID,
    "versionId": "enterprise-rag.validation.incumbent.v1",
    "title": "本场景内置默认（incumbent）",
    "origin": "runner_builtin",
    "recipe": _VALIDATION_RECIPE,
    "sourceExperimentId": "",
    "sourceCandidateRunId": "",
}
# Recipe versions are immutable evidence. Keep v1 and the registered candidate
# byte-compatible; a new default has its own literal, frozen version identity.
_CURRENT_BUILTIN_VERSION = {
    **_BUILTIN_VERSION,
    "versionId": "enterprise-rag.validation.incumbent.v2",
    "title": "本场景内置默认（GPT-6.1 Sol）",
    "recipe": {**_VALIDATION_RECIPE, "model": "gpt-6.1-sol"},
}
_CANDIDATE_VERSION = {
    "schemaVersion": "rag-ime.agent-lab-scene-recipe-version.v1",
    "sceneId": ENTERPRISE_RAG_VALIDATION_SCENE_ID,
    "versionId": "enterprise-rag.validation.luna-prompt-v4-r6.v1",
    "title": "Luna Prompt-v4 · Standard r6 Validation",
    "origin": "registered_candidate",
    "recipe": {
        **_VALIDATION_RECIPE,
        "model": "gpt-5.6-luna",
        "promptProfile": "coverage-balanced-evidence-gate-v4",
    },
    "sourceExperimentId": _EXPERIMENT_ID,
    "sourceCandidateRunId": _CANDIDATE_RUN_ID,
}


class AgentLabSceneRecipeConflict(RuntimeError):
    http_status = 409

    def __init__(self, reason_code: str, message: str, *, current_revision: int) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.current_revision = current_revision

    def response_payload(self) -> dict[str, object]:
        return {
            "ok": False, "code": "AGENT_LAB_SCENE_RECIPE_CONFLICT",
            "reasonCode": self.reason_code, "error": str(self),
            "currentRevision": self.current_revision,
        }


class AgentLabSceneRecipeUnavailable(RuntimeError):
    http_status = 422

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code

    def response_payload(self) -> dict[str, object]:
        return {
            "ok": False, "code": "AGENT_LAB_SCENE_RECIPE_UNAVAILABLE",
            "reasonCode": self.reason_code, "error": str(self),
        }


class AgentLabSceneRecipeServiceUnavailable(RuntimeError):
    http_status = 503

    def __init__(self, reason_code: str) -> None:
        super().__init__("场景配置服务暂不可用，请稍后重试。")
        self.reason_code = reason_code

    def response_payload(self) -> dict[str, object]:
        return {
            "ok": False, "code": "AGENT_LAB_SCENE_RECIPE_SERVICE_UNAVAILABLE",
            "reasonCode": self.reason_code, "error": str(self),
        }


class AgentLabSceneRecipeStore:
    """Immutable versions plus an atomic, append-only selection event stream."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        experiment_provider: Callable[[], Sequence[Mapping[str, object]]] | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self._experiment_provider = experiment_provider or self._public_experiments
        self._initialize_lock = threading.Lock()
        self._initialized_version: int | None = None

    @contextmanager
    def _connection(self, *, foreign_keys: bool = False) -> Iterator[sqlite3.Connection]:
        try:
            with sqlite_connection(self.db_path, row_factory=sqlite3.Row, foreign_keys=foreign_keys) as conn:
                yield conn
        except (sqlite3.Error, OSError) as exc:
            # This includes commit failures: callers must keep the request ID
            # to recover a receipt if the mutation's outcome is still unknown.
            raise AgentLabSceneRecipeServiceUnavailable("storage_unavailable") from exc

    def initialize(self) -> int:
        with self._initialize_lock:
            if self._initialized_version is not None:
                return self._initialized_version
            try:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise AgentLabSceneRecipeServiceUnavailable("storage_unavailable") from exc
            with self._connection(foreign_keys=True) as conn:
                version = apply_database_migrations(conn).current_version
                conn.execute("BEGIN IMMEDIATE")
                for payload in (_BUILTIN_VERSION, _CURRENT_BUILTIN_VERSION, _CANDIDATE_VERSION):
                    encoded = _json(payload)
                    existing = conn.execute(
                        "SELECT payload_json FROM agent_lab_scene_recipe_versions "
                        "WHERE scene_id = ? AND version_id = ?",
                        (payload["sceneId"], payload["versionId"]),
                    ).fetchone()
                    if existing is not None:
                        if existing[0] != encoded:
                            raise RuntimeError("已登记的场景 recipe 内容发生变化，需要使用新的版本号。")
                    else:
                        conn.execute(
                            "INSERT INTO agent_lab_scene_recipe_versions "
                            "(scene_id, version_id, payload_json) VALUES (?, ?, ?)",
                            (payload["sceneId"], payload["versionId"], encoded),
                        )
            self._initialized_version = version
            return version

    def get_state(self, scene_id: str, *, experiment_id: str = "") -> dict[str, object]:
        _require_scene(scene_id)
        self.initialize()
        candidate = self._candidate(experiment_id or _EXPERIMENT_ID)
        with self._connection() as conn:
            return self._state(conn, scene_id, self._latest(conn, scene_id), candidate)

    def resolve_for_run(self, scene_id: str) -> dict[str, object]:
        """Return a detached binding to pin once when admitting a future run."""
        _require_scene(scene_id)
        self.initialize()
        with self._connection() as conn:
            latest = self._latest(conn, scene_id)
            active = self._active_version(conn, scene_id, latest)
            return {
                "schemaVersion": "rag-ime.agent-lab-scene-recipe-binding.v1",
                "sceneId": scene_id,
                "revision": int(latest["revision"]) if latest else 0,
                "versionId": active["versionId"],
                "recipe": active["recipe"],
                "effectScope": _EFFECT_SCOPE,
            }

    def apply_candidate(
        self, scene_id: str, *, experiment_id: str,
        expected_revision: int, client_request_id: str,
    ) -> dict[str, object]:
        return self._mutate(
            scene_id, operation="apply", experiment_id=experiment_id,
            expected_revision=expected_revision, client_request_id=client_request_id,
        )

    def rollback(
        self, scene_id: str, *, expected_revision: int, client_request_id: str,
    ) -> dict[str, object]:
        return self._mutate(
            scene_id, operation="rollback", experiment_id="",
            expected_revision=expected_revision, client_request_id=client_request_id,
        )

    def _mutate(
        self, scene_id: str, *, operation: str, experiment_id: str,
        expected_revision: int, client_request_id: str,
    ) -> dict[str, object]:
        _require_scene(scene_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expectedRevision 必须是非负整数。")
        if (
            not isinstance(client_request_id, str)
            or not client_request_id.strip()
            or len(client_request_id) > 240
            or any(ord(character) < 32 for character in client_request_id)
        ):
            raise ValueError("clientRequestId 必须是有效的请求标识。")
        self.initialize()
        request_json = _json({
            "operation": operation, "experimentId": experiment_id,
            "expectedRevision": expected_revision,
        })
        # An acknowledged request remains replayable even if the public
        # catalog later changes or is unavailable. Check again inside CAS for
        # concurrent duplicate clicks. Provider reads stay outside the write
        # transaction because the public projection also reads this database.
        with self._connection() as conn:
            replay = self._replay(conn, scene_id, client_request_id, request_json)
            if replay is not None:
                return replay
        # Rollback depends only on immutable history. Its receipt does not
        # claim that public candidate eligibility has just been rechecked.
        candidate = self._candidate(experiment_id) if operation == "apply" else {
            "available": False, "reasonCode": "not_checked",
            "reason": "回滚已完成，请刷新查看候选实验状态。",
            "experimentId": _EXPERIMENT_ID, "version": copy.deepcopy(_CANDIDATE_VERSION),
        }
        with self._connection(foreign_keys=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = self._replay(conn, scene_id, client_request_id, request_json)
            if replay is not None:
                return replay
            latest = self._latest(conn, scene_id)
            current_revision = int(latest["revision"]) if latest else 0
            if current_revision != expected_revision:
                raise AgentLabSceneRecipeConflict(
                    "stale_revision", "场景选择已变化，请刷新后再操作。",
                    current_revision=current_revision,
                )
            active = self._active_version(conn, scene_id, latest)
            if operation == "apply":
                if not candidate["available"]:
                    raise AgentLabSceneRecipeUnavailable(str(candidate["reasonCode"]), str(candidate["reason"]))
                target = self._version(conn, scene_id, str(_CANDIDATE_VERSION["versionId"]))
                if target["versionId"] == active["versionId"]:
                    raise AgentLabSceneRecipeUnavailable("already_active", "本场景已选择这个 recipe。")
                rollback_revision: int | None = current_revision
            else:
                if latest is None or latest["rollback_revision"] is None:
                    raise AgentLabSceneRecipeUnavailable("no_previous_version", "本场景没有可回滚的上一版。")
                prior = self._at_revision(conn, scene_id, int(latest["rollback_revision"]))
                target = self._active_version(conn, scene_id, prior) if prior else self._initial_version(conn, scene_id)
                rollback_revision = int(prior["rollback_revision"]) if prior and prior["rollback_revision"] is not None else None
            revision = current_revision + 1
            created_at_ms = int(time.time() * 1000)
            event = {
                "schemaVersion": "rag-ime.agent-lab-scene-recipe-event.v1",
                "eventId": "lab-recipe:" + uuid.uuid4().hex,
                "sceneId": scene_id, "revision": revision, "operation": operation,
                "clientRequestId": client_request_id,
                "fromVersionId": active["versionId"], "versionId": target["versionId"],
                "sourceExperimentId": target["sourceExperimentId"],
                "sourceCandidateRunId": target["sourceCandidateRunId"],
                "sourceExperimentRevision": candidate.get("sourceExperimentRevision", "") if operation == "apply" else "",
                "createdAtMs": created_at_ms, "effectScope": _EFFECT_SCOPE,
            }
            next_row = {
                "revision": revision, "version_id": target["versionId"],
                "rollback_revision": rollback_revision, "event_json": _json(event),
            }
            result = {
                **self._state(conn, scene_id, next_row, candidate),
                "event": event, "replayed": False,
            }
            conn.execute(
                "INSERT INTO agent_lab_scene_recipe_events "
                "(scene_id, revision, event_id, client_request_id, operation, request_json, "
                "from_version_id, version_id, rollback_revision, event_json, response_json, created_at_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (scene_id, revision, event["eventId"], client_request_id, operation, request_json,
                 active["versionId"], target["versionId"], rollback_revision,
                 _json(event), _json(result), created_at_ms),
            )
            return result

    def _public_experiments(self) -> Sequence[Mapping[str, object]]:
        from ..eval_lab import EvalLabProjection

        ledger = Path(__file__).resolve().parents[2] / "eval/interview-metrics/agent-experiments.v1.json"
        # Use the same public owner as the Lab page, including its fresh ledger
        # overlay. A stale imported DB alone must not hide the current candidate.
        return EvalLabProjection(self.db_path, source_ledger_path=ledger).list_runs()["experiments"]

    def _candidate(self, experiment_id: str) -> dict[str, object]:
        result: dict[str, object] = {
            "available": False, "reasonCode": "", "reason": "",
            "experimentId": experiment_id, "version": None,
        }

        def unavailable(code: str, reason: str) -> dict[str, object]:
            return {**result, "reasonCode": code, "reason": reason}

        if experiment_id != _EXPERIMENT_ID:
            return unavailable("unsupported_experiment", "这个实验尚未登记可应用的场景 recipe。")
        result["version"] = copy.deepcopy(_CANDIDATE_VERSION)
        try:
            items = self._experiment_provider()
            matches = [item for item in items if isinstance(item, Mapping) and item.get("experimentId") == experiment_id]
        except Exception as exc:
            raise AgentLabSceneRecipeServiceUnavailable("experiment_source_unavailable") from exc
        if len(matches) != 1:
            return unavailable("experiment_unavailable", "当前公开实验中没有唯一匹配的候选。")
        experiment = matches[0]
        if experiment.get("projectionState") != "current":
            return unavailable("experiment_not_current", "这个实验已是历史记录，请选择当前实验。")
        comparison = experiment.get("comparison")
        if experiment.get("status") != "kept" or not isinstance(comparison, Mapping) or str(comparison.get("decision") or "").casefold() != "keep":
            return unavailable("experiment_not_kept", "这个实验尚未得到 Keep 结论，不能用于后续场景。")
        candidate = experiment.get("candidate")
        if not isinstance(candidate, Mapping) or candidate.get("runId") != _CANDIDATE_RUN_ID:
            return unavailable("candidate_identity_mismatch", "公开实验的候选运行与已登记 recipe 不匹配。")
        dataset = experiment.get("dataset")
        if not isinstance(dataset, Mapping) or dataset.get("split") != "validation":
            return unavailable("validation_required", "这个 recipe 仅支持 Validation 场景。")
        return {**result, "available": True, "sourceExperimentRevision": str(experiment.get("revisionSha256") or "")}

    def _replay(self, conn: sqlite3.Connection, scene_id: str, request_id: str, request_json: str) -> dict[str, object] | None:
        row = conn.execute(
            "SELECT request_json, response_json FROM agent_lab_scene_recipe_events "
            "WHERE scene_id = ? AND client_request_id = ?", (scene_id, request_id),
        ).fetchone()
        if row is None:
            return None
        if row["request_json"] != request_json:
            latest = self._latest(conn, scene_id)
            raise AgentLabSceneRecipeConflict(
                "request_id_reused", "这个请求标识已用于另一项操作，请刷新后再操作。",
                current_revision=int(latest["revision"]) if latest else 0,
            )
        return {**json.loads(row["response_json"]), "replayed": True}

    @staticmethod
    def _latest(conn: sqlite3.Connection, scene_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM agent_lab_scene_recipe_events WHERE scene_id = ? ORDER BY revision DESC LIMIT 1",
            (scene_id,),
        ).fetchone()

    @staticmethod
    def _at_revision(conn: sqlite3.Connection, scene_id: str, revision: int) -> sqlite3.Row | None:
        if revision == 0:
            return None
        row = conn.execute(
            "SELECT * FROM agent_lab_scene_recipe_events WHERE scene_id = ? AND revision = ?", (scene_id, revision),
        ).fetchone()
        if row is None:
            raise RuntimeError("场景 recipe 的历史版本引用不存在。")
        return row

    @staticmethod
    def _version(conn: sqlite3.Connection, scene_id: str, version_id: str) -> dict[str, object]:
        row = conn.execute(
            "SELECT payload_json FROM agent_lab_scene_recipe_versions WHERE scene_id = ? AND version_id = ?",
            (scene_id, version_id),
        ).fetchone()
        if row is None:
            raise RuntimeError("场景 recipe 版本不存在。")
        return json.loads(row["payload_json"])

    def _active_version(self, conn: sqlite3.Connection, scene_id: str, row: Mapping[str, object] | sqlite3.Row | None) -> dict[str, object]:
        return self._version(conn, scene_id, str(row["version_id"] if row else _CURRENT_BUILTIN_VERSION["versionId"]))

    def _initial_version(self, conn: sqlite3.Connection, scene_id: str, *, fallback_version_id: str = "") -> dict[str, object]:
        first = conn.execute(
            "SELECT from_version_id FROM agent_lab_scene_recipe_events WHERE scene_id = ? ORDER BY revision ASC LIMIT 1",
            (scene_id,),
        ).fetchone()
        version_id = str(first["from_version_id"]) if first is not None else fallback_version_id
        if not version_id:
            raise RuntimeError("场景 recipe 的初始历史版本引用不存在。")
        return self._version(conn, scene_id, version_id)

    def _state(self, conn: sqlite3.Connection, scene_id: str, latest: Mapping[str, object] | sqlite3.Row | None, candidate: Mapping[str, object]) -> dict[str, object]:
        active = self._active_version(conn, scene_id, latest)
        previous = None
        if latest is not None and latest["rollback_revision"] is not None:
            prior = self._at_revision(conn, scene_id, int(latest["rollback_revision"]))
            previous = self._active_version(conn, scene_id, prior) if prior else self._initial_version(
                conn, scene_id, fallback_version_id=str(json.loads(str(latest["event_json"]))["fromVersionId"]),
            )
        candidate = copy.deepcopy(dict(candidate))
        if candidate["available"] and active["versionId"] == _CANDIDATE_VERSION["versionId"]:
            candidate.update(available=False, reasonCode="already_active", reason="本场景已选择这个 recipe。")
        return {
            "schemaVersion": "rag-ime.agent-lab-scene-recipe-state.v1", "ok": True,
            "sceneId": scene_id, "revision": int(latest["revision"]) if latest else 0,
            "activeVersion": active, "previousVersion": previous,
            "rollbackAvailable": previous is not None, "candidate": candidate,
            "lastEvent": json.loads(str(latest["event_json"])) if latest else None,
            "effectScope": _EFFECT_SCOPE,
        }


def _require_scene(scene_id: str) -> None:
    if scene_id != ENTERPRISE_RAG_VALIDATION_SCENE_ID:
        raise AgentLabSceneRecipeUnavailable("unsupported_scene", "这个场景尚不支持 recipe 应用与回滚。")


def validate_scene_recipe_binding(value: Mapping[str, object]) -> dict[str, object]:
    """Validate and detach a run snapshot without opening the live Store."""
    if not isinstance(value, Mapping) or set(value) != {
        "schemaVersion", "sceneId", "revision", "versionId", "recipe", "effectScope",
    }:
        raise ValueError("scene recipe binding fields are invalid")
    if (
        value.get("schemaVersion") != "rag-ime.agent-lab-scene-recipe-binding.v1"
        or value.get("sceneId") != ENTERPRISE_RAG_VALIDATION_SCENE_ID
        or value.get("effectScope") != _EFFECT_SCOPE
        or type(value.get("revision")) is not int
        or value["revision"] < 0
    ):
        raise ValueError("scene recipe binding identity is invalid")
    version = next(
        (item for item in (_BUILTIN_VERSION, _CURRENT_BUILTIN_VERSION, _CANDIDATE_VERSION) if item["versionId"] == value.get("versionId")),
        None,
    )
    recipe = value.get("recipe")
    if version is None or not isinstance(recipe, Mapping) or _json(recipe) != _json(version["recipe"]):
        raise ValueError("scene recipe does not match a registered Validation version")
    # Canonical serialization also rejects non-JSON values and detaches the
    # caller's object. No database or current application revision is consulted.
    return json.loads(_json(value))


def _json(value: Mapping[str, object]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
