import assert from 'node:assert/strict';
import test from 'node:test';
import { renderGISReport } from './gis-report.mjs';
const input = { run: {runId:'run-1',status:'completed',op:'site-selection',params:{distance:300},updatedAt:'2026-09-22'}, stats:{params:{distance:300},inputVersions:[],outputs:[{path:'<script>.gpkg',featureCount:2,areaM2:123.45,crs:'EPSG:32651'}]}, cartography:{title:'<script>alert(1)</script>',crs:'EPSG:32651',paperSize:'A4',orientation:'landscape'}, mapPng:Buffer.from('89504e470d0a1a0a','hex') };
test('portable reports embed the map, escape supplied text and state missing evidence',()=>{
 const html=renderGISReport(input);assert.match(html,/src="data:image\/png;base64,/);assert.ok(!html.includes('src="map.png"'));assert.ok(!html.includes('<script>'));assert.match(html,/123.45/);assert.match(html,/300/);assert.match(html,/未记录输入版本/);assert.match(html,/NDVI 变化不等于耕地面积变化/);assert.match(html,/@media print/);
});
test('failed runs and invalid map files cannot yield a finished report',()=>{
 assert.throws(()=>renderGISReport({...input,run:{...input.run,status:'failed'}}),/completed/);
 assert.throws(()=>renderGISReport({...input,mapPng:Buffer.from('not an image')}),/PNG/);
});
test('missing statistics remain unknown rather than zero',()=>{
 const html=renderGISReport({...input,stats:{params:{},outputs:[{path:'raster.tif'}]}});assert.match(html,/<td>未记录<\/td>/);assert.ok(!html.includes('<td>0</td>'));
});
