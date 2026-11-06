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


def destination(source):
    stat = source.stat()
    key = hashlib.sha256(f'{source}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()[:24]
    return CACHE / (key + '.mp4')


def status(source):
    target = destination(source)
    with LOCK:
        item = dict(JOBS.get(str(target), {}))
    if target.is_file() and target.stat().st_size > 0:
        return {'state': 'ready', 'size': target.stat().st_size}
    return item or {'state': 'idle'}


def start(source, encoder_busy=lambda: False):
    target = destination(source)
    if target.is_file() and target.stat().st_size > 0:
        return status(source)
    with LOCK:
        item = JOBS.get(str(target))
        if item and item['state'] in ('waiting', 'working'):
            return dict(item)
        if any(job['state'] in ('waiting', 'working') for job in JOBS.values()):
            return {'state': 'busy', 'message': 'Un altro film viene preparato. Riprova tra poco.'}
        JOBS[str(target)] = {'state': 'waiting', 'message': 'In attesa che la GPU sia libera.'}
    threading.Thread(target=_run, args=(source, target, encoder_busy), daemon=True).start()
    return status(source)


def _run(source, target, encoder_busy):
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
        italian = next((s for s in audio if s.get('tags', {}).get('language', '').lower() in ('ita', 'it')), audio[0])
        partial = target.with_name(target.stem + f'.partial-{os.getpid()}-{int(time.time())}.mp4')
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                   '-i', str(source), '-map', '0:v:0', '-map', f"0:{italian['index']}",
                   '-vf', "scale=w='min(1280,iw)':h=-2,format=yuv420p",
                   '-c:v', 'h264_nvenc', '-preset', 'p4', '-cq', '27', '-b:v', '1800k',
                   '-maxrate', '2800k', '-bufsize', '5600k', '-c:a', 'aac', '-b:a', '128k',
                   '-movflags', '+faststart', '-f', 'mp4', str(partial)]
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
