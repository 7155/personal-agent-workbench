export function snapshotGISInputs(root: string, runDir: string, inputs?: Record<string,string>): {bindings:Record<string,string>;versions:Array<Record<string,unknown>>};
export function listGISRuns(input:{root:string}): {status:string;runs:Array<Record<string,any>>};
export function readGISRun(input:{root:string;runId:string}): Record<string,any>;
export interface GISMapOptions { title?: string; subtitle?: string; paperSize?: 'A4' | 'A3' | 'Letter'; orientation?: 'landscape' | 'portrait'; crs?: string; legend?: boolean; scaleBar?: boolean; northArrow?: boolean }
export function createGISBundle(input:{root:string;runId:string;name?:string;version?:number;include?:string[];mapOptions?:GISMapOptions}): {status:string;path:string;manifest:Record<string,any>;verification:Record<string,any>};
export function verifyGISBundle(input:{root:string;path:string}): Record<string,any>;
export function compareGISRuns(input:{root:string;firstRunId:string;secondRunId:string}): Record<string,any>;
