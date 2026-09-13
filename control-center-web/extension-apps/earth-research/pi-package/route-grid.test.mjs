import { test } from 'node:test';
import assert from 'node:assert/strict';
import { routeGrid } from './route-grid.mjs';
test('recovers a route around exclusions, with exact endpoints and no corner cutting', () => {
  const grid = [[1,1,1,1],[1,0,0,1],[1,1,1,1]];
  const result = routeGrid(grid,[1,0],[1,3]);
  assert.equal(result.status,'completed'); assert.equal(result.gridCost,5);
  assert.deepEqual(result.cells[0],[1,0]); assert.deepEqual(result.cells.at(-1),[1,3]);
  result.cells.forEach(([r,c],i) => { assert.ok(grid[r][c]>0); if(i) assert.equal(Math.abs(r-result.cells[i-1][0])+Math.abs(c-result.cells[i-1][1]),1); });
});
test('missing data and complete barriers produce no route', () => {
  assert.equal(routeGrid([[1,NaN,1],[1,null,1]],[0,0],[0,2]).status,'no_route');
  assert.equal(routeGrid([[1,0],[0,1]],[0,0],[1,1]).status,'no_route');
});
test('soft cost never overrides a forbidden cell, and clearance can close a narrow corridor', () => {
  const grid = Array.from({length:7},()=>Array(7).fill(1));
  for(let r=0;r<7;r++) if(r!==3) grid[r][3]=0;
  assert.equal(routeGrid(grid,[3,1],[3,5]).status,'completed');
  assert.equal(routeGrid(grid,[3,1],[3,5],{clearanceCells:1}).status,'no_route');
});
test('invalid endpoints and malformed grids do not manufacture geometry', () => {
  assert.deepEqual(routeGrid([[0,1]],[0,0],[0,1]),{status:'invalid_endpoint',cells:[]});
  assert.throws(()=>routeGrid([[1],[1,1]],[0,0],[1,0]));
});
