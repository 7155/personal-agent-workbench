import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MessageActions } from './MessageActions';

afterEach(() => { cleanup(); vi.restoreAllMocks(); Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined }); });
function deferred() {
  let resolve!: () => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

describe('message copy feedback', () => {
  it('does not announce success until the clipboard accepts the copy and prevents duplicate writes', async () => {
    const pending = deferred();
    const writeText = vi.fn(() => pending.promise);
    const onCopy = vi.fn();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    render(<MessageActions text="已生成的内容" onCopy={onCopy} />);
    const copy = screen.getByRole('button', { name: '复制' });
    fireEvent.click(copy); fireEvent.click(copy);
    expect(screen.queryByText('已复制')).not.toBeInTheDocument();
    expect(writeText).toHaveBeenCalledTimes(1);
    expect(onCopy).not.toHaveBeenCalled();
    await act(async () => pending.resolve());
    expect(screen.getByRole('button', { name: '已复制' })).toBeInTheDocument();
    expect(onCopy).toHaveBeenCalledTimes(1);
  });

  it('keeps a rejected clipboard action retryable instead of showing a success tick', async () => {
    const writeText = vi.fn().mockRejectedValueOnce(new Error('clipboard denied')).mockResolvedValueOnce(undefined);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const onCopy = vi.fn();
    render(<MessageActions text="真实内容" onCopy={onCopy} />);
    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    await waitFor(() => expect(screen.getByRole('button', { name: '重试复制' })).toBeInTheDocument());
    expect(screen.queryByText('已复制')).not.toBeInTheDocument();
    expect(onCopy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '重试复制' }));
    expect(await screen.findByRole('button', { name: '已复制' })).toBeInTheDocument();
  });

  it('reports unavailable clipboard support without fabricating completion', async () => {
    render(<MessageActions text="手动选择内容" onCopy={() => {}} />);
    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    expect(await screen.findByRole('status')).toHaveTextContent('无法复制，请选择文字后手动复制');
    expect(screen.queryByText('已复制')).not.toBeInTheDocument();
  });

  it('does not label newer text as copied when an older clipboard write settles', async () => {
    const pending = deferred();
    const writeText = vi.fn(() => pending.promise);
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
    const onCopy = vi.fn();
    const view = render(<MessageActions text="旧片段" onCopy={onCopy} />);
    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    view.rerender(<MessageActions text="新片段" onCopy={onCopy} />);
    expect(screen.getByRole('button', { name: '正在复制' })).toBeDisabled();
    await act(async () => pending.resolve());
    expect(screen.getByRole('button', { name: '复制' })).toBeEnabled();
    expect(screen.queryByText('已复制')).not.toBeInTheDocument();
    expect(writeText).toHaveBeenCalledWith('旧片段');
  });

  it('does not publish late feedback after its message unmounts', async () => {
    const pending = deferred();
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: () => pending.promise } });
    const onCopy = vi.fn();
    const view = render(<MessageActions text="原消息" onCopy={onCopy} />);
    fireEvent.click(screen.getByRole('button', { name: '复制' }));
    view.unmount();
    await act(async () => pending.resolve());
    expect(onCopy).not.toHaveBeenCalled();
  });
});
