import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { CodeContentBlock } from '../CodeDiffRenderers';
import { RichDiagram } from './RichDiagram';
import { RichMediaPlayer } from './RichMediaPlayer';

vi.mock('../../file-preview/syntax-highlighter', () => ({ highlightCode: async () => '' }));
afterEach(cleanup);

describe('rich rendering boundaries', () => {
  it('keeps unfinished JSON as source and only enables structure after completion', async () => {
    const view = render(<CodeContentBlock language="json" code={'{"ok":'} streamingTail />);
    expect(screen.getByRole('tab', { name: '结构' })).toBeDisabled();
    expect(screen.queryByRole('region', { name: 'JSON 结构' })).not.toBeInTheDocument();
    view.rerender(<CodeContentBlock language="json" code={'{"ok":true}'} />);
    fireEvent.click(screen.getByRole('tab', { name: '结构' }));
    expect(screen.getByRole('region', { name: 'JSON 结构' })).toHaveTextContent('true');
    view.rerender(<CodeContentBlock language="json" code={'{"broken"'} />);
    expect(screen.getByText(/JSON 尚未闭合或语法无效/)).toHaveAttribute('role', 'status');
  });
  it('sanitizes static SVG and keeps it in an opaque scriptless frame', async () => {
    render(<RichDiagram svg source={'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"><script>alert(1)</script><foreignObject>bad</foreignObject><image href="https://invalid.example/a.png"/><rect width="20" height="20"/></svg>'} />);
    const frame = await screen.findByTitle('静态 SVG 图示');
    expect(frame).toHaveAttribute('sandbox', '');
    const document = frame.getAttribute('srcdoc')!;
    expect(document).not.toMatch(/onload|<script|foreignObject|invalid\.example/);
    expect(document).toContain("default-src 'none'");
    expect(document).toContain('<rect');
  });
  it('shows genuine media events and recoverable failures rather than simulated playback', async () => {
    const { container } = render(<RichMediaPlayer source="/api/agent/media/test/content" name="sample.wav" />);
    const audio = container.querySelector('audio')!;
    expect(audio).not.toHaveAttribute('autoplay');
    fireEvent.play(audio);
    expect(screen.getByText(/正在播放/)).toBeVisible();
    fireEvent.pause(audio);
    expect(screen.getByText(/由你控制播放/)).toBeVisible();
    fireEvent.error(audio);
    expect(screen.getByRole('status')).toHaveTextContent('暂时不能');
    fireEvent.click(screen.getByRole('button', { name: '重试播放' }));
    await waitFor(() => expect(container.querySelector('audio')).not.toBeNull());
  });
});
