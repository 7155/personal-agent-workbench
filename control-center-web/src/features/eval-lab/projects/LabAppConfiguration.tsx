import { FileCheck2 } from 'lucide-react';
import { Button } from '@/components/primitives';
import type { LabAppVersion } from './apps';

export function LabAppConfiguration({ version, onOpenEvaluation }: { version: LabAppVersion; onOpenEvaluation?: (suiteId: string, jobId: string) => void }) {
  const selection = version.spec.evaluationSelection;
  const knowledge = version.spec.knowledge;
  const methodFile = version.sourceFiles.find((file) => file.path === version.spec.skill);
  return <section className="lab-app-configuration" aria-label="当前应用版本的冻结配置"><header><FileCheck2 size={18} /><h3>此版本实际使用的方案</h3><span>v{version.version}</span></header><dl>
    <div><dt>模型</dt><dd>{version.spec.model.provider} / {version.spec.model.model}<small>推理强度：{version.spec.model.thinkingLevel || '默认'}</small></dd></div>
    <div><dt>应用 Skill 方法</dt><dd>{selection?.applicationMethod?.title || version.spec.skill || '未提供'}<small>{selection?.applicationMethod?.sha256 ? `已选方法 SHA ${selection.applicationMethod.sha256.slice(0, 12)}` : methodFile ? `冻结文件 SHA ${methodFile.sha256.slice(0, 12)}` : '未返回方法文件指纹'}</small></dd></div>
    <div><dt>知识范围</dt><dd>{knowledge ? `${knowledge.documentCount.toLocaleString()} 篇文档 · ${knowledge.chunkCount.toLocaleString()} 个切片` : '未绑定知识快照'}{knowledge ? <small>{knowledge.sourceCount.toLocaleString()} 个来源</small> : null}</dd></div>
    <div><dt>资料怎么找（检索方式）</dt><dd>{knowledge ? `${knowledge.profile.mode} · 取前 ${knowledge.profile.topK} 段 · 最多带 ${knowledge.profile.contextChars.toLocaleString()} 字` : '本版本未提供资料查找方式'}{knowledge ? <small>使用的资料索引：{knowledge.sourceIndexId}</small> : null}</dd></div>
  </dl>{selection ? <div className="lab-app-configuration__selection"><div><strong>绑定已保存的{selection.variant === 'candidate' ? '候选' : '基线'}配置</strong><p>评测快照：{selection.snapshotId || '未返回'} · 原运行：{selection.jobId}</p>{selection.configurationSha256 ? <small title={selection.configurationSha256}>配置 SHA {selection.configurationSha256.slice(0, 12)}</small> : null}</div>{onOpenEvaluation ? <Button size="small" onClick={() => onOpenEvaluation(selection.suiteId, selection.jobId)}>查看原评测</Button> : null}</div> : <p className="lab-app-configuration__unbound">此版本尚未绑定可追溯的评测选择。当前展示的是应用自身的冻结配置。</p>}<details><summary>应用方法文件与版本说明</summary><p>应用使用包内 {version.spec.skill || '方法文件'}。这里的 Skill 指应用方法正文；不代表已完成 Pi Skill 自动路由评测。切换或导出版本会使用对应的冻结内容。</p>{methodFile ? <p>方法文件完整 SHA：{methodFile.sha256}</p> : null}</details></section>;
}
