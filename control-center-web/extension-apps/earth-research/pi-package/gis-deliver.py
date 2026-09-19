"""Render portable vector/raster deliverables from an already completed run."""
import json
import sys
from pathlib import Path

import geopandas as gpd
import numpy as np
import rasterio
import matplotlib
matplotlib.use('Agg')
matplotlib.rcParams['font.family'] = 'DejaVu Sans'
import matplotlib.pyplot as plt

request=json.load(sys.stdin)
root=Path(request['root']).resolve()
run=request['run']
target=Path(request['target']).resolve()
if not target.is_relative_to(root):
    raise ValueError('Delivery must remain inside workspace')
fig,ax=plt.subplots(figsize=(11.7,8.3))
quality=[]
vector_count=0
plot_crs=None
for index,output in enumerate(run.get('outputs', [])):
    source=(root/output['path']).resolve()
    if not source.is_relative_to(root):
        raise ValueError('Output escaped workspace')
    if output.get('kind')=='vector':
        frame=gpd.read_file(source)
        if frame.crs is None:
            raise ValueError('Delivery vector CRS is unknown')
        if not frame.geometry.is_valid.all():
            raise ValueError('Delivery contains invalid geometry')
        table=f'result_{index+1}'
        frame.to_file(target/'result.gpkg',layer=table,driver='GPKG',index=False)
        restored=gpd.read_file(target/'result.gpkg',layer=table)
        if len(restored)!=len(frame) or not all(a.equals(b) for a,b in zip(frame.geometry,restored.geometry)):
            raise ValueError('GeoPackage readback mismatch')
        quality.append({'layer':table,'features':len(frame),'validGeometries':int(frame.geometry.is_valid.sum()),'readback':True,'crs':str(frame.crs)})
        vector_count+=1
        if not frame.empty:
            bounds=frame.to_crs(4326).total_bounds
            metric='EPSG:3031' if bounds[3]<-80 else 'EPSG:3413' if bounds[1]>84 else frame.estimate_utm_crs()
            if plot_crs is None: plot_crs=metric
            frame.to_crs(plot_crs).plot(ax=ax,facecolor='#b9d9c8',edgecolor='#24634b',linewidth=1.2)
    elif output.get('kind')=='raster' and vector_count==0:
        with rasterio.open(source) as raster:
            sample=raster.read(1,out_shape=(min(1024,raster.height),min(1024,raster.width)),masked=True)
            im=ax.imshow(sample,cmap='viridis',extent=(raster.bounds.left,raster.bounds.right,raster.bounds.bottom,raster.bounds.top))
            fig.colorbar(im,ax=ax,label='Raster value')
            plot_crs=raster.crs
            quality.append({'file':source.name,'previewDownsampled':True,'crs':str(raster.crs)})
ax.set_title(f"GIS analysis / {run['op']}\n{run['runId']}")
ax.set_xlabel(f'Coordinates: {plot_crs or "no non-empty output"}')
ax.annotate('N',xy=(.96,.93),xytext=(.96,.83),xycoords='axes fraction',ha='center',arrowprops={'arrowstyle':'->','color':'#24634b'})
if vector_count and plot_crs:
    xmin,xmax=ax.get_xlim();ymin,ymax=ax.get_ylim();length=(xmax-xmin)*.2
    ax.plot([xmin+(xmax-xmin)*.05,xmin+(xmax-xmin)*.05+length],[ymin+(ymax-ymin)*.06]*2,color='black',linewidth=3)
    ax.text(xmin+(xmax-xmin)*.05,ymin+(ymax-ymin)*.085,f'{length:.0f} m',fontsize=9)
fig.text(.1,.035,f"Run date: {run.get('startedAt','')} | Parameters: {json.dumps(run.get('params',{}),ensure_ascii=True)}",fontsize=8)
fig.text(.1,.015,'Source: recorded run outputs. Geometry only; no basemap. Engineering constraints not supplied remain unchecked.',fontsize=8)
fig.tight_layout(rect=[0,.06,1,1])
fig.savefig(target/'map.pdf');fig.savefig(target/'map.svg');plt.close(fig)
(target/'quality.json').write_text(json.dumps({'runId':run['runId'],'checks':quality,'scope':'geometry, format readback and declared CRS; business conditions beyond supplied inputs are not checked'},indent=2))
print(json.dumps({'status':'completed','vectorLayers':vector_count,'quality':quality}))
