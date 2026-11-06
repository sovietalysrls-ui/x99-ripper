"""On-demand browser copies: H.264/AAC MP4, stored separately from originals."""
import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import ripper as core

CACHE = Path(os.environ.get('X99_RIPPER_PREVIEW_DIR') or core.STATE / 'previews').expanduser()
LOCK = threading.Lock()
JOBS = {}


def destination(source, audio=None, subtitle=0):
    stat = source.stat()
    variant = '' if audio is None and not subtitle else f':a{audio}:s{subtitle}'
    key = hashlib.sha256(f'{source}:{stat.st_size}:{stat.st_mtime_ns}{variant}'.encode()).hexdigest()[:24]
    return CACHE / (key + '.mp4')


def tracks(source):
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(source)],
                           capture_output=True, text=True, timeout=30, check=True)
    streams = json.loads(probe.stdout)['streams']
    names = {'ita': 'Italiano', 'eng': 'Inglese', 'spa': 'Spagnolo', 'fra': 'Francese', 'deu': 'Tedesco'}
    def describe(items):
        return [{'id': i, 'language': core.lang(s),
                 'label': names.get(core.lang(s), 'Altra lingua') +
                          (f" · {s['channels']} canali" if s.get('channels') else '')}
                for i, s in enumerate(items, 1)]
    audio = [s for s in streams if s.get('codec_type') == 'audio']
    subtitles = [s for s in streams if s.get('codec_type') == 'subtitle']
    return {'audio': describe(audio), 'subtitles': describe(subtitles)}


def validate(source, audio=None, subtitle=0):
    available = tracks(source)
    if not available['audio']:
        raise ValueError('Nessuna traccia audio disponibile.')
    if audio is None:
        audio = next((item['id'] for item in available['audio'] if item['language'] == 'ita'), 1)
    if audio not in [item['id'] for item in available['audio']]:
        raise ValueError('Traccia audio non disponibile.')
    if subtitle not in [0] + [item['id'] for item in available['subtitles']]:
        raise ValueError('Sottotitoli non disponibili.')
    return audio, subtitle


def status(source, audio=None, subtitle=0):
    target = destination(source, audio, subtitle)
    with LOCK:
        item = dict(JOBS.get(str(target), {}))
    if target.is_file() and target.stat().st_size > 0:
        return {'state': 'ready', 'size': target.stat().st_size}
    return item or {'state': 'idle'}


def start(source, encoder_busy=lambda: False, audio=None, subtitle=0):
    audio, subtitle = validate(source, audio, subtitle)
    default_audio, _ = validate(source)
    cache_audio = None if audio == default_audio and subtitle == 0 else audio
    target = destination(source, cache_audio, subtitle)
    if target.is_file() and target.stat().st_size > 0:
        return status(source, cache_audio, subtitle)
    with LOCK:
        item = JOBS.get(str(target))
        if item and item['state'] in ('waiting', 'working'):
            return dict(item)
        if any(job['state'] in ('waiting', 'working') for job in JOBS.values()):
            return {'state': 'busy', 'message': 'Un altro film viene preparato. Riprova tra poco.'}
        JOBS[str(target)] = {'state': 'waiting', 'message': 'In attesa che la GPU sia libera.'}
    threading.Thread(target=_run, args=(source, target, encoder_busy, audio, subtitle), daemon=True).start()
    return status(source, cache_audio, subtitle)


def _run(source, target, encoder_busy, selected_audio, selected_subtitle):
    key = str(target)
    try:
        # The acquisition can continue; wait only for the video encoder queue.
        while encoder_busy():
            time.sleep(2)
        if shutil.disk_usage(CACHE.parent).free < 3 * 2**30:
            raise RuntimeError('Spazio insufficiente per preparare la visione.')
        CACHE.mkdir(parents=True, exist_ok=True)
        probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(source)],
                               capture_output=True, text=True, timeout=30, check=True)
        streams = json.loads(probe.stdout)['streams']
        audio = [s for s in streams if s.get('codec_type') == 'audio']
        if not audio:
            raise RuntimeError('Nessuna traccia audio disponibile.')
        italian = audio[selected_audio - 1]
        partial = target.with_name(target.stem + f'.partial-{os.getpid()}-{int(time.time())}.mp4')
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                   '-i', str(source), '-map', '0:v:0', '-map', f"0:{italian['index']}",
                   '-vf', "scale=w='min(1280,iw)':h=-2,format=yuv420p",
                   '-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '27', '-b:v', '1800k',
                   '-maxrate', '2800k', '-bufsize', '5600k', '-c:a', 'aac', '-b:a', '128k',
                   '-movflags', '+faststart', '-f', 'mp4', str(partial)]
        if selected_subtitle:
            # DVD bitmap subtitles need burning into the browser copy. The MKV keeps soft tracks.
            command = core.HB + ['-i', str(source), '-o', str(partial), '-f', 'av_mp4',
                      '-e', 'nvenc_h264', '--encoder-preset', 'medium', '-q', '27',
                      '-a', str(selected_audio), '-E', 'av_aac', '-B', '128', '-6', 'stereo',
                      '-s', str(selected_subtitle), f'--subtitle-burned={selected_subtitle}',
                      '--subtitle-default=none', '--maxWidth', '1280']
        with LOCK:
            JOBS[key] = {'state': 'working', 'message': 'Preparo il film per il browser con la Quadro P1000.'}
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError('Preparazione non riuscita: ' + result.stderr[-300:].strip())
        partial.rename(target)
        with LOCK:
            JOBS[key] = {'state': 'ready'}
    except Exception as exc:
        with LOCK:
            JOBS[key] = {'state': 'error', 'message': str(exc)}
