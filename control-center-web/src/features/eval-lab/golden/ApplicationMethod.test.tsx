import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApplicationMethodDiff, ApplicationMethodEditor } from './ApplicationMethod';
import { isApplicationMethodComparison, type ApplicationMethodComparison } from './application-method';
import { isRunnableGoldenModel, type ModelConfig } from './types';

afterEach(cleanup);
const model: ModelConfig = { provider: 'openai', model: 'configured-model', thinkingLevel: 'high', prompt: '原规则' };
const comparison: ApplicationMethodComparison = { scope: 'application_skill_body', changed: true, baseline: { kind: 'application_skill', title: '旧阅读方法', sha256: 'a'.repeat(64), source: { kind: 'inline' } }, candidate: { kind: 'application_skill', title: '逐项证据核对', sha256: 'b'.repeat(64), source: { kind: 'project_artifact', projectId: 'project', artifactId: 'method', artifactRevision: 2 } }, diff: '--- baseline\n+++ candidate\n-直接总结\n+先核对研究方法与限制' };
describe('Application Skill methods', () => {
  it('edits only an optional method body and rejects an empty enabled method', () => {
    const changed = vi.fn(); function Editor() { const [value, setValue] = useState(model); return <ApplicationMethodEditor label="候选" value={value} disabled={false} onChange={(next) => { changed(next); setValue(next); }} />; }
    render(<Editor />); fireEvent.click(screen.getByRole('checkbox', { name: '候选使用应用 Skill 方法' }));
    expect(isRunnableGoldenModel(changed.mock.lastCall![0])).toBe(false);
    fireEvent.change(screen.getByRole('textbox', { name: '候选应用方法正文' }), { target: { value: '先阅读完整方法，再提取限制。' } });
    expect(changed.mock.lastCall![0]).toMatchObject({ ...model, applicationMethod: { body: '先阅读完整方法，再提取限制。' } });
    expect(isRunnableGoldenModel(changed.mock.lastCall![0])).toBe(true);
    fireEvent.click(screen.getByRole('checkbox', { name: '候选使用应用 Skill 方法' })); expect(changed.mock.lastCall![0]).toEqual(model);
  });
  it('shows the exact frozen diff and distinguishes it from native Skill routing', () => {
    render(<ApplicationMethodDiff comparison={comparison} />);
    expect(screen.getByText('方法正文有变化')).toBeInTheDocument();
    expect(screen.getByText('+先核对研究方法与限制')).toBeInTheDocument();
    expect(screen.getByText('项目成果 · v2')).toBeInTheDocument();
    expect(screen.getByText(/未评测 Pi Skill 自动路由/)).toBeInTheDocument();
  });
  it('validates frozen method identities instead of accepting title-only metadata', () => {
    expect(isApplicationMethodComparison(comparison)).toBe(true);
    expect(isApplicationMethodComparison({ ...comparison, candidate: { title: 'Skill 已优化' } })).toBe(false);
  });
});
