#!/usr/bin/env python3
"""Configure private Telegram completion/error notifications without echoing the bot token."""
import getpass
import json
import os
import re
import secrets
import shlex
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

CONFIG = Path.home()/'.config/x99-ripper/config'

def api(token, method, params=None):
    url='https://api.telegram.org/bot'+token+'/'+method
    data=urllib.parse.urlencode(params).encode() if params is not None else None
    with urllib.request.urlopen(urllib.request.Request(url,data=data),timeout=12) as response:
        result=json.load(response)
    if not result.get('ok'):
        raise RuntimeError('Telegram non ha accettato la richiesta.')
    return result['result']

def main():
    if not sys.stdin.isatty():
        raise RuntimeError('Apri un terminale interattivo sull’OptiPlex.')
    token=getpass.getpass('Token del bot personale (non appare sullo schermo): ').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+',token):
        raise RuntimeError('Formato del token non valido.')
    try:
        info=api(token,'getMe')
    except Exception:
        raise RuntimeError('Impossibile verificare il bot. Controlla il token e la connessione.') from None
    code='RIPPER-'+secrets.token_hex(3).upper()
    print('Apri Telegram e invia al bot @'+info['username']+' questo messaggio: '+code)
    input('Poi premi Invio qui. Attenderò fino a 90 secondi… ')
    chat=None
    deadline=time.monotonic()+90
    while time.monotonic()<deadline:
        try:
            updates=api(token,'getUpdates',{'limit':100,'timeout':2})
            for update in reversed(updates):
                msg=update.get('message') or update.get('edited_message') or {}
                if msg.get('text','').strip()==code:
                    chat=str(msg['chat']['id']);break
        except Exception:
            break
        if chat:break
        time.sleep(3)
    if not chat:
        chat=input('Chat non trovata. Se conosci il suo ID numerico, inseriscilo (altrimenti Invio per annullare): ').strip()
    if not re.fullmatch(r'-?\d+',chat or ''):
        raise RuntimeError('Configurazione annullata: ID chat mancante o non valido.')
    try:
        api(token,'sendMessage',{'chat_id':chat,'text':'X99 Ripper: notifiche attive.'})
    except Exception:
        raise RuntimeError('Messaggio di prova non inviato. Nessuna configurazione salvata.') from None
    CONFIG.parent.mkdir(parents=True,exist_ok=True)
    old=CONFIG.read_text().splitlines() if CONFIG.exists() else []
    old=[x for x in old if not x.startswith(('export X99_RIPPER_TELEGRAM_TOKEN=','export X99_RIPPER_TELEGRAM_CHAT_ID='))]
    old += ['export X99_RIPPER_TELEGRAM_TOKEN='+shlex.quote(token),
            'export X99_RIPPER_TELEGRAM_CHAT_ID='+shlex.quote(chat)]
    tmp=CONFIG.with_suffix('.tmp')
    fd=os.open(tmp,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f:f.write('\n'.join(old)+'\n')
    os.replace(tmp,CONFIG)
    CONFIG.chmod(0o600)
    subprocess.run(['systemctl','--user','restart','x99-ripper-web.service'],check=True)
    print('Notifiche attive. Il messaggio di prova è arrivato nella chat.')

if __name__=='__main__':
    try:main()
    except Exception as exc:
        print('Errore: '+str(exc),file=sys.stderr);sys.exit(1)
