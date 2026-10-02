import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ControlTransportProvider } from '../../src/app/control-transport';
import { createPreviewTransport } from '../../src/app/preview-control-transport';
import { TooltipProvider } from '../../src/components/primitives';
import type { UiAgentBlock } from '../../src/contracts/ui-events';
import { AgentBlocks, MarkdownBody } from '../../src/features/agent/timeline/BlockRenderer';
import { CodeContentBlock } from '../../src/features/agent/timeline/CodeDiffRenderers';
import { RichImage, MarkdownImage } from '../../src/features/agent/timeline/rich/RichImage';
import { RichMediaPlayer } from '../../src/features/agent/timeline/rich/RichMediaPlayer';
import { RoomPlanetAvatar } from '../../src/features/rooms/RoomPlanetAvatar';
import { previewImage, previewAudio, previewVideo } from './conversation-rich-v2-assets';
import '../../src/design/tokens.css';
import '../../src/design/typography.css';
import '../../src/components/primitives/primitives.css';
import '../../src/features/agent/agent.css';
import '../../src/features/conversation-ui/conversation-ui.css';
import '../../src/features/agent/timeline/rich/rich-conversation.css';
import './conversation-rich-v2.css';

const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
const transport = createPreviewTransport();
const prose = `正文是一条连续的阅读路径。**强调**、行内 \`代码\` 和引用保持克制。

## 表格不撑破对话

| 内容 | 展示方式 | 进一步操作 |
| :--- | :--- | :--- |
| 代码 | 高亮与行号 | 复制、换行、保存 |
| 表格 | 独立滚动 | 复制可见行列、导出 CSV |
| 媒体 | 真实播放状态 | 放大或打开原文件 |

> 这里是合成 UI fixture，不调用模型，不宣称已完成真实多 Agent 任务。

- [x] 保留已返回的内容
- [ ] 等待下一条真实回执


det(A) 的显示示例：$\\det(A)=6$。

$$
\\operatorname{Attention}(Q,K,V)=\\operatorname{softmax}\\left(\\frac{QK^{\\mathsf T}}{\\sqrt{d_k}}\\right)V
$$

完整来源可使用 [工作区文档](docs/README.md) 链接，由现有 Evidence Echo 接管。
`;
const source = `type Receipt = { id: string; status: 'running' | 'done' };

export function settle(previous: Receipt, next: Receipt) {
  if (previous.id !== next.id) return previous;
  return { ...previous, ...next };
}`;
const diagram = `flowchart LR
  goal[用户目标] --> a[Mars 整理资料]
  goal --> b[Venus 完善页面]
  a --> c[Earth 独立复核]
  b --> c
  c --> result[交付]`;
const blocks: UiAgentBlock[] = [
  { id:'card', type:'card', presentationKind:'card', status:'completed', data:{title:'一个明确的说明',tone:'info',bodyMarkdown:'正文、文件与过程分别组织，**已有能力仍由原渲染器负责**。',fields:[{label:'来源',value:'UI fixture'},{label:'范围',value:'仅展示'}]} },
  { id:'checklist', type:'checklist', presentationKind:'checklist', status:'completed', data:{title:'合成检查项',items:[{id:'a',text:'保留原始文件',checked:true},{id:'b',text:'等待独立复核',checked:false}]} },
  { id:'table', type:'table', presentationKind:'table', status:'completed', data:{title:'结构化数据',columns:[{key:'kind',label:'类型'},{key:'count',label:'示例数量'}],rows:[{kind:'代码',count:8},{kind:'表格',count:6}]} },
  { id:'reference', type:'citation', presentationKind:'citation', status:'completed', data:{title:'项目中的设计说明',source:'本地 fixture',excerpt:'这是为了确认引用组件形态而准备的片段，不代表外部资料。'} },
  { id:'status', type:'status', presentationKind:'status', status:'completed', data:{title:'等待前置任务',state:'waiting',detail:'没有运行中的工具，不播放持续旋转。'} },
  { id:'plan', type:'task_plan', presentationKind:'task_plan', status:'completed', data:{title:'分工示例',items:[{title:'整理资料',status:'done'},{title:'完善页面',status:'pending'},{title:'独立复核',status:'pending'}]} },
  { id:'diff', type:'diff', presentationKind:'diff', status:'completed', data:{fileName:'receipt.ts',diff:'--- a/receipt.ts\n+++ b/receipt.ts\n@@ -1 +1,2 @@\n-return next;\n+if (previous.id !== next.id) return previous;\n+return { ...previous, ...next };'} },
  { id:'file', type:'file', presentationKind:'file', status:'completed', data:{fileName:'示例演示文稿.pptx',mimeType:'application/vnd.openxmlformats-officedocument.presentationml.presentation'} },
  { id:'image-missing', type:'image', presentationKind:'image', status:'completed', data:{alt:'没有回执的图片'} },
  { id:'unknown', type:'unknown', presentationKind:'custom_widget', rawType:'custom_widget', status:'completed', summary:'未注册类型的摘要仍可读，原始回执不在这里自动展开。', data:{} },
];

function Fixture() {
  const [dark, setDark] = useState(false);
  const [narrow, setNarrow] = useState(false);
  const [stream, setStream] = useState('');
  const [streaming, setStreaming] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const [draft, setDraft] = useState('');
  const [saved, setSaved] = useState('');
  useEffect(() => { document.documentElement.dataset.theme = dark ? 'dark' : 'light'; }, [dark]);
  useEffect(() => () => clearInterval(timer.current), []);
  const stop = () => { clearInterval(timer.current); timer.current = undefined; setStreaming(false); };
  const play = () => {
    stop(); let i = 0; setStreaming(true); setStream('');
    const sample = `这是一段明确标记的流式样例。\n\n\`\`\`typescript\n${source}\n\`\`\`\n\n已展示完整样例，不调用模型。`;
    timer.current = setInterval(() => { i += 8; setStream(sample.slice(0, i)); if (i >= sample.length) stop(); }, 35);
  };
  return <QueryClientProvider client={client}><ControlTransportProvider transport={transport}><TooltipProvider>
    <div className="paw-desktop-root rich-v2-fixture" data-paw-visual="stellar" data-narrow={narrow}>
      <header className="rich-v2-fixture__toolbar"><strong>真实 React 渲染器 · 合成输入</strong><span>不连接模型</span>
        <button type="button" onClick={() => setDark(v => !v)}>{dark ? '浅色' : '深色'}</button>
        <button type="button" onClick={() => setNarrow(v => !v)}>{narrow ? '宽窗口' : '窄窗口'}</button>
      </header>
      <main className="rich-v2-fixture__reader">
        <header className="rich-v2-fixture__identity"><RoomPlanetAvatar ordinal={0} size={30} activity="static" /><div><h1>内容应该有自己的展示方式</h1><p>Session / Room 共用的渲染入口，不是独立复制的 UI。</p></div></header>
        <nav aria-label="展示内容索引">{['正文','代码','公式与图示','媒体','结构化块','流式输入'].map((label,i)=><a key={label} href={`#rich-section-${i}`}>{label}</a>)}</nav>
        <section id="rich-section-0"><h2>正文、表格与行内公式</h2><MarkdownBody documentKey="rich-v2-prose" text={prose} /></section>
        <section id="rich-section-1"><h2>代码与数据</h2><CodeContentBlock code={source} language="typescript" fileName="receipt.ts" />
          <CodeContentBlock language="json" code={JSON.stringify({title:'示例',blocks:[{type:'text'},{type:'file'}],source:{owner:'session',ready:true}},null,2)} />
          <CodeContentBlock language="csv" code={'内容类型,示例数量\n正文,12\n代码,8\n表格,6\n图片,5'} />
        </section>
        <section id="rich-section-2"><h2>完整内容返回后再生成预览</h2><CodeContentBlock language="mermaid" code={diagram} />
          <CodeContentBlock language="latex" code={String.raw`A=\begin{bmatrix}2&1\\0&3\end{bmatrix},\quad\det(A)=6`} />
          <CodeContentBlock language="svg" code={'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 400 120"><rect x="20" y="25" width="150" height="70" rx="8" fill="#edf0ea"/><text x="95" y="65" text-anchor="middle" font-size="16">静态 SVG</text><path d="M180 60h80" fill="none" stroke="#66796a"/><circle cx="320" cy="60" r="33" fill="#d3ded2"/></svg>'} />
          <MarkdownBody text={'```html\n<section style="padding:24px;font:16px system-ui;background:#f4f4ee"><h2>HTML 输出样例</h2><p>沿用项目现有的隔离预览。</p></section>\n```'} />
        </section>
        <section id="rich-section-3"><h2>真实媒体控件，合成媒体材料</h2><RichImage source={previewImage} alt="上一版工作台设计截图" width={1200} height={853} caption="用户已提供的上一版设计，仅作对照材料" />
          <RichImage source="/e2e/fixtures/retry-image.png" alt="失败重试图" width={32} height={32} />
          <RichMediaPlayer name="notification-sample.wav" source={previewAudio} />
          <RichMediaPlayer name="workbench-sample.mp4 · 静帧示例" source={previewVideo} kind="video" />
          <MarkdownImage src="https://invalid.example/not-loaded.png" alt="外部图片默认不自动加载" />
        </section>
        <section id="rich-section-4"><h2>原有结构化类型与失败边界</h2><AgentBlocks blocks={blocks} sessionId="rich-v2-fixture" /></section>
        <section id="rich-section-5"><h2>流式文本与输入法</h2><div className="rich-v2-fixture__stream-tools"><button type="button" disabled={streaming} onClick={play}>播放合成流式文本</button><button type="button" disabled={!streaming} onClick={stop}>停止</button></div>
          <MarkdownBody text={stream} streamingTail={streaming} documentKey="rich-v2-stream" />
          <textarea aria-label="输入法行为示例" placeholder="输入仅留在 fixture；中文组词时 Enter 不提交。" value={draft} onChange={e=>setDraft(e.target.value)} onKeyDown={e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.nativeEvent.isComposing&&e.keyCode!==229){e.preventDefault();setSaved(draft);setDraft('');}}} />
          {saved ? <p role="status">本地输入：{saved}</p> : null}
        </section>
      </main>
    </div>
  </TooltipProvider></ControlTransportProvider></QueryClientProvider>;
}
const root = document.getElementById('root');
if (root) createRoot(root).render(<Fixture />);
