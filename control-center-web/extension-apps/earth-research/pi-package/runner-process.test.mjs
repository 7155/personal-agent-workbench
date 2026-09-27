import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import test from 'node:test';
import { runProcess, runJSONProcess } from './runner-process.mjs';

const options = code => ({ executable: process.execPath, args: ['-e', code], timeoutMs: 3000, graceMs: 40 });
async function waitFor(file) { for (let i=0;i<150;i++) { if(fs.existsSync(file))return; await delay(10); } throw new Error('Child did not become ready'); }

test('reads one structured receipt after logs and sends input without a shell', async () => {
  const result = await runJSONProcess({ ...options(`let text='';process.stdin.on('data',c=>text+=c);process.stdin.on('end',()=>{console.log('library log');console.log(JSON.stringify({status:'completed',input:JSON.parse(text)}));});`), input: JSON.stringify({ text: '$(touch forbidden); 空间数据' }) });
  assert.deepEqual(result, { status:'completed', input:{ text:'$(touch forbidden); 空间数据' } });
});
test('does not accept success printed before a nonzero exit', async () => {
  await assert.rejects(runJSONProcess(options(`console.log('{"status":"completed"}');process.exitCode=9;`)), { code:'runner_exit_failed' });
});
test('keeps structured failures even when a runner exits nonzero', async () => {
  assert.equal((await runJSONProcess(options(`console.log('{"status":"failed","code":"bad_crs"}');process.exitCode=1;`))).code, 'bad_crs');
});
test('rejects unstructured output and startup failure', async () => {
  await assert.rejects(runJSONProcess(options(`console.log('not a receipt');`)), { code:'runner_invalid_receipt' });
  await assert.rejects(runProcess({executable:'/missing-paw-runtime-absolute-path'}), { code:'runner_unavailable' });
});
test('does not launch an already-cancelled request', async () => {
  const controller=new AbortController();controller.abort();
  await assert.rejects(async()=>runProcess({...options('process.exit(0)'),signal:controller.signal}), {code:'execution_cancelled'});
});
test('cancellation terminates a stubborn child before settling', async () => {
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'earth-cancel-test-')), file=path.join(root,'ready');
  const controller=new AbortController(); let pid;
  try {
    const pending=runProcess({...options(`process.on('SIGTERM',()=>{});require('node:fs').writeFileSync(${JSON.stringify(file)},String(process.pid));setInterval(()=>{},100);`),signal:controller.signal});
    const rejected=assert.rejects(pending,{code:'execution_cancelled'});
    await waitFor(file);pid=Number(fs.readFileSync(file,'utf8'));controller.abort();await rejected;
    assert.throws(()=>process.kill(pid,0),{code:'ESRCH'});
  } finally { if(pid){try{process.kill(pid,'SIGKILL');}catch{}} fs.rmSync(root,{recursive:true,force:true}); }
});
test('timeout escalates, and combined stdout/stderr are bounded', async () => {
  await assert.rejects(runProcess({...options(`process.on('SIGTERM',()=>{});setInterval(()=>{},100);`),timeoutMs:100}),{code:'runner_timeout'});
  await assert.rejects(runProcess({...options(`process.stderr.write('x'.repeat(100000));setInterval(()=>{},100);`),maxOutputBytes:1024}),{code:'runner_output_limit'});
});
test('validates limits before spawning', async () => {
  for(const timeoutMs of [0,-1,NaN,Infinity,1.5]) await assert.rejects(async()=>runProcess({...options(''),timeoutMs}),TypeError);
});

test('cancellation also kills descendants whose pipes are already closed', {skip:process.platform!=='linux'?'Uses /proc to distinguish a killed zombie from a running process':false}, async()=>{
  const root=fs.mkdtempSync(path.join(os.tmpdir(),'earth-descendant-test-')), file=path.join(root,'grandchild');
  let pid; const controller=new AbortController();
  try {
    const grandchild=`process.on('SIGTERM',()=>{});require('node:fs').writeFileSync(${JSON.stringify(file)},String(process.pid));setInterval(()=>{},100);`;
    const code=`require('node:child_process').spawn(process.execPath,['-e',${JSON.stringify(grandchild)}],{stdio:'ignore'});setInterval(()=>{},100);`;
    const pending=runProcess({...options(code),signal:controller.signal});const rejected=assert.rejects(pending,{code:'execution_cancelled'});
    await waitFor(file);pid=Number(fs.readFileSync(file,'utf8'));controller.abort();await rejected;
    const live=()=>{try{return !/\) Z /.test(fs.readFileSync(`/proc/${pid}/stat`,'utf8'));}catch(error){if(error.code==='ENOENT')return false;throw error;}};
    for(let i=0;i<100&&live();i++)await delay(10);
    assert.equal(live(),false,'No running descendant may survive cancellation');
  } finally {if(pid){try{process.kill(pid,'SIGKILL');}catch{}}fs.rmSync(root,{recursive:true,force:true});}
});
