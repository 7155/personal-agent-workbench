"""Versioned note proposals and source-backed date views in the Knowledge store.

The connector creates new files only. The paired editor is the sole existing-note
writer. Model output never authorizes a write or a Memory adoption.
"""

from __future__ import annotations

import difflib
import base64
import io
import zipfile
import hashlib
import hmac
import json
import os
import secrets
import uuid
from datetime import datetime
from pathlib import Path, PurePosixPath

from .models import (
    KnowledgeConflictError,
    KnowledgeLibraryError,
    KnowledgeNotFoundError,
)
from .store import now_ms
from ..memory_lifecycle.daily import day_bounds


def sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def text(payload, key, limit=32_000):
    value = payload.get(key, "")
    if not isinstance(value, str) or len(value) > limit:
        raise KnowledgeLibraryError(
            "字段格式或长度无效：" + key, code="invalid_argument"
        )
    return value


class VaultWorkflow:
    def __init__(self, vault):
        self.vault = vault
        self.store = vault.store
        with self.store.connection() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS knowledge_vault_activity_refs (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, source_id TEXT NOT NULL,
                revision TEXT NOT NULL, project TEXT NOT NULL, day TEXT NOT NULL,
                timezone TEXT NOT NULL, occurred_ms INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS knowledge_vault_diary_drafts (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, day TEXT NOT NULL, timezone TEXT NOT NULL,
                project TEXT NOT NULL, markdown TEXT NOT NULL, sources_json TEXT NOT NULL,
                generator TEXT NOT NULL, created_ms INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS knowledge_vault_memory_links (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, note_id TEXT NOT NULL,
                revision TEXT NOT NULL, statement TEXT NOT NULL, project TEXT NOT NULL,
                created_ms INTEGER NOT NULL, atom_id TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending');
              CREATE TABLE IF NOT EXISTS knowledge_vault_policy (
                vault_id TEXT PRIMARY KEY, policy_json TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS knowledge_vault_materials (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, request_id TEXT NOT NULL,
                fingerprint TEXT NOT NULL, note_id TEXT NOT NULL, revision TEXT NOT NULL,
                project TEXT NOT NULL, occurred_ms INTEGER NOT NULL,
                UNIQUE(vault_id,request_id));
              CREATE TABLE IF NOT EXISTS knowledge_note_proposals (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, note_id TEXT NOT NULL,
                revision INTEGER NOT NULL, base_revision TEXT NOT NULL, edits_json TEXT NOT NULL,
                sources_json TEXT NOT NULL, reason TEXT NOT NULL, state TEXT NOT NULL,
                application_id TEXT NOT NULL, after_revision TEXT NOT NULL,
                created_ms INTEGER NOT NULL, generator TEXT NOT NULL, project TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS knowledge_editor_pairings (
                vault_id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, created_ms INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS knowledge_note_applications (
                application_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL,
                before_revision TEXT NOT NULL, after_revision TEXT NOT NULL,
                state TEXT NOT NULL, updated_ms INTEGER NOT NULL);
              CREATE TABLE IF NOT EXISTS knowledge_vault_days (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, day TEXT NOT NULL,
                timezone TEXT NOT NULL, project TEXT NOT NULL, digest TEXT NOT NULL,
                revision INTEGER NOT NULL);
            """)

    def policy(self, v):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT policy_json FROM knowledge_vault_policy WHERE vault_id=?",
                (v["id"],),
            ).fetchone()
        return {
            "inbox": "",
            "remoteProcessing": False,
            "jevEnabled": False,
            "personalDiary": "",
            "captureFolder": "",
            "captureProject": "",
            "activityProject": "",
            "activityTimezone": "Asia/Shanghai",
            **(json.loads(row[0]) if row else {}),
        }

    def dispatch(self, v, p):
        action = p["action"]
        if action == "suggest_targets":
            return self.suggest_targets(v,p)
        if action == "export_model_diary":
            with self.store.connection() as db:
                draft = db.execute("SELECT * FROM knowledge_vault_diary_drafts WHERE vault_id=? AND id=?",
                    (v["id"], text(p,"diaryId",80))).fetchone()
            if draft is None:
                raise KnowledgeNotFoundError("回顾不存在。")
            self.sources(v,json.loads(draft["sources_json"]))
            return self.create_new(v,"work-model-"+draft["id"]+".md",
                "# "+draft["day"]+" 工作回顾草稿\n\n机器生成 · 仅覆盖所选材料\n\n"+draft["markdown"])
        if action == "store_diary":
            self.dispatch(v, {**p, "action": "organize_context"})
            day = text(p, "date", 10) or datetime.now().date().isoformat()
            zone = text(p, "timezone", 80) or "Asia/Shanghai"
            day_bounds(day, zone)
            project = text(p, "project", 240)
            body = text(p, "markdown", 16000)
            refs = json.dumps(p["sourceRefs"], sort_keys=True)
            identity = sha(json.dumps([v["id"], day, zone, project, body, refs]))
            with self.store.connection() as db:
                db.execute("INSERT OR IGNORE INTO knowledge_vault_diary_drafts VALUES(?,?,?,?,?,?,?,?,?)",
                    (identity, v["id"], day, zone, project, body, refs,
                     text(p, "generator", 120), now_ms()))
            return {"id": identity, "state": "draft_saved", "date": day}
        if action == "graph_business":
            return self.graph(v, p)
        if action == "memory_prepare":
            note = self.vault.read(v, text(p, "noteId", 80))
            statement, project = (
                text(p, "statement", 800).strip(),
                text(p, "project", 240).strip(),
            )
            if (
                not statement
                or not project
                or statement not in note["markdown"]
                or p.get("baseRevision") != note["revision"]
            ):
                raise KnowledgeConflictError(
                    "请选择原文中的精确陈述，并指定项目和当前版本。"
                )
            identity = sha(
                json.dumps(
                    [v["id"], note["noteId"], note["revision"], statement, project]
                )
            )
            with self.store.connection() as db:
                db.execute(
                    "INSERT OR IGNORE INTO knowledge_vault_memory_links(id,vault_id,note_id,revision,statement,project,created_ms) VALUES(?,?,?,?,?,?,?)",
                    (
                        identity,
                        v["id"],
                        note["noteId"],
                        note["revision"],
                        statement,
                        project,
                        now_ms(),
                    ),
                )
                row = dict(
                    db.execute(
                        "SELECT * FROM knowledge_vault_memory_links WHERE id=?",
                        (identity,),
                    ).fetchone()
                )
            return row
        if action == "memory_receipt":
            with self.store.connection() as db:
                db.execute(
                    "UPDATE knowledge_vault_memory_links SET atom_id=?,state=? WHERE id=? AND vault_id=?",
                    (
                        text(p, "atomId", 200),
                        text(p, "state", 40),
                        text(p, "linkId", 80),
                        v["id"],
                    ),
                )
            return {"recorded": True}
        if action == "memory_links":
            with self.store.connection() as db:
                rows = [
                    dict(r)
                    for r in db.execute(
                        "SELECT * FROM knowledge_vault_memory_links WHERE vault_id=?",
                        (v["id"],),
                    )
                ]
            for row in rows:
                try:
                    note = self.vault.read(v, row["note_id"])
                    # Whitespace-only changes do not invalidate the statement.
                    row["needsReview"] = " ".join(
                        row["statement"].split()
                    ) not in " ".join(note["markdown"].split())
                except (OSError, KnowledgeLibraryError):
                    row["needsReview"] = True
            return {"items": rows}
        if action == "export_notes":
            self.vault.snapshot(v)
            with self.store.connection() as db:
                rows = db.execute(
                    "SELECT id FROM knowledge_vault_notes WHERE vault_id=? AND present=1",
                    (v["id"],),
                ).fetchall()
            archive = io.BytesIO()
            total = 0
            with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as out:
                for row in rows:
                    note = self.vault.read(v, row["id"])
                    total += len(note["markdown"].encode())
                    if total > 32 * 1024 * 1024:
                        raise KnowledgeLibraryError(
                            "范围过大，请分目录导出。", code="export_too_large"
                        )
                    out.writestr(note["path"], note["markdown"])
                out.writestr(
                    "PAW-export-info.txt",
                    "普通 Markdown 导出。不包含私人排除目录、Key、批准或执行权限。控制记录需独立备份。",
                )
            if archive.tell() > 10 * 1024 * 1024:
                raise KnowledgeLibraryError(
                    "压缩包过大，请分目录导出。", code="export_too_large"
                )
            return {
                "fileName": "paw-notes.zip",
                "base64": base64.b64encode(archive.getvalue()).decode(),
                "noteCount": len(rows),
            }
        if action == "settings":
            return {"policy": self.policy(v)}
        if action == "configure":
            from .vault import relative

            policy = self.policy(v)
            for key in ("remoteProcessing", "jevEnabled"):
                if key in p:
                    if type(p[key]) is not bool:
                        raise KnowledgeLibraryError(
                            "请明确开关状态。", code="invalid_argument"
                        )
                    policy[key] = p[key]
            if "inbox" in p:
                inbox = relative(text(p, "inbox", 240))
                if not self.vault._allowed(v, inbox):
                    raise KnowledgeLibraryError(
                        "收件箱不在授权范围。", code="scope_mismatch"
                    )
                policy["inbox"] = inbox
            if "activityProject" in p:
                policy["activityProject"] = text(p,"activityProject",240).strip()
                zone = text(p,"timezone",80) or "Asia/Shanghai"
                day_bounds("2026-01-01",zone)
                policy["activityTimezone"] = zone
            if "captureFolder" in p:
                folder = text(p, "captureFolder", 240)
                if folder:
                    folder = relative(folder)
                    if not self.vault._allowed(v, folder):
                        raise KnowledgeLibraryError("采集目录不在授权范围。", code="scope_mismatch")
                policy["captureFolder"] = folder
                policy["captureProject"] = text(p, "captureProject", 240)
            if "personalDiary" in p:
                diary = text(p, "personalDiary", 240)
                if diary:
                    relative(diary.replace("{date}", "2000-01-01"))
                    if not diary.endswith(".md") or "{date}" not in diary:
                        raise KnowledgeLibraryError(
                            "日记路径需包含 {date} 并以 .md 结尾。",
                            code="invalid_argument",
                        )
                policy["personalDiary"] = diary
            with self.store.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO knowledge_vault_policy VALUES(?,?)",
                    (v["id"], json.dumps(policy)),
                )
            return {"policy": policy}
        if action == "organize_context":
            policy = self.policy(v)
            if not policy["remoteProcessing"]:
                raise KnowledgeLibraryError(
                    "远程整理未开启；本地阅读和手动提案仍可用。", code="remote_disabled"
                )
            notes = self.sources(v, p.get("sourceRefs", []))
            target = (
                self.vault.read(v, text(p, "noteId", 80)) if p.get("noteId") else None
            )
            if (
                sum(len(n["markdown"]) for n in notes)
                + len(target["markdown"] if target else "")
                > 24_000
            ):
                raise KnowledgeLibraryError(
                    "请减少所选材料或选择较短笔记。", code="context_too_large"
                )
            return {"sources": notes, "target": target, "policy": policy}
        if action == "save":
            return self.save(v, p)
        if action == "day":
            return self.day(v, p)
        if action == "prepare":
            return self.prepare(v, p)
        if action == "proposals":
            with self.store.connection() as db:
                rows = db.execute(
                    "SELECT * FROM knowledge_note_proposals WHERE vault_id=? ORDER BY created_ms DESC LIMIT 100",
                    (v["id"],),
                ).fetchall()
            return {"items": [self.public(v, r) for r in rows]}
        if action in {"approve", "dismiss", "draft"}:
            row = self.proposal(v, text(p, "proposalId", 80))
            if (
                type(p.get("proposalRevision")) is not int
                or p["proposalRevision"] != row["revision"]
            ):
                raise KnowledgeConflictError("提案版本已改变，请重新审核。")
            if action == "draft":
                body = self.materialize(v, row)
                return self.create_new(v, "draft-" + row["id"] + ".md", body)
            if row["state"] in {"saved", "saved_index_pending", "applying"}:
                return self.public(v, row)
            if action == "approve":
                self.materialize(v, row)
            with self.store.connection() as db:
                db.execute(
                    "UPDATE knowledge_note_proposals SET state=? WHERE id=?",
                    (
                        "waiting_editor" if action == "approve" else "dismissed",
                        row["id"],
                    ),
                )
            return self.public(v, self.proposal(v, row["id"]))
        if action == "pair":
            token = secrets.token_urlsafe(32)
            with self.store.connection() as db:
                db.execute(
                    "INSERT OR REPLACE INTO knowledge_editor_pairings VALUES(?,?,?)",
                    (v["id"], sha(token), now_ms()),
                )
            return {"vaultId": v["id"], "root": v["root"], "pairingToken": token}
        if action == "revoke":
            with self.store.connection() as db:
                db.execute(
                    "DELETE FROM knowledge_editor_pairings WHERE vault_id=?", (v["id"],)
                )
            return {"revoked": True}
        if action == "context":
            ids = p.get("noteIds", [])
            if (
                not isinstance(ids, list)
                or len(ids) > 8
                or any(not isinstance(i, str) for i in ids)
            ):
                raise KnowledgeLibraryError(
                    "每次最多选择 8 篇笔记。", code="invalid_argument"
                )
            notes = [self.vault.read(v, i) for i in ids]
            if sum(len(n["markdown"]) for n in notes) > 24_000:
                raise KnowledgeLibraryError(
                    "选择的内容过长，请减少笔记。", code="context_too_large"
                )
            return {
                "originPackId": str(uuid.uuid4()),
                "purpose": "discussion",
                "authority": "reference_only",
                "createdAtMs": now_ms(),
                "references": [
                    {"noteId": n["noteId"], "revision": n["revision"]} for n in notes
                ],
                "markdown": "\n\n".join(
                    f"## {n['path']}\n{n['markdown']}" for n in notes
                ),
                "notice": "仅作为本次讨论的参考，不代表采纳或执行授权。再次使用前须重新读取。",
            }
        if action == "export_day":
            day = self.day(v, p)
            return self.create_new(
                v,
                "work-" + day["id"] + "-v" + str(day["revision"]) + ".md",
                day["markdown"],
            )
        if action == "forget_preview":
            with self.store.connection() as db:
                count = db.execute(
                    "SELECT COUNT(*) FROM knowledge_note_proposals WHERE vault_id=?",
                    (v["id"],),
                ).fetchone()[0]
            return {
                "proposals": count,
                "preservesMarkdown": True,
                "notice": "删除本文件夹的整理记录、配对与派生状态；保留所有用户 Markdown。",
            }
        if action == "forget":
            if p.get("confirm") is not True:
                raise KnowledgeLibraryError(
                    "请先确认清理范围。", code="confirmation_required"
                )
            with self.store.connection() as db:
                db.execute(
                    "DELETE FROM knowledge_note_applications WHERE proposal_id IN (SELECT id FROM knowledge_note_proposals WHERE vault_id=?)",
                    (v["id"],),
                )
                for table in (
                    "knowledge_vault_activity_refs",
                    "knowledge_vault_diary_drafts",
                    "knowledge_vault_materials",
                    "knowledge_note_proposals",
                    "knowledge_editor_pairings",
                    "knowledge_vault_days",
                ):
                    db.execute(f"DELETE FROM {table} WHERE vault_id=?", (v["id"],))
            return {"forgotten": True, "userFilesPreserved": True}
        raise KnowledgeLibraryError("不支持的笔记操作。", code="invalid_argument")

    def suggest_targets(self, v, p):
        from ..text_utils import token_terms
        originals = self.sources(v,p.get("sourceRefs",[]))
        query = "\n".join(item["markdown"] for item in originals)[:24000]
        terms=token_terms(query,max_terms=32)
        def overlap(value):
            lowered=value.lower()
            return [term for term in terms if term.lower() in lowered]
        excluded = {item["noteId"] for item in originals}
        snapshot = self.vault.snapshot(v)
        policy = self.policy(v)
        with self.store.connection() as db:
            rows = db.execute("SELECT id,path,revision FROM knowledge_vault_notes WHERE vault_id=? AND present=1 AND identity_state!='duplicate_id'",(v["id"],)).fetchall()
        candidates=[]
        for row in rows:
            if row["id"] in excluded or row["path"].startswith(policy["inbox"]+"/work-"):
                continue
            cached=self.vault._metadata_cache.get((v["id"],row["path"]))
            if not cached or cached[1]["revision"]!=row["revision"]:
                continue
            note=cached[1]
            heading=note["title"]+" "+" ".join(note["aliases"])
            title_hits=overlap(heading)
            hits=overlap(note["body"])
            if not title_hits and len(hits)<2:
                continue
            score=len(hits)+3*len(title_hits)
            lines=[line.strip() for line in note["body"].splitlines() if overlap(line)]
            candidates.append({"id":row["id"],"path":row["path"],"revision":row["revision"],
                "title":note["title"],"score":score,"matchedTerms":sorted(set(title_hits+hits)),
                "snippet":" ".join(lines)[:400],"relation":"possibly_related"})
        candidates.sort(key=lambda item:(-item["score"],item["path"]))
        return {"candidates":candidates[:8],"total":len(candidates),"remoteProcessing":False,
            "scanIncomplete":snapshot["unreadableCount"]>0,
            "notice":"本地匹配只表示可能相关，不代表证据支持。请选择要核对的旧笔记；也可以不匹配任何笔记。"}

    def activity_materials(self, v, day, timezone, project):
        policy = self.policy(v)
        provider = self.vault.activity_provider
        if not project or project != policy["activityProject"] or provider is None:
            return []
        packet = provider(project,day,timezone)
        result = []
        with self.store.connection() as db:
            for source in packet.get("evidence",[])[:100]:
                if str(source.get("origin",{}).get("namespace","")).startswith("paw-vault:"):
                    continue  # A note adoption is not another independent original.
                if source.get("provenance",{}).get("derivedArtifactType"):
                    continue
                revision = sha(json.dumps([source,packet.get("admissionRevisions",{}).get(source["id"])],sort_keys=True,ensure_ascii=False))
                identity = sha(v["id"]+source["id"]+revision)
                db.execute("INSERT OR IGNORE INTO knowledge_vault_activity_refs VALUES(?,?,?,?,?,?,?,?)",
                    (identity,v["id"],source["id"],revision,project,day,timezone,source["occurredAtMs"]))
                result.append({"id":identity,"activityId":identity,"noteId":"activity:"+identity,
                    "revision":revision,"occurredAtMs":source["occurredAtMs"],"readable":True,
                    "text":source["text"],"authorship":"mixed_or_unknown","submission":"unknown",
                    "completeness":"admitted_excerpt"})
        return result

    def activity_source(self,v,identity,cache=None):
        with self.store.connection() as db:
            row=db.execute("SELECT * FROM knowledge_vault_activity_refs WHERE vault_id=? AND id=?",(v["id"],identity)).fetchone()
        if row is None:
            raise KnowledgeNotFoundError("活动来源不存在。")
        key=(row["day"],row["timezone"],row["project"])
        if cache is not None and key in cache:
            current=cache[key]
        else:
            current=self.activity_materials(v,*key)
            if cache is not None:
                cache[key]=current
        match=next((item for item in current if item["id"]==identity),None)
        if match is None:
            raise KnowledgeConflictError("活动来源已变化、撤回或不再获准。")
        return {"noteId":"activity:"+identity,"revision":row["revision"],"path":"activity/"+identity,
            "markdown":match["text"]}

    def capture_files(self, v, discovered):
        policy = self.policy(v)
        folder = policy["captureFolder"]
        if not folder:
            return
        with self.store.connection() as db:
            for item in discovered:
                if not item["path"].startswith(folder + "/"):
                    continue
                # Generated reports are projections, never independent raw evidence.
                if (item["path"].startswith(policy["inbox"] + "/") and PurePosixPath(item["path"]).name.startswith(("work-", "idea-", "draft-"))) or item["identityState"] == "duplicate_id":
                    continue
                request = "watch:" + sha(item["id"] + item["revision"])
                identity = sha(v["id"] + request)
                db.execute("INSERT OR IGNORE INTO knowledge_vault_materials VALUES(?,?,?,?,?,?,?,?)",
                    (identity,v["id"],request,item["revision"],item["id"],item["revision"],policy["captureProject"],now_ms()))

    def graph(self, v, p):
        mode = p.get('graphMode', 'project')
        project = text(p, 'project', 240)
        nodes, edges = {}, []
        def note(identity):
            if identity in nodes:
                return True
            if len(nodes) >= 160:
                return False
            try:
                current = self.vault.read(v, identity)
            except (OSError, KnowledgeLibraryError):
                return False
            nodes[identity] = {'id':identity,'kind':'note','label':PurePosixPath(current['path']).stem,'noteId':identity}
            return True
        with self.store.connection() as db:
            materials = db.execute('SELECT * FROM knowledge_vault_materials WHERE vault_id=? ORDER BY occurred_ms DESC LIMIT 100', (v['id'],)).fetchall()
            applied = db.execute("SELECT * FROM knowledge_note_proposals WHERE vault_id=? AND state IN ('saved','saved_index_pending') ORDER BY created_ms DESC LIMIT 100", (v['id'],)).fetchall()
            adopted = db.execute("SELECT * FROM knowledge_vault_memory_links WHERE vault_id=? AND state IN ('adopted','needs_review') LIMIT 100", (v['id'],)).fetchall()
        for item in materials:
            if project and item['project'] != project:
                continue
            if not note(item['note_id']):
                continue
            if mode == 'growth':
                identity = 'episode:' + item['id']
                nodes[identity] = {'id':identity,'kind':'episode','label':'保存的工作片段','noteId':item['note_id']}
                edges.append({'source':identity,'target':item['note_id'],'label':'来自主动保存','basis':item['revision']})
            elif item['project']:
                identity = 'project:' + sha(item['project'])[:20]
                nodes[identity] = {'id':identity,'kind':'project','label':item['project'],'noteId':''}
                edges.append({'source':identity,'target':item['note_id'],'label':'用于项目','basis':'用户保存时明确绑定'})
        for item in applied:
            if project and item['project'] != project:
                continue
            if not note(item['note_id']):
                continue
            for source in json.loads(item['sources_json']):
                if source.get('noteId') and note(source['noteId']):
                    edges.append({'source':source['noteId'],'target':item['note_id'],'label':'用于真实修订','basis':item['application_id']})
        if mode == 'project':
            for item in adopted:
                if project and item['project'] != project:
                    continue
                if not note(item['note_id']):
                    continue
                identity = 'memory:' + item['id']
                nodes[identity] = {'id':identity,'kind':'decision','label':('待复核：' if item['state']=='needs_review' else '已采纳：') + item['statement'][:60],'noteId':item['note_id']}
                edges.append({'source':item['note_id'],'target':identity,'label':'明确采纳为项目事项','basis':item['revision']})
        return {'nodes':list(nodes.values())[:200],'edges':[e for e in edges if e['source'] in set(list(nodes)[:200]) and e['target'] in set(list(nodes)[:200])],'mode':mode}

    def save(self, v, p):
        body = text(p, "markdown")
        if not body.strip():
            raise KnowledgeLibraryError("请写下一点想法。", code="invalid_argument")
        request_id = text(p, "requestId", 80)
        if not request_id:
            raise KnowledgeLibraryError("保存缺少重试身份。", code="invalid_argument")
        project = text(p, "project", 240)
        fingerprint = sha(json.dumps([body, project], ensure_ascii=False))
        with self.store.connection() as db:
            old = db.execute(
                "SELECT * FROM knowledge_vault_materials WHERE vault_id=? AND request_id=?",
                (v["id"], request_id),
            ).fetchone()
        if old:
            if old["fingerprint"] != fingerprint:
                raise KnowledgeConflictError("同一次保存的内容已改变。")
            return {
                "saved": True,
                "noteId": old["note_id"],
                "materialId": old["id"],
                "replayed": True,
            }
        result = self.create_new(v, "idea-" + sha(request_id)[:24] + ".md", body)
        self.vault.snapshot(v)
        with self.store.connection() as db:
            note = db.execute(
                "SELECT * FROM knowledge_vault_notes WHERE vault_id=? AND path=? AND present=1",
                (v["id"], result["path"]),
            ).fetchone()
            if not note:
                raise KnowledgeLibraryError(
                    "文件已保存，尚未索引；请刷新后重试。", code="saved_index_pending"
                )
            identity = str(uuid.uuid4())
            db.execute(
                "INSERT INTO knowledge_vault_materials VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity,
                    v["id"],
                    request_id,
                    fingerprint,
                    note["id"],
                    note["revision"],
                    project,
                    now_ms(),
                ),
            )
        return {**result, "noteId": note["id"], "materialId": identity}

    def create_new(self, v, name, body):
        """Exclusive create beneath the separately authorized inbox; never replace."""
        from .vault import relative

        inbox = self.policy(v)["inbox"]
        if not inbox:
            raise KnowledgeLibraryError(
                "请先指定允许创建文件的收件箱。", code="write_scope_required"
            )
        path = relative(inbox + "/" + name)
        if not self.vault._allowed(v, path):
            raise KnowledgeLibraryError(
                "文件不在允许创建的范围。", code="scope_mismatch"
            )
        fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in Path(v["root"]).parts[1:]:
                nxt = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = nxt
            for part in PurePosixPath(path).parts[:-1]:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                nxt = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
                )
                os.close(fd)
                fd = nxt
            leaf = PurePosixPath(path).name
            # Write a complete temporary inode, then publish using an exclusive
            # hard link. A crash never leaves an incomplete authoritative file.
            temp = ".paw-" + uuid.uuid4().hex
            out = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=fd,
            )
            try:
                with os.fdopen(out, "wb") as stream:
                    stream.write(body.encode())
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temp, leaf, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False
                    )
                except FileExistsError:
                    current, _ = self.vault._read_path(v, path)
                    if current != body:
                        raise KnowledgeConflictError(
                            "已有文件经过编辑，已保留原文；请另存新的草稿。"
                        ) from None
            finally:
                os.unlink(temp, dir_fd=fd)
            os.fsync(fd)
        finally:
            os.close(fd)
        return {
            "saved": True,
            "path": path,
            "revision": sha(body),
            "existingNoteUpdated": False,
        }

    def day(self, v, p):
        day = text(p, "date", 10) or datetime.now().date().isoformat()
        timezone = text(p, "timezone", 80) or "Asia/Shanghai"
        start, end = day_bounds(day, timezone)
        project = text(p, "project", 240)
        with self.store.connection() as db:
            rows = db.execute(
                "SELECT * FROM knowledge_vault_materials WHERE vault_id=? AND occurred_ms>=? AND occurred_ms<? AND project=? ORDER BY occurred_ms",
                (v["id"], start, end, project),
            ).fetchall()
        sources = self.activity_materials(v, day, timezone, project)
        for row in rows:
            try:
                note = self.vault.read(v, row["note_id"])
                readable = note["revision"] == row["revision"]
            except (OSError, KnowledgeLibraryError):
                note = {}
                readable = False
            sources.append(
                {
                    "id": row["id"],
                    "noteId": row["note_id"],
                    "revision": row["revision"],
                    "occurredAtMs": row["occurred_ms"],
                    "readable": readable,
                    "text": note.get("markdown", "") if readable else "",
                    "authorship": "observed_file" if row["request_id"].startswith("watch:") else "user_saved",
                    "submission": "unknown",
                    "completeness": "selected_only",
                }
            )
        identity = sha(json.dumps([v["id"], day, timezone, project]))[:32]
        with self.store.connection() as db:
            latest_model = db.execute("SELECT id FROM knowledge_vault_diary_drafts WHERE vault_id=? AND day=? AND timezone=? AND project=? ORDER BY created_ms DESC,id DESC LIMIT 1",
                (v["id"],day,timezone,project)).fetchone()
        digest = sha(json.dumps([sources,latest_model[0] if latest_model else None], ensure_ascii=False))
        with self.store.connection() as db:
            old = db.execute(
                "SELECT * FROM knowledge_vault_days WHERE id=?", (identity,)
            ).fetchone()
            revision = (old["revision"] + int(old["digest"] != digest)) if old else 1
            db.execute(
                "INSERT OR REPLACE INTO knowledge_vault_days VALUES(?,?,?,?,?,?,?)",
                (identity, v["id"], day, timezone, project, digest, revision),
            )
        lines = [
            f"# {day} 工作回顾",
            "",
            "机器整理 · 仅覆盖获准文件与已准入的来源材料，不代表全天记录或已经完成的工作。",
            "",
        ]
        for source in sources:
            lines += [
                "## 保存的片段",
                "\n".join("> " + line for line in source["text"].splitlines())
                if source["readable"]
                else "来源已变化或不可读，原陈述不再作为依据。",
                f"来源：{source['noteId']} · {source['revision'][:8]}",
                "",
            ]
        with self.store.connection() as db:
            drafts = db.execute("SELECT * FROM knowledge_vault_diary_drafts WHERE vault_id=? AND day=? AND timezone=? AND project=? ORDER BY created_ms DESC,id DESC LIMIT 20",
                (v["id"], day, timezone, project)).fetchall()
        model_drafts = []
        for draft in drafts:
            refs = json.loads(draft["sources_json"])
            try:
                self.sources(v, refs)
                current = True
            except (OSError, KnowledgeLibraryError):
                current = False
            model_drafts.append({"id": draft["id"], "markdown": draft["markdown"],
                "sourceRefs": refs, "sourcesCurrent": current, "generator": draft["generator"],
                "createdAtMs": draft["created_ms"], "state": "draft_saved", "derived": True})
        personal = None
        template = self.policy(v)["personalDiary"]
        if template:
            # Separately authorized exact date file, never indexed or cloud sent.
            narrow = dict(v)
            narrow["excluded_json"] = "[]"
            narrow["personal_pattern"] = ""
            path = template.replace("{date}", day)
            try:
                body, _ = self.vault._read_path(narrow, path)
                personal = {"path": path, "markdown": body, "readOnly": True}
            except (OSError, KnowledgeLibraryError):
                personal = {"path": path, "unavailable": True}
        return {
            "id": identity,
            "date": day,
            "timezone": timezone,
            "revision": revision,
            "project": project,
            "sources": sources,
            "markdown": "\n".join(lines),
            "modelDrafts": model_drafts,
            "personalDiary": personal,
            "derived": True,
        }

    def sources(self, v, refs):
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
            raise KnowledgeLibraryError(
                "需要 1–8 个可核对的原始来源。", code="sources_required"
            )
        result = []
        activity_cache = {}
        for ref in refs:
            if not isinstance(ref, dict):
                raise KnowledgeLibraryError("来源格式无效。", code="invalid_argument")
            if ref.get("activityId"):
                note = self.activity_source(v, str(ref["activityId"]), activity_cache)
            else:
                note = self.vault.read(v, str(ref.get("noteId", "")))
            if ref.get("revision") != note["revision"]:
                raise KnowledgeConflictError("来源已变化，请重新整理。")
            if note["path"].startswith(self.policy(v)["inbox"] + "/work-"):
                raise KnowledgeLibraryError(
                    "请引用日记的原始材料，不重复计算派生摘要。", code="derived_source"
                )
            result.append(note)
        return result

    def prepare(self, v, p):
        note = self.vault.read(v, text(p, "noteId", 80))
        if p.get("baseRevision") != note["revision"]:
            raise KnowledgeConflictError("笔记已变化，请重新读取后提出修改。")
        refs = p.get("sourceRefs", [])
        self.sources(v, refs)
        before, after = text(p, "before"), text(p, "after")
        if (
            not after.strip()
            or before == after
            or (before and note["markdown"].count(before) != 1)
        ):
            raise KnowledgeConflictError(
                "修改片段需唯一且确实发生变化；留空原片段表示追加。"
            )
        body = (
            note["markdown"].replace(before, after, 1)
            if before
            else note["markdown"].rstrip() + "\n\n" + after + "\n"
        )
        edits = json.dumps({"before": before, "after": after}, ensure_ascii=False)
        identity = sha(
            json.dumps(
                [v["id"], note["noteId"], note["revision"], edits, refs], sort_keys=True
            )
        )[:32]
        with self.store.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO knowledge_note_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    identity,
                    v["id"],
                    note["noteId"],
                    1,
                    note["revision"],
                    edits,
                    json.dumps(refs),
                    text(p, "reason", 1000),
                    "prepared",
                    str(uuid.uuid4()),
                    sha(body),
                    now_ms(),
                    text(p, "generator", 120) or "user",
                    text(p, "project", 240),
                ),
            )
        return self.public(v, self.proposal(v, identity))

    def proposal(self, v, identity):
        with self.store.connection() as db:
            row = db.execute(
                "SELECT * FROM knowledge_note_proposals WHERE id=? AND vault_id=?",
                (identity, v["id"]),
            ).fetchone()
        if not row:
            raise KnowledgeNotFoundError("提案不存在。")
        return row

    def materialize(self, v, row):
        self.sources(v, json.loads(row["sources_json"]))
        current = self.vault.read(v, row["note_id"])
        if current["revision"] != row["base_revision"]:
            raise KnowledgeConflictError(
                "原文已经改变；旧提案未应用，请保留双方内容重新审核。"
            )
        edit = json.loads(row["edits_json"])
        body = current["markdown"]
        if edit["before"]:
            if body.count(edit["before"]) != 1:
                raise KnowledgeConflictError("无法唯一定位修改片段。")
            body = body.replace(edit["before"], edit["after"], 1)
        else:
            body = body.rstrip() + "\n\n" + edit["after"] + "\n"
        if sha(body) != row["after_revision"]:
            raise KnowledgeConflictError("提案内容校验失败。")
        return body

    def public(self, v, row):
        edit = json.loads(row["edits_json"])
        # No stale original/source body is exposed after scope or version changes.
        try:
            current = self.vault.read(v, row["note_id"])
            self.sources(v, json.loads(row["sources_json"]))
            readable = True
        except (OSError, KnowledgeLibraryError):
            current = {}
            readable = False
        return {
            "id": row["id"],
            "noteId": row["note_id"],
            "revision": row["revision"],
            "baseRevision": row["base_revision"],
            "afterRevision": row["after_revision"],
            "state": row["state"],
            "applicationId": row["application_id"],
            "project": row["project"],
            "path": current.get("path", ""),
            "sourceRefs": json.loads(row["sources_json"]) if readable else [],
            "reason": row["reason"] if readable else "来源不可用",
            "readable": readable,
            "conflict": bool(
                current
                and current["revision"]
                not in {row["base_revision"], row["after_revision"]}
            ),
            "diff": "\n".join(
                difflib.unified_diff(
                    edit["before"].splitlines(),
                    edit["after"].splitlines(),
                    fromfile="原片段",
                    tofile="建议片段",
                    lineterm="",
                )
            )
            if readable
            else "",
            "generator": row["generator"],
        }

    def editor(self, token, p):
        with self.vault.lock:
            v = self.vault._vault(str(p.get("vaultId", "")))
            with self.store.connection() as db:
                pair = db.execute(
                    "SELECT token_hash FROM knowledge_editor_pairings WHERE vault_id=?",
                    (v["id"],),
                ).fetchone()
            if (
                not pair
                or not token
                or not hmac.compare_digest(pair[0], sha(token))
                or v["paused"]
            ):
                raise KnowledgeLibraryError(
                    "编辑器配对失效或文件夹已暂停。", code="editor_unauthorized"
                )
            if str(Path(str(p.get("root", ""))).resolve()) != v["root"]:
                raise KnowledgeLibraryError(
                    "编辑器打开了不同的笔记库。", code="scope_mismatch"
                )
            action = p.get("action")
            if action == "pending":
                return self.dispatch(v, {"action": "proposals"})
            row = self.proposal(v, str(p.get("proposalId", "")))
            if action == "begin":
                if row["state"] not in {"waiting_editor", "applying"}:
                    raise KnowledgeConflictError("提案尚未批准或已结束。")
                body = self.materialize(v, row)
                current = self.vault.read(v, row["note_id"])
                with self.store.connection() as db:
                    db.execute(
                        "INSERT OR IGNORE INTO knowledge_note_applications VALUES(?,?,?,?,?,?)",
                        (
                            row["application_id"],
                            row["id"],
                            row["base_revision"],
                            row["after_revision"],
                            "intent",
                            now_ms(),
                        ),
                    )
                    db.execute(
                        "UPDATE knowledge_note_proposals SET state='applying' WHERE id=?",
                        (row["id"],),
                    )
                return {
                    "applicationId": row["application_id"],
                    "path": current["path"],
                    "beforeRevision": row["base_revision"],
                    "afterRevision": row["after_revision"],
                    "markdown": body,
                }
            if action == "receipt":
                if p.get("applicationId") != row["application_id"] or row[
                    "state"
                ] not in {"applying", "saved", "saved_index_pending"}:
                    raise KnowledgeConflictError("应用回执与写入意图不匹配。")
                current = self.vault.read(v, row["note_id"])
                if current["revision"] != row["after_revision"]:
                    raise KnowledgeConflictError("未观察到批准版本的正文，未标记成功。")
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE knowledge_note_proposals SET state='saved_index_pending' WHERE id=?",
                        (row["id"],),
                    )
                    db.execute(
                        "UPDATE knowledge_note_applications SET state='saved',updated_ms=? WHERE application_id=?",
                        (now_ms(), row["application_id"]),
                    )
                try:
                    self.vault.snapshot(v)
                except (OSError, KnowledgeLibraryError):
                    return {"state": "saved_index_pending", "bodySaved": True}
                with self.store.connection() as db:
                    db.execute(
                        "UPDATE knowledge_note_proposals SET state='saved' WHERE id=?",
                        (row["id"],),
                    )
                return {"state": "saved", "bodySaved": True}
            raise KnowledgeLibraryError("不支持的编辑动作。", code="invalid_argument")
