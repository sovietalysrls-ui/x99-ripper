#!/usr/bin/env python3
"""Register an X99 final already transferred to the OptiPlex staging folder."""
import json
import os
import re
import sys
import urllib.error
import urllib.request


def receive(staged, digest, title, base=None):
    base = base or f"http://127.0.0.1:{os.environ.get('X99_RIPPER_WEB_PORT', '8766')}/ripper"
    page = urllib.request.urlopen(base + '/', timeout=10).read().decode('utf-8')
    match = re.search(r"'X-Ripper-Token':'([^']+)'", page)
    if not match:
        raise RuntimeError('Autorizzazione DiscFlow non trovata.')
    payload = json.dumps({'staged': staged, 'sha256': digest, 'title': title}).encode()
    request = urllib.request.Request(base + '/import/complete', data=payload,
                                     headers={'X-Ripper-Token': match.group(1),
                                              'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.load(response)['imported']
    except urllib.error.HTTPError as error:
        raise RuntimeError(error.read().decode('utf-8', errors='replace')) from error


if __name__ == '__main__':
    if len(sys.argv) != 4:
        raise SystemExit('Uso: receive_external.py ID/film.mkv.part SHA256 titolo')
    print(receive(*sys.argv[1:]))
