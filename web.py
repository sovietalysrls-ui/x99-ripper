#!/usr/bin/env python3
"""Small browser controller for the OptiPlex DVD workflow."""
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import ripper as core

BASE = '/ripper'
STATE = core.STATE / 'web-state.json'
TOKEN = secrets.token_urlsafe(24)
PORT = int(os.environ.get('X99_RIPPER_WEB_PORT', '8766'))
HOST = os.environ.get('X99_RIPPER_WEB_HOST', '127.0.0.1')
ALLOWED_PROXY = os.environ.get('X99_RIPPER_ALLOWED_PROXY', '')
TELEGRAM_TOKEN = os.environ.get('X99_RIPPER_TELEGRAM_TOKEN', '')
TELEGRAM_CHAT = os.environ.get('X99_RIPPER_TELEGRAM_CHAT_ID', '')
DESKTOP_MEDIA_CACHE = {}


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
                      'encoder': 'cpu'}
        try:
            old = json.loads(STATE.read_text())
            if old.get('phase') not in ('Pronto', 'In attesa', 'Coda terminata', 'Errore'):
                old['phase'] = 'Interrotto'
                old['detail'] = 'Il servizio è stato riavviato. Sorgente e parziale sono conservati.'
            self.state.update(old)
            if self.state.get('encoder') not in ('cpu', 'gpu'):
                self.state['encoder'] = 'cpu'
            for job in self.state['queue']:
                if job['status'] in ('compressione', 'estrazione'):
                    job['status']='errore'
                    job['error']='Lavoro interrotto; controlla il file sorgente e usa Riprova errori.'
        except (OSError, ValueError):
            pass
        self.busy = False
        self.encoding = False
        self.last_media = False
        threading.Thread(target=self.watch, daemon=True).start()

    def save(self):
        core.STATE.mkdir(parents=True, exist_ok=True)
        tmp = STATE.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.state, ensure_ascii=False))
        tmp.replace(STATE)

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
            active = active or encoding or next((j for j in jobs if j['status'] == 'analisi'),None)
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
                if media and not self.last_media and not busy and not desktop:
                    self.start_scan()
                elif desktop:
                    self.mirror_desktop()
                elif not media and self.state['phase'] in ('Desktop attivo','Estrazione desktop','Compressione desktop','Analisi desktop'):
                    self.set('Pronto', 'Inserisci un DVD.',0,job=None)
                self.last_media = media
            except Exception as e:
                self.set(log='Controllo disco: ' + str(e))
            time.sleep(8)

    def start_scan(self):
        with self.lock:
            pending = sum(j['status'] in ('pronto','compressione') for j in self.state['queue'])
            if self.busy or self.desktop_active() or pending >= self.state['max_discs']:
                return False
            self.busy = True
        threading.Thread(target=self.workflow, daemon=True).start()
        return True

    def workflow(self):
        try:
            self.set('Rilevamento', 'Cerco il titolo principale…', 0)
            p = subprocess.run(['makemkvcon', '-r', 'info', 'dev:' + core.DEVICE],
                               capture_output=True, text=True, timeout=240)
            text = p.stdout + p.stderr
            titles = core.titles_from_robot(text)
            if not titles:
                raise RuntimeError('Nessun titolo DVD trovato. Controlla il disco o il log di MakeMKV.')
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
            folder = core.ROOT / name
            if folder.exists():
                name += '_' + time.strftime('%Y%m%d_%H%M%S'); folder = core.ROOT / name
            expected = int(title['size'] or 0)
            if shutil.disk_usage(core.ROOT).free < max(expected+8*2**30, 12*2**30):
                raise RuntimeError('Spazio insufficiente per estrazione e compressione.')
            folder.mkdir(parents=True)
            queued = {'name':name,'folder':str(folder),'source':'',
                      'title_id':title['id'],'duration':title['duration'],
                      'expected_bytes':expected,'status':'estrazione'}
            with self.lock:
                self.set('Estrazione', 'Estraggo ' + name + '…', 0,
                         job=queued, queue=self.state['queue'] + [queued])
            rc = self.run(['makemkvcon','-r','mkv','dev:'+core.DEVICE,str(title['id']),str(folder)], folder/'estrazione.log')
            if rc: raise RuntimeError('MakeMKV si è fermato. Controlla estrazione.log; i file restano conservati.')
            files = sorted(folder.glob('*.mkv'), key=lambda p:p.stat().st_size, reverse=True)
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
                queued['status'] = 'pronto'
                jobs = self.state['queue']
            self.set('In attesa', name+' estratto. Inserisci il prossimo DVD oppure avvia la compressione.',
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

    def start_encoding(self):
        with self.lock:
            if (self.encoding or self.desktop_active() or
                    not any(j['status']=='pronto' for j in self.state['queue'])):
                return False
            self.encoding = True
        threading.Thread(target=self.encode_queue,daemon=True).start()
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
            self.set(queue=self.state['queue'])
        return True

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

    def encode_queue(self):
        try:
            while True:
                with self.lock:
                    job=next((j for j in self.state['queue'] if j['status']=='pronto'),None)
                    if not job: break
                    job['status']='compressione'
                    self.set('Analisi', 'Preparo '+job['name']+'…',0,job=job)
                try:
                    source=Path(job['source']);folder=Path(job['folder']);name=job['name']
                    encoder = ('nvenc_h265_10bit' if self.state.get('encoder') == 'gpu'
                               else 'x265_10bit')
                    job['encoder'] = encoder
                    meta=core.probe(source)
                    subs=core.sub_choices(meta)
                    plan=core.audio_plan(meta,core.audio_choices(meta))
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
                    rc=self.run(core.hb_command(source,partial,crop_value,plan,subs,encoder),folder/'compressione.log',True)
                    if rc: raise RuntimeError('HandBrake terminato con codice '+str(rc))
                    self.set('Verifica','Controllo '+name+'…',99)
                    core.verify(meta,partial,subs,plan)
                    partial.rename(out)
                    job['status']='completato';job['output']=str(out)
                    if job.get('catalog_title'):
                        try:
                            write_jellyfin_nfo(folder, job['catalog_title'], job.get('catalog_year'))
                        except OSError as e:
                            job['catalog_error'] = str(e)
                    self.set('Verificato',name+' pronto. Intermedio conservato.',100,queue=self.state['queue'])
                    notify('X99 Ripper: '+name+' completato e verificato.')
                except Exception as e:
                    job['status']='errore';job['error']=str(e)
                    self.set('Errore',name+': '+str(e),log=name+': '+str(e),queue=self.state['queue'])
                    notify('X99 Ripper: errore in '+name+': '+str(e))
            with self.lock:
                done=sum(j['status']=='completato' for j in self.state['queue'])
                errors=sum(j['status']=='errore' for j in self.state['queue'])
            self.set('Coda terminata',f'{done} completati · {errors} da controllare.',100,job=None)
            notify(f'X99 Ripper: coda terminata. {done} completati, {errors} errori.')
        finally:
            with self.lock:self.encoding=False


manager = None

class Handler(BaseHTTPRequestHandler):
    def allowed(self):
        if self.client_address[0] in ('127.0.0.1', ALLOWED_PROXY):
            return True
        self.send_error(403)
        return False

    def do_GET(self):
        if not self.allowed(): return
        if self.path == BASE+'/state':
            with manager.lock:
                queue = []
                for job in manager.state['queue']:
                    item = dict(job)
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
                data = json.dumps(state,ensure_ascii=False).encode()
            self.reply(data,'application/json'); return
        if self.path not in (BASE, BASE+'/'):
            self.send_error(404); return
        page = Path(__file__).with_name('web.html').read_text().replace('TOKEN', TOKEN)
        self.reply(page.encode(),'text/html; charset=utf-8')

    def do_POST(self):
        if not self.allowed(): return
        if not secrets.compare_digest(self.headers.get('X-Ripper-Token',''),TOKEN):
            self.send_error(403); return
        if self.path == BASE+'/scan':ok=manager.start_scan()
        elif self.path == BASE+'/encode':ok=manager.start_encoding()
        elif self.path == BASE+'/retry':ok=manager.retry_errors()
        elif self.path in (BASE+'/limit/5',BASE+'/limit/10'):
            ok=manager.set_limit(int(self.path.rsplit('/',1)[1]))
        elif self.path in (BASE+'/encoder/cpu', BASE+'/encoder/gpu'):
            ok=manager.set_encoder(self.path.rsplit('/', 1)[1])
        elif self.path.startswith(BASE+'/catalog/'):
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 512: raise ValueError
                payload = json.loads(self.rfile.read(size))
                index = int(self.path.rsplit('/', 1)[1])
                ok = manager.set_catalog(index, payload['title'], payload.get('year'))
            except (ValueError, KeyError, TypeError, json.JSONDecodeError):
                ok = False
        else:
            self.send_error(404);return
        self.reply(json.dumps({'started':ok}).encode(),'application/json')

    def reply(self,body,ctype):
        self.send_response(200); self.send_header('Content-Type',ctype)
        self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(body)))
        self.end_headers(); self.wfile.write(body)
    def log_message(self,*args): pass

if __name__ == '__main__':
    manager = Manager()
    ThreadingHTTPServer((HOST,PORT),Handler).serve_forever()
