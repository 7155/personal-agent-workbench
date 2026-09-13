import { fireEvent, render, screen, within } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { TooltipProvider } from '@/components/primitives';
import type { CapabilityCatalog } from '@/features/plugins/capability-policy';
import { previewSessions } from '../preview-data';
import type { ToolManifest } from '../types';
import { AgentComposer } from './AgentComposer';

describe('AgentComposer macOS input methods', () => {
  it('keeps marked text local until composition ends and does not send the commit key', () => {
    const onDraftChange = vi.fn();
    const onSend = vi.fn();

    function Harness() {
      const [draft, setDraft] = useState('');
      return (
        <TooltipProvider>
          <AgentComposer
            draft={draft}
            attachments={[]}
            session={previewSessions[0]}
            commands={[]}
            tools={[]}
            toolCatalogStatus="ready"
            busy={false}
            sending={false}
            onDraftChange={(value) => { onDraftChange(value); setDraft(value); }}
            onAttachmentsChange={() => {}}
            onPickAttachments={() => {}}
            onPasteImages={() => {}}
            onToolSelect={() => {}}
            onProductCommand={() => {}}
            onSend={onSend}
            onStop={() => {}}
            onPermissionChange={() => {}}
            onWorkspaceRootsChange={() => {}}
            onModelChange={() => {}}
          />
        </TooltipProvider>
      );
    }

    render(<Harness />);
    const composer = screen.getByRole('textbox', { name: '消息' });

    expect(composer).toHaveAttribute('placeholder', expect.stringContaining('给当前 Session发消息'));
    expect(composer).toHaveAttribute('autocapitalize', 'none');
    expect(composer).toHaveAttribute('autocomplete', 'off');
    expect(composer).toHaveAttribute('autocorrect', 'off');
    expect(composer).toHaveAttribute('spellcheck', 'false');
    expect(composer).not.toHaveAttribute('role');
    expect(composer).not.toHaveAttribute('aria-expanded');
    expect(composer).not.toHaveAttribute('aria-autocomplete');

    fireEvent.focus(composer);
    fireEvent.change(composer, { target: { value: '/' } });
    const commandInput = screen.getByRole('combobox', { name: '消息' });
    expect(commandInput).toHaveAttribute('aria-autocomplete', 'list');
    expect(commandInput).toHaveAttribute('aria-expanded', 'true');
    expect(commandInput).toHaveAttribute('aria-controls', 'agent-command-palette');
    expect(screen.getByRole('listbox', { name: '命令面板' })).toBeInTheDocument();
    onDraftChange.mockClear();

    fireEvent.compositionStart(composer);
    fireEvent.change(composer, { target: { value: 'jinr' } });
    expect(composer).toHaveValue('jinr');
    expect(onDraftChange).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter', keyCode: 229, isComposing: false });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.change(composer, { target: { value: '今儿' } });
    fireEvent.compositionEnd(composer, { data: '今儿' });
    expect(onDraftChange).toHaveBeenLastCalledWith('今儿');
    expect(composer).toHaveValue('今儿');

    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' });
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenLastCalledWith('prompt', '今儿');
  });

  it('labels the capability picker and provides an explicit close target', () => {
    const tool: ToolManifest = {
      schemaVersion: 'rag-ime.control-tool-manifest.v1',
      id: 'planning',
      domain: 'planning',
      displayName: '规划与任务',
      description: '查看每日计划',
      category: 'planning',
      riskLevel: 'R1',
      sessionModes: ['coordinator'],
      operations: ['dashboard'],
      resultPresentation: 'status',
      availability: 'online',
      version: '1',
    };
    const capabilityCatalog: CapabilityCatalog = {
      schemaVersion: 'rag-ime.capability-catalog.v1',
      ok: true,
      revision: 'catalog-1',
      effectiveAtMs: 1,
      projectScope: {
        supported: false,
        identityKind: 'none',
        reason: 'No project scope in this focused picker test.',
      },
      sessionPolicy: {
        sessionId: previewSessions[0].id,
        policyRevision: 1,
        disclosurePreferences: {
          globalDefault: {},
          projectDefault: {},
          session: {},
          effective: { 'tool:planning': 'enabled' },
        },
        effectiveAtMs: 1,
      },
      items: [{
        id: tool.id,
        canonicalId: 'tool:planning',
        kind: 'tool',
        displayName: tool.displayName,
        description: tool.description,
        source: { kind: 'built_in', label: 'Control Center' },
        status: 'available',
        risk: tool.riskLevel,
        requiredPermissions: [],
        authorization: { state: 'authorized', reason: 'Focused picker test fixture.' },
        disclosure: {
          preference: 'inherit',
          effective: 'enabled',
          state: 'disclosed',
          reason: 'Focused picker test fixture.',
        },
        effectiveScope: 'built_in_default',
        reasons: [],
        revision: 'capability-1',
        effectiveAtMs: 1,
      }],
    };
    render(
      <TooltipProvider>
        <AgentComposer
          draft=""
          attachments={[]}
          session={previewSessions[0]}
          commands={[]}
          capabilityCatalog={capabilityCatalog}
          tools={[tool]}
          toolCatalogStatus="ready"
          busy={false}
          sending={false}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={() => {}}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={() => {}}
          onStop={() => {}}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
        />
      </TooltipProvider>,
    );

    const trigger = screen.getByRole('button', { name: '对话功能：记忆、工具、插件与技能；这段对话可执行工具：1 个；已登记工具：1 个' });
    expect(trigger).toHaveTextContent('功能');
    expect(screen.queryByRole('button', { name: '当前对话插件与技能' })).not.toBeInTheDocument();
    fireEvent.click(trigger);
    const dialog = screen.getByRole('dialog', { name: '当前对话工具' });
    expect(within(dialog).getByRole('combobox', { name: '规划与任务的当前对话使用' })).toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'true');
    fireEvent.click(within(dialog).getByRole('button', { name: '当前对话插件与技能' }));
    expect(screen.getByRole('dialog', { name: '当前对话插件与技能' })).toHaveTextContent('没有找到插件或技能');
    fireEvent.click(screen.getByRole('button', { name: '工具 · 1/1' }));
    expect(screen.getByRole('dialog', { name: '当前对话工具' })).toHaveTextContent('规划与任务');
    fireEvent.click(within(dialog).getByRole('button', { name: '关闭当前对话工具' }));
    expect(screen.queryByRole('dialog', { name: '当前对话工具' })).not.toBeInTheDocument();
    expect(trigger).toHaveAttribute('aria-expanded', 'false');
  });

  it('keeps responsive controls together while the send action stays fixed', () => {
    const onJumpLatest = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <AgentComposer
          draft="准备发送"
          attachments={[]}
          session={previewSessions[0]}
          commands={[]}
          tools={[]}
          toolCatalogStatus="ready"
          imageSupport="supported"
          busy={false}
          sending={false}
          showJumpLatest
          onJumpLatest={onJumpLatest}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={() => {}}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={() => {}}
          onStop={() => {}}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
        />
      </TooltipProvider>,
    );

    const controls = container.querySelector('.agent-composer__controls');
    const view = within(container);
    const send = view.getByRole('button', { name: '发送' });
    expect(controls).toContainElement(view.getByRole('button', { name: '添加内容' }));
    expect(controls).toContainElement(view.getByRole('button', { name: /对话权限/ }));
    expect(controls).toContainElement(view.getByRole('button', { name: /这段对话可执行工具/ }));
    expect(controls).not.toContainElement(send);
    expect(send.closest('.agent-composer__toolbar')).not.toBeNull();
    expect(view.queryByRole('button', { name: '打开命令面板' })).not.toBeInTheDocument();
    const jumpLatest = view.getByRole('button', { name: '回到最新' });
    expect(jumpLatest.closest('.agent-composer-wrap')).not.toBeNull();
    expect(jumpLatest.closest('.agent-composer')).toBeNull();
    fireEvent.click(jumpLatest);
    expect(onJumpLatest).toHaveBeenCalledTimes(1);
  });

  it('keeps an embedded vertical App composer focused on data, attachment and send', () => {
    const { container } = render(
      <TooltipProvider>
        <AgentComposer
          minimal
          placeholder="继续追问经营数据…"
          draft=""
          attachments={[]}
          session={previewSessions[0]}
          commands={[]}
          tools={[]}
          toolCatalogStatus="ready"
          busy={false}
          sending={false}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={() => {}}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={() => {}}
          onStop={() => {}}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
        />
      </TooltipProvider>,
    );

    const view = within(container);
    expect(container.querySelector('.agent-composer-wrap')).toHaveAttribute('data-minimal', 'true');
    expect(view.getByRole('textbox', { name: '消息' })).toHaveAttribute('placeholder', '继续追问经营数据…');
    expect(view.getByRole('button', { name: '添加内容' })).toBeInTheDocument();
    expect(view.queryByRole('button', { name: /对话权限/ })).not.toBeInTheDocument();
    expect(view.queryByRole('button', { name: /这段对话可执行工具/ })).not.toBeInTheDocument();
    expect(view.getByRole('button', { name: /发送/ })).toBeInTheDocument();
  });

  it('explains why send is unavailable instead of leaving a silently disabled button', () => {
    function harness(overrides: Partial<Parameters<typeof AgentComposer>[0]> = {}) {
      return (
        <TooltipProvider>
          <AgentComposer
            draft=""
            attachments={[]}
            session={previewSessions[0]}
            commands={[]}
            tools={[]}
            toolCatalogStatus="ready"
            busy={false}
            sending={false}
            onDraftChange={() => {}}
            onAttachmentsChange={() => {}}
            onPickAttachments={() => {}}
            onPasteImages={() => {}}
            onToolSelect={() => {}}
            onProductCommand={() => {}}
            onSend={() => {}}
            onStop={() => {}}
            onPermissionChange={() => {}}
            onWorkspaceRootsChange={() => {}}
            onModelChange={() => {}}
            {...overrides}
          />
        </TooltipProvider>
      );
    }

    const { container, rerender } = render(harness());
    const view = within(container);
    expect(view.getByRole('button', { name: '发送（先输入内容或添加附件）' })).toBeDisabled();

    rerender(harness({ draft: '已有内容', modelChanging: true }));
    expect(view.getByRole('button', { name: '发送（正在切换模型）' })).toBeDisabled();

    rerender(harness({ draft: '已有内容', session: undefined }));
    expect(view.getByRole('button', { name: '发送（先选择或创建对话）' })).toBeDisabled();

    rerender(harness({ draft: '已有内容' }));
    expect(view.getByRole('button', { name: '发送' })).toBeEnabled();
  });

  it('uses double Escape to request an in-place edit without disturbing IME input', () => {
    const onEditPrevious = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <AgentComposer
          draft=""
          attachments={[]}
          session={previewSessions[0]}
          commands={[]}
          tools={[]}
          toolCatalogStatus="ready"
          busy={false}
          sending={false}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={() => {}}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={() => {}}
          onStop={() => {}}
          onEditPrevious={onEditPrevious}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
        />
      </TooltipProvider>,
    );
    const composer = within(container).getByRole('textbox', { name: '消息' });
    fireEvent.keyDown(composer, { key: 'Escape' });
    expect(onEditPrevious).not.toHaveBeenCalled();
    fireEvent.keyDown(composer, { key: 'Escape' });
    expect(onEditPrevious).toHaveBeenCalledTimes(1);
  });

  it('sends a native follow-up without exposing a permanent delivery-mode switch', () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <AgentComposer
          draft="补充要求"
          attachments={[]}
          session={previewSessions[0]}
          commands={[]}
          tools={[]}
          toolCatalogStatus="ready"
          imageSupport="supported"
          busy
          sending={false}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={() => {}}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={onSend}
          onStop={onStop}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
        />
      </TooltipProvider>,
    );

    const view = within(container);
    const composer = view.getByRole('textbox', { name: '消息' });
    const busyFrame = container.querySelector('.agent-composer__busy-frame');
    expect(busyFrame).toBeInTheDocument();
    expect(busyFrame).toHaveAttribute('aria-hidden', 'true');
    expect(busyFrame?.querySelector(':scope > i')).toBeInTheDocument();
    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' });
    expect(onSend).toHaveBeenLastCalledWith('followUp', '补充要求');
    expect(view.queryByRole('radiogroup', { name: '消息投递方式' })).not.toBeInTheDocument();
    fireEvent.click(view.getByRole('button', { name: '停止本轮' }));
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(view.getByRole('button', { name: '添加内容' })).toBeEnabled();
  });

  it('sends an attachment follow-up directly when the local text hold cannot carry it', () => {
    const onQueue = vi.fn(() => true);
    const onSend = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <AgentComposer
          draft=""
          attachments={[{ id: 'a1', name: '设计稿.png', mimeType: 'image/png', byteSize: 2048, source: 'picker' }]}
          session={previewSessions[0]}
          commands={[]}
          tools={[]}
          toolCatalogStatus="ready"
          imageSupport="supported"
          busy
          sending={false}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={() => {}}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={onSend}
          onStop={() => {}}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
          onQueue={onQueue}
        />
      </TooltipProvider>,
    );

    const view = within(container);
    const composer = view.getByRole('textbox', { name: '消息' });
    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' });
    expect(onQueue).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenLastCalledWith('followUp', '');
    expect(view.queryByRole('radiogroup', { name: '消息投递方式' })).not.toBeInTheDocument();
  });

  it('does not split text from attachments when a running turn has a text-only local queue', () => {
    const onQueue = vi.fn(() => true);
    const onSend = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <AgentComposer
          draft="请结合设计稿继续"
          attachments={[{ id: 'a1', name: '设计稿.png', mimeType: 'image/png', byteSize: 2048, source: 'picker' }]}
          session={previewSessions[0]}
          commands={[]}
          tools={[]}
          toolCatalogStatus="ready"
          imageSupport="supported"
          busy
          sending={false}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={() => {}}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={onSend}
          onStop={() => {}}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
          onQueue={onQueue}
        />
      </TooltipProvider>,
    );

    fireEvent.keyDown(within(container).getByRole('textbox', { name: '消息' }), {
      key: 'Enter',
      code: 'Enter',
    });

    expect(onQueue).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith('followUp', '请结合设计稿继续');
  });

  it('accepts pasted non-image files and shows a type badge instead of a broken thumbnail', () => {
    const onPasteImages = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <AgentComposer
          draft=""
          attachments={[{
            id: 'media_pdf01',
            name: '发布说明.pdf',
            mimeType: 'application/pdf',
            byteSize: 2048,
            source: 'clipboard',
          }]}
          session={previewSessions[0]}
          commands={[]}
          tools={[]}
          toolCatalogStatus="ready"
          busy={false}
          sending={false}
          onDraftChange={() => {}}
          onAttachmentsChange={() => {}}
          onPickAttachments={() => {}}
          onPasteImages={onPasteImages}
          onToolSelect={() => {}}
          onProductCommand={() => {}}
          onSend={() => {}}
          onStop={() => {}}
          onPermissionChange={() => {}}
          onWorkspaceRootsChange={() => {}}
          onModelChange={() => {}}
        />
      </TooltipProvider>,
    );

    const view = within(container);
    const chip = container.querySelector('.agent-composer__attachment-chip');
    expect(chip).toHaveAttribute('data-attachment-kind', 'file');
    expect(chip?.querySelector('img')).toBeNull();
    expect(chip?.querySelector('.agent-composer__attachment-badge')).toHaveTextContent('PDF');
    expect(view.getByRole('button', { name: '移除 发布说明.pdf' })).toBeInTheDocument();

    const pdf = new File(['%PDF-1.7'], '发布说明.pdf', { type: 'application/pdf' });
    fireEvent.paste(view.getByRole('textbox', { name: '消息' }), {
      clipboardData: { files: [pdf], items: [], getData: () => '' },
    });
    expect(onPasteImages).toHaveBeenCalledWith([pdf]);
  });
});
