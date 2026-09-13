import { expect, it } from 'vitest';
import { applyWorkspaceDraft, messageWithWorkspaceContext } from './workspace-draft';

it('preserves the question while replacing only the latest map context', () => {
  const first = applyWorkspaceDraft('分析这里的坡度', {id:1,text:'Point A',contextKey:'地图'});
  expect(first).toContain('分析这里的坡度');
  const next = applyWorkspaceDraft(first + '\n优先避开林地', {id:2,text:'Polygon B',contextKey:'地图'});
  expect(next).toContain('分析这里的坡度');
  expect(next).toContain('优先避开林地');
  expect(next).toContain('Polygon B');
  expect(next).not.toContain('Point A');
  const cleared = applyWorkspaceDraft(next,{id:3,text:'',contextKey:'地图'});
  expect(cleared).toContain('分析这里的坡度');
  expect(cleared).toContain('优先避开林地');
  expect(cleared).not.toContain('Polygon B');
});
it('keeps existing replacement requests compatible', () => {
  expect(applyWorkspaceDraft('old',{id:1,text:'new'})).toBe('new');
});
it('includes the selected geometry only in an explicit message and preserves slash commands',()=>{
  const context={label:'区域 A',detail:'WGS84',text:'{"type":"Polygon"}',onClear:()=>{}};
  expect(messageWithWorkspaceContext('比较坡度',context)).toContain('比较坡度\n\n地图上下文：区域 A');
  expect(messageWithWorkspaceContext('比较坡度',context)).toContain('```geojson\n{"type":"Polygon"}\n```');
  expect(messageWithWorkspaceContext('/new',context)).toBe('/new');
  expect(messageWithWorkspaceContext('问题')).toBe('问题');
});
