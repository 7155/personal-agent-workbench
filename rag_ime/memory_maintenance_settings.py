from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from .agent_model_defaults import DEFAULT_AGENT_MODEL_PROFILE, PREVIOUS_PRODUCT_MODEL_PROFILES
from .memory_catalog_scheduler import DEFAULT_CATALOG_CONSOLIDATION_CADENCE_DAYS
from .settings_store import ManagementSettingsStore
from .text_utils import compact_whitespace


DEFAULT_MAINTENANCE_MODEL = DEFAULT_AGENT_MODEL_PROFILE
DEFAULT_MAINTENANCE_THINKING_LEVEL = "max"
# Routine owner-scoped curation is allowed to promote only after the existing
# Evidence, Atom-first, and plan validation gates pass. The resulting run keeps
# its immutable source references and rollback receipt; it does not wait for a
# second human approval step.
MEMORY_CURATION_AUTO_APPLY = True
SECONDS_PER_DAY = 24 * 60 * 60


@dataclass(frozen=True)
class MemoryMaintenanceSettings:
    automatic_organization_enabled: bool = True
    automatic_organization_model: str = DEFAULT_MAINTENANCE_MODEL
    automatic_organization_thinking_level: str = DEFAULT_MAINTENANCE_THINKING_LEVEL
    automatic_organization_runs_per_day: int = 2
    include_agent_dialogue: bool = True
    dreaming_enabled: bool = True
    dreaming_model: str = DEFAULT_MAINTENANCE_MODEL
    dreaming_thinking_level: str = DEFAULT_MAINTENANCE_THINKING_LEVEL
    dreaming_runs_per_day: int = 2
    recall_detail_level: str = "compact"
    timeline_recall_enabled: bool = True
    timeline_max_items: int = 2
    # Keep the master switch before newly appended lane settings so positional
    # callers of the existing settings value object retain their field order.
    memory_enabled: bool = True
    catalog_consolidation_enabled: bool = True
    catalog_consolidation_cadence_days: int = DEFAULT_CATALOG_CONSOLIDATION_CADENCE_DAYS

    @property
    def automatic_organization_auto_apply(self) -> bool:
        """Whether validated routine curation promotes its stored run."""

        return MEMORY_CURATION_AUTO_APPLY

    @classmethod
    def load(
        cls,
        db_path: str | Path,
        *,
        preverified_schema: bool = False,
    ) -> "MemoryMaintenanceSettings":
        settings = ManagementSettingsStore(
            db_path,
            preverified_schema=preverified_schema,
        ).get_settings(
            include_sensitive=True
        )
        memory = _mapping(settings.get("memory"))
        automatic = _mapping(memory.get("automaticOrganization"))
        dreaming = _mapping(memory.get("dreaming"))
        recall = _mapping(memory.get("recall"))
        catalog = _mapping(memory.get("catalogConsolidation"))
        memory_enabled = bool(memory.get("enabled", True))
        return cls(
            memory_enabled=memory_enabled,
            automatic_organization_enabled=(
                memory_enabled and bool(automatic.get("enabled", True))
            ),
            automatic_organization_model=_maintenance_model(
                automatic.get("model")
            ),
            automatic_organization_thinking_level=_thinking_level(
                automatic.get("thinkingLevel")
            ),
            automatic_organization_runs_per_day=_runs_per_day(
                automatic.get("runsPerDay")
            ),
            include_agent_dialogue=bool(
                automatic.get("includeAgentDialogue", True)
            ),
            dreaming_enabled=memory_enabled and bool(dreaming.get("enabled", True)),
            dreaming_model=_maintenance_model(dreaming.get("model")),
            dreaming_thinking_level=_thinking_level(dreaming.get("thinkingLevel")),
            dreaming_runs_per_day=_runs_per_day(dreaming.get("runsPerDay")),
            recall_detail_level=_detail_level(recall.get("detailLevel")),
            timeline_recall_enabled=bool(recall.get("timelineEnabled", True)),
            timeline_max_items=_bounded_int(
                recall.get("timelineMaxItems"),
                default=2,
                minimum=1,
                maximum=4,
            ),
            catalog_consolidation_enabled=(
                memory_enabled and bool(catalog.get("enabled", True))
            ),
            catalog_consolidation_cadence_days=_cadence_days(
                catalog.get("cadenceDays")
            ),
        )

    @property
    def automatic_organization_interval_seconds(self) -> int:
        return max(
            60,
            SECONDS_PER_DAY // self.automatic_organization_runs_per_day,
        )

    @property
    def catalog_consolidation_interval_seconds(self) -> int:
        return self.catalog_consolidation_cadence_days * SECONDS_PER_DAY

    @property
    def dreaming_interval_seconds(self) -> int:
        return max(60, SECONDS_PER_DAY // self.dreaming_runs_per_day)

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": "rag-ime.memory-maintenance-settings.v1",
            "enabled": self.memory_enabled,
            "automaticOrganization": {
                "enabled": self.automatic_organization_enabled,
                "model": self.automatic_organization_model,
                "thinkingLevel": self.automatic_organization_thinking_level,
                "runsPerDay": self.automatic_organization_runs_per_day,
                "intervalSeconds": self.automatic_organization_interval_seconds,
                "autoApply": self.automatic_organization_auto_apply,
                "includeAgentDialogue": self.include_agent_dialogue,
            },
            "dreaming": {
                "enabled": self.dreaming_enabled,
                "model": self.dreaming_model,
                "thinkingLevel": self.dreaming_thinking_level,
                "runsPerDay": self.dreaming_runs_per_day,
                "intervalSeconds": self.dreaming_interval_seconds,
            },
            "catalogConsolidation": {
                "enabled": self.catalog_consolidation_enabled,
                "cadenceDays": self.catalog_consolidation_cadence_days,
                "intervalSeconds": self.catalog_consolidation_interval_seconds,
            },
            "recall": {
                "detailLevel": self.recall_detail_level,
                "timelineEnabled": self.timeline_recall_enabled,
                "timelineMaxItems": self.timeline_max_items,
            },
        }


def memory_enabled_from_settings(db_path: str | Path) -> bool:
    """Read the live memory master switch without replaying migrations.

    Runtime callers use this small adapter so a settings change takes effect
    on the next turn/tool call without rebuilding the long-lived Agent.  A
    missing database is treated as the default-on state for lightweight
    standalone gateways; an existing database that cannot be read is treated
    as disabled to avoid leaking memory while its policy is unknowable.
    """

    if not db_path:
        return True
    path = Path(db_path)
    if not path.is_file():
        return True
    try:
        return MemoryMaintenanceSettings.load(
            path,
            preverified_schema=True,
        ).memory_enabled
    except Exception:
        return False


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _maintenance_model(value: object) -> str:
    model = compact_whitespace(str(value or DEFAULT_MAINTENANCE_MODEL))
    if model == "gpt/gpt-5.6-luna" or model in PREVIOUS_PRODUCT_MODEL_PROFILES:
        return DEFAULT_MAINTENANCE_MODEL
    if "/" not in model:
        if model.casefold().replace("_", "-").startswith("deepseek-v4"):
            return f"deepseek/{model}"
        return DEFAULT_MAINTENANCE_MODEL
    provider, _, model_id = model.partition("/")
    if not provider or not model_id or any(character.isspace() for character in model):
        return DEFAULT_MAINTENANCE_MODEL
    return f"{provider}/{model_id}"


def _thinking_level(value: object) -> str:
    normalized = compact_whitespace(str(value or DEFAULT_MAINTENANCE_THINKING_LEVEL)).casefold()
    return (
        normalized
        if normalized in {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
        else DEFAULT_MAINTENANCE_THINKING_LEVEL
    )


def _runs_per_day(value: object) -> int:
    return _bounded_int(value, default=2, minimum=1, maximum=6)


def _cadence_days(value: object) -> int:
    return _bounded_int(
        value,
        default=DEFAULT_CATALOG_CONSOLIDATION_CADENCE_DAYS,
        minimum=1,
        maximum=365,
    )


def _bounded_int(
    value: object,
    *,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        normalized = default
    return max(minimum, min(normalized, maximum))


def _detail_level(value: object) -> str:
    normalized = compact_whitespace(str(value or "")).casefold()
    return (
        normalized
        if normalized in {"compact", "balanced", "detailed"}
        else "compact"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve managed Dream and automatic-memory settings.",
    )
    parser.add_argument("--db-path", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = MemoryMaintenanceSettings.load(args.db_path).as_dict()
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
