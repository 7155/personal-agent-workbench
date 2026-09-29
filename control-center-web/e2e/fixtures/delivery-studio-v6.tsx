import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Dialog, DialogContent, DialogTitle, DialogDescription, TooltipProvider } from '../../src/components/primitives';
import { ImageGallery } from '../../src/features/conversation-ui/media/ImageGallery';
import { RoomPlanetAvatar } from '../../src/features/rooms/RoomPlanetAvatar';
import { usePresentationMotion } from '../../src/features/conversation-ui/reading/reading-preferences';
import { JevDeliveryDesk, JevDeliveryDeskLaunch } from '../../src/paw-os/apps/JevDeliveryDesk';
import { emptyDeliveryDesk, type JevDeliveryFile } from '../../src/paw-os/apps/jev-delivery-desk-model';
import { demoImages } from './pi-content-v5-data';
import { studioFiles, studioStageLabel, studioInputFiles, type StudioScene } from './delivery-studio-v6-data';
import '../../src/design/tokens.css';
import '../../src/design/typography.css';
import '../../src/components/primitives/primitives.css';
import './delivery-studio-v6.css';

/** Uses the installed project React/primitives, real planet assets and image wrapper.
 * The data is synthetic and no file path / task command is executed.
 */
function Fixture() {
  const [scene, setScene] = useState<StudioScene>('review');
  const [state, setState] = useState(() => emptyDeliveryDesk('fixture'));
  const [open, setOpen] = useState(false);
  const [task, setTask] = useState<JevDeliveryFile | null>(null);
  const [note, setNote] = useState('');
  const motion = usePresentationMotion();
  const files = studioFiles(scene);
  return <TooltipProvider><main className="studio-v6-fixture">
    <header><strong>V6 · 真实组件 / 合成数据</strong><select aria-label="选择成果场景" value={scene} onChange={event => setScene(event.currentTarget.value as StudioScene)}><option value="review">待复核</option><option value="delivered">已交付</option><option value="empty">无成果</option><option value="offline">断线保留</option><option value="many">85 项长文件名</option></select></header>
    <h1>对话里的图片，手边的成果</h1><p>这个入口使用原项目的组件与资源。它不是 Pi 联调入口。</p>
    <ImageGallery items={demoImages()}/><JevDeliveryDeskLaunch count={files.length} onClick={() => { setTask(null); setOpen(true); }}/>
    {note ? <p role="status">{note}</p> : null}
    <Dialog open={open} onOpenChange={setOpen}><DialogContent className="studio-v6-dialog"><DialogTitle>成果桌组件预览</DialogTitle><DialogDescription>数据为合成样例，不会读取文件、发送消息或运行任务。</DialogDescription>
      <div className="studio-v6-desk" hidden={Boolean(task)}><JevDeliveryDesk files={files} state={state} onState={change => setState(previous => ({ ...previous, ...change }))}
        active={open && !task} motion={motion && scene !== 'offline'} paused={scene === 'offline'} stageLabel={studioStageLabel}
        attachments={studioInputFiles} renderOwner={file => <RoomPlanetAvatar ordinal={file.task.ownerId === 'earth' ? 2 : file.task.ownerId === 'mars' ? 3 : 1} size={26} activity="static" decorative/>}
        onInspect={setTask} onOpen={file => setNote(`合成入口收到文件打开请求：${file.name}。没有读取该路径。`)} /></div>
      {task ? <section className="studio-v6-task"><button type="button" onClick={() => setTask(null)}>返回成果桌</button><h2>{task.task.objective}</h2><p>{task.task.result}</p><code>{task.ref}</code><p>这里仅示范同一弹层中的返回行为；真正 Room 里进入原有任务详情。</p></section> : null}
    </DialogContent></Dialog>
  </main></TooltipProvider>;
}
createRoot(document.getElementById('root')!).render(<Fixture/>);
