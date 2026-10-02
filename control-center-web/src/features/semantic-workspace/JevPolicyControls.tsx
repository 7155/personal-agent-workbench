import { ChevronDown, Workflow } from 'lucide-react';
import { Menu, MenuContent, MenuLabel, MenuRadioGroup, MenuRadioItem, MenuSeparator, MenuTrigger } from '@/components/primitives';
import type { JevModelRouting, JevToolApproval, JevVerificationMode } from './jev-execution';

export function JevPolicyControls({ modelRouting, toolApprovalMode, verificationMode, onModelRouting, onToolApprovalMode, onVerificationMode, disabled = false }: {
  modelRouting: JevModelRouting; toolApprovalMode: JevToolApproval; verificationMode: JevVerificationMode;
  onModelRouting: (value: JevModelRouting) => void; onToolApprovalMode: (value: JevToolApproval) => void; onVerificationMode: (value: JevVerificationMode) => void; disabled?: boolean;
}) {
  return <Menu modal={false}><MenuTrigger asChild><button className="an-chip jev-policy-control" type="button" disabled={disabled} aria-label="Jev 模型与工具设置"><Workflow size={14} aria-hidden /><span>{modelRouting === 'balanced' ? '自动分配模型' : '沿用伙伴模型'}</span><ChevronDown size={13} aria-hidden /></button></MenuTrigger>
    <MenuContent align="start" aria-label="Jev 模型与工具设置">
      <MenuLabel>下一次任务的模型</MenuLabel><MenuRadioGroup value={modelRouting} onValueChange={value => onModelRouting(value as JevModelRouting)}><MenuRadioItem value="balanced">Sol 6.1 · medium / xhigh · 简单任务 Luna</MenuRadioItem><MenuRadioItem value="participant">沿用各伙伴的模型配置</MenuRadioItem></MenuRadioGroup>
      <MenuSeparator /><MenuLabel>工具执行</MenuLabel><MenuRadioGroup value={toolApprovalMode} onValueChange={value => onToolApprovalMode(value as JevToolApproval)}><MenuRadioItem value="dispatch">按任务授权执行，无需逐项批准</MenuRadioItem><MenuRadioItem value="jev_dangerous">危险操作由 Jev 自动审批</MenuRadioItem></MenuRadioGroup>
      <MenuSeparator /><MenuLabel>下一次任务的结果复核</MenuLabel><MenuRadioGroup value={verificationMode} onValueChange={value => onVerificationMode(value as JevVerificationMode)}><MenuRadioItem value="auto">由 Jev 按任务与证据决定</MenuRadioItem><MenuRadioItem value="independent">始终由其他伙伴复核</MenuRadioItem></MenuRadioGroup>
      <MenuLabel>自动模式会直接接受证据，或按需要启动 Pi 复核</MenuLabel>
    </MenuContent>
  </Menu>;
}
