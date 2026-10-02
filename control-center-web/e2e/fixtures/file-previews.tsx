import type { ControlRequest } from '@/platform/transport';
import { createRoot } from 'react-dom/client';
import { ControlTransportProvider } from '../../src/app/control-transport';
import { TooltipProvider } from '../../src/components/primitives';
import type { AgentFilePreviewV1 } from '../../src/contracts/generated/agent-file-preview.v1';
import { AgentFileBlock } from '../../src/features/agent/file-preview/AgentFileBlock';
import { FilePreviewHost } from '../../src/features/agent/file-preview/FilePreviewHost';
import type { FilePreviewRequest } from '../../src/features/agent/file-preview/file-descriptor';
import { InlineHtmlOutput } from '../../src/features/agent/timeline/InlineHtmlOutput';
import { MockControlTransport } from '../../src/test/mock-transport';
import '../../src/design/tokens.css';
import '../../src/design/typography.css';
import '../../src/components/primitives/primitives.css';
import '../../src/features/agent/agent.css';

const sessionId = 'session-file-preview-qa';
type PreviewFixture = FilePreviewRequest & {
  content: string | null;
  kind: AgentFilePreviewV1['descriptor']['previewKind'];
  language: string;
};
const files: PreviewFixture[] = [
  {
    mediaId: 'media_previewmd001', sessionId, expectedSha256: 'a'.repeat(64),
    fileNameHint: 'handoff.md', mimeTypeHint: 'text/markdown', byteSizeHint: 0,
    kind: 'markdown', language: '',
    content: '# 交接清单\n\n- 原始需求保留\n- 验收证据已绑定',
  },
  {
    mediaId: 'media_previewcode01', sessionId, expectedSha256: 'b'.repeat(64),
    fileNameHint: 'room-commit.ts', mimeTypeHint: 'text/plain', byteSizeHint: 0,
    kind: 'code', language: 'typescript',
    content: 'export function roomCommit(post: string) {\n  return { post, committed: true };\n}',
  },
  {
    mediaId: 'media_previewdiff01', sessionId, expectedSha256: 'c'.repeat(64),
    fileNameHint: 'room-runtime.diff', mimeTypeHint: 'text/x-diff', byteSizeHint: 0,
    kind: 'diff', language: '',
    content: 'diff --git a/runtime.ts b/runtime.ts\n--- a/runtime.ts\n+++ b/runtime.ts\n@@ -1 +1 @@\n-return oldRoute;\n+return managedRoute;',
  },
  {
    mediaId: 'media_previewimg001', sessionId, expectedSha256: 'd'.repeat(64),
    fileNameHint: 'room-proof.png', mimeTypeHint: 'image/png', byteSizeHint: 0,
    kind: 'image', language: '', content: null,
  },
  {
    mediaId: 'media_previewhtml01', sessionId, expectedSha256: 'e'.repeat(64),
    fileNameHint: 'acceptance-report.html', mimeTypeHint: 'text/html', byteSizeHint: 0,
    kind: 'html', language: '',
    content: '<!doctype html><html><head><link rel="stylesheet" href="https://evil.example/theme.css"><style>body{background:#fff}.remote{min-height:24px;background:url(https://evil.example/bg.png)}</style><script>addEventListener("DOMContentLoaded",async()=>{const response=await fetch("https://evil.example/run");document.querySelector("#result").textContent=await response.text()})</script></head><body><h1>交互验收报告</h1><p id="result">正在运行脚本…</p><div class="remote">远程样式区域</div><img alt="远程验收图" src="https://evil.example/image.png"><form><label>备注 <input aria-label="报告备注" value="初始值"></label><button type="button" onclick="document.querySelector(\'#result\').textContent=document.querySelector(\'input\').value">更新报告</button></form></body></html>',
  },
];

const byMediaId = new Map(files.map((file) => [file.mediaId, file]));
const inlineHtml = '<section><h2>页内 HTML 已渲染</h2><label>页内备注 <input aria-label="页内报告备注" value="初始页内值"></label><button type="button" onclick="document.querySelector(\'h2\').textContent=document.querySelector(\'input\').value">更新页内报告</button></section>';
const largeHtml = `<!doctype html><html><body><h1>大型交互报告</h1><button type="button" onclick="document.querySelector('h1').textContent='大型报告脚本已运行'">运行脚本</button><!--${'x'.repeat(1_600_000)}--></body></html>`;
const transport = new MockControlTransport({
  routes: {
    'agent.media.preview': (request: ControlRequest) => {
      const file = byMediaId.get(String(request.params?.mediaId ?? ''));
      if (!file || request.query?.sessionId !== sessionId) throw new Error('file preview denied');
      return preview(file);
    },
  },
});

createRoot(document.getElementById('root')!).render(
  <TooltipProvider>
    <ControlTransportProvider transport={transport}>
      <section aria-labelledby="file-preview-title">
        <h1 id="file-preview-title">受控文件产物</h1>
        <p>同一个 file Rich Block，按类型选择渲染器。</p>
        {files.map((file) => (
          <AgentFileBlock
            data={{
              mediaId: file.mediaId,
              fileName: file.fileNameHint,
              mimeType: file.mimeTypeHint,
              sha256: file.expectedSha256,
            }}
            key={file.mediaId}
            sessionId={sessionId}
          />
        ))}
        <InlineHtmlOutput content={location.search.includes('large-html') ? largeHtml : inlineHtml} />
      </section>
      <FilePreviewHost />
    </ControlTransportProvider>
  </TooltipProvider>,
);

function preview(file: PreviewFixture): AgentFilePreviewV1 {
  const previewByteSize = file.content === null ? 0 : new TextEncoder().encode(file.content).byteLength;
  return {
    schemaVersion: 'rag-ime.agent-file-preview.v1',
    descriptor: {
      schemaVersion: 'rag-ime.agent-file-descriptor.v1',
      mediaId: file.mediaId,
      sessionId,
      fileName: file.fileNameHint,
      mimeType: file.mimeTypeHint,
      byteSize: Math.max(1, previewByteSize),
      sha256: file.expectedSha256,
      previewKind: file.kind,
      language: file.language,
      contentUrl: `/api/agent/media/${file.mediaId}/content?sessionId=${sessionId}`,
    },
    content: file.content,
    previewByteSize,
    truncated: false,
  };
}
