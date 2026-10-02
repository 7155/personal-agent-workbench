import { describe, expect, it } from 'vitest';

import agentCss from '../agent.css?raw';
import workspaceCss from '../../../design/workspace.css?raw';
import agentFxCss from '../../../paw-os/styles/paw-os-agent-fx.css?raw';
import agentMigratedCss from '../../../paw-os/styles/paw-os-agent.css?raw';
import webModelCss from '../../../paw-os/styles/paw-os-webmodel-v1.css?raw';
import marksCss from './conversation-marks.css?raw';
import composerShellSource from '../../composer/ComposerShell.tsx?raw';
import modelPickerSource from '../composer/ModelPicker.tsx?raw';
import permissionPickerSource from '../composer/PermissionPicker.tsx?raw';
import toolPickerSource from '../composer/ToolPicker.tsx?raw';

/**
 * The conversation chrome has to shed words, not controls, when a PAWOS window
 * is dragged narrow. These checks pin the two halves of that contract: the
 * container the collapse measures, and the mark that survives the collapse.
 */
describe('conversation logo-first compact chrome', () => {
  it('measures the composer toolbar itself instead of the viewport', () => {
    expect(workspaceCss).toMatch(
      /\.agent-composer__toolbar\s*\{[^}]*container:\s*paw-composer-toolbar \/ inline-size;/s,
    );
  });

  it('sheds the secondary detail before the label and the label before the control', () => {
    const detailStep = agentCss.slice(
      agentCss.indexOf('@container paw-composer-toolbar (max-width: 620px)'),
      agentCss.indexOf('@container paw-composer-toolbar (max-width: 460px)'),
    );
    expect(detailStep).toMatch(/\.agent-composer__picker-detail\s*\{\s*display:\s*none;/s);
    expect(detailStep).toMatch(/\.agent-composer__picker\s*\{[^}]*max-width:\s*136px;/s);

    const iconOnlyStep = agentCss.slice(
      agentCss.indexOf('@container paw-composer-toolbar (max-width: 460px)'),
    );
    expect(iconOnlyStep).toMatch(/\.agent-composer__picker\s*\{[^}]*flex:\s*0 0 32px;[^}]*padding-inline:\s*0;/s);
    // Collapsed means visually hidden, never removed: the label still answers
    // a screen reader and the trigger keeps its full title.
    expect(iconOnlyStep).toMatch(
      /\.agent-composer__picker \.ui-button__label\s*\{[^}]*clip-path:\s*inset\(50%\);/s,
    );
    expect(iconOnlyStep).not.toMatch(
      /\.agent-composer__picker \.ui-button__label\s*\{[^}]*display:\s*none;/s,
    );
  });

  it('keeps exactly one owner for the picker collapse', () => {
    // A viewport copy in either stylesheet would fight the container ladder
    // the moment a narrow window sat on a wide display.
    expect(agentCss).not.toMatch(
      /@media \([^)]*width[^)]*\)\s*\{[^@]*?\.agent-composer__picker \.ui-button__label/s,
    );
    expect(agentMigratedCss).not.toContain('.agent-composer__picker .ui-button__label');
    expect(agentMigratedCss).not.toMatch(/\.agent-composer__picker\s*\{[^}]*flex:\s*0 0 32px;/s);
  });

  it('gives each mark family its own identity policy', () => {
    expect(marksCss).toMatch(/\.paw-mark\[data-mark\^='provider-'\]\s*\{[^}]*color:\s*var\(--paw-mark-ink/s);
    // Permission and capability marks inherit the control tone, so a danger or
    // disabled control is never contradicted by a saturated identity hue.
    expect(marksCss).not.toMatch(/\.paw-mark\[data-mark\^='permission-'\]\s*\{[^}]*--paw-mark-ink/s);
    expect(marksCss).toMatch(
      /\.ui-button:disabled \.paw-mark\[data-mark\^='provider-'\],[\s\S]*?\{[^}]*color:\s*inherit;/s,
    );
    for (const kind of ['openai', 'anthropic', 'google', 'deepseek', 'local', 'generic']) {
      expect(marksCss).toContain(`.paw-mark[data-mark='provider-${kind}']`);
    }
  });

  it('replaces the generic Lucide leading icons on every conversation picker', () => {
    expect(permissionPickerSource).toContain('<PermissionMark mode={current.executionMode}');
    expect(permissionPickerSource).not.toContain('ShieldCheck size={15}');
    expect(modelPickerSource).toContain('<ProviderMark');
    // One merged trigger names both facts: model label plus the reasoning level.
    expect(modelPickerSource).toContain('className="agent-composer__picker-thinking"');
    expect(modelPickerSource).not.toContain('agent-composer__thinking-picker');
    expect(toolPickerSource).toContain('<CapabilityMark');
    expect(toolPickerSource).not.toContain('Wrench');
    // A pending model switch still needs its own motion, so the loader stays.
    expect(modelPickerSource).toContain('<LoaderCircle className="ui-spin" size={15} />');
  });

  it('keeps narrow controls scrollable and carries the busy gradient around the whole composer frame', () => {
    const narrowStep = agentCss.slice(
      agentCss.indexOf('@container paw-composer-toolbar (max-width: 360px)'),
    );
    expect(narrowStep).toMatch(/\.agent-composer__controls\s*\{[^}]*overflow-x:\s*auto;/s);
    expect(composerShellSource).toContain('className="agent-composer__busy-frame"');
    expect(agentCss).not.toContain('.agent-composer[data-busy]::before');
    expect(agentCss).toMatch(/\.agent-composer__busy-frame\s*\{[^}]*inset:\s*-2px;[^}]*padding:\s*2px;[^}]*mask-composite:\s*exclude;/s);
    expect(agentCss).toMatch(/\.agent-composer__busy-frame\s*>\s*i\s*\{[^}]*linear-gradient[^}]*animation:\s*agent-composer-busy-flow\s+2\.6s\s+linear\s+infinite;/s);
    expect(agentCss).toMatch(/@keyframes agent-composer-busy-flow\s*\{[^}]*transform:\s*translate3d\([^}]*\}[^}]*transform:\s*translate3d\(/s);
    expect(webModelCss).not.toMatch(/\.agent-composer::before[\s\S]{0,120}display:\s*none/);
  });
});
