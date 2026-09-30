import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ModelCatalog } from '../types';
import { ModelPicker } from './ModelPicker';

afterEach(cleanup);

function catalog(): ModelCatalog {
  return {
    schemaVersion: 'rag-ime.agent-model-catalog.v1',
    ok: true,
    thinkingLevel: 'high',
    selected: {
      provider: 'gpt',
      id: 'gpt-5.6-luna',
      modelId: 'gpt-5.6-luna',
      name: 'GPT-5.6 Luna',
    },
    providers: [
      {
        id: 'gpt',
        displayName: 'OpenAI Codex',
        models: [
          {
            provider: 'gpt',
            id: 'gpt-5.6-luna',
            name: 'GPT-5.6 Luna',
            api: 'responses',
            reasoning: true,
            thinkingLevels: ['off', 'medium', 'high', 'max'],
            supportsImages: true,
            contextWindow: 272_000,
            maxTokens: 64_000,
          },
        ],
      },
      {
        id: 'deepseek',
        displayName: 'DeepSeek',
        models: [
          {
            provider: 'deepseek',
            id: 'deepseek-v4-flash',
            name: 'DeepSeek V4 Flash',
            api: 'chat-completions',
            reasoning: true,
            thinkingLevels: ['off', 'low'],
            supportsImages: false,
            contextWindow: 128_000,
            maxTokens: 32_000,
          },
        ],
      },
    ],
  } as unknown as ModelCatalog;
}

function renderPicker(onChange = vi.fn()) {
  render(
    <ModelPicker
      catalog={catalog()}
      disabled={false}
      pending={false}
      requestOpen={0}
      onChange={onChange}
    />,
  );
  return onChange;
}

describe('merged model and reasoning control', () => {
  it('shows ChatGPT plan usage next to the selected SIWC model', () => {
    const value = catalog();
    value.selected = { ...value.selected, provider: 'openai-chatgpt' };
    value.providers[0] = { ...value.providers[0], id: 'openai-chatgpt' };
    render(<ModelPicker catalog={value} disabled={false} pending={false} requestOpen={0} onChange={vi.fn()} />);
    expect(screen.getByText(/正在使用 ChatGPT 套餐/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '管理用量' })).toHaveAttribute('href', 'https://chatgpt.com/settings/usage');
  });
  it('searches models in the merged popover and preserves a legal level when switching models', async () => {
    const onChange = renderPicker();
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · OpenAI Codex · 高',
    });

    await user.click(trigger);
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    expect(within(picker).getByRole('radiogroup', { name: '推理强度' })).toBeInTheDocument();
    expect(within(picker).queryByRole('listbox')).not.toBeInTheDocument();
    await user.click(within(picker).getByRole('button', { name: /更换模型/ }));
    const search = within(picker).getByRole('searchbox', { name: '搜索模型' });
    await waitFor(() => expect(search).toHaveFocus());
    await user.type(search, 'flash');
    expect(within(picker).queryByRole('option', { name: '选择模型 GPT-5.6 Luna' }))
      .not.toBeInTheDocument();
    const flash = within(picker).getByRole('option', { name: '选择模型 DeepSeek V4 Flash' });
    await user.keyboard('{ArrowDown}');
    expect(flash).toHaveFocus();
    await user.keyboard('{Enter}');

    expect(onChange).toHaveBeenCalledWith('deepseek', 'deepseek-v4-flash', 'off');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());
  });

  it('changes only the current model reasoning level and returns focus on Escape', async () => {
    const onChange = renderPicker();
    const user = userEvent.setup();
    const trigger = screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · OpenAI Codex · 高',
    });

    await user.click(trigger);
    const picker = screen.getByRole('dialog', { name: '选择模型与推理强度' });
    await waitFor(() => expect(within(picker).getByRole('radio', { name: '高' })).toHaveFocus());
    await user.keyboard('{ArrowRight}{Enter}');
    expect(onChange).toHaveBeenCalledWith('gpt', 'gpt-5.6-luna', 'max');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());

    await user.click(trigger);
    expect(screen.getByRole('dialog', { name: '选择模型与推理强度' })).toBeInTheDocument();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it('exposes one merged control whose accessible name carries both facts', () => {
    renderPicker();
    const trigger = screen.getByRole('button', {
      name: '模型与推理：GPT-5.6 Luna · OpenAI Codex · 高',
    });
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(trigger).toHaveClass('agent-composer__picker');
    expect(trigger).toHaveTextContent('GPT-5.6 Luna');
    expect(trigger).toHaveTextContent('高');
  });

  it('opens the merged popover on the requested section for each picker command', async () => {
    const { rerender } = render(
      <ModelPicker
        catalog={catalog()}
        disabled={false}
        pending={false}
        requestOpen={0}
        thinkingRequestOpen={0}
        onChange={vi.fn()}
      />,
    );

    rerender(
      <ModelPicker
        catalog={catalog()}
        disabled={false}
        pending={false}
        requestOpen={0}
        thinkingRequestOpen={1}
        onChange={vi.fn()}
      />,
    );

    let picker = await screen.findByRole('dialog', { name: '选择模型与推理强度' });
    await waitFor(() => expect(within(picker).getByRole('radio', { name: '高' }))
      .toHaveFocus());
    expect(within(picker).queryByRole('searchbox', { name: '搜索模型' })).not.toBeInTheDocument();

    await userEvent.setup().keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: '选择模型与推理强度' }))
      .not.toBeInTheDocument());

    rerender(
      <ModelPicker
        catalog={catalog()}
        disabled={false}
        pending={false}
        requestOpen={1}
        thinkingRequestOpen={1}
        onChange={vi.fn()}
      />,
    );

    picker = await screen.findByRole('dialog', { name: '选择模型与推理强度' });
    await waitFor(() => expect(within(picker).getByRole('searchbox', { name: '搜索模型' }))
      .toHaveFocus());
  });
});
