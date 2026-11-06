#!/usr/bin/env python3
"""Open a signed DiscFlow stream in an available desktop media player."""
import shutil
import subprocess
import sys
import urllib.parse
from pathlib import Path

CONFIG = Path.home() / '.config/discflow/player-host'
PLAYERS = ('vlc', 'mpv', 'celluloid', 'smplayer', 'parole')


def stream_from_link(link, expected_host):
    wrapper = urllib.parse.urlsplit(link)
    if wrapper.scheme != 'discflow-player' or wrapper.netloc != 'open':
        raise ValueError('Collegamento DiscFlow non valido.')
    values = urllib.parse.parse_qs(wrapper.query).get('url', [])
    if len(values) != 1:
        raise ValueError('Film mancante nel collegamento.')
    stream = urllib.parse.urlsplit(values[0])
    if (stream.scheme != 'http' or stream.hostname != expected_host or stream.port != 8766
            or stream.path != '/ripper/films/stream'):
        raise ValueError('Indirizzo del film non autorizzato.')
    return values[0]


def main():
    if len(sys.argv) != 2:
        raise SystemExit('Serve un collegamento DiscFlow.')
    expected_host = CONFIG.read_text(encoding='utf-8').strip()
    url = stream_from_link(sys.argv[1], expected_host)
    player = next((shutil.which(name) for name in PLAYERS if shutil.which(name)), None)
    if not player:
        raise SystemExit('Nessun lettore trovato. Installa VLC o apri la playlist dal sito.')
    subprocess.Popen([player, url], start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
