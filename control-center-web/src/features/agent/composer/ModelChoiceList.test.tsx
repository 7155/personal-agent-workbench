import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ModelChoiceList } from './ModelChoiceList';

afterEach(cleanup);

describe('model choices with an automatic default', () => {
  it('keeps automatic selection and provider models usable without showing an empty catalog', () => {
    const choose = vi.fn();
    const model = { key: 'openai/example', providerId: 'openai', providerName: 'OpenAI', modelId: 'example', name: 'Example', detail: '' };
    render(<ModelChoiceList ariaLabel="默认模型" selectedKey="" onChoose={choose}
      leadingOptions={[{ key: '', providerId: '', providerName: '', modelId: '', name: '自动选择', detail: '' }]}
      groups={[{ providerId: 'openai', displayName: 'OpenAI', options: [model] }]} />);
    const automatic = screen.getByRole('option', { name: '选择模型 自动选择' });
    expect(automatic).toHaveAttribute('aria-selected', 'true');
    expect(screen.queryByText('当前没有可用模型')).not.toBeInTheDocument();
    automatic.focus();
    fireEvent.keyDown(automatic, { key: 'ArrowDown' });
    const explicit = screen.getByRole('option', { name: '选择模型 Example' });
    expect(explicit).toHaveFocus();
    fireEvent.click(explicit);
    expect(choose).toHaveBeenCalledWith(model);
  });

  it('reports an empty catalog when it has no selectable rows', () => {
    render(<ModelChoiceList ariaLabel="默认模型" selectedKey="" onChoose={vi.fn()} groups={[]} />);
    expect(screen.getByText('当前没有可用模型')).toBeInTheDocument();
    expect(screen.queryByRole('option')).not.toBeInTheDocument();
  });
});
