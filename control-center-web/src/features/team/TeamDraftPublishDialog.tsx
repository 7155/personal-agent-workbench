import { CheckCircle2, GitCommitHorizontal, Upload } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  Input,
} from '@/components/primitives';
import { teamApiErrorMessage } from './team-api';
import { useTeam } from './team-context';
import type { TeamProjectDraft } from './types';

/** Publish the currently open Session as an immutable project snapshot. */
export function TeamDraftPublishDialog({
  open,
  onOpenChange,
  sessionId,
  sessionTitle,
}: {
  open: boolean;
  onOpenChange(open: boolean): void;
  sessionId: string;
  sessionTitle: string;
}) {
  const team = useTeam();
  const projectSpaces = useMemo(
    () => team.spaces.filter((space) => space.kind === 'project'),
    [team.spaces],
  );
  const [spaceId, setSpaceId] = useState('');
  const [title, setTitle] = useState(sessionTitle);
  const [error, setError] = useState<string | null>(null);
  const [draft, setDraft] = useState<TeamProjectDraft | null>(null);
  const preferredSpaceId = team.activeSpace?.kind === 'project' && projectSpaces.some((space) => space.id === team.activeSpace?.id)
    ? team.activeSpace.id
    : projectSpaces[0]?.id ?? '';
  const projectSpaceIds = projectSpaces.map((space) => space.id).join('|');

  useEffect(() => {
    if (!open) {
      setError(null);
      setDraft(null);
      setTitle(sessionTitle.trim() || '共享固定版本');
    }
  }, [open, sessionTitle]);

  useEffect(() => {
    if (!open) return;
    setSpaceId((current) => current && projectSpaces.some((space) => space.id === current) ? current : preferredSpaceId);
    setTitle((current) => current || sessionTitle.trim() || '共享固定版本');
  }, [open, preferredSpaceId, projectSpaceIds, projectSpaces, sessionTitle]);

  async function publish(): Promise<void> {
    const normalizedTitle = title.trim();
    if (!spaceId) {
      setError('当前账号没有可发布的项目空间。');
      return;
    }
    if (!sessionId.trim() || !normalizedTitle) {
      setError('请填写固定版本标题。');
      return;
    }
    setError(null);
    try {
      const created = await team.publishProjectDraft(spaceId, {
        sessionId: sessionId.trim(),
        title: normalizedTitle,
      });
      setDraft(created);
    } catch (reason) {
      setError(teamApiErrorMessage(reason, '发布固定版本失败，请重试。'));
    }
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="team-draft-publish-dialog">
        <DialogHeader>
          <DialogTitle>发布 Session 固定版本</DialogTitle>
          <DialogDescription>服务会暂停当前 Session，读取受管工作区并保存不可变草稿。</DialogDescription>
        </DialogHeader>
        {draft ? (
          <div className="team-draft-publish-dialog__receipt" role="status">
            <CheckCircle2 aria-hidden="true" size={18} />
            <div>
              <strong>固定版本已发布</strong>
              <span>{draft.title} · 草稿 {draft.draftId}</span>
              <small>基线 {shortCommit(draft.baseCommit)} → {shortCommit(draft.draftCommit)} · 清单 {shortCommit(draft.manifestHash)}</small>
            </div>
          </div>
        ) : (
          <>
            <Field htmlFor="team-draft-project" label="目标项目" required>
              <select className="paw-select" id="team-draft-project" onChange={(event) => setSpaceId(event.target.value)} value={spaceId}>
                <option value="">选择项目空间</option>
                {projectSpaces.map((space) => <option key={space.id} value={space.id}>{space.name}</option>)}
              </select>
            </Field>
            <Field htmlFor="team-draft-title" label="固定版本标题" required>
              <Input autoFocus id="team-draft-title" maxLength={240} onChange={(event) => setTitle(event.target.value)} placeholder="例如：客服流程评测基线" value={title} />
            </Field>
            <p className="team-draft-publish-dialog__session"><GitCommitHorizontal size={14} />当前 Session：<strong>{sessionTitle || sessionId}</strong></p>
          </>
        )}
        {error ? <p className="team-dialog__error" role="alert">{error}</p> : null}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} variant="quiet">{draft ? '关闭' : '取消'}</Button>
          {!draft ? <Button disabled={!spaceId || !title.trim()} leadingIcon={<Upload size={15} />} loading={team.busy === 'publish-project-draft'} onClick={() => void publish()} variant="primary">发布固定版本</Button> : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function shortCommit(value: string): string {
  const normalized = value.trim();
  if (!normalized) return '未知';
  return normalized.length > 12 ? normalized.slice(0, 12) : normalized;
}
