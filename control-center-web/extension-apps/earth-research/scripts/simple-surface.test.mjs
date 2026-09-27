import assert from 'node:assert/strict';
import { test } from 'node:test';
import { INITIAL_SURFACE, reduceWorkbenchSurface as reduce } from '../workbench-surface.ts';
const count = s => Number(s.agentVisible) + Number(Boolean(s.drawer)) + Number(Boolean(s.dockView));

test('starts with the map and no ancillary surface', () => {
  assert.deepEqual(INITIAL_SURFACE, { agentVisible: false, drawer: null, dockView: null });
});
for (const [name, action, expected] of [
  ['agent', {type:'agent', value:true}, {agentVisible:true, drawer:null, dockView:null}],
  ['analysis', {type:'drawer', value:'gis'}, {agentVisible:false, drawer:'gis', dockView:null}],
  ['data', {type:'dock', value:'layers'}, {agentVisible:false, drawer:null, dockView:'layers'}],
]) test(`opening ${name} replaces other surfaces atomically`, () => {
  const previous = {agentVisible:true, drawer:'remote', dockView:'files'};
  assert.deepEqual(reduce(previous, action), expected);
  assert.deepEqual(previous, {agentVisible:true, drawer:'remote', dockView:'files'});
});
test('closing an old task after launching Agent does not close Agent', () => {
  let s=reduce(INITIAL_SURFACE,{type:'drawer',value:'gis'});
  s=reduce(s,{type:'agent',value:true}); s=reduce(s,{type:'drawer',value:null});
  assert.deepEqual(s,{agentVisible:true,drawer:null,dockView:null});
});
test('a late data-close preserves a newly opened task', () => {
  let s=reduce(INITIAL_SURFACE,{type:'dock',value:'files'});
  s=reduce(s,{type:'drawer',value:'delivery'}); s=reduce(s,{type:'dock',value:null});
  assert.equal(s.drawer,'delivery');
});
test('functional updates resolve against the most recent state', () => {
  let s=reduce(INITIAL_SURFACE,{type:'agent',value:v=>!v});
  assert.equal(s.agentVisible,true);
  s=reduce(s,{type:'agent',value:v=>!v}); assert.equal(count(s),0);
  s=reduce(s,{type:'drawer',value:v=>v===null?'gis':null}); assert.equal(s.drawer,'gis');
  s=reduce(s,{type:'drawer',value:v=>v===null?'gis':null}); assert.equal(count(s),0);
});
test('reset removes only presentation state without mutating prior state', () => {
  const previous={agentVisible:false,drawer:'remote',dockView:null};
  assert.deepEqual(reduce(previous,{type:'reset'}),INITIAL_SURFACE);
  assert.equal(previous.drawer,'remote');
});
test('tabs can change without mounting another type of surface', () => {
  let s=reduce(INITIAL_SURFACE,{type:'dock',value:'layers'});
  for(const tab of ['attributes','files','databases','runs']) {
    s=reduce(s,{type:'dock',value:tab}); assert.equal(s.dockView,tab); assert.equal(count(s),1);
  }
});
test('bounded exhaustive action sequences never expose multiple surfaces', () => {
  const actions=[{type:'agent',value:true},{type:'agent',value:false},{type:'agent',value:v=>!v},
    {type:'drawer',value:'gis'},{type:'drawer',value:'remote'},{type:'drawer',value:null},
    {type:'dock',value:'layers'},{type:'dock',value:'runs'},{type:'dock',value:null},{type:'reset'}];
  let states=[INITIAL_SURFACE];
  for(let depth=0; depth<4; depth++) states=states.flatMap(s=>actions.map(a=>{
    const next=reduce(s,a); assert.ok(count(next)<=1); return next;
  }));
  assert.equal(states.length,10000);
});
