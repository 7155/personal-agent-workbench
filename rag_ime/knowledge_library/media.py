"""Local, bounded media evidence; timestamps refer to the preserved source."""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Callable

from .models import DocumentParseError, ParsedAsset, ParsedBlock, ParsedDocument

# Force container demuxers, rather than trusting a renamed playlist or URL.
_FORMATS = {'.wav': 'wav', '.mp3': 'mp3', '.flac': 'flac', '.ogg': 'ogg', '.m4a': 'mov',
            '.mp4': 'mov', '.mov': 'mov', '.webm': 'matroska', '.mkv': 'matroska'}
AUDIO_EXTENSIONS = frozenset({'.wav', '.mp3', '.flac', '.ogg', '.m4a'})
VIDEO_EXTENSIONS = frozenset(_FORMATS) - AUDIO_EXTENSIONS
MEDIA_EXTENSIONS = frozenset(_FORMATS)
MAX_MEDIA_UNITS = 64
AUDIO_SEGMENT_SECONDS = 30
VIDEO_FRAME_SECONDS = 10
MAX_UNIT_BYTES = 4 * 1024 * 1024


def _run(args: list[str], *, output: Path | None = None) -> bytes:
    try:
        with tempfile.TemporaryFile() as captured:
            result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=captured,
                                    stderr=subprocess.DEVNULL, timeout=30, check=False)
            if result.returncode:
                raise DocumentParseError('local media decoding failed', code='invalid_media')
            if output is not None:
                if not output.is_file() or not 0 < output.stat().st_size <= MAX_UNIT_BYTES:
                    raise DocumentParseError('media unit exceeds output limit or is empty', code='invalid_media')
                return output.read_bytes()
            if captured.tell() > 64 * 1024:
                raise DocumentParseError('media metadata exceeds limit', code='invalid_media')
            captured.seek(0)
            return captured.read()
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DocumentParseError('local media decoding unavailable or timed out', code='media_decode_failed') from exc


def parse_native_media(path: Path, *, supports_images: bool, supports_audio: bool,
                       should_stop: Callable[[], bool] = lambda: False) -> ParsedDocument:
    if path.suffix.lower() not in MEDIA_EXTENSIONS:
        raise DocumentParseError('unsupported media container', code='unsupported_type')
    video = path.suffix.lower() in VIDEO_EXTENSIONS
    if not video and not supports_audio:
        raise DocumentParseError('profile has no native audio encoder', code='audio_embedding_unavailable')
    if video and not supports_images:
        raise DocumentParseError('profile has no native frame encoder', code='video_embedding_unavailable')
    ffmpeg, ffprobe = shutil.which('ffmpeg'), shutil.which('ffprobe')
    if not ffmpeg or not ffprobe:
        raise DocumentParseError('media intake requires local ffmpeg and ffprobe', code='media_dependencies_missing')
    input_args = ['-protocol_whitelist', 'file,pipe', '-f', _FORMATS[path.suffix.lower()], '-i', str(path)]
    try:
        info = json.loads(_run([ffprobe, '-v', 'error', *input_args, '-show_entries',
                               'format=duration:stream=codec_type,width,height,duration', '-of', 'json']))
        duration = float(info['format']['duration'])
        streams = info['streams']
        if not math.isfinite(duration) or duration <= 0 or len(streams) > 64:
            raise ValueError('invalid duration or stream count')
    except (ValueError, KeyError, TypeError) as exc:
        raise DocumentParseError('media duration or streams unavailable', code='invalid_media') from exc
    for stream in streams:
        if stream.get('codec_type') == 'video':
            width, height = stream.get('width', 0), stream.get('height', 0)
            if not isinstance(width, int) or not isinstance(height, int) or not 0 < width * height <= 20_000_000:
                raise DocumentParseError('video frame dimensions exceed limit', code='media_too_large')
    has_audio = any(stream.get('codec_type') == 'audio' for stream in streams)
    has_video = any(stream.get('codec_type') == 'video' for stream in streams)
    if video and not has_video or not video and not has_audio:
        raise DocumentParseError('container lacks expected media stream', code='invalid_media')
    audio_duration = duration
    if has_audio:
        raw_duration = next(stream.get('duration') for stream in streams if stream.get('codec_type') == 'audio')
        try:
            stream_duration = float(raw_duration)
            if math.isfinite(stream_duration) and stream_duration > 0:
                audio_duration = min(duration, stream_duration)
        except (ValueError, TypeError):
            pass  # Container duration is only an upper bound; decoded PCM determines the end.
    # Share the document budget across both lanes. Each lane samples independently.
    audio_enabled = has_audio and supports_audio
    frame_limit = MAX_MEDIA_UNITS // 2 if video and audio_enabled else MAX_MEDIA_UNITS
    audio_limit = MAX_MEDIA_UNITS - frame_limit if video else MAX_MEDIA_UNITS
    assets, blocks = [], []
    with tempfile.TemporaryDirectory(prefix='paw-knowledge-media-') as directory:
        target = Path(directory)
        lanes = [('image', VIDEO_FRAME_SECONDS, frame_limit)] if video else []
        if audio_enabled:
            lanes.append(('audio', AUDIO_SEGMENT_SECONDS, audio_limit))
        for modality, interval, limit in lanes:
            lane_duration = audio_duration if modality == 'audio' else duration
            for index in range(min(limit, math.ceil(lane_duration / interval))):
                if should_stop():
                    raise DocumentParseError('media job cancelled', code='cancelled')
                start = index * interval
                end = min(lane_duration, start + interval) if modality == 'audio' else start
                name = f'{modality}-{index:04d}.{"png" if modality == "image" else "wav"}'
                output = target / name
                args = [ffmpeg, '-v', 'error', '-nostdin', '-threads', '1', '-ss', str(start), *input_args]
                if modality == 'image':
                    args += ['-map', '0:v:0', '-frames:v', '1', '-vf', "scale='min(1024,iw)':'min(1024,ih)':force_original_aspect_ratio=decrease", '-an']
                else:
                    args += ['-map', '0:a:0', '-t', str(end - start), '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', '-vn']
                payload = _run([*args, '-fs', str(MAX_UNIT_BYTES), '-y', str(output)], output=output)
                if modality == 'audio':
                    with wave.open(str(output), 'rb') as audio:
                        seconds = audio.getnframes() / audio.getframerate()
                    if seconds <= 0:
                        if index == 0:
                            raise DocumentParseError('audio stream decoded empty', code='empty_document')
                        break
                    end = start + seconds
                digest = hashlib.sha256(payload).hexdigest()
                assets.append(ParsedAsset(name, 'image/png' if modality == 'image' else 'audio/wav', digest, payload))
                blocks.append(ParsedBlock(modality, '', metadata={
                    'assetSha256': digest, 'assetName': name,
                    'sourcePart': 'video-frame' if modality == 'image' else 'audio-segment',
                    'startSeconds': start, 'endSeconds': end, 'transcriptionApplied': False,
                    'ocrApplied': False, 'timestampKind': 'seek-offset' if modality == 'image' else 'segment-offset',
                }))
    if not blocks:
        raise DocumentParseError('media produced no usable evidence', code='empty_document')
    return ParsedDocument(text=f'[{"Video" if video else "Audio"} source; no transcription]',
        title=path.stem, provider='builtin', provider_version='ffmpeg-media-v1', assets=tuple(assets), blocks=tuple(blocks),
        metadata={'nativeMediaEmbeddings': True, 'textSource': 'source-label', 'durationSeconds': duration, 'audioDurationUpperBoundSeconds': audio_duration if has_audio else None,
                  'mediaUnitLimit': MAX_MEDIA_UNITS, 'mediaUnitCount': len(blocks),
                  'frameIntervalSeconds': VIDEO_FRAME_SECONDS if video else None,
                  'audioSegmentSeconds': AUDIO_SEGMENT_SECONDS if audio_enabled else None,
                  'videoFramesTruncated': video and math.ceil(duration / VIDEO_FRAME_SECONDS) > frame_limit,
                  'audioSegmentsTruncated': audio_enabled and math.ceil(audio_duration / AUDIO_SEGMENT_SECONDS) > audio_limit,
                  'audioOmitted': has_audio and not supports_audio, 'transcriptionApplied': False})


def media_spans(parsed: ParsedDocument) -> list[dict]:
    if not parsed.metadata.get('nativeMediaEmbeddings'):
        return []
    spans = []
    for order, block in enumerate(parsed.blocks):
        metadata = block.metadata
        modality = block.kind
        if modality not in {'image', 'audio'}:
            continue
        start, end = metadata['startSeconds'], metadata['endSeconds']
        spans.append({'content': f'[{metadata["sourcePart"]}: {start:g}–{end:g}s; no transcription]',
            'heading': '', 'page': None, 'provenance': {
                'kind': modality, 'modality': modality, 'assetSha256': metadata['assetSha256'],
                'startSeconds': start, 'endSeconds': end, 'parser': parsed.provider, 'parserVersion': parsed.provider_version,
                'sourceBlocks': [{'id': hashlib.sha256(f'{modality}:{metadata["assetSha256"]}:{start}'.encode()).hexdigest()[:24],
                                  'order': order, 'kind': modality, 'page': None, 'bbox': None, 'metadata': metadata}],
            }})
    return spans
