import type { ControlTransport } from '@/platform/transport';

/** Native-only builds deliberately exclude HTTP transport code. TeamGateway is served by an HTTP build. */
export function createTeamTransport(_options: unknown): ControlTransport {
  throw new Error('TeamGateway requires the HTTP control-center build');
}
