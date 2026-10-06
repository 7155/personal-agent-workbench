import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { useOptionalControlTransport } from '@/app/control-transport';
import { Button, Popover, PopoverContent, PopoverTrigger } from '@/components/primitives';
import type { CapabilityCatalog, CapabilityPreference } from '@/features/plugins/capability-policy';
import { openPawOsRoute, usePawOsDesktop } from '@/features/paw-os/surface-context';
import { CapabilityMark } from '../marks/ConversationMarks';
import type { CodemodeMode, SessionSummary, ToolManifest } from '../types';
import { countAvailableTools, countRegisteredTools, toolAvailableForConversation } from './tool-policy';
import { buildCapabilityRows, type CapabilityFilter, type CapabilitySection } from './capability-display';
import { usePresentationMotion } from '@/features/conversation-ui/reading/reading-preferences';
import { PiCapabilityBrowser } from './PiCapabilityBrowser';
import { NativeMcpPanel } from './NativeMcpPanel';
import './pi-capabilities.css';

/** One entry for Session and Room. Pi retains native MCP connection/execution ownership. */
export function ToolPicker({ adjustmentDisabled, capabilityCatalog, capabilityPolicyPending,
  codemodeMode, codemodeModePending = false,
  tools, status: receivedStatus, session, sessionId = session?.id, disabled, requestOpen, requestQuery = '',
  onCapabilityPreferenceChange, onCodemodeModeChange, onSelect,
}: {
  adjustmentDisabled: boolean; capabilityCatalog?: CapabilityCatalog; capabilityPolicyPending: boolean;
  codemodeMode?: CodemodeMode; codemodeModePending?: boolean;
  tools: ToolManifest[]; status: 'loading' | 'ready' | 'failed'; session?: SessionSummary; sessionId?: string;
  disabled: boolean; requestOpen: number; requestQuery?: string;
  onCapabilityPreferenceChange: (canonicalId: string, preference: CapabilityPreference) => void;
  onCodemodeModeChange?: (mode: CodemodeMode) => void;
  onSelect: (tool: ToolManifest) => void;
}) {
  const desktop = usePawOsDesktop(); const titleId = useId();
  const transport = useOptionalControlTransport();
  const loadNative = useCallback(async (owner: string) => {
    if (!transport) throw new Error('Transport unavailable');
    return transport.request({ pathId: 'agent.session.commands', params: { sessionId: owner } });
  }, [transport]);
  const invokeNative = useCallback(async (owner: string, command: string) => {
    if (!transport) throw new Error('Transport unavailable');
    return transport.request({ pathId: 'agent.session.command.invoke', params: { sessionId: owner }, body: { command } });
  }, [transport]);
  const [openedFor, setOpenedFor] = useState<string | null>(null);
  const [query, setQuery] = useState(''); const [section, setSection] = useState<CapabilitySection>('all');
  const [filter, setFilter] = useState<CapabilityFilter>('all'); const [selectedKey, setSelectedKey] = useState('');
  const searchRef = useRef<HTMLInputElement>(null); const consumedRequest = useRef('');
  const scope = sessionId ?? '';
  const matched = !capabilityCatalog?.sessionPolicy || capabilityCatalog.sessionPolicy.sessionId === sessionId;
  const status = matched ? receivedStatus : 'loading';
  const open = openedFor === scope && !disabled;
  const motion = usePresentationMotion(open);
  const browserRef = useRef<HTMLDivElement>(null);
  const managedViewRef = useRef<{ scope: string; scrollPositions: Record<string, number>; expandedDetails: number[] } | undefined>(undefined);
  const restoringViewRef = useRef<typeof managedViewRef.current>(undefined);
  const mountBrowser = useCallback((node: HTMLDivElement | null) => {
    browserRef.current = node;
    const retained = restoringViewRef.current;
    if (!node || !retained) return;
    requestAnimationFrame(() => {
      if (!node.isConnected || restoringViewRef.current !== retained) return;
      const details = node.querySelectorAll<HTMLDetailsElement>('.pi-capabilities__detail details');
      retained.expandedDetails.forEach(index => { if (details[index]) details[index].open = true; });
      for (const [selector, scrollTop] of Object.entries(retained.scrollPositions)) {
        const element = node.querySelector<HTMLElement>(selector);
        if (element) element.scrollTop = scrollTop;
      }
      restoringViewRef.current = undefined;
    });
  }, []);
  const availableCount = matched && status === 'ready' ? countAvailableTools(tools, session, capabilityCatalog, sessionId) : 0;
  const registeredCount = matched && status === 'ready' ? countRegisteredTools(tools) : 0;
  const rows = useMemo(() => status === 'ready' ? buildCapabilityRows(tools, session, capabilityCatalog, sessionId) : [],
    [tools, session, capabilityCatalog, sessionId, status]);
  function begin(value = '') { managedViewRef.current = undefined; restoringViewRef.current = undefined; setQuery(value); setSection('all'); setFilter('all'); setSelectedKey(''); setOpenedFor(scope); }
  useEffect(() => { setOpenedFor(null); setSelectedKey(''); managedViewRef.current = undefined; restoringViewRef.current = undefined; }, [scope]);
  useEffect(() => {
    const request = `${scope}:${requestOpen}`;
    if (requestOpen <= 0 || consumedRequest.current === request || status !== 'ready' || disabled) return;
    consumedRequest.current = request;
    if (managedViewRef.current?.scope === scope && !requestQuery) {
      restoringViewRef.current = managedViewRef.current; managedViewRef.current = undefined;
      setOpenedFor(scope);
    } else begin(requestQuery);
  }, [scope, requestOpen, requestQuery, status, disabled]);
  const label = status === 'ready' ? `${availableCount} 个当前可用工具，${registeredCount} 个已登记工具`
    : status === 'failed' ? '能力目录暂不可用' : '能力目录正在读取';
  return <Popover open={open} onOpenChange={value => value ? begin() : setOpenedFor(null)}>
    <PopoverTrigger asChild><Button aria-label={`对话功能：记忆、工具、插件与技能；${label}`} title={label}
      className="agent-composer__picker" size="small" variant="quiet" disabled={disabled}
      data-effective-tool-count={availableCount} data-registered-tool-count={registeredCount} data-status={status}
      leadingIcon={<CapabilityMark size={16} />}>
      <span className="agent-composer__picker-text">功能</span><span className="agent-composer__picker-detail">{status === 'ready' ? ` · ${availableCount}` : status === 'failed' ? ' · 未同步' : ' · 读取中'}</span>
    </Button></PopoverTrigger>
    <PopoverContent align="start" aria-labelledby={titleId} className="pi-capabilities-popover" onMouseDown={event => event.stopPropagation()}
      onOpenAutoFocus={event => { event.preventDefault(); searchRef.current?.focus(); }}>
      <div ref={mountBrowser}><PiCapabilityBrowser rows={rows} query={query} section={section} filter={filter} selectedKey={selectedKey}
        status={status} motion={motion} locked={adjustmentDisabled || disabled} pending={capabilityPolicyPending} titleId={titleId} searchRef={searchRef}
        codemodeMode={codemodeMode} codemodeModePending={codemodeModePending} onCodemodeModeChange={onCodemodeModeChange}
        mcpPanel={open && section === 'mcp' ? <NativeMcpPanel key={scope} sessionId={scope} query={query} filter={filter}
          locked={adjustmentDisabled || disabled || capabilityPolicyPending} load={loadNative} invoke={invokeNative} /> : null}
        onQuery={setQuery} onSection={value => { setSection(value); setSelectedKey(''); }} onFilter={setFilter} onSelect={key => {
          const previous = selectedKey; setSelectedKey(key);
          requestAnimationFrame(() => {
            if (!key) {
              Array.from(browserRef.current?.querySelectorAll<HTMLButtonElement>('[data-capability-key]') ?? []).find(button => button.dataset.capabilityKey === previous)?.focus({ preventScroll: true });
            } else if (window.matchMedia?.('(max-width:620px)').matches) {
              browserRef.current?.querySelector<HTMLElement>('.pi-capabilities__detail h3')?.focus({ preventScroll: true });
            }
          });
        }}
        onPreference={(key, preference) => {
          const row = rows.find(item => item.key === key);
          if (!matched || !row?.configurable || disabled || adjustmentDisabled || capabilityPolicyPending) return;
          if (!['inherit', 'enabled', 'disabled'].includes(preference)) return;
          onCapabilityPreferenceChange(key, preference);
        }}
        onInsert={row => {
          if (!row.tool || disabled || adjustmentDisabled || !matched || status !== 'ready') return;
          if (!toolAvailableForConversation(row.tool, session, capabilityCatalog, sessionId)) return;
          setOpenedFor(null); onSelect(row.tool);
        }} onClose={() => setOpenedFor(null)} onManage={() => {
          const node = browserRef.current;
          managedViewRef.current = { scope,
            scrollPositions: Object.fromEntries(['.pi-capabilities__body', '.pi-capabilities__list', '.pi-capabilities__detail'].map(selector => [selector, node?.querySelector<HTMLElement>(selector)?.scrollTop ?? 0])),
            expandedDetails: Array.from(node?.querySelectorAll<HTMLDetailsElement>('.pi-capabilities__detail details') ?? []).flatMap((detail, index) => detail.open ? [index] : []),
          };
          setOpenedFor(null); const params = new URLSearchParams({ view: 'capabilities' });
          if (sessionId) params.set('sessionId', sessionId);
          if (section === 'memory') params.set('capability', 'tool:memory');
          else if (selectedKey) params.set('capability', selectedKey);
          openPawOsRoute(desktop, `/plugins?${params.toString()}`);
        }} /></div>
    </PopoverContent>
  </Popover>;
}
