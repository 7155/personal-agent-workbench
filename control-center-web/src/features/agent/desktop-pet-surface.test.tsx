import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { DesktopPetSurface } from './desktop-pet-surface';

afterEach(() => { cleanup(); delete window.pawDesktopPet; vi.restoreAllMocks(); });
function renderPet() {
  const host = { ready: vi.fn().mockResolvedValue(undefined), hide: vi.fn().mockResolvedValue(undefined), openAssistant: vi.fn().mockResolvedValue(undefined), drag: vi.fn().mockResolvedValue(undefined) };
  window.pawDesktopPet = host;
  render(<DesktopPetSurface />);
  return { host, button: screen.getByRole('button', { name: '打开 PAW 主助手' }) };
}
function pointer(button: HTMLElement, type: string, options = {}) {
  const event = new Event(type, { bubbles: true });
  Object.assign(event, { pointerId: 1, button: 0, isPrimary: true, screenX: 0, screenY: 0, ...options });
  fireEvent(button, event);
}

describe('desktop pet surface', () => {
  it('reuses bundled artwork and opens only on double click or keyboard activation', () => {
    const { host, button } = renderPet();
    expect(host.ready).toHaveBeenCalledTimes(1);
    expect(screen.getByAltText('PAW头像').getAttribute('src')).toBe('/companions/personas/companion-present-v9.webp');
    fireEvent.click(button, { detail: 1 }); expect(host.openAssistant).not.toHaveBeenCalled();
    fireEvent.doubleClick(button); expect(host.openAssistant).toHaveBeenCalledTimes(1);
    fireEvent.click(button, { detail: 0 }); expect(host.openAssistant).toHaveBeenCalledTimes(2);
  });
  it('cleans cancelled drags and does not turn a drag into a double click', () => {
    const { host, button } = renderPet();
    pointer(button, 'pointerdown'); pointer(button, 'pointermove', { screenX: 10 }); pointer(button, 'pointercancel');
    fireEvent.doubleClick(button);
    expect(host.drag.mock.calls).toEqual([['start'], ['move'], ['cancel']]);
    expect(host.openAssistant).not.toHaveBeenCalled();
    pointer(button, 'pointerdown'); pointer(button, 'pointerup');
    expect(host.drag.mock.calls.slice(-2)).toEqual([['start'], ['end']]);
  });
  it('offers explicit hide and escape without invoking the assistant', () => {
    const { host, button } = renderPet();
    fireEvent.click(screen.getByRole('button', { name: '隐藏桌面伙伴' }));
    fireEvent.keyDown(button, { key: 'Escape' });
    expect(host.hide).toHaveBeenCalledTimes(2);
    expect(host.openAssistant).not.toHaveBeenCalled();
  });
  it('bounds pointer movement to one in-flight IPC call', () => {
    const { host, button } = renderPet();
    host.drag.mockImplementation((phase: string) => phase === 'move' ? new Promise(() => {}) : Promise.resolve());
    pointer(button, 'pointerdown');
    for (let x = 5; x < 100; x += 1) pointer(button, 'pointermove', { screenX: x });
    pointer(button, 'pointerup');
    expect(host.drag.mock.calls).toEqual([['start'], ['move'], ['end']]);
  });
  it('reports bridge failure without an unhandled rejection', async () => {
    const { host, button } = renderPet(); host.openAssistant.mockRejectedValueOnce(new Error('unavailable'));
    fireEvent.doubleClick(button);
    await waitFor(() => expect(screen.getByRole('status').textContent).toBe('操作未完成，请重试'));
  });
  it('does not pretend the ordinary browser is a native desktop pet', () => {
    render(<DesktopPetSurface />);
    expect(screen.getByText('请从 PAW 桌面端开启')).toBeTruthy();
    expect(screen.getByRole('button', { name: '打开 PAW 主助手' })).toBeDisabled();
  });
});
