import { HttpControlTransport, type HttpControlTransportOptions } from '@/platform/http-transport';
import type { ControlTransport } from '@/platform/transport';

export function createTeamTransport(options: HttpControlTransportOptions): ControlTransport {
  return new HttpControlTransport(options);
}
