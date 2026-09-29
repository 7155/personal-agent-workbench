import { FileAudio, Film, RefreshCw } from 'lucide-react';
import { useRef, useState } from 'react';
import './rich-conversation.css';

/** Presentation of an already validated media URL. It does not resolve or fetch arbitrary links. */
export function RichMediaPlayer({ source, name, kind = 'audio', transcript = '' }: {
  source: string; name: string; kind?: 'audio' | 'video'; transcript?: string;
}) {
  const media = useRef<HTMLMediaElement | null>(null);
  const [failed, setFailed] = useState('');
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [attempt, setAttempt] = useState(0);
  const broken = failed === source;
  const controls = { controls: true, preload: 'none', src: source,
    onError: () => { setFailed(source); setPlaying(false); },
    onPlay: () => setPlaying(true), onPause: () => setPlaying(false), onEnded: () => setPlaying(false),
    onLoadedMetadata: () => { if (media.current) media.current.playbackRate = speed; } } as const;
  return <figure className="paw-rich-player" data-kind={kind} data-playing={playing || undefined}>
    <figcaption><span className="paw-rich-player__icon">{kind === 'video' ? <Film size={19} /> : <FileAudio size={19} />}</span>
      <span><strong>{name}</strong><small>{kind === 'video' ? '视频' : '音频'} · {broken ? '暂时无法播放' : playing ? '正在播放' : '由你控制播放'}</small></span>
      <select className="paw-rich-player__speed" aria-label="播放速度" value={speed} onChange={event => {
        const value = Number(event.target.value); setSpeed(value); if (media.current) media.current.playbackRate = value;
      }}>{[.75, 1, 1.25, 1.5, 2].map(value => <option key={value} value={value}>{value}×</option>)}</select>
    </figcaption>
    {broken ? <div className="paw-rich-media-failure" role="status"><p>此文件暂时不能在对话内播放。可以重试，或打开原文件。</p>
      <button className="paw-rich-text-action" type="button" onClick={() => { setFailed(''); setAttempt(v => v + 1); }}><RefreshCw size={14} />重试播放</button>
      <a href={source} target="_blank" rel="noopener noreferrer">打开原文件</a>
    </div> : kind === 'video' ? <video key={`${source}:${attempt}`} {...controls} ref={node => { media.current = node; }} playsInline />
      : <audio key={`${source}:${attempt}`} {...controls} ref={node => { media.current = node; }} />}
    {transcript ? <details className="paw-rich-transcript"><summary>查看随文件返回的文字</summary><p>{transcript}</p></details> : null}
  </figure>;
}
