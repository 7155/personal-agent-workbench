import { fireEvent, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { startControlCenter } from './start-control-center';

afterEach(() => {
  document.body.innerHTML = '';
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('startControlCenter', () => {
  it('mounts the application after its split entry is available', async () => {
    document.body.innerHTML = '<div id="root"><div class="app-boot">正在准备工作台</div></div>';
    const mountControlCenter = vi.fn();

    await startControlCenter({ load: async () => ({ mountControlCenter }) });

    expect(mountControlCenter).toHaveBeenCalledWith(document.getElementById('root'));
  });

  it('replaces a failed entry import with a focused recovery action', async () => {
    document.body.innerHTML = '<div id="root"><div class="app-boot">正在准备工作台</div></div>';
    const reload = vi.fn();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    await startControlCenter({
      load: async () => { throw new Error('chunk request failed'); },
      reload,
    });

    expect(screen.getByRole('heading', { name: '工作台没有打开' })).toBeInTheDocument();
    expect(screen.queryByText('chunk request failed')).not.toBeInTheDocument();
    const retry = screen.getByRole('button', { name: '重新载入' });
    expect(retry).toHaveFocus();

    fireEvent.click(retry);
    expect(reload).toHaveBeenCalledOnce();
  });

  it('offers recovery after an entry import remains pending for 30 seconds', async () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div id="root"><div class="app-boot">正在准备工作台</div></div>';
    const reload = vi.fn();
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const started = startControlCenter({ load: () => new Promise(() => undefined), reload });

    await vi.advanceTimersByTimeAsync(29_999);
    expect(screen.getByText('正在准备工作台')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新载入' })).not.toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(1);
    expect(screen.getByRole('heading', { name: '工作台没有打开' })).toBeInTheDocument();
    const retry = screen.getByRole('button', { name: '重新载入' });
    expect(retry).toHaveFocus();
    expect(reload).not.toHaveBeenCalled();
    await started;

    fireEvent.click(retry);
    expect(reload).toHaveBeenCalledOnce();
  });

  it('keeps recovery visible when the entry import finishes after its deadline', async () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div id="root"><div class="app-boot">正在准备工作台</div></div>';
    const mountControlCenter = vi.fn();
    let finishLoad!: (entry: { mountControlCenter: typeof mountControlCenter }) => void;
    const entry = new Promise<{ mountControlCenter: typeof mountControlCenter }>((resolve) => {
      finishLoad = resolve;
    });
    vi.spyOn(console, 'error').mockImplementation(() => undefined);

    const started = startControlCenter({ load: () => entry, reload: vi.fn() });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(screen.getByRole('button', { name: '重新载入' })).toHaveFocus();
    await started;

    finishLoad({ mountControlCenter });
    await vi.advanceTimersByTimeAsync(0);

    expect(mountControlCenter).not.toHaveBeenCalled();
    expect(screen.getByRole('button', { name: '重新载入' })).toHaveFocus();
  });

  it('keeps a successful mount visible after the entry deadline has passed', async () => {
    vi.useFakeTimers();
    document.body.innerHTML = '<div id="root"><div class="app-boot">正在准备工作台</div></div>';
    const mountControlCenter = vi.fn((root: HTMLElement) => { root.textContent = '工作台已打开'; });
    const errorLog = vi.spyOn(console, 'error').mockImplementation(() => undefined);

    await startControlCenter({ load: async () => ({ mountControlCenter }) });
    await vi.advanceTimersByTimeAsync(30_000);

    expect(screen.getByText('工作台已打开')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重新载入' })).not.toBeInTheDocument();
    expect(errorLog).not.toHaveBeenCalled();
  });
});
