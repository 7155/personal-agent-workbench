/* eslint-disable */
/**
 * This file is generated. Do not edit it by hand.
 * Source: rag_ime/contracts/json/space-organization-command.v1.json
 */

export interface SpaceOrganizationCommandV1 {
  commandId: string;
  spaceKey: string;
  expectedRevision: number;
  operation: 'category' | 'placement' | 'pinned' | 'group' | 'proposal';
  value: string | boolean;
}
