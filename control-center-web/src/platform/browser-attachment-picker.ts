import type { FilePickOptions } from './transport';
/** Browser-selected bytes go through the same owner-bound import as paste. */
export function pickBrowserAttachments(options: FilePickOptions): Promise<File[]> {
  const owner=options.sessionId ?? options.roomId;
  if(options.purpose!=='attachment'||options.selection==='directory')return Promise.reject(new Error('此选择器用于会话附件；目录请从项目入口选择。'));
  if(!owner||!/^[A-Za-z0-9][A-Za-z0-9:._-]{0,159}$/.test(owner)||(options.sessionId&&options.roomId))return Promise.reject(new Error('请先打开一个有效会话再添加附件。'));
  const maximum=options.maxFiles ?? (options.multiple?8:1);
  if(!Number.isInteger(maximum)||maximum<1||maximum>8)return Promise.reject(new Error('每次最多选择 8 个附件。'));
  if(options.signal?.aborted)return Promise.reject(new DOMException('Aborted','AbortError'));
  return new Promise((resolve,reject)=>{
    const input=document.createElement('input');input.type='file';input.multiple=Boolean(options.multiple);input.accept=options.accepts?.join(',')||'';input.hidden=true;
    let settled=false;
    const finish=(files:File[],error?:Error)=>{if(settled)return;settled=true;input.remove();options.signal?.removeEventListener('abort',abort);error?reject(error):resolve(files);};
    const abort=()=>finish([],new DOMException('Aborted','AbortError'));
    input.addEventListener('change',()=>{const files=Array.from(input.files??[]);finish(files,files.length>maximum?new Error(`最多还能添加 ${maximum} 个附件，请重新选择。`):undefined);},{once:true});
    input.addEventListener('cancel',()=>finish([]),{once:true});
    options.signal?.addEventListener('abort',abort,{once:true});document.body.appendChild(input);
    try{input.click();}catch(error){finish([],error instanceof Error?error:new Error(String(error)));}
  });
}
