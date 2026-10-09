import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { afterEach, expect, it } from 'vitest';
import userEvent from '@testing-library/user-event';
import { ControlTransportProvider } from '@/app/control-transport';
import { MockControlTransport } from '@/test/mock-transport';
import type { ControlRequest } from '@/platform/transport';
import { PawOsFilesApp } from '@/features/files/PawOsFilesApp';
import { PawContextMenu } from '@/paw-os/shell/PawContextMenu';
afterEach(()=>{cleanup();window.localStorage.clear();});
function Harness({transport,initialRoute}:{transport:MockControlTransport;initialRoute?:string}){
 const [open,setOpen]=useState(false);const ref=useRef<HTMLButtonElement>(null);
 return <QueryClientProvider client={new QueryClient({defaultOptions:{queries:{retry:false}}})}><ControlTransportProvider transport={transport}>
 <button ref={ref} onClick={()=>setOpen(true)} aria-expanded={open}>Files 菜单</button>
 <div className="paw-window-shell" data-active="true" tabIndex={-1}><PawOsFilesApp initialRoute={initialRoute} /></div>
 {open?<PawContextMenu anchor={ref} ariaLabel="Files 菜单" items={[{id:'hide',label:'隐藏窗口',action:()=>{}}]} onClose={()=>setOpen(false)} x={0} y={0}/>:null}
 <button aria-label="External command">External command</button><div className="paw-window-shell" data-active="true"><input aria-label="Other window field" /></div>{createPortal(<button aria-label="Portal command">Portal command</button>,document.body)}
 </ControlTransportProvider></QueryClientProvider>;
}
it.each([false,true])('preserves user menu focus across first listing (late=%s) and original menu focus',async(late)=>{
 let release!:(r:unknown)=>void;
 const result={ok:true,scope:'local',path:'/owned',homePath:'/owned',items:[{path:'/owned/notes.md',name:'notes.md',kind:'file'}]};
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':()=>late?new Promise(resolve=>{release=resolve;}):result}});
 render(<Harness transport={transport}/>);
 if(late)await waitFor(()=>expect(release).toBeDefined());else await screen.findByRole('treeitem',{name:'打开文件 notes.md'});
 const opener=screen.getByRole('button',{name:'Files 菜单'});opener.focus();
 await userEvent.setup().keyboard('{Enter}');
 const menu=screen.getByRole('menu',{name:'Files 菜单'});
 expect(within(menu).getByRole('menuitem',{name:'隐藏窗口'})).toHaveFocus();
 if(late)await act(async()=>release(result));
 expect(menu.contains(document.activeElement)).toBe(true);
});

it.each(['External command','Portal command','Other window field','文件或文件夹路径'])('preserves %s while the first listing arrives',async(name)=>{
 let release!:(r:unknown)=>void;let calls=0;
 const result={ok:true,scope:'local',path:'/owned',homePath:'/owned',items:[{path:'/owned/notes.md',name:'notes.md',kind:'file'}]};
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':()=>calls++===0?new Promise(resolve=>{release=resolve;}):result}});
 render(<Harness transport={transport}/>);await waitFor(()=>expect(release).toBeDefined());
 const control=name==='Other window field'?screen.getByRole('textbox',{name}):name==='文件或文件夹路径'?screen.getByRole('textbox',{name}):screen.getByRole('button',{name});
 control.focus();expect(control).toHaveFocus();
 await act(async()=>release(result));await screen.findByRole('treeitem',{name:'打开文件 notes.md'});expect(control).toHaveFocus();
});
it('promotes the original own-shell focus when no user control intent intervened',async()=>{
 let release!:(r:unknown)=>void;let calls=0;
 const result={ok:true,scope:'local',path:'/owned',homePath:'/owned',items:[]};
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':()=>calls++===0?new Promise(resolve=>{release=resolve;}):result}});
 render(<Harness transport={transport}/>);await waitFor(()=>expect(release).toBeDefined());
 const shell=document.querySelector<HTMLElement>('.paw-window-shell')!;shell.focus();
 await act(async()=>release(result));await waitFor(()=>expect(screen.getByRole('treeitem',{name:/收起工作区 owned/})).toHaveFocus());
});
it('does not let automatic pending root location borrow a menu intent',async()=>{
 const releases:Array<(r:unknown)=>void>=[];const result={ok:true,scope:'local',path:'/owned',homePath:'/owned',items:[]};
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':()=>new Promise(resolve=>{releases.push(resolve);})}});
 render(<Harness transport={transport} initialRoute="/files?path=%2Fowned"/>);await waitFor(()=>expect(releases.length).toBeGreaterThan(0));
 const opener=screen.getByRole('button',{name:'Files 菜单'});opener.focus();await userEvent.setup().keyboard('{Enter}');const menu=screen.getByRole('menu');
 await act(async()=>{for(const release of releases.splice(0))release(result);});
 await screen.findByRole('treeitem',{name:/收起工作区 owned/});expect(menu.contains(document.activeElement)).toBe(true);
});
it.each([false,true])('respects exact pending manual location intent (newer menu=%s)',async(intervene)=>{
 const user=userEvent.setup();let release!:(r:unknown)=>void;
 const listing=(path:string)=>({ok:true,scope:'local',path,homePath:'/owned',items:[]});
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':(r:ControlRequest)=>r.query?.path==='/next'?new Promise(resolve=>{release=resolve;}):listing('/owned')}});
 render(<Harness transport={transport}/>);await screen.findByRole('treeitem',{name:/收起工作区 owned/});
 const input=screen.getByRole('textbox',{name:'文件或文件夹路径'});await user.clear(input);await user.type(input,'/next');await user.click(screen.getByRole('button',{name:'打开路径'}));await waitFor(()=>expect(release).toBeDefined());
 if(intervene){screen.getByRole('button',{name:'Files 菜单'}).focus();await user.keyboard('{Enter}');}
 await act(async()=>release(listing('/next')));await screen.findByRole('treeitem',{name:/收起工作区 next/});
 if(intervene)expect(screen.getByRole('menu').contains(document.activeElement)).toBe(true);else expect(screen.getByRole('treeitem',{name:/收起工作区 next/})).toHaveFocus();
 expect(transport.requests.every(x=>x.request.pathId==='files.list'||x.request.pathId==='agent.sessions.list')).toBe(true);
});

it('keeps newer typing in the same path input while its earlier Enter request completes',async()=>{
 const user=userEvent.setup();let release!:(r:unknown)=>void;
 const listing=(path:string)=>({ok:true,scope:'local',path,homePath:'/owned',items:[]});
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':(r:ControlRequest)=>r.query?.path==='/next'?new Promise(resolve=>{release=resolve;}):listing('/owned')}});
 render(<Harness transport={transport}/>);await screen.findByRole('treeitem',{name:/收起工作区 owned/});
 const input=screen.getByRole('textbox',{name:'文件或文件夹路径'});await user.clear(input);await user.type(input,'/next{Enter}');await waitFor(()=>expect(release).toBeDefined());await user.type(input,'-new-intent');
 const field=input as HTMLInputElement;field.setSelectionRange(2,9);const newer=[field.value,field.selectionStart,field.selectionEnd];
 await act(async()=>release(listing('/next')));await screen.findByRole('treeitem',{name:/收起工作区 next/});expect(input).toHaveFocus();expect([field.value,field.selectionStart,field.selectionEnd]).toEqual(newer);
});

it('keeps a newer field value and selection through the separate root synchronization effect',async()=>{
 let sessions!:(r:unknown)=>void;const user=userEvent.setup();
 const transport=new MockControlTransport({routes:{'agent.sessions.list':()=>new Promise(resolve=>{sessions=resolve;}),'files.list':(r:ControlRequest)=>({ok:true,scope:'local',path:r.query?.path||'/home',homePath:'/home',items:[]})}});
 render(<Harness transport={transport}/>);await waitFor(()=>expect(sessions).toBeDefined());await screen.findByRole('treeitem',{name:/收起工作区 home/});
 const input=screen.getByRole('textbox',{name:'文件或文件夹路径'}) as HTMLInputElement;await user.clear(input);await user.type(input,'/newer-unsubmitted');input.setSelectionRange(3,8);const newer=[input.value,input.selectionStart,input.selectionEnd];
 await act(async()=>sessions({ok:true,activeSessionId:'writer',items:[{id:'writer',title:'writer',updatedAtMs:1,status:'idle',workspaceRoots:['/canonical-workspace']}]}));
 await screen.findByRole('treeitem',{name:/收起工作区 canonical-workspace/});expect([input.value,input.selectionStart,input.selectionEnd]).toEqual(newer);expect(input).toHaveFocus();expect(screen.getByRole('combobox',{name:'选择文件所属 Session'})).toHaveValue('writer');
});
it('updates the canonical path and actual directory for an unchanged submitted input',async()=>{
 const user=userEvent.setup();let release!:(r:unknown)=>void;
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':(r:ControlRequest)=>r.query?.path==='/next/.'?new Promise(resolve=>{release=resolve;}):{ok:true,scope:'local',path:r.query?.path||'/home',homePath:'/home',items:[]}}});
 render(<Harness transport={transport}/>);await screen.findByRole('treeitem',{name:/收起工作区 home/});const input=screen.getByRole('textbox',{name:'文件或文件夹路径'});await user.clear(input);await user.type(input,'/next/.{Enter}');await waitFor(()=>expect(release).toBeDefined());await act(async()=>release({ok:true,scope:'local',path:'/next',homePath:'/home',items:[]}));await screen.findByRole('treeitem',{name:/收起工作区 next/});expect(input).toHaveValue('/next');
});
it('keeps explicit Session selection as a new canonical location intention',async()=>{
 const user=userEvent.setup();const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,activeSessionId:'writer',items:['writer','reader'].map((id,i)=>({id,title:id,updatedAtMs:1,status:'idle',workspaceRoots:[i?'/two':'/one']}))},'files.list':(r:ControlRequest)=>({ok:true,scope:'local',path:r.query?.path||'/home',homePath:'/home',items:[]})}});
 render(<Harness transport={transport}/>);await screen.findByRole('treeitem',{name:/收起工作区 one/});const input=screen.getByRole('textbox',{name:'文件或文件夹路径'});await user.clear(input);await user.type(input,'/old-draft');await user.selectOptions(screen.getByRole('combobox',{name:'选择文件所属 Session'}),'reader');await screen.findByRole('treeitem',{name:/收起工作区 two/});expect(input).toHaveValue('/two');expect(screen.getByRole('combobox',{name:'选择文件所属 Session'})).toHaveValue('reader');
});

it('keeps a newer edit even when its text returns to the originally submitted spelling',async()=>{
 const user=userEvent.setup();let release!:(r:unknown)=>void;
 const transport=new MockControlTransport({routes:{'agent.sessions.list':{ok:true,items:[]},'files.list':(r:ControlRequest)=>r.query?.path==='/next/.'?new Promise(resolve=>{release=resolve;}):{ok:true,scope:'local',path:r.query?.path||'/home',homePath:'/home',items:[]}}});
 render(<Harness transport={transport}/>);await screen.findByRole('treeitem',{name:/收起工作区 home/});const input=screen.getByRole('textbox',{name:'文件或文件夹路径'}) as HTMLInputElement;await user.clear(input);await user.type(input,'/next/.{Enter}');await waitFor(()=>expect(release).toBeDefined());await user.clear(input);await user.type(input,'/next/.');input.setSelectionRange(1,5);
 await act(async()=>release({ok:true,scope:'local',path:'/next',homePath:'/home',items:[]}));await screen.findByRole('treeitem',{name:/收起工作区 next/});expect(input).toHaveValue('/next/.');expect([input.selectionStart,input.selectionEnd]).toEqual([1,5]);expect(input).toHaveFocus();
});
