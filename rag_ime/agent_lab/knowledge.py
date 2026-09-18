"""Project Knowledge resources composed from existing Lab/Knowledge owners.

Trial owns admission, durable progress, cancellation and restart settlement.
RagBenchmarkSandbox owns isolated Knowledge libraries. The existing retrieval
evaluator owns metric definitions. This adapter only connects their contracts.
"""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import math
import os
import re
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .knowledge_data import (
    MAX_BYTES, MAX_DOCUMENT_BYTES, KnowledgeIntakeError, collect_folder, content_identities, digest,
    file_hash, normalize_cases, normalize_documents, read_case_file,
    read_jsonl, write_json, write_jsonl,
)
from .knowledge_package import read_index_package, match_corpus
from ..embeddings import embedding_provider_from_env, embedding_provider_info
from ..knowledge_embedding_profile import embedding_environment_from_settings, normalize_knowledge_embedding_profile
from ..knowledge_library import KnowledgeLibraryConfig, KnowledgeLibraryService
from ..knowledge_library.dense import NullDenseIndex, dense_index_from_env
from ..knowledge_library.rerank import knowledge_reranker_from_env
from ..rag_benchmark_sandbox import RagBenchmarkSandbox, RagBenchmarkSandboxPolicy
from ..rag_retrieval_experiment import evaluate_retrieval_configuration

SCENE_ID = "knowledge-resource"
_SCHEMA = "paw.lab-knowledge-resource.v1"
_JOB = re.compile(r"^lab-trial:[a-f0-9]{32}$")
_ACTIVE = {"queued", "running", "cancelling"}
_PROFILE_FIELDS = {"provider", "model", "baseUrl", "dimensions", "secretReference", "queryPrefix", "documentPrefix", "denseBackend"}
_POLICY = RagBenchmarkSandboxPolicy(max_runs=1, max_documents_per_run=20_000, max_total_source_bytes=MAX_BYTES,
                                   max_document_bytes=MAX_DOCUMENT_BYTES, max_search_calls=100_000)


def _string(value: object, name: str, *, limit: int = 1000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise KnowledgeIntakeError(f"{name}需要有效文本。")
    return value


def _integer(value: object, name: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise KnowledgeIntakeError(f"{name}需要是 {low}–{high} 之间的整数。")
    return value


def retrieval_profile(value: object) -> dict:
    if not isinstance(value, Mapping) or set(value) - {"mode", "topK", "threshold", "rerank", "candidateDepth", "contextChars"}:
        raise KnowledgeIntakeError("检索配置字段无效。")
    mode = value.get("mode", "lexical")
    if mode not in {"lexical", "dense", "hybrid"}:
        raise KnowledgeIntakeError("请选择关键词、语义或混合检索。")
    threshold = value.get("threshold", 0.0)
    if type(threshold) not in {int, float} or not math.isfinite(threshold) or not 0 <= threshold <= 1:
        raise KnowledgeIntakeError("检索阈值需要在 0–1 之间。")
    top_k = _integer(value.get("topK", 10), "Top K", 1, 20)
    rerank = value.get("rerank", False)
    if type(rerank) is not bool:
        raise KnowledgeIntakeError("重排开关无效。")
    return {"mode": mode, "topK": top_k, "threshold": threshold, "rerank": rerank,
            "candidateDepth": _integer(value.get("candidateDepth", 40), "重排候选数", top_k, 100),
            "contextChars": _integer(value.get("contextChars", 16000), "回答证据字数预算", 1000, 60000)}


class AgentLabKnowledgeResource:
    def __init__(self, root: str | Path, *, read_trials: Callable[[str], dict],
                 settings: Callable[[], Mapping] = lambda: {}, knowledge_client: Callable[[], Any] = lambda: None):
        self.root = Path(root).expanduser().resolve()
        self.read_trials, self.settings, self.knowledge_client = read_trials, settings, knowledge_client
        self._upload_lock = threading.RLock()

    def upload(self, project_id: str, request_id: str, value: Mapping) -> dict:
        """Idempotent bounded browser upload, without a model or a Trial job."""
        operation = value.get("operation")
        with self._upload_lock:
            if operation == "upload_begin":
                if set(value) != {"operation", "name", "bytes", "sha256"}:
                    raise KnowledgeIntakeError("文件上传参数无效。")
                name = _string(value["name"], "文件名", limit=240)
                size = _integer(value["bytes"], "文件大小", 1, MAX_BYTES)
                if not isinstance(value["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"]):
                    raise KnowledgeIntakeError("上传文件缺少有效的内容摘要。")
                upload_id = "upload:" + digest([project_id, request_id])
                root = self.root / "uploads" / upload_id.partition(":")[2]
                manifest = {"projectId": project_id, "uploadId": upload_id, "name": name, "bytes": size,
                            "sha256": value["sha256"], "chunkBytes": 512 * 1024}
                root.mkdir(parents=True, exist_ok=True, mode=0o700)
                record = root / "manifest.json"
                replayed = record.exists()
                if replayed:
                    if json.loads(record.read_text()) != manifest:
                        raise KnowledgeIntakeError("此上传请求已绑定另一份文件。")
                else:
                    write_json(record, manifest)
                return {"upload": manifest, "replayed": replayed}
            upload_id = value.get("uploadId")
            if not isinstance(upload_id, str) or not re.fullmatch(r"upload:[a-f0-9]{64}", upload_id):
                raise KnowledgeIntakeError("上传标识无效，请重新选择文件。")
            root = self.root / "uploads" / upload_id.partition(":")[2]
            record = root / "manifest.json"
            if not record.is_file():
                raise KnowledgeIntakeError("原上传记录不存在，请重新选择文件。")
            manifest = json.loads(record.read_text())
            if manifest["projectId"] != project_id:
                raise KnowledgeIntakeError("上传文件不属于此项目。")
            count = math.ceil(manifest["bytes"] / manifest["chunkBytes"])
            if operation == "upload_chunk":
                if set(value) != {"operation", "uploadId", "index", "data"} or not isinstance(value["data"], str) or len(value["data"]) > 710000:
                    raise KnowledgeIntakeError("上传分片参数无效。")
                number = _integer(value["index"], "上传分片序号", 0, count - 1)
                try:
                    data = base64.b64decode(value["data"], validate=True)
                except ValueError as exc:
                    raise KnowledgeIntakeError("上传分片不是有效的 Base64 数据。") from exc
                expected = min(manifest["chunkBytes"], manifest["bytes"] - number * manifest["chunkBytes"])
                if len(data) != expected:
                    raise KnowledgeIntakeError("上传分片长度不匹配，请重试原分片。")
                path = root / f"chunk-{number:04d}"
                replayed = path.exists()
                if replayed:
                    if path.read_bytes() != data:
                        raise KnowledgeIntakeError("原分片内容不匹配，请重新选择文件。")
                else:
                    with path.open("xb") as out:
                        os.chmod(path, 0o600)
                        out.write(data)
                return {"upload": {"uploadId": upload_id, "index": number, "receivedBytes": expected}, "replayed": replayed}
            if operation != "upload_seal" or set(value) != {"operation", "uploadId"}:
                raise KnowledgeIntakeError("上传操作无效。")
            destination = root / ("source" + Path(manifest["name"]).suffix.lower())
            replayed = destination.exists()
            if not replayed:
                temporary = root / ("assembling-" + uuid.uuid4().hex)
                try:
                    with temporary.open("xb") as out:
                        os.chmod(temporary, 0o600)
                        for number in range(count):
                            path = root / f"chunk-{number:04d}"
                            if not path.is_file():
                                raise KnowledgeIntakeError("上传尚未完整，请重新选择同一文件以继续。")
                            out.write(path.read_bytes())
                    if temporary.stat().st_size != manifest["bytes"] or file_hash(temporary) != manifest["sha256"]:
                        raise KnowledgeIntakeError("上传文件校验未通过，请重新选择原文件。")
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
            if file_hash(destination) != manifest["sha256"]:
                raise KnowledgeIntakeError("原上传文件发生变化，请重新导入。")
            return {"upload": {**manifest, "ready": True}, "replayed": replayed}

    def _uploaded_path(self, project_id: str, upload_id: object) -> Path:
        if not isinstance(upload_id, str) or not re.fullmatch(r"upload:[a-f0-9]{64}", upload_id):
            raise KnowledgeIntakeError("上传文件标识无效。")
        root = self.root / "uploads" / upload_id.partition(":")[2]
        manifest = json.loads((root / "manifest.json").read_text())
        if manifest["projectId"] != project_id:
            raise KnowledgeIntakeError("上传文件不属于此项目。")
        path = root / ("source" + Path(manifest["name"]).suffix.lower())
        if not path.is_file() or file_hash(path) != manifest["sha256"]:
            raise KnowledgeIntakeError("上传未完成或文件已变化，请重新选择同一文件。")
        return path

    def _path(self, job_id: str) -> Path:
        if not isinstance(job_id, str) or not _JOB.fullmatch(job_id):
            raise KnowledgeIntakeError("知识库任务标识无效。")
        return self.root / job_id.partition(":")[2]

    def _jobs(self, project_id: str) -> list[dict]:
        return [job for job in self.read_trials("").get("jobs", [])
                if job.get("sceneId") == SCENE_ID and job.get("publicSpec", {}).get("projectId") == project_id]

    def project_resources(self) -> dict[str, dict]:
        """Small navigation projection from completed owner receipts, once per catalog read.

        Counts do not copy corpus content or trigger parsing/indexing. Document
        and chunk counts describe the latest completed index (or corpus while
        no index exists), not the sum of overlapping resource versions.
        """
        grouped: dict[str, list[dict]] = {}
        for job in self.read_trials("").get("jobs", []):
            result = job.get("result")
            project_id = job.get("publicSpec", {}).get("projectId")
            if (job.get("sceneId") == SCENE_ID and job.get("state") == "completed"
                    and isinstance(project_id, str) and project_id and isinstance(result, dict)
                    and result.get("schemaVersion") == _SCHEMA and result.get("kind") in {"corpus", "index", "dataset"}):
                grouped.setdefault(project_id, []).append(job)
        resources = {}
        for project_id, jobs in grouped.items():
            jobs.sort(key=lambda job: (int(job.get("updatedAtMs") or job.get("createdAtMs") or 0), str(job.get("jobId") or "")), reverse=True)
            corpora = [job for job in jobs if job["result"]["kind"] == "corpus"]
            indexes = [job for job in jobs if job["result"]["kind"] == "index"]
            datasets = [job for job in jobs if job["result"]["kind"] == "dataset"]
            latest = (indexes or corpora or [{}])[0].get("result", {})
            resources[project_id] = {"corpusCount": len(corpora), "indexCount": len(indexes), "datasetCount": len(datasets),
                "documentCount": max(0, int(latest.get("documentCount") or 0)),
                "chunkCount": max(0, int(latest.get("chunkCount") or 0))}
        return resources

    def read(self, project_id: str) -> dict:
        jobs = self._jobs(project_id)
        finished = [job for job in jobs if job.get("state") == "completed" and isinstance(job.get("result"), dict)
                    and job["result"].get("schemaVersion") == _SCHEMA]
        corpora = [job["result"] for job in finished if job["result"].get("kind") == "corpus"]
        indexes = [job["result"] for job in finished if job["result"].get("kind") == "index"]
        datasets = [job["result"]["dataset"] for job in finished if job["result"].get("dataset")]
        evaluations = [job["result"] for job in finished if job["result"].get("kind") == "evaluation"]
        try:
            embedding = normalize_knowledge_embedding_profile(self.settings())
        except ValueError:
            embedding = {"provider": "unavailable", "model": "", "profileSha256": ""}
        return {"schemaVersion": _SCHEMA, "corpora": corpora, "indexes": indexes, "datasets": datasets,
                "evaluations": evaluations, "jobs": jobs[:30], "embedding": embedding,
                "limits": {"documents": 20000, "corpusBytes": MAX_BYTES, "cases": 10000},
                "owners": ["KnowledgeLibraryService", "RagBenchmarkSandbox", "evaluate_retrieval_configuration", "AgentLabTrialApplication"]}

    def _dependency(self, job_id: object, project_id: str, kind: str) -> dict:
        identifier = _string(job_id, "关联任务", limit=100)
        self._path(identifier)
        job = self.read_trials(identifier).get("job", {})
        result = job.get("result")
        if (job.get("sceneId") != SCENE_ID or job.get("publicSpec", {}).get("projectId") != project_id
                or job.get("state") != "completed" or not isinstance(result, dict) or result.get("schemaVersion") != _SCHEMA
                or (not result.get("dataset") if kind == "dataset" else result.get("kind") != kind)):
            raise KnowledgeIntakeError("所选来源不属于此项目，或原任务尚未完成。请重新读取。")
        return copy.deepcopy(result)

    def prepare(self, spec: Mapping, job_id: str) -> Mapping:
        if not isinstance(spec, Mapping):
            raise KnowledgeIntakeError("知识库操作无效。")
        operation = spec.get("operation")
        allowed = {
            "import_corpus": {"path", "uploadId", "fields"},
            "connect_base": {"kbId"},
            "import_dataset": {"corpusId", "path", "uploadId", "fields"},
            "index": {"corpusId", "chunking", "embedding"},
            "restore_index": {"corpusId", "packagePath"},
            "search": {"indexId", "profile", "query"},
            "evaluate": {"indexId", "datasetId", "profile", "split"},
        }
        if operation not in allowed or set(spec) - ({"operation", "projectId"} | allowed[operation]):
            raise KnowledgeIntakeError("此知识库操作或参数尚不支持。")
        project_id = _string(spec.get("projectId"), "项目标识", limit=240)
        public = {"operation": operation, "projectId": project_id}
        private = {**copy.deepcopy(dict(spec)), "jobId": job_id}
        if operation in {"import_corpus", "import_dataset"}:
            if bool(spec.get("path")) == bool(spec.get("uploadId")):
                raise KnowledgeIntakeError("请提供文件路径或已完成的上传文件，不能同时提供。")
            path = self._uploaded_path(project_id, spec["uploadId"]) if spec.get("uploadId") else Path(_string(spec.get("path"), "执行器上的资料路径", limit=4000)).expanduser()
            if not path.is_absolute() or not path.exists():
                raise KnowledgeIntakeError("路径必须是当前执行器上存在的绝对路径。")
            if operation == "import_dataset" and not path.is_file():
                raise KnowledgeIntakeError("评测集需要选择 JSONL 或 CSV 文件。")
            private["path"] = str(path.resolve())
            private["sourceName"] = json.loads((path.parent / "manifest.json").read_text())["name"] if spec.get("uploadId") else path.name
            public["sourceName"] = private["sourceName"]
            if not isinstance(spec.get("fields", {}), dict):
                raise KnowledgeIntakeError("字段映射必须是对象。")
        if operation == "connect_base":
            private["kbId"] = _string(spec.get("kbId"), "知识库标识", limit=240)
            public["kbId"] = private["kbId"]
        if operation in {"import_dataset", "index", "restore_index"}:
            corpus = self._dependency(spec.get("corpusId"), project_id, "corpus")
            private["corpus"] = corpus
            public.update(corpusId=corpus["jobId"], corpusHash=corpus["corpusHash"])
        if operation == "restore_index":
            path = Path(_string(spec.get("packagePath"), "已导出 App 路径", limit=4000)).expanduser()
            if not path.is_absolute() or not path.exists():
                raise KnowledgeIntakeError("请选择本机存在的完整已导出 App 目录或 ZIP。")
            package = read_index_package(path)
            match_corpus(package, self._documents(corpus), corpus["corpusHash"])
            private.update(packagePath=str(path.resolve()), packageHashes=package["hashes"])
            public.update(sourceName=path.name, packageHashes=package["hashes"], embeddingModelCalls=0)
        if operation == "index":
            chunking = spec.get("chunking", {})
            if not isinstance(chunking, Mapping) or set(chunking) - {"strategy", "size", "overlap"}:
                raise KnowledgeIntakeError("切片配置无效。")
            strategy = chunking.get("strategy", "markdown")
            if strategy not in {"general", "markdown", "fixed", "qa"}:
                raise KnowledgeIntakeError("请选择段落、Markdown、固定长度或问答切片。")
            size = _integer(chunking.get("size", 1200), "切片长度", 200, 8000)
            overlap = _integer(chunking.get("overlap", 160), "切片重叠", 0, min(size - 1, 2000))
            private["chunking"] = {"strategy": strategy, "size": size, "overlap": overlap}
            choice = spec.get("embedding", "none")
            if choice not in {"none", "configured"}:
                raise KnowledgeIntakeError("请选择不使用向量或沿用知识库 Embedding 配置。")
            normalized = normalize_knowledge_embedding_profile(self.settings() if choice == "configured" else {"knowledgeLibrary": {"embedding": {"provider": "none"}}})
            if choice == "configured" and normalized["provider"] in {"none", "local-hash"}:
                raise KnowledgeIntakeError("尚未配置语义 Embedding。请先在知识库设置中配置真实模型，或使用关键词索引。")
            frozen = {key: normalized[key] for key in _PROFILE_FIELDS | {"modelReference", "modelRevision"} if key in normalized}
            private["embeddingSettings"] = {"knowledgeLibrary": {"embedding": frozen}}
            private["embeddingProfile"] = normalize_knowledge_embedding_profile(private["embeddingSettings"])
            public.update(chunking=private["chunking"], embedding=private["embeddingProfile"])
        if operation in {"search", "evaluate"}:
            index = self._dependency(spec.get("indexId"), project_id, "index")
            private["index"] = index
            profile = retrieval_profile(spec.get("profile", {}))
            if profile["mode"] != "lexical" and index.get("dense", {}).get("provider", {}).get("semantic") is not True:
                raise KnowledgeIntakeError("此索引没有语义向量；请创建使用真实 Embedding 的新索引。")
            private["profile"] = profile
            public.update(indexId=index["jobId"], corpusHash=index["corpusHash"], profile=profile)
        if operation == "search":
            private["query"] = _string(spec.get("query"), "检索问题", limit=20000)
        if operation == "evaluate":
            dataset = self._dependency(spec.get("datasetId"), project_id, "dataset")["dataset"]
            if dataset["corpusHash"] != private["index"]["corpusHash"]:
                raise KnowledgeIntakeError("评测集与索引的知识库版本不一致。")
            split = spec.get("split", "development")
            if split not in {"development", "holdout"}:
                raise KnowledgeIntakeError("请选择开发集或保留集。")
            if not dataset["splits"].get(split):
                raise KnowledgeIntakeError("此分组没有题目。共享来源会合并题目分组，请补充独立问题。")
            private["dataset"] = dataset
            public.update(datasetId=dataset["datasetId"], datasetHash=dataset["sha256"], split=split)
            uses = sum(job.get("publicSpec", {}).get("datasetHash") == dataset["sha256"]
                       and job.get("publicSpec", {}).get("split") == "holdout" for job in self._jobs(project_id))
            private["holdoutUseNumber"] = uses + 1 if split == "holdout" else 0
        return {"publicSpec": public, "privateInput": private}

    def execute(self, value: Mapping, observer: Any, cancelled: Callable[[], bool]) -> Mapping:
        root = self._path(value["jobId"])
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        try:
            result = self._execute(dict(value), root, observer, cancelled)
            if cancelled():
                return {"status": "cancelled", "message": "已停止，之前完成的知识库与索引仍保留。"}
            result.update(schemaVersion=_SCHEMA, jobId=value["jobId"], projectId=value["projectId"], status="completed")
            write_json(root / "result.json", result)
            return result
        except (KnowledgeIntakeError, ValueError) as exc:
            return {"status": "failed", "message": str(exc) if isinstance(exc, KnowledgeIntakeError) else "知识库处理失败，请检查来源和模型配置。"}
        except InterruptedError:
            return {"status": "cancelled", "message": "已停止，未完成的结果没有成为可用索引。"}

    def _execute(self, value: dict, root: Path, observer: Any, cancelled: Callable[[], bool]) -> dict:
        operation = value["operation"]
        if operation in {"import_corpus", "connect_base"}:
            details = {}
            observer.progress("正在整理知识库来源")
            if operation == "connect_base":
                documents, details = self._connected_base(value["kbId"], observer, cancelled)
                title = details["name"]
            else:
                path = Path(value["path"])
                title = value.get("sourceName", path.name)
                if path.is_dir():
                    documents, details = collect_folder(path, progress=observer.progress, cancelled=cancelled)
                else:
                    if path.suffix.lower() not in {".jsonl", ".ndjson"}:
                        raise KnowledgeIntakeError("语料文件请使用 JSONL；TXT、Markdown 或 HTML 请通过其所在文件夹接入。")
                    original_hash = file_hash(path)
                    documents = normalize_documents(read_jsonl(path), value.get("fields"))
                    if file_hash(path) != original_hash:
                        raise KnowledgeIntakeError("读取期间源文件发生变化，请重新导入以保存一致快照。")
                    details["sourceSha256"] = original_hash
            corpus_hash = write_jsonl(root / "corpus.jsonl", documents)
            result = {"kind": "corpus", "title": title, "corpusHash": corpus_hash, "documentCount": len(documents),
                      "byteSize": sum(row["byteSize"] for row in documents), "intake": details,
                      "preview": [{**{key: row[key] for key in ("sourceId", "title", "uri", "byteSize")}, "excerpt": row["text"][:500]} for row in documents[:8]],
                      "sourceReceipt": None, "indexReady": False}
            observer.progress(f"资料已整理 · {len(documents):,} 篇文档，尚未建立索引")
            return result
        if operation == "import_dataset":
            documents = self._documents(value["corpus"])
            cases, summary = normalize_cases(read_case_file(Path(value["path"])), documents, value.get("fields"))
            hashed = write_jsonl(root / "references.jsonl", cases)
            return {"kind": "dataset", "dataset": {**summary, "datasetId": value["jobId"], "corpusId": value["corpus"]["jobId"],
                    "corpusHash": value["corpus"]["corpusHash"], "sha256": hashed, "title": value.get("sourceName", Path(value["path"]).name),
                    "provenance": "user_import"}}
        if operation == "index":
            return self._index(value, root, observer, cancelled)
        if operation == "restore_index":
            return self._restore_index(value, root, observer, cancelled)
        if operation in {"search", "evaluate"}:
            index = value["index"]
            corpus = self._dependency(index["corpusId"], value["projectId"], "corpus")
            documents = self._documents(corpus)
            _, source_aliases = content_identities(documents)
            by_external = {row["externalId"]: row for row in documents}
            with self._sandbox(index) as sandbox:
                def search(query: str) -> dict:
                    if cancelled():
                        raise InterruptedError("已停止检索。")
                    return self._search(sandbox, index, value["profile"], query, by_external)
                if operation == "search":
                    observer.progress("正在从索引检索证据")
                    return {"kind": "search", "indexId": index["jobId"], "query": value["query"], **search(value["query"])}
                cases = [row for row in self._cases(value["dataset"]) if row["split"] == value["split"]]
                eligible = [row for row in cases if row["retrievalEvaluable"]]
                if not eligible:
                    raise KnowledgeIntakeError("此分组没有参考来源标注，不能计算检索命中率。可补充来源 ID，或继续回答评测。")
                count = 0
                # Corpus IDs can be URLs, paths or Unicode. The metric owner
                # accepts bounded identifiers; encode at this adapter boundary
                # while retaining the original IDs in intake/search receipts.
                def metric_id(kind: str, identity: str) -> str:
                    return kind + ':' + hashlib.sha256(identity.encode('utf-8')).hexdigest()
                def retrieve(query: dict, _config: dict) -> list[str]:
                    nonlocal count
                    result = search(str(query["text"]))
                    count += 1
                    observer.progress(f"检索评测 · {count}/{len(eligible)} 题")
                    return list(dict.fromkeys(metric_id('source', hit["sourceId"]) for hit in result["hits"]))
                k = value["profile"]["topK"]
                report = evaluate_retrieval_configuration(
                    cases=[{"queryId": metric_id('query', row["caseId"]), "system": "knowledge", "query": row["question"],
                            "split": "validation" if value["split"] == "development" else "held_out",
                            "relevant": {metric_id('source', source_aliases[source]): 1 for source in row["sourceIds"]}} for row in eligible],
                    config=value["profile"], retrieve=retrieve, k_values=tuple(sorted({n for n in (1, 3, 5, 10, k) if n <= k})))
                write_json(root / "retrieval-private.json", report)
                report.pop("privateCases", None)
                return {"kind": "evaluation", "indexId": index["jobId"], "corpusHash": index["corpusHash"],
                        "indexConfig": {"chunking": index["chunking"], "embedding": index["embeddingProfile"], "configHash": index["configHash"]},
                        "datasetId": value["dataset"]["datasetId"], "datasetHash": value["dataset"]["sha256"],
                        "split": value["split"], "plannedCount": len(cases), "evaluatedCount": len(eligible),
                        "unlabeledCount": len(cases) - len(eligible), "report": report, "profile": value["profile"],
                        "holdoutUseNumber": value.get("holdoutUseNumber", 0), "answerModelCalls": 0,
                        "embeddingQueryCalls": count if value["profile"]["mode"] != "lexical" else 0,
                        "rerankerCalls": count if value["profile"]["rerank"] else 0,
                        "qualityBoundary": "retrieval_only", "providerCost": None}
        raise KnowledgeIntakeError("此知识库操作尚不支持。")

    def _documents(self, corpus: Mapping) -> list[dict]:
        path = self._path(corpus["jobId"]) / "corpus.jsonl"
        if file_hash(path) != corpus["corpusHash"]:
            raise KnowledgeIntakeError("知识库快照发生变化；请重新导入，已有实验记录仍保留。")
        return read_jsonl(path)

    def _cases(self, dataset: Mapping) -> list[dict]:
        path = self._path(dataset["datasetId"]) / "references.jsonl"
        if file_hash(path) != dataset["sha256"]:
            raise KnowledgeIntakeError("评测集快照发生变化，请重新导入。")
        return read_jsonl(path, maximum=10000)

    def _index(self, value: dict, root: Path, observer: Any, cancelled: Callable[[], bool]) -> dict:
        original_documents = self._documents(value["corpus"])
        documents, aliases = content_identities(original_documents)
        duplicate_aliases = {source: canonical for source, canonical in aliases.items() if source != canonical}
        index = {"jobId": value["jobId"], "projectId": value["projectId"], "embeddingSettings": value["embeddingSettings"]}
        with self._sandbox(index) as sandbox:
            run = sandbox.create_run(value["projectId"], label=value["corpus"]["title"][:200])
            index["runId"] = run["runId"]
            sandbox.create_base(value["projectId"], run["runId"], alias="corpus", name=value["corpus"]["title"][:240],
                                chunking_config=value["chunking"], retrieval_config={"mode": "lexical", "graphEnabled": False})
            for offset in range(0, len(documents), 32):
                if cancelled():
                    raise InterruptedError("已停止建索引。")
                observer.progress(f"解析与建索引 · {offset}/{len(documents)} 篇文档")
                sandbox.import_documents(value["projectId"], run["runId"], base_alias="corpus", documents=[
                    {"externalId": row["externalId"], "name": re.sub(r"[/\\\x00]", "-", row["title"])[:196] + ".md",
                     "text": row["text"], "mimeType": "text/markdown"}
                    for row in documents[offset:offset + 32]])
            status = sandbox.status(value["projectId"], run["runId"])
            base = status["bases"][0]
            if base["readyDocumentCount"] != len(documents) or base["reindexRequired"]:
                raise KnowledgeIntakeError("部分文档未完成索引；此次索引没有成为可用版本。")
            dense = status["dense"]
            if value["embeddingProfile"]["provider"] != "none" and (not dense["available"] or dense["vectorCount"] < base["chunkCount"]):
                raise KnowledgeIntakeError("语义向量未完整建立；请检查 Embedding 服务后重试。关键词索引未冒充语义索引。")
            observer.progress(f"索引完成 · {len(documents):,} 篇独立正文 / {base['chunkCount']:,} 个切片 · 保留 {len(original_documents):,} 个来源")
            return {**index, "kind": "index", "corpusId": value["corpus"]["jobId"], "corpusHash": value["corpus"]["corpusHash"],
                    "title": value["corpus"]["title"], "documentCount": len(documents), "chunkCount": base["chunkCount"],
                    "sourceCount": len(original_documents), "duplicateSourceCount": len(duplicate_aliases),
                    "sourceAliases": duplicate_aliases, "identityPolicy": "exact-body-sha256; first-source-in-corpus",
                    "chunking": value["chunking"], "embeddingProfile": value["embeddingProfile"], "dense": dense,
                    "reranker": status["reranker"], "configHash": digest({"chunking": value["chunking"], "embedding": value["embeddingProfile"]})}

    def _restore_index(self, value: dict, root: Path, observer: Any, cancelled: Callable[[], bool]) -> dict:
        package = read_index_package(Path(value["packagePath"]))
        if package["hashes"] != value["packageHashes"]:
            raise KnowledgeIntakeError("索引包在准入后发生变化，请保留旧任务并重新选择。")
        original_documents = self._documents(value["corpus"])
        mapping = match_corpus(package, original_documents, value["corpus"]["corpusHash"])
        documents, aliases = content_identities(original_documents)
        dense = package["dense"]
        # Only public encoding options become the new run's provider settings.
        encoding = {key: dense["provider"][key] for key in
                    ("provider", "model", "modelRevision", "modelReference", "queryPrefix", "documentPrefix", "dimensions")
                    if key in dense["provider"]} if dense else {"provider": "none"}
        encoding["denseBackend"] = "sqlite-exact"
        settings = {"knowledgeLibrary": {"embedding": encoding}}
        profile = normalize_knowledge_embedding_profile(settings)
        chunking = {key: package["search"]["base"]["chunkingConfig"][key] for key in ("strategy", "size", "overlap")}
        index = {"jobId": value["jobId"], "projectId": value["projectId"], "embeddingSettings": settings}
        if cancelled():
            raise InterruptedError("已停止恢复索引。")
        observer.progress(f"正在核对并恢复 {len(documents):,} 篇全文的冻结索引，不重新计算文档向量")
        with self._sandbox(index) as sandbox:
            run = sandbox.create_run(value["projectId"], label=value["corpus"]["title"][:200])
            index["runId"] = run["runId"]
            sandbox.restore_search_snapshot(value["projectId"], run["runId"], base_alias="corpus",
                snapshot=package["search"], external_document_ids=mapping, dense_snapshot=dense, cancelled=cancelled)
            if cancelled():
                raise InterruptedError("已停止恢复索引。")
            status = sandbox.status(value["projectId"], run["runId"])
            base = status["bases"][0]
            if (base["readyDocumentCount"] != len(documents) or base["reindexRequired"]
                    or (dense is not None and status["dense"]["vectorCount"] != base["chunkCount"])):
                raise KnowledgeIntakeError("冻结索引未完整恢复，此次任务没有成为可用索引。")
        return {**index, "kind": "index", "corpusId": value["corpus"]["jobId"], "corpusHash": value["corpus"]["corpusHash"],
                "title": value["corpus"]["title"], "documentCount": len(documents), "chunkCount": base["chunkCount"],
                "sourceCount": len(original_documents), "duplicateSourceCount": len(original_documents) - len(documents),
                "sourceAliases": {s: c for s, c in aliases.items() if s != c},
                "identityPolicy": "exact-body-sha256; first-source-in-corpus", "chunking": chunking,
                "embeddingProfile": profile, "dense": status["dense"], "reranker": status["reranker"],
                "configHash": digest({"chunking": chunking, "embedding": profile}),
                "indexReuse": {"sourceIndexId": package["knowledge"]["sourceIndexId"], "packageHashes": package["hashes"],
                    "documentCount": len(documents), "chunkCount": base["chunkCount"], "embeddingModelCalls": 0,
                    "verification": "complete_corpus_hash_source_identity_and_chunk_content"}}

    @contextmanager
    def _sandbox(self, index: Mapping):
        root = self._path(index["jobId"]) / "sandbox"
        environment = embedding_environment_from_settings(index["embeddingSettings"])
        provider = embedding_provider_from_env(environment)
        reranker = knowledge_reranker_from_env(root)
        def factory(path: Path, policy: RagBenchmarkSandboxPolicy) -> KnowledgeLibraryService:
            config = KnowledgeLibraryConfig(path, max_source_bytes=policy.max_document_bytes)
            service = KnowledgeLibraryService(config, dense_index=NullDenseIndex(), background_jobs=False)
            service.dense_index = dense_index_from_env(config.database_path, provider, environment)
            return service
        sandbox = RagBenchmarkSandbox(root, policy=_POLICY, service_factory=factory, reranker=reranker)
        try:
            yield sandbox
        finally:
            sandbox.close()

    @staticmethod
    def _search(sandbox: RagBenchmarkSandbox, index: Mapping, profile: dict, query: str, documents: dict) -> dict:
        result = sandbox.search(index["projectId"], index["runId"], base_alias="corpus", query=query,
                                top_k=profile["topK"], mode=profile["mode"], threshold=profile["threshold"],
                                rerank=profile["rerank"], rerank_candidate_depth=profile["candidateDepth"])
        if profile["mode"] in {"dense", "hybrid"} and not result.get("retrieval", {}).get("denseCandidates"):
            raise KnowledgeIntakeError("语义查询未返回可用的冻结向量候选；本次检索不能回退到关键词结果。")
        hits = []
        for hit in result["hits"]:
            source = documents.get(hit["externalDocumentId"])
            if source is None:
                raise KnowledgeIntakeError("检索命中了此知识库以外的来源。")
            hits.append({**hit, "sourceId": source["sourceId"], "title": source["title"], "uri": source["uri"]})
            aliases = {source["sourceId"]} | {alias for alias, canonical in index.get("sourceAliases", {}).items() if canonical == source["sourceId"]}
            if len(aliases) > 1:
                hits[-1]["sourceAliases"] = [{key: document[key] for key in ("sourceId", "title", "uri")}
                                             for document in documents.values() if document["sourceId"] in aliases]
        retrieval = result.get("retrieval", {})
        return {"hits": hits, "profile": profile, "reranker": retrieval.get("rerank", {}), "retrieval": retrieval}

    def _management(self, operation: str, *args, **kwargs):
        client = self.knowledge_client()
        if client is None:
            raise KnowledgeIntakeError("知识库服务尚未连接。可先导入文件或文件夹。")
        if callable(getattr(client, "management_call", None)):
            return client.management_call(operation, *args, **kwargs)
        method = getattr(client, operation, None)
        if not callable(method):
            raise KnowledgeIntakeError("当前知识库连接尚不支持读取来源快照。")
        return method(*args, **kwargs)

    def _connected_base(self, kb_id: str, observer: Any, cancelled: Callable[[], bool]) -> tuple[list[dict], dict]:
        from ..knowledge_library.parsers import ParserRouter
        import tempfile
        base = self._management("management_get_base", kb_id)
        listed = self._management("management_list_documents", kb_id).get("documents", [])
        if not listed or len(listed) > 20000:
            raise KnowledgeIntakeError("请选择包含 1–20,000 篇文档的知识库。")
        rows = []
        with tempfile.TemporaryDirectory(prefix="paw-knowledge-intake-") as temporary:
            parser = ParserRouter(KnowledgeLibraryConfig(Path(temporary)))
            for number, document in enumerate(listed, 1):
                if cancelled():
                    raise InterruptedError("已停止读取知识库。")
                observer.progress(f"正在复制知识库来源 · {number}/{len(listed)}")
                file_id = document.get("documentId") or document.get("fileId") or document.get("id")
                blob = self._management("management_read_source", kb_id, file_id)
                if len(blob.data) > MAX_DOCUMENT_BYTES:
                    raise KnowledgeIntakeError("已有知识库中包含超过 4 MB 的文档，请先在知识库中拆分该来源。")
                source = Path(temporary) / ("source" + Path(blob.file_name).suffix)
                source.write_bytes(blob.data)
                parsed = parser.parse(source, mode="builtin")
                rows.append({"id": str(file_id), "title": blob.file_name, "text": parsed.text,
                             "uri": f"knowledge://{kb_id}/{file_id}"})
        after = self._management("management_list_documents", kb_id).get("documents", [])
        if digest(listed) != digest(after):
            raise KnowledgeIntakeError("读取期间知识库文档发生变化，请重新连接以保存一致快照。")
        return normalize_documents(rows), {"kbId": kb_id, "name": base.get("name", "已连接知识库"),
                                           "sourceManifestHash": digest(listed), "sourceUnchanged": True}

    def answer_sources(self, binding: Mapping, question: str) -> list[dict]:
        project_id = _string(binding.get("projectId"), "项目标识", limit=240)
        index = self._dependency(binding.get("indexId"), project_id, "index")
        if binding.get("corpusHash") != index["corpusHash"] or binding.get("configHash") != index["configHash"]:
            raise KnowledgeIntakeError("回答任务绑定的知识库版本不一致。")
        profile = retrieval_profile(binding.get("profile", {}))
        corpus = self._dependency(index["corpusId"], project_id, "corpus")
        documents = self._documents(corpus)
        with self._sandbox(index) as sandbox:
            result = self._search(sandbox, index, profile, question, {row["externalId"]: row for row in documents})
        sources, remaining = [], profile["contextChars"]
        for hit in result["hits"]:
            if remaining <= 0:
                break
            content = hit["content"][:remaining]
            sources.append({"sourceId": hit["externalDocumentId"], "title": hit["title"], "uri": hit["uri"],
                            "text": content, "chunkId": hit["chunkId"], "citationNumber": len(sources) + 1})
            remaining -= len(content)
        return sources

    def app_resources(self, project_id: str, value: Mapping) -> dict:
        """Freeze portable retrieval data through the owning Knowledge APIs."""
        if not isinstance(value, Mapping) or set(value) - {"indexId", "profile", "queryField"}:
            raise KnowledgeIntakeError("应用知识库配置字段无效。")
        index = self._dependency(value.get("indexId"), project_id, "index")
        profile = retrieval_profile(value.get("profile", {}))
        if profile["rerank"]:
            raise KnowledgeIntakeError("当前独立应用包尚未冻结重排执行器，不能直接替换成非重排结果导出。")
        semantic = profile["mode"] in {"dense", "hybrid"}
        if semantic and index.get("dense", {}).get("provider", {}).get("semantic") is not True:
            raise KnowledgeIntakeError("此索引没有完整语义向量，不能直接替换为 dense/hybrid 导出。")
        field = value.get("queryField", "question")
        if not isinstance(field, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", field):
            raise KnowledgeIntakeError("应用的检索问题字段无效。")
        corpus = self._dependency(index["corpusId"], project_id, "corpus")
        documents = {row["externalId"]: row for row in self._documents(corpus)}
        with self._sandbox(index) as sandbox:
            snapshot = sandbox.export_search_snapshot(project_id, index["runId"], base_alias="corpus")
            if snapshot["base"]["retrievalConfig"].get("graphEnabled"):
                raise KnowledgeIntakeError("此应用未冻结图检索执行器，不能改变检索配置后导出。")
            if snapshot["base"]["retrievalConfig"].get("rerankEnabled"):
                raise KnowledgeIntakeError("此应用未冻结重排执行器，不能直接替换成非重排结果导出。")
            dense_snapshot = sandbox.export_dense_snapshot(project_id, index["runId"], base_alias="corpus") if semantic else None
        external = snapshot.pop("externalDocumentIds")
        snapshot["sources"] = {}
        for document in snapshot["documents"]:
            source = documents[external[document["id"]]]
            snapshot["sources"][document["id"]] = {
                "sourceId": source["externalId"], "originalSourceId": source["sourceId"], "title": source["title"],
                "uri": source["uri"] if source["uri"].startswith(("https://", "http://")) else "knowledge-source:" + source["externalId"],
                "sourceAliases": [alias for alias, canonical in index.get("sourceAliases", {}).items() if canonical == source["sourceId"]],
            }
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        if len(encoded.encode()) > 80 * 1024 * 1024:
            raise KnowledgeIntakeError("知识库应用快照超过 80 MB，请拆分应用知识范围后再准备。")
        owner = Path(__file__).parent.parent / "knowledge_library"
        names = ("store.py", "models.py", "permissions.py")
        files = {"knowledge/search-snapshot.json": encoded, "knowledge_owner/__init__.py": '"""Frozen PAW KnowledgeStore search owner."""\n',
                 "knowledge_runtime.py": Path(__file__).with_name("app_knowledge_runtime.py").read_text()}
        files.update({"knowledge_owner/" + name: (owner / name).read_text() for name in names})
        manifest = {"schemaVersion": "paw.lab-app-knowledge.v1", "sourceIndexId": index["jobId"], "corpusHash": index["corpusHash"],
                    "indexConfigHash": index["configHash"], "queryField": field, "profile": profile,
                    "snapshotFile": "knowledge/search-snapshot.json", "snapshotSha256": hashlib.sha256(encoded.encode()).hexdigest(),
                    "ownerSha256": hashlib.sha256(b''.join(files['knowledge_owner/' + name].encode() for name in names)).hexdigest(),
                    "sourceCount": index.get("sourceCount", index["documentCount"]), "documentCount": index["documentCount"], "chunkCount": index["chunkCount"]}
        if dense_snapshot is not None:
            dense_encoded = json.dumps(dense_snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            if len(dense_encoded.encode()) > 80 * 1024 * 1024:
                raise KnowledgeIntakeError("向量快照超过 80 MB，请拆分知识范围。")
            files["knowledge/dense-snapshot.json"] = dense_encoded
            files["knowledge_owner/dense.py"] = (owner / "dense.py").read_text()
            for name in ("embeddings.py", "text_utils.py"):
                files["knowledge_owner/" + name] = (owner.parent / name).read_text()
            manifest["embedding"] = dense_snapshot["provider"]
            manifest["dense"] = {"enabled": True, "backend": "sqlite-exact", "snapshotFile": "knowledge/dense-snapshot.json",
                "snapshotSha256": hashlib.sha256(dense_encoded.encode()).hexdigest(),
                "fingerprint": dense_snapshot["fingerprint"], "providerFingerprint": dense_snapshot["fingerprint"],
                "modelRevision": dense_snapshot["modelRevision"], "dimension": dense_snapshot["dimension"],
                "vectorCount": dense_snapshot["vectorCount"], "provider": dense_snapshot["provider"],
                "denseOwnerSha256": hashlib.sha256(b''.join(files['knowledge_owner/' + name].encode()
                    for name in ("dense.py", "embeddings.py", "text_utils.py"))).hexdigest()}
        return {"knowledge": manifest, "files": files}

    def golden_inputs(self, project: Mapping, value: Mapping) -> dict:
        """Connect immutable Knowledge to the existing Golden/Pi workflow.

        Evaluation references are bounded separately; only answer_sources may
        assemble solver context. No imported case or sample is human-approved.
        """
        if set(value) - {"indexId", "datasetId", "targetCount", "profile", "scenario"}:
            raise KnowledgeIntakeError("知识库回答评测的绑定参数无效。")
        index = self._dependency(value.get("indexId"), project["projectId"], "index")
        corpus = self._dependency(index["corpusId"], project["projectId"], "corpus")
        documents = self._documents(corpus)
        unique_documents, source_aliases = content_identities(documents)
        canonical_documents = {row["sourceId"]: row for row in unique_documents}
        count = _integer(value.get("targetCount", 12), "回答评测题数", 2, 100)
        profile = retrieval_profile(value.get("profile", {}))
        if profile["mode"] != "lexical" and index.get("dense", {}).get("provider", {}).get("semantic") is not True:
            raise KnowledgeIntakeError("所选索引没有语义向量，请使用关键词检索或建立新索引。")
        binding = {"projectId": project["projectId"], "indexId": index["jobId"], "corpusHash": index["corpusHash"],
                   "configHash": index["configHash"], "profile": profile, "documentCount": index["documentCount"],
                   "chunkCount": index["chunkCount"]}
        dataset_id = value.get("datasetId")
        selected, source_documents = [], []
        provenance = {"kind": "synthetic_draft_requested", "totalCases": 0, "selectedCases": count}
        if dataset_id:
            dataset = self._dependency(dataset_id, project["projectId"], "dataset")["dataset"]
            if dataset["corpusHash"] != corpus["corpusHash"]:
                raise KnowledgeIntakeError("评测集与知识库版本不一致。")
            cases = sorted(self._cases(dataset), key=lambda row: digest(row["caseId"]))
            buckets = {split: [row for row in cases if row["split"] == split] for split in ("development", "holdout")}
            if not all(buckets.values()):
                raise KnowledgeIntakeError("回答对照需要独立的开发题和保留题，请补充不同来源的问题。")
            selected = [buckets["development"][0], buckets["holdout"][0]]
            selected_ids = {row["caseId"] for row in selected}
            selected.extend(row for row in cases if row["caseId"] not in selected_ids)
            selected = selected[:count]
            if any(not row["answer"].strip() or not row["sourceIds"] for row in selected):
                raise KnowledgeIntakeError("所选原题缺少参考答案或来源 ID。请补充标注，或选择“没有评测集”起草待审核标准。")
            used = {source_aliases[source] for row in selected for source in row["sourceIds"]}
            source_documents = [row for row in unique_documents if row["sourceId"] in used]
            provenance = {"kind": "imported_reference", "datasetId": dataset_id, "datasetHash": dataset["sha256"],
                          "totalCases": len(cases), "selectedCases": len(selected), "selectionPolicy": "stable-case-hash; both splits represented",
                          "humanApproved": False, "diagnosticSamples": "Reference answer plus explicitly constructed incorrect/partial samples; labels remain empty."}
        else:
            # A bounded, deterministic document sample is used for drafting
            # standards only; solver retrieval still searches the entire index.
            source_documents = sorted(unique_documents, key=lambda row: digest(row["sourceId"]))[:min(12, len(unique_documents))]
        if len(source_documents) > 100 or sum(len(row["text"]) for row in source_documents) > 2_000_000:
            raise KnowledgeIntakeError("本次参考材料超过回答评测预算，请减少题目数。知识库索引无需缩小。")
        sources = [{"sourceId": row["externalId"], "title": row["title"], "kind": "document", "text": row["text"],
                    "uri": row["uri"] or f"lab-knowledge://{index['jobId']}/{row['externalId']}"} for row in source_documents]
        imported = []
        for row in selected:
            evidence = [{"sourceId": canonical_documents[source]["externalId"], "quote": canonical_documents[source]["text"][:100_000]}
                        for source in dict.fromkeys(source_aliases[source] for source in row["sourceIds"])]
            imported.append({"caseId": row["caseId"], "question": row["question"], "taskType": "knowledge_qa", "answerable": True,
                             "requiredFacts": [row["answer"]], "evidence": evidence,
                             "rubric": ["Answer the original question accurately and cover the reference answer's material facts. Cite supporting knowledge sources and avoid unsupported claims."],
                             "split": row["split"], "samples": [
                                 {"sampleId": "reference", "answer": row["answer"], "category": "correct"},
                                 {"sampleId": "diagnostic-unsupported", "answer": "This feature always works automatically and has no requirements or limitations.", "category": "incorrect"},
                                 {"sampleId": "diagnostic-partial", "answer": row["answer"][:max(1, len(row["answer"]) // 3)], "category": "boundary"},
                             ]})
        return {"title": project["title"], "scenario": value.get("scenario") or project["description"], "sources": sources,
                "targetCount": len(imported) if imported else count, "knowledge": binding, "datasetProvenance": provenance,
                **({"importedCases": imported} if imported else {})}
