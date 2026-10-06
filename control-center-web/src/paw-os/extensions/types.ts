import type { ComponentType } from 'react';

import type { PawOsWindowTarget } from '@/features/paw-os/model/desktop';
import type { ChatSurfaceRequirement } from './chat-surface-contract';

export type PawExtensionAppId = `extension:${string}`;

export type PawExtensionAppPresentation =
  | 'workspace'
  | 'conversation'
  | 'library'
  | 'studio'
  | 'utility';

export type PawExtensionAppIconSymbol =
  | 'analytics'
  | 'assistant'
  | 'document'
  | 'commerce';

export type PawExtensionAppSandboxContract = {
  default: 'required' | 'optional' | 'disabled';
  connectorPackageId: 'vertical-agent-sandbox';
  policyId: 'vertical-readonly-v1';
};

export type PawExtensionAppManifest = {
  schemaVersion: 'pawos.extension-app.v1' | 'pawos.lab-app.v1';
  id: PawExtensionAppId;
  version: string;
  bindingSha256: string;
  packageId: string;
  label: string;
  shortLabel: string;
  tagline: string;
  route: `/extensions/${string}`;
  presentation: PawExtensionAppPresentation;
  accent: 'cyan' | 'blue' | 'violet' | 'amber' | 'green' | 'rose' | 'slate';
  icon: {
    symbol: PawExtensionAppIconSymbol;
    background: `#${string}`;
  };
  skillRef: string;
  skillSha256: string;
  verticalSuiteId: string;
  verticalSuiteRevision: string;
  sandbox?: PawExtensionAppSandboxContract;
  hosting?: { kind: 'lab-html'; appId: PawExtensionAppId; projectId: string; version: number };
};

export type PawExtensionAppInstallationEvidence = {
  id: PawExtensionAppId;
  packageId: string;
  version: string;
  bindingSha256: string;
  bindingCapability: string;
  skillRef: string;
  skillSha256: string;
  verticalSuiteId: string;
  verticalSuiteRevision: string;
  sandbox?: PawExtensionAppSandboxContract;
};

export type PawExtensionAppProps = {
  entityId?: string;
  initialRoute?: string;
  manifest: PawExtensionAppManifest;
  target?: PawOsWindowTarget;
};

export type PawExtensionAppModule = {
  default: ComponentType<PawExtensionAppProps>;
  /** Optional for existing non-chat Apps. Checked before React mounts a chat
   * App; this does not change package activation, tool permissions or hashes. */
  chatSurface?: ChatSurfaceRequirement;
};
