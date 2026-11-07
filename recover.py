#!/usr/bin/env python3
"""Recupero di dischi ottici rovinati con GNU ddrescue + MakeMKV da immagine ISO.

Metodo "ostinato": il disco viene copiato in un'immagine ISO in più fasi
(settori sani, ritaglio dei bordi danneggiati, scavo delle aree difficili,
giri extra anche in direzione inversa) finché ogni giro recupera ancora
dati. La mappa di avanzamento è riusata a ogni invocazione, quindi non si
ricomincia mai da capo. L'estrazione del titolo avviene poi dall'immagine
con MakeMKV (sorgente "iso:") invece che dal lettore.
"""
import os
import shutil
import subprocess
import threading
from pathlib import Path

SECTOR = 2048
EXTRA_ROUNDS = max(0, int(os.environ.get('X99_RIPPER_RESCUE_ROUNDS', '3')))
MIN_IMPROVEMENT = 64 * 1024
STALL_LIMIT = 2
STATUS_KEYS = {'+': 'finished', '-': 'bad', '?': 'nontried', '*': 'nontrimmed', '/': 'nonscraped'}


def binary():
    return shutil.which('ddrescue')


def available():
    """ddrescue è installato? (pacchetto Debian/Ubuntu: gddrescue)"""
    return binary() is not None


def iso_source(iso_path):
    """Sorgente MakeMKV per lavorare su un'immagine ISO."""
    return 'iso:' + str(iso_path)


def map_stats(map_path):
    """Legge un mapfile ddrescue e restituisce i byte per stato."""
    stats = {'total': 0, 'finished': 0, 'bad': 0, 'nontried': 0,
             'nontrimmed': 0, 'nonscraped': 0, 'percent': 0.0, 'phase': ''}
    try:
        text = Path(map_path).read_text()
    except OSError:
        return stats
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split()
        if len(parts) != 3:
            continue
        try:
            length = int(parts[1], 16)
        except ValueError:
            continue
        key = STATUS_KEYS.get(parts[2])
        if key is None:
            continue
        stats[key] += length
        stats['total'] += length
    if stats['total']:
        stats['percent'] = round(stats['finished'] / stats['total'] * 100, 2)
    return stats


def summary(stats):
    """Frase compatta sul risultato del recupero, per messaggi e notifiche."""
    total = stats.get('total') or 0
    if not total:
        return 'nessun dato letto dal disco'
    bad = stats.get('bad', 0)
    pending = total - stats.get('finished', 0)
    text = f"{stats.get('percent', 0):.1f}% letto · {bad / 2**20:.1f} MiB irrecuperabili"
    if pending > bad:
        text += f" · {pending / 2**20:.1f} MiB non ancora recuperati"
    return text


def _run_pass(cmd, log_path):
    with open(log_path, 'a') as log:
        log.write('$ ' + subprocess.list2cmdline(list(map(str, cmd))) + '\n')
        log.flush()
        process = subprocess.Popen(list(map(str, cmd)), stdout=log,
                                   stderr=subprocess.STDOUT)
        return process.wait()


def rescue_iso(device, iso_path, map_path, log_path, progress=None):
    """Copia il disco in ISO con più fasi e giri di tentativo.

    progress, se fornito, viene chiamato ogni ~2 secondi con le statistiche
    aggiornate del mapfile (chiave "phase" inclusa). Restituisce le
    statistiche finali. Riutilizza iso e mapfile esistenti: un recupero
    interrotto riprende dal punto raggiunto.
    """
    exe = binary()
    if not exe:
        raise RuntimeError('ddrescue non trovato: installare con "sudo apt install gddrescue".')
    iso_path = Path(iso_path)
    map_path = Path(map_path)
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(phase):
        if progress is None:
            return
        stats = map_stats(map_path)
        stats['phase'] = phase
        try:
            progress(stats)
        except Exception:
            pass
        return stats

    base = [exe, '-b', str(SECTOR)]
    use_direct = True

    def run_pass(extra, phase):
        nonlocal use_direct
        cmd = base + list(extra)
        if use_direct:
            cmd += ['-d']
        cmd += [str(device), str(iso_path), str(map_path)]
        rc = _run_pass(cmd, log_path)
        stats = map_stats(map_path)
        if rc != 0 and use_direct and not stats['total']:
            # Regola: non arrendersi al primo errore; riprovare senza accesso diretto.
            use_direct = False
            cmd = base + list(extra) + [str(device), str(iso_path), str(map_path)]
            rc = _run_pass(cmd, log_path)
            stats = map_stats(map_path)
        return rc, stats

    def monitor_loop(stop, phase):
        while not stop.wait(2):
            emit(phase)

    def monitored_pass(extra, phase):
        stop = threading.Event()
        watcher = threading.Thread(target=monitor_loop, args=(stop, phase), daemon=True)
        watcher.start()
        try:
            return run_pass(extra, phase)
        finally:
            stop.set()
            watcher.join(timeout=3)
            emit(phase)

    # Fase 1: lettura rapida di tutti i settori sani (nessuno scavo).
    rc, stats = monitored_pass(['-n'], 'copia dei settori sani')
    if rc != 0 and not stats['total'] and not iso_path.exists():
        raise RuntimeError('ddrescue non è riuscito a leggere il disco (codice '
                           f'{rc}). Verifica che il disco sia inserito e leggi recupero.log.')

    # Fase 2: ritaglio dei bordi delle aree danneggiate.
    monitored_pass(['-r', '1'], 'ritaglio dei bordi danneggiati')
    # Fase 3: scavo delle aree difficili con tentativi ripetuti.
    monitored_pass(['-r', '3'], 'scavo delle aree difficili')

    # Fasi extra: provare e riprovare finché ogni giro recupera ancora dati.
    previous_pending = None
    stalled = 0
    for round_index in range(1, EXTRA_ROUNDS + 1):
        stats = map_stats(map_path)
        pending = stats['total'] - stats['finished']
        if previous_pending is not None:
            improvement = previous_pending - pending
            if improvement < MIN_IMPROVEMENT:
                stalled += 1
            else:
                stalled = 0
            if stalled >= STALL_LIMIT or pending == 0:
                break
        previous_pending = pending
        direction = ['-R'] if round_index % 2 == 0 else []
        monitored_pass(['-r', '3'] + direction, f'giro extra {round_index}')

    stats = map_stats(map_path)
    stats['phase'] = 'concluso'
    if progress is not None:
        try:
            progress(stats)
        except Exception:
            pass
    return stats
