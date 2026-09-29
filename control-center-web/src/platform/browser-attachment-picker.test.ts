import {afterEach,it,expect,vi} from 'vitest';
import {pickBrowserAttachments} from './browser-attachment-picker';
import {HttpControlTransport} from './http-transport';
afterEach(()=>{vi.restoreAllMocks();document.body.replaceChildren();});
it('opens the native browser file dialog and imports files into the selected Session',async()=>{
 const file=new File(['test'],'demo.txt',{type:'text/plain'});
 vi.spyOn(HTMLInputElement.prototype,'click').mockImplementation(function(this:HTMLInputElement){Object.defineProperty(this,'files',{value:[file]});this.dispatchEvent(new Event('change'));});
 const transport=new HttpControlTransport({baseUrl:'http://127.0.0.1:8770'});
 const imported=[{id:'media',name:'demo.txt',mimeType:'text/plain',byteSize:4}];const paste=vi.spyOn(transport,'pasteImages').mockResolvedValue(imported);
 expect(await transport.pickFiles({purpose:'attachment',sessionId:'agent:demo',multiple:true,maxFiles:8})).toEqual(imported);
 expect(paste).toHaveBeenCalledWith({files:[file],sessionId:'agent:demo',maxFiles:8});expect(document.querySelector('input')).toBeNull();
});
it('cancel does not import or send a message; owner and count are checked',async()=>{
 vi.spyOn(HTMLInputElement.prototype,'click').mockImplementation(function(this:HTMLInputElement){this.dispatchEvent(new Event('cancel'));});
 expect(await pickBrowserAttachments({purpose:'attachment',roomId:'room:demo'})).toEqual([]);
 await expect(pickBrowserAttachments({purpose:'attachment'})).rejects.toThrow('有效会话');
 await expect(pickBrowserAttachments({purpose:'attachment',sessionId:'s',roomId:'r'})).rejects.toThrow('有效会话');
 await expect(pickBrowserAttachments({purpose:'attachment',sessionId:'s',maxFiles:9})).rejects.toThrow('8');
});
