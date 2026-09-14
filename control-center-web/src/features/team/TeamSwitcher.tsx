import { ChevronDown, FolderKanban, Link2, LogOut, Plus, Settings2, UserRound } from 'lucide-react';
import { useEffect, useState } from 'react';
import {
  Menu,
  MenuContent,
  MenuItem,
  MenuLabel,
  MenuSeparator,
  MenuTrigger,
} from '@/components/primitives';
import { useOptionalTeam } from './team-context';
import { TeamManagementDialog } from './TeamManagementDialog';
import { TeamConnectionsDialog } from './TeamConnectionsDialog';
import { TeamProjectDialog } from './TeamProjectDialog';
import type { TeamSpace } from './types';

export function TeamSwitcher() {
  const team = useOptionalTeam();
  const [projectDialogOpen, setProjectDialogOpen] = useState(false);
  const [managementDialogOpen, setManagementDialogOpen] = useState(false);
  const [connectionsDialogOpen, setConnectionsDialogOpen] = useState(false);

  useEffect(() => {
    if (!team?.user || team.phase !== 'authenticated' || typeof window === 'undefined') return;
    const result = new URLSearchParams(window.location.search).get('teamConnection');
    if (result === 'connected' || result === 'failed') setConnectionsDialogOpen(true);
  }, [team?.phase, team?.user?.id]);

  if (!team?.user || !team.activeSpace || team.phase !== 'authenticated') return null;

  const personalSpaces = team.spaces.filter((space) => space.kind === 'personal');
  const projectSpaces = team.spaces.filter((space) => space.kind === 'project');
  const canManageCurrentProject = team.activeSpace.role === 'owner' || team.activeSpace.role === 'maintainer';
  const canManageMembers = team.user.role === 'admin' || canManageCurrentProject;
  const canViewProjectManagement = team.activeSpace.kind === 'project';
  const userLabel = team.user.displayName || team.user.username;

  return (
    <>
      <Menu>
        <MenuTrigger asChild>
          <button
            aria-label={`账户与工作空间：${userLabel}，${team.activeSpace.name}`}
            className="paw-team-switcher"
            data-testid="team-switcher"
            type="button"
          >
            <UserRound aria-hidden="true" size={13} />
            <span className="paw-team-switcher__space">{team.activeSpace.name}</span>
            <ChevronDown aria-hidden="true" size={12} />
          </button>
        </MenuTrigger>
        <MenuContent align="end" className="paw-team-menu">
          <MenuLabel className="paw-team-menu__account">
            <span>{userLabel}</span>
            <small>{team.user.username} · {team.user.role === 'admin' ? '管理员' : '成员'}</small>
          </MenuLabel>
          <MenuSeparator />
          {personalSpaces.length > 0 ? <MenuLabel>个人空间</MenuLabel> : null}
          {personalSpaces.map((space) => <SpaceMenuItem key={space.id} onSelect={() => team.selectSpace(space.id)} space={space} selected={space.id === team.activeSpace?.id} />)}
          {projectSpaces.length > 0 ? <MenuLabel>项目空间</MenuLabel> : null}
          {projectSpaces.map((space) => <SpaceMenuItem key={space.id} onSelect={() => team.selectSpace(space.id)} space={space} selected={space.id === team.activeSpace?.id} />)}
          <MenuSeparator />
          <MenuItem onSelect={() => setProjectDialogOpen(true)}><Plus aria-hidden="true" size={14} />新建项目</MenuItem>
          <MenuItem onSelect={() => setConnectionsDialogOpen(true)}><Link2 aria-hidden="true" size={14} />连接 GitHub</MenuItem>
          {canViewProjectManagement || canManageMembers ? <MenuItem onSelect={() => setManagementDialogOpen(true)}><Settings2 aria-hidden="true" size={14} />{canManageMembers ? '项目与成员管理' : '查看项目交付'}</MenuItem> : null}
          <MenuSeparator />
          <MenuItem data-danger onSelect={() => void team.logout()}><LogOut aria-hidden="true" size={14} />退出登录</MenuItem>
        </MenuContent>
      </Menu>
      <TeamProjectDialog onOpenChange={setProjectDialogOpen} open={projectDialogOpen} />
      <TeamManagementDialog onOpenChange={setManagementDialogOpen} open={managementDialogOpen} />
      <TeamConnectionsDialog onOpenChange={setConnectionsDialogOpen} open={connectionsDialogOpen} />
    </>
  );
}

function SpaceMenuItem({ onSelect, selected, space }: { onSelect(): void; selected: boolean; space: TeamSpace }) {
  return (
    <MenuItem aria-current={selected ? 'true' : undefined} onSelect={onSelect}>
      <FolderKanban aria-hidden="true" size={14} />
      <span className="paw-team-menu__space-copy"><strong>{space.name}</strong><small>{space.role === 'owner' ? '所有者' : space.role === 'maintainer' ? '维护者' : space.role === 'contributor' ? '贡献者' : '查看者'}</small></span>
      {selected ? <span aria-label="当前空间" className="paw-team-menu__selected">当前</span> : null}
    </MenuItem>
  );
}
