import {
  Check,
  FolderOpen,
  LockKeyhole,
  ShieldCheck,
  TriangleAlert,
} from 'lucide-react';
import * as RadioGroup from '@radix-ui/react-radio-group';
import { useEffect, useState } from 'react';

import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from '@/components/primitives';
import type { AgentPersonaV1 } from '@/contracts/generated/agent-persona.v1';
import { PermissionMark } from '../marks/ConversationMarks';
import type {
  AgentPermissionSelection,
  SessionSummary,
  ToolManifest,
} from '../types';
import {
  PERMISSION_PRESETS,
  permissionPreset,
  unrestrictedWorkspaceRoots,
} from './permission-policy';
import { toolAvailableForPolicy } from './tool-policy';
import { isTeamDeployment } from '@/features/team/deployment';

export function PermissionPicker({
  session,
  metadataKnown = Boolean(session),
  persona,
  tools,
  disabled,
  requestOpen,
  onChange,
  onWorkspaceRootsChange,
}: {
  session?: SessionSummary;
  metadataKnown?: boolean;
  persona?: AgentPersonaV1;
  tools: ToolManifest[];
  disabled: boolean;
  requestOpen: number;
  onChange: (selection: AgentPermissionSelection) => void;
  onWorkspaceRootsChange: () => void;
}) {
  const teamMode = isTeamDeployment();
  const [open, setOpen] = useState(false);
  const [dangerousOpen, setDangerousOpen] = useState(false);
  const profile = session?.toolProfileVersion ?? 'control-center-v1';
  const current = permissionPreset(session?.executionMode, profile);
  // Session ownership decides whether a user conversation can enter the
  // coordinator profiles. Persona is optional metadata and never grants or
  // withholds execution permissions.
  const canCoordinate = Boolean(session && !session.roomParticipant);
  const scopedWorkspaceRoots = (session?.workspaceRoots ?? []).filter((root) => root !== '/');

  useEffect(() => {
    if (requestOpen > 0 && session && metadataKnown && !disabled) setOpen(true);
  }, [disabled, metadataKnown, requestOpen, session]);

  useEffect(() => {
    if (!disabled && metadataKnown) return;
    setOpen(false);
    setDangerousOpen(false);
  }, [disabled, metadataKnown]);

  return (
    <>
      {teamMode ? (
        <Button
          aria-label="对话权限：团队工作区托管"
          className="agent-composer__picker"
          data-permission="workspace-managed"
          disabled
          leadingIcon={<PermissionMark mode="workspace_managed" size={16} />}
          size="small"
          title="团队工作区和执行权限由服务管理。"
          variant="quiet"
        >
          <span className="agent-composer__picker-text">团队工作区托管</span>
        </Button>
      ) : (
        <Popover open={open} onOpenChange={setOpen}>
        <PopoverTrigger asChild>
          <Button
            aria-label={metadataKnown ? `对话权限：${current.label}` : '对话权限：尚未同步'}
            className="agent-composer__picker"
            data-permission={metadataKnown ? current.id : 'unknown'}
            size="small"
            title={!metadataKnown ? '会话权限信息尚未返回，载入后可调整。消息仍可编辑。' : disabled
              ? '请先结束或停止当前任务，再调整运行权限。'
              : `对话权限：${current.label}`}
            variant="quiet"
            disabled={!session || !metadataKnown || disabled}
            leadingIcon={metadataKnown ? <PermissionMark mode={current.executionMode} size={16} /> : <LockKeyhole size={16} />}
          >
            <span className="agent-composer__picker-text">{metadataKnown ? current.label : '权限待同步'}</span>
          </Button>
        </PopoverTrigger>
        <PopoverContent align="start" className="agent-picker-popover">
          <header>
            <LockKeyhole size={16} />
            <span><strong>对话权限</strong><small>模式、工具范围与审批共同生效</small></span>
          </header>
          <RadioGroup.Root
            className="agent-picker-popover__options"
            aria-label="对话权限模式"
            value={current.id}
            onValueChange={(presetId) => {
              const preset = PERMISSION_PRESETS.find((item) => item.id === presetId);
              if (!preset) return;
              if (preset.id === 'full-auto') {
                setOpen(false);
                setDangerousOpen(true);
                return;
              }
              if (preset.executionMode === 'workspace_managed' && !scopedWorkspaceRoots.length) {
                setOpen(false);
                onWorkspaceRootsChange();
                return;
              }
              onChange({
                mode: preset.mode,
                toolProfileVersion: preset.toolProfileVersion,
                executionMode: preset.executionMode,
                workspaceRoots: preset.executionMode === 'per_action'
                  ? unrestrictedWorkspaceRoots(...scopedWorkspaceRoots)
                  : scopedWorkspaceRoots,
                workspaceScopeConfirmed: preset.executionMode === 'workspace_managed',
              });
              setOpen(false);
            }}
          >
            {PERMISSION_PRESETS.map((preset) => {
              const selected = current.id === preset.id;
              const available = canCoordinate && (
                preset.executionMode !== 'workspace_managed' || scopedWorkspaceRoots.length > 0
              );
              const toolCount = tools.filter(
                (tool) => toolAvailableForPolicy(
                  tool,
                  'coordinator',
                  preset.toolProfileVersion,
                ),
              ).length;
              return (
                <RadioGroup.Item
                  className="agent-picker-popover__option"
                  data-danger={preset.id === 'full-auto' || undefined}
                  value={preset.id}
                  key={preset.id}
                  disabled={disabled || !available}
                >
                  <PermissionMark mode={preset.executionMode} size={18} />
                  <span>
                    <strong>{preset.label}</strong>
                    <small>
                      {available
                        ? `${preset.description} · ${toolCount} 个工具`
                        : preset.executionMode === 'workspace_managed' && canCoordinate
                          ? '先选择授权目录后可用'
                          : '当前会话未开放协调权限'}
                    </small>
                  </span>
                  {selected ? <Check size={15} /> : null}
                </RadioGroup.Item>
              );
            })}
          </RadioGroup.Root>
          {canCoordinate ? (
            <section className="agent-picker-popover__workspace" aria-label="授权工作区">
              <FolderOpen size={16} />
              <span>
                <strong>授权工作区</strong>
                <small title={scopedWorkspaceRoots.join('\n')}>
                  {scopedWorkspaceRoots.length
                    ? `${scopedWorkspaceRoots.length} 个目录 · ${scopedWorkspaceRoots.map(shortPath).join('、')}`
                    : '尚未授权目录，工作区托管沙箱不可用'}
                </small>
              </span>
              <Button size="small" variant="quiet" disabled={disabled} onClick={onWorkspaceRootsChange}>
                {scopedWorkspaceRoots.length ? '更改目录' : '选择目录'}
              </Button>
            </section>
          ) : null}
          {!PERMISSION_PRESETS.some((preset) => preset.id === current.id) ? (
            <p className="agent-picker-popover__note">
              当前 Session 使用旧版权限策略；选择上方任一模式后会切换到对应的新合同。
            </p>
          ) : null}
          {session?.toolAllowlistMode === 'explicit' ? (
            <p className="agent-picker-popover__note">
              当前会话还受 {session.allowedTools?.length ?? 0} 项自定义工具上限约束；选择新预设后恢复完整工具范围。
            </p>
          ) : null}
        </PopoverContent>
        </Popover>
      )}
      <Dialog
        open={dangerousOpen}
        onOpenChange={setDangerousOpen}
      >
        <DialogContent className="agent-dangerous-permission-dialog">
          <DialogHeader>
            <span className="agent-dangerous-permission-dialog__symbol">
              <PermissionMark mode="full_trust" size={22} />
            </span>
            <DialogTitle>启用全自动模式？</DialogTitle>
            <DialogDescription>
              当前 Session 将拥有整个系统与所有 Tool，并自动批准后续动作。
            </DialogDescription>
          </DialogHeader>
          <div className="agent-dangerous-permission-dialog__limits">
            <p>
              <ShieldCheck size={16} />
              <span><strong>全磁盘与全 Tool</strong> Agent 可访问整个系统与所有 Tool，不受项目目录限制。</span>
            </p>
            <p>
              <TriangleAlert size={16} />
              <span><strong>自动批准每个动作</strong> 不再等待 Tool、路径或审批确认；请只对可信任务启用。</span>
            </p>
            <p>
              <ShieldCheck size={16} />
              <span><strong>仍有 OS 边界</strong> macOS 权限、系统安全策略和底层不可用资源仍可能拒绝操作。</span>
            </p>
          </div>
          <DialogFooter>
            <Button variant="quiet" onClick={() => setDangerousOpen(false)}>
              取消
            </Button>
            <Button
              variant="danger"
              leadingIcon={<TriangleAlert size={15} />}
              onClick={() => {
                onChange({
                  mode: 'coordinator',
                  toolProfileVersion: 'control-center-auto-approve-v1',
                  executionMode: 'full_trust',
                  workspaceRoots: unrestrictedWorkspaceRoots(...(session?.workspaceRoots ?? [])),
                  dangerousModeConfirmed: true,
                });
                setDangerousOpen(false);
              }}
            >
              启用全自动
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function shortPath(path: string): string {
  const parts = path.split('/').filter(Boolean);
  return parts.at(-1) ?? path;
}
