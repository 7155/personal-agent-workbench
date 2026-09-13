import { AtSign, Paperclip, Plus, UsersRound } from 'lucide-react';
import { useRef } from 'react';
import { IconButton, Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from '@/components/primitives';

/** Secondary actions share one entry; paste and typed @ remain direct inputs. */
export function ComposerAddMenu({ canAttach, disabled, onPickAttachments, onMention, onInvite }: {
  canAttach: boolean;
  disabled?: boolean;
  onPickAttachments: () => void;
  onMention?: () => void;
  onInvite?: () => void;
}) {
  const moveFocus = useRef(false);
  return <Menu>
    <MenuTrigger asChild><IconButton className="composer-add-menu__trigger" label="添加内容" icon={<Plus size={18} />} disabled={disabled} tooltip /></MenuTrigger>
    <MenuContent className="composer-add-menu" side="top" align="start" onCloseAutoFocus={(event) => {
      // Mention intentionally moves focus back into the text editor.
      if (moveFocus.current) event.preventDefault();
      moveFocus.current = false;
    }}>
      <MenuItem disabled={!canAttach} onSelect={onPickAttachments}><Paperclip size={16} /><span>选择附件<small>也可直接粘贴或拖入</small></span></MenuItem>
      {onMention || onInvite ? <MenuSeparator /> : null}
      {onMention ? <MenuItem onSelect={() => { moveFocus.current = true; onMention(); }}><AtSign size={16} /><span>点名一位伙伴<small>输入 @ 也可以点名</small></span></MenuItem> : null}
      {onInvite ? <MenuItem onSelect={onInvite}><UsersRound size={16} /><span>邀请新伙伴</span></MenuItem> : null}
    </MenuContent>
  </Menu>;
}
