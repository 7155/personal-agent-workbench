"""Render a portable, CRS-aligned map from the frozen bytes of a completed run."""
import json
import math
import sys
import textwrap
import unicodedata
from pathlib import Path

import geopandas as gpd
import fiona
import numpy as np
import rasterio
from pyproj import CRS, Geod, Transformer
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.warp import transform_bounds
import matplotlib

matplotlib.use('Agg')
matplotlib.rcParams.update({'font.family': 'DejaVu Sans', 'pdf.fonttype': 42, 'svg.fonttype': 'path'})
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.text import Text


def wrap_label(value, columns):
    """Count CJK glyphs at double width without a system font scan."""
    lines, current, width = [], '', 0
    for character in str(value):
        advance = 2 if unicodedata.east_asian_width(character) in ('W', 'F') else 1
        if character == '\n' or width + advance > columns:
            lines.append(current)
            current, width = '', 0
        if character != '\n':
            current += character
            width += advance
    return '\n'.join(lines + [current])


def select_crs(bounds, requested):
    if requested:
        crs = CRS.from_user_input(requested)
        if not (crs.is_projected or crs.is_geographic):
            raise ValueError('Map CRS must be a projected or geographic CRS')
        return crs
    if not bounds:
        return CRS.from_epsg(4326)
    west, south, east, north = combined_bounds(bounds)
    if north < -80:
        return CRS.from_epsg(3031)
    if south > 84:
        return CRS.from_epsg(3413)
    if east - west > 12 or north - south > 12:
        return CRS.from_epsg(8857)
    longitude, latitude = (west + east) / 2, (south + north) / 2
    zone = min(60, max(1, int((longitude + 180) / 6) + 1))
    return CRS.from_epsg((32600 if latitude >= 0 else 32700) + zone)


def combined_bounds(bounds):
    array = np.asarray(bounds, dtype=float)
    if not np.isfinite(array).all():
        raise ValueError('Map extent contains non-finite coordinates')
    return [float(array[:, 0].min()), float(array[:, 1].min()),
            float(array[:, 2].max()), float(array[:, 3].max())]


def decorations(ax, crs, options):
    """Use WGS84 geodesy for scale and true north, including foot/degree CRSs."""
    left, right = ax.get_xlim()
    bottom, top = ax.get_ylim()
    center_x, center_y = (left + right) / 2, (bottom + top) / 2
    to_wgs84 = Transformer.from_crs(crs, 4326, always_xy=True)
    from_wgs84 = Transformer.from_crs(4326, crs, always_xy=True)
    longitude, latitude = to_wgs84.transform(center_x, center_y)
    geod = Geod(ellps='WGS84')
    result = {'scaleBar': None, 'northArrow': None}
    if not all(math.isfinite(value) for value in (longitude, latitude)) or abs(latitude) >= 89.9:
        result['decorationWarning'] = 'Scale and true north are undefined at this map center.'
        return result
    if options['scaleBar']:
        def metres(span):
            lon1, lat1 = to_wgs84.transform(center_x - span / 2, center_y)
            lon2, lat2 = to_wgs84.transform(center_x + span / 2, center_y)
            return abs(geod.inv(lon1, lat1, lon2, lat2)[2])

        maximum_span = (right - left) * .23
        estimate = metres(maximum_span)
        if math.isfinite(estimate) and estimate > 0:
            power = 10 ** math.floor(math.log10(estimate))
            length = max(value * power for value in (1, 2, 5) if value * power <= estimate)
            low, high = 0., maximum_span
            for _ in range(45):
                middle = (low + high) / 2
                if metres(middle) < length:
                    low = middle
                else:
                    high = middle
            span = (low + high) / 2
            fraction = span / (right - left)
            x, y = .055, .06
            ax.plot([x, x + fraction], [y, y], transform=ax.transAxes, color='#182e2b', linewidth=3, zorder=20)
            ax.plot([x, x + fraction / 2], [y, y], transform=ax.transAxes, color='white', linewidth=1.3, zorder=21)
            for tick in (x, x + fraction / 2, x + fraction):
                ax.plot([tick, tick], [y - .006, y + .006], transform=ax.transAxes, color='#182e2b', linewidth=1, zorder=21)
            label = f'{length / 1000:g} km' if length >= 1000 else f'{length:g} m'
            for position, text, alignment in [(x, '0', 'left'), (x + fraction, label, 'right')]:
                ax.text(position, y + .018, text, transform=ax.transAxes, fontsize=8, ha=alignment,
                        color='#182e2b', bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .85, 'pad': 2}, zorder=22)
            result['scaleBar'] = {'lengthMetres': length, 'label': label, 'method': 'geodesic at map center',
                                  'centerWgs84': [longitude, latitude], 'mapUnits': span}
    if options['northArrow']:
        lon_north, lat_north, _ = geod.fwd(longitude, latitude, 0, 1000)
        north_x, north_y = from_wgs84.transform(lon_north, lat_north)
        angle = math.atan2(north_x - center_x, north_y - center_y)
        box = ax.get_window_extent()
        base = (.93, .82)
        tip = (base[0] + math.sin(angle) * .075 * box.height / box.width,
               base[1] + math.cos(angle) * .075)
        ax.annotate('', xy=tip, xytext=base, xycoords='axes fraction',
                    arrowprops={'arrowstyle': '-|>', 'color': '#182e2b', 'lw': 1.8}, zorder=22)
        ax.text(tip[0], tip[1] + .014, 'N', transform=ax.transAxes, ha='center', va='bottom',
                fontsize=11, fontweight='bold', color='#182e2b', zorder=22,
                bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': .85, 'pad': 1})
        result['northArrow'] = {'direction': 'true north at map center', 'angleDegrees': math.degrees(angle)}
    return result


def render(request):
    root = Path(request['root']).resolve()
    run = request['run']
    target = Path(request['target']).resolve()
    options = request['mapOptions']
    if not target.is_relative_to(root):
        raise ValueError('Delivery must remain inside workspace')
    # Resolve only known font files, including macOS's relocated font assets.
    # Direct FontProperties avoids TTC family-name fallback to placeholder glyphs.
    font_candidates = [Path('/System/Library/Fonts/Supplemental/Arial Unicode.ttf'),
                       Path('/System/Library/Fonts/PingFang.ttc')]
    assets = Path('/System/Library/AssetsV2')
    if assets.exists():
        font_candidates.extend(sorted(assets.glob('com_apple_MobileAsset_Font*/*.asset/AssetData/PingFang.ttc')))
    font_candidates.append(Path('/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc'))
    label_font = next((font for font in font_candidates if font.is_file()), None)
    quality, vectors, rasters, geographic_bounds, features = [], [], [], [], []
    for index, output in enumerate(run.get('outputs', [])):
        relative = Path(output['path']).relative_to(Path('.earth/gis/runs') / run['runId'])
        source = (target / 'run' / relative).resolve()
        if not source.is_relative_to(target / 'run'):
            raise ValueError('Output escaped the frozen run')
        label = output.get('name') or source.name
        if output.get('kind') == 'vector':
            frame = gpd.read_file(source)
            if frame.crs is None:
                raise ValueError('Delivery vector CRS is unknown')
            if not frame.geometry.is_valid.all():
                raise ValueError('Delivery contains invalid geometry')
            table = f'result_{index + 1}'
            frame.to_file(target / 'result.gpkg', layer=table, driver='GPKG', index=False)
            restored = gpd.read_file(target / 'result.gpkg', layer=table)
            if len(restored) != len(frame) or not all(a.equals(b) for a, b in zip(frame.geometry, restored.geometry)):
                raise ValueError('GeoPackage readback mismatch')
            check = {'layer': table, 'kind': 'vector', 'features': len(frame), 'validGeometries': int(frame.geometry.is_valid.sum()),
                     'readback': True, 'crs': str(frame.crs), 'label': label}
            quality.append(check)
            vectors.append((frame, label, check))
            wgs = frame.to_crs(4326)
            if not frame.empty and not frame.geometry.is_empty.all():
                geographic_bounds.append(wgs.total_bounds)
            exported = json.loads(wgs.to_json(drop_id=True))['features']
            if source.suffix.lower() in ('.geojson', '.json'):
                original = json.loads(source.read_text()).get('features', [])
                if len(original) == len(exported):
                    for feature, recorded in zip(exported, original):
                        if 'id' in recorded:
                            feature['id'] = recorded['id']
            elif source.suffix.lower() in ('.gpkg', '.sqlite'):
                with fiona.open(source) as collection:
                    metadata = collection.tags()
                    row_ids = [row.id for row in collection]
                identity_json = metadata.get('PAW_FEATURE_ID_JSON_FIELD')
                identity_display = metadata.get('PAW_FEATURE_ID_FIELD')
                for index, feature in enumerate(exported):
                    properties = feature['properties']
                    identifier = json.loads(properties[identity_json]) if identity_json else properties.get('id', row_ids[index])
                    if isinstance(identifier, str) or type(identifier) in (int, float):
                        feature['id'] = identifier
                    for helper in (identity_json, identity_display):
                        if helper:
                            properties.pop(helper, None)
            features.extend(exported)
        elif output.get('kind') == 'raster':
            with rasterio.open(source) as raster:
                if not raster.crs:
                    raise ValueError('Delivery raster CRS is unknown')
                geographic_bounds.append(transform_bounds(raster.crs, 'EPSG:4326', *raster.bounds))
                check = {'file': source.name, 'kind': 'raster', 'crs': str(raster.crs), 'label': label,
                         'band': 1, 'sourceWidth': raster.width, 'sourceHeight': raster.height}
                quality.append(check)
                rasters.append((source, label, check))
    if not quality:
        raise ValueError('The run has no vector or raster outputs to map')

    crs = select_crs(geographic_bounds, options.get('crs'))
    width_mm, height_mm = {'A4': (210, 297), 'A3': (297, 420), 'Letter': (215.9, 279.4)}[options['paperSize']]
    if options['orientation'] == 'landscape':
        width_mm, height_mm = height_mm, width_mm
    fig = plt.figure(figsize=(width_mm / 25.4, height_mm / 25.4), facecolor='white')
    title = wrap_label(options['title'], int((width_mm - 24) / 3.3))
    subtitle = wrap_label(options['subtitle'], int((width_mm - 24) / 1.8))
    title_lines = len(title.splitlines()) or 1
    subtitle_lines = len(subtitle.splitlines()) or 1
    header_mm = 17 + title_lines * 7 + subtitle_lines * 4
    fig.text(.045, 1 - 10 / height_mm, title, va='top', fontsize=17, fontweight='bold', color='#183f37', linespacing=1.2)
    fig.text(.045, 1 - (12 + title_lines * 7) / height_mm, subtitle, va='top', fontsize=9, color='#53645f', linespacing=1.3)
    portrait = options['orientation'] == 'portrait'
    bottom = .25 if portrait else 24 / height_mm
    ax = fig.add_axes([.09 if portrait else .07, bottom, .83 if portrait else .65, 1 - bottom - header_mm / height_mm])
    ax.set_anchor('N')
    ax.set_facecolor('#f5f8f6')
    handles, map_bounds = [], []
    palette = ['#bf4a21', '#2463a5', '#8a3d8f', '#173f36'] if rasters else ['#287b62', '#b96537', '#4b72a5', '#8a669c', '#8c862d']
    for index, (source, label, check) in enumerate(rasters):
        with rasterio.open(source) as raster, WarpedVRT(raster, crs=crs, resampling=Resampling.nearest) as projected:
            ratio = max(1, projected.width / 1024, projected.height / 1024)
            shape = (max(1, round(projected.height / ratio)), max(1, round(projected.width / ratio)))
            sample = projected.read(1, out_shape=shape, masked=True, resampling=Resampling.nearest)
            sample = np.ma.masked_invalid(sample)
            bounds = list(projected.bounds)
            map_bounds.append(bounds)
            image = ax.imshow(sample, cmap='viridis', extent=(bounds[0], bounds[2], bounds[1], bounds[3]),
                              interpolation='nearest', alpha=1 if index == 0 else .7, zorder=1 + index)
            check.update({'mapCrs': str(crs), 'mapBounds': bounds, 'previewWidth': shape[1], 'previewHeight': shape[0],
                          'previewDownsampled': ratio > 1, 'previewReprojected': CRS(raster.crs) != crs,
                          'validPreviewPixels': int(sample.count())})
            if options['legend']:
                handles.append(Patch(facecolor='#21918c', label=wrap_label(f'{label} (band 1)', 26)))
            if sample.count():
                check['previewRange'] = [float(sample.min()), float(sample.max())]
                if options['legend'] and index < 3:
                    colorbar = fig.add_axes([.74 if portrait else .77, .14 + index * .06 if portrait else .15 + index * .105, .2 if portrait else .17, .012 if portrait else .016])
                    fig.colorbar(image, cax=colorbar, orientation='horizontal')
                    colorbar.tick_params(labelsize=7)
                    colorbar.set_title(wrap_label(label, 25), fontsize=8, loc='left', pad=4)
    for index, (frame, label, check) in enumerate(vectors):
        projected = frame.to_crs(crs)
        color = palette[index % len(palette)]
        kinds = set(projected.geom_type)
        check['mapCrs'] = str(crs)
        if not projected.empty and not projected.geometry.is_empty.all():
            bounds = projected.total_bounds.tolist()
            check['mapBounds'] = bounds
            map_bounds.append(bounds)
            for geometry_types, style in [
                (['Polygon', 'MultiPolygon'], {'facecolor': matplotlib.colors.to_rgba(color, .24), 'edgecolor': color, 'linewidth': 1.4}),
                (['LineString', 'MultiLineString'], {'color': color, 'linewidth': 1.7}),
                (['Point', 'MultiPoint'], {'color': color, 'edgecolor': 'white', 'markersize': 28, 'linewidth': .6}),
                (['GeometryCollection'], {'color': color, 'linewidth': 1.2}),
            ]:
                selection = projected[projected.geom_type.isin(geometry_types)]
                if not selection.empty:
                    selection.plot(ax=ax, zorder=5 + index, **style)
        if options['legend']:
            text = wrap_label(f'{label}' + (' (empty)' if frame.empty else ''), 26)
            if kinds & {'Polygon', 'MultiPolygon'}:
                handles.append(Patch(facecolor=matplotlib.colors.to_rgba(color, .24), edgecolor=color, label=text))
            else:
                handles.append(Line2D([], [], color=color, linewidth=1.7, marker='o' if kinds & {'Point', 'MultiPoint'} else None, label=text))
    if map_bounds:
        west, south, east, north = combined_bounds(map_bounds)
        minimum_span = .0001 if crs.is_geographic else 1.
        dx, dy = max(east - west, minimum_span), max(north - south, minimum_span)
        ax.set_xlim(west - dx * .08, east + dx * .08)
        ax.set_ylim(south - dy * .08, north + dy * .08)
    else:
        ax.text(.5, .5, 'No non-empty output geometry', transform=ax.transAxes, ha='center', fontsize=12)
    ax.set_aspect('equal', adjustable='box')
    ax.tick_params(labelsize=7, colors='#53645f')
    ax.ticklabel_format(useOffset=False, style='plain')
    unit = crs.axis_info[0].unit_name if crs.axis_info else 'map units'
    ax.set_xlabel(f'X / {unit}', fontsize=8, color='#53645f')
    ax.set_ylabel(f'Y / {unit}', fontsize=8, color='#53645f')
    ax.grid(color='#92a49b', alpha=.22, linewidth=.5, linestyle=':')
    for spine in ax.spines.values():
        spine.set_color('#a8b8b0')
    sidebar = fig.add_axes([.07, .08, .27, .13] if portrait else [.755, bottom, .215, 1 - bottom - header_mm / height_mm], frameon=False)
    sidebar.set_axis_off()
    if handles:
        sidebar.legend(handles=handles, loc='upper left', borderaxespad=0, frameon=False, fontsize=8, title='Legend', title_fontsize=10, labelspacing=1.2)
    details = f"CRS: {crs.to_string()}\nRun date: {str(run.get('startedAt', ''))[:10]}\n\nTrue north and scale refer to the map center.\n\nSource: recorded run outputs. No basemap."
    detail_text = '\n'.join(textwrap.fill(line, 34 if portrait else 29) for line in details.split('\n'))
    if portrait:
        fig.text(.39, .205, detail_text, fontsize=8, va='top', linespacing=1.5, color='#53645f')
    else:
        sidebar.text(0, .52 if handles else .98, detail_text, fontsize=8, va='top', linespacing=1.5, color='#53645f')
    fig.text(.045, .035, f"Run: {run['runId']} | Operation: {run['op']}", fontsize=7, color='#53645f')
    fig.text(.045, .019, 'Raster previews may be resampled. Original data and provenance are included in the delivery.', fontsize=7, color='#53645f')
    warnings = []
    font_coverage = font_manager.get_font(str(label_font)).get_charmap() if label_font else {}
    for label in fig.findobj(Text):
        if any(ord(character) > 127 for character in label.get_text()):
            if label_font:
                label.set_fontproperties(font_manager.FontProperties(fname=label_font, size=label.get_fontsize(), weight=label.get_fontweight()))
                if any(ord(character) > 127 and ord(character) not in font_coverage for character in label.get_text()):
                    warnings.append('Some labels contain glyphs unavailable in the local font.')
            elif not warnings:
                warnings.append('No CJK font is installed; non-Latin labels may contain missing glyphs.')
    if options['legend'] and len(rasters) > 3:
        warnings.append('Only the first three raster color ramps fit on this page; all ranges are in quality.json.')
    fig.canvas.draw()
    adornments = decorations(ax, crs, options) if map_bounds else {'scaleBar': None, 'northArrow': None}
    cartography = {**options, 'crs': str(crs), 'widthMm': width_mm, 'heightMm': height_mm,
                   'extent': [ax.get_xlim()[0], ax.get_ylim()[0], ax.get_xlim()[1], ax.get_ylim()[1]],
                   'legendEntries': [{'label': check['label'], 'kind': check['kind']} for check in quality] if options['legend'] else [],
                   'warnings': list(dict.fromkeys(warnings)), **adornments}
    fig.savefig(target / 'map.pdf', metadata={'Title': options['title'], 'Subject': f"GIS run {run['runId']}"})
    fig.savefig(target / 'map.svg')
    fig.savefig(target / 'map.png', dpi=140)
    plt.close(fig)
    (target / 'map.geojson').write_text(json.dumps({'type': 'FeatureCollection', 'features': features}, ensure_ascii=False))
    (target / 'quality.json').write_text(json.dumps({'runId': run['runId'], 'checks': quality, 'cartography': cartography,
        'scope': 'Geometry, format readback, common map CRS and local cartographic scale; business conditions beyond supplied inputs are not checked'}, indent=2, ensure_ascii=False))
    return {'status': 'completed', 'vectorLayers': len(vectors), 'rasterLayers': len(rasters), 'cartography': cartography}


if __name__ == '__main__':
    print(json.dumps(render(json.load(sys.stdin)), ensure_ascii=False))
