/** Recover the exact submitted script after an HTTP command timeout; never start it again. */
export async function recoverCloudRun({ read, sourceHash, startedAfter, wait = () => new Promise<void>(resolve => setTimeout(resolve, 2000)), isCurrent = () => true }: {
  read: () => Promise<Record<string, any>>; sourceHash: string; startedAfter: number;
  wait?: () => Promise<void>; isCurrent?: () => boolean;
}): Promise<Record<string, any>> {
  for (let attempt = 0; attempt < 90; attempt++) {
    if (!isCurrent()) throw new Error('项目已切换；请回到原项目查看云端结果。');
    let run: Record<string, any> | undefined;
    try { run = await read(); } catch { /* Transient read failure is not an execution outcome. */ }
    if (run && run.sourceHash === sourceHash && Date.parse(run.startedAt) >= startedAfter) {
      if (run.status === 'completed') return run;
      if (['failed', 'cancelled'].includes(run.status)) throw new Error(run.error || '云端分析未完成。');
    }
    await wait();
  }
  throw new Error('尚未收到本次云端分析的终态；请查看运行结果，勿重复启动。');
}
