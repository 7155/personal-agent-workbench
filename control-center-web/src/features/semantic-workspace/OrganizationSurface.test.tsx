import { useRef, useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { OrganizationSurface } from './OrganizationSurface';

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

it.each(['button', 'escape', 'cancel'])('releases native modality before returning focus: %s', action => {
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({ width: 800 } as DOMRect);
  const closed = vi.fn();
  Object.defineProperties(HTMLDialogElement.prototype, {
    showModal: { configurable: true, value: function (this: HTMLDialogElement) { this.open = true; } },
    close: { configurable: true, value: function (this: HTMLDialogElement) { this.open = false; } },
  });
  function Harness() {
    const [open, setOpen] = useState(true);
    const button = useRef<HTMLButtonElement>(null);
    return <><button ref={button}>工作空间</button>{open ? <OrganizationSurface onClose={() => {
      closed(document.querySelector('dialog')?.open);
      button.current?.focus();
      setOpen(false);
    }}>{close => <button onClick={close}>关闭工作空间</button>}</OrganizationSurface> : null}</>;
  }
  render(<Harness />);
  const dialog = document.querySelector('dialog')!;
  const close = screen.getByRole('button', { name: '关闭工作空间' });
  close.focus();
  if (action === 'button') fireEvent.click(close);
  else if (action === 'escape') fireEvent.keyDown(close, { key: 'Escape' });
  else fireEvent(dialog, new Event('cancel', { bubbles: false, cancelable: true }));
  expect(closed).toHaveBeenCalledWith(false);
  expect(screen.getByRole('button', { name: '工作空间' })).toHaveFocus();
});
