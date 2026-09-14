from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from .adapters import ControlTargetAdapter
from .models import ControlAccessContext, ControlClientKind
from .route_policy import ControlPathId, ControlRoutePolicy


CONTROL_API_VERSION = "rag-ime.control.v1"
CONTROL_CAPABILITIES_VERSION = "rag-ime.control-capabilities.v1"
CONTROL_BOOTSTRAP_VERSION = "rag-ime.control-bootstrap.v1"
NATIVE_BRIDGE_HANDLER = "ragImeNativeBridge"


@dataclass(frozen=True)
class NativeCapabilityState:
    file_picker: bool = False
    managed_agent_image_import: bool = False
    reveal_path: bool = False
    keychain_status: bool = False
    tcc_status: bool = False
    approved_external_actions: bool = False


def capability_feature_flags(
    route_manifest: Iterable[Mapping[str, object]],
) -> dict[str, bool]:
    routes = tuple(route_manifest)
    route_ids = {str(item["pathId"]) for item in routes}
    return {
        "agentSessions": ControlPathId.AGENT_SESSIONS_LIST.value in route_ids,
        "agentRooms": ControlPathId.AGENT_ROOMS_LIST.value in route_ids,
        "agentRoles": ControlPathId.AGENT_ROLES_LIST.value in route_ids,
        "agentPersonaCreate": ControlPathId.AGENT_ROLES_CREATE.value in route_ids,
        "piProviderCredentials": ControlPathId.AGENT_PROVIDER_AUTH_PREVIEW.value in route_ids,
        "agentApprovals": ControlPathId.AGENT_APPROVALS_LIST.value in route_ids,
        "agentDelegation": ControlPathId.AGENT_SUBAGENTS_LIST.value in route_ids,
        "managementReads": ControlPathId.OVERVIEW_GET.value in route_ids,
        "managementWorkContract": ControlPathId.PLANNING_MUTATION_PREVIEW.value in route_ids,
        "inputLexiconWorkContract": all(
            path_id.value in route_ids
            for path_id in (
                ControlPathId.INPUT_LEXICON_REVIEW,
                ControlPathId.INPUT_LEXICON_APPLY,
                ControlPathId.INPUT_LEXICON_ROLLBACK,
            )
        ),
        "planningWorkContract": ControlPathId.PLANNING_TASK_SAVE.value in route_ids,
        "knowledgeDatabaseWorkContract": ControlPathId.KNOWLEDGE_DATABASE_APPLY_PREVIEW.value in route_ids,
        "historyWorkContract": ControlPathId.HISTORY_TOMBSTONE_PREVIEW.value in route_ids,
        "configurationSettingsWorkContract": ControlPathId.CONFIGURATION_SETTINGS_PREVIEW.value in route_ids,
        "memoryGraphRead": ControlPathId.MEMORY_GRAPH_GET.value in route_ids,
        "memoryEntityRead": ControlPathId.MEMORY_ENTITY_GET.value in route_ids,
        "memoryEdit": ControlPathId.MEMORY_EDIT.value in route_ids,
        "memoryBookArchiveWorkContract": all(
            path_id.value in route_ids
            for path_id in (
                ControlPathId.MEMORY_BOOK_ARCHIVE_PREVIEW,
                ControlPathId.MEMORY_BOOK_ARCHIVE_APPLY,
                ControlPathId.MEMORY_BOOK_ARCHIVE_ROLLBACK,
            )
        ),
        "subscriptions": any(item.get("subscription") is True for item in routes),
        "sessionSnapshot": ControlPathId.AGENT_SESSION_SNAPSHOT.value in route_ids,
        "roomSnapshot": ControlPathId.AGENT_ROOM_SNAPSHOT.value in route_ids,
        "agentConfigurationWrite": ControlPathId.AGENT_CONFIGURATION_UPDATE.value in route_ids,
        "sessionIntercom": ControlPathId.AGENT_SESSION_INTERCOM_SEND.value in route_ids,
        "boundedArtifacts": ControlPathId.AGENT_ARTIFACT_GET.value in route_ids,
        "agentMediaList": ControlPathId.AGENT_MEDIA_LIST.value in route_ids,
        "agentDeepSearch": ControlPathId.AGENT_DEEP_SEARCH.value in route_ids,
        "agentWakeScheduling": ControlPathId.AGENT_WAKE_SCHEDULES_CREATE.value in route_ids,
        "globalControlEvents": ControlPathId.CONTROL_EVENTS.value in route_ids,
    }


def build_capabilities(
    *,
    policy: ControlRoutePolicy,
    context: ControlAccessContext,
    adapter: ControlTargetAdapter,
    adapter_wired: bool,
    http_mounted: bool = False,
    native: NativeCapabilityState | None = None,
) -> dict[str, object]:
    native_state = native or NativeCapabilityState()
    native_client = context.client_kind is ControlClientKind.NATIVE
    route_manifest = policy.manifest(context=context, include_targets=False)
    subscriptions = [
        str(item["pathId"])
        for item in route_manifest
        if item.get("subscription") is True
    ]
    return {
        "schemaVersion": CONTROL_CAPABILITIES_VERSION,
        "apiVersion": CONTROL_API_VERSION,
        "client": {
            "kind": context.client_kind.value,
            "remote": context.is_remote,
            "deviceAuthenticated": context.remote_authenticated,
            "grantedScopes": sorted(context.granted_scopes) if context.is_remote else [],
        },
        "transport": {
            **adapter.public_descriptor(wired=adapter_wired),
            "httpMounted": bool(http_mounted),
            "nativeBridgeHandler": NATIVE_BRIDGE_HANDLER,
        },
        "requestContract": {
            "fields": ["id", "pathId", "params", "query", "body"],
            "acceptsUrl": False,
            "acceptsHost": False,
            "response": {"success": ["id", "ok", "result"], "failure": ["id", "ok", "error"]},
        },
        "features": capability_feature_flags(route_manifest),
        "native": {
            "filePicker": native_client and native_state.file_picker,
            "managedAgentImageImport": native_client and native_state.managed_agent_image_import,
            "revealPath": native_client and native_state.reveal_path,
            "keychainStatus": native_client and native_state.keychain_status,
            "tccStatus": native_client and native_state.tcc_status,
            "approvedExternalActions": native_client and native_state.approved_external_actions,
            "keychainValues": False,
        },
        "security": {
            "failClosed": True,
            "remoteUsesGatewayOnly": True,
            "devicePairingRequired": not context.is_team,
            "corsWildcard": False,
            "cookieCredentials": context.is_team,
            "csrfRequiredForCookieAuth": True,
            "debugApi": False,
            "arbitraryUrl": False,
            "arbitraryHost": False,
            "arbitraryShell": False,
            "arbitraryFileRead": False,
            "arbitraryFileWrite": False,
            "databaseApply": False,
        },
        "subscriptions": {
            "pathIds": subscriptions,
            "lastEventIdRequired": True,
            "gapRecovery": "snapshot_required",
        },
        "routes": route_manifest,
    }


def build_bootstrap(
    *,
    policy: ControlRoutePolicy,
    context: ControlAccessContext,
    adapter: ControlTargetAdapter,
    adapter_wired: bool,
    http_mounted: bool = False,
    native: NativeCapabilityState | None = None,
) -> dict[str, object]:
    capabilities = build_capabilities(
        policy=policy,
        context=context,
        adapter=adapter,
        adapter_wired=adapter_wired,
        http_mounted=http_mounted,
        native=native,
    )
    return {
        "schemaVersion": CONTROL_BOOTSTRAP_VERSION,
        "apiVersion": CONTROL_API_VERSION,
        "capabilities": capabilities,
        "recovery": {
            "sessionSnapshotPathId": ControlPathId.AGENT_SESSION_SNAPSHOT.value,
            "globalEventPathId": ControlPathId.CONTROL_EVENTS.value,
            "globalSnapshotPathId": ControlPathId.AGENT_CONFIGURATION_GET.value,
            "lastEventIdQueryKey": "lastEventId",
        },
        "integration": {
            "facadeOnly": not http_mounted,
            "httpMounted": bool(http_mounted),
            "adapterWired": bool(adapter_wired),
        },
    }


@runtime_checkable
class AgentCapabilityGateway(Protocol):
    """Plugin/tool boundary shared by Pi, native UI, Web, and remote gateways."""

    def manifests(self) -> Mapping[str, object]: ...

    def execute(self, payload: Mapping[str, object]) -> Mapping[str, object]: ...


def public_capability_catalog(gateway: AgentCapabilityGateway) -> dict[str, object]:
    payload = gateway.manifests()
    items = payload.get("items") if isinstance(payload.get("items"), list) else []
    return {
        "schemaVersion": "rag-ime.control-capability-list.v1",
        "items": [
            dict(item)
            for item in items
            if isinstance(item, Mapping) and item.get("availability") != "hidden"
        ],
    }
