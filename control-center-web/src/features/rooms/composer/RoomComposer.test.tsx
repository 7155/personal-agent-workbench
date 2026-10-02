import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TooltipProvider } from '@/components/primitives';
import { RoomComposer, roomMentionedParticipants } from './RoomComposer';

afterEach(cleanup);

describe('RoomComposer macOS input methods', () => {
  const queuedCommon = () => ({
    room: { id: 'room-awaiting-start', status: 'active', participants: [] },
    personas: [],
    draft: '补充发布边界',
    attachments: [],
    sending: false,
    onDraftChange: vi.fn(),
    onAttachmentsChange: vi.fn(),
    onPasteImages: vi.fn(),
    onPasteFromClipboard: vi.fn(),
    onPickAttachments: vi.fn(),
    onSend: vi.fn(() => false),
    onQueue: vi.fn(() => true),
    onStop: vi.fn(),
  });

  it('accepts external recovery after local edit and clear leave the host draft unchanged', () => {
    const common = queuedCommon();
    function Harness() {
      const [draft, setDraft] = useState('');
      return <TooltipProvider>
        <button onClick={() => setDraft('a')}>Restore externally</button>
        <RoomComposer {...common} draft={draft} onDraftChange={setDraft} />
      </TooltipProvider>;
    }
    render(<Harness />);
    const editor = screen.getByRole('textbox', { name: '协作消息' });
    act(() => {
      fireEvent.change(editor, { target: { value: 'a' } });
      fireEvent.change(editor, { target: { value: '' } });
    });
    expect(editor).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Restore externally' }));
    expect(editor).toHaveValue('a');
    expect(common.onSend).not.toHaveBeenCalled();
  });

  it('applies an external clear during pending local publication even when the same clear was edited locally', () => {
    const common = queuedCommon();
    function Harness() {
      const [draft, setDraft] = useState('原草稿');
      return <TooltipProvider>
        <button onClick={() => setDraft('')}>Clear externally</button>
        <RoomComposer {...common} draft={draft} onDraftChange={setDraft} />
      </TooltipProvider>;
    }
    render(<Harness />);
    const editor = screen.getByRole('textbox', { name: '协作消息' });
    act(() => {
      fireEvent.change(editor, { target: { value: '' } });
      fireEvent.change(editor, { target: { value: '新的本地内容' } });
      fireEvent.click(screen.getByRole('button', { name: 'Clear externally' }));
    });
    expect(editor).toHaveValue('');
    expect(common.onSend).not.toHaveBeenCalled();
  });

  it.each(['ABC_xyz_0123456789', '中文输入必须保留每个字符'])('keeps controlled host input and external replacements intact: %s', (value) => {
    const common = { ...queuedCommon(), draft: '' };
    function Harness() {
      const [draft, setDraft] = useState('');
      return <TooltipProvider>
        <button onClick={() => setDraft('')}>Clear externally</button>
        <button onClick={() => setDraft(value.slice(0, 1))}>Restore an earlier value externally</button>
        <button onClick={() => setDraft('恢复的外部草稿')}>Recover externally</button>
        <RoomComposer {...common} draft={draft} onDraftChange={(next) => { common.onDraftChange(next); setDraft(next); }} />
      </TooltipProvider>;
    }
    render(<Harness />);
    const editor = screen.getByRole('textbox', { name: '协作消息' });
    for (let index = 1; index <= value.length; index += 1) {
      fireEvent.change(editor, { target: { value: value.slice(0, index) } });
    }
    expect(common.onDraftChange).toHaveBeenLastCalledWith(value);
    expect(editor).toHaveValue(value);
    fireEvent.keyDown(editor, { key: 'Enter' });
    expect(common.onSend).toHaveBeenCalledExactlyOnceWith(value);
    expect(editor).toHaveValue(value);
    fireEvent.click(screen.getByRole('button', { name: 'Clear externally' }));
    expect(editor).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Restore an earlier value externally' }));
    expect(editor).toHaveValue(value.slice(0, 1));
    fireEvent.click(screen.getByRole('button', { name: 'Recover externally' }));
    expect(editor).toHaveValue('恢复的外部草稿');
    expect(common.onSend).toHaveBeenCalledTimes(1);
  });

  it('keeps the mention selection and newer text as the controlled host publishes edits', () => {
    const common = { ...queuedCommon(), draft: '', room: {
      id: 'room-mention-echo', status: 'active', participants: [{
        id: 'earth', sessionId: 'earth-session', roleId: 'worker', roleVersion: '1',
        displayName: 'Earth', ordinal: 0, status: 'active',
      }],
    } };
    function Harness() {
      const [draft, setDraft] = useState('');
      return <TooltipProvider><RoomComposer {...common} draft={draft} onDraftChange={setDraft} /></TooltipProvider>;
    }
    render(<Harness />);
    const editor = screen.getByRole('textbox', { name: '协作消息' });
    fireEvent.change(editor, { target: { value: '@' } });
    fireEvent.change(editor, { target: { value: '@E' } });
    expect(editor).toHaveValue('@E');
    expect(screen.getByRole('listbox', { name: '选择要点名的伙伴' })).toBeVisible();
    fireEvent.click(screen.getByRole('option', { name: /Earth/ }));
    expect(editor).toHaveValue('@Earth ');
    expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
    fireEvent.change(editor, { target: { value: '@Earth ABC中文' } });
    expect(editor).toHaveValue('@Earth ABC中文');
    fireEvent.keyDown(editor, { key: 'Enter' });
    expect(common.onSend).toHaveBeenCalledExactlyOnceWith('@Earth ABC中文');
  });

  it('keeps the next controlled draft after an accepted send and applies external clear and recovery', () => {
    const common = { ...queuedCommon(), onSend: vi.fn(() => true) };
    function Harness() {
      const [draft, setDraft] = useState('第一条');
      return <TooltipProvider>
        <button onClick={() => setDraft('')}>Clear externally</button>
        <button onClick={() => setDraft('恢复第一条')}>Recover externally</button>
        <RoomComposer {...common} draft={draft} onDraftChange={setDraft} />
      </TooltipProvider>;
    }
    render(<Harness />);
    const editor = screen.getByRole('textbox', { name: '协作消息' });
    fireEvent.keyDown(editor, { key: 'Enter' });
    expect(common.onSend).toHaveBeenCalledExactlyOnceWith('第一条');
    expect(editor).toHaveValue('');
    fireEvent.change(editor, { target: { value: '下一条 ABC中文' } });
    expect(editor).toHaveValue('下一条 ABC中文');
    fireEvent.click(screen.getByRole('button', { name: 'Clear externally' }));
    expect(editor).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: 'Recover externally' }));
    expect(editor).toHaveValue('恢复第一条');
    expect(common.onSend).toHaveBeenCalledTimes(1);
  });

  it('shows awaiting execution without claiming it started and preserves supplement and queue callbacks', () => {
    const common = queuedCommon();
    render(<TooltipProvider><RoomComposer {...common} taskBusyState="running" awaitingExecutionStart /></TooltipProvider>);
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('请求已排队，正在等待开始；可补充要求，或将新消息排到下一轮。');
    expect(status).not.toHaveTextContent('当前任务仍在执行');
    expect(status).not.toHaveAttribute('data-running');
    expect(status.querySelector('.room-composer__status-icon')).not.toBeInTheDocument();
    const editor = screen.getByRole('textbox', { name: '协作消息' });
    expect(editor).toHaveAttribute('placeholder', '补充当前请求…');
    const supplement = screen.getByRole('button', { name: '补充当前请求' });
    expect(supplement).toBeEnabled();
    fireEvent.click(supplement);
    expect(common.onSend).toHaveBeenCalledExactlyOnceWith('补充发布边界');
    expect(common.onQueue).not.toHaveBeenCalled();
    expect(editor).toHaveValue('补充发布边界');
    fireEvent.click(screen.getByRole('button', { name: '排到当前回合之后' }));
    expect(common.onQueue).toHaveBeenCalledExactlyOnceWith('补充发布边界');
    expect(common.onSend).toHaveBeenCalledTimes(1);
    expect(editor).toHaveValue('');
    fireEvent.click(screen.getByRole('button', { name: '停止当前协作' }));
    expect(common.onStop).toHaveBeenCalledTimes(1);
  });

  it('keeps evidence-confirmed running copy when execution is no longer awaiting start', () => {
    const common = queuedCommon();
    const view = render(<TooltipProvider><RoomComposer {...common} taskBusyState="running" awaitingExecutionStart /></TooltipProvider>);
    expect(screen.getByRole('status')).not.toHaveAttribute('data-running');
    view.rerender(<TooltipProvider><RoomComposer {...common} taskBusyState="running" /></TooltipProvider>);
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('当前任务仍在执行');
    expect(status).toHaveAttribute('data-running', 'true');
    expect(status.querySelector('.room-composer__status-icon')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '立即干预当前回合' })).toBeEnabled();
    expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveAttribute('placeholder', '立即干预当前回合…');
  });

  it('gives a pending answer priority over awaiting execution presentation', () => {
    const common = queuedCommon();
    render(<TooltipProvider><RoomComposer {...common} taskBusyState="running" awaitingExecutionStart pendingUserAnswer /></TooltipProvider>);
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('当前任务正在等待你的回答');
    expect(status).not.toHaveTextContent('请求已排队');
    expect(status).not.toHaveAttribute('data-running');
    expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveAttribute('placeholder', '回答伙伴正在等待的问题…');
    expect(screen.queryByRole('button', { name: '排到当前回合之后' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '发送问题回答' }));
    expect(common.onSend).toHaveBeenCalledExactlyOnceWith('补充发布边界');
    expect(common.onQueue).not.toHaveBeenCalled();
  });

  it('retains attachments ahead of awaiting execution copy without admitting a supplement or queue', () => {
    const common = queuedCommon();
    render(<TooltipProvider><RoomComposer {...common} taskBusyState="running" awaitingExecutionStart attachments={[{
      mediaId: 'media-awaiting', fileName: 'proof.png', mimeType: 'image/png', byteSize: 8,
      sha256: 'a'.repeat(64), roomId: 'room-awaiting-start',
    }]} /></TooltipProvider>);
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('附件已保留，当前协作结束后就能发送。');
    expect(status).not.toHaveTextContent('请求已排队');
    expect(status).not.toHaveAttribute('data-running');
    const supplement = screen.getByRole('button', { name: '补充当前请求' });
    const queue = screen.getByRole('button', { name: '排到当前回合之后' });
    expect(supplement).toBeDisabled();
    expect(queue).toBeDisabled();
    fireEvent.click(supplement);
    fireEvent.click(queue);
    expect(common.onSend).not.toHaveBeenCalled();
    expect(common.onQueue).not.toHaveBeenCalled();
  });

  it.each(['blocked', 'waiting'] as const)('does not replace the %s contract with awaiting execution copy', (state) => {
    render(<TooltipProvider><RoomComposer {...queuedCommon()} taskBusyState={state} awaitingExecutionStart /></TooltipProvider>);
    const status = screen.getByRole('status');
    expect(status).not.toHaveTextContent('请求已排队');
    expect(status).not.toHaveAttribute('data-running');
    expect(screen.getByRole('button', { name: state === 'blocked' ? '告诉伙伴怎样继续' : '立即干预当前回合' })).toBeEnabled();
    expect(screen.queryByRole('button', { name: '排到当前回合之后' })).not.toBeInTheDocument();
  });

  it('shows a route waiting state without implying that execution is running', () => {
    render(<TooltipProvider><RoomComposer
      room={{ id: 'room-waiting', status: 'active', participants: [] }} personas={[]} draft="下一轮补充"
      attachments={[]} sending={false} taskBusyState="waiting" busySubmitBehavior="queue"
      onDraftChange={vi.fn()} onAttachmentsChange={vi.fn()} onPasteImages={vi.fn()}
      onPasteFromClipboard={vi.fn()} onPickAttachments={vi.fn()} onSend={vi.fn()}
    /></TooltipProvider>);
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('当前任务等待重新判断，原任务已保留');
    expect(status).not.toHaveAttribute('data-running');
    expect(status.querySelector('.room-composer__status-icon')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '排入下一轮任务' })).toBeEnabled();
  });
  it('keeps add, settings, stop and send on one shared toolbar without changing the draft', async () => {
    const user = userEvent.setup();
    const onSend = vi.fn();
    const onStop = vi.fn();
    const { container } = render(<TooltipProvider><RoomComposer
      room={{ id: 'room-toolbar', status: 'active', participants: [] }}
      personas={[]}
      draft=""
      attachments={[]}
      sending={false}
      taskBusyState="running"
      capabilityControls={<button type="button" aria-label="伙伴设置">伙伴设置</button>}
      onDraftChange={vi.fn()}
      onAttachmentsChange={vi.fn()}
      onPasteImages={vi.fn()}
      onPasteFromClipboard={vi.fn()}
      onPickAttachments={vi.fn()}
      onSend={onSend}
      onStop={onStop}
    /></TooltipProvider>);
    const controls = container.querySelector('.agent-composer__controls');
    const actions = container.querySelector('.agent-composer__actions');
    expect(controls?.firstElementChild).toBe(screen.getByRole('button', { name: '添加内容' }));
    expect(controls).toContainElement(screen.getByRole('button', { name: '伙伴设置' }));
    expect(actions).toContainElement(screen.getByRole('button', { name: '停止当前协作' }));
    expect(actions).toContainElement(screen.getByRole('button', { name: '立即干预当前回合' }));

    const editor = screen.getByRole('textbox', { name: '协作消息' });
    await user.type(editor, '继续检查当前结果');
    await user.click(screen.getByRole('button', { name: '伙伴设置' }));
    expect(editor).toHaveValue('继续检查当前结果');
    expect(onSend).not.toHaveBeenCalled();
    await user.click(screen.getByRole('button', { name: '停止当前协作' }));
    expect(onStop).toHaveBeenCalledTimes(1);
    expect(editor).toHaveValue('继续检查当前结果');
    await user.click(screen.getByRole('button', { name: '立即干预当前回合' }));
    expect(onSend).toHaveBeenCalledWith('继续检查当前结果');
  });

  it('shows stable planet aliases and resolves @Earth to the real participant', () => {
    const participant = {
      id: 'participant-earth',
      sessionId: 'session-earth',
      roleId: 'implementer',
      roleVersion: '1',
      displayName: 'Agent 1',
      collaborationRole: 'implementer' as const,
      status: 'active',
    };
    render(
      <TooltipProvider>
        <RoomComposer
          room={{ id: 'room-sol', status: 'active', participants: [participant] }}
          participantAliases={{ 'participant-earth': 'Earth' }}
          personas={[]}
          draft=""
          attachments={[]}
          sending={false}
          onDraftChange={vi.fn()}
          onAttachmentsChange={vi.fn()}
          onPasteImages={vi.fn()}
          onPasteFromClipboard={vi.fn()}
          onPickAttachments={vi.fn()}
          onSend={vi.fn()}
        />
      </TooltipProvider>,
    );

    const editor = screen.getByRole('textbox', { name: '协作消息' });
    fireEvent.change(editor, { target: { value: '@' } });
    expect(editor.getAttribute('aria-controls')).toBe(screen.getByRole('listbox').id);
    const earth = screen.getByRole('option', { name: /Earth/ });
    expect(earth).toHaveTextContent('实现与验证');
    expect(earth).not.toHaveTextContent('Agent 1');
    fireEvent.click(earth);
    expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveValue('@Earth ');
    expect(roomMentionedParticipants([participant], '@Earth 请复核', { 'participant-earth': 'Earth' }))
      .toEqual([participant]);
  });

  it('resolves every explicit planet mention so active-turn routing can reject ambiguity', () => {
    const participants = [
      {
        id: 'participant-earth', sessionId: 'session-earth', roleId: 'implementer', roleVersion: '1',
        displayName: 'Agent 1', status: 'active',
      },
      {
        id: 'participant-mars', sessionId: 'session-mars', roleId: 'reviewer', roleVersion: '1',
        displayName: 'Agent 2', status: 'active',
      },
    ];

    expect(roomMentionedParticipants(participants, '@Earth @Mars 分别调整', {
      'participant-earth': 'Earth',
      'participant-mars': 'Mars',
    })).toEqual(participants);
  });

  it('keeps marked text local and does not send the IME commit key', () => {
    const onDraftChange = vi.fn();
    const onSend = vi.fn();

    function Harness() {
      const [draft, setDraft] = useState('');
      return (
        <TooltipProvider>
          <RoomComposer
            room={{
              id: 'room-1',
              status: 'active',
              roomKind: 'collaboration',
              participants: [{
                id: 'participant-1',
                sessionId: 'session-1',
                roleId: 'companion-present-v1',
                roleVersion: '1',
                displayName: '澄',
                status: 'active',
              }],
            }}
            personas={[]}
            draft={draft}
            attachments={[]}
            sending={false}
            onDraftChange={(value) => {
              onDraftChange(value);
              setDraft(value);
            }}
            onAttachmentsChange={vi.fn()}
            onPasteImages={vi.fn()}
            onPasteFromClipboard={vi.fn()}
            onPickAttachments={vi.fn()}
            onSend={onSend}
          />
        </TooltipProvider>
      );
    }

    render(<Harness />);
    const composer = screen.getByRole('textbox', { name: '协作消息' });
    expect(composer).toHaveAttribute('autocapitalize', 'none');
    expect(composer).toHaveAttribute('autocomplete', 'off');
    expect(composer).toHaveAttribute('autocorrect', 'off');
    expect(composer).toHaveAttribute('spellcheck', 'false');

    fireEvent.compositionStart(composer);
    fireEvent.change(composer, { target: { value: 'duiq' } });
    expect(composer).toHaveValue('duiq');
    expect(onDraftChange).not.toHaveBeenCalled();

    fireEvent.keyDown(composer, {
      key: 'Enter',
      code: 'Enter',
      keyCode: 229,
      isComposing: false,
    });
    expect(onSend).not.toHaveBeenCalled();

    fireEvent.change(composer, { target: { value: '对齐' } });
    fireEvent.compositionEnd(composer, { data: '对齐' });
    expect(onDraftChange).toHaveBeenLastCalledWith('对齐');
    expect(composer).toHaveValue('对齐');

    fireEvent.keyDown(composer, { key: 'Enter', code: 'Enter' });
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenLastCalledWith('对齐');
  });

  it('imports a clipboard File and invokes native fallback for an empty WebKit paste', () => {
    const onPasteImages = vi.fn();
    const onPasteFromClipboard = vi.fn();
    const image = new File(['png'], 'diagram.png', { type: 'image/png' });
    render(
      <TooltipProvider>
        <RoomComposer
          room={{ id: 'room-1', status: 'active', participants: [] }}
          personas={[]}
          draft=""
          attachments={[]}
          sending={false}
          onDraftChange={vi.fn()}
          onAttachmentsChange={vi.fn()}
          onPasteImages={onPasteImages}
          onPasteFromClipboard={onPasteFromClipboard}
          onPickAttachments={vi.fn()}
          onSend={vi.fn()}
        />
      </TooltipProvider>,
    );
    const composer = screen.getByRole('textbox', { name: '协作消息' });
    expect(fireEvent.paste(composer, {
      clipboardData: { files: [image], items: [], getData: () => '' },
    })).toBe(false);
    expect(onPasteImages).toHaveBeenCalledWith([image]);
    expect(fireEvent.paste(composer, {
      clipboardData: { files: [], items: [], getData: () => '' },
    })).toBe(false);
    expect(onPasteFromClipboard).toHaveBeenCalledTimes(1);
  });

  it('hands Finder local references to the native pasteboard fallback while leaving links as text', () => {
    const onPasteFromClipboard = vi.fn();
    render(<TooltipProvider><RoomComposer
      room={{ id: 'room-finder-paste', status: 'active', participants: [] }} personas={[]} draft=""
      attachments={[]} sending={false} onDraftChange={vi.fn()} onAttachmentsChange={vi.fn()}
      onPasteImages={vi.fn()} onPasteFromClipboard={onPasteFromClipboard} onPickAttachments={vi.fn()} onSend={vi.fn()}
    /></TooltipProvider>);
    const editor = screen.getByRole('textbox', { name: '协作消息' });

    fireEvent.paste(editor, { clipboardData: {
      files: [], items: [],
      getData: (kind: string) => kind === 'text/uri-list' ? 'file:///Users/example/Desktop/brief.pdf' : 'brief.pdf',
    } });
    expect(onPasteFromClipboard).toHaveBeenCalledTimes(1);
    expect(editor).toHaveValue('');

    fireEvent.paste(editor, { clipboardData: {
      files: [], items: [],
      getData: (kind: string) => kind === 'text/uri-list' ? 'https://example.test/brief.pdf' : 'https://example.test/brief.pdf',
    } });
    expect(onPasteFromClipboard).toHaveBeenCalledTimes(1);
  });

  it('renders a removable managed attachment chip', () => {
    const onAttachmentsChange = vi.fn();
    render(
      <TooltipProvider>
        <RoomComposer
          room={{ id: 'room-1', status: 'active', participants: [] }}
          personas={[]}
          draft=""
          attachments={[{
            mediaId: 'media_room_attachment01',
            roomId: 'room-1',
            fileName: 'diagram.png',
            mimeType: 'image/png',
            byteSize: 128,
            sha256: 'a'.repeat(64),
          }]}
          sending={false}
          onDraftChange={vi.fn()}
          onAttachmentsChange={onAttachmentsChange}
          onPasteImages={vi.fn()}
          onPasteFromClipboard={vi.fn()}
          onPickAttachments={vi.fn()}
          onSend={vi.fn()}
        />
      </TooltipProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: '移除 diagram.png' }));
    expect(onAttachmentsChange).toHaveBeenCalledWith([]);
  });

  it('forwards pasted non-image files and badges non-image receipts instead of faking thumbnails', () => {
    const onPasteImages = vi.fn();
    const { container } = render(
      <TooltipProvider>
        <RoomComposer
          room={{ id: 'room-1', status: 'active', participants: [] }}
          personas={[]}
          draft=""
          attachments={[{
            mediaId: 'media_room_attachment02',
            roomId: 'room-1',
            fileName: 'release-notes.zip',
            mimeType: 'application/zip',
            byteSize: 4096,
            sha256: 'b'.repeat(64),
          }]}
          sending={false}
          onDraftChange={vi.fn()}
          onAttachmentsChange={vi.fn()}
          onPasteImages={onPasteImages}
          onPasteFromClipboard={vi.fn()}
          onPickAttachments={vi.fn()}
          onSend={vi.fn()}
        />
      </TooltipProvider>,
    );

    const chip = container.querySelector('.agent-composer__attachment-chip');
    expect(chip).toHaveAttribute('data-attachment-kind', 'file');
    expect(chip?.querySelector('img')).toBeNull();
    expect(chip?.querySelector('.agent-composer__attachment-badge')).toHaveTextContent('ZIP');
    expect(screen.getByRole('button', { name: '移除 release-notes.zip' })).toBeInTheDocument();

    const pdf = new File(['%PDF-1.7'], 'spec.pdf', { type: 'application/pdf' });
    fireEvent.paste(screen.getByRole('textbox', { name: '协作消息' }), {
      clipboardData: { files: [pdf], items: [], getData: () => '' },
    });
    expect(onPasteImages).toHaveBeenCalledWith([pdf]);
  });

  it('offers native steer while a task is busy and keeps pending answers constrained', () => {
    const onSend = vi.fn();
    const common = {
      room: {
        id: 'room-1',
        status: 'active',
        roomKind: 'collaboration' as const,
        participants: [],
      },
      personas: [],
      draft: '补充发布边界',
      attachments: [],
      sending: false,
      onDraftChange: vi.fn(),
      onAttachmentsChange: vi.fn(),
      onPasteImages: vi.fn(),
      onPasteFromClipboard: vi.fn(),
      onPickAttachments: vi.fn(),
      onSend,
    };
    const view = render(
      <TooltipProvider>
        <RoomComposer {...common} taskBusyState="running" />
      </TooltipProvider>,
    );

    expect(screen.getByRole('button', { name: '立即干预当前回合' })).toBeEnabled();
    expect(screen.getByText(/发送文字会立即干预主持伙伴的当前回合/)).toBeInTheDocument();

    view.rerender(
      <TooltipProvider>
        <RoomComposer
          {...common}
          pendingUserAnswer
          taskBusyState="running"
        />
      </TooltipProvider>,
    );

    const answer = screen.getByRole('button', { name: '发送问题回答' });
    expect(answer).toBeEnabled();
    expect(screen.getByRole('button', { name: '添加内容' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '点名一位伙伴' })).not.toBeInTheDocument();
    fireEvent.click(answer);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith('补充发布边界');
  });
  it('uses Continue for an admitted failed Room turn when the draft is empty', () => {
    const onContinue = vi.fn();
    render(
      <TooltipProvider>
        <RoomComposer
          room={{ id: 'room-continue', status: 'active', participants: [] }}
          personas={[]}
          draft=""
          attachments={[]}
          sending={false}
          continuationAvailable
          onContinue={onContinue}
          onDraftChange={vi.fn()}
          onAttachmentsChange={vi.fn()}
          onPasteImages={vi.fn()}
          onPasteFromClipboard={vi.fn()}
          onPickAttachments={vi.fn()}
          onSend={vi.fn()}
        />
      </TooltipProvider>,
    );

    const button = screen.getByRole('button', { name: '继续当前 Room 协作' });
    expect(button).toBeEnabled();
    fireEvent.click(button);
    expect(onContinue).toHaveBeenCalledTimes(1);
  });
});

it('retains an asynchronously rejected message without overwriting the next draft', async () => {
  let reject!: (reason: Error) => void;
  const pending = new Promise<boolean>((_resolve, fail) => { reject = fail; });
  const onSend = vi.fn(() => pending);
  function Harness() {
    const [draft, setDraft] = useState('第一条');
    return <TooltipProvider><RoomComposer room={{ id: 'room-recovery', status: 'active', participants: [] }} personas={[]} draft={draft} attachments={[]} sending={false} onDraftChange={setDraft} onAttachmentsChange={vi.fn()} onPasteImages={vi.fn()} onPasteFromClipboard={vi.fn()} onPickAttachments={vi.fn()} onSend={onSend} /></TooltipProvider>;
  }
  render(<Harness />);
  const editor = screen.getByRole('textbox', { name: '协作消息' });
  fireEvent.keyDown(editor, { key: 'Enter' });
  fireEvent.change(editor, { target: { value: '接下来要补充的内容' } });
  reject(new Error('offline'));
  expect(await screen.findByText('上一条没有发出，内容已保留。')).toBeInTheDocument();
  expect(editor).toHaveValue('接下来要补充的内容');
  fireEvent.click(screen.getByRole('button', { name: '找回未发送内容' }));
  expect(editor).toHaveValue('第一条\n\n接下来要补充的内容');
  expect(onSend).toHaveBeenCalledTimes(1);
});

it('moves the one JEV draft into a dialog and restores it and focus without sending', async () => {
  const onSend = vi.fn();
  function Harness() {
    const [draft, setDraft] = useState('补充任务');
    return <TooltipProvider><RoomComposer expandInDialog room={{ id: 'room-editor-dialog', status: 'active', participants: [] }} personas={[]} draft={draft} attachments={[]} sending={false} taskBusyState="running" busySubmitBehavior="queue" onDraftChange={setDraft} onAttachmentsChange={vi.fn()} onPasteImages={vi.fn()} onPasteFromClipboard={vi.fn()} onPickAttachments={vi.fn()} onSend={onSend} /></TooltipProvider>;
  }
  const user = userEvent.setup();
  render(<Harness />);
  await user.click(screen.getByRole('button', { name: '展开长文本编辑' }));
  expect(screen.getByRole('dialog', { name: '编辑任务消息' })).toBeVisible();
  expect(screen.getAllByRole('textbox', { name: '协作消息' })).toHaveLength(1);
  const editor = screen.getByRole('textbox', { name: '协作消息' });
  expect(editor).toHaveFocus();
  fireEvent.change(editor, { target: { value: '补充任务\n保留这份草稿' } });
  await user.click(screen.getByRole('button', { name: '关闭' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveValue('补充任务\n保留这份草稿');
  expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveFocus();
  expect(onSend).not.toHaveBeenCalled();
});

it('keeps one add menu, stop and expanded editing and stages file drops during execution', async () => {
  const onStop = vi.fn(); const onInvite = vi.fn(); const onFiles = vi.fn();
  render(<TooltipProvider><RoomComposer room={{ id: 'room-controls', status: 'active', participants: [] }} personas={[]} draft="长文本" attachments={[]} sending={false} taskBusyState="running" onStop={onStop} onInvitePartners={onInvite} onDraftChange={vi.fn()} onAttachmentsChange={vi.fn()} onPasteImages={onFiles} onPasteFromClipboard={vi.fn()} onPickAttachments={vi.fn()} onSend={vi.fn()} /></TooltipProvider>);
  fireEvent.click(screen.getByRole('button', { name: '停止当前协作' }));
  expect(screen.queryByRole('button', { name: '邀请新伙伴' })).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '添加内容' }));
  expect(screen.getByRole('menuitem', { name: /选择附件/ })).not.toHaveAttribute('aria-disabled', 'true');
  await user.click(screen.getByRole('menuitem', { name: '邀请新伙伴' }));
  expect(onStop).toHaveBeenCalledTimes(1); expect(onInvite).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' }));
  expect(screen.getByRole('button', { name: '收起长文本编辑' })).toHaveAttribute('aria-expanded', 'true');
  const editor = screen.getByRole('textbox', { name: '协作消息' });
  fireEvent.keyDown(editor, { key: 'Escape' });
  expect(screen.getByRole('button', { name: '展开长文本编辑' })).toHaveAttribute('aria-expanded', 'false');
  fireEvent.drop(editor, { dataTransfer: { types: ['Files'], files: [new File(['a'], 'test.txt')] } });
  expect(onFiles).toHaveBeenCalledTimes(1);
  fireEvent.paste(editor, { clipboardData: { files: [new File(['pdf'], 'reference.pdf', { type: 'application/pdf' })] } });
  expect(onFiles).toHaveBeenCalledTimes(2);
  expect(editor).toHaveValue('长文本');
});

it('keeps the mention menu open across the controlled host draft echo', () => {
  function Harness() {
    const [draft, setDraft] = useState('请核对');
    return <TooltipProvider><RoomComposer room={{ id: 'room-mentioned', status: 'active', participants: [{ id: 'earth', sessionId: 'earth-session', roleId: 'worker', roleVersion: '1', displayName: 'Earth', ordinal: 0, status: 'active' }] }} personas={[]} draft={draft} attachments={[]} sending={false} onDraftChange={setDraft} onAttachmentsChange={vi.fn()} onPasteImages={vi.fn()} onPasteFromClipboard={vi.fn()} onPickAttachments={vi.fn()} onSend={vi.fn()} /></TooltipProvider>;
  }
  render(<Harness />);
  fireEvent.change(screen.getByRole('textbox', { name: '协作消息' }), { target: { value: '请核对 @' } });
  expect(screen.getByRole('listbox', { name: '选择要点名的伙伴' })).toBeVisible();
  fireEvent.click(screen.getByRole('option', { name: /Earth/ }));
  expect(screen.getByRole('textbox', { name: '协作消息' })).toHaveValue('请核对 @Earth ');
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
});

it('places mentions outside the composer scrollport while keeping editor selection and dismissal', async () => {
  const onSend = vi.fn();
  function Harness() {
    const [draft, setDraft] = useState('');
    return <TooltipProvider><div data-testid="composer-scrollport" style={{ maxHeight: 120, overflowY: 'auto' }}><RoomComposer
      room={{ id: 'room-scrolling', status: 'active', participants: [
        { id: 'earth', sessionId: 'earth-session', roleId: 'worker', roleVersion: '1', displayName: 'Earth', ordinal: 0, status: 'active' },
        { id: 'mars', sessionId: 'mars-session', roleId: 'worker', roleVersion: '1', displayName: 'Mars', ordinal: 1, status: 'active' },
      ] }}
      participantAliases={{ earth: 'Earth', mars: 'Mars' }} personas={[]} draft={draft} attachments={[]} sending={false}
      onDraftChange={setDraft} onAttachmentsChange={vi.fn()} onPasteImages={vi.fn()} onPasteFromClipboard={vi.fn()} onPickAttachments={vi.fn()} onSend={onSend}
    /></div><button type="button">Outside composer</button></TooltipProvider>;
  }
  render(<Harness />);
  const user = userEvent.setup();
  const editor = screen.getByRole('textbox', { name: '协作消息' });
  await user.type(editor, '@');
  const menu = screen.getByRole('listbox', { name: '选择要点名的伙伴' });
  expect(screen.getByTestId('composer-scrollport')).not.toContainElement(menu);
  expect(editor).toHaveFocus();
  expect(editor).toHaveAttribute('aria-controls', menu.id);
  await user.keyboard('{ArrowDown}');
  expect(screen.getByRole('option', { name: /Mars/ })).toHaveAttribute('aria-selected', 'true');
  await user.keyboard('{Enter}');
  expect(editor).toHaveValue('@Mars ');
  expect(editor).toHaveFocus();
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(onSend).not.toHaveBeenCalled();

  await user.clear(editor);
  await user.type(editor, '@');
  await user.click(screen.getByRole('option', { name: /Earth/ }));
  expect(editor).toHaveValue('@Earth ');
  expect(editor).toHaveFocus();
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();

  await user.clear(editor);
  await user.type(editor, '@');
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(editor).toHaveFocus();
  await user.type(editor, 'E');
  expect(screen.getByRole('listbox')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Outside composer' }));
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Outside composer' })).toHaveFocus();
  expect(onSend).not.toHaveBeenCalled();

  await user.click(screen.getByRole('button', { name: '添加内容' }));
  await user.click(screen.getByRole('menuitem', { name: /点名一位伙伴/ }));
  expect(screen.getByRole('listbox')).toBeInTheDocument();
  expect(editor).toHaveFocus();
  await user.keyboard('{Tab}');
  expect(editor).toHaveValue('@Earth ');
  expect(editor).toHaveFocus();
  expect(screen.queryByRole('listbox')).not.toBeInTheDocument();
  expect(onSend).not.toHaveBeenCalled();
});
