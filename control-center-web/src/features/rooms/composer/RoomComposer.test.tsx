import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { TooltipProvider } from '@/components/primitives';
import { RoomComposer, roomMentionedParticipants } from './RoomComposer';

afterEach(cleanup);

describe('RoomComposer macOS input methods', () => {
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

it('keeps one add menu, stop and expanded editing and blocks file drops during execution', async () => {
  const onStop = vi.fn(); const onInvite = vi.fn(); const onFiles = vi.fn();
  render(<TooltipProvider><RoomComposer room={{ id: 'room-controls', status: 'active', participants: [] }} personas={[]} draft="长文本" attachments={[]} sending={false} taskBusyState="running" onStop={onStop} onInvitePartners={onInvite} onDraftChange={vi.fn()} onAttachmentsChange={vi.fn()} onPasteImages={onFiles} onPasteFromClipboard={vi.fn()} onPickAttachments={vi.fn()} onSend={vi.fn()} /></TooltipProvider>);
  fireEvent.click(screen.getByRole('button', { name: '停止当前协作' }));
  expect(screen.queryByRole('button', { name: '邀请新伙伴' })).not.toBeInTheDocument();
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: '添加内容' }));
  expect(screen.getByRole('menuitem', { name: /选择附件/ })).toHaveAttribute('aria-disabled', 'true');
  await user.click(screen.getByRole('menuitem', { name: '邀请新伙伴' }));
  expect(onStop).toHaveBeenCalledTimes(1); expect(onInvite).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: '展开长文本编辑' }));
  expect(screen.getByRole('button', { name: '收起长文本编辑' })).toHaveAttribute('aria-expanded', 'true');
  const editor = screen.getByRole('textbox', { name: '协作消息' });
  fireEvent.keyDown(editor, { key: 'Escape' });
  expect(screen.getByRole('button', { name: '展开长文本编辑' })).toHaveAttribute('aria-expanded', 'false');
  fireEvent.drop(editor, { dataTransfer: { types: ['Files'], files: [new File(['a'], 'test.txt')] } });
  expect(onFiles).not.toHaveBeenCalled();
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
