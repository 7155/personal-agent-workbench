import { describe, expect, it } from 'vitest';
import Ajv2020 from 'ajv/dist/2020';

import approvalFixture from '../../../tests/fixtures/agent/agent-approval.json';
import eventFixture from '../../../tests/fixtures/agent/agent-event.json';
import mediaFixture from '../../../tests/fixtures/agent/agent-media.json';
import maintenanceFixture from '../../../tests/fixtures/agent/agent-memory-maintenance-status.json';
import messageFixture from '../../../tests/fixtures/agent/agent-message.json';
import sessionFixture from '../../../tests/fixtures/agent/agent-session.json';

import { contractSchemas } from './schema-index';
import {
  ContractValidationError,
  parseAgentEvent,
  parseAgentMessage,
  parseContract,
  validateContract,
} from './validators';

describe('generated JSON contracts', () => {
  it('preserves reference validation and ordered public issues across all contracts', () => {
    // Keep Ajv's default code generation as an independent reference for the
    // smaller standalone output. schemaPath is internal; the UI exposes these
    // four fields, including their order in the bounded boundary error.
    const reference = new Ajv2020({
      allErrors: true,
      allowUnionTypes: true,
      strict: false,
    });
    for (const schema of Object.values(contractSchemas)) reference.addSchema(schema);
    const fixtures = [
      approvalFixture, eventFixture, mediaFixture, maintenanceFixture,
      messageFixture, sessionFixture,
    ];
    const samples: unknown[] = [null, false, 0, '', [], {}, ...fixtures];
    for (const fixture of fixtures) {
      for (const key of Object.keys(fixture)) {
        const missing: Record<string, unknown> = { ...fixture };
        delete missing[key];
        samples.push(missing, { ...fixture, [key]: null });
      }
    }
    for (const [name, schema] of Object.entries(contractSchemas)) {
      const validate = reference.getSchema(schema.$id)!;
      for (const sample of samples) {
        const expected = validate(sample);
        const actual = validateContract(name as keyof typeof contractSchemas, sample);
        expect(actual.ok, `${name}: ${JSON.stringify(sample)}`).toBe(expected);
        if (!actual.ok) {
          expect(actual.issues, name).toEqual((validate.errors ?? []).map((issue) => ({
            instancePath: issue.instancePath,
            keyword: issue.keyword,
            message: issue.message ?? '',
            params: issue.params,
          })));
        }
      }
    }
  });

  it('validates the same fixtures as the Python contract suite', () => {
    expect(parseContract('agent-approval.v1', approvalFixture).approvalId).toBe(
      'approval-fixture-1',
    );
    expect(parseContract('agent-event.v1', eventFixture).sequence).toBe(7);
    expect(parseContract('agent-media.v1', mediaFixture).mediaId).toBe('media-1234');
    expect(
      parseContract('agent-memory-maintenance-status.v1', maintenanceFixture).due,
    ).toBe(true);
    expect(parseContract('agent-message.v1', messageFixture).blocks).toHaveLength(2);
    expect(parseContract('agent-session.v1', sessionFixture).id).toBe('agent-session-1');
  });

  it('accepts the configured Memory model in the maintenance status', () => {
    const status = {
      ...maintenanceFixture,
      modelCuration: {
        ...maintenanceFixture.modelCuration,
        requiredModel: 'openai-codex/gpt-6-luna',
      },
    };
    expect(parseContract('agent-memory-maintenance-status.v1', status).modelCuration.requiredModel)
      .toBe('openai-codex/gpt-6-luna');
  });

  it('builds a stable schema index for every source contract', () => {
    expect(Object.keys(contractSchemas)).toHaveLength(170);
    expect(contractSchemas['trace-optimization.v1'].$id).toBe(
      'rag-ime.contract.trace-optimization.v1',
    );
    expect(contractSchemas['agent-lab-cost-receipt.v1'].$id).toBe(
      'rag-ime.contract.agent-lab-cost-receipt.v1',
    );
    expect(contractSchemas['agent-lab-cost-request.v1'].$id).toBe(
      'rag-ime.contract.agent-lab-cost-request.v1',
    );
    expect(contractSchemas['agent-lab-experiment.v1'].$id).toBe(
      'rag-ime.contract.agent-lab-experiment.v1',
    );
    expect(contractSchemas['agent-lab-path-search-request.v1'].$id).toBe(
      'rag-ime.contract.agent-lab-path-search-request.v1',
    );
    expect(contractSchemas['agent-lab-path-search.v1'].$id).toBe(
      'rag-ime.contract.agent-lab-path-search.v1',
    );
    expect(contractSchemas['eval-lab-run-list.v1'].$id).toBe(
      'rag-ime.contract.eval-lab-run-list.v1',
    );
    expect(contractSchemas['agent-event.v1'].$id).toBe('rag-ime.contract.agent-event.v1');
    expect(contractSchemas['agent-background-job.v1'].$id).toBe(
      'rag-ime.contract.agent-background-job.v1',
    );
    expect(contractSchemas['agent-room-snapshot.v1'].$id).toBe(
      'rag-ime.contract.agent-room-snapshot.v1',
    );
    expect(contractSchemas['collaboration-role.v1'].$id).toBe(
      'rag-ime.contract.collaboration-role.v1',
    );
    expect(contractSchemas['collaboration-profile.v1'].$id).toBe(
      'rag-ime.contract.collaboration-profile.v1',
    );
    expect(contractSchemas['compiled-agent-runtime-profile.v1'].$id).toBe(
      'rag-ime.contract.compiled-agent-runtime-profile.v1',
    );
    expect(contractSchemas['memory-reference.v1'].$id).toBe(
      'rag-ime.contract.memory-reference.v1',
    );
    expect(contractSchemas['observation-event.v1'].$id).toBe(
      'rag-ime.contract.observation-event.v1',
    );
    expect(contractSchemas['observation-snapshot.v1'].$id).toBe(
      'rag-ime.contract.observation-snapshot.v1',
    );
    expect(contractSchemas['knowledge-graph.v1'].$id).toBe('rag-ime.contract.knowledge-graph.v1');
    expect(contractSchemas['agent-artifact-inspection.v1'].$id).toBe(
      'rag-ime.contract.agent-artifact-inspection.v1',
    );
    expect(contractSchemas['agent-room-intercom.v1'].$id).toBe(
      'rag-ime.contract.agent-room-intercom.v1',
    );
    expect(contractSchemas['agent-session-telemetry.v1'].$id).toBe(
      'rag-ime.contract.agent-session-telemetry.v1',
    );
    expect(contractSchemas['trace-repair-receipt.v1'].$id).toBe(
      'rag-ime.contract.trace-repair-receipt.v1',
    );
    expect(contractSchemas['trace-replay-case.v1'].$id).toBe(
      'rag-ime.contract.trace-replay-case.v1',
    );
    expect(contractSchemas['trace-verification-receipt.v1'].$id).toBe(
      'rag-ime.contract.trace-verification-receipt.v1',
    );
    expect(contractSchemas['activity-timeline-context.v1'].$id).toBe(
      'rag-ime.contract.activity-timeline-context.v1',
    );
    expect(contractSchemas['agent-conversation-context.v1'].$id).toBe(
      'rag-ime.contract.agent-conversation-context.v1',
    );
    expect(contractSchemas['role-book-curation.v1'].$id).toBe(
      'rag-ime.contract.role-book-curation.v1',
    );
    expect(contractSchemas['session-memory-recall.v1'].$id).toBe(
      'rag-ime.contract.session-memory-recall.v1',
    );
    expect(contractSchemas['trace-envelope.v1'].$id).toBe(
      'rag-ime.contract.trace-envelope.v1',
    );
    expect(contractSchemas['observability-trace-get.v1'].$id).toBe(
      'rag-ime.contract.observability-trace-get.v1',
    );
    expect(contractSchemas['eval-run.v1'].$id).toBe(
      'rag-ime.contract.eval-run.v1',
    );
    expect(contractSchemas['eval-schedule.v1'].$id).toBe(
      'rag-ime.contract.eval-schedule.v1',
    );
    expect(contractSchemas['eval-schedule-create.v1'].$id).toBe(
      'rag-ime.contract.eval-schedule-create.v1',
    );
    expect(contractSchemas['eval-schedule-error.v1'].$id).toBe(
      'rag-ime.contract.eval-schedule-error.v1',
    );
    expect(contractSchemas['eval-schedule-list.v1'].$id).toBe(
      'rag-ime.contract.eval-schedule-list.v1',
    );
    expect(contractSchemas['eval-schedule-run-list.v1'].$id).toBe(
      'rag-ime.contract.eval-schedule-run-list.v1',
    );
  });

  it('returns structured Ajv issues and throws a bounded boundary error', () => {
    const invalid = { ...eventFixture } as Record<string, unknown>;
    delete invalid.sequence;
    const result = validateContract('agent-event.v1', invalid);
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.issues.some((issue) => issue.keyword === 'required')).toBe(true);
    expect(() => parseContract('agent-event.v1', invalid)).toThrow(ContractValidationError);
  });

  it('preserves a forward event as unknown instead of crashing the stream', () => {
    const event = parseAgentEvent({
      ...eventFixture,
      eventType: 'future_agent_chart',
      payload: { chartId: 'chart-1' },
    });
    expect(event.eventType).toBe('unknown');
    expect(event.rawEventType).toBe('future_agent_chart');
    expect(event.payload).toEqual({ chartId: 'chart-1' });
  });

  it('preserves a forward block as an unknown diagnostic block', () => {
    const message = parseAgentMessage({
      ...messageFixture,
      blocks: [
        ...messageFixture.blocks,
        {
          id: 'future-block',
          type: 'interactive_chart',
          status: 'completed',
          presentationKind: 'chart',
          data: { series: [1, 2, 3] },
        },
      ],
    });
    expect(message.blocks.at(-1)).toMatchObject({
      id: 'future-block',
      type: 'unknown',
      rawType: 'interactive_chart',
      data: { series: [1, 2, 3] },
    });
  });

  it('normalizes canonical typed block metadata without exposing sidecar JSON', () => {
    const message = parseAgentMessage({
      ...messageFixture,
      blocks: [{
        schemaVersion: 'rag-ime.agent-block.v1',
        id: 'check:1',
        type: 'checklist',
        status: 'completed',
        presentationKind: 'checklist.v1',
        data: { title: '发布', items: [{ text: '测试', checked: true }] },
        summary: '清单：发布，1/1 完成',
        source: { kind: 'pi_session_message', ref: 'message:1' },
        visibility: 'private_session',
        digest: 'a'.repeat(64),
        ref: 'block:check:1:aaaaaaaaaaaaaaaa',
        generation: 0,
        raw_json: '<script>never project me</script>',
      }],
    });

    expect(message.blocks[0]).toMatchObject({
      type: 'checklist',
      presentationKind: 'checklist.v1',
      summary: '清单：发布，1/1 完成',
      source: { kind: 'pi_session_message', ref: 'message:1' },
      visibility: 'private_session',
      ref: 'block:check:1:aaaaaaaaaaaaaaaa',
    });
    expect(message.blocks[0]).not.toHaveProperty('raw_json');
  });
});
