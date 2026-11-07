#!/usr/bin/env python3
"""Copy a verified X99 final to the OptiPlex and register it in DiscFlow."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import shlex
import subprocess
import uuid

import ripper


def send(path, host, transfer_id=None):
    path = Path(path).expanduser()
    if path.is_symlink() or not path.is_file() or not path.name.endswith('_H265.mkv'):
        raise ValueError('Scegli un film finale *_H265.mkv verificato sull’X99.')
    path = path.resolve()
    meta = ripper.probe(path)
    if len(ripper.tracks(meta, 'video')) != 1 or not ripper.tracks(meta, 'audio'):
        raise ValueError('Il file finale deve contenere video e audio.')
    ripper.verify_decode_samples(path, float(meta['format']['duration']))
    title = path.stem.removesuffix('_H265')
    transfer_id = transfer_id or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', transfer_id):
        raise ValueError('ID di ripresa non valido.')
    print('ID trasferimento:', transfer_id, flush=True)
    remote_dir = f'.local/state/x99-ripper/incoming/{transfer_id}'
    staged = f'{transfer_id}/film.mkv.part'
    subprocess.run(['ssh', host, f'mkdir -p -- {shlex.quote(remote_dir)}'], check=True)
    subprocess.run(['rsync', '-a', '--partial', '--append-verify', '--progress',
                    str(path), f'{host}:{remote_dir}/film.mkv.part'], check=True)
    with path.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    command = shlex.join(['python3', '.local/share/x99-ripper/receive_external.py',
                           staged, digest, title])
    result = subprocess.run(['ssh', host, command], check=True, capture_output=True, text=True)
    return result.stdout.strip()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Copia un film finale verificato nel catalogo DiscFlow dell’OptiPlex.')
    parser.add_argument('film', help='Percorso del file *_H265.mkv sull’X99')
    parser.add_argument('--host', default=os.environ.get('DISCFLOW_OPTIPLEX_SSH', 'andrea@100.107.201.72'))
    parser.add_argument('--resume', help='ID di un trasferimento interrotto, 32 caratteri esadecimali')
    args = parser.parse_args()
    print('Film importato:', send(args.film, args.host, args.resume))
