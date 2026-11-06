#!/usr/bin/env python3
"""X99 DVD queue: MakeMKV extraction, reviewed HandBrake encoding."""
import csv
import json
import os
import queue
import re
import signal
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = Path(os.environ.get('X99_RIPPER_OUTPUT_DIR', '/mnt/x99/DVD_RIPPATI')).expanduser()
DEVICE = os.environ.get('X99_RIPPER_DEVICE', '/dev/sr0')
STATE = Path.home() / '.local/state/x99-ripper'
HB = ([shutil.which('HandBrakeCLI')] if shutil.which('HandBrakeCLI') else
      ['flatpak', 'run', '--command=HandBrakeCLI', 'fr.handbrake.ghb'])
SUBTITLE_LANGUAGES = ('ita', 'eng', 'spa')
COPY_ENCODERS = {'aac':'copy:aac', 'ac3':'copy:ac3', 'eac3':'copy:eac3',
    'truehd':'copy:truehd', 'dts':'copy:dts', 'mp2':'copy:mp2',
    'mp3':'copy:mp3', 'flac':'copy:flac'}
DURATION = re.compile(r'\b\d{1,2}:\d{2}:\d{2}\b')
CROP = re.compile(r'autocrop\s*[=:]\s*(\d+)/(\d+)/(\d+)/(\d+)', re.I)
MAKEMKV_KEY_PAGE = 'https://forum.makemkv.com/forum/viewtopic.php?f=5&t=1053'
LICENSE_ERROR = re.compile(r'evaluation period (?:has )?expired|shareware functionality unavailable|registration key|activation key|license (?:key|expired)', re.I)


def robot_fields(line):
    kind, _, rest = line.partition(':')
    if kind not in {'TINFO', 'CINFO'}:
        return None
    try:
        fields = next(csv.reader([rest]))
        return kind, int(fields[0]), int(fields[1]), fields[-1]
    except (ValueError, IndexError, csv.Error):
        return None


def titles_from_robot(output):
    found = {}
    for line in output.splitlines():
        parsed = robot_fields(line)
        if not parsed or parsed[0] != 'TINFO':
            continue
        _, number, code, value = parsed
        found.setdefault(number, {'id': number, 'name': '', 'duration': '', 'size': ''})
        item = found[number]
        if code == 2:
            item['name'] = value
        if code == 49 and not item['name']:
            item['name'] = value
        if code == 9 or (not item['duration'] and DURATION.fullmatch(value)):
            if DURATION.fullmatch(value):
                item['duration'] = value
        if code == 11 and value.isdigit():
            item['size'] = value
    return sorted(found.values(), key=lambda x: x['id'])


def safe_name(value):
    value = re.sub(r'[^\w .-]+', '_', value, flags=re.UNICODE).strip(' ._')
    return (value[:80] or 'Film')


def disc_label(output):
    for line in output.splitlines():
        if not line.startswith('DRV:'):continue
        try:
            fields=next(csv.reader([line[4:]]))
            if fields[-1] == DEVICE and fields[-2]:return fields[-2]
        except (csv.Error, IndexError):pass
    return ''


def disc_type_from_robot(output):
    for line in output.splitlines():
        parsed=robot_fields(line)
        if parsed and parsed[0]=='CINFO' and parsed[1]==1:
            value=parsed[3].lower()
            if 'blu-ray' in value or 'bd disc' in value:return 'blu-ray'
            if 'dvd' in value:return 'dvd'
    return 'unknown'


def optical_properties():
    try:
        result=subprocess.run(['udevadm','info','--query=property','--name',DEVICE],
            capture_output=True,text=True,timeout=5)
        if result.returncode:return {}
        return dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
    except (OSError,subprocess.TimeoutExpired):return {}


def bluray_capable(properties):
    return any(key.startswith('ID_CDROM_BD') and value=='1' for key,value in properties.items())


def bluray_media(properties):
    return any(key.startswith('ID_CDROM_MEDIA_BD') and value=='1' for key,value in properties.items())


def probe(path):
    p = subprocess.run(['ffprobe', '-v', 'error', '-show_entries',
        'format=duration,size:stream=index,codec_type,codec_name,profile,channels,width,height:stream_tags=language,title:stream_disposition=default,forced',
        '-of', 'json', str(path)], capture_output=True, text=True, timeout=90)
    if p.returncode:
        raise RuntimeError('ffprobe: ' + p.stderr[-1500:])
    return json.loads(p.stdout)


def tracks(meta, kind):
    return [s for s in meta['streams'] if s['codec_type'] == kind]


def lang(s):
    x = s.get('tags', {}).get('language', 'und').lower()
    return {'it': 'ita', 'en': 'eng', 'es': 'spa', 'fr': 'fra', 'fre': 'fra',
            'de': 'deu', 'ger': 'deu', 'ja': 'jpn'}.get(x, x)


def audio_choices(meta, original_language=None):
    aa = tracks(meta, 'audio')
    def best(language):
        matches = [(i, stream) for i, stream in enumerate(aa, 1) if lang(stream) == language]
        return max(matches, key=lambda item: (item[1].get('channels', 0), -item[0]))[0] if matches else None
    italian = best('ita')
    spanish = best('spa')
    original = best(lang({'tags': {'language': original_language}})) if original_language else None
    if original is None:
        candidate = next((lang(stream) for stream in aa if lang(stream) not in ('ita', 'spa', 'und')), None)
        original = best(candidate) if candidate else None
    order = ([italian, spanish, original] if italian else [original, spanish])
    selected = list(dict.fromkeys(index for index in order if index is not None))
    return selected or ([1] if aa else [])


def sub_choices(meta):
    return [i for i, s in enumerate(tracks(meta, 'subtitle'), 1) if lang(s) in SUBTITLE_LANGUAGES]


def audio_plan(meta, chosen):
    aa=tracks(meta,'audio')
    if not chosen:raise ValueError('Nessuna traccia audio disponibile.')
    plan=[]
    for index in chosen:
        if not 1<=index<=len(aa):raise ValueError('Indice audio non valido.')
        stream=aa[index-1];language=lang(stream)
        if not re.fullmatch(r'[a-z]{2,3}', language):raise ValueError('Lingua audio non valida.')
        if language=='ita':
            encoder='av_aac';bitrate='160';mixdown='stereo'
        elif stream['codec_name'] in COPY_ENCODERS:
            encoder=COPY_ENCODERS[stream['codec_name']];bitrate='0';mixdown='none'
        else:
            encoder='av_aac';bitrate='384' if stream.get('channels',0)>=6 else '160'
            mixdown='5point1' if stream.get('channels',0)>=6 else 'stereo'
        plan.append({'index':index,'language':language,'encoder':encoder,
            'bitrate':bitrate,'mixdown':mixdown,'codec':stream['codec_name'],
            'channels':stream.get('channels')})
    return plan


def hb_command(source, output, crop, plan, subs, encoder='x265_10bit'):
    if not re.fullmatch(r'\d+:\d+:\d+:\d+', crop):
        raise ValueError('Crop: usare top:bottom:left:right, quattro numeri.')
    if encoder not in ('x265_10bit', 'nvenc_h265_10bit'):
        raise ValueError('Encoder video non supportato.')
    preset = 'slow' if encoder == 'x265_10bit' else 'medium'
    quality = '22' if encoder == 'x265_10bit' else '28'
    cmd = HB + ['-i', str(source), '-o', str(output), '-f', 'av_mkv',
        '-e', encoder, '--encoder-preset', preset, '-q', quality,
        '--crop-mode', 'custom', '--crop', crop, '-m',
        '-a', ','.join(str(x['index']) for x in plan),
        '-E', ','.join(x['encoder'] for x in plan),
        '-B', ','.join(x['bitrate'] for x in plan),
        '-6', ','.join(x['mixdown'] for x in plan),
        '--audio-fallback', 'none',
        '-s', ','.join(map(str, subs)) if subs else 'none',
        '--subtitle-burned=none', '--subtitle-default=none']
    return cmd


def verify(source_meta, outpath, expected_subs, plan):
    if not outpath.exists() or outpath.stat().st_size < 100000:
        raise RuntimeError('Output assente o troppo piccolo.')
    m = probe(outpath)
    v = tracks(m, 'video')
    a = tracks(m, 'audio')
    s = tracks(m, 'subtitle')
    if len(v) != 1 or v[0]['codec_name'] != 'hevc' or '10' not in v[0].get('profile', ''):
        raise RuntimeError('Video HEVC 10-bit non verificato.')
    if len(a)!=len(plan):raise RuntimeError('Numero tracce audio diverso dalla selezione.')
    for actual,expected in zip(a,plan):
        if lang(actual)!=expected['language']:
            raise RuntimeError('Lingua audio diversa dalla selezione.')
        wanted=expected['codec'] if expected['encoder'].startswith('copy:') else 'aac'
        if actual['codec_name']!=wanted:
            raise RuntimeError('Codec audio diverso dalla selezione.')
        if expected['encoder'].startswith('copy:') and actual.get('channels')!=expected['channels']:
            raise RuntimeError('Numero canali diverso dalla sorgente in passthrough.')
    if not a[0].get('disposition',{}).get('default') or any(x.get('disposition',{}).get('default') for x in a[1:]):
        raise RuntimeError('Audio predefinito errato.')
    if any(x['language']=='ita' for x in plan) and lang(a[0])!='ita':
        raise RuntimeError('Italiano non impostato come audio predefinito.')
    if len(s) != len(expected_subs) or any(x.get('disposition', {}).get('default') for x in s):
        raise RuntimeError('Numero sottotitoli o flag default errato.')
    src_subs=tracks(source_meta,'subtitle')
    if any(lang(actual)!=lang(src_subs[index-1]) for actual,index in zip(s,expected_subs)):
        raise RuntimeError('Lingua sottotitoli diversa dalla sorgente.')
    if any(x.get('disposition', {}).get('forced') for x in s):
        raise RuntimeError('Un sottotitolo risulta forzato: controllare manualmente.')
    src_duration = float(source_meta['format']['duration'])
    dst_duration = float(m['format']['duration'])
    if abs(src_duration - dst_duration) > max(10, src_duration * .01):
        raise RuntimeError('Durata output diversa dalla sorgente.')
    verify_decode_samples(outpath, dst_duration)
    return m


def verify_decode_samples(path, duration):
    """Decode short video samples near the beginning and end of the output."""
    starts = (5, max(5, int(duration) - 20)) if duration > 40 else (0,)
    for start in dict.fromkeys(starts):
        cmd = ['ffmpeg', '-nostdin', '-v', 'error', '-xerror', '-ss', str(start),
               '-i', str(path), '-t', '10', '-map', '0:v:0', '-an', '-sn',
               '-f', 'null', '-']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode:
            raise RuntimeError('Decodifica campione video fallita: ' + result.stderr[-600:])


def remove_owned_intermediate(job, output):
    """Remove only an app-created MKV after its final output exists."""
    if 'title_id' not in job:
        return False
    source=Path(job['source'])
    folder=Path(job['folder']).resolve()
    output=Path(output)
    if not output.is_file() or source.is_symlink() or not source.is_file():
        raise RuntimeError('Output o intermedio non disponibile: rimozione annullata.')
    if source.resolve().parent != folder or source.resolve() == output.resolve():
        raise RuntimeError('Percorso intermedio inatteso: rimozione annullata.')
    source.unlink()
    return True


def archive_partial(output):
    """Keep an interrupted encode intact before starting a new attempt."""
    output=Path(output)
    partial=output.with_name(output.stem+'.parziale.mkv')
    if not partial.exists():return None
    stamp=time.strftime('%Y%m%d-%H%M%S')
    archived=output.with_name(output.stem+f'.interrotto-{stamp}.mkv')
    counter=1
    while archived.exists():
        archived=output.with_name(output.stem+f'.interrotto-{stamp}-{counter}.mkv')
        counter+=1
    partial.rename(archived)
    return archived


class App:
    def __init__(self, root):
        self.root = root
        root.title('X99 Ripper — DVD in serie')
        root.geometry('1000x690')
        root.minsize(820, 650)
        STATE.mkdir(parents=True, exist_ok=True)
        self.events = queue.Queue()
        self.jobs = self.load_jobs()
        self.scan_busy = False
        self.extract_busy = False
        self.encode_busy = False
        self.disc_titles = []
        self.selected_job = None
        self.drive_lock = threading.Lock()
        self.scan_started = None
        self.encode_percent = None
        self.last_sizes = {}
        self.last_growth = {}
        self.read_warning = ''
        self.disc_type = 'unknown'
        properties=optical_properties()
        self.drive_bluray_capable = bluray_capable(properties) if properties.get('ID_CDROM')=='1' else None
        self.bluray_verified = self.load_bluray_verification()
        self.build_ui()
        self.refresh_jobs()
        self.refresh_activity()
        self.root.after(150, self.pump)
        self.root.after(2000, self.maybe_encode)
        self.root.protocol('WM_DELETE_WINDOW', self.close)

    def load_jobs(self):
        try:
            jobs = json.loads((STATE / 'jobs.json').read_text())
            for j in jobs:
                if j['status'] in {'estrazione', 'compressione'}:
                    j['interrupted_stage']=j['status']
                    j['status']='interrotto'
                    j['error']='Il programma si è chiuso durante il lavoro. L’MKV originale è conservato; premi Riprova compressione.' if j['interrupted_stage']=='compressione' else 'Il programma si è chiuso durante l’estrazione. Controlla la cartella e ripeti dal disco.'
            return jobs
        except (FileNotFoundError, ValueError):
            return []

    def save(self):
        p = STATE / 'jobs.json'
        tmp = p.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.jobs, indent=2, ensure_ascii=False))
        tmp.replace(p)

    @staticmethod
    def load_bluray_verification():
        try:
            state=json.loads((STATE/'bluray-verification.json').read_text())
            return bool(state.get('last_success'))
        except (OSError,ValueError):return False

    def set_bluray_verification(self,verified):
        self.bluray_verified=verified
        path=STATE/'bluray-verification.json'
        tmp=path.with_suffix('.tmp')
        tmp.write_text(json.dumps({'last_success':time.time() if verified else None}))
        tmp.replace(path)
        self.update_bluray_notice()

    def update_bluray_notice(self):
        if self.disc_type=='blu-ray' and not self.bluray_verified:
            self.license_notice_var.set('Blu-ray: verifica che la chiave beta o permanente di MakeMKV sia valida prima di estrarre. Dopo una prima estrazione riuscita questo avviso scompare.')
            self.license_notice.pack(fill='x',pady=(0,8),before=self.titles)
        else:
            self.license_notice.pack_forget()

    def build_ui(self):
        self.root.configure(bg='#f3f5f8')
        style=ttk.Style(self.root)
        style.theme_use('clam')
        style.configure('App.TFrame',background='#f3f5f8')
        style.configure('Card.TFrame',background='#ffffff')
        style.configure('App.TLabel',background='#f3f5f8',foreground='#263244',font=('DejaVu Sans',10))
        style.configure('Card.TLabel',background='#ffffff',foreground='#263244',font=('DejaVu Sans',10))
        style.configure('Title.TLabel',background='#f3f5f8',foreground='#172337',font=('DejaVu Sans',18,'bold'))
        style.configure('Section.TLabel',background='#ffffff',foreground='#172337',font=('DejaVu Sans',12,'bold'))
        style.configure('Activity.TLabel',background='#ffffff',foreground='#172337',font=('DejaVu Sans',15,'bold'))
        style.configure('Muted.TLabel',background='#ffffff',foreground='#55647a',font=('DejaVu Sans',10))
        style.configure('Warning.TLabel',background='#ffffff',foreground='#8a4d07',font=('DejaVu Sans',10))
        style.configure('Primary.TButton',background='#225bb5',foreground='#ffffff',font=('DejaVu Sans',10,'bold'),padding=(14,5),borderwidth=0)
        style.map('Primary.TButton',background=[('active','#17458e'),('disabled','#aab6c7')])
        style.configure('App.TButton',font=('DejaVu Sans',10),padding=(11,4))
        style.configure('App.Horizontal.TProgressbar',troughcolor='#e3e9f1',background='#225bb5',borderwidth=0,thickness=12)
        style.configure('Jobs.Treeview',rowheight=28,font=('DejaVu Sans',10),background='#ffffff',fieldbackground='#ffffff',foreground='#263244',borderwidth=0)
        style.configure('Jobs.Treeview.Heading',font=('DejaVu Sans',10,'bold'),background='#e8edf4',foreground='#263244',padding=(8,6))
        outer=ttk.Frame(self.root,style='App.TFrame',padding=(18,12));outer.pack(fill='both',expand=True)

        head=ttk.Frame(outer,style='App.TFrame');head.pack(fill='x',pady=(0,10))
        ttk.Label(head,text='X99 Ripper',style='Title.TLabel').pack(side='left')
        self.free_var=tk.StringVar()
        ttk.Label(head,textvariable=self.free_var,style='App.TLabel').pack(side='right')

        activity=ttk.Frame(outer,style='Card.TFrame',padding=14);activity.pack(fill='x',pady=(0,10))
        self.activity_var=tk.StringVar(value='Pronto per un disco')
        ttk.Label(activity,textvariable=self.activity_var,style='Activity.TLabel').pack(anchor='w')
        self.activity_detail=tk.StringVar(value='Inserisci un disco e premi Rileva disco.')
        ttk.Label(activity,textvariable=self.activity_detail,style='Muted.TLabel',wraplength=920).pack(anchor='w',pady=(4,7))
        self.progress=ttk.Progressbar(activity,style='App.Horizontal.TProgressbar',mode='determinate',maximum=100)
        self.progress.pack(fill='x')
        self.progress_mode='determinate'
        self.warning_var=tk.StringVar(value='')
        ttk.Label(activity,textvariable=self.warning_var,style='Warning.TLabel',wraplength=920).pack(anchor='w',pady=(5,0))
        self.retry_action=ttk.Button(activity,text='Riprova compressione',style='Primary.TButton',command=self.retry)

        disc=ttk.Frame(outer,style='Card.TFrame',padding=12);disc.pack(fill='x',pady=(0,10))
        self.disc_card=disc
        line=ttk.Frame(disc,style='Card.TFrame');line.pack(fill='x',pady=(0,7))
        ttk.Label(line,text='Disco',style='Section.TLabel').pack(side='left')
        ttk.Button(line,text='Rileva disco',style='App.TButton',command=self.scan_disc).pack(side='right')
        ttk.Button(line,text='Apri un MKV',style='App.TButton',command=self.import_mkv).pack(side='right',padx=(0,8))
        self.disk_status=tk.StringVar(value='Nessun disco analizzato.')
        ttk.Label(disc,textvariable=self.disk_status,style='Muted.TLabel',wraplength=920).pack(anchor='w',pady=(0,5))
        self.license_notice=ttk.Frame(disc,style='Card.TFrame')
        self.license_notice_var=tk.StringVar()
        ttk.Label(self.license_notice,textvariable=self.license_notice_var,style='Warning.TLabel',wraplength=555).pack(side='left')
        ttk.Button(self.license_notice,text='Apri pagina ufficiale',style='App.TButton',
            command=lambda:webbrowser.open(MAKEMKV_KEY_PAGE)).pack(side='right',padx=(10,0))
        self.titles=ttk.Treeview(disc,columns=('id','name','duration','size'),show='headings',height=1,style='Jobs.Treeview')
        for c,label,w in [('id','N.',48),('name','Titolo',360),('duration','Durata',115),('size','Dimensione',145)]:
            self.titles.heading(c,text=label);self.titles.column(c,width=w)
        self.titles.pack(fill='x')
        disc_actions=ttk.Frame(disc,style='Card.TFrame');disc_actions.pack(fill='x',pady=(7,0))
        ttk.Label(disc_actions,text='Nome del film',style='Card.TLabel').pack(side='left')
        self.name_var=tk.StringVar()
        ttk.Entry(disc_actions,textvariable=self.name_var,width=35).pack(side='left',padx=(8,12))
        ttk.Button(disc_actions,text='Estrai il film',style='Primary.TButton',command=self.extract).pack(side='left')

        jobs=ttk.Frame(outer,style='Card.TFrame',padding=12);jobs.pack(fill='x',pady=(0,10))
        self.jobs_card=jobs
        jobs_head=ttk.Frame(jobs,style='Card.TFrame');jobs_head.pack(fill='x',pady=(0,7))
        ttk.Label(jobs_head,text='Lavori',style='Section.TLabel').pack(side='left')
        self.details_button=ttk.Button(jobs_head,text='Mostra dettagli',style='App.TButton',command=self.toggle_details)
        self.details_button.pack(side='right')
        self.job_list=ttk.Treeview(jobs,columns=('name','status','source','output'),show='headings',height=2,style='Jobs.Treeview')
        for c,label,w in [('name','Film',150),('status','Fase',130),('source','Intermedio',235),('output','Risultato',235)]:
            self.job_list.heading(c,text=label);self.job_list.column(c,width=w)
        self.job_list.pack(fill='x');self.job_list.bind('<<TreeviewSelect>>',self.select_job)
        self.details_panel=ttk.Frame(jobs,style='Card.TFrame')
        self.details=tk.StringVar(value='Seleziona un lavoro per vedere le tracce.')
        ttk.Label(self.details_panel,textvariable=self.details,style='Muted.TLabel',wraplength=920,justify='left').pack(anchor='w',pady=(12,6))
        cr=ttk.Frame(self.details_panel,style='Card.TFrame');cr.pack(fill='x')
        ttk.Label(cr,text='Crop',style='Card.TLabel').pack(side='left')
        self.crop_var=tk.StringVar();ttk.Entry(cr,textvariable=self.crop_var,width=15).pack(side='left',padx=(6,8))
        for label,action in [('Anteprima',self.preview_crop),('Rianalizza',self.rescan_crop)]:
            ttk.Button(cr,text=label,style='App.TButton',command=action).pack(side='left',padx=3)
        recovery=ttk.Frame(self.details_panel,style='Card.TFrame');recovery.pack(fill='x',pady=(8,0))
        for label,action in [('Accoda',self.queue_encode),('Riprova',self.retry),('Apri cartella',self.open_folder),('Rimuovi intermedio',self.remove_intermediate)]:
            ttk.Button(recovery,text=label,style='App.TButton',command=action).pack(side='left',padx=3)
        self.track_var=tk.StringVar(value='Audio italiano, spagnolo e originale se presenti; italiano predefinito. Sottotitoli spenti.')
        ttk.Label(self.details_panel,textvariable=self.track_var,style='Muted.TLabel',wraplength=920).pack(anchor='w',pady=(9,0))
        self.details_open=False

        loghead=ttk.Frame(outer,style='App.TFrame');loghead.pack(fill='x')
        self.status=tk.StringVar(value='Pronto.')
        ttk.Label(loghead,textvariable=self.status,style='App.TLabel',wraplength=760).pack(side='left')
        self.log_button=ttk.Button(loghead,text='Mostra log',style='App.TButton',command=self.toggle_log)
        self.log_button.pack(side='right')
        self.log_frame=ttk.Frame(outer,style='Card.TFrame',padding=8)
        self.log=tk.Text(self.log_frame,height=8,wrap='word',font=('DejaVu Sans Mono',9),bg='#ffffff',fg='#263244',relief='flat')
        self.log.pack(fill='both',expand=True);self.log.configure(state='disabled')
        self.log_open=False
        self.update_free()

    def toggle_details(self):
        if not self.details_open and self.log_open:self.toggle_log()
        self.details_open=not self.details_open
        if self.details_open:
            self.disc_card.pack_forget()
            self.details_panel.pack(fill='x')
            self.details_button.configure(text='Nascondi dettagli')
        else:
            self.details_panel.pack_forget()
            self.details_button.configure(text='Mostra dettagli')
            self.disc_card.pack(fill='x',pady=(0,10),before=self.jobs_card)

    def toggle_log(self):
        if not self.log_open and self.details_open:self.toggle_details()
        self.log_open=not self.log_open
        if self.log_open:
            self.disc_card.pack_forget()
            self.log_frame.pack(fill='both',expand=True,pady=(8,0))
            self.log_button.configure(text='Nascondi log')
        else:
            self.log_frame.pack_forget()
            self.log_button.configure(text='Mostra log')
            if not self.details_open:self.disc_card.pack(fill='x',pady=(0,10),before=self.jobs_card)

    @staticmethod
    def amount(size):
        return f'{size / 2**30:.2f} GiB' if size >= 2**30 else f'{size / 2**20:.0f} MiB'

    @staticmethod
    def elapsed(start):
        seconds=max(0,int(time.time()-(start or time.time())))
        return f'{seconds//3600:02}:{seconds//60%60:02}:{seconds%60:02}'

    def set_progress(self, percent=None):
        mode='indeterminate' if percent is None else 'determinate'
        if mode!=self.progress_mode:
            self.progress.stop()
            self.progress.configure(mode=mode)
            self.progress_mode=mode
            if percent is None:self.progress.start(12)
        if percent is not None:self.progress.configure(value=max(0,min(100,percent)))

    def refresh_activity(self):
        extracting=next((j for j in self.jobs if j['status']=='estrazione'),None)
        encoding=next((j for j in self.jobs if j['status']=='compressione'),None)
        analyzing=next((j for j in self.jobs if j['status']=='analisi'),None)
        queued=next((j for j in self.jobs if j['status']=='in coda'),None)
        failed=next((j for j in reversed(self.jobs) if j['status'] in {'errore','interrotto'}),None)
        can_retry=bool(failed and not (extracting or encoding or self.scan_busy or analyzing or queued)
            and failed.get('source') and Path(failed['source']).is_file())
        if can_retry:
            if not self.retry_action.winfo_manager():self.retry_action.pack(anchor='w',pady=(10,0))
        else:self.retry_action.pack_forget()
        if extracting:
            folder=Path(extracting['folder'])
            files=list(folder.glob('*.mkv'))
            size=max((p.stat().st_size for p in files),default=0)
            expected=int(extracting.get('expected_bytes') or 0)
            if size>self.last_sizes.get(extracting['id'],0):
                self.last_growth[extracting['id']]=time.monotonic()
            self.last_sizes[extracting['id']]=size
            self.activity_var.set('Estrazione in corso · '+extracting['name'])
            detail=f'{self.amount(size)} scritti'
            if expected:
                percent=min(99,size/expected*100)
                detail+=f' su circa {self.amount(expected)} · {percent:.0f}% stimato'
                self.set_progress(percent)
            else:self.set_progress(None)
            detail+=' · tempo '+self.elapsed(extracting.get('started_at'))
            idle=time.monotonic()-self.last_growth.get(extracting['id'],time.monotonic())
            if size and idle>30:detail+=f' · nessuna crescita da {int(idle)} s, lettura del disco in corso'
            self.activity_detail.set(detail)
            self.warning_var.set(self.read_warning)
        elif encoding:
            self.activity_var.set('Compressione in corso · '+encoding['name'])
            partial=Path(encoding.get('output') or '').with_name(Path(encoding.get('output') or 'film.mkv').stem+'.parziale.mkv')
            size=partial.stat().st_size if partial.is_file() else 0
            detail='HEVC 10-bit · RF22 · Slow · tempo '+self.elapsed(encoding.get('started_at'))
            if self.encode_percent is not None:
                detail+=f' · {self.encode_percent:.0f}%';self.set_progress(self.encode_percent)
            else:self.set_progress(None)
            if size:detail+=' · output parziale '+self.amount(size)
            self.activity_detail.set(detail)
            self.warning_var.set('')
        elif self.scan_busy:
            self.activity_var.set('Lettura del disco in corso')
            self.activity_detail.set('Ricerca dei titoli · tempo '+self.elapsed(self.scan_started))
            self.warning_var.set('');self.set_progress(None)
        elif analyzing:
            self.activity_var.set('Analisi video · '+analyzing['name'])
            self.activity_detail.set('Rilevamento del crop e delle tracce. La compressione partirà da sola.')
            self.warning_var.set('');self.set_progress(None)
        elif queued:
            self.activity_var.set('Compressione in coda · '+queued['name'])
            self.activity_detail.set('HandBrake partirà automaticamente tra pochi istanti.')
            self.warning_var.set('');self.set_progress(None)
        elif failed:
            self.activity_var.set('Serve attenzione · '+failed['name'])
            self.activity_detail.set(failed.get('error') or 'Controlla il log e riprova.')
            self.warning_var.set('L’MKV originale e il file parziale sono conservati.' if can_retry else 'Controlla la cartella prima di ripetere l’estrazione.')
            self.set_progress(0)
        elif self.jobs and self.jobs[-1]['status']=='verificato':
            last=self.jobs[-1]
            self.activity_var.set('Film completato · '+last['name'])
            self.activity_detail.set('File finale verificato. '+('Intermedio rimosso.' if last.get('intermediate_removed') else 'Controlla l’intermedio nella cartella.'))
            self.warning_var.set(last.get('cleanup_error',''));self.set_progress(100)
        else:
            self.activity_var.set('Pronto per un disco')
            self.activity_detail.set('Inserisci un disco e premi Rileva disco. Dopo l’estrazione la compressione parte da sola.')
            self.warning_var.set('');self.set_progress(0)
        self.root.after(1000,self.refresh_activity)

    def update_free(self):
        try: self.free_var.set(f'Spazio libero: {shutil.disk_usage(ROOT).free/2**30:.1f} GiB')
        except OSError: self.free_var.set('Cartella di destinazione non disponibile')
        self.root.after(10000,self.update_free)

    def append(self,msg):
        self.log.configure(state='normal'); self.log.insert('end',msg.rstrip()+'\n'); self.log.see('end'); self.log.configure(state='disabled')

    def run_logged(self,cmd,logpath,timeout=None):
        self.events.put(('log','$ '+' '.join(map(str,cmd))))
        with open(logpath,'a') as f:
            f.write('$ '+subprocess.list2cmdline(list(map(str,cmd)))+'\n')
            p=subprocess.Popen(cmd,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,bufsize=1,errors='replace')
            try:
                line='';last_update=0
                while True:
                    ch=p.stdout.read(1)
                    if not ch:break
                    if ch in '\r\n':
                        if line:
                            f.write(line+'\n'); f.flush()
                            if line.startswith('MSG:2004'):
                                self.events.put(('warning','Il lettore ha segnalato un errore di lettura. L’app continua e verificherà il risultato.'))
                            if (line.startswith(('PRGV:','Progress:','Encoding:','MSG:','Muxing:'))
                                    and time.monotonic()-last_update>1):
                                self.events.put(('progress',line.strip()[:180]));last_update=time.monotonic()
                        line=''
                    else:
                        line=(line+ch)[-4000:]
                if line:f.write(line+'\n')
                return p.wait(timeout=timeout)
            except Exception:
                p.kill(); p.wait(); raise

    def scan_disc(self):
        if self.scan_busy or self.extract_busy:
            messagebox.showinfo('Unità occupata','Attendere il termine della scansione o estrazione.'); return
        self.scan_busy=True;self.scan_started=time.time();self.disk_status.set('Scansione disco in corso…')
        self.status.set('Il lettore sta cercando i titoli del DVD.')
        def work():
            try:
                properties=optical_properties()
                with self.drive_lock:
                    p=subprocess.run(['makemkvcon','-r','info','dev:'+DEVICE],capture_output=True,text=True,timeout=180)
                titles=titles_from_robot(p.stdout)
                disc_type=disc_type_from_robot(p.stdout)
                if disc_type=='unknown' and bluray_media(properties):disc_type='blu-ray'
                if disc_type=='unknown' and bluray_capable(properties) and LICENSE_ERROR.search(p.stdout):
                    disc_type='blu-ray'
                self.events.put(('scan',(titles,disc_label(p.stdout),disc_type,properties,
                    p.stdout[-1500:],p.returncode,bool(LICENSE_ERROR.search(p.stdout)))))
            except Exception as e:self.events.put(('scan_error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def extract(self):
        if self.extract_busy or self.scan_busy:return
        sel=self.titles.selection()
        if not sel:messagebox.showerror('Titolo','Selezionare un titolo.');return
        title_id=int(sel[0]); title=next(x for x in self.disc_titles if x['id']==title_id)
        name=safe_name(self.name_var.get() or title['name'] or f'DVD_{time.strftime("%Y%m%d_%H%M")}')
        try:
            free=shutil.disk_usage(ROOT).free
            size=int(title.get('size') or 0)
            if free < max(size + 8*2**30, 12*2**30):
                raise ValueError('Spazio insufficiente per intermedio e compressione; liberare spazio nella cartella di destinazione.')
        except Exception as e:messagebox.showerror('Spazio',str(e));return
        folder=ROOT/name
        if folder.exists():
            messagebox.showerror('Nome già esistente','La cartella esiste già. Scegliere un nome diverso.');return
        folder.mkdir(parents=True)
        j={'id':uuid.uuid4().hex,'name':name,'status':'estrazione','source':'','output':'','folder':str(folder),'title_id':title_id,'disc_type':self.disc_type,'expected_bytes':size,'started_at':time.time(),'error':''}
        self.jobs.append(j); self.save();self.refresh_jobs();self.extract_busy=True
        self.read_warning=''
        self.status.set('Estrazione avviata. La dimensione del file comparirà qui sopra.')
        def work():
            logpath=folder/'estrazione.log'
            try:
                with self.drive_lock:
                    rc=self.run_logged(['makemkvcon','-r','mkv','dev:'+DEVICE,str(title_id),str(folder)],logpath)
                if rc:
                    recent=logpath.read_text(errors='replace')[-8000:]
                    if j['disc_type']=='blu-ray' and LICENSE_ERROR.search(recent):
                        self.events.put(('bluray_license_error',j['id']))
                        raise RuntimeError('MakeMKV segnala una chiave scaduta o non valida. Apri la pagina ufficiale e registra una chiave valida; intermedio conservato.')
                    raise RuntimeError(f'MakeMKV terminato con codice {rc}. Consultare estrazione.log.')
                files=list(folder.glob('*.mkv'))
                if len(files)!=1:raise RuntimeError(f'Atteso 1 MKV, trovati {len(files)}. Controllare la cartella.')
                source=files[0]; probe(source)
                self.events.put(('extracted',(j['id'],str(source))))
                p=subprocess.run(['eject',DEVICE],capture_output=True,text=True,timeout=30)
                self.events.put(('eject',(p.returncode,p.stderr.strip())))
                self.scan_crop_async(j['id'],source)
            except Exception as e:self.events.put(('job_error',(j['id'],str(e))))
            finally:self.events.put(('extract_done',None))
        threading.Thread(target=work,daemon=True).start()

    def import_mkv(self):
        path=filedialog.askopenfilename(title='Scegli MKV intermedio',filetypes=[('Matroska','*.mkv')])
        if not path:return
        try:
            source=Path(path).resolve(); probe(source)
            name=safe_name(source.stem)
            folder=ROOT/name
            folder.mkdir(parents=True,exist_ok=True)
            j={'id':uuid.uuid4().hex,'name':name,'status':'analisi','source':str(source),'output':'','folder':str(folder),'error':''}
            self.jobs.append(j);self.save();self.refresh_jobs();self.scan_crop_async(j['id'],source)
        except Exception as e:messagebox.showerror('MKV',str(e))

    def scan_crop_async(self,jid,source):
        def work():
            try:
                p=subprocess.run(HB+['-i',str(source),'--scan','--previews','10:0'],capture_output=True,text=True,timeout=180)
                out=p.stdout+p.stderr
                crop=CROP.search(out)
                if p.returncode or not crop:raise RuntimeError('Crop automatico non rilevato. Vedere il log di scansione.')
                self.events.put(('crop',(jid,':'.join(crop.groups()),out[-1000:])))
            except Exception as e:self.events.put(('job_error',(jid,str(e))))
        threading.Thread(target=work,daemon=True).start()

    def selected(self):
        sel=self.job_list.selection()
        return next((j for j in self.jobs if sel and j['id']==sel[0]),None)

    def select_job(self,event=None):
        j=self.selected(); self.selected_job=j['id'] if j else None
        if not j:return
        self.crop_var.set(j.get('crop',''))
        source=Path(j.get('source') or '')
        if source.is_file():
            try:
                m=probe(source);a=tracks(m,'audio');s=tracks(m,'subtitle')
                describe=lambda arr: ' | '.join(f'{i} {lang(x)} {x["codec_name"]} {x.get("channels","")}ch' for i,x in enumerate(arr,1))
                self.details.set('Audio: '+describe(a)+'\nSottotitoli: '+describe(s))
                chosen=audio_choices(m); subs=sub_choices(m)
                self.track_var.set('Audio selezionati: '+', '.join(f'{i} {lang(a[i-1])}' for i in chosen)
                    +' · Sottotitoli selezionati: '+(', '.join(f'{i} {lang(s[i-1])}' for i in subs) or 'nessuno')
                    +' · Default audio: '+(lang(a[chosen[0]-1]) if chosen else 'nessuno')+'; sottotitoli spenti.')
            except Exception as e:self.details.set(str(e))
        else:self.details.set(j.get('error') or 'Intermedio non ancora disponibile.')

    def rescan_crop(self):
        j=self.selected()
        if j and j.get('source') and Path(j['source']).is_file():
            j['status']='analisi';self.save();self.refresh_jobs();self.scan_crop_async(j['id'],Path(j['source']))

    def preview_crop(self):
        j=self.selected()
        if not j or not j.get('source') or not Path(j['source']).is_file():return
        try:
            top,bottom,left,right=map(int,self.crop_var.get().split(':'))
            m=probe(j['source']);v=tracks(m,'video')[0]
            width=v['width']-left-right;height=v['height']-top-bottom
            if min(top,bottom,left,right)<0 or min(width,height)<2:raise ValueError('Valori crop non validi.')
            second=max(1,int(float(m['format']['duration'])*.5))
        except Exception as e:messagebox.showerror('Crop',str(e));return
        self.status.set('Preparo anteprima al centro del film…')
        def work():
            try:
                dest=STATE/'previews'/j['id'];dest.mkdir(parents=True,exist_ok=True)
                paths=[]
                for label,filters in [('prima',[]),('dopo',['-vf',f'crop={width}:{height}:{left}:{top}'])]:
                    path=dest/(label+'.png')
                    cmd=['ffmpeg','-nostdin','-v','error','-y','-ss',str(second),'-i',j['source'],*filters,'-frames:v','1',str(path)]
                    p=subprocess.run(cmd,capture_output=True,text=True,timeout=90)
                    if p.returncode:raise RuntimeError(p.stderr[-1000:])
                    paths.append(str(path))
                self.events.put(('preview',paths))
            except Exception as e:self.events.put(('preview_error',str(e)))
        threading.Thread(target=work,daemon=True).start()

    def show_preview(self,paths):
        win=tk.Toplevel(self.root);win.title('Anteprima crop — fotogramma a metà film')
        win.geometry('980x580')
        frame=ttk.Frame(win,padding=8);frame.pack(fill='both',expand=True)
        for col,(label,path) in enumerate(zip(('Prima','Dopo'),paths)):
            panel=ttk.Frame(frame);panel.grid(row=0,column=col,sticky='n',padx=5)
            ttk.Label(panel,text=label).pack()
            image=tk.PhotoImage(file=path)
            factor=max(1,(image.width()+460)//461,(image.height()+500)//501)
            image=image.subsample(factor)
            view=ttk.Label(panel,image=image);view.image=image;view.pack()
        self.status.set('Anteprima pronta. Controllare che il crop non tagli l’immagine.')

    def queue_encode(self,jid=None):
        automatic=jid is not None
        j=self.by_id(jid) if automatic else self.selected()
        if not j or j['status'] not in {'da confermare','errore','interrotto'}:
            if not automatic:messagebox.showinfo('Lavoro','Selezionare un MKV pronto.');
            return
        source=Path(j.get('source') or '')
        if not source.is_file():
            if automatic:self.events.put(('job_error',(j['id'],'MKV intermedio mancante.')))
            else:messagebox.showerror('Intermedio','MKV intermedio mancante.')
            return
        try:
            m=probe(source); chosen=audio_choices(m); subs=sub_choices(m)
            plan=audio_plan(m,chosen)
            crop=(j.get('crop','') if automatic else self.crop_var.get()).strip()
            nums=list(map(int,crop.split(':')))
            v=tracks(m,'video')[0]
            if len(nums)!=4 or min(nums)<0 or nums[0]+nums[1]>=v['height'] or nums[2]+nums[3]>=v['width']:raise ValueError('Crop fuori dalle dimensioni del video.')
            crop=':'.join(map(str,nums))
            out=Path(j['folder'])/(j['name']+'_H265.mkv')
            if out.exists():raise ValueError('Output già esistente: non verrà sovrascritto.')
            if shutil.disk_usage(out.parent).free < max(4*2**30, source.stat().st_size//4):raise ValueError('Spazio insufficiente per la compressione.')
            hb_command(source,out,crop,plan,subs)
            j.update(status='in coda',crop=crop,audio_indices=chosen,subs=subs,output=str(out),error='')
            self.save();self.refresh_jobs()
        except Exception as e:
            if automatic:self.events.put(('job_error',(j['id'],str(e))))
            else:messagebox.showerror('Verifica prima della coda',str(e))

    def maybe_encode(self):
        if not self.encode_busy:
            j=next((x for x in self.jobs if x['status']=='in coda'),None)
            if j:
                self.encode_busy=True;self.encode_percent=None;j['status']='compressione';j['started_at']=time.time();self.save();self.refresh_jobs()
                self.status.set('HandBrake sta avviando la compressione.')
                def work():
                    source=Path(j['source']);out=Path(j['output']);partial=out.with_name(out.stem+'.parziale.mkv')
                    try:
                        if out.exists():raise RuntimeError('Output finale già esistente: non viene sovrascritto.')
                        if partial.exists():raise RuntimeError('File parziale esistente: controllare prima di riprovare.')
                        m=probe(source); subs=j['subs'];plan=audio_plan(m,j['audio_indices'])
                        rc=self.run_logged(hb_command(source,partial,j['crop'],plan,subs),Path(j['folder'])/'compressione.log')
                        if rc:raise RuntimeError(f'HandBrake terminato con codice {rc}. Intermedio conservato.')
                        verify(m,partial,subs,plan)
                        partial.rename(out)
                        self.events.put(('encoded',(j['id'],False,'')))
                    except Exception as e:self.events.put(('job_error',(j['id'],str(e))))
                    finally:self.events.put(('encode_done',None))
                threading.Thread(target=work,daemon=True).start()
        self.root.after(2000,self.maybe_encode)

    def retry(self):
        j=self.selected()
        if not j or j['status'] not in {'errore','interrotto'}:
            j=next((x for x in reversed(self.jobs) if x['status'] in {'errore','interrotto'}),None)
        if not j or j['status'] not in {'errore','interrotto'}:return
        if j.get('source') and Path(j['source']).is_file():
            try:
                output=Path(j.get('output') or '')
                if output.name and output.exists():
                    raise RuntimeError('Output finale già presente: controlla la cartella.')
                archived=archive_partial(output) if output.name else None
                if archived:
                    j.setdefault('previous_partials',[]).append(str(archived))
                    self.append('Tentativo interrotto conservato: '+str(archived))
                j['status']='da confermare';j['error']='';self.save();self.refresh_jobs()
                self.queue_encode(j['id'])
                self.status.set('Nuova compressione accodata dall’MKV originale.')
            except Exception as exc:messagebox.showerror('Riprova compressione',str(exc))
        else:messagebox.showinfo('Estrazione','Inserire di nuovo il disco e avviare una nuova estrazione. La cartella del tentativo precedente resta intatta.')

    def open_folder(self):
        j=self.selected()
        if j:subprocess.Popen(['xdg-open',j['folder']])

    def remove_intermediate(self):
        j=self.selected()
        if not j or j['status']!='verificato':
            messagebox.showinfo('Verifica','La rimozione è disponibile solo dopo una compressione verificata.');return
        source=Path(j.get('source') or '')
        if not source.is_file():return
        if not source.is_relative_to(Path(j['folder'])):
            messagebox.showerror('File importato','Un MKV importato da un’altra cartella non viene rimosso da questa app.');return
        if messagebox.askyesno('Rimozione esplicita',f'Eliminare solo questo intermedio?\n{source}\n\nL’output verificato resterà intatto.'):
            source.unlink();j['intermediate_removed']=True;self.save();self.refresh_jobs()

    def refresh_jobs(self):
        current=self.selected_job
        for x in self.job_list.get_children():self.job_list.delete(x)
        self.job_list.configure(height=max(1,min(3,len(self.jobs))))
        labels={'estrazione':'Estrazione','analisi':'Analisi video','da confermare':'Pronto','in coda':'In coda','compressione':'Compressione','verificato':'Completato','errore':'Errore','interrotto':'Interrotto'}
        for j in self.jobs:
            source=Path(j['source']).name if j.get('source') else '—'
            output=Path(j['output']).name if j.get('output') else '—'
            self.job_list.insert('', 'end',iid=j['id'],values=(j['name'],labels.get(j['status'],j['status']),source,output))
        if current and self.job_list.exists(current):self.job_list.selection_set(current)

    def pump(self):
        try:
            while True:
                typ,data=self.events.get_nowait()
                if typ=='log':self.append(data)
                elif typ=='status':self.status.set(data);self.append(data)
                elif typ=='progress':
                    if data.startswith('Encoding:'):
                        match=re.search(r'(\d+(?:\.\d+)?)\s*%',data)
                        if match:
                            self.encode_percent=float(match.group(1))
                            self.status.set(f'HandBrake: {self.encode_percent:.0f}% completato.')
                    elif data.startswith('MSG:5014'):
                        self.status.set('MakeMKV ha iniziato a scrivere il film sul Samsung.')
                    elif data.startswith('PRGV:'):
                        self.status.set('MakeMKV sta leggendo il DVD.')
                elif typ=='warning':self.read_warning=data;self.append('AVVISO '+data)
                elif typ=='scan':
                    self.scan_busy=False;t,label,kind,properties,tail,rc,license_error=data
                    self.disc_titles=t;self.disc_type=kind
                    self.drive_bluray_capable=bluray_capable(properties) if properties.get('ID_CDROM')=='1' else None
                    if kind=='blu-ray' and license_error:self.set_bluray_verification(False)
                    self.update_bluray_notice()
                    for x in self.titles.get_children():self.titles.delete(x)
                    self.titles.configure(height=max(1,min(3,len(t))))
                    for x in t:
                        size=int(x['size'] or 0)
                        self.titles.insert('','end',iid=str(x['id']),values=(x['id'],x['name'],x['duration'],self.amount(size) if size else '—'))
                    if t:
                        best=max(t,key=lambda x:(self.seconds(x['duration']),int(x['size'] or 0)))
                        self.titles.selection_set(str(best['id']));self.name_var.set(safe_name(label or best['name'] or 'Film'))
                        format_name='Blu-ray' if kind=='blu-ray' else 'DVD' if kind=='dvd' else 'disco'
                        self.disk_status.set(f'{label or format_name}: {len(t)} titoli trovati; proposto il più lungo. Controllare la scelta.')
                    elif kind=='blu-ray' and license_error:
                        self.disk_status.set('MakeMKV non ha aperto il Blu-ray: verifica la chiave nella pagina ufficiale.')
                    elif self.drive_bluray_capable is False:
                        self.disk_status.set('Nessun titolo leggibile. Se hai inserito un Blu-ray, questo lettore legge solo DVD: serve un lettore Blu-ray.')
                    else:self.disk_status.set('Nessun titolo leggibile. Controllare disco/unità. '+tail[-250:])
                    self.append(tail)
                elif typ=='scan_error':self.scan_busy=False;self.disk_status.set('Scansione fallita: '+data)
                elif typ=='extracted':
                    jid,path=data;j=self.by_id(jid);j['source']=path;j['status']='analisi';self.save();self.refresh_jobs();self.status.set('Estratto. Scansione crop in corso.')
                    if j.get('disc_type')=='blu-ray':
                        self.set_bluray_verification(True)
                        self.status.set('Blu-ray estratto correttamente: avviso licenza disattivato.')
                elif typ=='bluray_license_error':
                    self.disc_type='blu-ray'
                    self.set_bluray_verification(False)
                    self.status.set('Chiave MakeMKV da aggiornare. Apri la pagina ufficiale nel riquadro Disco.')
                elif typ=='crop':
                    jid,crop,tail=data;j=self.by_id(jid);j['crop']=crop;j['status']='da confermare';self.save();self.refresh_jobs()
                    if self.selected_job==jid:self.select_job()
                    self.status.set('Crop automatico: '+crop+' — compressione in coda.');self.append(tail)
                    self.queue_encode(jid)
                elif typ=='eject':
                    rc,err=data
                    if rc:self.status.set('Estrazione finita, espulsione fallita: '+err)
                    else:self.status.set('Disco espulso. Inserire il successivo.')
                elif typ=='preview':self.show_preview(data)
                elif typ=='preview_error':self.status.set('Anteprima non riuscita: '+data)
                elif typ=='encoded':
                    jid,removed,cleanup_error=data
                    j=self.by_id(jid);j['status']='verificato';j['error']=''
                    j['intermediate_removed']=removed;j['cleanup_error']=cleanup_error
                    self.save();self.refresh_jobs()
                    if cleanup_error:
                        self.status.set('Output verificato; intermedio ancora presente: '+cleanup_error)
                    elif removed:
                        self.status.set('Output verificato; intermedio rimosso: '+j['output'])
                    else:
                        self.status.set('Output verificato: '+j['output'])
                elif typ=='job_error':
                    jid,msg=data;j=self.by_id(jid);j['status']='errore';j['error']=msg;self.save();self.refresh_jobs();self.status.set(j['name']+': '+msg);self.append('ERRORE '+msg)
                elif typ=='extract_done':self.extract_busy=False
                elif typ=='encode_done':self.encode_busy=False
        except queue.Empty:pass
        self.root.after(150,self.pump)

    def by_id(self,jid):return next(j for j in self.jobs if j['id']==jid)
    @staticmethod
    def seconds(d):
        try:h,m,s=map(int,d.split(':'));return h*3600+m*60+s
        except ValueError:return 0
    def close(self):
        if self.extract_busy or self.encode_busy:
            messagebox.showinfo('Operazione in corso','Attendere la fine dell’estrazione o compressione prima di chiudere.');return
        self.root.destroy()


if __name__=='__main__':
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    root=tk.Tk(); App(root);root.mainloop()
