from __future__ import annotations

from .agent_model_defaults import DEFAULT_AGENT_MODEL_ID

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import unquote


NEAR_BUDGET_VERDICT_SCHEMA_VERSION = (
    "rag-ime.memory-near-budget-verdict.v1"
)
NEAR_BUDGET_EVALUATION_SCHEMA_VERSION = (
    "rag-ime.memory-near-budget-evaluation.v1"
)
MINIMUM_NEAR_BUDGET_PAYLOAD_CHARS = 100_000
MAXIMUM_NEAR_BUDGET_PAYLOAD_CHARS = 900_000
DEFAULT_NEAR_BUDGET_PAYLOAD_CHARS = 650_000


@dataclass(frozen=True)
class NearBudgetCase:
    payload: str
    payload_sha256: str
    payload_chars: int
    payload_utf8_bytes: int
    markers: Mapping[str, str]
    nonces: Mapping[str, str]


class SqliteConnectionAudit:
    """Fail closed if an evaluation process leaves its private SQLite root."""

    def __init__(
        self,
        *,
        private_root: str | Path,
        production_db: str | Path,
    ) -> None:
        self.private_root = Path(private_root).expanduser().resolve(strict=True)
        self.production_db = Path(production_db).expanduser().resolve(strict=False)
        self._original_connect = sqlite3.connect
        self._observed: list[Path] = []
        self._blocked_production_attempts = 0
        self._blocked_outside_private_attempts = 0

    def __enter__(self) -> SqliteConnectionAudit:
        original = self._original_connect

        def guarded_connect(database: object, *args: object, **kwargs: object):
            path = _sqlite_database_path(database)
            if path is not None:
                if path == self.production_db:
                    self._blocked_production_attempts += 1
                    raise RuntimeError(
                        "near-budget evaluation attempted to open production SQLite"
                    )
                if not _path_within(path, self.private_root):
                    self._blocked_outside_private_attempts += 1
                    raise RuntimeError(
                        "near-budget evaluation attempted SQLite outside private root"
                    )
                self._observed.append(path)
            return original(database, *args, **kwargs)

        sqlite3.connect = guarded_connect  # type: ignore[assignment]
        return self

    def __exit__(self, *_exc: object) -> None:
        sqlite3.connect = self._original_connect  # type: ignore[assignment]

    def summary(self) -> dict[str, object]:
        observed_hashes = sorted(
            {
                _sha256_text(str(path))
                for path in self._observed
            }
        )
        guard_passed = (
            bool(self._observed)
            and self._blocked_production_attempts == 0
            and self._blocked_outside_private_attempts == 0
        )
        return {
            "guardPassed": guard_passed,
            "productionDatabaseOpened": False,
            "allFileConnectionsPrivate": (
                bool(self._observed)
                and all(_path_within(path, self.private_root) for path in self._observed)
            ),
            "observedConnectionCount": len(self._observed),
            "observedDatabasePathSha256": observed_hashes,
            "blockedProductionAttemptCount": self._blocked_production_attempts,
            "blockedOutsidePrivateAttemptCount": self._blocked_outside_private_attempts,
        }


def build_near_budget_case(
    payload_chars: int = DEFAULT_NEAR_BUDGET_PAYLOAD_CHARS,
    *,
    seed: str = "gateway-memory-transport-v1",
) -> NearBudgetCase:
    target = int(payload_chars)
    if not MINIMUM_NEAR_BUDGET_PAYLOAD_CHARS <= target <= MAXIMUM_NEAR_BUDGET_PAYLOAD_CHARS:
        raise ValueError(
            "near-budget payload chars must be between "
            f"{MINIMUM_NEAR_BUDGET_PAYLOAD_CHARS} and "
            f"{MAXIMUM_NEAR_BUDGET_PAYLOAD_CHARS}"
        )
    normalized_seed = " ".join(str(seed or "").split())
    if not normalized_seed or len(normalized_seed) > 160:
        raise ValueError("near-budget seed is required and must be at most 160 chars")

    nonces = {
        position: hashlib.sha256(
            f"{normalized_seed}:{position}".encode("utf-8")
        ).hexdigest()[:24]
        for position in ("begin", "middle", "end")
    }
    markers = {
        position: (
            f"MEMORY_PACKET_{position.upper()} nonce={nonces[position]}\n"
        )
        for position in ("begin", "middle", "end")
    }
    fixed_chars = sum(len(value) for value in markers.values())
    filler_chars = target - fixed_chars
    if filler_chars <= 0:
        raise ValueError("near-budget payload is too small for integrity markers")
    first_chars = filler_chars // 2
    second_chars = filler_chars - first_chars
    payload = "".join(
        (
            markers["begin"],
            _deterministic_hex(first_chars, seed=f"{normalized_seed}:first"),
            markers["middle"],
            _deterministic_hex(second_chars, seed=f"{normalized_seed}:second"),
            markers["end"],
        )
    )
    if len(payload) != target:
        raise AssertionError("near-budget payload length drifted")
    for marker in markers.values():
        if payload.count(marker) != 1:
            raise AssertionError("near-budget marker is not unique")
    positions = [payload.index(markers[name]) for name in ("begin", "middle", "end")]
    if positions != sorted(positions):
        raise AssertionError("near-budget markers are out of order")
    return NearBudgetCase(
        payload=payload,
        payload_sha256=_sha256_text(payload),
        payload_chars=len(payload),
        payload_utf8_bytes=len(payload.encode("utf-8")),
        markers=markers,
        nonces=nonces,
    )


def memory_near_budget_messages(case: NearBudgetCase) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "Validate transport integrity for the length-delimited application "
                "data. Locate MEMORY_PACKET_BEGIN, MEMORY_PACKET_MIDDLE, and "
                "MEMORY_PACKET_END. Copy only the nonce following each label. "
                "Do not summarize, quote, or reproduce filler. Return exactly one "
                "JSON object with schemaVersion, beginNonce, middleNonce, endNonce, "
                "and order. schemaVersion must equal "
                f"\"{NEAR_BUDGET_VERDICT_SCHEMA_VERSION}\". order must be "
                "[\"begin\",\"middle\",\"end\"]."
            ),
        },
        {"role": "user", "content": case.payload},
    ]


def parse_near_budget_verdict(
    output_text: object,
    case: NearBudgetCase,
) -> dict[str, object]:
    try:
        payload = json.loads(str(output_text or ""))
    except json.JSONDecodeError as exc:
        raise ValueError("near-budget verdict is not one JSON object") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("near-budget verdict must be a JSON object")
    if str(payload.get("schemaVersion") or "") != NEAR_BUDGET_VERDICT_SCHEMA_VERSION:
        raise ValueError("near-budget verdict schemaVersion is invalid")
    matches: dict[str, bool] = {}
    for position in ("begin", "middle", "end"):
        key = f"{position}Nonce"
        matches[position] = str(payload.get(key) or "") == case.nonces[position]
        if not matches[position]:
            raise ValueError(f"near-budget {position} marker nonce did not match")
    order = payload.get("order")
    order_matched = isinstance(order, list) and [str(item) for item in order] == [
        "begin",
        "middle",
        "end",
    ]
    if not order_matched:
        raise ValueError("near-budget marker order did not match")
    return {
        "passed": True,
        "beginMatched": matches["begin"],
        "middleMatched": matches["middle"],
        "endMatched": matches["end"],
        "orderMatched": order_matched,
    }


def redacted_near_budget_summary(
    case: NearBudgetCase,
    *,
    response: Mapping[str, object],
    verdict: Mapping[str, object],
    runtime_manifest_sha256: str,
    source_hashes: Mapping[str, str],
    production_access: Mapping[str, object],
    production_file_identity_changed: bool,
    expected_model_id: str = DEFAULT_AGENT_MODEL_ID,
) -> dict[str, object]:
    receipt_value = response.get("receipt")
    receipt = dict(receipt_value) if isinstance(receipt_value, Mapping) else {}
    marker_hashes = {
        position: _sha256_text(marker)
        for position, marker in case.markers.items()
    }
    transport_gate = {
        "providerMatched": str(receipt.get("provider") or "") == "openai-codex",
        "modelMatched": str(receipt.get("modelId") or "") == expected_model_id,
        "thinkingMatched": str(receipt.get("thinkingLevel") or "") == "max",
        "contextWindowSufficient": int(receipt.get("contextWindow") or 0) >= 272_000,
        "inputLengthCovered": int(receipt.get("inputChars") or 0) >= case.payload_chars,
        "exceededLegacy64KSlice": int(receipt.get("inputChars") or 0) > 64_000,
        "gatewayInternalSession": (
            str(receipt.get("transport") or "") == "gateway_internal_session"
        ),
    }
    passed = (
        bool(verdict.get("passed"))
        and all(transport_gate.values())
        and bool(production_access.get("guardPassed"))
        and not bool(production_access.get("productionDatabaseOpened"))
        and bool(production_access.get("allFileConnectionsPrivate"))
    )
    return {
        "schemaVersion": NEAR_BUDGET_EVALUATION_SCHEMA_VERSION,
        "status": "pass" if passed else "iterate",
        "passed": passed,
        "evaluationKind": "private_gateway_near_budget_transport",
        "payloadChars": case.payload_chars,
        "payloadUtf8Bytes": case.payload_utf8_bytes,
        "payloadSha256": case.payload_sha256,
        "markerSha256": marker_hashes,
        "markerVerdict": {
            "beginMatched": bool(verdict.get("beginMatched")),
            "middleMatched": bool(verdict.get("middleMatched")),
            "endMatched": bool(verdict.get("endMatched")),
            "orderMatched": bool(verdict.get("orderMatched")),
        },
        "transportGate": transport_gate,
        "modelReceipt": {
            "provider": str(receipt.get("provider") or ""),
            "modelId": str(receipt.get("modelId") or ""),
            "thinkingLevel": str(receipt.get("thinkingLevel") or ""),
            "contextWindow": max(0, int(receipt.get("contextWindow") or 0)),
            "maxTokens": max(0, int(receipt.get("maxTokens") or 0)),
            "inputChars": max(0, int(receipt.get("inputChars") or 0)),
            "elapsedMs": max(0, int(receipt.get("elapsedMs") or 0)),
            "transport": str(receipt.get("transport") or ""),
            "inputSha256": str(receipt.get("inputSha256") or ""),
            "usage": _numeric_tree(receipt.get("usage")),
        },
        "runtimeManifestSha256": str(runtime_manifest_sha256 or ""),
        "sourceSha256": {
            str(name): str(digest)
            for name, digest in sorted(source_hashes.items())
        },
        "sqliteConnectionAudit": dict(production_access),
        "productionDatabaseOpened": bool(
            production_access.get("productionDatabaseOpened")
        ),
        "productionMutationPerformed": False,
        "productionFileIdentityChangedDuringRun": bool(
            production_file_identity_changed
        ),
        "rawPayloadRetainedInPublicReport": False,
        "rawModelOutputRetainedInPublicReport": False,
    }


def render_near_budget_public_report(summary: Mapping[str, object]) -> str:
    receipt_value = summary.get("modelReceipt")
    receipt = dict(receipt_value) if isinstance(receipt_value, Mapping) else {}
    verdict_value = summary.get("markerVerdict")
    verdict = dict(verdict_value) if isinstance(verdict_value, Mapping) else {}
    source_value = summary.get("sourceSha256")
    sources = dict(source_value) if isinstance(source_value, Mapping) else {}
    audit_value = summary.get("sqliteConnectionAudit")
    audit = dict(audit_value) if isinstance(audit_value, Mapping) else {}
    lines = [
        "# Memory near-budget Gateway/Luna test — 2026-08-02",
        "",
        "## Result",
        "",
        f"- Status: `{summary.get('status')}`.",
        f"- Synthetic payload: `{summary.get('payloadChars')}` characters, "
        f"`{summary.get('payloadUtf8Bytes')}` UTF-8 bytes.",
        f"- Payload SHA-256: `{summary.get('payloadSha256')}`.",
        f"- Beginning, middle, end and order matched: "
        f"`{all(bool(verdict.get(key)) for key in ('beginMatched', 'middleMatched', 'endMatched', 'orderMatched'))}`.",
        "",
        "## Real execution receipt",
        "",
        f"- Transport: `{receipt.get('transport')}`.",
        f"- Model: `{receipt.get('provider')}/{receipt.get('modelId')}`; "
        f"thinking `{receipt.get('thinkingLevel')}`.",
        f"- Context window: `{receipt.get('contextWindow')}` tokens.",
        f"- Gateway Session input: `{receipt.get('inputChars')}` characters.",
        f"- Elapsed: `{receipt.get('elapsedMs')}` ms.",
        f"- Usage receipt: `{json.dumps(receipt.get('usage') or {}, sort_keys=True)}`.",
        "",
        "## Reproducibility and privacy",
        "",
        f"- Managed Runtime manifest SHA-256: `{summary.get('runtimeManifestSha256')}`.",
    ]
    for name, digest in sorted(sources.items()):
        lines.append(f"- `{name}` SHA-256: `{digest}`.")
    lines.extend(
        [
            f"- Process-local SQLite connection guard passed: "
            f"`{audit.get('guardPassed')}`; observed private connections "
            f"`{audit.get('observedConnectionCount')}`; production opens "
            f"`{audit.get('blockedProductionAttemptCount')}`.",
            f"- Production file identity changed while the live product continued "
            f"running: `{summary.get('productionFileIdentityChangedDuringRun')}`. "
            "This is recorded as concurrent external activity, not used as a "
            "causality gate.",
            "- The public report stores hashes, lengths, booleans, numeric usage, "
            "and model/runtime receipts only. It excludes the synthetic filler, "
            "raw model output, private database paths, credentials, and Session files.",
            "",
            "## Interview framing",
            "",
            "The risk was not whether Python could hold a large string. The real "
            "failure mode was an older 64K transport slice between Memory curation "
            "and the Provider. This test sends one content-addressed packet through "
            "the actual Gateway-owned internal Session, asks the model to recover "
            "unknown nonces at the beginning, middle, and end, and checks the persisted "
            "receipt. It proves this isolated current-source path did not truncate the "
            "packet at 64K; installed same-generation foreground acceptance remains "
            "a separate gate.",
            "",
        ]
    )
    return "\n".join(lines)


def _deterministic_hex(length: int, *, seed: str) -> str:
    chunks: list[str] = []
    remaining = max(0, int(length))
    counter = 0
    while remaining:
        block = hashlib.sha256(f"{seed}:{counter}".encode("utf-8")).hexdigest()
        chunks.append(block[:remaining])
        remaining -= min(remaining, len(block))
        counter += 1
    return "".join(chunks)


def _numeric_tree(value: object) -> object:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): child
            for key, item in value.items()
            if (child := _numeric_tree(item)) is not None
        }
    if isinstance(value, list):
        return [child for item in value if (child := _numeric_tree(item)) is not None]
    return None


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sqlite_database_path(database: object) -> Path | None:
    if isinstance(database, int):
        return None
    value = str(database or "").strip()
    if not value or value == ":memory:":
        return None
    if value.startswith("file:"):
        value = unquote(value[5:].split("?", 1)[0])
    if not value or value == ":memory:":
        return None
    return Path(value).expanduser().resolve(strict=False)


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
