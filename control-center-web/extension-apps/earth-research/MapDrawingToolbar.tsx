import { Check, MousePointer2, Pencil, Redo2, Save, Scissors, Trash2, Undo2, X } from 'lucide-react';
import { DisclosureMenu } from './DisclosureMenu';
import type { DrawingKind } from './drawing-controls';

type Props = {
  drawing: boolean; kind?: DrawingKind; saving: boolean; vertexCount: number;
  selectedCount: number; canEdit: boolean; canCut: boolean; readOnlyHint?: string;
  snapping: boolean; tolerance: number; multiSelect: boolean;
  onDraw: (kind: DrawingKind) => void; onEdit: (kind: 'edit' | 'cut') => void;
  onRemove: () => void; onCancel: () => void; onSave: () => void; onFinish: () => void;
  onUndo: () => void; onRedo: () => void; onUndoVertex: () => void;
  onSnapping: (enabled: boolean, pixels: number) => void; onMultiSelect: (enabled: boolean) => void;
};
export function MapDrawingToolbar(p: Props) {
  const editing = ['edit', 'remove', 'cut'].includes(p.kind ?? '');
  const vertices = p.kind === 'polygon' || p.kind === 'polyline';
  return <fieldset className="earth-gis-toolbar earth-simple-drawing" aria-label="地图绘制工具" disabled={p.saving}>
    {!p.drawing ? <>
      <button type="button" aria-pressed title="单击选择，Shift 多选" onClick={p.onCancel}><MousePointer2 size={16} aria-hidden="true" />选择</button>
      <DisclosureMenu label="绘制" disabled={p.saving}>
        {close => <>{([['polygon', '面'], ['rectangle', '矩形'], ['point', '点'], ['polyline', '线']] as const).map(([kind, label]) =>
          <button type="button" key={kind} onClick={() => { close(); p.onDraw(kind); }}>{label}</button>)}
          <label className="earth-disclosure__group"><input type="checkbox" checked={p.multiSelect} onChange={event => p.onMultiSelect(event.target.checked)} />连续多选</label>
        </>}
      </DisclosureMenu>
      {p.selectedCount > 0 ? <>
        <button type="button" disabled={!p.canEdit} title={p.readOnlyHint} onClick={() => p.onEdit('edit')}><Pencil size={16} aria-hidden="true" />编辑所选</button>
        <DisclosureMenu label="所选操作" disabled={p.saving}>
          {close => <>
            <button type="button" disabled={!p.canEdit} title={p.readOnlyHint} onClick={() => { close(); p.onRemove(); }}><Trash2 size={15} aria-hidden="true" />删除所选</button>
            <button type="button" disabled={!p.canCut} title={p.readOnlyHint} onClick={() => { close(); p.onEdit('cut'); }}><Scissors size={15} aria-hidden="true" />挖洞</button>
          </>}
        </DisclosureMenu>
      </> : null}
    </> : <>
      <span className="earth-simple-drawing__mode">{({ edit: '编辑顶点', remove: '暂存删除', cut: '绘制洞口', polygon: '绘制面', polyline: '绘制线', point: '放置点', rectangle: '绘制矩形' } as Record<string, string>)[p.kind ?? ''] || '绘制中'}</span>
      {editing ? <>
        <button type="button" onClick={p.onUndo}><Undo2 size={15} aria-hidden="true" />撤销</button>
        <button type="button" onClick={p.onRedo}><Redo2 size={15} aria-hidden="true" />重做</button>
        <button type="button" className="earth-gis-toolbar__commit" onClick={p.onSave}><Save size={15} aria-hidden="true" />保存</button>
      </> : vertices ? <>
        <button type="button" disabled={!p.vertexCount} onClick={p.onUndoVertex}><Undo2 size={15} aria-hidden="true" />撤销顶点</button>
        <button type="button" className="earth-gis-toolbar__commit" onClick={p.onFinish}><Check size={15} aria-hidden="true" />完成</button>
      </> : null}
      <button type="button" onClick={p.onCancel}><X size={15} aria-hidden="true" />取消</button>
      {p.kind !== 'remove' ? <DisclosureMenu label="绘制设置" disabled={p.saving}>
        {() => <>
          <label><input type="checkbox" checked={p.snapping} onChange={event => p.onSnapping(event.target.checked, p.tolerance)} />启用吸附</label>
          <label>容差（像素）<input aria-label="吸附容差（像素）" type="number" min="1" max="80" value={p.tolerance} onChange={event => {
            const value = Number(event.target.value);
            if (Number.isFinite(value) && value >= 1 && value <= 80) p.onSnapping(p.snapping, value);
          }} /></label>
        </>}
      </DisclosureMenu> : null}
    </>}
  </fieldset>;
}
