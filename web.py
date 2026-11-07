#!/usr/bin/env python3
"""Small browser controller for the OptiPlex DVD workflow."""
import html
import hashlib
import hmac
from datetime import datetime
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ripper as core
import film_library
import film_preview
import recover
import duplicates

BASE = '/ripper'
STATE = core.STATE / 'web-state.json'
TOKEN = secrets.token_urlsafe(24)
PORT = int(os.environ.get('X99_RIPPER_WEB_PORT', '8766'))
HOST = os.environ.get('X99_RIPPER_WEB_HOST', '127.0.0.1')
TAILSCALE_HOST = os.environ.get('X99_RIPPER_TAILSCALE_HOST', '')
ALLOWED_PROXY = os.environ.get('X99_RIPPER_ALLOWED_PROXY', '')
ALLOWED_LAN_CLIENTS = {address.strip() for address in
                       os.environ.get('X99_RIPPER_ALLOWED_LAN_CLIENTS', '').split(',') if address.strip()}
TELEGRAM_TOKEN = os.environ.get('X99_RIPPER_TELEGRAM_TOKEN', '')
TELEGRAM_CHAT = os.environ.get('X99_RIPPER_TELEGRAM_CHAT_ID', '')
DESKTOP_MEDIA_CACHE = {}
COMPLETED_CUSTOM = bool(os.environ.get('X99_RIPPER_COMPLETED_DIR'))
COMPLETED_DIR = Path(os.environ.get('X99_RIPPER_COMPLETED_DIR') or core.ROOT).expanduser().resolve()
AUTO_CATALOG_LOCK = threading.Lock()
STORAGE_LOCK = threading.Lock()
STORAGE_CACHE = (0, None)
UPLOAD_LOCK = threading.Lock()
SOURCE_RETENTION_SECONDS = 48 * 3600
READ_ATTEMPTS = 3
READ_RETRY_DELAY = 20


def has_disc_read_error(message):
    text = message.lower()
    return any(marker in text for marker in ('medium error', 'uncorrectable error',
                                           'i/o error', 'read error'))


def archive_failed_acquisition(folder, attempt):
    """Preserve incomplete MKVs before another MakeMKV attempt in the same folder."""
    archived = []
    for path in folder.glob('*.mkv'):
        if path.is_symlink() or 'interrotto-' in path.name.lower(): continue
        target = path.with_name(path.stem + f'.interrotto-{attempt}-{int(time.time())}.mkv')
        if target.exists(): raise FileExistsError(target)
        path.rename(target)
        archived.append(target)
    return archived
STREAM_SECRET_FILE = core.STATE / 'stream-secret'
STREAM_SECRET_LOCK = threading.Lock()
STREAM_SECRET = None


def stream_secret():
    global STREAM_SECRET
    with STREAM_SECRET_LOCK:
        if STREAM_SECRET is None:
            STREAM_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                with STREAM_SECRET_FILE.open('xb') as output:
                    os.chmod(STREAM_SECRET_FILE, 0o600)
                    output.write(os.urandom(32))
            except FileExistsError:
                pass
            STREAM_SECRET = STREAM_SECRET_FILE.read_bytes()
        return STREAM_SECRET


def signed_stream_url(relative, now=None):
    if not TAILSCALE_HOST:
        return ''
    expiry = int(now or time.time()) + 24 * 3600
    name = relative.as_posix() if isinstance(relative, Path) else relative
    message = f'{name}|{expiry}'.encode()
    signature = hmac.new(stream_secret(), message, hashlib.sha256).hexdigest()
    query = urllib.parse.urlencode({'path': name, 'expires': expiry, 'signature': signature})
    return f'http://{TAILSCALE_HOST}:{PORT}{BASE}/films/stream?{query}'


def authorized_stream(query):
    params = urllib.parse.parse_qs(query)
    try:
        name = params['path'][0]
        expiry = int(params['expires'][0])
        signature = params['signature'][0]
    except (KeyError, ValueError, IndexError):
        return None
    now = int(time.time())
    if not now <= expiry <= now + 24 * 3600 or not re.fullmatch(r'[0-9a-f]{64}', signature):
        return None
    expected = hmac.new(stream_secret(), f'{name}|{expiry}'.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        return None
    return completed_file(name)


def completed_file(relative):
    """Resolve a final MKV inside the configured directory, never a source or link."""
    name = Path(relative)
    if name.is_absolute() or '..' in name.parts or not name.parts:
        return None
    candidate = COMPLETED_DIR / name
    if candidate.is_symlink() or not candidate.is_file():
        return None
    resolved = candidate.resolve()
    if not resolved.is_relative_to(COMPLETED_DIR):
        return None
    if resolved.suffix.lower() != '.mkv':
        return None
    if not COMPLETED_CUSTOM and not resolved.name.endswith('_H265.mkv'):
        return None
    return resolved


def completed_files():
    if not COMPLETED_DIR.is_dir():
        return []
    files = []
    for path in COMPLETED_DIR.rglob('*.mkv'):
        relative = path.relative_to(COMPLETED_DIR)
        if completed_file(relative) == path.resolve():
            files.append(path)
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def measure_storage(root, preview_dir):
    """Account for DiscFlow files on the media filesystem without double counting."""
    device = root.stat().st_dev
    totals = {'sources': 0, 'work': 0, 'final': 0, 'iso': 0}
    seen = set()
    def add(path, kind):
        try:
            if path.is_symlink() or not path.is_file(): return
            stat = path.stat()
            key = (stat.st_dev, stat.st_ino)
            if stat.st_dev != device or key in seen: return
            seen.add(key)
            totals[kind] += stat.st_size
        except OSError:
            pass
    for path in root.rglob('*'):
        if path.parent.name == '.uploads' and path.suffix == '.part':
            add(path, 'work'); continue
        if re.fullmatch(r'.+_import_[0-9a-f]{10}', path.parent.name) and path.name.startswith('Originale_'):
            add(path, 'sources'); continue
        if path.suffix.lower() == '.iso':
            add(path, 'iso'); continue
        if path.suffix.lower() not in ('.mkv', '.mp4', '.avi', '.mov', '.mpeg', '.mpg',
                                       '.m2ts', '.mts', '.ts', '.webm', '.wmv', '.vob'): continue
        name = path.name.lower()
        if name.endswith('_h265.mkv'):
            add(path, 'final')
        elif any(part in name for part in ('parziale', 'partial', 'interrotto', 'precedente', 'repaired')):
            add(path, 'work')
        else:
            add(path, 'sources')
    if preview_dir.is_dir():
        for path in preview_dir.rglob('*.mp4'):
            add(path, 'work')
    totals['free'] = shutil.disk_usage(root).free
    return totals


def storage_breakdown():
    global STORAGE_CACHE
    with STORAGE_LOCK:
        if time.monotonic() - STORAGE_CACHE[0] < 15 and STORAGE_CACHE[1] is not None:
            return STORAGE_CACHE[1]
        measured = measure_storage(core.ROOT, film_preview.CACHE)
        STORAGE_CACHE = (time.monotonic(), measured)
        return measured


def film_data():
    result = []
    for path in completed_files():
        info = film_library.read(path)
        local_poster = film_library.poster(path)
        cover_url = (BASE + '/films/poster?path=' + urllib.parse.quote(path.relative_to(COMPLETED_DIR).as_posix())
                     + '&v=' + str(local_poster.stat().st_mtime_ns)) if local_poster else info.get('cover_url', '')
        result.append({'path': path.relative_to(COMPLETED_DIR).as_posix(),
                       'title': info.get('title') or path.stem.removesuffix('_H265').replace('_', ' '),
                       'year': info.get('year'), 'director': info.get('director', []),
                       'cast': info.get('cast', []), 'genres': info.get('genres', []),
                       'imdb_id': info.get('imdb_id', ''), 'wikidata_id': info.get('wikidata_id', ''),
                       'cover_url': cover_url, 'cover_source': 'Copertina personale' if local_poster else info.get('cover_source', ''),
                       'manual_cover': bool(local_poster),
                       'version_label': info.get('version_label', ''),
                       'source': info.get('source', ''), 'filename': path.name,
                       'stream_url': signed_stream_url(path.relative_to(COMPLETED_DIR)),
                       'size': path.stat().st_size, 'modified': path.stat().st_mtime})
    return result


def catalog_automatically(paths=None):
    """Use clear title/runtime matches; leave ambiguous discs for the film page."""
    if not AUTO_CATALOG_LOCK.acquire(blocking=False):
        return
    try:
        for path in paths or completed_files():
            if (not path.is_file() or film_library.read(path) or (path.parent / 'movie.nfo').exists()
                    or film_preview.status(path)['state'] in ('waiting', 'working')):
                continue
            try:
                choice = film_library.automatic_choice(path)
                if not choice:
                    continue
                renamed = film_library.save(path, choice)
                if manager:
                    with manager.lock:
                        for job in manager.state['queue']:
                            if job.get('output') == str(path):
                                job['output'] = str(renamed)
                                job['catalog_title'] = choice['title']
                                job['catalog_year'] = choice.get('year')
                        manager.set(queue=manager.state['queue'])
            except (OSError, ValueError, KeyError, FileExistsError):
                continue
    finally:
        AUTO_CATALOG_LOCK.release()


def films_page():
    files = completed_files()
    rows = []
    for path in files:
        relative = path.relative_to(COMPLETED_DIR)
        stat = path.stat()
        link = BASE + '/films/file?path=' + urllib.parse.quote(relative.as_posix(), safe='')
        rows.append('<li><div><strong>' + html.escape(path.name) + '</strong><small>' +
                    html.escape(str(relative.parent)) + ' · ' +
                    f'{stat.st_size / 2**30:.2f} GiB · ' +
                    datetime.fromtimestamp(stat.st_mtime).strftime('%d/%m/%Y') +
                    '</small></div><a href="' + html.escape(link, quote=True) +
                    '">Scarica</a></li>')
    listing = ''.join(rows) or '<li class="empty">Nessun film finale trovato in questa cartella.</li>'
    return ('<!doctype html><html lang="it"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>Film completati · DiscFlow</title><style>'
            'body{margin:0;background:#f4f6f3;color:#192e3b;font:16px/1.5 system-ui,sans-serif}'
            'main{max-width:900px;margin:auto;padding:24px 20px 60px}'
            'a{color:#176b69;font-weight:650;text-underline-offset:3px}'
            'a:focus-visible{outline:3px solid #75c9bb;outline-offset:3px}'
            'h1{font-size:1.75rem;margin:22px 0 4px}p{color:#526877;margin:0 0 22px}'
            'ul{list-style:none;margin:0;padding:0}li{display:flex;justify-content:space-between;'
            'align-items:center;gap:18px;background:#fff;border:1px solid #d5dfdc;'
            'border-radius:12px;padding:16px;margin:9px 0}strong,small{display:block;overflow-wrap:anywhere}'
            'small{color:#526877;margin-top:3px}.empty{border:1px dashed #b8c8c5;'
            'border-radius:12px;padding:20px}code{overflow-wrap:anywhere}'
            '@media(max-width:560px){li{align-items:start;flex-direction:column}}'
            '</style></head><body><main><a href="' + BASE + '">← Torna a DiscFlow</a>'
            '<h1>Film completati</h1><p>' + str(len(files)) + ' film in <code>' +
            html.escape(str(COMPLETED_DIR)) + '</code></p><ul>' + listing +
            '</ul></main></body></html>')


def notify(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return
    body = urllib.parse.urlencode({'chat_id': TELEGRAM_CHAT, 'text': message}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            'https://api.telegram.org/bot' + TELEGRAM_TOKEN + '/sendMessage', data=body), timeout=10).close()
    except Exception:
        pass


def write_jellyfin_nfo(folder, title, year=None):
    """Write only reviewed metadata; never replace a user's existing NFO."""
    target = Path(folder) / 'movie.nfo'
    if not title or target.exists():
        return False
    movie = ET.Element('movie')
    ET.SubElement(movie, 'title').text = title
    if year:
        ET.SubElement(movie, 'year').text = str(year)
    with target.open('xb') as stream:
        ET.ElementTree(movie).write(stream, encoding='utf-8', xml_declaration=True)
    return True


def desktop_jobs():
    """Read the desktop queue independently of the browser controller queue."""
    try:
        jobs = json.loads((core.STATE / 'jobs.json').read_text())
    except (OSError, ValueError):
        return []
    result = []
    for job in jobs:
        status = job.get('status', '')
        if status == 'trasferito':
            continue
        folder = Path(job.get('folder') or '/nonexistent')
        item = {key: job.get(key, '') for key in ('id', 'name', 'status', 'error', 'title_id', 'crop')}
        item['percent'] = None
        item['eta'] = ''
        item['written_gib'] = None
        item['expected_gib'] = None
        source = job.get('source')
        if source and Path(source).is_file():
            try:
                stat = Path(source).stat()
                cache_key = (source, stat.st_size, stat.st_mtime_ns)
                if cache_key not in DESKTOP_MEDIA_CACHE:
                    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
                                            'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
                                            source], capture_output=True, text=True, timeout=10)
                    seconds = float(probe.stdout.strip()) if probe.returncode == 0 else 0
                    if len(DESKTOP_MEDIA_CACHE) > 50: DESKTOP_MEDIA_CACHE.clear()
                    DESKTOP_MEDIA_CACHE[cache_key] = seconds
                item['duration_seconds'] = round(DESKTOP_MEDIA_CACHE[cache_key])
                item['size_gib'] = round(stat.st_size / 2**30, 1)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
        if status == 'compressione':
            try:
                tail = (folder / 'compressione.log').read_bytes()[-12000:].decode(errors='replace')
                matches = re.findall(r'Encoding:.*?(\d+(?:\.\d+)?)\s*%[^\r\n]*?ETA\s+([0-9hms]+)', tail)
                if matches:
                    item['percent'] = float(matches[-1][0])
                    item['eta'] = matches[-1][1]
            except OSError:
                pass
        elif status == 'recupero':
            rescue = job.get('rescue') or {}
            item['percent'] = rescue.get('percent')
            item['recovery_phase'] = rescue.get('phase', '')
        elif status == 'estrazione':
            try:
                size = max((p.stat().st_size for p in folder.glob('*.mkv')), default=0)
                expected = int(job.get('expected_bytes') or 0)
                item['written_gib'] = round(size / 2**30, 2)
                if expected:
                    item['expected_gib'] = round(expected / 2**30, 2)
                    item['percent'] = round(min(99, size / expected * 100), 1)
            except (OSError, ValueError):
                pass
        result.append(item)
    return result


class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        self.state = {'phase': 'Pronto', 'detail': 'Inserisci un DVD.', 'percent': 0,
                      'job': None, 'log': [], 'queue': [], 'max_discs': 10,
                      'encoder': 'cpu', 'recovery_mode': False}
        migrated = False
        try:
            old = json.loads(STATE.read_text())
            if old.get('phase') not in ('Pronto', 'In attesa', 'Coda terminata', 'Errore'):
                old['phase'] = 'Interrotto'
                old['detail'] = 'Il servizio è stato riavviato. Sorgente e parziale sono conservati.'
            self.state.update(old)
            if self.state.get('encoder') not in ('cpu', 'gpu'):
                self.state['encoder'] = 'cpu'
            for job in self.state['queue']:
                if job['status'] in ('compressione', 'estrazione', 'recupero'):
                    job['status']='errore'
                    job['error']='Lavoro interrotto; controlla il file sorgente e usa Riprova errori.'
                elif job['status'] == 'pronto' and job.get('catalog_metadata'):
                    job['auto_encode'] = True
                elif job['status'] == 'completato':
                    if not job.get('verified_at'):
                        job['verified_at'] = time.time()
                        migrated = True
                    if not job.get('catalog_title'):
                        output_path = Path(job.get('output') or '')
                        info = film_library.read(output_path) if output_path.is_file() else {}
                        if info.get('title'):
                            job['catalog_title'] = info['title']
                            job['catalog_year'] = info.get('year')
                            migrated = True
                    if not job.get('duration_seconds'):
                        try:
                            job['duration_seconds'] = round(float(core.probe(job['output'])['format']['duration']))
                            migrated = True
                        except (OSError, ValueError, KeyError, IndexError, RuntimeError):
                            pass
                    if not job.get('size_gib'):
                        path = Path(job.get('source') or '')
                        if not path.is_file(): path = Path(job.get('output') or '')
                        if path.is_file():
                            job['size_gib'] = round(path.stat().st_size / 2**30, 1)
                            migrated = True
        except (OSError, ValueError):
            pass
        self.busy = False
        self.encoding = False
        self.last_media = False
        if migrated: self.save()
        threading.Thread(target=self.watch, daemon=True).start()

    def save(self):
        core.STATE.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False))
        tmp.replace(STATE)

    def completed_source_lookup(self):
        return {job['output']: job['source'] for job in self.state['queue']
                if job.get('output') and job.get('source') and job.get('status') == 'completato'}

    def set(self, phase=None, detail=None, percent=None, log=None, **extra):
        with self.lock:
            if phase is not None: self.state['phase'] = phase
            if detail is not None: self.state['detail'] = detail
            if percent is not None: self.state['percent'] = percent
            if log: self.state['log'] = (self.state['log'] + [log])[-40:]
            self.state.update(extra)
            self.save()

    def run(self, cmd, logfile, progress=False):
        with open(logfile, 'a') as f:
            f.write('$ ' + subprocess.list2cmdline(list(map(str, cmd))) + '\n')
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, bufsize=1, errors='replace')
            line = ''
            while True:
                ch = p.stdout.read(1)
                if not ch: break
                if ch in '\r\n':
                    if line:
                        f.write(line + '\n'); f.flush()
                        if progress:
                            m = re.search(r'Encoding:.*?(\d+(?:\.\d+)?)\s*%', line)
                            if m:
                                with self.lock:
                                    active = next((j for j in self.state['queue'] if j['status'] == 'compressione'), None)
                                    if active:
                                        active['percent'] = float(m.group(1))
                                        self.set(percent=active['percent'], detail=active['name']+' · '+m.group(1)+'%', queue=self.state['queue'])
                        elif line.startswith('PRGV:'):
                            self.set(detail='MakeMKV sta leggendo il DVD…')
                        elif line.startswith(('MSG:2003','MSG:2004')) and has_disc_read_error(line):
                            self.set(detail='DVD difficile: MakeMKV sta riprovando la lettura dei settori.')
                    line = ''
                else:
                    line = (line + ch)[-4000:]
            if line: f.write(line + '\n')
            return p.wait()

    def media_present(self):
        p = core.optical_properties()
        return p.get('ID_CDROM_MEDIA') == '1'

    def desktop_active(self):
        p = subprocess.run(['pgrep', '-f', str(Path(__file__).with_name('ripper.py'))],
                           capture_output=True, text=True)
        return p.returncode == 0

    def mirror_desktop(self):
        try:
            jobs = json.loads((core.STATE/'jobs.json').read_text())
            active = next((j for j in jobs if j['status'] == 'estrazione'),None)
            encoding = next((j for j in jobs if j['status'] == 'compressione'),None)
            active = active or encoding or next((j for j in jobs if j['status'] in ('analisi','recupero')),None)
            if not active:
                self.set('Desktop attivo', 'Il programma desktop è aperto. Chiudilo per usare il controllo web.')
                return
            name = active['name']
            if active['status'] == 'estrazione':
                folder = Path(active['folder'])
                size = max((p.stat().st_size for p in folder.glob('*.mkv')),default=0)
                expected = int(active.get('expected_bytes') or 0)
                percent = min(99, size/expected*100) if expected else 0
                detail = name+' · '+f'{size/2**30:.1f} GiB scritti'
                if encoding: detail += ' · In parallelo: compressione di '+encoding['name']
                self.set('Estrazione desktop', detail,percent,job={'name':name})
            elif active['status'] == 'compressione':
                logfile = Path(active['folder'])/'compressione.log'
                tail = logfile.read_bytes()[-5000:].decode(errors='replace') if logfile.exists() else ''
                matches = re.findall(r'Encoding:.*?(\d+(?:\.\d+)?)\s*%',tail)
                percent = float(matches[-1]) if matches else 0
                self.set('Compressione desktop', name+' · '+f'{percent:.1f}%',percent,job={'name':name})
            elif active['status'] == 'recupero':
                rescue = active.get('rescue') or {}
                percent = float(rescue.get('percent') or 0)
                detail = name+' · '+(rescue.get('phase') or 'recupero')+' · '+f'{percent:.1f}%'
                if rescue.get('bad_gib'):
                    detail += ' · irrecuperabili '+f"{rescue['bad_gib']:.2f} GiB"
                self.set('Recupero desktop', detail,percent,job={'name':name})
            else:
                self.set('Analisi desktop', name,job={'name':name})
        except (OSError,ValueError,KeyError):
            self.set('Desktop attivo', 'Il DVD è gestito dalla finestra desktop.')

    def watch(self):
        while True:
            try:
                media = self.media_present()
                with self.lock: busy = self.busy
                desktop = self.desktop_active()
                if busy and self.state['phase']=='Estrazione' and self.state.get('job'):
                    current=self.state['job']
                    folder=Path(current['folder'])
                    size=max((p.stat().st_size for p in folder.glob('*.mkv')),default=0)
                    expected=int(current.get('expected_bytes') or 0)
                    percent=min(99,size/expected*100) if expected else 0
                    detail=current['name']+' · '+f'{size/2**30:.1f} GiB scritti'
                    if expected:detail+=' su circa '+f'{expected/2**30:.1f} GiB'
                    self.set(detail=detail,percent=percent)
                if media and not self.last_media and not busy and not desktop and not self.state.get('duplicate_pending'):
                    self.start_scan()
                elif desktop:
                    self.mirror_desktop()
                elif not media and self.state['phase'] in ('Desktop attivo','Estrazione desktop','Compressione desktop','Analisi desktop'):
                    self.set('Pronto', 'Inserisci un DVD.',0,job=None)
                if not desktop and not self.encoding and any(
                        job['status'] == 'pronto' and job.get('auto_encode')
                        for job in self.state['queue']):
                    self.start_encoding(auto_only=True)
                if not desktop and not self.busy and not self.encoding:
                    self.cleanup_due_sources()
                self.last_media = media
            except Exception as e:
                self.set(log='Controllo disco: ' + str(e))
            time.sleep(8)

    def start_scan(self):
        with self.lock:
            pending = sum(j['status'] in ('pronto','compressione') for j in self.state['queue'])
            if self.busy or self.desktop_active() or pending >= self.state['max_discs'] or self.state.get('duplicate_pending'):
                return False
            self.busy = True
        threading.Thread(target=self.workflow, daemon=True).start()
        return True

    def workflow(self):
        if self.state.get('recovery_mode'):
            self.workflow_recovery()
        else:
            self.workflow_disc()

    def workflow_disc(self):
        try:
            titles = []
            for attempt in range(1, READ_ATTEMPTS + 1):
                if attempt > 1 and not self.media_present():
                    raise RuntimeError('DVD rimosso: tentativi di lettura interrotti.')
                self.set('Rilevamento', f'Cerco i titoli del DVD · tentativo {attempt}/{READ_ATTEMPTS}…', 0)
                try:
                    p = subprocess.run(['makemkvcon', '-r', 'info', 'dev:' + core.DEVICE],
                                       capture_output=True, text=True, timeout=600)
                except subprocess.TimeoutExpired:
                    if attempt == READ_ATTEMPTS or not self.media_present():
                        raise RuntimeError('La lettura del DVD non risponde dopo tre tentativi.')
                    self.set(log=f'Scansione troppo lenta: nuovo tentativo tra {READ_RETRY_DELAY} secondi.')
                    time.sleep(READ_RETRY_DELAY)
                    continue
                text = p.stdout + p.stderr
                titles = core.titles_from_robot(text)
                if titles: break
                if not has_disc_read_error(text) or attempt == READ_ATTEMPTS or not self.media_present(): break
                self.set(log=f'Lettura difficile: riprovo la scansione tra {READ_RETRY_DELAY} secondi ({attempt}/{READ_ATTEMPTS}).')
                time.sleep(READ_RETRY_DELAY)
            if not titles:
                raise RuntimeError('Nessun titolo DVD trovato dopo i tentativi di lettura. Pulisci il disco e reinseriscilo.')
            def seconds(t):
                try:
                    h, m, s = map(int, t['duration'].split(':')); return h*3600+m*60+s
                except ValueError: return 0
            title = max(titles, key=seconds)
            self.set(detected_titles=[{'id':t['id'], 'name':t['name'],
                                        'duration':t['duration'],
                                        'size_gib':round(int(t['size'] or 0)/2**30,1)}
                                       for t in sorted(titles,key=seconds,reverse=True)],
                     selected_title_id=title['id'])
            name = core.safe_name(core.disc_label(text) or title['name'] or 'Film_' + time.strftime('%Y%m%d_%H%M'))
            matches = duplicates.candidates(name, seconds(title), completed_files())
            override = self.state.get('duplicate_override') or {}
            if matches and override.get('name') != name:
                self.set('Possibile duplicato', 'Controlla il film già presente prima di acquisire il DVD.', 0,
                         duplicate_pending={'stage': 'disc', 'name': name, 'matches': matches})
                return
            decision = override.get('action') if override.get('name') == name else None
            if decision:
                self.set(duplicate_override=None, duplicate_pending=None)
            folder = core.ROOT / name
            if folder.exists():
                base, stamp, n = name, time.strftime('%Y%m%d_%H%M%S'), 1
                while folder.exists():
                    name = f'{base}_{stamp}-{n}'; n += 1; folder = core.ROOT / name
            expected = int(title['size'] or 0)
            if shutil.disk_usage(core.ROOT).free < max(expected+8*2**30, 12*2**30):
                raise RuntimeError('Spazio insufficiente per estrazione e compressione.')
            folder.mkdir(parents=True)
            queued = {'name':name,'folder':str(folder),'source':'',
                      'title_id':title['id'],'duration':title['duration'],
                      'expected_bytes':expected,'status':'estrazione'}
            if decision:
                queued['duplicate_action'] = decision
                queued['duplicate_existing'] = override['existing']
                self.attach_duplicate_metadata(queued, override['existing'])
            with self.lock:
                self.set('Estrazione', 'Estraggo ' + name + '…', 0,
                         job=queued, queue=self.state['queue'] + [queued])
            logpath = folder/'estrazione.log'
            for attempt in range(1, READ_ATTEMPTS + 1):
                if attempt > 1 and not self.media_present():
                    raise RuntimeError('DVD rimosso: tentativi di estrazione interrotti.')
                queued['read_attempt'] = attempt
                self.set('Estrazione', f'{name} · tentativo {attempt}/{READ_ATTEMPTS}', 0,
                         job=queued, queue=self.state['queue'])
                log_start = logpath.stat().st_size if logpath.exists() else 0
                rc = self.run(['makemkvcon','-r','mkv','dev:'+core.DEVICE,str(title['id']),str(folder)], logpath)
                if rc == 0: break
                with logpath.open('rb') as stream:
                    stream.seek(log_start)
                    attempt_log = stream.read().decode(errors='replace')
                if not has_disc_read_error(attempt_log) or attempt == READ_ATTEMPTS:
                    raise RuntimeError(f'MakeMKV si è fermato dopo {attempt} tentativi. Controlla estrazione.log; i file restano conservati.')
                archived = archive_failed_acquisition(folder, attempt)
                self.set(log=f'Errore di lettura al tentativo {attempt}/{READ_ATTEMPTS}. '
                             f'Conservo {len(archived)} file incompleti e riprovo tra {READ_RETRY_DELAY} secondi.')
                time.sleep(READ_RETRY_DELAY)
            files = sorted((p for p in folder.glob('*.mkv') if 'interrotto-' not in p.name.lower()),
                           key=lambda p:p.stat().st_size, reverse=True)
            if not files: raise RuntimeError('Nessun MKV estratto.')
            source = files[0]
            core.probe(source)
            ejected = subprocess.run(['eject',core.DEVICE], capture_output=True, text=True, timeout=30)
            if ejected.returncode == 0:
                self.last_media = False
            else:
                self.set(log='DVD estratto ma espulsione fallita: '+ejected.stderr[-300:])
            with self.lock:
                queued['source'] = str(source)
                queued['size_gib'] = round(source.stat().st_size / 2**30, 1)
                post_matches = duplicates.candidates(name, seconds(title),
                                  [p for p in completed_files() if str(p) != queued.get('duplicate_existing')],
                                  source=source, source_lookup=self.completed_source_lookup())
                if post_matches and not decision:
                    queued['status'] = 'duplicato'
                    queued['duplicate_matches'] = post_matches
                    self.set(duplicate_pending={'stage': 'extracted', 'name': name,
                               'job_index': self.state['queue'].index(queued), 'matches': post_matches})
                else:
                    queued['status'] = 'pronto'
                jobs = self.state['queue']
            self.set('Possibile duplicato' if queued['status'] == 'duplicato' else 'In attesa',
                     name+(' estratto: scegli come gestire il duplicato.' if queued['status'] == 'duplicato' else ' estratto. Inserisci il prossimo DVD oppure avvia la compressione.'),
                     0,job=None,queue=jobs)
            notify('X99 Ripper: '+name+' estratto; '+str(sum(j['status']=='pronto' for j in jobs))+' film in attesa.')
        except Exception as e:
            if 'queued' in locals():
                with self.lock:
                    queued['status'] = 'errore'
                    queued['error'] = str(e)
                    self.set(queue=self.state['queue'])
            self.set('Errore', str(e), log=str(e))
            notify('X99 Ripper: serve attenzione. '+str(e))
        finally:
            with self.lock: self.busy = False

    def choose_duplicate(self, action, existing, confirm_name):
        if action not in ('cancel', 'alternative', 'replace'):
            return False
        with self.lock:
            pending = self.state.get('duplicate_pending')
            if not pending or pending['name'] != confirm_name:
                return False
            if action != 'cancel' and existing not in [item['path'] for item in pending['matches']]:
                return False
            if pending['stage'] == 'disc':
                self.set(duplicate_pending=None, duplicate_override=None)
                if action == 'cancel':
                    self.last_media = True
                    self.set('Annullato', 'DVD non acquisito. Rimuovilo dal lettore.')
                    return True
                self.set(duplicate_override={'name': pending['name'], 'action': action, 'existing': existing})
                return self.start_scan()
            index = pending['job_index']
            if not 0 <= index < len(self.state['queue']): return False
            job = self.state['queue'][index]
            if job['status'] != 'duplicato' or job['name'] != confirm_name: return False
            job['status'] = 'annullato' if action == 'cancel' else 'pronto'
            if action != 'cancel':
                job['duplicate_action'] = action
                job['duplicate_existing'] = existing
                job['auto_encode'] = True
                self.attach_duplicate_metadata(job, existing)
            self.set('In attesa', 'Scelta duplicato registrata.', duplicate_pending=None, queue=self.state['queue'])
        if action != 'cancel': self.start_encoding(auto_only=True)
        return True

    @staticmethod
    def attach_duplicate_metadata(job, existing):
        info = {**film_library.read(Path(existing)), **(job.get('catalog_metadata') or {})}
        if job.get('catalog_title'):
            info['title'] = job['catalog_title']
            info['year'] = job.get('catalog_year')
        if info.get('title'):
            job['catalog_title'] = info['title']
            job['catalog_year'] = info.get('year')
            job['original_language'] = info.get('original_language', '')
            job['catalog_metadata'] = {**info, 'version_label': 'DVD/Blu-ray · ' +
                       (job.get('duration') or 'durata da verificare') + ' · ' + job['name']}

    def finalize_replacement(self, index, confirm_name, confirm_existing):
        """Archive the previous version only after explicit, second confirmation."""
        with self.lock:
            if not 0 <= index < len(self.state['queue']): return False
            job = self.state['queue'][index]
            if (job.get('status') != 'completato' or job.get('duplicate_action') != 'replace'
                    or job.get('name') != confirm_name or job.get('duplicate_existing') != confirm_existing
                    or job.get('replacement_archived') or job.get('catalog_error')):
                return False
            new = Path(job.get('output') or '')
            old = Path(confirm_existing)
            if (not new.is_file() or not film_library.read(new).get('title')
                    or not completed_file(old.relative_to(COMPLETED_DIR)) == old.resolve()):
                return False
            if old == new: return False
            archive_root = core.STATE / 'archived-films'
            archive_root.mkdir(parents=True, exist_ok=True)
            if archive_root.stat().st_dev != old.stat().st_dev:
                raise RuntimeError('Archivio su un altro disco: sostituzione annullata.')
            target = archive_root / (old.parent.name + '-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
            if old.parent != COMPLETED_DIR and old.parent.is_relative_to(COMPLETED_DIR) and len(list(old.parent.glob('*_H265.mkv'))) == 1:
                old.parent.rename(target)
            else:
                target.mkdir()
                old.rename(target / old.name)
                for name in ('discflow-film.json', 'movie.nfo', 'poster.jpg'):
                    companion = old.parent / name
                    if companion.is_file() and not companion.is_symlink():
                        companion.rename(target / name)
            job['replacement_archived'] = str(target)
            for other in self.state['queue']:
                if other.get('output') == str(old):
                    other['status'] = 'archiviato'
                    other['output'] = str(target / old.name)
            self.set(log='Versione precedente archiviata in ' + str(target), queue=self.state['queue'])
        return True

    def workflow_recovery(self):
        """Modalità recupero: ddrescue copia il disco in immagine, MakeMKV estrae dall'immagine."""
        def seconds(t):
            try:
                h, m, s = map(int, t['duration'].split(':')); return h*3600+m*60+s
            except ValueError: return 0
        try:
            if not recover.available():
                raise RuntimeError('Manca GNU ddrescue: installa il pacchetto gddrescue e riprova.')
            self.set('Recupero', "Provo a leggere l'etichetta del disco…", 0)
            label = ''
            try:
                p = subprocess.run(['makemkvcon', '-r', 'info', 'dev:' + core.DEVICE],
                                   capture_output=True, text=True, timeout=180)
                label = core.disc_label(p.stdout + p.stderr)
            except (OSError, subprocess.SubprocessError):
                label = ''
            name = core.safe_name(label or 'Recupero_' + time.strftime('%Y%m%d_%H%M'))
            matches = duplicates.candidates(name, None, completed_files())
            override = self.state.get('duplicate_override') or {}
            if matches and override.get('name') != name:
                self.set('Possibile duplicato', 'Controlla il film già presente prima del recupero.', 0,
                         duplicate_pending={'stage': 'disc', 'name': name, 'matches': matches})
                return
            decision = override.get('action') if override.get('name') == name else None
            if decision:
                self.set(duplicate_override=None, duplicate_pending=None)
            folder = core.ROOT / name
            if folder.exists():
                base, stamp, n = name, time.strftime('%Y%m%d_%H%M%S'), 1
                while folder.exists():
                    name = f'{base}_{stamp}-{n}'; n += 1; folder = core.ROOT / name
            if shutil.disk_usage(core.ROOT).free < 21 * 2**30:
                raise RuntimeError('Spazio insufficiente: il recupero richiede 21 GiB liberi (immagine + pipeline).')
            folder.mkdir(parents=True)
            iso = folder / (name + '.iso')
            mapfile = folder / 'recupero.map'
            rescue_log = folder / 'recupero.log'
            logpath = folder / 'estrazione.log'
            queued = {'name': name, 'folder': str(folder), 'source': '',
                      'title_id': None, 'duration': '', 'expected_bytes': 0,
                      'status': 'recupero', 'recovery': {}}
            if decision:
                queued['duplicate_action'] = decision
                queued['duplicate_existing'] = override['existing']
                self.attach_duplicate_metadata(queued, override['existing'])
            with self.lock:
                self.set('Recupero', 'Copio il disco con ddrescue · ' + name + '…', 0,
                         job=queued, queue=self.state['queue'] + [queued])
            def progress(stats):
                remaining = stats['total'] - stats['finished'] - stats['bad']
                with self.lock:
                    queued['recovery'] = {'phase': stats['phase'], 'percent': stats['percent'],
                                          'bad_gib': round(stats['bad'] / 2**30, 2),
                                          'remaining_gib': round(remaining / 2**30, 2),
                                          'read_gib': round(stats['finished'] / 2**30, 2),
                                          'total_gib': round(stats['total'] / 2**30, 2)}
                    self.set('Recupero',
                             name + ' · ' + f"{stats['percent']:.1f}% · {stats['phase']}",
                             stats['percent'], job=queued, queue=self.state['queue'])
            recover.rescue_iso(core.DEVICE, iso, mapfile, rescue_log, progress)
            source = None
            for attempt in range(1, 4):
                self.set('Recupero', f"Estraggo il titolo dall'immagine · tentativo {attempt}/3", 0,
                         job=queued, queue=self.state['queue'])
                p = subprocess.run(['makemkvcon', '-r', 'info', recover.iso_source(iso)],
                                   capture_output=True, text=True, timeout=3600)
                titles = core.titles_from_robot(p.stdout + p.stderr)
                title = max(titles, key=seconds) if titles else None
                rc = 1
                if title:
                    queued['title_id'] = title['id']
                    queued['duration'] = title['duration']
                    queued['expected_bytes'] = int(title['size'] or 0)
                    rc = self.run(['makemkvcon', '-r', 'mkv', recover.iso_source(iso),
                                   str(title['id']), str(folder)], logpath)
                files = sorted((p2 for p2 in folder.glob('*.mkv')
                                if 'interrotto-' not in p2.name.lower()),
                               key=lambda p2: p2.stat().st_size, reverse=True)
                if rc == 0 and files:
                    source = files[0]
                    break
                for stale in files:
                    stale.unlink(missing_ok=True)
                self.set(log=f"Tentativo {attempt}/3 fallito: MakeMKV non ha completato l'estrazione dall'immagine. Dettagli in estrazione.log.")
                if attempt == 3:
                    break
                stats = recover.map_stats(mapfile)
                remaining = stats['nontried'] + stats['nontrimmed'] + stats['nonscraped'] + stats['bad']
                if remaining:
                    self.set('Recupero', 'Le aree difficili restano: altro giro di ddrescue…', 0,
                             job=queued, queue=self.state['queue'])
                    recover.rescue_iso(core.DEVICE, iso, mapfile, rescue_log, progress)
                else:
                    break
            if not source:
                stats = recover.map_stats(mapfile)
                raise RuntimeError("Impossibile estrarre il titolo dall'immagine. "
                                   + recover.summary(stats)
                                   + ' Consulta estrazione.log e recupero.map.')
            core.probe(source)
            iso.unlink(missing_ok=True)
            ejected = subprocess.run(['eject', core.DEVICE], capture_output=True, text=True, timeout=30)
            if ejected.returncode == 0:
                self.last_media = False
            else:
                self.set(log='DVD recuperato ma espulsione fallita: ' + ejected.stderr[-300:])
            stats = recover.map_stats(mapfile)
            with self.lock:
                queued['source'] = str(source)
                queued['size_gib'] = round(source.stat().st_size / 2**30, 1)
                queued['recovery_bad_gib'] = round(stats['bad'] / 2**30, 2)
                post_matches = duplicates.candidates(name, queued['duration'] and seconds(title),
                                  [p for p in completed_files() if str(p) != queued.get('duplicate_existing')],
                                  source=source, source_lookup=self.completed_source_lookup())
                if post_matches and not decision:
                    queued['status'] = 'duplicato'
                    queued['duplicate_matches'] = post_matches
                    self.set(duplicate_pending={'stage': 'extracted', 'name': name,
                               'job_index': self.state['queue'].index(queued), 'matches': post_matches})
                else:
                    queued['status'] = 'pronto'
                queued.pop('recovery', None)
                jobs = self.state['queue']
            self.set('Possibile duplicato' if queued['status'] == 'duplicato' else 'In attesa',
                     name + ' recuperato. ' + recover.summary(stats)
                     + (' Scegli come gestire il duplicato.' if queued['status'] == 'duplicato' else ' Inserisci il prossimo DVD oppure avvia la compressione.'),
                     0, job=None, queue=jobs)
            notify('X99 Ripper: ' + name + " recuperato dall'immagine; "
                   + str(sum(j['status'] == 'pronto' for j in jobs)) + ' film in attesa.')
        except Exception as e:
            if 'queued' in locals():
                with self.lock:
                    queued['status'] = 'errore'
                    queued['error'] = str(e)
                    self.set(queue=self.state['queue'])
            self.set('Errore', str(e), log=str(e))
            notify('X99 Ripper: serve attenzione. ' + str(e))
        finally:
            with self.lock: self.busy = False

    def start_encoding(self, auto_only=False):
        with self.lock:
            if (self.encoding or self.desktop_active() or
                    not any(j['status']=='pronto' and (not auto_only or j.get('auto_encode'))
                            for j in self.state['queue'])):
                return False
            self.encoding = True
        threading.Thread(target=self.encode_queue,args=(auto_only,),daemon=True).start()
        return True

    def set_limit(self,limit):
        if limit not in (5,10):return False
        with self.lock:
            if self.busy or self.encoding:return False
            self.set(max_discs=limit)
        return True

    def set_encoder(self, value):
        if value not in ('cpu', 'gpu'):
            return False
        with self.lock:
            if self.encoding or self.desktop_active():
                return False
            self.set(encoder=value)
        return True

    def set_recovery(self, value):
        if value not in (True, False):
            return False
        with self.lock:
            if self.busy or self.encoding or self.desktop_active():
                return False
            self.set(recovery_mode=value)
        return True

    def import_video(self, staged, filename, crop_mode='auto', deinterlace='auto'):
        """Register a browser-uploaded video for the existing encoding queue."""
        if crop_mode not in ('auto', 'none') or deinterlace not in ('auto', 'on', 'off'):
            raise ValueError('Opzioni video non valide.')
        if not isinstance(filename, str) or not filename or len(filename) > 220:
            raise ValueError('Nome del video non valido.')
        if Path(filename).name != filename or any(ord(c) < 32 for c in filename):
            raise ValueError('Il nome del file contiene un percorso o caratteri non validi.')
        staged = Path(staged)
        if staged.is_symlink() or not staged.is_file():
            raise ValueError('Video caricato non trovato.')
        meta = core.probe(staged)
        videos = core.tracks(meta, 'video')
        if len(videos) != 1 or not videos[0].get('width') or not videos[0].get('height'):
            raise ValueError('Serve un file con una sola traccia video leggibile.')
        try: duration = float(meta['format']['duration'])
        except (KeyError, TypeError, ValueError): raise ValueError('Durata del video non leggibile.')
        if duration < 1: raise ValueError('Video troppo breve o incompleto.')
        name = core.safe_name(Path(filename).stem)
        suffix = Path(filename).suffix.lower()
        if len(suffix) > 12 or not re.fullmatch(r'\.[a-z0-9]+', suffix or '.x'):
            suffix = ''
        folder = core.ROOT / (name + '_import_' + uuid.uuid4().hex[:10])
        source = folder / ('Originale_' + name[:55] + suffix)
        with self.lock:
            if self.busy or self.desktop_active():
                raise RuntimeError('Il lettore è occupato: riprova dopo l’acquisizione del disco.')
            if folder.exists(): raise FileExistsError('Cartella di importazione già presente.')
            folder.mkdir()
            staged.replace(source)
            job = {'name': name, 'folder': str(folder), 'source': str(source),
                   'title_id': None, 'duration_seconds': round(duration),
                   'size_gib': round(source.stat().st_size / 2**30, 2),
                   'status': 'pronto', 'managed_source': True, 'source_type': 'file',
                   'crop_mode': crop_mode, 'deinterlace': deinterlace,
                   'original_filename': filename}
            self.set('In attesa', name + ' importato. Avvia la compressione.', 0,
                     queue=self.state['queue'] + [job])
        return job

    def set_catalog(self, index, title, year):
        if not isinstance(title, str):
            return False
        title = title.strip()
        if not title or len(title) > 120 or any(ord(c) < 32 for c in title):
            return False
        if year not in (None, ''):
            try: year = int(year)
            except (TypeError, ValueError): return False
            if not 1888 <= year <= 2100: return False
        with self.lock:
            if not 0 <= index < len(self.state['queue']): return False
            job = self.state['queue'][index]
            if job['status'] != 'pronto': return False
            job['catalog_title'] = title
            job['catalog_year'] = year or None
            job.pop('catalog_metadata', None)
            job.pop('auto_encode', None)
            matches = duplicates.candidates(title, job.get('duration_seconds'), completed_files(), year=year or None)
            if matches and not job.get('duplicate_action'):
                job['status'] = 'duplicato'
                job['duplicate_matches'] = matches
                self.set(duplicate_pending={'stage': 'extracted', 'name': job['name'],
                           'job_index': index, 'matches': matches})
            self.set(queue=self.state['queue'])
        return True

    def set_catalog_choice(self, index, qid):
        metadata = film_library.details(qid)
        if not metadata.get('title') or not metadata.get('year'):
            return False
        with self.lock:
            if not 0 <= index < len(self.state['queue']): return False
            job = self.state['queue'][index]
            if job['status'] != 'pronto': return False
            job['catalog_title'] = metadata['title']
            job['catalog_year'] = metadata['year']
            job['catalog_metadata'] = metadata
            job['original_language'] = metadata.get('original_language', '')
            matches = duplicates.candidates(metadata['title'], job.get('duration_seconds'), completed_files(),
                       year=metadata.get('year'), wikidata_id=metadata.get('wikidata_id', ''), imdb_id=metadata.get('imdb_id', ''))
            if matches and not job.get('duplicate_action'):
                job['status'] = 'duplicato'
                job['duplicate_matches'] = matches
                self.set(duplicate_pending={'stage': 'extracted', 'name': job['name'],
                           'job_index': index, 'matches': matches})
            else:
                job['auto_encode'] = True
            self.set(queue=self.state['queue'])
        if not matches or job.get('duplicate_action'):
            self.start_encoding(auto_only=True)
        return True

    def delete_ready(self, index, confirm_name):
        with self.lock:
            if self.busy or self.encoding or self.desktop_active(): return False
            if not 0 <= index < len(self.state['queue']): return False
            job = self.state['queue'][index]
            if job['status'] != 'pronto' or job['name'] != confirm_name: return False
            folder = Path(job['folder'])
            root = core.ROOT.resolve()
            if folder.is_symlink() or folder.resolve().parent != root or not folder.is_dir(): return False
            source = Path(job.get('source') or '')
            if source.resolve().parent != folder.resolve() or not source.is_file(): return False
            if any(p.is_symlink() or p.is_dir() for p in folder.iterdir()): return False
            shutil.rmtree(folder)
            queue = self.state['queue'][:index] + self.state['queue'][index + 1:]
            self.set('In attesa', confirm_name + ' eliminato dalla coda e dal disco.', 0,
                     queue=queue, job=None)
        return True

    def remove_verified_source(self, index, confirm_name, automatic=False):
        """Remove one app-acquired source after its verified output is available."""
        with self.lock:
            if self.busy or self.encoding or self.desktop_active(): return False
            if not 0 <= index < len(self.state['queue']): return False
            job = self.state['queue'][index]
            if job.get('status') != 'completato' or job.get('name') != confirm_name or not (job.get('title_id') is not None or job.get('managed_source')):
                return False
            if automatic and time.time() - float(job.get('verified_at') or time.time()) < SOURCE_RETENTION_SECONDS:
                return False
            folder = Path(job['folder'])
            source = Path(job.get('source') or '')
            output = Path(job.get('output') or '')
            root = core.ROOT.resolve()
            if folder.is_symlink() or folder.resolve().parent != root: return False
            if source.is_symlink() or output.is_symlink() or not source.is_file() or not output.is_file(): return False
            if source.resolve().parent != folder.resolve() or output.resolve().parent != folder.resolve(): return False
            if source.resolve() == output.resolve() or source.stat().st_ino == output.stat().st_ino: return False
            source.unlink()
            job['source_removed'] = True
            job['source_removed_at'] = time.time()
            job['size_gib'] = round(output.stat().st_size / 2**30, 1)
            self.set(detail=confirm_name + ': File originale rimosso; video finale conservato.',
                     log=confirm_name + ': originale rimosso ' + ('dopo 48 ore.' if automatic else 'su conferma.'),
                     queue=self.state['queue'])
            global STORAGE_CACHE
            with STORAGE_LOCK:
                STORAGE_CACHE = (0, None)
        return True

    def cleanup_due_sources(self):
        with self.lock:
            due = [(index, job['name']) for index, job in enumerate(self.state['queue'])
                   if job.get('status') == 'completato' and (job.get('title_id') is not None or job.get('managed_source'))
                   and job.get('verified_at') and not job.get('source_removed')
                   and time.time() - float(job['verified_at']) >= SOURCE_RETENTION_SECONDS]
        for index, name in due:
            self.remove_verified_source(index, name, automatic=True)

    def import_external_final(self, staged, digest, title_hint):
        """Register a checked X99 final without touching any existing film."""
        if not re.fullmatch(r'[0-9a-f]{32}/film\.mkv\.part', staged or ''):
            raise ValueError('Percorso di importazione non valido.')
        if not re.fullmatch(r'[0-9a-f]{64}', digest or ''):
            raise ValueError('Impronta del file non valida.')
        if not isinstance(title_hint, str) or not 1 <= len(title_hint) <= 120:
            raise ValueError('Titolo mancante o troppo lungo.')
        incoming = core.STATE / 'incoming'
        path = incoming / staged
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(incoming.resolve()):
            raise ValueError('File trasferito non trovato.')
        if path.stat().st_size < 100_000:
            raise ValueError('File trasferito troppo piccolo.')
        with path.open('rb') as source:
            measured = hashlib.file_digest(source, 'sha256').hexdigest()
        if not hmac.compare_digest(measured, digest):
            raise ValueError('La copia non corrisponde all’originale: importazione annullata.')
        media = core.probe(path)
        video = core.tracks(media, 'video')
        if len(video) != 1 or video[0].get('codec_name') != 'hevc' or not core.tracks(media, 'audio'):
            raise ValueError('Serve un film finale HEVC con audio.')
        duration = float(media['format']['duration'])
        core.verify_decode_samples(path, duration)
        name = core.safe_name(title_hint)
        folder = core.ROOT / name
        output = folder / (name + '_H265.mkv')
        with self.lock:
            if folder.exists() or any(j.get('name') == name or j.get('output') == str(output)
                                      for j in self.state['queue']):
                raise FileExistsError('Questo film è già presente: nessun file è stato sovrascritto.')
            folder.mkdir()
            path.replace(output)
            job = {'name': name, 'folder': str(folder), 'source': '', 'output': str(output),
                   'status': 'completato', 'verified_at': time.time(), 'source_removed': True,
                   'duration_seconds': round(duration),
                   'size_gib': round(output.stat().st_size / 2**30, 1), 'encoder': 'x99'}
            self.set(detail=name + ' importato dall’X99 e verificato.',
                     log=name + ': importato dall’X99.', queue=self.state['queue'] + [job])
            global STORAGE_CACHE
            with STORAGE_LOCK: STORAGE_CACHE = (0, None)
        threading.Thread(target=catalog_automatically, args=([output],), daemon=True).start()
        return output

    def retry_errors(self):
        with self.lock:
            if self.encoding or self.desktop_active():return False
            changed=False
            for job in self.state['queue']:
                if job['status']!='errore':continue
                source=Path(job.get('source') or '/nonexistent')
                if not source.is_file():
                    candidates=list(Path(job['folder']).glob('*.mkv'))
                    candidates=[p for p in candidates if '.parziale.' not in p.name and '.interrotto-' not in p.name and not p.name.endswith('_H265.mkv')]
                    if len(candidates)!=1:continue
                    source=candidates[0]
                    try:core.probe(source)
                    except Exception:continue
                    job['source']=str(source)
                out=Path(job['folder'])/(job['name']+'_H265.mkv')
                if out.exists():continue
                archived=core.archive_partial(out)
                if archived:job.setdefault('previous_partials',[]).append(str(archived))
                job['status']='pronto';job.pop('error',None);changed=True
            if changed:self.set('In attesa','I film con errore sono pronti per un nuovo tentativo.',0,queue=self.state['queue'])
            return changed

    def next_ready_or_wait(self, auto_only=False):
        """Keep the encoder queue alive until an in-progress extraction joins it."""
        while True:
            with self.lock:
                job = next((j for j in self.state['queue'] if j['status'] == 'pronto' and
                            (not auto_only or j.get('auto_encode'))), None)
                if job or auto_only or not self.busy:
                    return job
            time.sleep(0.25)

    def encode_queue(self, auto_only=False):
        try:
            while True:
                job = self.next_ready_or_wait(auto_only)
                if not job:
                    break
                with self.lock:
                    job['status']='compressione'
                    job.pop('auto_encode', None)
                    self.set('Analisi', 'Preparo '+job['name']+'…',0,job=job)
                try:
                    source=Path(job['source']);folder=Path(job['folder']);name=job['name']
                    encoder = ('nvenc_h265_10bit' if self.state.get('encoder') == 'gpu'
                               else 'x265_10bit')
                    job['encoder'] = encoder
                    meta=core.probe(source)
                    subs=core.sub_choices(meta)
                    if not job.get('original_language'):
                        try:
                            match = film_library.automatic_choice(source, title_hint=name)
                            if match:
                                job['original_language'] = match.get('original_language')
                        except (OSError, ValueError, KeyError, TimeoutError):
                            pass
                    plan=core.audio_plan(meta,core.audio_choices(meta, job.get('original_language')))
                    if job.get('source_type') == 'file':
                        crop_value = None if job.get('crop_mode') == 'auto' else '0:0:0:0'
                    else:
                        scan=subprocess.run(core.HB+['-i',str(source),'--scan','--previews','10:0'],
                                            capture_output=True,text=True,timeout=240)
                        crop=core.CROP.search(scan.stdout+scan.stderr)
                        if not crop: raise RuntimeError('Crop automatico non rilevato.')
                        crop_value=':'.join(crop.groups())
                    out=folder/(name+'_H265.mkv')
                    partial=folder/(name+'_H265.parziale.mkv')
                    if out.exists() or partial.exists():
                        raise RuntimeError('Output già presente: non viene sovrascritto.')
                    if shutil.disk_usage(folder).free < max(4*2**30,source.stat().st_size//4):
                        raise RuntimeError('Spazio insufficiente per comprimere.')
                    self.set('Compressione',name+' · 0%',0,job=job)
                    rc=self.run(core.hb_command(source,partial,crop_value,plan,subs,encoder,
                                                job.get('deinterlace', 'off')),
                                folder/'compressione.log',True)
                    if rc: raise RuntimeError('HandBrake terminato con codice '+str(rc))
                    self.set('Verifica','Controllo '+name+'…',99)
                    core.verify(meta,partial,subs,plan)
                    partial.rename(out)
                    job['status']='completato';job['output']=str(out);job['verified_at']=time.time()
                    if job.get('catalog_title'):
                        try:
                            metadata = job.get('catalog_metadata') or {
                                'title': job['catalog_title'], 'year': job.get('catalog_year')}
                            renamed = film_library.save(out, metadata)
                            job['output'] = str(renamed)
                        except (OSError, ValueError, FileExistsError) as e:
                            job['catalog_error'] = str(e)
                    self.set('Verificato',name+' pronto. Intermedio conservato.',100,queue=self.state['queue'])
                    if not job.get('catalog_title'):
                        threading.Thread(target=catalog_automatically, args=([out],), daemon=True).start()
                    notify('X99 Ripper: '+name+' completato e verificato.')
                except Exception as e:
                    job['status']='errore';job['error']=str(e)
                    self.set('Errore',name+': '+str(e),log=name+': '+str(e),queue=self.state['queue'])
                    notify('X99 Ripper: errore in '+name+': '+str(e))
            with self.lock:
                done=sum(j['status']=='completato' for j in self.state['queue'])
                errors=sum(j['status']=='errore' for j in self.state['queue'])
                ready=sum(j['status']=='pronto' for j in self.state['queue'])
            phase = 'In attesa' if ready else 'Coda terminata'
            self.set(phase,f'{done} completati · {ready} in attesa · {errors} da controllare.',100,job=None)
            notify(f'X99 Ripper: {done} completati, {ready} in attesa, {errors} errori.')
        finally:
            with self.lock:self.encoding=False


manager = None

class Handler(BaseHTTPRequestHandler):
    def allowed(self):
        if self.client_address[0] in {'127.0.0.1', ALLOWED_PROXY, HOST} | ALLOWED_LAN_CLIENTS:
            return True
        self.send_error(403)
        return False

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        if TAILSCALE_HOST and self.server.server_address[0] == TAILSCALE_HOST:
            if parsed.path != BASE+'/films/stream':
                self.send_error(403); return
            selected = authorized_stream(parsed.query)
            if not selected:
                self.send_error(403); return
            self.send_range_file(selected, 'video/x-matroska', False)
            return
        if not self.allowed(): return
        if parsed.path in (BASE+'/manifest.webmanifest', BASE+'/sw.js', BASE+'/icon-192.png', BASE+'/icon-512.png'):
            files = {BASE+'/manifest.webmanifest': ('manifest.webmanifest', 'application/manifest+json'),
                     BASE+'/sw.js': ('sw.js', 'text/javascript; charset=utf-8'),
                     BASE+'/icon-192.png': ('icon-192.png', 'image/png'),
                     BASE+'/icon-512.png': ('icon-512.png', 'image/png')}
            filename, ctype = files[parsed.path]
            self.reply(Path(__file__).with_name(filename).read_bytes(), ctype); return
        if parsed.path == BASE+'/films':
            page = Path(__file__).with_name('films.html').read_text().replace('TOKEN', TOKEN)
            self.reply(page.encode(), 'text/html; charset=utf-8'); return
        if parsed.path == BASE+'/films/data':
            self.reply(json.dumps(film_data(), ensure_ascii=False).encode(), 'application/json'); return
        if parsed.path == BASE+'/films/poster':
            choices = urllib.parse.parse_qs(parsed.query).get('path', [])
            selected = completed_file(choices[0]) if len(choices) == 1 else None
            image = film_library.poster(selected) if selected else None
            if not image: self.send_error(404); return
            self.reply(image.read_bytes(), 'image/jpeg'); return
        if parsed.path == BASE+'/films/playlist':
            choices = urllib.parse.parse_qs(parsed.query).get('path', [])
            selected = completed_file(choices[0]) if len(choices) == 1 else None
            if not selected or not TAILSCALE_HOST: self.send_error(404); return
            title = film_library.read(selected).get('title') or selected.stem.removesuffix('_H265')
            body = ('#EXTM3U\n#EXTINF:-1,' + title.replace('\n', ' ') + '\n' +
                    signed_stream_url(selected.relative_to(COMPLETED_DIR)) + '\n').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'audio/x-mpegurl; charset=utf-8')
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" +
                             urllib.parse.quote(selected.stem + '.m3u', safe=''))
            self.send_header('Content-Length', str(len(body)))
            self.end_headers(); self.wfile.write(body)
            return
        if parsed.path == BASE+'/films/search':
            query = urllib.parse.parse_qs(parsed.query).get('q', [''])[0]
            try: self.reply(json.dumps(film_library.search(query), ensure_ascii=False).encode(), 'application/json')
            except (ValueError, OSError, TimeoutError, KeyError) as exc: self.reply_error(400, str(exc))
            return
        if parsed.path == BASE+'/films/details':
            qid = urllib.parse.parse_qs(parsed.query).get('id', [''])[0]
            try: self.reply(json.dumps(film_library.details(qid), ensure_ascii=False).encode(), 'application/json')
            except (ValueError, OSError, TimeoutError, KeyError) as exc: self.reply_error(400, str(exc))
            return
        if parsed.path == BASE+'/films/suggest':
            choices = urllib.parse.parse_qs(parsed.query).get('path', [])
            selected = completed_file(choices[0]) if len(choices) == 1 else None
            if not selected: self.send_error(404); return
            try: self.reply(json.dumps(film_library.suggest(selected)[:5], ensure_ascii=False).encode(), 'application/json')
            except (ValueError, OSError, TimeoutError, KeyError) as exc: self.reply_error(400, str(exc))
            return
        if parsed.path in (BASE+'/films/preview/status', BASE+'/films/preview/file', BASE+'/films/preview/tracks'):
            params = urllib.parse.parse_qs(parsed.query)
            choices = params.get('path', [])
            selected = completed_file(choices[0]) if len(choices) == 1 else None
            if not selected: self.send_error(404); return
            if parsed.path.endswith('/tracks'):
                try: self.reply(json.dumps(film_preview.tracks(selected), ensure_ascii=False).encode(), 'application/json')
                except (OSError, ValueError, subprocess.SubprocessError) as exc: self.reply_error(400, str(exc))
                return
            try:
                audio = int(params['audio'][0]) if 'audio' in params else None
                subtitle = int(params['subtitle'][0]) if 'subtitle' in params else 0
                if audio is not None or subtitle:
                    audio, subtitle = film_preview.validate(selected, audio, subtitle)
                    if audio == film_preview.validate(selected)[0] and subtitle == 0: audio = None
            except (ValueError, subprocess.SubprocessError) as exc:
                self.reply_error(400, str(exc)); return
            if parsed.path.endswith('/status'):
                self.reply(json.dumps(film_preview.status(selected, audio, subtitle), ensure_ascii=False).encode(), 'application/json')
            else:
                preview = film_preview.destination(selected, audio, subtitle)
                if not preview.is_file(): self.send_error(404); return
                self.send_range_file(preview, 'video/mp4', False)
            return
        if parsed.path == BASE+'/films/file':
            choices = urllib.parse.parse_qs(parsed.query).get('path', [])
            selected = completed_file(choices[0]) if len(choices) == 1 else None
            if not selected:
                self.send_error(404); return
            self.send_range_file(selected, 'video/x-matroska', True); return
        if self.path == BASE+'/state':
            with manager.lock:
                queue = []
                for job in manager.state['queue']:
                    item = dict(job)
                    if item['status'] == 'completato' and (item.get('title_id') is not None or item.get('managed_source')):
                        item['source_present'] = Path(item.get('source') or '').is_file()
                    if item['status'] == 'recupero':
                        recovery = item.get('recovery') or {}
                        if recovery.get('percent') is not None:
                            item['percent'] = recovery['percent']
                    if item['status'] == 'estrazione':
                        try:
                            size = max((p.stat().st_size for p in Path(item['folder']).glob('*.mkv')), default=0)
                            expected = int(item.get('expected_bytes') or 0)
                            item['written_gib'] = round(size / 2**30, 2)
                            item['expected_gib'] = round(expected / 2**30, 2) if expected else None
                            item['percent'] = min(99, round(size / expected * 100, 1)) if expected else None
                        except (OSError, ValueError):
                            pass
                    queue.append(item)
                state={**manager.state,'scan_busy':manager.busy,
                       'encode_busy':manager.encoding,'desktop_active':manager.desktop_active(),
                       'disc_present':manager.media_present(),
                       'queue':queue,
                       'desktop_jobs':desktop_jobs(),
                       'free_gib':round(shutil.disk_usage(core.ROOT).free/2**30,1)}
            try: state['storage'] = storage_breakdown()
            except OSError: state['storage'] = None
            data = json.dumps(state,ensure_ascii=False).encode()
            self.reply(data,'application/json'); return
        if self.path not in (BASE, BASE+'/'):
            self.send_error(404); return
        page = (Path(__file__).with_name('web.html').read_text()
                .replace('LAN_URL', 'http://' + HOST + ':' + str(PORT) + BASE)
                .replace('TOKEN', TOKEN))
        self.reply(page.encode(),'text/html; charset=utf-8')

    def do_POST(self):
        if not self.allowed(): return
        if not secrets.compare_digest(self.headers.get('X-Ripper-Token',''),TOKEN):
            self.send_error(403); return
        parsed = urllib.parse.urlsplit(self.path)
        if parsed.path == BASE+'/films/poster':
            try:
                choices = urllib.parse.parse_qs(parsed.query).get('path', [])
                selected = completed_file(choices[0]) if len(choices) == 1 else None
                if not selected: raise ValueError('Film non trovato.')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 10 * 2**20: raise ValueError('Copertina troppo grande: massimo 10 MB.')
                film_library.save_poster(selected, self.rfile.read(size), self.headers.get('Content-Type', ''))
                self.reply(json.dumps({'saved': True}).encode(), 'application/json')
            except (ValueError, OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
                self.reply_error(400, str(exc))
            return
        if parsed.path == BASE+'/video/import':
            staged = None
            if not UPLOAD_LOCK.acquire(blocking=False):
                self.reply_error(409, 'Un altro video è in caricamento. Riprova quando ha finito.'); return
            try:
                if manager.busy or manager.desktop_active():
                    raise RuntimeError('Acquisizione disco in corso: carica il video quando il lettore è libero.')
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 80 * 2**30:
                    raise ValueError('Dimensione del video non valida o superiore a 80 GiB.')
                if shutil.disk_usage(core.ROOT).free < size + 4 * 2**30:
                    raise ValueError('Spazio insufficiente per caricare e comprimere il video.')
                options = urllib.parse.parse_qs(parsed.query)
                filename = options.get('name', [''])[0]
                crop_mode = options.get('crop', ['auto'])[0]
                deinterlace = options.get('deinterlace', ['auto'])[0]
                if not filename or len(filename) > 220 or crop_mode not in ('auto', 'none') or deinterlace not in ('auto', 'on', 'off'):
                    raise ValueError('Nome o opzioni del video non validi.')
                incoming = core.ROOT / '.uploads'
                incoming.mkdir(exist_ok=True)
                staged = incoming / (uuid.uuid4().hex + '.part')
                self.connection.settimeout(120)
                with staged.open('xb') as output:
                    remaining = size
                    while remaining:
                        chunk = self.rfile.read(min(4 * 2**20, remaining))
                        if not chunk: raise ValueError('Caricamento interrotto; riprova.')
                        output.write(chunk)
                        remaining -= len(chunk)
                job = manager.import_video(staged, filename, crop_mode, deinterlace)
                self.reply(json.dumps({'name': job['name'], 'started': True}, ensure_ascii=False).encode(), 'application/json')
            except (ValueError, OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                self.reply_error(400, str(exc))
            finally:
                if staged is not None: staged.unlink(missing_ok=True)
                UPLOAD_LOCK.release()
            return
        if self.path == BASE+'/import/complete':
            try:
                payload = self.read_json()
                output = manager.import_external_final(payload['staged'], payload['sha256'], payload['title'])
                self.reply(json.dumps({'imported': str(output)}, ensure_ascii=False).encode(), 'application/json')
            except (ValueError, KeyError, TypeError, OSError, RuntimeError, FileExistsError) as exc:
                self.reply_error(400, str(exc))
            return
        if self.path == BASE+'/films/preview/start':
            try:
                payload = self.read_json()
                path = completed_file(payload['path'])
                if not path: raise ValueError('Film non trovato.')
                audio = int(payload['audio']) if payload.get('audio') is not None else None
                subtitle = int(payload.get('subtitle', 0))
                result = film_preview.start(path, lambda: bool(manager and manager.encoding), audio, subtitle)
                self.reply(json.dumps(result, ensure_ascii=False).encode(), 'application/json')
            except (ValueError, KeyError) as exc: self.reply_error(400, str(exc))
            return
        if self.path == BASE+'/films/metadata':
            try:
                payload = self.read_json()
                path = completed_file(payload['path'])
                if not path: raise ValueError('Film non trovato.')
                if film_preview.status(path)['state'] in ('waiting', 'working'):
                    raise ValueError('Attendi che la copia per il browser sia pronta.')
                if payload.get('wikidata_id'):
                    metadata = film_library.details(payload['wikidata_id'])
                else:
                    metadata = {**film_library.read(path), 'title': payload['title'],
                                'year': payload.get('year'), 'source': 'manuale'}
                old_path = str(path)
                renamed = film_library.save(path, metadata)
                if manager:
                    with manager.lock:
                        for job in manager.state['queue']:
                            if job.get('output') == old_path:
                                job['output'] = str(renamed)
                        manager.set(queue=manager.state['queue'])
                self.reply(json.dumps({'path': renamed.relative_to(COMPLETED_DIR).as_posix()}, ensure_ascii=False).encode(), 'application/json')
            except (ValueError, KeyError, FileExistsError, OSError) as exc: self.reply_error(400, str(exc))
            return
        if self.path == BASE+'/duplicate/choose':
            try:
                payload = self.read_json()
                ok = manager.choose_duplicate(payload['action'], payload.get('existing', ''), payload['name'])
            except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
                ok = False
            self.reply(json.dumps({'started': ok}).encode(), 'application/json'); return
        if self.path.startswith(BASE+'/duplicate/finalize/'):
            try:
                payload = self.read_json()
                ok = manager.finalize_replacement(int(self.path.rsplit('/', 1)[1]),
                              payload['name'], payload['existing'])
            except (ValueError, KeyError, TypeError, OSError, RuntimeError, json.JSONDecodeError):
                ok = False
            self.reply(json.dumps({'started': ok}).encode(), 'application/json'); return
        if self.path == BASE+'/scan':ok=manager.start_scan()
        elif self.path == BASE+'/encode':ok=manager.start_encoding()
        elif self.path == BASE+'/retry':ok=manager.retry_errors()
        elif self.path in (BASE+'/limit/5',BASE+'/limit/10'):
            ok=manager.set_limit(int(self.path.rsplit('/',1)[1]))
        elif self.path in (BASE+'/encoder/cpu', BASE+'/encoder/gpu'):
            ok=manager.set_encoder(self.path.rsplit('/', 1)[1])
        elif self.path in (BASE+'/recovery/on', BASE+'/recovery/off'):
            ok=manager.set_recovery(self.path.endswith('/on'))
        elif self.path.startswith(BASE+'/catalog/'):
            try:
                payload = self.read_json()
                index = int(self.path.rsplit('/', 1)[1])
                if payload.get('delete') is True:
                    ok = manager.delete_ready(index, payload.get('confirm_name'))
                elif payload.get('wikidata_id'):
                    ok = manager.set_catalog_choice(index, payload['wikidata_id'])
                else:
                    ok = manager.set_catalog(index, payload['title'], payload.get('year'))
            except (ValueError, KeyError, TypeError, OSError, TimeoutError, json.JSONDecodeError):
                ok = False
        elif self.path.startswith(BASE+'/source/remove/'):
            try:
                payload = self.read_json()
                index = int(self.path.rsplit('/', 1)[1])
                ok = manager.remove_verified_source(index, payload.get('confirm_name'))
            except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError):
                ok = False
        else:
            self.send_error(404);return
        self.reply(json.dumps({'started':ok}).encode(),'application/json')

    def reply(self,body,ctype):
        self.send_response(200); self.send_header('Content-Type',ctype)
        self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def reply_error(self, status, message):
        body = json.dumps({'error': message}, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def read_json(self):
        size = int(self.headers.get('Content-Length', '0'))
        if not 0 < size <= 2048: raise ValueError('Dati troppo lunghi o mancanti.')
        return json.loads(self.rfile.read(size))
    def send_range_file(self, path, ctype, download):
        size = path.stat().st_size
        start, end = 0, size - 1
        requested = self.headers.get('Range', '')
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if not match or (not match[1] and not match[2]):
                self.send_error(416); return
            if match[1]:
                start = int(match[1]); end = int(match[2]) if match[2] else end
            else:
                start = max(0, size - int(match[2]))
            if start >= size or end < start:
                self.send_error(416); return
            end = min(end, size - 1)
        self.send_response(206 if requested else 200)
        self.send_header('Content-Type', ctype)
        if download:
            self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" +
                             urllib.parse.quote(path.name, safe=''))
        self.send_header('Accept-Ranges', 'bytes')
        if requested:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(end - start + 1))
        self.end_headers()
        try:
            with path.open('rb') as stream:
                stream.seek(start)
                remaining = end - start + 1
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk: break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
    def log_message(self,*args): pass

if __name__ == '__main__':
    manager = Manager()
    threading.Thread(target=catalog_automatically, daemon=True).start()
    if HOST != '127.0.0.1':
        threading.Thread(target=ThreadingHTTPServer(('127.0.0.1', PORT), Handler).serve_forever,
                         daemon=True).start()
    if TAILSCALE_HOST:
        threading.Thread(target=ThreadingHTTPServer((TAILSCALE_HOST, PORT), Handler).serve_forever,
                         daemon=True).start()
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
