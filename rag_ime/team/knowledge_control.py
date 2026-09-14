"""Role and resource fences around the existing Knowledge control facade."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ..knowledge_control import KnowledgeControlFacade
from ..knowledge_library import KnowledgeLibraryError, KnowledgeNotFoundError
from .errors import TeamError
from .knowledge import validate_team_document_bytes


_READ_OPERATIONS = frozenset({
    "status",
    "list_bases",
    "get_base",
    "list_documents",
    "search",
    "find",
    "open",
})
_IMPORT_OPERATIONS = frozenset({"import_text", "import_document", "import"})


class TeamKnowledgeControl(KnowledgeControlFacade):
    """Use the existing worker and facade with a live Team authorization check.

    The worker is intentionally still an ordinary loopback Knowledge worker.
    This class is the Team-owned boundary: every call rereads the current
    account/membership and every document/job mutation verifies that the
    selected child resource belongs to the selected base before invoking the
    worker mutation.
    """

    def __init__(
        self,
        *,
        identity: Any,
        space_id: str,
        actor_provider: Callable[[], Mapping[str, object]],
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.identity = identity
        self.space_id = str(space_id)
        self.actor_provider = actor_provider

    def _actor(self, action: str = "read") -> str:
        try:
            current = self.actor_provider()
        except Exception as exc:
            raise TeamError(401, "authentication_required", "Sign in to use team Knowledge") from exc
        actor = str(current.get("userId") or "") if isinstance(current, Mapping) else ""
        if not actor:
            raise TeamError(401, "authentication_required", "Sign in to use team Knowledge")
        self.identity.require_space(actor, self.space_id, action=action)
        return actor

    def _read(self) -> str:
        return self._actor("read")

    def _write(self) -> str:
        return self._actor("write")

    def _manage(self) -> str:
        return self._actor("manage")

    @staticmethod
    def _parser_allowed(value: object) -> str:
        normalized = str(value or "auto").strip().lower()
        if normalized in {"mineru", "mineru_local_http"}:
            raise TeamError(
                403,
                "parser_not_allowed",
                "Team Knowledge uses the local bounded builtin parser; external OCR is unavailable",
            )
        return normalized

    def list_bases(self) -> dict[str, object]:
        self._read()
        return super().list_bases()

    def embedding_profile(self) -> dict[str, object]:
        self._read()
        return super().embedding_profile()

    def embedding_probe(self, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        raise TeamError(403, "feature_unavailable", "Team embedding providers are disabled")

    def embedding_impact(self, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        raise TeamError(403, "feature_unavailable", "Team embedding providers are disabled")

    def create_base(self, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        if "parserProvider" in payload:
            self._parser_allowed(payload.get("parserProvider"))
        return super().create_base(payload)

    def get_base(self, kb_id: str) -> dict[str, object]:
        self._read()
        return super().get_base(kb_id)

    def update_base(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        if "parserProvider" in payload:
            self._parser_allowed(payload.get("parserProvider"))
        return super().update_base(kb_id, payload)

    def delete_preview(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        return super().delete_preview(kb_id, payload)

    def delete_apply(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        return super().delete_apply(kb_id, payload)

    def list_documents(self, kb_id: str) -> dict[str, object]:
        self._read()
        return super().list_documents(kb_id)

    def import_document(
        self,
        kb_id: str,
        *,
        data: bytes,
        file_name: str,
        mime_type: str,
        parser_provider: str = "auto",
    ) -> dict[str, object]:
        self._write()
        self._parser_allowed(parser_provider)
        normalized_file_name = _team_file_name(file_name)
        # This checks decompression and PDF stream budgets before the intake
        # receipt is stored or the worker gets a chance to copy the source.
        normalized_data = bytes(data)
        validate_team_document_bytes(normalized_data, normalized_file_name)
        return super().import_document(
            kb_id,
            data=normalized_data,
            file_name=normalized_file_name,
            mime_type=mime_type,
            parser_provider=parser_provider,
        )

    def _require_document_in_base(self, kb_id: str, file_id: str) -> None:
        # Call the parent implementation directly to avoid a second actor
        # lookup while performing the pre-mutation ownership check.
        KnowledgeControlFacade.document_detail(self, kb_id, file_id, {})

    def retry_document(
        self,
        kb_id: str,
        file_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        self._manage()
        if payload.get("parserProvider") is not None:
            self._parser_allowed(payload.get("parserProvider"))
        self._require_document_in_base(kb_id, file_id)
        return super().retry_document(kb_id, file_id, payload)

    def delete_document(self, kb_id: str, file_id: str) -> dict[str, object]:
        self._manage()
        self._require_document_in_base(kb_id, file_id)
        return super().delete_document(kb_id, file_id)

    def document_detail(
        self,
        kb_id: str,
        file_id: str,
        query: Mapping[str, object],
    ) -> dict[str, object]:
        self._read()
        return super().document_detail(kb_id, file_id, query)

    def document_source(self, kb_id: str, file_id: str):  # type: ignore[no-untyped-def]
        self._read()
        return super().document_source(kb_id, file_id)

    def document_asset(self, kb_id: str, file_id: str, asset_id: str):  # type: ignore[no-untyped-def]
        self._read()
        return super().document_asset(kb_id, file_id, asset_id)

    def graph(self, kb_id: str, query: Mapping[str, object]) -> dict[str, object]:
        self._read()
        return super().graph(kb_id, query)

    def rebuild_graph(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        extractor_mode = str(payload.get("extractorMode") or "deterministic").strip().lower()
        model_id = str(payload.get("modelId") or "").strip()
        if extractor_mode != "deterministic" or model_id:
            raise TeamError(
                403,
                "knowledge_model_not_allowed",
                "Team Knowledge graph rebuild uses deterministic local extraction only",
            )
        document_ids = payload.get("documentIds")
        if document_ids is not None:
            if not isinstance(document_ids, list):
                raise KnowledgeLibraryError("documentIds must be an array", code="invalid_argument")
            if len(document_ids) > 100:
                raise KnowledgeLibraryError("documentIds exceeds the Team limit", code="invalid_argument")
            for document_id in document_ids:
                self._require_document_in_base(kb_id, str(document_id))
        return super().rebuild_graph(kb_id, payload)

    def reindex_preview(self, kb_id: str) -> dict[str, object]:
        self._manage()
        return super().reindex_preview(kb_id)

    def rebuild(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._manage()
        return super().rebuild(kb_id, payload)

    def jobs(self, kb_id: str) -> dict[str, object]:
        self._read()
        return super().jobs(kb_id)

    def cancel_job(self, kb_id: str, job_id: str) -> dict[str, object]:
        self._manage()
        normalized_job_id = str(job_id or "")
        jobs = KnowledgeControlFacade.jobs(self, kb_id).get("items", [])
        if not any(
            isinstance(item, Mapping)
            and normalized_job_id in {str(item.get("id") or ""), str(item.get("jobId") or "")}
            for item in jobs
        ):
            raise KnowledgeNotFoundError("job is outside the selected knowledge base")
        return super().cancel_job(kb_id, normalized_job_id)

    def preview_chunking(
        self,
        kb_id: str,
        file_id: str,
        payload: Mapping[str, object],
    ) -> dict[str, object]:
        self._manage()
        self._require_document_in_base(kb_id, file_id)
        return super().preview_chunking(kb_id, file_id, payload)

    def search(self, kb_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._read()
        return super().search(kb_id, payload)

    def find(self, kb_id: str, file_id: str, payload: Mapping[str, object]) -> dict[str, object]:
        self._read()
        if payload.get("regex") is True or payload.get("useRegex") is True:
            raise TeamError(
                403,
                "regex_not_allowed",
                "Team Knowledge document search accepts literal patterns only",
            )
        return super().find(kb_id, file_id, payload)

    def open(self, kb_id: str, file_id: str, query: Mapping[str, object]) -> dict[str, object]:
        self._read()
        return super().open(kb_id, file_id, query)

    def health(self) -> dict[str, object]:
        self._read()
        return super().health()

    def parsers(self) -> dict[str, object]:
        self._read()
        return super().parsers()


__all__ = ["TeamKnowledgeControl"]


def _team_file_name(value: object) -> str:
    if not isinstance(value, str):
        raise KnowledgeLibraryError("fileName must be a string", code="invalid_argument")
    name = value.strip()
    if not name or name in {".", ".."} or len(name) > 512:
        raise KnowledgeLibraryError("fileName is invalid", code="invalid_argument")
    if any(character in {"/", "\\", "\x00"} or ord(character) < 0x20 for character in name):
        raise KnowledgeLibraryError("fileName must be a single safe file name", code="invalid_argument")
    return name
