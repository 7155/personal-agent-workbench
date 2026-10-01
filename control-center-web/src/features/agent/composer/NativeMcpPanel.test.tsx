import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { NativeMcpPanel, parseNativeCapabilities } from './NativeMcpPanel';

afterEach(cleanup);
const catalog = (sessionId = 'one') => ({ nativeCapabilities: {
  schemaVersion: 'rag-ime.pi-native-capabilities.v1', sessionId, codemodeMode: 'on',
  mcp: { available: true, active: true, configErrorCount: 0, servers: [
    { name: 'docs', namespace: 'mcp__docs', scope: 'global', state: 'connected', exposure: 'codemode', toolCount: 1, resourceCount: 0, resourceTemplateCount: 0 },
  ] }, tools: [{ name: 'mcp__docs__search', namespace: { name: 'mcp__docs' }, exposure: 'codemode', active: false, routable: true, description: '查找文档', parameters: { type: 'object' } }],
} });

describe('native MCP capability panel', () => {
  it('shows the actual native tool and exposure without equating disclosure with availability', async () => {
    const load = vi.fn().mockResolvedValue(catalog()); const invoke = vi.fn();
    render(<NativeMcpPanel sessionId="one" query="" filter="all" locked={false} load={load} invoke={invoke} />);
    expect(await screen.findByText('docs')).toBeInTheDocument();
    const user = userEvent.setup(); await user.click(screen.getByText('docs'));
    expect(screen.getByText('mcp__docs__search')).toBeInTheDocument();
    expect(screen.getByText(/未直接披露/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '重连 docs' }));
    expect(invoke).toHaveBeenCalledWith('one', '/mcp reconnect docs');
    expect(load).toHaveBeenCalledWith('one');
  });
  it('keeps inspection available during a turn while refusing connection mutations', async () => {
    const invoke = vi.fn();
    render(<NativeMcpPanel sessionId="one" query="" filter="all" locked load={vi.fn().mockResolvedValue(catalog())} invoke={invoke} />);
    await screen.findByText('docs');
    expect(screen.getByRole('button', { name: '重连 docs' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '刷新 MCP 状态' })).toBeEnabled();
    expect(invoke).not.toHaveBeenCalled();
  });
  it('rejects a previous Session response and retries a read failure', async () => {
    const load = vi.fn().mockResolvedValueOnce(catalog('previous')).mockResolvedValue(catalog());
    render(<NativeMcpPanel sessionId="one" query="" filter="all" locked={false} load={load} invoke={vi.fn()} />);
    expect(await screen.findByText('无法读取 MCP 状态')).toBeInTheDocument();
    expect(screen.queryByText('docs')).not.toBeInTheDocument();
    await userEvent.setup().click(screen.getByRole('button', { name: '刷新 MCP 状态' }));
    expect(await screen.findByText('docs')).toBeInTheDocument();
  });
  it('discards a slow response after the selected partner changes', async () => {
    let resolve: (value: unknown) => void = () => {};
    const load = vi.fn().mockImplementationOnce(() => new Promise(value => { resolve = value; })).mockResolvedValue(catalog('two'));
    const props = { query: '', filter: 'all' as const, locked: false, load, invoke: vi.fn() };
    const { rerender } = render(<NativeMcpPanel {...props} sessionId="one" />);
    rerender(<NativeMcpPanel {...props} sessionId="two" />);
    await screen.findByText('docs'); resolve(catalog());
    await waitFor(() => expect(load).toHaveBeenCalledWith('two'));
    expect(parseNativeCapabilities(catalog('two'), 'two').sessionId).toBe('two');
    expect(() => parseNativeCapabilities(catalog(), 'two')).toThrow();
  });
});
