import { LayoutGrid, ArrowUpRight, ChevronLeft, ChevronRight, Columns2, Image as ImageIcon, Maximize2, Minus, Plus, RefreshCw } from 'lucide-react';
import type { ReactNode } from 'react';
import { imageBytesLabel, imageDimensions, imageKey, imageOriginLabel, imageSizeLabel, type GalleryImage, type ImageLoadMap, type ImageViewerMode } from './image-gallery-model';
import './image-gallery.css';

export interface GalleryViewProps {
  items: readonly GalleryImage[]; loads: ImageLoadMap; retries: Readonly<Record<string, number>>;
  limit: number; compact?: boolean; motion?: boolean;
  layout?: 'grid' | 'focus'; focusId?: string;
  onLayout?: (layout: 'grid' | 'focus') => void; onFocus?: (id: string) => void;
  onOpen: (id: string) => void; onMore: () => void;
  onLoad: (image: GalleryImage, width: number, height: number) => void;
  onError: (image: GalleryImage) => void; onRetry: (image: GalleryImage) => void;
}
export function GalleryImageElement({ image, loads, retries, onLoad, onError, lazy = true, style }: {
  image: GalleryImage; loads: ImageLoadMap; retries: Readonly<Record<string, number>>;
  onLoad: GalleryViewProps['onLoad']; onError: GalleryViewProps['onError']; lazy?: boolean;
  style?: import('react').CSSProperties;
}) {
  const key = imageKey(image); const failed = loads[key]?.state === 'failed';
  if (!image.source || failed) return <span className="paw-image-unavailable"><ImageIcon size={26} aria-hidden /><span>{image.source ? '图片未能加载' : '图片回执不可用'}</span></span>;
  return <img key={`${key}:${retries[key] || 0}`} src={image.source} alt={image.alt || image.name} loading={lazy ? 'lazy' : 'eager'}
    decoding="async" draggable={false} referrerPolicy="no-referrer" data-loaded={loads[key]?.state === 'loaded'}
    width={image.width} height={image.height} style={style}
    onLoad={event => onLoad(image, event.currentTarget.naturalWidth, event.currentTarget.naturalHeight)} onError={() => onError(image)} />;
}
export function ImageGalleryView(props: GalleryViewProps) {
  const { items, loads, limit, compact = false } = props;
  const focus = !compact && props.layout === 'focus' && Boolean(props.onFocus) && items.length > 1;
  const current = items.find(image => image.id === props.focusId) ?? items[0];
  const position = items.findIndex(image => image.id === current?.id);
  const tile = (image: GalleryImage, featured = false) => <span className="paw-image-gallery__item" key={image.id}>
    <button type="button" className={`paw-image-gallery__tile${featured ? ' paw-image-gallery__tile--focus' : ''}`}
      data-image-id={image.id} onClick={() => props.onOpen(image.id)} aria-label={`查看图片 ${image.name}`}>
      <GalleryImageElement image={image} {...props} />
      {image.source && loads[imageKey(image)]?.state !== 'failed' ? <span className="paw-image-gallery__enlarge"><Maximize2 size={14} aria-hidden/><span>打开原图</span></span> : null}
    </button>
    {!compact ? <span className="paw-image-gallery__caption"><strong title={image.name}>{image.name}</strong><small>{imageSizeLabel(image, loads)}{image.byteSize !== undefined ? ` · ${imageBytesLabel(image.byteSize)}` : ''}</small><span>{imageOriginLabel(image)}</span>
      {loads[imageKey(image)]?.state === 'failed' && image.source ? <button type="button" className="paw-image-action" onClick={() => props.onRetry(image)}><RefreshCw size={12} aria-hidden/>重试预览</button> : null}
    </span> : null}
  </span>;
  return <span className="paw-image-gallery" data-count={items.length === 1 ? 'single' : 'multiple'} data-compact={compact} data-layout={focus ? 'focus' : 'grid'} data-motion={props.motion !== false}>
    {!compact && items.length > 1 ? <span className="paw-image-gallery__head"><span><ImageIcon size={14} aria-hidden/>图片 <strong>{items.length}</strong></span>
      {props.onLayout && props.onFocus ? <span className="paw-image-layout" role="group" aria-label="图片浏览布局"><button type="button" aria-pressed={!focus} onClick={() => props.onLayout?.('grid')}><LayoutGrid size={13} aria-hidden/>总览</button><button type="button" aria-pressed={focus} onClick={() => props.onLayout?.('focus')}><Maximize2 size={13} aria-hidden/>聚焦</button></span> : <small>逐张查看或并排对照</small>}
    </span> : null}
    {focus && current ? <span className="paw-image-focus">
      {tile(current, true)}
      <span className="paw-image-focus__navigation"><small>逐张浏览 <b>{String(position + 1).padStart(2, '0')}</b> / {String(items.length).padStart(2, '0')}</small><span><button className="paw-image-icon" type="button" aria-label="聚焦上一张" disabled={position <= 0} onClick={() => props.onFocus?.(items[position - 1]!.id)}><ChevronLeft size={16}/></button><button className="paw-image-icon" type="button" aria-label="聚焦下一张" disabled={position >= items.length - 1} onClick={() => props.onFocus?.(items[position + 1]!.id)}><ChevronRight size={16}/></button></span></span>
      <span className="paw-image-focus__reel" aria-label="选择聚焦图片">{items.map((image, index) => <button type="button" key={image.id} aria-pressed={image.id === current.id} aria-label={`聚焦第 ${index + 1} 张 ${image.name}`} onClick={() => props.onFocus?.(image.id)}><span><GalleryImageElement image={image} {...props}/></span><small>{String(index + 1).padStart(2, '0')}</small></button>)}</span>
    </span> : <span className="paw-image-gallery__grid">{items.slice(0, limit).map(image => tile(image))}</span>}
    {!focus && items.length > limit ? <button type="button" className="paw-image-gallery__more" onClick={props.onMore}>显示其余 {items.length - limit} 张图片</button> : null}
  </span>;
}
export interface ImageViewerViewProps extends Pick<GalleryViewProps, 'items' | 'loads' | 'retries' | 'onLoad' | 'onError' | 'onRetry' | 'motion'> {
  currentId: string; mode: ImageViewerMode; compareIds: readonly [string, string]; zoom: number;
  onCurrent: (id: string) => void; onMode: (mode: ImageViewerMode) => void;
  onCompare: (slot: 0 | 1, id: string) => void; onZoom: (zoom: number) => void;
  onOriginal?: (image: GalleryImage) => void; additionalDetails?: ReactNode;
  backdrop?: 'paper' | 'ink' | 'checker'; onBackdrop?: (value: 'paper' | 'ink' | 'checker') => void;
}
export function ImageViewerView(props: ImageViewerViewProps) {
  const { items, loads, currentId, mode, compareIds, zoom } = props;
  const current = items.find(image => image.id === currentId) ?? items[0];
  if (!current) return null;
  const position = items.findIndex(image => image.id === current.id);
  const dimensions = imageDimensions(current, loads);
  const haveDimensions = Boolean(dimensions.width && dimensions.height);
  const blocked = !current.source || loads[imageKey(current)]?.state === 'failed';
  const navigate = (delta: number) => { const target = items[position + delta]; if (target) props.onCurrent(target.id); };
  return <div className="paw-image-viewer" data-motion={props.motion !== false} data-mode={mode} data-backdrop={props.backdrop ?? 'paper'}
    onKeyDown={event => {
      if (event.altKey || event.ctrlKey || event.metaKey || (event.target as HTMLElement).closest('input,select,textarea,[contenteditable=true]')) return;
      if (mode === 'single' && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) { event.preventDefault(); event.stopPropagation(); navigate(event.key === 'ArrowLeft' ? -1 : 1); }
      else if (mode === 'single' && (event.key === '0' || event.key === '1' || event.key === '+' || event.key === '=' || event.key === '-')) {
        if (!haveDimensions && event.key !== '0') return;
        event.preventDefault(); event.stopPropagation(); props.onZoom(event.key === '0' ? 0 : event.key === '1' ? 1 : (zoom || .75) + (event.key === '-' ? -.25 : .25));
      }
    }}>
    <div className="paw-image-viewer__toolbar">
      <div className="paw-image-viewer__switch" role="group" aria-label="图片阅读方式"><button type="button" aria-pressed={mode === 'single'} onClick={() => props.onMode('single')}><ImageIcon size={14} aria-hidden />查看</button><button type="button" aria-pressed={mode === 'compare'} disabled={items.length < 2} onClick={() => props.onMode('compare')}><Columns2 size={14} aria-hidden />对照</button></div>
      {mode === 'single' ? <><span className="paw-image-viewer__counter">{position + 1}<i>/</i>{items.length}</span><div className="paw-image-viewer__zoom" role="group" aria-label="图片缩放">
        <button type="button" className="paw-image-action" aria-pressed={zoom === 0} onClick={() => props.onZoom(0)}>适应</button>
        <button type="button" className="paw-image-action" disabled={!haveDimensions || blocked} aria-pressed={zoom === 1} onClick={() => props.onZoom(1)}>原尺寸</button>
        <button type="button" className="paw-image-icon" aria-label="缩小图片" disabled={!haveDimensions || blocked || zoom !== 0 && zoom <= .25} onClick={() => props.onZoom((zoom || .75) - .25)}><Minus size={15} /></button>
        <output>{zoom === 0 ? '适应视口' : `${Math.round(zoom * 100)}%`}</output>
        <button type="button" className="paw-image-icon" aria-label="放大图片" disabled={!haveDimensions || blocked || zoom >= 4} onClick={() => props.onZoom((zoom || .75) + .25)}><Plus size={15} /></button>
      </div></> : <span className="paw-image-viewer__compare-hint">独立适应画面，不进行配准或像素比较</span>}
      {props.onBackdrop ? <span className="paw-image-backdrops" role="group" aria-label="图片画布背景"><small>画布</small>{([
        ['paper', '浅色画布'], ['ink', '深色画布'], ['checker', '透明网格画布'],
      ] as const).map(([value, label]) => <button type="button" key={value} aria-label={label} title={label} aria-pressed={(props.backdrop ?? 'paper') === value} data-backdrop={value} onClick={() => props.onBackdrop?.(value)}><span aria-hidden/></button>)}</span> : null}
    </div>
    {mode === 'single' ? <div className="paw-image-viewer__stage">
      {items.length > 1 ? <button type="button" className="paw-image-viewer__prev paw-image-icon" aria-label="上一张图片" disabled={position === 0} onClick={() => navigate(-1)}><ChevronLeft size={19} /></button> : null}
      <div className="paw-image-viewer__viewport" data-fit={zoom === 0 || !haveDimensions} role="region" aria-label="可滚动的图片画面" tabIndex={0}>
        <GalleryImageElement {...props} image={current} lazy={false} style={zoom && dimensions.width ? { width: dimensions.width * zoom, height: 'auto', maxWidth: 'none', maxHeight: 'none' } : undefined} />
        {blocked ? <div className="paw-image-viewer__failure"><p>{current.source ? '原回执仍保留，可以重新加载预览。' : '未获得可读取的图片地址。不会自动改用外部图片。'}</p>{current.source ? <button type="button" className="paw-image-action" onClick={() => props.onRetry(current)}><RefreshCw size={14} />重新加载</button> : null}</div> : null}
      </div>
      {items.length > 1 ? <button type="button" className="paw-image-viewer__next paw-image-icon" aria-label="下一张图片" disabled={position === items.length - 1} onClick={() => navigate(1)}><ChevronRight size={19} /></button> : null}
    </div> : <div className="paw-image-viewer__comparison">{([0, 1] as const).map(slot => {
      const image = items.find(item => item.id === compareIds[slot]) ?? items[slot] ?? current;
      return <section key={slot}><label><strong>{slot === 0 ? 'A' : 'B'}</strong><select aria-label={`对照图片 ${slot === 0 ? 'A' : 'B'}`} value={image.id} onChange={event => props.onCompare(slot, event.currentTarget.value)}>{items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></label><div><GalleryImageElement {...props} image={image} lazy={false} /></div><footer><span>{imageSizeLabel(image, loads)}</span><span>{imageOriginLabel(image)}</span></footer></section>;
    })}</div>}
    {mode === 'single' && items.length > 1 ? <div className="paw-image-viewer__filmstrip" aria-label="图片缩略图列表">{items.map((image, index) => <button type="button" key={image.id} title={image.name} aria-label={`选择第 ${index + 1} 张图片 ${image.name}`} aria-pressed={current.id === image.id} onClick={() => props.onCurrent(image.id)}>{image.source && loads[imageKey(image)]?.state !== 'failed' ? <img src={image.source} alt="" loading="lazy" draggable={false} referrerPolicy="no-referrer" /> : <ImageIcon size={17} aria-hidden />}<span>{index + 1}</span></button>)}</div> : null}
    {mode === 'single' ? <div className="paw-image-viewer__info"><div><strong>{current.name}</strong>{current.caption ? <p>{current.caption}</p> : null}<small>{imageSizeLabel(current, loads)}{current.mimeType ? ` · ${current.mimeType}` : ''}{current.byteSize !== undefined ? ` · ${imageBytesLabel(current.byteSize)}` : ''}</small></div><span>{imageOriginLabel(current)}</span>
      {props.onOriginal && current.source ? <button type="button" className="paw-image-action" onClick={() => props.onOriginal?.(current)}><ArrowUpRight size={14} aria-hidden />独立窗口</button> : null}
      <details><summary>图片来源与回执</summary><dl><div><dt>来源</dt><dd>{imageOriginLabel(current)}</dd></div><div><dt>标识</dt><dd><code>{current.id}</code></dd></div>{current.receipt ? <div><dt>回执</dt><dd><code>{current.receipt}</code></dd></div> : null}</dl><p>图片加载成功仅表示预览可读，不代表模型已经阅读或任务已经验收。</p>{props.additionalDetails}</details>
    </div> : null}
  </div>;
}
