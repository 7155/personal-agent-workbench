/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Run scripts/generate_control_center_contracts.mjs instead.
 */

import type { ActiveRagStartV1 } from './generated/active-rag-start.v1';
import type { ActiveRagStatusV1 } from './generated/active-rag-status.v1';
import type { ActivityTimelineContextV1 } from './generated/activity-timeline-context.v1';
import type { AgentApprovalModelDecisionV1 } from './generated/agent-approval-model-decision.v1';
import type { AgentApprovalV1 } from './generated/agent-approval.v1';
import type { AgentArtifactInspectionV1 } from './generated/agent-artifact-inspection.v1';
import type { AgentArtifactRefV1 } from './generated/agent-artifact-ref.v1';
import type { AgentBackgroundJobV1 } from './generated/agent-background-job.v1';
import type { AgentConfigurationV1 } from './generated/agent-configuration.v1';
import type { AgentContextItemV1 } from './generated/agent-context-item.v1';
import type { AgentContextTraceV1 } from './generated/agent-context-trace.v1';
import type { AgentControlBootstrapV1 } from './generated/agent-control-bootstrap.v1';
import type { AgentControlEventV1 } from './generated/agent-control-event.v1';
import type { AgentConversationContextV1 } from './generated/agent-conversation-context.v1';
import type { AgentEventV1 } from './generated/agent-event.v1';
import type { AgentFileDescriptorV1 } from './generated/agent-file-descriptor.v1';
import type { AgentFilePreviewV1 } from './generated/agent-file-preview.v1';
import type { AgentGoalMutationV1 } from './generated/agent-goal-mutation.v1';
import type { AgentGoalSettleRequestV1 } from './generated/agent-goal-settle-request.v1';
import type { AgentGoalSettleResultV1 } from './generated/agent-goal-settle-result.v1';
import type { AgentGoalUsageV1 } from './generated/agent-goal-usage.v1';
import type { AgentLabCostReceiptV1 } from './generated/agent-lab-cost-receipt.v1';
import type { AgentLabCostRequestV1 } from './generated/agent-lab-cost-request.v1';
import type { AgentLabExperimentV1 } from './generated/agent-lab-experiment.v1';
import type { AgentLabPathSearchRequestV1 } from './generated/agent-lab-path-search-request.v1';
import type { AgentLabPathSearchV1 } from './generated/agent-lab-path-search.v1';
import type { AgentLifecycleCancellationAuditV1 } from './generated/agent-lifecycle-cancellation-audit.v1';
import type { AgentMediaV1 } from './generated/agent-media.v1';
import type { AgentMemoryEvidenceV1 } from './generated/agent-memory-evidence.v1';
import type { AgentMemoryMaintenanceStatusV1 } from './generated/agent-memory-maintenance-status.v1';
import type { AgentMemorySourceV1 } from './generated/agent-memory-source.v1';
import type { AgentMessageV1 } from './generated/agent-message.v1';
import type { AgentModelCatalogV1 } from './generated/agent-model-catalog.v1';
import type { AgentModelSelectionV1 } from './generated/agent-model-selection.v1';
import type { AgentParticipantV1 } from './generated/agent-participant.v1';
import type { AgentPersonaV1 } from './generated/agent-persona.v1';
import type { AgentRoleBookToolResultV1 } from './generated/agent-role-book-tool-result.v1';
import type { AgentRoleBookV1 } from './generated/agent-role-book.v1';
import type { AgentRoleRoutingProfileV1 } from './generated/agent-role-routing-profile.v1';
import type { AgentRoomConversationSnapshotV1 } from './generated/agent-room-conversation-snapshot.v1';
import type { AgentRoomEventPageV1 } from './generated/agent-room-event-page.v1';
import type { AgentRoomEventV1 } from './generated/agent-room-event.v1';
import type { AgentRoomIntercomV1 } from './generated/agent-room-intercom.v1';
import type { AgentRoomSnapshotV1 } from './generated/agent-room-snapshot.v1';
import type { AgentRoomWorkItemV1 } from './generated/agent-room-work-item.v1';
import type { AgentRoomV1 } from './generated/agent-room.v1';
import type { AgentRuntimeBindingV1 } from './generated/agent-runtime-binding.v1';
import type { AgentRuntimeV1 } from './generated/agent-runtime.v1';
import type { AgentSessionCodemodeSelectionV1 } from './generated/agent-session-codemode-selection.v1';
import type { AgentSessionForkCandidatesV1 } from './generated/agent-session-fork-candidates.v1';
import type { AgentSessionForkCreateV1 } from './generated/agent-session-fork-create.v1';
import type { AgentSessionTelemetryV1 } from './generated/agent-session-telemetry.v1';
import type { AgentSessionV1 } from './generated/agent-session.v1';
import type { AgentSubagentBatchV1 } from './generated/agent-subagent-batch.v1';
import type { AgentSubagentRunV1 } from './generated/agent-subagent-run.v1';
import type { AgentTemplateV1 } from './generated/agent-template.v1';
import type { AgentThinkingSelectionV1 } from './generated/agent-thinking-selection.v1';
import type { AgentToolCallV1 } from './generated/agent-tool-call.v1';
import type { AgentToolResultV1 } from './generated/agent-tool-result.v1';
import type { AgentWorkflowStateV1 } from './generated/agent-workflow-state.v1';
import type { AssistantCandidateActionV1 } from './generated/assistant-candidate-action.v1';
import type { AssistantOverlayV1 } from './generated/assistant-overlay.v1';
import type { CapabilityCatalogV1 } from './generated/capability-catalog.v1';
import type { CollaborationProfileCommandReceiptV1 } from './generated/collaboration-profile-command-receipt.v1';
import type { CollaborationProfileCommandV1 } from './generated/collaboration-profile-command.v1';
import type { CollaborationProfileCompileReceiptV1 } from './generated/collaboration-profile-compile-receipt.v1';
import type { CollaborationProfileProjectionV1 } from './generated/collaboration-profile-projection.v1';
import type { CollaborationProfileV1 } from './generated/collaboration-profile.v1';
import type { CollaborationRoleV1 } from './generated/collaboration-role.v1';
import type { CompiledAgentRuntimeProfileV1 } from './generated/compiled-agent-runtime-profile.v1';
import type { ControlToolManifestV1 } from './generated/control-tool-manifest.v1';
import type { DailyActivityTimelineV1 } from './generated/daily-activity-timeline.v1';
import type { DailyConversationDigestV1 } from './generated/daily-conversation-digest.v1';
import type { DeliveryGateObservationV1 } from './generated/delivery-gate-observation.v1';
import type { EvalLabRunListV1 } from './generated/eval-lab-run-list.v1';
import type { EvalRunV1 } from './generated/eval-run.v1';
import type { EvalScheduleCreateV1 } from './generated/eval-schedule-create.v1';
import type { EvalScheduleErrorV1 } from './generated/eval-schedule-error.v1';
import type { EvalScheduleListV1 } from './generated/eval-schedule-list.v1';
import type { EvalScheduleRunListV1 } from './generated/eval-schedule-run-list.v1';
import type { EvalScheduleV1 } from './generated/eval-schedule.v1';
import type { EvalSuiteListV1 } from './generated/eval-suite-list.v1';
import type { ForegroundCommitV1 } from './generated/foreground-commit.v1';
import type { ForegroundContextV2 } from './generated/foreground-context.v2';
import type { FrontendCapabilitiesV1 } from './generated/frontend-capabilities.v1';
import type { FrontendSelectionResponseV1 } from './generated/frontend-selection-response.v1';
import type { FrontendSelectionV1 } from './generated/frontend-selection.v1';
import type { FrontendSuggestRequestV1 } from './generated/frontend-suggest-request.v1';
import type { FrontendSuggestResponseV1 } from './generated/frontend-suggest-response.v1';
import type { GuardActivationProjectionV1 } from './generated/guard-activation-projection.v1';
import type { GuardActivePointerProjectionV1 } from './generated/guard-active-pointer-projection.v1';
import type { GuardApprovalProjectionV1 } from './generated/guard-approval-projection.v1';
import type { GuardCandidateProjectionV1 } from './generated/guard-candidate-projection.v1';
import type { GuardEvalRunProjectionV1 } from './generated/guard-eval-run-projection.v1';
import type { GuardMaterializationStatusProjectionV1 } from './generated/guard-materialization-status-projection.v1';
import type { GuardRollbackProjectionV1 } from './generated/guard-rollback-projection.v1';
import type { IncidentOccurrenceProjectionV1 } from './generated/incident-occurrence-projection.v1';
import type { KnowledgeDocumentDetailV1 } from './generated/knowledge-document-detail.v1';
import type { KnowledgeDocumentImportV1 } from './generated/knowledge-document-import.v1';
import type { KnowledgeGraphV1 } from './generated/knowledge-graph.v1';
import type { KnowledgeLibraryV1 } from './generated/knowledge-library.v1';
import type { KnowledgeSearchUseEvalRunV1 } from './generated/knowledge-search-use-eval-run.v1';
import type { LessonCandidateProjectionV1 } from './generated/lesson-candidate-projection.v1';
import type { ManagementWorkErrorV1 } from './generated/management-work-error.v1';
import type { ManagementWorkPreviewV1 } from './generated/management-work-preview.v1';
import type { ManagementWorkReceiptV1 } from './generated/management-work-receipt.v1';
import type { MemoryBootstrapV1 } from './generated/memory-bootstrap.v1';
import type { MemoryCatalogV1 } from './generated/memory-catalog.v1';
import type { MemoryEntityV1 } from './generated/memory-entity.v1';
import type { MemoryGovernancePreviewV1 } from './generated/memory-governance-preview.v1';
import type { MemoryGraphV1 } from './generated/memory-graph.v1';
import type { MemoryReadErrorV1 } from './generated/memory-read-error.v1';
import type { MemoryReferenceV1 } from './generated/memory-reference.v1';
import type { ObservabilityEvalListV1 } from './generated/observability-eval-list.v1';
import type { ObservabilityEvidenceEvalRequestV1 } from './generated/observability-evidence-eval-request.v1';
import type { ObservabilitySandboxRunListV1 } from './generated/observability-sandbox-run-list.v1';
import type { ObservabilityTraceErrorV1 } from './generated/observability-trace-error.v1';
import type { ObservabilityTraceGetV1 } from './generated/observability-trace-get.v1';
import type { ObservationEventV1 } from './generated/observation-event.v1';
import type { ObservationSnapshotV1 } from './generated/observation-snapshot.v1';
import type { OverlayConfigV1 } from './generated/overlay-config.v1';
import type { PawPluginUsageQueryV1 } from './generated/paw.plugin-usage-query.v1';
import type { PawPluginUsageV1 } from './generated/paw.plugin-usage.v1';
import type { PiDurableToolOutcomeV1 } from './generated/pi-durable-tool-outcome.v1';
import type { PiRuntimeManifestV1 } from './generated/pi-runtime-manifest.v1';
import type { PromptCompileReceiptV1 } from './generated/prompt-compile-receipt.v1';
import type { PromptPlanV1 } from './generated/prompt-plan.v1';
import type { ProviderProjectionJournalV1 } from './generated/provider-projection-journal.v1';
import type { ProviderProjectionReceiptV1 } from './generated/provider-projection-receipt.v1';
import type { ReflectionDeadLetterProjectionV1 } from './generated/reflection-dead-letter-projection.v1';
import type { RequirementAnchorV1 } from './generated/requirement-anchor.v1';
import type { RequirementCatalogRevisionV1 } from './generated/requirement-catalog-revision.v1';
import type { RimeRankSelectionV1 } from './generated/rime-rank-selection.v1';
import type { RimeSelectV1 } from './generated/rime-select.v1';
import type { RimeSuggestRequestV1 } from './generated/rime-suggest-request.v1';
import type { RimeSuggestResponseV1 } from './generated/rime-suggest-response.v1';
import type { RoleBookCurationV1 } from './generated/role-book-curation.v1';
import type { RoleBookRevisionDraftV1 } from './generated/role-book-revision-draft.v1';
import type { RoomBindingV2 } from './generated/room-binding.v2';
import type { RoomCommitV2 } from './generated/room-commit.v2';
import type { RoomCommitV3 } from './generated/room-commit.v3';
import type { RoomDispatchEnvelopeV2 } from './generated/room-dispatch-envelope.v2';
import type { RoomEventEnvelopeV2 } from './generated/room-event-envelope.v2';
import type { RoomPermissionPolicyV1 } from './generated/room-permission-policy.v1';
import type { RoomPostV2 } from './generated/room-post.v2';
import type { RoomQualityGateReceiptV1 } from './generated/room-quality-gate-receipt.v1';
import type { RoomRootExecutionV3 } from './generated/room-root-execution.v3';
import type { RoomShadowObservationV1 } from './generated/room-shadow-observation.v1';
import type { RoomTaskV3 } from './generated/room-task.v3';
import type { RunnerVerificationReceiptV2 } from './generated/runner-verification-receipt.v2';
import type { SandboxRunV1 } from './generated/sandbox-run.v1';
import type { SessionMemoryRecallV1 } from './generated/session-memory-recall.v1';
import type { SessionRecallEffectReceiptV1 } from './generated/session-recall-effect-receipt.v1';
import type { SpaceOrganizationCommandV1 } from './generated/space-organization-command.v1';
import type { TraceDiagnosticInspectionV1 } from './generated/trace-diagnostic-inspection.v1';
import type { TraceDiagnosticReportListV1 } from './generated/trace-diagnostic-report-list.v1';
import type { TraceDiagnosticReportV1 } from './generated/trace-diagnostic-report.v1';
import type { TraceDiagnosticResultV1 } from './generated/trace-diagnostic-result.v1';
import type { TraceEnvelopeV1 } from './generated/trace-envelope.v1';
import type { TraceOptimizationV1 } from './generated/trace-optimization.v1';
import type { TraceRepairReceiptV1 } from './generated/trace-repair-receipt.v1';
import type { TraceReplayCaseV1 } from './generated/trace-replay-case.v1';
import type { TraceVerificationReceiptV1 } from './generated/trace-verification-receipt.v1';
import type { TypedVerificationReceiptV1 } from './generated/typed-verification-receipt.v1';
import type { UserMemoryDraftV1 } from './generated/user-memory-draft.v1';
import type { VerticalAgentSelfTestSuiteV1 } from './generated/vertical-agent-self-test-suite.v1';
import type { WorkDocumentCommandV1 } from './generated/work-document-command.v1';
import type { WorkDocumentContextV1 } from './generated/work-document-context.v1';
import type { WorkDocumentDetailV1 } from './generated/work-document-detail.v1';
import type { WorkDocumentListV1 } from './generated/work-document-list.v1';
import type { WorkspaceLspMutationReceiptV1 } from './generated/workspace-lsp-mutation-receipt.v1';
import type { WorkspaceLspResultV1 } from './generated/workspace-lsp-result.v1';
import type { WorkspaceLspStatusV1 } from './generated/workspace-lsp-status.v1';

export type {
  ActiveRagStartV1,
  ActiveRagStatusV1,
  ActivityTimelineContextV1,
  AgentApprovalModelDecisionV1,
  AgentApprovalV1,
  AgentArtifactInspectionV1,
  AgentArtifactRefV1,
  AgentBackgroundJobV1,
  AgentConfigurationV1,
  AgentContextItemV1,
  AgentContextTraceV1,
  AgentControlBootstrapV1,
  AgentControlEventV1,
  AgentConversationContextV1,
  AgentEventV1,
  AgentFileDescriptorV1,
  AgentFilePreviewV1,
  AgentGoalMutationV1,
  AgentGoalSettleRequestV1,
  AgentGoalSettleResultV1,
  AgentGoalUsageV1,
  AgentLabCostReceiptV1,
  AgentLabCostRequestV1,
  AgentLabExperimentV1,
  AgentLabPathSearchRequestV1,
  AgentLabPathSearchV1,
  AgentLifecycleCancellationAuditV1,
  AgentMediaV1,
  AgentMemoryEvidenceV1,
  AgentMemoryMaintenanceStatusV1,
  AgentMemorySourceV1,
  AgentMessageV1,
  AgentModelCatalogV1,
  AgentModelSelectionV1,
  AgentParticipantV1,
  AgentPersonaV1,
  AgentRoleBookToolResultV1,
  AgentRoleBookV1,
  AgentRoleRoutingProfileV1,
  AgentRoomConversationSnapshotV1,
  AgentRoomEventPageV1,
  AgentRoomEventV1,
  AgentRoomIntercomV1,
  AgentRoomSnapshotV1,
  AgentRoomWorkItemV1,
  AgentRoomV1,
  AgentRuntimeBindingV1,
  AgentRuntimeV1,
  AgentSessionCodemodeSelectionV1,
  AgentSessionForkCandidatesV1,
  AgentSessionForkCreateV1,
  AgentSessionTelemetryV1,
  AgentSessionV1,
  AgentSubagentBatchV1,
  AgentSubagentRunV1,
  AgentTemplateV1,
  AgentThinkingSelectionV1,
  AgentToolCallV1,
  AgentToolResultV1,
  AgentWorkflowStateV1,
  AssistantCandidateActionV1,
  AssistantOverlayV1,
  CapabilityCatalogV1,
  CollaborationProfileCommandReceiptV1,
  CollaborationProfileCommandV1,
  CollaborationProfileCompileReceiptV1,
  CollaborationProfileProjectionV1,
  CollaborationProfileV1,
  CollaborationRoleV1,
  CompiledAgentRuntimeProfileV1,
  ControlToolManifestV1,
  DailyActivityTimelineV1,
  DailyConversationDigestV1,
  DeliveryGateObservationV1,
  EvalLabRunListV1,
  EvalRunV1,
  EvalScheduleCreateV1,
  EvalScheduleErrorV1,
  EvalScheduleListV1,
  EvalScheduleRunListV1,
  EvalScheduleV1,
  EvalSuiteListV1,
  ForegroundCommitV1,
  ForegroundContextV2,
  FrontendCapabilitiesV1,
  FrontendSelectionResponseV1,
  FrontendSelectionV1,
  FrontendSuggestRequestV1,
  FrontendSuggestResponseV1,
  GuardActivationProjectionV1,
  GuardActivePointerProjectionV1,
  GuardApprovalProjectionV1,
  GuardCandidateProjectionV1,
  GuardEvalRunProjectionV1,
  GuardMaterializationStatusProjectionV1,
  GuardRollbackProjectionV1,
  IncidentOccurrenceProjectionV1,
  KnowledgeDocumentDetailV1,
  KnowledgeDocumentImportV1,
  KnowledgeGraphV1,
  KnowledgeLibraryV1,
  KnowledgeSearchUseEvalRunV1,
  LessonCandidateProjectionV1,
  ManagementWorkErrorV1,
  ManagementWorkPreviewV1,
  ManagementWorkReceiptV1,
  MemoryBootstrapV1,
  MemoryCatalogV1,
  MemoryEntityV1,
  MemoryGovernancePreviewV1,
  MemoryGraphV1,
  MemoryReadErrorV1,
  MemoryReferenceV1,
  ObservabilityEvalListV1,
  ObservabilityEvidenceEvalRequestV1,
  ObservabilitySandboxRunListV1,
  ObservabilityTraceErrorV1,
  ObservabilityTraceGetV1,
  ObservationEventV1,
  ObservationSnapshotV1,
  OverlayConfigV1,
  PawPluginUsageQueryV1,
  PawPluginUsageV1,
  PiDurableToolOutcomeV1,
  PiRuntimeManifestV1,
  PromptCompileReceiptV1,
  PromptPlanV1,
  ProviderProjectionJournalV1,
  ProviderProjectionReceiptV1,
  ReflectionDeadLetterProjectionV1,
  RequirementAnchorV1,
  RequirementCatalogRevisionV1,
  RimeRankSelectionV1,
  RimeSelectV1,
  RimeSuggestRequestV1,
  RimeSuggestResponseV1,
  RoleBookCurationV1,
  RoleBookRevisionDraftV1,
  RoomBindingV2,
  RoomCommitV2,
  RoomCommitV3,
  RoomDispatchEnvelopeV2,
  RoomEventEnvelopeV2,
  RoomPermissionPolicyV1,
  RoomPostV2,
  RoomQualityGateReceiptV1,
  RoomRootExecutionV3,
  RoomShadowObservationV1,
  RoomTaskV3,
  RunnerVerificationReceiptV2,
  SandboxRunV1,
  SessionMemoryRecallV1,
  SessionRecallEffectReceiptV1,
  SpaceOrganizationCommandV1,
  TraceDiagnosticInspectionV1,
  TraceDiagnosticReportListV1,
  TraceDiagnosticReportV1,
  TraceDiagnosticResultV1,
  TraceEnvelopeV1,
  TraceOptimizationV1,
  TraceRepairReceiptV1,
  TraceReplayCaseV1,
  TraceVerificationReceiptV1,
  TypedVerificationReceiptV1,
  UserMemoryDraftV1,
  VerticalAgentSelfTestSuiteV1,
  WorkDocumentCommandV1,
  WorkDocumentContextV1,
  WorkDocumentDetailV1,
  WorkDocumentListV1,
  WorkspaceLspMutationReceiptV1,
  WorkspaceLspResultV1,
  WorkspaceLspStatusV1,
};

export interface ContractTypeMap {
  'active-rag-start.v1': ActiveRagStartV1;
  'active-rag-status.v1': ActiveRagStatusV1;
  'activity-timeline-context.v1': ActivityTimelineContextV1;
  'agent-approval-model-decision.v1': AgentApprovalModelDecisionV1;
  'agent-approval.v1': AgentApprovalV1;
  'agent-artifact-inspection.v1': AgentArtifactInspectionV1;
  'agent-artifact-ref.v1': AgentArtifactRefV1;
  'agent-background-job.v1': AgentBackgroundJobV1;
  'agent-configuration.v1': AgentConfigurationV1;
  'agent-context-item.v1': AgentContextItemV1;
  'agent-context-trace.v1': AgentContextTraceV1;
  'agent-control-bootstrap.v1': AgentControlBootstrapV1;
  'agent-control-event.v1': AgentControlEventV1;
  'agent-conversation-context.v1': AgentConversationContextV1;
  'agent-event.v1': AgentEventV1;
  'agent-file-descriptor.v1': AgentFileDescriptorV1;
  'agent-file-preview.v1': AgentFilePreviewV1;
  'agent-goal-mutation.v1': AgentGoalMutationV1;
  'agent-goal-settle-request.v1': AgentGoalSettleRequestV1;
  'agent-goal-settle-result.v1': AgentGoalSettleResultV1;
  'agent-goal-usage.v1': AgentGoalUsageV1;
  'agent-lab-cost-receipt.v1': AgentLabCostReceiptV1;
  'agent-lab-cost-request.v1': AgentLabCostRequestV1;
  'agent-lab-experiment.v1': AgentLabExperimentV1;
  'agent-lab-path-search-request.v1': AgentLabPathSearchRequestV1;
  'agent-lab-path-search.v1': AgentLabPathSearchV1;
  'agent-lifecycle-cancellation-audit.v1': AgentLifecycleCancellationAuditV1;
  'agent-media.v1': AgentMediaV1;
  'agent-memory-evidence.v1': AgentMemoryEvidenceV1;
  'agent-memory-maintenance-status.v1': AgentMemoryMaintenanceStatusV1;
  'agent-memory-source.v1': AgentMemorySourceV1;
  'agent-message.v1': AgentMessageV1;
  'agent-model-catalog.v1': AgentModelCatalogV1;
  'agent-model-selection.v1': AgentModelSelectionV1;
  'agent-participant.v1': AgentParticipantV1;
  'agent-persona.v1': AgentPersonaV1;
  'agent-role-book-tool-result.v1': AgentRoleBookToolResultV1;
  'agent-role-book.v1': AgentRoleBookV1;
  'agent-role-routing-profile.v1': AgentRoleRoutingProfileV1;
  'agent-room-conversation-snapshot.v1': AgentRoomConversationSnapshotV1;
  'agent-room-event-page.v1': AgentRoomEventPageV1;
  'agent-room-event.v1': AgentRoomEventV1;
  'agent-room-intercom.v1': AgentRoomIntercomV1;
  'agent-room-snapshot.v1': AgentRoomSnapshotV1;
  'agent-room-work-item.v1': AgentRoomWorkItemV1;
  'agent-room.v1': AgentRoomV1;
  'agent-runtime-binding.v1': AgentRuntimeBindingV1;
  'agent-runtime.v1': AgentRuntimeV1;
  'agent-session-codemode-selection.v1': AgentSessionCodemodeSelectionV1;
  'agent-session-fork-candidates.v1': AgentSessionForkCandidatesV1;
  'agent-session-fork-create.v1': AgentSessionForkCreateV1;
  'agent-session-telemetry.v1': AgentSessionTelemetryV1;
  'agent-session.v1': AgentSessionV1;
  'agent-subagent-batch.v1': AgentSubagentBatchV1;
  'agent-subagent-run.v1': AgentSubagentRunV1;
  'agent-template.v1': AgentTemplateV1;
  'agent-thinking-selection.v1': AgentThinkingSelectionV1;
  'agent-tool-call.v1': AgentToolCallV1;
  'agent-tool-result.v1': AgentToolResultV1;
  'agent-workflow-state.v1': AgentWorkflowStateV1;
  'assistant-candidate-action.v1': AssistantCandidateActionV1;
  'assistant-overlay.v1': AssistantOverlayV1;
  'capability-catalog.v1': CapabilityCatalogV1;
  'collaboration-profile-command-receipt.v1': CollaborationProfileCommandReceiptV1;
  'collaboration-profile-command.v1': CollaborationProfileCommandV1;
  'collaboration-profile-compile-receipt.v1': CollaborationProfileCompileReceiptV1;
  'collaboration-profile-projection.v1': CollaborationProfileProjectionV1;
  'collaboration-profile.v1': CollaborationProfileV1;
  'collaboration-role.v1': CollaborationRoleV1;
  'compiled-agent-runtime-profile.v1': CompiledAgentRuntimeProfileV1;
  'control-tool-manifest.v1': ControlToolManifestV1;
  'daily-activity-timeline.v1': DailyActivityTimelineV1;
  'daily-conversation-digest.v1': DailyConversationDigestV1;
  'delivery-gate-observation.v1': DeliveryGateObservationV1;
  'eval-lab-run-list.v1': EvalLabRunListV1;
  'eval-run.v1': EvalRunV1;
  'eval-schedule-create.v1': EvalScheduleCreateV1;
  'eval-schedule-error.v1': EvalScheduleErrorV1;
  'eval-schedule-list.v1': EvalScheduleListV1;
  'eval-schedule-run-list.v1': EvalScheduleRunListV1;
  'eval-schedule.v1': EvalScheduleV1;
  'eval-suite-list.v1': EvalSuiteListV1;
  'foreground-commit.v1': ForegroundCommitV1;
  'foreground-context.v2': ForegroundContextV2;
  'frontend-capabilities.v1': FrontendCapabilitiesV1;
  'frontend-selection-response.v1': FrontendSelectionResponseV1;
  'frontend-selection.v1': FrontendSelectionV1;
  'frontend-suggest-request.v1': FrontendSuggestRequestV1;
  'frontend-suggest-response.v1': FrontendSuggestResponseV1;
  'guard-activation-projection.v1': GuardActivationProjectionV1;
  'guard-active-pointer-projection.v1': GuardActivePointerProjectionV1;
  'guard-approval-projection.v1': GuardApprovalProjectionV1;
  'guard-candidate-projection.v1': GuardCandidateProjectionV1;
  'guard-eval-run-projection.v1': GuardEvalRunProjectionV1;
  'guard-materialization-status-projection.v1': GuardMaterializationStatusProjectionV1;
  'guard-rollback-projection.v1': GuardRollbackProjectionV1;
  'incident-occurrence-projection.v1': IncidentOccurrenceProjectionV1;
  'knowledge-document-detail.v1': KnowledgeDocumentDetailV1;
  'knowledge-document-import.v1': KnowledgeDocumentImportV1;
  'knowledge-graph.v1': KnowledgeGraphV1;
  'knowledge-library.v1': KnowledgeLibraryV1;
  'knowledge-search-use-eval-run.v1': KnowledgeSearchUseEvalRunV1;
  'lesson-candidate-projection.v1': LessonCandidateProjectionV1;
  'management-work-error.v1': ManagementWorkErrorV1;
  'management-work-preview.v1': ManagementWorkPreviewV1;
  'management-work-receipt.v1': ManagementWorkReceiptV1;
  'memory-bootstrap.v1': MemoryBootstrapV1;
  'memory-catalog.v1': MemoryCatalogV1;
  'memory-entity.v1': MemoryEntityV1;
  'memory-governance-preview.v1': MemoryGovernancePreviewV1;
  'memory-graph.v1': MemoryGraphV1;
  'memory-read-error.v1': MemoryReadErrorV1;
  'memory-reference.v1': MemoryReferenceV1;
  'observability-eval-list.v1': ObservabilityEvalListV1;
  'observability-evidence-eval-request.v1': ObservabilityEvidenceEvalRequestV1;
  'observability-sandbox-run-list.v1': ObservabilitySandboxRunListV1;
  'observability-trace-error.v1': ObservabilityTraceErrorV1;
  'observability-trace-get.v1': ObservabilityTraceGetV1;
  'observation-event.v1': ObservationEventV1;
  'observation-snapshot.v1': ObservationSnapshotV1;
  'overlay-config.v1': OverlayConfigV1;
  'paw.plugin-usage-query.v1': PawPluginUsageQueryV1;
  'paw.plugin-usage.v1': PawPluginUsageV1;
  'pi-durable-tool-outcome.v1': PiDurableToolOutcomeV1;
  'pi-runtime-manifest.v1': PiRuntimeManifestV1;
  'prompt-compile-receipt.v1': PromptCompileReceiptV1;
  'prompt-plan.v1': PromptPlanV1;
  'provider-projection-journal.v1': ProviderProjectionJournalV1;
  'provider-projection-receipt.v1': ProviderProjectionReceiptV1;
  'reflection-dead-letter-projection.v1': ReflectionDeadLetterProjectionV1;
  'requirement-anchor.v1': RequirementAnchorV1;
  'requirement-catalog-revision.v1': RequirementCatalogRevisionV1;
  'rime-rank-selection.v1': RimeRankSelectionV1;
  'rime-select.v1': RimeSelectV1;
  'rime-suggest-request.v1': RimeSuggestRequestV1;
  'rime-suggest-response.v1': RimeSuggestResponseV1;
  'role-book-curation.v1': RoleBookCurationV1;
  'role-book-revision-draft.v1': RoleBookRevisionDraftV1;
  'room-binding.v2': RoomBindingV2;
  'room-commit.v2': RoomCommitV2;
  'room-commit.v3': RoomCommitV3;
  'room-dispatch-envelope.v2': RoomDispatchEnvelopeV2;
  'room-event-envelope.v2': RoomEventEnvelopeV2;
  'room-permission-policy.v1': RoomPermissionPolicyV1;
  'room-post.v2': RoomPostV2;
  'room-quality-gate-receipt.v1': RoomQualityGateReceiptV1;
  'room-root-execution.v3': RoomRootExecutionV3;
  'room-shadow-observation.v1': RoomShadowObservationV1;
  'room-task.v3': RoomTaskV3;
  'runner-verification-receipt.v2': RunnerVerificationReceiptV2;
  'sandbox-run.v1': SandboxRunV1;
  'session-memory-recall.v1': SessionMemoryRecallV1;
  'session-recall-effect-receipt.v1': SessionRecallEffectReceiptV1;
  'space-organization-command.v1': SpaceOrganizationCommandV1;
  'trace-diagnostic-inspection.v1': TraceDiagnosticInspectionV1;
  'trace-diagnostic-report-list.v1': TraceDiagnosticReportListV1;
  'trace-diagnostic-report.v1': TraceDiagnosticReportV1;
  'trace-diagnostic-result.v1': TraceDiagnosticResultV1;
  'trace-envelope.v1': TraceEnvelopeV1;
  'trace-optimization.v1': TraceOptimizationV1;
  'trace-repair-receipt.v1': TraceRepairReceiptV1;
  'trace-replay-case.v1': TraceReplayCaseV1;
  'trace-verification-receipt.v1': TraceVerificationReceiptV1;
  'typed-verification-receipt.v1': TypedVerificationReceiptV1;
  'user-memory-draft.v1': UserMemoryDraftV1;
  'vertical-agent-self-test-suite.v1': VerticalAgentSelfTestSuiteV1;
  'work-document-command.v1': WorkDocumentCommandV1;
  'work-document-context.v1': WorkDocumentContextV1;
  'work-document-detail.v1': WorkDocumentDetailV1;
  'work-document-list.v1': WorkDocumentListV1;
  'workspace-lsp-mutation-receipt.v1': WorkspaceLspMutationReceiptV1;
  'workspace-lsp-result.v1': WorkspaceLspResultV1;
  'workspace-lsp-status.v1': WorkspaceLspStatusV1;
}

export type GeneratedContractName = keyof ContractTypeMap;
