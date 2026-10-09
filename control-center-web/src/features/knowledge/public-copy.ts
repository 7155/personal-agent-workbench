export function publicKnowledgeText(value: string): string {
  return value
    .replace(/\bKnowledge Worker\b/giu, '知识整理服务')
    .replace(/\bAgent Runtime\b/giu, '伙伴运行环境')
    .replace(/\bAgent Loop\b/giu, '伙伴工作循环')
    .replace(/\bAgent Tool(?:s)?\b/giu, '伙伴工具')
    .replace(/\bTools?\b\s*(?=\p{Script=Han})/giu, '工具')
    .replace(/\bTools?\b/giu, '工具')
    .replace(/\bAgent\b\s*(?=\p{Script=Han})/giu, '伙伴')
    .replace(/\bAgent\b/giu, '伙伴');
}

export function knowledgeBlockKindLabel(kind: string): string {
  return ({ paragraph: '正文', text: '正文', heading: '标题', title: '标题', table: '表格', chart: '图表数据', formula: '公式', equation: '公式', image: '图片', figure: '图片', audio: '音频', caption: '图注', reference: '参考文献', list: '列表', notes: '演讲备注', speaker_notes: '演讲备注' } as Record<string, string>)[kind] ?? '文档内容';
}

export function publicKnowledgeRelationKind(value: string): string {
  return ({
    contains: '包含',
    covers: '涵盖',
    derived_from: '衍生自',
    evidence: '证据',
    mentions: '提及',
    next: '下一段',
    next_chunk: '下一段',
    provenance: '来源',
    source: '来源',
  } as Record<string, string>)[value.trim().toLocaleLowerCase('en-US')] ?? '其他关系';
}

export function publicKnowledgeRelationLabel(label: string, kind: string): string {
  const value = label.trim();
  if (!value || value.toLocaleLowerCase('en-US') === kind.trim().toLocaleLowerCase('en-US') || /^[a-z][a-z0-9_.-]*$/iu.test(value)) {
    return publicKnowledgeRelationKind(kind);
  }
  return publicKnowledgeText(value);
}
