#!/usr/bin/env python3
"""Read-only progress window for an already running X99 Ripper job."""
import json
import re
import time
from pathlib import Path
import tkinter as tk
from tkinter import ttk

STATE=Path.home()/'.local/state/x99-ripper/jobs.json'

def amount(size):
    return f'{size/2**30:.2f} GiB' if size>=2**30 else f'{size/2**20:.0f} MiB'

def last_percent(path):
    try:
        with open(path,'rb') as f:
            f.seek(max(0,f.seek(0,2)-16000))
            tail=f.read().decode(errors='replace')
        matches=re.findall(r'Encoding:.*?(\d+(?:\.\d+)?)\s*%',tail)
        return float(matches[-1]) if matches else None
    except OSError:return None

def process_active(stage):
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():continue
        try:
            name=(proc/'comm').read_text().strip()
            if stage=='compressione' and name=='HandBrakeCLI':return True
            if stage=='estrazione' and name=='makemkvcon':
                args=(proc/'cmdline').read_bytes().replace(b'\0',b' ')
                if b' -r mkv ' in args:return True
        except (OSError,PermissionError):pass
    return False

class Monitor:
    def __init__(self):
        root=tk.Tk();self.root=root
        root.title('X99 Ripper · avanzamento')
        root.geometry('650x185');root.resizable(False,False);root.configure(bg='#f3f5f8')
        style=ttk.Style(root);style.theme_use('clam')
        style.configure('TFrame',background='#f3f5f8')
        style.configure('TLabel',background='#f3f5f8',foreground='#253246',font=('DejaVu Sans',11))
        style.configure('Title.TLabel',background='#f3f5f8',foreground='#172337',font=('DejaVu Sans',17,'bold'))
        style.configure('Small.TLabel',background='#f3f5f8',foreground='#55647a',font=('DejaVu Sans',10))
        style.configure('TProgressbar',troughcolor='#e3e9f1',background='#225bb5',thickness=12)
        frame=ttk.Frame(root,padding=20);frame.pack(fill='both',expand=True)
        self.title=tk.StringVar(value='Controllo lavorazione…')
        self.detail=tk.StringVar(value='Lettura stato in corso.')
        self.note=tk.StringVar(value='Questa finestra legge soltanto lo stato; chiuderla non ferma il lavoro.')
        ttk.Label(frame,textvariable=self.title,style='Title.TLabel').pack(anchor='w',pady=(0,14))
        ttk.Label(frame,textvariable=self.detail,wraplength=600).pack(anchor='w',pady=(0,13))
        self.bar=ttk.Progressbar(frame,maximum=100);self.bar.pack(fill='x',pady=(0,15))
        ttk.Label(frame,textvariable=self.note,style='Small.TLabel',wraplength=600).pack(anchor='w')
        self.last_size=0;self.last_growth=time.monotonic();self.mode='determinate';self.missing_count=0
        root.after(100,self.update)
        root.mainloop()
    def update(self):
        try:
            jobs=json.loads(STATE.read_text())
            job=jobs[-1] if jobs else None
        except (OSError,ValueError):job=None
        if not job:
            self.title.set('Nessun lavoro registrato');self.detail.set('Apri X99 Ripper per iniziare.');self.bar.configure(value=0)
        else:
            name=job.get('name','Film');stage=job.get('status','')
            folder=Path(job.get('folder','/nonexistent'))
            if stage in {'estrazione','compressione','recupero'}:
                self.missing_count=0 if process_active(stage) else self.missing_count+1
            else:self.missing_count=0
            if stage in {'estrazione','compressione','recupero'} and self.missing_count>=3:
                percent=last_percent(folder/'compressione.log') if stage=='compressione' else None
                self.title.set('Lavorazione interrotta · '+name)
                self.detail.set(f'HandBrake non è più attivo. Ultimo avanzamento: {percent:.2f}%.' if percent is not None else 'Il processo non è più attivo.')
                self.bar.stop();self.bar.configure(mode='determinate',value=percent or 0);self.mode='determinate'
                self.note.set('L’MKV originale è salvo. Apri X99 Ripper aggiornato e premi Riprova compressione.')
            elif stage=='recupero':
                rescue=job.get('rescue') or {}
                percent=float(rescue.get('percent') or 0)
                self.title.set('Recupero in corso · '+name)
                self.detail.set((rescue.get('phase') or 'copia del disco')+f' · {percent:.1f}% letto')
                self.bar.stop();self.bar.configure(mode='determinate',value=percent);self.mode='determinate'
                self.note.set('La mappa di recupero è conservata: un recupero interrotto riprende da dove era arrivato.')
            elif stage=='estrazione':
                files=list(folder.glob('*.mkv'))
                size=max((p.stat().st_size for p in files),default=0)
                if size>self.last_size:self.last_growth=time.monotonic()
                self.last_size=size
                self.title.set('Estrazione in corso · '+name)
                self.detail.set(f'{amount(size)} scritti sul Samsung')
                if self.mode!='indeterminate':
                    self.bar.configure(mode='indeterminate');self.bar.start(20);self.mode='indeterminate'
                idle=int(time.monotonic()-self.last_growth)
                if idle>30:self.note.set(f'Nessuna crescita da {idle} s. Il lettore può essere impegnato in un nuovo tentativo di lettura.')
                else:self.note.set('Il file cresce. Puoi chiudere questo monitor; l’estrazione continua.')
            elif stage=='compressione':
                percent=last_percent(folder/'compressione.log')
                self.title.set('Compressione in corso · '+name)
                self.detail.set(f'HandBrake: {percent:.0f}% completato' if percent is not None else 'HandBrake sta preparando il video.')
                self.bar.stop();self.bar.configure(mode='determinate',value=percent or 0);self.mode='determinate'
                self.note.set('Il DVD è stato estratto. Il file finale viene verificato prima della pulizia.')
            elif stage=='verificato':
                self.title.set('Film completato · '+name)
                self.detail.set('Output verificato: '+Path(job.get('output','')).name)
                self.bar.stop();self.bar.configure(mode='determinate',value=100);self.mode='determinate'
                self.note.set('L’intermedio è stato rimosso.' if job.get('intermediate_removed') else 'Controlla lo stato dell’intermedio nella cartella.')
            elif stage in {'errore','interrotto'}:
                self.title.set('Serve attenzione · '+name)
                self.detail.set(job.get('error') or 'Controlla il log.')
                self.bar.stop();self.bar.configure(mode='determinate',value=0);self.mode='determinate'
                self.note.set('Il file intermedio resta disponibile per recuperare il lavoro.')
            else:
                self.title.set('Lavoro in corso · '+name)
                self.detail.set('Fase: '+stage)
                self.bar.stop();self.bar.configure(mode='determinate',value=0);self.mode='determinate'
        self.root.after(2000,self.update)

if __name__=='__main__':Monitor()
