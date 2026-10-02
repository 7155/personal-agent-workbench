#!/usr/bin/env python3
"""One frozen, medium coding task through source Lab and real Pi owners."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))

from rag_ime.agent_model_defaults import DEFAULT_AGENT_MODEL_ID, DEFAULT_AGENT_MODEL_PROFILE
from rag_ime.agent_lab.micro import canonical,digest,turn_usage,write_private
from rag_ime.agent_lab.medium import SEED,SPEC,UPDATE,create_workspace,verify_workspace,prepare_checkpoint,verify_delivery,task_seed,task_update,checkpoint_name
from rag_ime.agent_lab.trial_execution import AgentLabTrialApplication
from rag_ime.agent_lab.trials import AgentLabTrialStore

MODEL={'provider':'openai-codex','model':DEFAULT_AGENT_MODEL_ID,'thinkingLevel':'low'}
BUDGET={'maxProviderCallsPerArm':18,'maxObservedTokensPerArm':100000,'maxStages':3,'stageTimeoutSeconds':300,'maxOutputTokens':4096}
WORKSPACE_TOOLS={'workspace_list','workspace_read','workspace_search','workspace_patch','workspace_edit','workspace_write','workspace_shell'}
STAGES=[
    ('storage','Implement ledger/store.py according to SPEC.md. Own the persistent store API; do not implement report.py or cli.py yet. Validate storage behavior with local tests, then write the storage API and next steps into HANDOFF.md.'),
    ('report','Read SPEC.md, HANDOFF.md and the store API. Implement ledger/report.py and ledger/cli.py. Own reporting and CLI; adjust storage only if an actual integration problem requires it. Run smoke.py and focused local tests. Record completed behavior and remaining risks in HANDOFF.md.'),
    ('continuation','The prior Pi Host was stopped after a settled checkpoint. Continue from this workspace and persisted files. Read SPEC.md, HANDOFF.md and UPDATE.md. Implement v2 while preserving all v1 behavior. Integrate and repair any outstanding implementation defects, run tests, perform the checkpoint.sqlite import/replay/export delivery in UPDATE.md, and update HANDOFF.md.'),
]

def tree_hashes(workspace):
    return {str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(workspace.rglob('*')) if p.is_file() and '__pycache__' not in p.parts and p.suffix not in {'.sqlite','.db'} and '-wal' not in p.name and '-shm' not in p.name}

def _pid(runtime):
    # Measurement only; lifecycle mutations always use the public Runtime owner.
    process=getattr(getattr(runtime,'_client',None),'_process',None)
    return process.pid if process is not None else None

def _alive(pid):
    if not pid:return False
    try:os.kill(pid,0);return True
    except ProcessLookupError:return False

class WorkspaceGateway:
    """Bind existing PAW workspace Tools; no custom coding executor."""
    def __init__(self):self.base=None
    def runtime_manifests(self,session):
        return [m for m in self.base.runtime_manifests(session) if m['name'] in WORKSPACE_TOOLS]
    def execute(self,payload):
        if payload.get('tool') not in WORKSPACE_TOOLS:raise ValueError('tool outside this coding experiment')
        from rag_ime.agent_workspace import WorkspaceHarnessError
        try:return self.base.execute(payload)
        except WorkspaceHarnessError as exc:
            # Preserve ordinary rejected/stale tool results for the Agent.
            # Do not turn product validation errors into a dropped HTTP reply.
            return {'ok':False,'error':str(exc),'errorCode':type(exc).__name__}

def preflight_workspace_tools(service,gateway,server,root,storage_format='sqlite'):
    workspace=root/'tool-preflight';workspace.mkdir()
    session=service.create_session({'title':'tool preflight without model','mode':'coordinator','executionMode':'workspace_managed',
        '_internalWorkspaceScopeGrant':True,'workspaceRoots':[str(workspace)],'projectContextEnabled':False,'piSkillsEnabled':False,'codexSkillsEnabled':False})['session']
    manifests=gateway.runtime_manifests(session);names={m['name'] for m in manifests}
    if not {'workspace_read','workspace_write','workspace_shell'}<=names:raise RuntimeError('coding tools missing before paid admission')
    responses=[]
    commands=[
        ('workspace_write',{'op':'apply','path':str(workspace/'probe.txt'),'resourceRevision':'missing','content':'medium-preflight\n'}),
        ('workspace_read',{'op':'read','path':str(workspace/'probe.txt')}),
        ('workspace_shell',{'op':'run','cwd':str(workspace),'command':'python3 -c "print(6 * 7)"','allowNetwork':False,'timeoutSeconds':10}),
    ]
    if storage_format=='jsonl':
        commands.extend([
            ('workspace_write',{'op':'apply','path':str(workspace/'json_probe.py'),'resourceRevision':'missing','content':
                'import os,json\nfrom pathlib import Path\np=Path("probe.jsonl");q=Path("probe.tmp")\nwith q.open("w") as f:\n f.write(json.dumps({"synthetic":True})+"\\n");f.flush();os.fsync(f.fileno())\nos.replace(q,p)\nassert json.loads(p.read_text())=={"synthetic":True}\nprint("atomic JSONL readback passed")\n'}),
            ('workspace_shell',{'op':'run','cwd':str(workspace),'command':'python3 json_probe.py','allowNetwork':False,'timeoutSeconds':10}),
        ])
    for ordinal,(tool,args) in enumerate(commands):
        payload={'schemaVersion':'rag-ime.agent-tool-call.v1','sessionId':session['id'],'tool':tool,'toolCallId':'preflight:'+str(ordinal),'args':args}
        request=urllib.request.Request(server.tool_gateway_url,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json','X-RAG-IME-Agent-Token':server.token})
        with urllib.request.urlopen(request,timeout=25) as reply:response=json.load(reply)
        responses.append(response)
        if response.get('ok') is not True or response.get('result',{}).get('approvalRequired') is True:raise RuntimeError('workspace preflight did not execute')
    if (workspace/'probe.txt').read_text()!='medium-preflight\n':raise RuntimeError('workspace write not applied')
    if '42' not in json.dumps(responses[2]):raise RuntimeError('workspace shell did not run')
    if storage_format=='jsonl' and json.loads((workspace/'probe.jsonl').read_text())!={'synthetic':True}:raise RuntimeError('atomic JSONL preflight failed')
    write_private(root/'tool-preflight.json',{'providerCalls':0,'toolNames':sorted(names),'responses':responses})

class MediumAdapter:
    def __init__(self,root,service,storage_format='sqlite',resume_root=None):
        self.root=Path(root);self.service=service;self.storage_format=storage_format;self.resume_root=resume_root
        self.stages=[(n,p.replace('checkpoint.sqlite',checkpoint_name(storage_format))) for n,p in STAGES]

    def prepare(self,spec,job_id):
        if spec.get('arm') not in {'solo','room'} or set(spec)!={'arm'}:raise ValueError('fixed arm only')
        public={**spec,'taskId':'durable-lab-ledger-'+self.storage_format+'-v1','storageFormat':self.storage_format,'model':MODEL,'budget':BUDGET,'taskSha256':digest({'seed':task_seed(self.storage_format),'update':task_update(self.storage_format),'stages':self.stages})}
        return {'publicSpec':public,'privateInput':{**public,'jobId':job_id}}

    def execute(self,private,observer,cancelled):
        arm=private['arm'];out=self.root/arm;workspace=(self.resume_root/arm/'workspace') if self.resume_root else out/'workspace';service=self.service
        measured={'providerCalls':0,'tokens':0,'estimatedCostUsd':0.0,'costComplete':True}
        result={'schemaVersion':'paw.medium-coding-arm.v1','arm':arm,'stages':[],'model':MODEL,'budget':BUDGET,'beforeHashes':tree_hashes(workspace)}
        error=None;room=None;sid=None;active=None;started=time.monotonic()
        start_index=0
        if self.resume_root:
            prior_path=self.resume_root/arm/'report.json';prior=json.loads(prior_path.read_text())
            if len(prior['stages'])!=1 or prior['stages'][0]['hostCheck']['passed']!=6 or prior['error']!='RuntimeError:observed_budget':raise ValueError('resume requires the exact budget-stopped storage checkpoint')
            if tree_hashes(workspace)!=prior['afterHashes']:raise ValueError('resume workspace changed since the retained checkpoint')
            measured=dict(prior['observed']);measured['costComplete']=False
            result.update(resumedAfterBudgetStop=True,priorReceiptSha256=hashlib.sha256(prior_path.read_bytes()).hexdigest(),
                priorObserved=prior['observed'],priorError=prior['error'],priorWallMs=prior['wallMs'],stages=list(prior['stages']),
                costBoundary='observed catalog estimates; prior in-flight budget cancellation may have unreported usage')
            sessions=json.loads((self.resume_root/arm/'session-bindings.json').read_text())['sessions']
            if arm=='room':room=json.loads((self.resume_root/arm/'room-binding.json').read_text())
            start_index=1
        elif arm=='solo':
            sid=service.create_session({'title':'Lab medium · same Session','mode':'coordinator','executionMode':'workspace_managed',
                '_internalWorkspaceScopeGrant':True,'workspaceRoots':[str(workspace)],'projectContextEnabled':True,
                'piSkillsEnabled':False,'codexSkillsEnabled':False,'modelProfile':DEFAULT_AGENT_MODEL_PROFILE,'thinkingLevel':'low'})['session']['id']
            sessions=[sid]*3
        else:
            room=service.create_room({'title':'Lab medium · durable ledger','routingPolicy':'manual_mentions','executionMode':'workspace_managed','workspaceRoots':[str(workspace)],
                'participants':[{'roleId':role,'roleVersion':'1'} for role in ['companion-present-v1','companion-future-v1','companion-firstlight-v1']]})['room']
            sessions=[p['sessionId'] for p in room['participants']]
            write_private(out/'room-binding.json',room)
        write_private(out/'session-bindings.json',{'sessions':sessions,'workspace':str(workspace),
            'policies':[ {k:service.sessions.get(s).get(k) for k in ['id','mode','executionMode','workspaceRoots','projectContextEnabled','piSkillsEnabled','codexSkillsEnabled']} for s in dict.fromkeys(sessions)]})
        try:
            for ordinal,(name,instruction) in enumerate(self.stages):
                if ordinal<start_index:continue
                if cancelled():raise InterruptedError('cancelled')
                if measured['providerCalls']>=BUDGET['maxProviderCallsPerArm'] or measured['tokens']>=BUDGET['maxObservedTokensPerArm']:
                    raise RuntimeError('arm_budget_exhausted_or_unknown_usage')
                sid=sessions[ordinal];observer.bind_session(sid,cancel=lambda s=sid:service.runtime.abort(s))
                service.runtime.set_model(sid,provider=MODEL['provider'],model_id=MODEL['model'],max_tokens=BUDGET['maxOutputTokens'])
                service.runtime.set_thinking_level(sid,level=MODEL['thinkingLevel'])
                request=private['jobId']+':'+name
                prompt=f'Authorized workspace: {workspace}\nStage {ordinal+1}/3: {name}\n{instruction}\nUse the available native workspace tools to edit real files. Batch related reads and writes, keep outputs short. No additional agents, network, package installation or commits. Keep all work within this workspace. Final prose is not the deliverable; working files and tests are. Stage time limit: 300 seconds.'
                observer.progress(arm+': '+name);print('stage_start',arm,name,flush=True)
                phase_started=time.monotonic()
                admission_pending=True
                if room:
                    admitted=service.post_room_message(room['id'],{'message':prompt,'clientMessageId':request,'participantIds':[room['participants'][ordinal]['id']]})
                    from scripts.run_paw_micro_eval import room_turn_identity
                    tid,client=room_turn_identity(admitted)
                else:
                    admitted=service.prompt(sid,{'message':prompt,'clientMessageId':request})
                    tid=admitted['turnId'];client=request
                write_private(out/(name+'-admission.json'),admitted)
                transcript=service.sessions.get(sid)['sessionFile'];stop_reason=None;last_progress=phase_started
                active={'name':name,'sessionId':sid,'turnId':tid,'transcript':transcript}
                admission_pending=False
                while True:
                    try:
                        settled=service.runtime.await_turn_settled(sid,tid,client_message_id=client,timeout_seconds=5)
                        break
                    except TimeoutError as exc:
                        if str(exc)!='Pi Session turn settlement timed out' and type(exc).__name__!='PiRuntimeSettlementLookupTimeout':raise
                    try:partial=turn_usage(transcript,tid)
                    except (OSError,ValueError,KeyError):partial=None
                    elapsed=time.monotonic()-phase_started
                    if time.monotonic()-last_progress>=20:
                        print('stage_progress',arm,name,round(elapsed),'seconds',partial['providerCalls'] if partial else 0,'settled_model_messages',flush=True);last_progress=time.monotonic()
                    if stop_reason is None:
                        if cancelled():stop_reason='cancelled'
                        elif elapsed>BUDGET['stageTimeoutSeconds']:stop_reason='stage_timeout'
                        elif partial and (measured['providerCalls']+partial['providerCalls']>=BUDGET['maxProviderCallsPerArm'] or measured['tokens']+partial['usage']['totalTokens']>=BUDGET['maxObservedTokensPerArm']):stop_reason='observed_budget'
                        if stop_reason:service.runtime.abort(sid)
                    elif elapsed>BUDGET['stageTimeoutSeconds']+40:raise TimeoutError('abort did not settle')
                from rag_ime.agent_lab.golden_pi import _settled_output
                aggregate=turn_usage(transcript,tid)
                measured['providerCalls']+=aggregate['providerCalls'];measured['tokens']+=aggregate['usage']['totalTokens']
                if 'estimatedCostUsd' in aggregate['usage']:measured['estimatedCostUsd']+=aggregate['usage']['estimatedCostUsd']
                else:measured['costComplete']=False
                active=None
                phase={'name':name,'sessionId':sid,'turnId':tid,'clientMessageId':client,'requestId':request,'promptSha256':hashlib.sha256(prompt.encode()).hexdigest(),
                    **aggregate,'wallMs':round((time.monotonic()-phase_started)*1000,3),'stopReason':stop_reason,'hostPid':_pid(service.runtime)}
                write_private(out/(name+'-settlement.json'),settled)
                try:text,_,receipt_id=_settled_output(settled,sid,tid,client);phase.update(text=text,settlementReceiptId=receipt_id)
                except Exception as exc:phase['completionError']=type(exc).__name__+':'+str(exc)
                phase['hostCheck']=verify_workspace(workspace,'storage' if ordinal==0 else 'base' if ordinal==1 else 'final',self.storage_format)
                phase['fileHashes']=tree_hashes(workspace)
                result['stages'].append(phase);write_private(out/(name+'-result.json'),phase)
                print('stage_done',arm,name,phase['hostCheck']['passed'],phase['hostCheck']['total'],measured,flush=True)
                if stop_reason or phase.get('completionError'):raise RuntimeError(stop_reason or phase['completionError'])
                if ordinal==1:
                    checkpoint=prepare_checkpoint(workspace,self.storage_format)
                    before=_pid(service.runtime);before_hashes=tree_hashes(workspace)
                    db=workspace/checkpoint_name(self.storage_format);db_hash=hashlib.sha256(db.read_bytes()).hexdigest() if db.exists() else None
                    service.runtime.stop()
                    checkpoint.update(hostPidBefore=before,oldHostExited=not _alive(before),filesUnchanged=tree_hashes(workspace)==before_hashes,
                        databaseUnchanged=db_hash is not None and db.exists() and hashlib.sha256(db.read_bytes()).hexdigest()==db_hash,
                        databaseSha256=db_hash,sessionFiles={s:service.sessions.get(s)['sessionFile'] for s in dict.fromkeys(sessions)})
                    result['checkpoint']=checkpoint;write_private(out/'restart-checkpoint.json',checkpoint)
                    print('checkpoint',arm,checkpoint['prepared'],checkpoint['oldHostExited'],flush=True)
                elif room:service.runtime.close_session(sid)
        except Exception as exc:
            error=type(exc).__name__+':'+str(exc)
            if locals().get('admission_pending'):measured['costComplete']=False
            if active:
                measured['costComplete']=False
                try:
                    partial=turn_usage(active['transcript'],active['turnId'])
                    measured['providerCalls']+=partial['providerCalls'];measured['tokens']+=partial['usage']['totalTokens']
                    measured['estimatedCostUsd']+=partial['usage'].get('estimatedCostUsd',0)
                    write_private(out/(active['name']+'-partial.json'),{**active,**partial,'costComplete':False})
                except (OSError,ValueError,KeyError):pass
            if sid:
                try:service.runtime.abort(sid)
                except Exception:pass
        finally:service.runtime.stop()
        final=verify_workspace(workspace,'final',self.storage_format);delivery=verify_delivery(workspace,self.storage_format)
        checkpoint=result.get('checkpoint',{})
        after_pid=result['stages'][-1].get('hostPid') if len(result['stages'])==3 else None
        recovered=bool(checkpoint.get('oldHostExited') and checkpoint.get('filesUnchanged') and checkpoint.get('databaseUnchanged') and after_pid and after_pid!=checkpoint.get('hostPidBefore'))
        result.update(status='failed' if error else 'completed',error=error,qualityVerdict='keep' if not error and final['allPassed'] and delivery['allPassed'] and recovered else 'reject',
            finalChecks=final,delivery=delivery,observed=measured,hostRestartedAndContinued=recovered,completeTaskPassed=bool(not error and final['allPassed'] and delivery['allPassed'] and recovered),
            withinBudget=measured['providerCalls']<=BUDGET['maxProviderCallsPerArm'] and measured['tokens']<=BUDGET['maxObservedTokensPerArm'],wallMs=round((time.monotonic()-started)*1000+result.get('priorWallMs',0),3),afterHashes=tree_hashes(workspace))
        write_private(out/'report.json',result)
        return result

def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output-root',required=True,type=Path);parser.add_argument('--live',action='store_true');parser.add_argument('--storage-format',choices=['sqlite','jsonl'],default='sqlite')
    parser.add_argument('--resume-root',type=Path);parser.add_argument('--max-observed-tokens',type=int,default=100000);parser.add_argument('--max-provider-calls',type=int,default=18);args=parser.parse_args()
    BUDGET.update(maxObservedTokensPerArm=args.max_observed_tokens,maxProviderCallsPerArm=args.max_provider_calls)
    if not 1<=args.max_observed_tokens<=300000 or not 1<=args.max_provider_calls<=40:parser.error('bounded trial budget required')
    resume=args.resume_root.expanduser().resolve(strict=True) if args.resume_root else None
    if resume and json.loads((resume/'frozen.json').read_text())['storageFormat']!=args.storage_format:parser.error('resume storage format must match')
    root=args.output_root.expanduser().resolve()
    if root.exists() or root.is_relative_to(ROOT):parser.error('new output root outside repository required')
    os.umask(0o077);root.mkdir(parents=True)
    stages=[(n,p.replace('checkpoint.sqlite',checkpoint_name(args.storage_format))) for n,p in STAGES]
    if not resume:
        for arm in ('solo','room'):create_workspace(root/arm/'workspace',args.storage_format)
    frozen={'schemaVersion':'paw.medium-coding-contract.v1','storageFormat':args.storage_format,'taskSha256':digest({'seed':task_seed(args.storage_format),'update':task_update(args.storage_format),'stages':stages}),
        'model':MODEL,'budget':BUDGET,'primary':'complete task, then continuity and no regressions; cost is secondary',
        'seedHashes':json.loads((resume/'frozen.json').read_text())['seedHashes'] if resume else tree_hashes(root/'solo/workspace'),'sourceHashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),ROOT/'rag_ime/agent_lab/medium.py',ROOT/'rag_ime/agent_lab/micro.py']},
        'sameStageInstructions':True,'armOrder':['solo','room'],'repeats':1,'candidateTuning':False,'holdoutClaim':False}
    if not resume:assert tree_hashes(root/'solo/workspace')==tree_hashes(root/'room/workspace')
    else:
        if frozen['taskSha256']!=json.loads((resume/'frozen.json').read_text())['taskSha256']:raise ValueError('resume task contract changed')
        frozen.update(resumeRoot=str(resume),priorFrozenSha256=hashlib.sha256((resume/'frozen.json').read_bytes()).hexdigest(),budgetCumulativeAcrossResume=True)
    write_private(root/'frozen.json',frozen)
    if not args.live:print(root);return 0
    from scripts.pi_canary_support import installed_agent_config_dir,stage_openai_codex_oauth,launch_environment
    from rag_ime.managed_pi_runtime import snapshot_managed_pi_runtime
    from rag_ime.pi.config import PiRuntimeConfig
    from rag_ime.agent_service import AgentService
    plist=Path.home()/'Library/LaunchAgents/com.rag-ime.agent-gateway.plist';source=installed_agent_config_dir(plist)
    for key,value in launch_environment(plist).items():os.environ.setdefault(key,value)
    service=None;app=None;server=None;jobs=[]
    try:
        execution_root=resume or root
        stage_openai_codex_oauth(source,execution_root/'config');inst=snapshot_managed_pi_runtime(source.parent.parent)
        cfg=replace(PiRuntimeConfig.from_environment(enabled_default=True),enabled=True,executable=inst.executable,extension_path=inst.extension_path,node_executable=inst.node_executable,
            tools=inst.tools,pi_version=inst.pi_version,installation_error='',agent_dir=execution_root/'config',session_dir=execution_root/'sessions',logs_dir=root/'logs',debug_context_dir=root/'debug-context',protocol_version='2',idle_timeout_seconds=0,max_sessions=4)
        from rag_ime.agent_tools import ControlToolGateway
        from rag_ime.rag_benchmark_agent import RagBenchmarkAgentGatewayServer
        gateway=WorkspaceGateway();server=RagBenchmarkAgentGatewayServer(gateway);server.start()
        service=AgentService(db_path=execution_root/'agent.sqlite',runtime_config=cfg,project=str(execution_root),background_job_execution_owner=False,startup_recovery_enabled=False,wake_scheduler_enabled=False,
            tool_gateway_url=server.tool_gateway_url,tool_gateway_token=server.token)
        gateway.base=ControlToolGateway(sessions=service.sessions,management=object(),core=object(),project=service.project,
            background_jobs=service.background_jobs,configuration_store=service.configuration_store,work_documents=service.work_documents)
        service.bind_tool_manifest_provider(gateway.runtime_manifests)
        service.bind_approval_executor(gateway.base.apply_approval)
        gateway.base.bind_auto_approval_executor(service.auto_approve_pending)
        preflight_workspace_tools(service,gateway,server,root,args.storage_format)
        adapter=MediumAdapter(root,service,args.storage_format,resume)
        app=AgentLabTrialApplication(AgentLabTrialStore(root/'agent.sqlite'),{'medium-coding':adapter},start_workers=False)
        for arm in ('solo','room'):
            admission=app.start('medium:'+root.name+':'+arm,'medium-coding',{'arm':arm});job=app.run_job(admission['job']['jobId'])['job'];jobs.append(job)
            write_private(root/(arm+'-job.json'),job);print('arm_done',arm,job['state'],flush=True)
        write_private(root/'runtime-identity.json',{'piVersion':inst.pi_version,'manifestSha256':inst.manifest_sha256,'execution':'source Lab/AgentService + installed Pi in isolated database'})
    finally:
        if app is not None:app.close()
        if service is not None:service.close()
        if server is not None:server.close()
        ((resume or root)/'config/auth.json').unlink(missing_ok=True)
        write_private(root/'summary.json',{'schemaVersion':'paw.medium-coding-run.v1','jobs':jobs})
    print('receipt',root/'summary.json',flush=True)
    return 0 if len(jobs)==2 and all(j['result'].get('completeTaskPassed') for j in jobs) else 1

if __name__=='__main__':raise SystemExit(main())
