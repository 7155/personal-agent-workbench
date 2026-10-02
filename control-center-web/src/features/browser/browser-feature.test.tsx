import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import { ControlTransportProvider } from '@/app/control-transport';
import { createPreviewTransport } from '@/app/preview-control-transport';
import { PawBrowserApp } from '@/paw-os/apps/PawBrowserApp';

afterEach(() => cleanup());

describe('PawBrowserApp', () => {
  it('is the direct PAW Browser rather than an extension setup surface', async () => {
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' })).toBeInTheDocument());
    expect(screen.queryByText('Agent 拥有完整控制权')).not.toBeInTheDocument();
    expect(screen.queryByText(/隔离 Profile/)).not.toBeInTheDocument();
    expect(screen.queryByText(/配对码|加载浏览器助手|允许站点/)).not.toBeInTheDocument();
    expect(screen.queryByText('Ego 轨迹')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: '显示 Agent 浏览器轨迹' })).toBeInTheDocument();
    expect(screen.queryByRole('complementary', { name: 'Agent 浏览器轨迹' })).not.toBeInTheDocument();
  });

  it('keeps the page dominant with no Agent execution chrome while nothing is running', async () => {
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' })).toBeInTheDocument());
    // The only Agent trace the preview transport reports is already completed,
    // so the execution field, its glow, and the task capsule stay away.
    expect(screen.queryByRole('region', { name: 'Agent 浏览器任务' })).not.toBeInTheDocument();
    expect(screen.queryByText('Agent 刚刚完成')).not.toBeInTheDocument();
    expect(document.querySelector('.paw-browser-agent-field')).toBeNull();
    expect(document.querySelector('.paw-browser-viewport')).not.toHaveAttribute('data-agent-state');
    expect(document.querySelector('.paw-browser-blank-page[data-live]')).toBeNull();
  });

  it('gives the on-demand trace panel its own column even with no Agent running', async () => {
    const user = userEvent.setup();
    render(
      <ControlTransportProvider transport={createPreviewTransport()}>
        <PawBrowserApp />
      </ControlTransportProvider>,
    );

    await waitFor(() => expect(screen.getByRole('textbox', { name: '页面地址' })).toBeInTheDocument());
    expect(document.querySelector('.paw-browser-workspace')).not.toHaveAttribute('data-show-agent');

    await user.click(screen.getByRole('button', { name: '显示 Agent 浏览器轨迹' }));
    const trace = await screen.findByRole('complementary', { name: 'Agent 浏览器轨迹' });
    expect(trace).toBeInTheDocument();
    expect(document.querySelector('.paw-browser-workspace')).toHaveAttribute('data-show-agent', 'true');
    expect(trace).toHaveTextContent('人和 Agent 共用当前页面');

    const toolbar = document.querySelector('.paw-toolbar-actions') as HTMLElement;
    await user.click(within(toolbar).getByRole('button', { name: '隐藏 Agent 浏览器轨迹' }));
    expect(screen.queryByRole('complementary', { name: 'Agent 浏览器轨迹' })).not.toBeInTheDocument();
    expect(document.querySelector('.paw-browser-workspace')).not.toHaveAttribute('data-show-agent');
  });
});
