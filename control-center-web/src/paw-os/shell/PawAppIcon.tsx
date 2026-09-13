import { type ReactNode, type SVGProps, useId } from 'react';
import type { PawOsAppId } from '@/features/paw-os/model/app-registry';
import type { PawAppId } from '../runtime/app-registry';
import { isPawExtensionAppId, pawExtensionApp } from '../extensions/registry';
import type { PawExtensionAppIconSymbol } from '../extensions/types';
import './paw-app-icon.css';

/** Room is a visible collaboration identity inside Agent, not a top-level App. */
export type PawIdentityIconId = PawAppId | 'room';
type PawBuiltinIdentityIconId = PawOsAppId | 'room';

export type PawAppIconProps = Omit<SVGProps<SVGSVGElement>, 'children'> & {
  appId: PawIdentityIconId;
  size?: number | string;
  title?: string;
};

/**
 * Approved identity wall source:
 * user-provided pawos-brand-icons-v1/icon-wall.html
 *
 * Keep this mapping explicit. The production App ids stay canonical while the
 * data attribute preserves the exact approved source symbol for regression
 * checks and future asset handoffs.
 */
const APPROVED_SYMBOL_IDS: Record<PawBuiltinIdentityIconId, string> = {
  schedules: 'app-schedules',
  agent: 'app-agent',
  'agent-capsule': 'app-agent-capsule',
  room: 'app-room',
  browser: 'app-browser',
  terminal: 'app-terminal',
  files: 'app-files',
  'project-workbench': 'app-workbench',
  memory: 'app-memory',
  knowledge: 'app-knowledge',
  'input-studio': 'app-input',
  'app-center': 'app-appcenter',
  'system-monitor': 'app-monitor',
  'trace-agent': 'app-trace-agent',
  'eval-lab': 'app-eval-lab',
  'system-settings': 'app-settings',
};

const APP_COLORS: Record<PawBuiltinIdentityIconId, string> = {
  schedules: '#3276CB',
  agent: '#0A84FF',
  'agent-capsule': '#7A5AF8',
  room: '#7A5AF8',
  browser: '#14B8C8',
  terminal: '#1D1D1F',
  files: '#F5A623',
  'project-workbench': '#FF6B4A',
  memory: '#E85D9E',
  knowledge: '#1FA54A',
  'input-studio': '#5E5CE6',
  'app-center': '#0E9F8A',
  'system-monitor': '#3E4C59',
  'trace-agent': '#087E8B',
  'eval-lab': '#C97718',
  'system-settings': '#8E8E93',
};

const PAW_ICON_PAPER = 'var(--paw-icon-paper)';

/** The deterministic SVG drawings for the approved App identities. */
const APPROVED_ART: Record<PawBuiltinIdentityIconId, ReactNode> = {
  schedules: <><rect x="11" y="13" width="26" height="25" rx="5" fill={PAW_ICON_PAPER} /><path d="M17 10v7m14-7v7M11 22h26" fill="none" stroke="#3276CB" strokeWidth="3" strokeLinecap="round" /><path d="M24 26v5l4 2" fill="none" stroke="#3276CB" strokeWidth="2.8" strokeLinecap="round" /></>,
  agent: <>
    <path d="M10 15 a10 10 0 0 1 10-10 h8 a10 10 0 0 1 10 10 v7 a10 10 0 0 1-10 10 h-9 l-6.5 6 v-6.2 A10 10 0 0 1 10 25 z" fill={PAW_ICON_PAPER} transform="translate(0,4)" />
    <circle cx="24" cy="22" fill="#0A84FF" r="5.2" />
  </>,
  'agent-capsule': <>
    <path d="M11 17 a7 7 0 0 1 7-7 h12 a7 7 0 0 1 7 7 v10 a7 7 0 0 1-7 7 h-7 l-7 5.5 v-5.5 h-1 a7 7 0 0 1-7-7 z" fill={PAW_ICON_PAPER} />
    <circle cx="19" cy="22" fill="#7A5AF8" r="2.5" />
    <circle cx="29" cy="22" fill="#7A5AF8" r="2.5" />
    <path d="M18 28 q6 4 12 0" fill="none" stroke="#7A5AF8" strokeLinecap="round" strokeWidth="2.4" />
  </>,
  room: <>
    <circle cx="19" cy="19" fill={PAW_ICON_PAPER} opacity=".96" r="8.5" />
    <circle cx="29" cy="19" fill={PAW_ICON_PAPER} opacity=".78" r="8.5" />
    <circle cx="24" cy="28.5" fill={PAW_ICON_PAPER} opacity=".62" r="8.5" />
  </>,
  browser: <>
    <circle cx="24" cy="24" fill="none" r="13.5" stroke={PAW_ICON_PAPER} strokeWidth="4" />
    <path d="M30.5 14.5 L27 27 17.5 33.5 21 21 z" fill={PAW_ICON_PAPER} />
  </>,
  terminal: <>
    <path d="M13 15.5 l9 8.5 -9 8.5" fill="none" stroke="#30D158" strokeLinecap="round" strokeLinejoin="round" strokeWidth="4.2" />
    <rect fill="#E9F7EE" height="3.8" rx="1.9" width="10.5" x="26" y="31" />
  </>,
  files: <>
    <path d="M9 16.5 a2.5 2.5 0 0 1 2.5-2.5 h8.6 l3.6 4.5 h13.8 a2.5 2.5 0 0 1 2.5 2.5 V33 a4 4 0 0 1-4 4 H13 a4 4 0 0 1-4-4 z" fill={PAW_ICON_PAPER} />
    <rect fill="#F5A623" height="3" opacity=".35" width="31" x="9" y="23.5" />
  </>,
  'project-workbench': <>
    <rect fill={PAW_ICON_PAPER} height="4.6" opacity=".96" rx="2.3" width="14" x="11" y="12.5" />
    <rect fill={PAW_ICON_PAPER} height="4.6" opacity=".8" rx="2.3" width="20" x="11" y="21.7" />
    <rect fill={PAW_ICON_PAPER} height="4.6" opacity=".64" rx="2.3" width="24" x="11" y="30.9" />
    <circle cx="38.5" cy="33.2" fill="#FFD9CF" r="3" />
  </>,
  memory: <>
    <circle cx="24" cy="24" fill="none" r="13.5" stroke={PAW_ICON_PAPER} strokeWidth="3.6" />
    <circle cx="24" cy="24" fill="none" opacity=".75" r="7.5" stroke={PAW_ICON_PAPER} strokeWidth="3.6" />
    <circle cx="24" cy="24" fill={PAW_ICON_PAPER} r="2.8" />
  </>,
  knowledge: <>
    <path d="M24 13.5 C21.5 11.2 17 10.6 12 11.2 a2 2 0 0 0-2 2 V32.6 a2 2 0 0 0 2 2.2 c5-.6 9.5-.1 12 1.9 2.5-2 7-2.5 12-1.9 a2 2 0 0 0 2-2.2 V13.2 a2 2 0 0 0-2-2 c-5-.6-9.5 0-12 2.3 z" fill={PAW_ICON_PAPER} />
    <path d="M24 13.5 V36" stroke="#1FA54A" strokeWidth="2.2" />
  </>,
  'input-studio': <>
    <rect fill={PAW_ICON_PAPER} height="12" opacity=".7" rx="1.9" width="3.8" x="10" y="18" />
    <rect fill={PAW_ICON_PAPER} height="26" rx="1.9" width="3.8" x="16.4" y="11" />
    <rect fill={PAW_ICON_PAPER} height="18" opacity=".85" rx="1.9" width="3.8" x="22.8" y="15" />
    <rect fill={PAW_ICON_PAPER} height="32" rx="1.9" width="3.8" x="29.2" y="8" />
    <rect fill={PAW_ICON_PAPER} height="8" opacity=".7" rx="1.9" width="3.8" x="35.6" y="20" />
  </>,
  'app-center': <>
    <path d="M24 9 37.5 16.8 v14.4 L24 39 10.5 31.2 V16.8 z" fill="none" stroke={PAW_ICON_PAPER} strokeLinejoin="round" strokeWidth="3.4" />
    <path d="M24 24.5 V39 M24 24.5 11.2 17.2 M24 24.5 36.8 17.2" fill="none" stroke={PAW_ICON_PAPER} strokeLinejoin="round" strokeWidth="3.4" />
  </>,
  'system-monitor': <path d="M9 27.5 h7.5 l3-9.5 5 16.5 3.6-11 H39" fill="none" stroke="#30D158" strokeLinecap="round" strokeLinejoin="round" strokeWidth="3.6" />,
  'trace-agent': <><path d="M12 33 22 24 29 28 37 14" fill="none" stroke={PAW_ICON_PAPER} strokeLinecap="round" strokeLinejoin="round" strokeWidth="3.2" /><circle cx="12" cy="33" fill={PAW_ICON_PAPER} r="3.5" /><circle cx="22" cy="24" fill={PAW_ICON_PAPER} r="3.5" /><path d="M29 14 h8 v8" fill="none" stroke={PAW_ICON_PAPER} strokeLinecap="round" strokeLinejoin="round" strokeWidth="3.2" /></>,
  'eval-lab': <><path d="M18 10 h12 M21 10 v12 l-8 13 a3 3 0 0 0 2.6 4.5 h16.8 A3 3 0 0 0 35 35 l-8-13 V10" fill="none" stroke={PAW_ICON_PAPER} strokeLinecap="round" strokeLinejoin="round" strokeWidth="3.4" /><path d="M16 31 h16" stroke="#FFE0A8" strokeLinecap="round" strokeWidth="3" /></>,
  'system-settings': <>
    <circle cx="24" cy="24" fill="none" r="6.6" stroke={PAW_ICON_PAPER} strokeWidth="3.6" />
    <path d="M24 8.5 v5.4 M24 34.1 v5.4 M8.5 24 h5.4 M34.1 24 h5.4 M13.2 13.2 l3.8 3.8 M31 31 l3.8 3.8 M34.8 13.2 31 17 M17 31 l-3.8 3.8" stroke={PAW_ICON_PAPER} strokeLinecap="round" strokeWidth="3.6" />
  </>,
};

export function PawAppIcon({ appId, className = '', size = 24, title, ...props }: PawAppIconProps) {
  const compact = typeof size === 'number' && size <= 18;
  const localId = useId().replaceAll(':', '');
  const extension = appId !== 'room' && isPawExtensionAppId(appId) ? pawExtensionApp(appId) : null;
  const builtinId = extension ? null : appId as PawBuiltinIdentityIconId;
  const symbolId = extension ? `app-extension-${extension.icon.symbol}` : APPROVED_SYMBOL_IDS[builtinId!];
  const color = extension?.icon.background ?? APP_COLORS[builtinId!];
  const art = extension ? extensionArt(extension.icon.symbol) : APPROVED_ART[builtinId!];
  const sheenId = `paw-icon-sheen-${symbolId}-${localId}`;

  return (
    <svg
      {...props}
      aria-hidden={title ? undefined : 'true'}
      aria-label={title}
      className={`paw-app-icon ${className}`.trim()}
      data-paw-app-icon={appId}
      data-paw-approved-symbol={symbolId}
      data-paw-icon-color={color}
      data-paw-icon-scale={compact ? 'small' : undefined}
      focusable="false"
      height={size}
      preserveAspectRatio="xMidYMid meet"
      role={title ? 'img' : undefined}
      viewBox="0 0 48 48"
      width={size}
    >
      {title ? <title>{title}</title> : null}
      <defs>
        <linearGradient id={sheenId} x1="0" x2="0" y1="0" y2="1">
          <stop offset="0" stopColor={PAW_ICON_PAPER} stopOpacity=".24" />
          <stop offset=".55" stopColor={PAW_ICON_PAPER} stopOpacity="0" />
        </linearGradient>
      </defs>
      <rect data-paw-icon-tile fill={color} height="45" rx="11.5" width="45" x="1.5" y="1.5" />
      <rect className="paw-app-icon__sheen" data-paw-icon-sheen fill={`url(#${sheenId})`} height="45" rx="11.5" width="45" x="1.5" y="1.5" />
      <g data-paw-icon-art>{art}</g>
    </svg>
  );
}

function extensionArt(symbol: PawExtensionAppIconSymbol): ReactNode {
  if (symbol === 'assistant') return <>
    <path d="M11 18 a7 7 0 0 1 7-7 h12 a7 7 0 0 1 7 7 v7 a7 7 0 0 1-7 7 h-8 l-7 6 v-6.7 a7 7 0 0 1-4-6.3z" fill={PAW_ICON_PAPER} />
    <circle cx="21" cy="21.5" fill="currentColor" opacity=".22" r="2" />
    <circle cx="29" cy="21.5" fill="currentColor" opacity=".22" r="2" />
  </>;
  if (symbol === 'document') return <>
    <path d="M14 9h14l7 7v23H14z" fill={PAW_ICON_PAPER} />
    <path d="M28 9v8h7M19 24h11M19 30h11" fill="none" stroke="currentColor" opacity=".24" strokeWidth="2.5" />
  </>;
  if (symbol === 'commerce') return <>
    <path d="M11 17h26l-2 19H13z" fill={PAW_ICON_PAPER} />
    <path d="M18 18a6 6 0 0 1 12 0" fill="none" stroke={PAW_ICON_PAPER} strokeWidth="3.2" />
  </>;
  return <>
    <rect fill={PAW_ICON_PAPER} height="10" rx="2" width="5" x="12" y="27" />
    <rect fill={PAW_ICON_PAPER} height="18" opacity=".82" rx="2" width="5" x="21.5" y="19" />
    <rect fill={PAW_ICON_PAPER} height="26" opacity=".66" rx="2" width="5" x="31" y="11" />
    <path d="M11 12l8 4 8-6 10 3" fill="none" stroke={PAW_ICON_PAPER} strokeLinecap="round" strokeLinejoin="round" strokeWidth="2.7" />
  </>;
}

export type PawBrandMarkProps = Omit<SVGProps<SVGSVGElement>, 'children'> & {
  size?: number | string;
  title?: string;
};

/**
 * The system mark uses the supplied planetary paw artwork shared by the
 * native icon and web app icons. It stays static on system chrome.
 */
export function PawBrandMark({ className = '', size = 16, title, ...props }: PawBrandMarkProps) {
  return (
    <svg
      {...props}
      aria-hidden={title ? undefined : 'true'}
      aria-label={title}
      className={`paw-brand-mark ${className}`.trim()}
      data-paw-brand="stellar"
      fill="currentColor"
      focusable="false"
      height={size}
      role={title ? 'img' : undefined}
      viewBox="0 0 48 48"
      width={size}
    >
      {title ? <title>{title}</title> : null}
      <image height="48" href={`${import.meta.env.BASE_URL}app-icon-192.png?v=20260909`} preserveAspectRatio="xMidYMid meet" width="48" />
    </svg>
  );
}
