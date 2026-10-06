import { BookOpen, Check, Plus } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import { useControlTransport } from '@/app/control-transport';
import { Button } from '@/components/primitives';
import { compactContractText, textCodePointCount } from '@/contracts/text-budget';
import type { MemoryReferenceSelection } from './MemoryReferenceDialog';
import './memory-profile.css';

export type PersonalProfileParagraph = { id: string; memoryIds: string[]; text: string; revision: string;
  sourceCount: number; sourceRefs: { kind: 'evidence' | 'event'; id: string }[] };
export type PersonalProfile = { schemaVersion: 'paw.personal-profile.v1'; revision: string; text: string;
  paragraphs: PersonalProfileParagraph[]; truncated: boolean };
type DraftParagraph = { id: string | null; memoryIds: string[]; text: string; revision?: string; key: string };

export function MemoryProfile({ onOpenReference, onSaved }: {
  onOpenReference: (source: MemoryReferenceSelection) => void; onSaved?: () => void;
}) {
  const transport = useControlTransport();
  const budgetId = useId();
  const [profile, setProfile] = useState<PersonalProfile>();
  const [draft, setDraft] = useState<DraftParagraph[]>([]);
  const [latest, setLatest] = useState<PersonalProfile>();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [readingLatest, setReadingLatest] = useState(false);
  const [error, setError] = useState('');
  const [saved, setSaved] = useState(false);
  const [conflict, setConflict] = useState(false);
  const [revision, setRevision] = useState(0);
  const owner = useRef(0);
  const lock = useRef(false);
  const latestReadLock = useRef(false);
  const attempt = useRef<{ signature: string; id: string } | undefined>(undefined);
  useEffect(() => {
    const generation = ++owner.current;
    const controller = new AbortController();
    setLoading(true); setError(''); setProfile(undefined); setLatest(undefined); setConflict(false); setSaving(false); setSaved(false); setReadingLatest(false); lock.current = false; latestReadLock.current = false;
    void transport.request({ pathId: 'memory.profile', signal: controller.signal }).then(value => {
      if (owner.current !== generation || controller.signal.aborted) return;
      const next = parsePersonalProfile(value);
      setProfile(next); setDraft(profileDraft(next));
    }).catch(reason => { if (owner.current === generation && !controller.signal.aborted) setError(errorMessage(reason)); })
      .finally(() => { if (owner.current === generation && !controller.signal.aborted) setLoading(false); });
    return () => { controller.abort(); owner.current += 1; };
  }, [transport, revision]);
  const dirty = profile && JSON.stringify(draft.map(item => ({ id: item.id, text: item.text }))) !== JSON.stringify(profileDraft(profile).map(item => ({ id: item.id, text: item.text })));
  const normalizedParagraphs = draft.map(item => compactContractText(item.text));
  const total = textCodePointCount(normalizedParagraphs.filter(Boolean).join('\n\n'));
  const invalid = total > 4000 || normalizedParagraphs.some(text => textCodePointCount(text) > 600);

  async function save() {
    if (!profile || !dirty || lock.current || invalid || conflict) return;
    lock.current = true; setSaving(true); setSaved(false); setError('');
    const generation = owner.current;
    const paragraphs = draft.filter(item => item.id || compactContractText(item.text)).map(({ key: _key, ...item }) => item);
    const signature = JSON.stringify({ expectedRevision: profile.revision, paragraphs });
    if (attempt.current?.signature !== signature) attempt.current = { signature, id: `profile-${crypto.randomUUID()}` };
    try {
      const value = await transport.request<{ ok: boolean; profile: unknown }>({ pathId: 'memory.profile.save', body: {
        expectedRevision: profile.revision, clientRequestId: attempt.current.id, paragraphs,
      } });
      if (generation !== owner.current) return;
      if (!value.ok) throw new Error('保存尚未确认。请保留草稿后重试。');
      const receipt = parsePersonalProfile(value.profile);
      setProfile(receipt); setDraft(profileDraft(receipt)); setSaved(true); attempt.current = undefined; onSaved?.();
      try {
        const current = parsePersonalProfile(await transport.request({ pathId: 'memory.profile' }));
        if (generation !== owner.current) return;
        if (current.revision !== receipt.revision) {
          setLatest(current); setConflict(true);
          setError('这次修改已保存，但另一个窗口随后更新了背景。请核对最新版本后继续编辑。');
        }
      } catch { if (generation === owner.current) setError('这次修改已保存；最新版本暂时无法核对。继续编辑时仍会检查版本。'); }
    } catch (reason) {
      if (generation !== owner.current) return;
      const text = errorMessage(reason);
      const envelope = record(reason); const payload = record(envelope.payload);
      if (envelope.status === 409 || payload.code === 'memory_profile_revision_conflict' || text.includes('memory_profile_revision_conflict')) {
        setConflict(true); setError('关于我已有新版本。你的草稿完整保留，请先核对最新内容。');
        try {
          const current = payload.current ?? await transport.request({ pathId: 'memory.profile' });
          if (generation === owner.current) setLatest(parsePersonalProfile(current));
        } catch { /* Failure to read the new version never discards the draft. */ }
      } else setError(`${text} 草稿已保留，重试会核对同一次保存。`);
    } finally {
      if (generation === owner.current) { lock.current = false; setSaving(false); }
    }
  }
  async function readLatest() {
    if (latestReadLock.current || lock.current) return;
    latestReadLock.current = true; setReadingLatest(true);
    const generation = owner.current;
    try {
      const next = parsePersonalProfile(await transport.request({ pathId: 'memory.profile' }));
      if (generation === owner.current) setLatest(next);
    } catch (reason) { if (generation === owner.current) setError(`最新版本暂时读不到：${errorMessage(reason)}。你的草稿仍保留在下方。`); }
    finally { if (generation === owner.current) { latestReadLock.current = false; setReadingLatest(false); } }
  }
  function adoptLatest() {
    if (!latest || latestReadLock.current || lock.current) return;
    setProfile(latest); setDraft(profileDraft(latest)); setLatest(undefined); setConflict(false); setError(''); setSaved(false); attempt.current = undefined;
  }
  return <section className="memory-profile" aria-label="关于我">
    <header><span className="memory-profile__eyebrow">PERSONAL CONTEXT</span><h2>关于我</h2><p>留下一点长期有用的背景。下次聊天，不用每次从头解释。</p></header>
    <div className="memory-profile__intro"><BookOpen aria-hidden="true" size={16} /><span>这些内容来自已有记忆卡片；修改后仍保留版本和来源。保存不会开启记忆召回；是否用于对话，仍由记忆偏好中的开关决定。</span></div>
    {loading ? <p role="status">正在读取个人背景…</p> : null}
    {error ? <div className="memory-profile__notice" role="alert"><p>{error}</p>{!profile ? <Button size="small" disabled={loading} onClick={() => setRevision(value => value + 1)}>重新读取</Button> : conflict ? <Button size="small" disabled={saving || readingLatest} onClick={() => void readLatest()}>{readingLatest ? '正在读取最新版本…' : '查看最新版本'}</Button> : null}</div> : null}
    {latest ? <section className="memory-profile__comparison" aria-label="服务器最新版本"><h3>最新保存的内容</h3><p>{latest.text || '暂无内容'}</p><small>版本 {latest.revision.slice(0, 12)}</small><Button disabled={saving || readingLatest} onClick={adoptLatest} size="small" variant="quiet">放弃下方草稿，编辑最新版本</Button></section> : null}
    {profile ? <>
      {profile.truncated ? <p className="memory-profile__notice">这里显示一部分长期背景。其余卡片仍保留在记忆库中，本次保存不会移除它们。</p> : null}
      <div className="memory-profile__paragraphs">{draft.map((item, index) => {
        const source = profile.paragraphs.find(paragraph => paragraph.id === item.id);
        const length = textCodePointCount(normalizedParagraphs[index]);
        const paragraphInvalid = length > 600;
        const paragraphErrorId = `${budgetId}-${item.key}`;
        return <section className="memory-profile__paragraph" key={item.key}>
          <label htmlFor={`profile-paragraph-${item.key}`}><span>背景 {index + 1}</span><small className={paragraphInvalid ? 'memory-profile__invalid-count' : undefined}>{length} / 600</small></label>
          {paragraphInvalid ? <p className="memory-profile__field-error" id={paragraphErrorId} role="alert">这条背景最多 600 字，请精简后保存。草稿已完整保留。</p> : null}
          <textarea id={`profile-paragraph-${item.key}`} aria-label={`个人背景 ${index + 1}`} aria-invalid={paragraphInvalid || undefined} aria-describedby={paragraphInvalid ? paragraphErrorId : undefined} value={item.text} rows={3} disabled={saving} onChange={event => { setDraft(current => current.map(row => row.key === item.key ? { ...row, text: event.target.value } : row)); setSaved(false); }} placeholder="例如：我正在做什么，希望助手怎样配合，哪些偏好值得长期记住。" />
          {source ? <footer><span>{item.text !== source.text ? '未保存的修改' : '已保存'} · 版本 {source.revision.slice(0, 10)}</span><div>{source.sourceRefs.slice(0, 3).map((ref, sourceIndex) => <button key={`${ref.kind}:${ref.id}`} onClick={() => onOpenReference({ kind: ref.kind, referenceId: ref.id })} type="button">来源 {sourceIndex + 1}</button>)}{source.sourceRefs.length > 3 ? <details className="memory-profile__more-sources"><summary>其余 {source.sourceRefs.length - 3} 条来源</summary><div>{source.sourceRefs.slice(3).map((ref, sourceIndex) => <button key={`${ref.kind}:${ref.id}`} onClick={() => onOpenReference({ kind: ref.kind, referenceId: ref.id })} type="button">来源 {sourceIndex + 4}</button>)}</div></details> : null}{source.sourceCount > source.sourceRefs.length ? <small>共 {source.sourceCount} 条来源</small> : null}</div></footer> : <footer><span>新背景 · 尚未保存</span></footer>}
          {item.id && !normalizedParagraphs[index] ? <small className="memory-profile__retract">保存后，这条背景会退出当前简介；历史版本仍可核对。</small> : null}
        </section>;
      })}</div>
      {total > 4000 ? <p className="memory-profile__field-error" id={`${budgetId}-total`} role="alert">全部背景最多 4000 字（含段间空行），请精简后保存。草稿已完整保留。</p> : null}
      <div className="memory-profile__actions"><Button leadingIcon={<Plus size={14} />} size="small" variant="quiet" disabled={saving || draft.length >= 12} onClick={() => setDraft(current => [...current, newParagraph()])}>补充一条背景</Button><span className={total > 4000 ? 'memory-profile__invalid-count' : undefined}>{total} / 4000 字</span><Button leadingIcon={saved ? <Check size={14} /> : undefined} aria-describedby={total > 4000 ? `${budgetId}-total` : undefined} disabled={!dirty || invalid || conflict} loading={saving} onClick={() => void save()} size="small">{saving ? '正在保存' : saved ? '已保存' : '保存修改'}</Button></div>
      <p className="memory-profile__footnote" role="status">{saving ? '正在保存，请稍候。' : conflict ? '草稿尚未覆盖最新版本。核对上方内容后再继续。' : dirty ? '有未保存的修改。点击“保存修改”后才会更新背景。' : saved ? '本次修改已保存。' : '当前显示已保存的背景。'}</p>
      <p className="memory-profile__footnote">只保留确认过、长期有用的事实。不确定的背景可以留到对话里再说。</p>
    </> : null}
  </section>;
}
export function parsePersonalProfile(value: unknown): PersonalProfile {
  const data = record(value);
  if (data.schemaVersion !== 'paw.personal-profile.v1' || typeof data.revision !== 'string' || !Array.isArray(data.paragraphs)
    || data.paragraphs.some(item => { const p = record(item); return typeof p.id !== 'string' || typeof p.revision !== 'string' || typeof p.text !== 'string' || !Array.isArray(p.memoryIds) || !Array.isArray(p.sourceRefs); })) {
    throw new Error('个人背景的读取结果不完整。');
  }
  return data as PersonalProfile;
}
function newParagraph(): DraftParagraph { return { id: null, memoryIds: [], text: '', key: crypto.randomUUID() }; }
function profileDraft(profile: PersonalProfile): DraftParagraph[] {
  return profile.paragraphs.length ? profile.paragraphs.map(({ id, memoryIds, text, revision }) => ({ id, memoryIds, text, revision, key: id })) : [{ id: null, memoryIds: [], text: '', key: 'empty' }];
}
function errorMessage(value: unknown): string { return value instanceof Error ? value.message : '暂时无法连接记忆服务'; }
function record(value: unknown): Record<string, unknown> { return value && typeof value === 'object' ? value as Record<string, unknown> : {}; }
