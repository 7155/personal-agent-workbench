export function saveProjectLayer(input:{root:string;layerId?:string;expectedRevision?:number;name:string;features:GeoJSON.Feature[];commandId?:string;source?:Record<string,unknown>}): {status:string;projectId:string;layer:Record<string,any>};
export function updateProjectLayerMetadata(input:{root:string;layerId:string;expectedRevision:number;visible?:boolean;remove?:boolean}):Record<string,any>;
