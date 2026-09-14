import { useEffect, useState } from 'react';
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

export function TeamProjectDialog({ open, onOpenChange }: { open: boolean; onOpenChange(open: boolean): void }) {
  const team = useTeam();
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setName('');
      setError(null);
    }
  }, [open]);

  async function submit(): Promise<void> {
    const normalized = name.trim();
    if (!normalized) {
      setError('请输入项目名称。');
      return;
    }
    setError(null);
    try {
      await team.createProject(normalized);
      onOpenChange(false);
    } catch (projectError) {
      setError(teamApiErrorMessage(projectError, '项目创建失败，请稍后重试。'));
    }
  }

  return (
    <Dialog onOpenChange={onOpenChange} open={open}>
      <DialogContent className="team-project-dialog">
        <DialogHeader>
          <DialogTitle>新建项目空间</DialogTitle>
          <DialogDescription>项目创建后会进入当前账号的空间列表，并成为当前选中的工作空间。</DialogDescription>
        </DialogHeader>
        <Field htmlFor="team-project-name" label="项目名称" required>
          <Input autoFocus id="team-project-name" onChange={(event) => setName(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') { event.preventDefault(); void submit(); } }} placeholder="例如：客服体验评测" value={name} />
        </Field>
        {error ? <p className="team-dialog__error" role="alert">{error}</p> : null}
        <DialogFooter>
          <Button onClick={() => onOpenChange(false)} variant="quiet">取消</Button>
          <Button loading={team.busy === 'create-project'} onClick={() => void submit()} variant="primary">创建项目</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
