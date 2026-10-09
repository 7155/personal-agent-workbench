import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrictMode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { TooltipProvider } from '@/components/primitives';
import { StubControlTransport } from '@/test/stub-control-transport';
import { AgentFileBlock } from './AgentFileBlock';
import { AgentFileCollection } from './AgentFileCollection';
import type { ControlRequest } from '@/platform/transport';
import type { UiAgentBlock } from '@/contracts/ui-events';
import { FilePreviewHost } from './FilePreviewHost';
import { RichHtmlPreview } from './RichHtmlPreview';
import { useFilePreviewStore } from './file-preview-store';

afterEach(() => {
  cleanup();
  useFilePreviewStore.getState().reset();
  delete window.webkit;
  vi.restoreAllMocks();
});

describe('file preview interaction', () => {
  it('renders an explicit HTML artifact as a sandboxed managed report preview', async () => {
    const content = '<!doctype html><h1>项目介绍</h1><script>window.pwned = true</script>';
    const createObjectUrl = vi.spyOn(URL, 'createObjectURL');
    const transport = new StubControlTransport('mock', {
      'agent.media.preview': htmlPreview(content),
    });
    const user = userEvent.setup();
    render(
      <StrictMode>
        <TooltipProvider>
          <ControlTransportProvider transport={transport}>
            <AgentFileBlock
              data={{
                mediaId: MEDIA_ID,
                fileName: 'project-intro.html',
                mimeType: 'text/html',
                byteSize: new TextEncoder().encode(content).byteLength,
                sha256: SHA256,
              }}
              sessionId={SESSION_ID}
            />
            <FilePreviewHost />
          </ControlTransportProvider>
        </TooltipProvider>
      </StrictMode>,
    );

    expect(screen.getByText('HTML 报告')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: '预览报告' }));

    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('project-intro.html')).toBeInTheDocument();
    const frame = within(dialog).getByTitle('project-intro.html 交互预览');
    expect(frame.getAttribute('sandbox')).toContain('allow-scripts');
    expect(frame.getAttribute('sandbox')).toContain('allow-forms');
    expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(previewSource(frame)).toContain('<h1>项目介绍</h1>');
    expect(frame).not.toHaveAttribute('srcdoc');
    expect(createObjectUrl).not.toHaveBeenCalled();
  });

  it('expands a managed Markdown file in its message instead of opening a dialog', async () => {
    const transport = new StubControlTransport('mock', {
      'agent.media.preview': preview('# 交付\n\n- 类型检查通过'),
    });
    const user = userEvent.setup();
    render(
      <TooltipProvider>
        <ControlTransportProvider transport={transport}>
          <AgentFileBlock
            data={{
              mediaId: MEDIA_ID,
              fileName: 'acceptance.md',
              mimeType: 'text/markdown',
              sha256: SHA256,
            }}
            sessionId={SESSION_ID}
          />
          <FilePreviewHost />
        </ControlTransportProvider>
      </TooltipProvider>,
    );

    await user.click(screen.getByRole('button', { name: '展开 acceptance.md' }));

    const inline = await screen.findByRole('region', { name: 'acceptance.md 内联预览' });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(within(inline).getByRole('heading', { name: '交付' })).toBeInTheDocument();
    expect(within(inline).getByText('类型检查通过')).toBeInTheDocument();
    expect(within(inline).getByText('文件内容 · 3 行')).toBeInTheDocument();
    await user.click(within(inline).getByRole('button', { name: '复制acceptance.md 内容' }));
    await expect(navigator.clipboard.readText()).resolves.toBe('# 交付\n\n- 类型检查通过');
    expect(transport.requests[0]).toMatchObject({
      pathId: 'agent.media.preview',
      params: { mediaId: MEDIA_ID },
      query: { sessionId: SESSION_ID, sha256: SHA256 },
    });

    await user.click(screen.getByRole('button', { name: '收起 acceptance.md' }));
    expect(screen.queryByRole('region', { name: 'acceptance.md 内联预览' })).not.toBeInTheDocument();
  });

  it('keeps same-name receipts separate and opens each original snapshot with its exact byte size', async () => {
    const blocks: UiAgentBlock[] = Array.from({ length: 6 }, (_, index) => ({
      id: `file-result-${index}`, type: 'file', status: 'completed', presentationKind: 'file.v1',
      data: { mediaId: `media_snapshot_00000${index}`, fileName: index % 2 ? 'notes.md.diff' : 'notes.md',
        mimeType: 'text/markdown', byteSize: index === 0 ? 72 : 124, sha256: String(index + 1).repeat(64) },
    }));
    const transport = new StubControlTransport('mock', {
      'agent.media.preview': (request: ControlRequest) => {
        const block = blocks.find(item => item.data.mediaId === request.params?.mediaId)!;
        const content = `snapshot ${block.id}`.padEnd(Number(block.data.byteSize), ' ');
        return { ...preview(content), descriptor: { ...preview('').descriptor,
          mediaId: block.data.mediaId, fileName: block.data.fileName, sha256: block.data.sha256, byteSize: block.data.byteSize,
          contentUrl: `/api/agent/media/${block.data.mediaId}/content?sessionId=${SESSION_ID}` } };
      },
    });
    const user = userEvent.setup();
    const { container } = render(<TooltipProvider><ControlTransportProvider transport={transport}>
      <AgentFileCollection blocks={[...blocks, blocks[0]!]} sessionId={SESSION_ID} />
    </ControlTransportProvider></TooltipProvider>);
    expect(container.querySelectorAll('.agent-file-block-shell')).toHaveLength(6);
    expect(transport.requests).toHaveLength(0);
    expect(screen.getByText(/72 B/)).toBeInTheDocument();
    for (let index = 0; index < blocks.length; index++) {
      const row = container.querySelectorAll<HTMLButtonElement>('.agent-file-block')[index]!;
      expect(row).toHaveTextContent(index === 0 ? '72 B' : '124 B');
      expect(row).toHaveAttribute('data-kind', index % 2 ? 'diff' : 'document');
      await user.click(row);
      expect(await screen.findByText(`snapshot file-result-${index}`)).toBeInTheDocument();
      expect(transport.requests[index]).toMatchObject({ pathId: 'agent.media.preview',
        params: { mediaId: blocks[index]!.data.mediaId }, query: { sessionId: SESSION_ID, sha256: blocks[index]!.data.sha256 } });
    }
    expect(container).not.toHaveTextContent('最新');
    expect(container).not.toHaveTextContent('版本');
  });

  it('keeps the isolated loopback transport for the native WebKit host', () => {
    window.webkit = {
      messageHandlers: {
        ragImeNativeBridge: { postMessage: () => undefined },
      },
    };
    render(
      <StrictMode>
        <RichHtmlPreview content="<h1>原生报告</h1>" title="native-report.html" />
      </StrictMode>,
    );

    const frame = screen.getByTitle('native-report.html 交互预览');
    expect(frame.getAttribute('src')).toMatch(/^\/__paw_html_preview#/u);
    expect(previewSource(frame)).toContain('<h1>原生报告</h1>');
    expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(frame).not.toHaveAttribute('srcdoc');
  });

  it('delivers large interactive reports once through the opaque preview frame, without an authored URL payload', () => {
    const content = '<!doctype html><html><body><button onclick="this.textContent=\'clicked\'">点击</button><script>document.body.dataset.interactive="yes"</script>'
      + ' '.repeat(1_600_000) + '</body></html>';
    render(<RichHtmlPreview content={content} title="large-earth-report.html" />);
    const frame = screen.getByTitle('large-earth-report.html 交互预览') as HTMLIFrameElement;
    const src = frame.getAttribute('src') ?? '';
    expect(src).toMatch(/^\/__paw_html_preview#message:[0-9a-f-]{36}$/u);
    expect(src).not.toContain('interactive');
    expect(src.length).toBeLessThan(100);
    expect(frame.getAttribute('sandbox')).toContain('allow-scripts');
    expect(frame.getAttribute('sandbox')).not.toContain('allow-same-origin');
    expect(frame).not.toHaveAttribute('srcdoc');
    const token = src.split('#message:')[1];
    const send = vi.spyOn(frame.contentWindow!, 'postMessage');
    const ready = (origin: string, source: MessageEventSource | null, nonce = token) =>
      fireEvent(window, new MessageEvent('message', { data: { type: 'paw-html-preview-ready', token: nonce }, origin, source }));
    ready(window.location.origin, frame.contentWindow);
    ready('null', frame.contentWindow, crypto.randomUUID());
    expect(send).not.toHaveBeenCalled();
    ready('null', frame.contentWindow);
    expect(send).toHaveBeenCalledOnce();
    expect(send).toHaveBeenCalledWith(expect.objectContaining({ type: 'paw-html-preview-document', token, source: expect.stringContaining('document.body.dataset.interactive="yes"') }), '*');
    expect(send.mock.calls[0][0].source).toContain(' '.repeat(1_600_000));
    ready('null', frame.contentWindow);
    expect(send).toHaveBeenCalledOnce();
  });


  it('keeps a file disabled when no authoritative parent session is available', () => {
    render(<AgentFileBlock data={{ mediaId: MEDIA_ID, fileName: 'orphan.diff' }} />);
    expect(screen.getByRole('button', { name: 'orphan.diff 的预览回执不可用' })).toBeDisabled();
  });
});

function previewSource(frame: HTMLElement): string {
  const source = frame.getAttribute('src');
  if (!source) throw new Error('preview URL is missing');
  const encoded = new URL(source, window.location.href).hash.slice(1).replaceAll('-', '+').replaceAll('_', '/');
  const padded = encoded + '='.repeat((4 - encoded.length % 4) % 4);
  const binary = window.atob(padded);
  return new TextDecoder().decode(Uint8Array.from(binary, (character) => character.charCodeAt(0)));
}

const MEDIA_ID = 'media_abcdefghijkl';
const SESSION_ID = 'session-preview-1';
const SHA256 = 'a'.repeat(64);

function preview(content: string) {
  return {
    schemaVersion: 'rag-ime.agent-file-preview.v1',
    descriptor: {
      schemaVersion: 'rag-ime.agent-file-descriptor.v1',
      mediaId: MEDIA_ID,
      sessionId: SESSION_ID,
      fileName: 'acceptance.md',
      mimeType: 'text/markdown',
      byteSize: new TextEncoder().encode(content).byteLength,
      sha256: SHA256,
      previewKind: 'markdown',
      language: '',
      contentUrl: `/api/agent/media/${MEDIA_ID}/content?sessionId=${SESSION_ID}`,
    },
    content,
    previewByteSize: new TextEncoder().encode(content).byteLength,
    truncated: false,
  };
}

function htmlPreview(content: string) {
  return {
    schemaVersion: 'rag-ime.agent-file-preview.v1',
    descriptor: {
      schemaVersion: 'rag-ime.agent-file-descriptor.v1',
      mediaId: MEDIA_ID,
      sessionId: SESSION_ID,
      fileName: 'project-intro.html',
      mimeType: 'text/html',
      byteSize: new TextEncoder().encode(content).byteLength,
      sha256: SHA256,
      previewKind: 'html',
      language: 'html',
      contentUrl: `/api/agent/media/${MEDIA_ID}/content?sessionId=${SESSION_ID}`,
    },
    content,
    previewByteSize: new TextEncoder().encode(content).byteLength,
    truncated: false,
  };
}
