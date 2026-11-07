#!/usr/bin/env python3
"""Notify Telegram when desktop jobs need attention or finish."""
import json
import os
import shlex
import time
import urllib.parse
import urllib.request
from pathlib import Path

STATE = Path.home() / '.local/state/x99-ripper'
JOBS = STATE / 'jobs.json'
SENT = STATE / 'desktop-notified.json'
CONFIG = Path.home() / '.config/x99-ripper/config'
MESSAGES = {
    'recupero': '{name}: recupero del disco rovinato avviato con ddrescue.',
    'in coda': '{name}: estrazione completata, in attesa di compressione.',
    'da confermare': '{name}: serve confermare il ritaglio prima della compressione.',
    'verificato': '{name}: compressione completata e file verificato.',
    'errore': '{name}: errore. {error}',
    'interrotto': '{name}: lavoro interrotto. {error}',
}


def credentials():
    values = {}
    try:
        for line in CONFIG.read_text().splitlines():
            parts = shlex.split(line)
            if len(parts) == 2 and parts[0] == 'export' and '=' in parts[1]:
                key, value = parts[1].split('=', 1)
                values[key] = value
    except (OSError, ValueError):
        pass
    return values.get('X99_RIPPER_TELEGRAM_TOKEN'), values.get('X99_RIPPER_TELEGRAM_CHAT_ID')


def read_jobs():
    try:
        jobs = json.loads(JOBS.read_text())
        return jobs if isinstance(jobs, list) else []
    except (OSError, ValueError):
        return []


def save(sent):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = SENT.with_suffix('.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(sent, stream)
    os.replace(tmp, SENT)
    SENT.chmod(0o600)


def send(token, chat, message):
    body = urllib.parse.urlencode({'chat_id': chat, 'text': 'X99 Ripper: ' + message}).encode()
    req = urllib.request.Request('https://api.telegram.org/bot' + token + '/sendMessage', data=body)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return json.load(response).get('ok') is True
    except Exception:
        return False


def main():
    try:
        sent = json.loads(SENT.read_text())
    except (OSError, ValueError):
        sent = {str(j.get('id')): [j.get('status')] for j in read_jobs() if j.get('id')}
        save(sent)
    while True:
        token, chat = credentials()
        if token and chat:
            changed = False
            for job in read_jobs():
                job_id = str(job.get('id') or '')
                status = job.get('status')
                if not job_id or status not in MESSAGES or status in sent.get(job_id, []):
                    continue
                message = MESSAGES[status].format(name=job.get('name', 'Film'),
                                                   error=str(job.get('error') or 'Controlla la pagina.')[:300])
                if send(token, chat, message):
                    sent.setdefault(job_id, []).append(status)
                    changed = True
            if changed:
                save(sent)
        time.sleep(8)


if __name__ == '__main__':
    main()
