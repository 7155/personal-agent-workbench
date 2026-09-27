import { useState } from 'react';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { WorkbenchToolbar } from './WorkbenchToolbar';
import { MapDrawingToolbar } from './MapDrawingToolbar';
import { TaskDrawer } from './TaskDrawer';
import { EarthDataDock } from './EarthDataDock';
import type { DockTab } from './EarthDataDock';
import { useWorkbenchSurface } from './use-workbench-surface';

afterEach(cleanup);
it('shows analysis and report actions directly', () => {
  render(<WorkbenchToolbar view="map" panel={null} onView={vi.fn()} onPanel={vi.fn()} />);
  expect(screen.getAllByRole('button').map(x=>x.textContent)).toEqual(['分析','Agent','数据','结果','报告','更多']);
  expect(screen.queryByRole('button',{name:'云端任务'})).not.toBeInTheDocument();
});
it('more closes with Escape and returns focus without requesting work', () => {
  const panel=vi.fn(); render(<WorkbenchToolbar view="map" panel={null} onView={vi.fn()} onPanel={panel} />);
  const more=screen.getByRole('button',{name:'更多'}); fireEvent.click(more);
  const cloud=screen.getByRole('button',{name:'云端任务'}); cloud.focus();
  fireEvent.keyDown(cloud,{key:'Escape'});
  expect(more).toHaveAttribute('aria-expanded','false'); expect(more).toHaveFocus();
  expect(panel).not.toHaveBeenCalled();
});
it('opens analysis tools without starting a GIS or Agent operation', () => {
  const panel=vi.fn(); render(<WorkbenchToolbar view="map" panel={null} onView={vi.fn()} onPanel={panel} />);
  fireEvent.click(screen.getByRole('button',{name:'分析'}));
  expect(panel).toHaveBeenCalledTimes(1); expect(panel).toHaveBeenCalledWith('tasks');
  expect(screen.getByRole('button',{name:'更多'})).toHaveAttribute('aria-expanded','false');
});
it('three primary entries are mutually exclusive and can all close', () => {
  function Harness(){ const s=useWorkbenchSurface();return <><WorkbenchToolbar view="map" panel={s.drawer} onView={vi.fn()} onPanel={s.setDrawer} agentVisible={s.agentVisible} onAgent={()=>s.setAgentVisible(v=>!v)} dockView={s.dockView} onData={s.setDockView}/><output>{JSON.stringify({agent:s.agentVisible,data:s.dockView,drawer:s.drawer})}</output></>; }
  render(<Harness/>);
  fireEvent.click(screen.getByRole('button',{name:'Agent'}));
  fireEvent.click(screen.getByRole('button',{name:'数据'}));
  expect(screen.getByRole('button',{name:'Agent'})).toHaveAttribute('aria-expanded','false');
  fireEvent.click(screen.getByRole('button',{name:'结果'}));
  expect(screen.getByRole('button',{name:'数据'})).toHaveAttribute('aria-expanded','false');
  fireEvent.click(screen.getByRole('button',{name:'结果'}));
  expect(screen.getByRole('status')).toHaveTextContent('"data":null');
});
const drawingProps=()=>({drawing:false,kind:undefined,saving:false,vertexCount:0,selectedCount:0,canEdit:false,canCut:false,snapping:true,tolerance:15,multiSelect:false,onDraw:vi.fn(),onEdit:vi.fn(),onRemove:vi.fn(),onCancel:vi.fn(),onSave:vi.fn(),onFinish:vi.fn(),onUndo:vi.fn(),onRedo:vi.fn(),onUndoVertex:vi.fn(),onSnapping:vi.fn(),onMultiSelect:vi.fn()});
it('starts drawing only after an explicit choice; no disabled edit wall', () => {
  const p=drawingProps();render(<MapDrawingToolbar {...p}/>);
  expect(screen.getAllByRole('button')).toHaveLength(2);
  fireEvent.click(screen.getByRole('button',{name:'绘制'}));
  expect(p.onDraw).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button',{name:'矩形'})); expect(p.onDraw).toHaveBeenCalledTimes(1); expect(p.onDraw).toHaveBeenCalledWith('rectangle');
});
it('offers edit actions only for selection, retaining read-only restrictions', () => {
  const p=drawingProps();render(<MapDrawingToolbar {...p} selectedCount={1} readOnlyHint="先保存为项目图层"/>);
  expect(screen.getByRole('button',{name:'编辑所选'})).toBeDisabled();
  fireEvent.click(screen.getByRole('button',{name:'所选操作'}));
  expect(screen.getByRole('button',{name:'删除所选'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'挖洞'})).toBeDisabled(); expect(p.onRemove).not.toHaveBeenCalled();
});
it('staged edits keep save/cancel and lock both during persistence', () => {
  render(<MapDrawingToolbar {...drawingProps()} drawing kind="edit" saving/>);
  expect(screen.getByRole('button',{name:'保存'})).toBeDisabled();
  expect(screen.getByRole('button',{name:'取消'})).toBeDisabled();
  expect(screen.queryByRole('button',{name:'矩形'})).not.toBeInTheDocument();
});
it('hidden task drawers preserve field values and keep the map interactive', () => {
  function Harness(){const[open,setOpen]=useState(false);return <><button onClick={()=>setOpen(v=>!v)}>任务</button><button>地图操作</button><TaskDrawer open={open} title="避让筛选" onClose={()=>setOpen(false)}><input aria-label="保留的距离" defaultValue="200"/></TaskDrawer></>;}
  render(<Harness/>);fireEvent.click(screen.getByRole('button',{name:'任务'}));
  fireEvent.change(screen.getByLabelText('保留的距离'),{target:{value:'300'}});
  fireEvent.click(screen.getByRole('button',{name:'地图操作'})); expect(screen.getByLabelText('保留的距离')).toBeVisible();
  fireEvent.click(screen.getByRole('button',{name:'关闭任务面板'}));
  fireEvent.click(screen.getByRole('button',{name:'任务'})); expect(screen.getByLabelText('保留的距离')).toHaveValue('300');
});
it('data is collapsed by default and reveals an export form only on request', () => {
  function Harness(){const[view,setView]=useState<DockTab|null>(null);return <><button onClick={()=>setView(v=>v?null:'layers')}>切换数据</button><EarthDataDock dockView={view} onDockViewChange={setView} run={null} workspaceRoot="/test" projectLayers={[{id:'a',name:'地块',path:'parcels.geojson',format:'geojson',featureCount:1,geometryTypes:['Polygon'],crs:'EPSG:4326',updatedAt:'',features:[],visible:true}]} spatialSources={[]} workspaceFiles={[]} selectedFeatures={[]} onSaveLayer={vi.fn()}/></>;}
  render(<Harness/>);expect(screen.queryByRole('tab',{name:/图层/})).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'切换数据'}));
  expect(screen.getByRole('tab',{name:/图层/})).toBeVisible();
  expect(screen.getByLabelText('导出格式')).not.toBeVisible();
  fireEvent.click(screen.getByText('导出图层')); expect(screen.getByRole('combobox',{name:'导出格式'})).toBeVisible();
  fireEvent.change(screen.getByRole('combobox',{name:'导出格式'}),{target:{value:'shp'}});
  fireEvent.click(screen.getByRole('button',{name:'切换数据'}));fireEvent.click(screen.getByRole('button',{name:'切换数据'}));
  expect(screen.getByRole('combobox',{name:'导出格式'})).toHaveValue('shp');
});
