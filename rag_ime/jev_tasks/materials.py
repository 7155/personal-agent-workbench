"""Task-scoped context through existing resource and permission owners.

No file registry, summary store, compaction loop or model loop lives here.
Supplied summaries must be existing owner records with exact source revisions.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import unquote, urlsplit

from rag_ime.agent_execution_policy import read_only_policy_active, unrestricted_workspace_policy_active
from rag_ime.agent_workspace_roots import system_wide_workspace_roots
from rag_ime.pi.public import inspectable_tool_result

from .context import ContextManifest, Material, build_manifest, choose_reading_depth
from .types import DecisionUnavailable, GraphConflict, GraphError, canonical, digest, text

_MODES = frozenset({"inline", "read_exact", "summary", "reference", "omit"})
_REF_FIELDS = frozenset({"sourceRef", "required", "selection", "selector", "externalAllowed",
                         "retrievalReceiptId", "expectedHash"})
_READ_BYTES = 96000
_ARTIFACT_FILE_BYTES = 2 * 1024 * 1024
_ARTIFACT_TOTAL_BYTES = 8 * 1024 * 1024
_ARTIFACT_SHA256_SUFFIX = re.compile(r"#sha256:([0-9a-fA-F]{64})$")
# Only task execution receipts are shared. Room/control/discovery and personal
# memory tool results may contain other participants' context, not task proof.
_EVIDENCE_TOOLS = frozenset({"read", "bash", "write", "edit", "grep", "find", "ls",
    "workspace_read", "workspace_list", "workspace_search", "workspace_shell",
    "workspace_write", "workspace_edit", "workspace_patch", "workspace_lsp", "workspace_job", "browser"})
_BROWSER_EVIDENCE_ACTIONS = frozenset({"run", "navigate", "back", "forward", "click", "type",
    "scroll", "wait", "screenshot"})
_BROWSER_RECEIPT_FIELDS = frozenset({"schemaVersion", "ok", "commandId", "action", "status",
    "durationMs", "failureReason", "summary"})
_BROWSER_RESULT_FIELDS = frozenset({"ok", "summary", "stdout", "stderr", "exitCode", "timedOut",
    "failureReason", "error", "url", "title", "tabId", "snapshotId", "imagePath", "width", "height",
    "truncated", "outputLimited"})
_EVIDENCE_RESULT_FIELDS = frozenset({"schemaVersion", "summary", "path", "relativePath", "resourceRef",
    "resourceRevision", "readOrigin", "startLine", "endLine", "nextLineOffset", "nextOffset",
    "offset", "size", "contentBytes", "truncated", "receipt", "output", "stdout", "stderr",
    "exitCode", "timedOut", "outputLimited", "outputBytes", "terminal", "retryable", "terminalReason"})
# Full scoped receipts live in the existing paginated media owner, not the
# bounded inline context. Stay within that owner's text attachment limit.
_EVIDENCE_ARCHIVE_BYTES = _ARTIFACT_FILE_BYTES
_EVIDENCE_RECORD_BYTES = 512000
_COMMAND_RECEIPT_SEMANTICS = {
    "schemaVersion": "rag-ime.workspace-command-receipt.v1",
    "owner": "WorkspaceHarness._run_sandboxed",
    "sideEffectsAssessed": False,
    "fields": {
        "mutationApplied": "owner计算值为 succeeded and not sourceReadOnly；succeeded表示exitCode==0且未超时、输出未超限。"
            "这是执行完成与隔离模式的组合标记，不是文件差异检测；true不证明实际写入，false也不证明未发生部分写入。",
        "sourceReadOnly": "来自Session的read_only_policy_active执行策略，表示源工作区只读隔离模式；"
            "false表示允许写入，不表示命令实际写入。本字段不是对命令语义或文件差异的检测。",
        "networkAllowed": "来自prepared.allow_network，表示此次命令获得的网络权限；true不是已发起网络调用的证据。",
        "temporaryWritesDiscarded": "owner直接使用sourceReadOnly作为此值，描述只读隔离模式的临时写入处理；"
            "不表示检测到了临时写入，也不是文件差异证据。",
    },
    "requestFields": {"allowNetwork": "请求的网络权限开关，不是实际网络访问日志。"},
    "limits": "这份说明只解释字段，不判断命令是否只读或无副作用。仍须独立核验真实命令、输出、具体成果，"
        "以及验收所需的实际变更或调用证据；不得仅凭这些flags宣告有或无文件写入、网络访问。",
}


def _bounded_evidence(value, maximum):
    encoded = canonical(value).encode("utf-8")
    if len(encoded) <= maximum:
        return value
    return {"truncated": True, "originalBytes": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest(),
            "previewJson": encoded[:max(0, maximum - 400)].decode("utf-8", errors="ignore")}


def _source_truncated(value):
    if isinstance(value, Mapping):
        return any(value.get(key) is True for key in ("truncated", "outputLimited", "modelResultTruncated")) or any(
            _source_truncated(item) for item in value.values())
    if isinstance(value, list):
        return any(_source_truncated(item) for item in value)
    # Pi's native bridge puts modelVisibleResult in content[].text while its
    # details retain the unprojected object. Recognize only that bounded owner
    # envelope; arbitrary output text is not a completeness declaration.
    if isinstance(value, str) and len(value) <= 100000 and value.lstrip().startswith("{"):
        try:
            envelope = json.loads(value)
        except ValueError:
            return False
        if isinstance(envelope, Mapping) and str(envelope.get("evidenceHandle", "")).startswith("tool-result://sha256/"):
            return envelope.get("truncated") is True or envelope.get("modelResultTruncated") is True
    return False


def context_reference(value: object) -> dict[str, object]:
    """Normalize a scoped reference without treating it as authorization."""
    if isinstance(value, str):
        value = {"sourceRef": value}
    if not isinstance(value, Mapping) or set(value) - _REF_FIELDS:
        raise GraphError("invalid task context reference")
    result = dict(value)
    text(result.get("sourceRef"), "source reference", 2000)
    for name in ("required", "externalAllowed"):
        if name in result and not isinstance(result[name], bool):
            raise GraphError("invalid context reference policy")
    if "selection" in result and (not isinstance(result["selection"], str) or result["selection"] not in _MODES):
        raise GraphError("invalid context reading selection")
    for name in ("selector", "retrievalReceiptId", "expectedHash"):
        if name in result:
            text(result[name], name, 2000)
    return result


class JevMaterialService:
    def __init__(self, service, *, decider=None):
        self.service = service
        self.decider = decider

    @staticmethod
    def _workspace_artifact_path(ref: str) -> str | None:
        """Recognize file-shaped delivery refs without interpreting prose/IDs as paths.

        Relative directory paths and bare filenames need an extension; `./`
        and `../` are explicit paths even without one. The WorkspaceHarness
        remains the authority for resolving each candidate under Session roots.
        """
        # A declared revision annotates a path; it is not part of its filename.
        # Other fragments remain untouched and are not interpreted as hashes.
        ref = _ARTIFACT_SHA256_SUFFIX.sub("", ref)
        # BrowserControl owns these immutable snapshot resources. Its public
        # imagePath is an HTTP route, not an absolute workspace filename.
        if re.fullmatch(r"/(?:api|control/v1)/browser/snapshots/snap_[0-9a-f]{32}/image", ref):
            return None
        if ref.startswith("workspace:"):
            return ref[len("workspace:"):]
        parsed = urlsplit(ref)
        if (parsed.scheme == "file" and parsed.netloc in {"", "localhost"}
            and not parsed.query and not parsed.fragment):
            return unquote(parsed.path)
        if parsed.scheme or "\x00" in ref:
            return None
        if ref.startswith(("/", "./", "../")):
            return ref
        if (re.fullmatch(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.[A-Za-z0-9]{1,12}", ref)
            and not ref.startswith("..")):
            return ref
        return None

    def artifact_revisions(self, snapshot, task, session_id: str) -> list[dict[str, str]]:
        """Read bounded bytes under the verifier Session's workspace owner scope.

        Opaque refs remain evidence pointers. A declared mutable file whose
        exact bytes cannot be read is unknown, never silently current.
        """
        refs = [(ref, self._workspace_artifact_path(ref)) for ref in task.artifacts]
        refs = [(ref, path) for ref, path in refs if path is not None]
        if not refs:
            return []
        try:
            actor = next(p for p in self.service.rooms.get(snapshot.room_id)["participants"]
                         if p["sessionId"] == session_id and p["status"] == "active")
            session = self.service.sessions.get(actor["sessionId"])
            if session.get("status") == "archived":
                raise GraphConflict("artifact reader is archived")
            tools = {str(tool.get("name") or tool.get("id"))
                     for tool in self.service._runtime_tool_manifest(session)}
            if not tools.intersection({"read", "workspace_read"}):
                raise GraphConflict("artifact reader no longer has file read capability")
            reader = self.service.background_jobs.workspace_harness
            roots = reader._read_session_roots(session)
        except (AttributeError, KeyError, StopIteration, ValueError, RuntimeError, OSError):
            return [{"sourceRef": ref, "status": "unavailable"} for ref, _ in refs]
        remaining = _ARTIFACT_TOTAL_BYTES
        revisions = []
        for ref, path in refs:
            try:
                if not path or "\x00" in path:
                    raise GraphError("invalid workspace artifact path")
                relocation = None
                try:
                    target, root = reader._resolve_existing_path(roots, path, allow_directory=False)
                except (ValueError, RuntimeError, OSError):
                    documents = getattr(self.service, "work_documents", None)
                    resolve = getattr(documents, "relocated_artifact_path", None)
                    relocation = resolve(path, authority_kind="room_work_item", authority_id=task.id) if callable(resolve) else None
                    if relocation is None:
                        raise
                    # The WorkDocument owner's receipt establishes identity;
                    # the verifier's existing reader still owns access and bytes.
                    target, root = reader._resolve_existing_path(roots, relocation["path"], allow_directory=False)
                if reader._is_sensitive_for_session(session, target, root):
                    raise GraphConflict("workspace artifact is unavailable")
                flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
                fd = os.open(target, flags)
                try:
                    before = os.fstat(fd)
                    if (not stat.S_ISREG(before.st_mode) or before.st_size > _ARTIFACT_FILE_BYTES
                        or before.st_size > remaining):
                        raise GraphConflict("workspace artifact exceeds bounded read")
                    chunks = []
                    unread = before.st_size + 1
                    while unread:
                        chunk = os.read(fd, min(65536, unread))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        unread -= len(chunk)
                    raw = b"".join(chunks)
                    after = os.fstat(fd)
                    current = target.stat()
                    identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                              value.st_mtime_ns, value.st_ctime_ns)
                    if (len(raw) != before.st_size or identity(before) != identity(after)
                        or identity(after) != identity(current)):
                        raise GraphConflict("workspace artifact changed during read")
                finally:
                    os.close(fd)
                remaining -= len(raw)
                actual_hash = hashlib.sha256(raw).hexdigest()
                declared = _ARTIFACT_SHA256_SUFFIX.search(ref)
                if declared and declared.group(1).lower() != actual_hash:
                    raise GraphConflict("workspace artifact differs from declared revision")
                revision = {"sourceRef": ref, "status": "available", "revision": "sha256:" + actual_hash}
                if relocation is not None:
                    revision.update(resolvedRef=str(target), relocationReceiptId=relocation["relocationReceiptId"])
                revisions.append(revision)
            except (AttributeError, ValueError, RuntimeError, OSError):
                revisions.append({"sourceRef": ref, "status": "unavailable"})
        return revisions

    def worker_tool_evidence(self, snapshot, task, effect, *, inline_byte_budget=24000):
        """Project one accepted business attempt through existing Runtime/media owners.

        This is evidence for independent verification, never a verdict or a new
        transcript authority. A media copy makes these bounded tool-only records
        readable by the verifier without exposing the worker's private Session.
        """
        missing = {"schemaVersion": "jev-worker-tool-evidence/1", "status": "unavailable", "tools": [],
                   "reason": "exact accepted worker tool evidence is unavailable"}
        if not isinstance(effect, Mapping):
            return missing
        request, receipt = effect.get("request", {}), effect.get("receipt", {})
        if not isinstance(request, Mapping) or not isinstance(receipt, Mapping):
            return missing
        expected = {"graphId": snapshot.graph_id, "rootId": snapshot.root_id, "roomId": snapshot.room_id,
                    "taskId": task.id, "taskRevision": task.revision, "ownerId": task.owner_id,
                    "assignmentKey": task.assignment_key, "acceptedTurnId": task.accepted_turn_id,
                    "dispatchId": task.accepted_turn_id}
        if (not task.accepted_turn_id or snapshot.task(task.id) != task
            or effect.get("operation") != "dispatch" or effect.get("state") != "accepted"
            or effect.get("effectId") != task.accepted_turn_id or request.get("purpose", "execute") != "execute"
            or any(request.get(key) != value for key, value in expected.items())
            or receipt.get("state") != "accepted" or not receipt.get("turnId")
            or any(receipt.get(key) != request.get(key) for key in ("taskId", "sessionId", "dispatchId"))):
            return missing
        try:
            actor = self.service.rooms.participant(task.owner_id)
            if (actor.get("status") != "active" or actor.get("roomId") != snapshot.room_id
                or actor.get("sessionId") != request.get("sessionId")):
                return missing
            reader = getattr(self.service.runtime, "session_tool_evidence", None)
            history = (reader(request["sessionId"], turn_id=receipt["turnId"]) if callable(reader)
                       else self.service.runtime.session_snapshot(request["sessionId"]))
        except (AttributeError, KeyError, ValueError, RuntimeError, OSError):
            return missing
        if not isinstance(history, Mapping) or not isinstance(history.get("toolHistoryEvents"), list):
            return missing
        binding = {**expected, "sessionId": request["sessionId"], "turnId": receipt["turnId"]}
        events = [event for event in history["toolHistoryEvents"] if isinstance(event, Mapping)
                  and event.get("sessionId") == binding["sessionId"] and event.get("turnId") == binding["turnId"]
                  and event.get("eventType") in {"tool_started", "tool_finished"}
                  and isinstance(event.get("payload"), Mapping)
                  and event["payload"].get("toolName") in _EVIDENCE_TOOLS]
        # Browser discovery/trace can contain unrelated Task Spaces. Share only
        # commands actually invoked by this exact worker turn, not global views.
        browser_calls = {event["payload"].get("toolCallId") for event in events
                         if event["eventType"] == "tool_started" and event["payload"].get("toolName") == "browser"
                         and isinstance(event["payload"].get("args"), Mapping)
                         and event["payload"]["args"].get("op") in _BROWSER_EVIDENCE_ACTIONS}
        events = [event for event in events if event["payload"].get("toolName") != "browser"
                  or event["payload"].get("toolCallId") in browser_calls]
        starts = {e["payload"].get("toolCallId"): e for e in events if e["eventType"] == "tool_started"}
        records, record_sizes, seen, finished_ids, omitted, partial = [], [], set(), set(), 0, False
        archive_bytes = len(canonical(binding).encode()) + 2000
        for event in events:
            payload = event["payload"]
            call_id, name = payload.get("toolCallId"), payload.get("toolName")
            if event["eventType"] != "tool_finished" or not call_id or not event.get("eventId"):
                continue
            if event["eventId"] in seen:
                continue
            seen.add(event["eventId"])
            start = starts.get(call_id, {})
            if start and start["payload"].get("toolName") != name:
                partial = True
                continue
            # A completion without an inspectable result (such as a cold PTC
            # child receipt) is missing evidence, not a still-running tool.
            finished_ids.add(call_id)
            raw = payload.get("result")
            if not isinstance(raw, Mapping):
                partial = True
                continue
            result = {key: value for key, value in raw.items() if key in _EVIDENCE_RESULT_FIELDS}
            # Never forward image bytes, private assistant messages, reasoning,
            # Room lists, governance internals or memory checkpoint metadata.
            if name == "browser":
                command_id = raw.get("commandId")
                action = start.get("payload", {}).get("args", {}).get("op")
                if (raw.get("schemaVersion") != "rag-ime.browser-control.v1"
                    or not isinstance(command_id, str) or not command_id.startswith("bcmd_")
                    or raw.get("action") != action):
                    result = {"receiptUnavailable": True}
                    partial = True
                else:
                    result = {key: value for key, value in raw.items() if key in _BROWSER_RECEIPT_FIELDS}
                    outcome = raw.get("result")
                    if isinstance(outcome, Mapping):
                        result["result"] = {key: value for key, value in outcome.items()
                                            if key in _BROWSER_RESULT_FIELDS}
            elif isinstance(raw.get("content"), list):
                result["content"] = [{"type": "text", "text": item["text"]} for item in raw["content"]
                                     if isinstance(item, Mapping) and item.get("type") == "text"
                                     and isinstance(item.get("text"), str)]
            elif isinstance(raw.get("content"), str):
                result["content"] = raw["content"]
            result = inspectable_tool_result(result)
            arguments = start.get("payload", {}).get("args", {})
            argument_source = start.get("payload", {}).get("argumentSource", "runtime_redacted_arguments")
            if argument_source not in {"native_transcript_arguments", "native_nested_call_arguments"}:
                argument_source = "runtime_redacted_arguments"
            # Native bash's public args intentionally shorten paths. Its applied
            # owner receipt retains the exact request, with its own causal binding.
            approval = raw.get("approval", {})
            if isinstance(approval, Mapping):
                causal = approval.get("causalMetadata", {})
                preview = approval.get("preview", {})
                if (approval.get("sessionId") == binding["sessionId"] and approval.get("toolCallId") == call_id
                    and isinstance(causal, Mapping) and isinstance(preview, Mapping)
                    and all(causal.get(key) == binding[key] for key in ("roomId", "rootId", "dispatchId", "turnId"))
                    and isinstance(preview.get("actionPayload"), Mapping)):
                    arguments = inspectable_tool_result(preview["actionPayload"])
                    argument_source = "causally_bound_owner_request"
            record = {"toolCallId": call_id, "toolName": name, "eventId": event["eventId"],
                      "startedEventId": start.get("eventId", ""), "startedAtMs": start.get("createdAtMs"),
                      "finishedAtMs": event.get("createdAtMs"), "timelineSequence": event.get("timelineSequence"),
                      "arguments": arguments, "argumentSource": argument_source,
                      "isError": payload.get("isError"), "result": result}
            command_receipt = result.get("receipt") if isinstance(result, Mapping) else None
            if (isinstance(command_receipt, Mapping)
                and command_receipt.get("schemaVersion") == _COMMAND_RECEIPT_SEMANTICS["schemaVersion"]):
                record["receiptSemantics"] = {**_COMMAND_RECEIPT_SEMANTICS, "receiptPath": "result.receipt"}
            elif isinstance(result, Mapping) and result.get("schemaVersion") == _COMMAND_RECEIPT_SEMANTICS["schemaVersion"]:
                record["receiptSemantics"] = {**_COMMAND_RECEIPT_SEMANTICS, "receiptPath": "result"}
            if not start or _source_truncated(raw):
                partial = True
            bounded = _bounded_evidence(record, _EVIDENCE_RECORD_BYTES)
            if bounded is not record:
                record = {key: value for key, value in record.items() if key not in {"arguments", "result"}}
                record["result"] = bounded
                partial = True
            size = len(canonical(record).encode())
            # Keep the latest bounded execution suffix: final tests and readback
            # receipts must not lose their place to earlier large file reads.
            # Eviction remains explicit partial evidence, never a success signal.
            while records and archive_bytes + size > _EVIDENCE_ARCHIVE_BYTES:
                records.pop(0)
                archive_bytes -= record_sizes.pop(0)
                omitted += 1
            if archive_bytes + size > _EVIDENCE_ARCHIVE_BYTES:
                omitted += 1
                continue
            archive_bytes += size
            records.append(record)
            record_sizes.append(size)
        if not records:
            return {**missing, "binding": binding}
        pending = len(set(starts) - finished_ids)
        archive = {"schemaVersion": "jev-worker-tool-evidence/1", "binding": binding,
                   "source": "PiRuntime.session_snapshot.toolHistoryEvents", "tools": records,
                   "selectionPolicy": "latest_completed_results_in_source_order",
                   "status": "partial" if partial or omitted or pending else "available",
                   "omittedToolResults": omitted, "unfinishedToolCalls": pending}
        raw_archive = canonical(archive).encode("utf-8")
        read_ref = ""
        try:
            media = self.service.media
            sha = hashlib.sha256(raw_archive).hexdigest()
            existing = next((item for item in media.list_for_room(snapshot.room_id, limit=500)
                             if item.get("originTool") == "jev_worker_tool_evidence" and item.get("sha256") == sha), None)
            if existing:
                _receipt, stored = media.read(existing["mediaId"], room_id=snapshot.room_id)
                if stored != raw_archive:
                    existing = None
            stored = existing or media.import_bytes(room_id=snapshot.room_id, data=raw_archive, mime_type="text/plain",
                file_name="worker-tool-evidence.json", origin="tool_result", origin_tool="jev_worker_tool_evidence",
                origin_receipt_id=task.accepted_turn_id)
            read_ref = "media://" + stored["mediaId"]
        except (AttributeError, KeyError, ValueError, RuntimeError, OSError):
            # Already read exact inline records remain useful if archive storage
            # is unavailable. Never publish an invented readable reference.
            pass
        result = {**archive, "readRef": read_ref, "archiveSha256": hashlib.sha256(raw_archive).hexdigest(),
                  "archiveBytes": len(raw_archive), "inlineTruncated": False}
        maximum = max(4096, min(24000, int(inline_byte_budget)))
        if len(canonical(result).encode()) > maximum:
            result["tools"] = [_bounded_evidence(record, max(800, (maximum - 3000) // len(records))) for record in records]
            result["inlineTruncated"] = True
            while result["tools"] and len(canonical(result).encode()) > maximum:
                result["tools"].pop()
            if not read_ref:
                result["status"] = "partial"
        return result

    def manifest(self, snapshot, task, *, context_refs: Sequence[object] = (),
                 decision_external_allowed: bool = False,
                 selections: Mapping[str, str] | None = None,
                 summaries: Sequence[Mapping[str, object]] = (),
                 write_targets: Sequence[str] = (), byte_budget: int = 24000) -> ContextManifest:
        """Revalidation supplies prepared selections and makes no Jev call.

        Existing summaries require sourceRef, sourceRevision, selector and content.
        The caller obtains them from their owner; a plan cannot supply new prose.
        """
        if not isinstance(decision_external_allowed, bool):
            raise GraphError("invalid external decision policy")
        if not isinstance(context_refs, Sequence) or isinstance(context_refs, (str, bytes)) or len(context_refs) > 32:
            raise GraphError("task context references exceed bound")
        actor = self.service.rooms.participant(task.owner_id)
        if actor.get("status") != "active" or actor.get("roomId") != snapshot.room_id:
            raise GraphConflict("context executor binding is no longer active")
        session = self.service.sessions.get(actor["sessionId"])
        if session.get("status") == "archived":
            raise GraphConflict("context executor session is archived")
        scope = self.execution_scope(session, task, write_targets=write_targets)
        materials = self._requirements(snapshot, task)
        notices, missing, receipts = [], [], []
        refs = [context_reference(value) for value in context_refs]

        # Explicit section refs replace the automatic whole-document pointer.
        try:
            document = self.service._work_document_for_authority("room_work_item", task.id)
            if document:
                source = "workdoc:" + str(document["documentId"])
                if not any(ref["sourceRef"] == source for ref in refs):
                    refs.append({"sourceRef": source})
        except (KeyError, ValueError, RuntimeError, OSError):
            notices.append("task work document discovery unavailable; other inputs remain usable")

        chosen = {material.id: "inline" for material in materials}
        seen = set()
        decision_context = self.decision_task_context(snapshot, task,
                                                       external_allowed=decision_external_allowed)
        for reference in refs:
            source, selector = str(reference["sourceRef"]), str(reference.get("selector") or "")
            # Plans may reference the exact automatically included requirement
            # or accepted dependency. Keep the canonical owner content rather
            # than trying to interpret its ID as a filesystem scheme.
            if not selector and any(source in {item.id, item.source_ref} for item in materials):
                continue
            identity = "material:" + digest([source, selector])[:32]
            if identity in seen:
                raise GraphError("duplicate scoped context reference")
            seen.add(identity)
            try:
                read = self._read(session, reference)
            except (KeyError, ValueError, RuntimeError, OSError, UnicodeError):
                # Do not echo a revoked path/title or an owner error containing
                # private details into the next prompt.
                target = missing if reference.get("required") else notices
                target.append(identity + ": source unavailable or not authorized")
                continue
            original, revision = str(read["content"]), str(read["revision"])
            summary = next((item for item in summaries
                            if item.get("sourceRef") == source
                            and item.get("sourceRevision") == revision
                            and str(item.get("selector") or "") == selector
                            and isinstance(item.get("content"), str)), None)
            material = Material(identity, revision, str(read["title"]), source, original[:1600],
                                original=original,
                                summary=str(summary["content"]) if summary else None,
                                summary_revision=revision if summary else None,
                                readable=True, required=bool(reference.get("required")),
                                external_allowed=decision_external_allowed and reference.get("externalAllowed") is True,
                                location=selector, read_complete=bool(read["complete"]))
            materials.append(material)
            receipts.append({"materialId": identity, "sourceRef": source, "revision": revision,
                             "selector": selector, "reader": read["reader"],
                             "contentSha256": _content_hash(original), "complete": read["complete"],
                             "displayedRanges": read.get("displayedRanges", []),
                             "taskId": task.id, "taskRevision": task.revision,
                             "ownerId": task.owner_id, "assignmentKey": task.assignment_key,
                             "sessionId": session["id"]})
            selected = (selections.get(identity) if selections is not None else reference.get("selection"))
            if selected is None and selections is None and self.decider is not None and material.external_allowed:
                try:
                    selected = choose_reading_depth(self.decider, task_context=decision_context, material=material)
                except DecisionUnavailable:
                    selected = "read_exact"
            chosen[identity] = str(selected or "read_exact")

        return build_manifest(task.id, task.revision, materials, chosen, byte_budget=byte_budget,
                              missing=missing, notices=notices, read_receipts=receipts, execution_scope=scope)

    @staticmethod
    def decision_task_context(snapshot, task, *, external_allowed: bool) -> dict[str, object]:
        """Only the graph-authorized task contract accompanies a depth choice."""
        if not external_allowed:
            return {}
        root = snapshot.task(snapshot.root_work_id)
        return {"rootObjective": root.objective, "taskId": task.id, "taskRevision": task.revision,
                "objective": task.objective, "expectedOutput": task.expected_output,
                "acceptanceCriteria": list(task.acceptance)}

    def execution_scope(self, session, task, *, write_targets: Sequence[str] = ()) -> dict[str, object]:
        roots = list(session.get("workspaceRoots") or [])
        if unrestricted_workspace_policy_active(session):
            roots = list(system_wide_workspace_roots(roots))
        tools = [tool for tool in self.service._runtime_tool_manifest(session)
                 if tool.get("available", True)]
        capability_ids = set()
        callable_names = set()
        bindings = []
        for tool in tools:
            capability = str(tool.get("name") or tool.get("id") or "")
            if not capability:
                continue
            capability_ids.add(capability)
            if tool.get("modelVisible") is not False:
                callable_names.add(capability)
            for projection in tool.get("runtimeProjections", []):
                if not isinstance(projection, Mapping) or not projection.get("name"):
                    continue
                name = str(projection["name"])
                callable_names.add(name)
                bindings.append({"capabilityId": capability, "name": name,
                                 "operation": str(projection.get("operation") or "")})
        return {"sessionId": session["id"], "ownerId": task.owner_id,
                "assignmentKey": task.assignment_key, "mode": session.get("mode"),
                "executionMode": session.get("executionMode"),
                "toolProfileVersion": session.get("toolProfileVersion"),
                "workspaceRoots": roots,
                "writeRoots": [] if read_only_policy_active(session) else roots,
                "writeTargets": [text(value, "write target", 2000) for value in write_targets],
                "tools": sorted(callable_names),
                "capabilityIds": sorted(capability_ids),
                "toolBindings": sorted(bindings, key=lambda item: (item["capabilityId"], item["name"], item["operation"]))}

    def _requirements(self, snapshot, task) -> list[Material]:
        materials = []
        for current in (snapshot.task(snapshot.root_work_id), task):
            if any(item.id == "requirements:" + current.id for item in materials):
                continue
            original = canonical({"objective": current.objective, "expectedOutput": current.expected_output,
                                  "acceptanceCriteria": list(current.acceptance)})
            materials.append(Material("requirements:" + current.id, str(current.revision),
                                      "当前有效要求", "work:" + current.id, "", original=original,
                                      readable=True, controlling=True))
            work = self.service.room_work.get(current.id, room_id=snapshot.room_id)
            review, blocker = work.get("review") or {}, work.get("blocker") or {}
            feedback = {key: value for key, value in {
                "review": review if review.get("reason") and current.state != "done" else None,
                "blocker": blocker if blocker else None,
            }.items() if value}
            if feedback:
                materials.append(Material("feedback:" + current.id, str(current.revision),
                                          "当前未解决反馈", "work:" + current.id, "",
                                          original=canonical(feedback), readable=True, controlling=True))
        for dependency_id in sorted(snapshot.graph().requires[task.id]):
            dependency = snapshot.task(dependency_id)
            if dependency.state != "done":
                continue
            # A dependency's prose/evidence IDs are not its Tool receipts. Share
            # the same exact-turn, Room-scoped projection used by verifiers so
            # downstream workers can inspect required upstream proof without
            # asking a sibling to replay work or borrowing its private history.
            lifecycle = getattr(getattr(self.service, "jev_application", None), "lifecycle", None)
            tool_evidence = self.worker_tool_evidence(
                snapshot, dependency, lifecycle.effect_for_dispatch(dependency.accepted_turn_id),
                inline_byte_budget=2000,
            ) if lifecycle is not None else {"status": "unavailable", "tools": []}
            handoff = {"taskId": dependency.id,
                       "taskRevision": dependency.revision,
                       "state": dependency.state,
                       "stateSource": "current canonical WorkItem",
                       "acceptedAtMs": dependency.completed_at_ms or None,
                       "statePolicy": "state 是宿主当前已验收状态；acceptedAtMs 来自该 WorkItem 的 completedAtMs，记录验收完成时间，可与下游工具 startedAtMs 核对前置时序。result 是验收前的历史执行者提交，可能仍写着待验收。不得用历史文字覆盖当前 state。",
                       "result": dependency.result,
                       "artifacts": list(dependency.artifacts),
                       "evidence": list(dependency.evidence),
                       "workerToolEvidence": tool_evidence}
            handoff = self._dependency_handoff(snapshot, task, dependency, handoff)
            materials.append(Material("dependency:" + dependency.id, str(dependency.revision),
                                      "已验收依赖成果", "work:" + dependency.id, "",
                                      original=canonical(handoff),
                                      readable=True, required=True))
        return materials

    def _dependency_handoff(self, snapshot, consumer, dependency, body):
        """Keep accepted identity inline and exact upstream proof under its
        existing Room media owner. Dispatch is not another complete read of
        every upstream transcript; Pi reads the versioned package as needed."""
        if len(canonical(body).encode('utf-8')) <= 2200:
            return body
        archive = {"schemaVersion": "jev-accepted-dependency/1", "graphId": snapshot.graph_id,
                   "rootId": snapshot.root_id, "roomId": snapshot.room_id,
                   "ownerId": dependency.owner_id, "dispatchId": dependency.accepted_turn_id, **body}
        try:
            actor = self.service.rooms.participant(consumer.owner_id)
            revisions = self.artifact_revisions(snapshot, dependency, actor['sessionId'])
            if revisions:
                archive['artifactRevisions'] = revisions
            media = self.service.media
            raw = canonical(archive).encode('utf-8')
            sha = hashlib.sha256(raw).hexdigest()
            existing = next((item for item in media.list_for_room(snapshot.room_id, limit=500)
                             if item.get('originTool') == 'jev_accepted_dependency' and item.get('sha256') == sha), None)
            if existing:
                _receipt, stored = media.read(existing['mediaId'], room_id=snapshot.room_id)
                if stored != raw:
                    existing = None
            stored = existing or media.import_bytes(room_id=snapshot.room_id, data=raw, mime_type='text/plain',
                file_name='accepted-dependency.json', origin='tool_result', origin_tool='jev_accepted_dependency',
                origin_receipt_id=dependency.accepted_turn_id or dependency.id)
            compact = {key: body[key] for key in ('taskId', 'taskRevision', 'state', 'stateSource', 'acceptedAtMs')}
            compact.update(readRef='media://' + stored['mediaId'], archiveSha256=sha, archiveBytes=len(raw),
                inlineTruncated=True, resultPreview=dependency.result.encode('utf-8')[:500].decode('utf-8', errors='ignore'),
                artifactCount=len(dependency.artifacts),
                readPolicy='readRef 是完整已验收成果、artifacts、当前文件路径/版本与 workerToolEvidence。'
                    'resultPreview 不是全文；接口、交接或证据需要原文时用 read 分页读取至 nextLineOffset=null。'
                    '验收状态以当前 state 为准，历史 result 不覆盖它；可读文件无需复制旧路径。')
            return compact
        except (AttributeError, KeyError, ValueError, RuntimeError, OSError):
            # Retain the original if no actual readable copy exists. Context
            # budgeting must report this gap, never invent a successful read.
            return body

    def _read(self, session, reference):
        source = str(reference["sourceRef"])
        selector = str(reference.get("selector") or "")
        if source.startswith("workdoc:"):
            document = self.service.work_documents.detail(source[len("workdoc:"):])["document"]
            if document["state"] not in {"active", "archived"}:
                raise GraphError("work document is not readable")
            result = self._workspace_read(session, str(document["path"]), selector)
            return {**result, "title": str(document["title"]), "reader": "WorkDocumentService/WorkspaceHarness"}
        if source.startswith("knowledge:"):
            if selector:
                raise GraphError("knowledge claims are already exact scoped reads")
            result = self.service.room_knowledge_read({"claimRef": source[len("knowledge:"):],
                "retrievalReceiptId": reference.get("retrievalReceiptId"),
                "expectedHash": reference.get("expectedHash")}, authenticated_session_id=session["id"])
            return _read_value(str(result["text"]), str(result["claimHash"]), "Knowledge claim", "KnowledgePromotionService")
        parsed = urlsplit(source)
        if parsed.scheme in {"media", "artifact"}:
            if parsed.query or parsed.fragment or selector:
                raise GraphError("unsupported managed resource selector")
            resource_id = unquote(parsed.netloc + parsed.path).strip("/")
            if not resource_id or "/" in resource_id or "\x00" in resource_id:
                raise GraphError("invalid managed resource identifier")
            if parsed.scheme == "media":
                receipt, raw = self.service.read_media_resource(resource_id, session_id=session["id"])
                if not str(receipt.get("mimeType") or "").startswith("text/"):
                    raise GraphError("non-text media requires its native reader")
                content = raw.decode("utf-8")
                return _read_value(content, "sha256:" + _content_hash(content), "Managed media", "AgentMediaStore")
            result = self.service.delegation.inspect_artifact(session["id"], resource_id, limit=100)
            content = canonical(result)
            return _read_value(content, "sha256:" + _content_hash(content), "Delegated artifact", "AgentDelegationCoordinator")
        if source.startswith("workspace:"):
            path = source[len("workspace:"):]
        elif parsed.scheme == "file" and parsed.netloc in {"", "localhost"} and not parsed.query and not parsed.fragment:
            path = unquote(parsed.path)
        elif not parsed.scheme:
            path = source
        else:
            raise GraphError("unsupported context reference")
        return self._workspace_read(session, path, selector)

    def _workspace_read(self, session, path: str, selector: str):
        reader = self.service.background_jobs.workspace_harness
        args = {"path": path, "limit": 32000}
        if selector:
            args["selector"] = selector
        content, revision, ranges, complete = [], "", [], False
        for _ in range(4):
            page = reader.read(session, args)
            current = str(page["resourceRevision"])
            if revision and current != revision:
                raise GraphConflict("context source changed during scoped read")
            revision = current
            content.append(str(page["content"]))
            origin = page.get("readOrigin") or {}
            ranges.extend(origin.get("displayedRanges") or [])
            if not page.get("truncated"):
                complete = True
                break
            if sum(len(part.encode("utf-8")) for part in content) >= _READ_BYTES:
                break
            key, output = ("selectorCursor", "nextSelectorCursor") if selector else ("offset", "nextOffset")
            if page.get(output) is None:
                break
            args[key] = page[output]
        result = _read_value("".join(content), revision, Path(path).name, "WorkspaceHarness")
        return {**result, "complete": complete, "displayedRanges": ranges}


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_value(content: str, revision: str, title: str, reader: str) -> dict[str, object]:
    if len(content.encode("utf-8")) > 100000:
        raise GraphError("context resource requires a smaller scoped read")
    return {"content": content, "revision": revision, "title": title,
            "reader": reader, "complete": True}
