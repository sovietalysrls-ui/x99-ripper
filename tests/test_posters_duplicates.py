import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import duplicates
import film_library
import web


class PosterAndDuplicateTests(unittest.TestCase):
    def test_only_configured_lan_client_can_open_dashboard(self):
        handler = object.__new__(web.Handler)
        handler.send_error = lambda code: setattr(handler, 'denied', code)
        with mock.patch.object(web, 'ALLOWED_LAN_CLIENTS', {'192.168.4.41'}):
            handler.client_address = ('192.168.4.41', 50000)
            self.assertTrue(handler.allowed())
            handler.client_address = ('192.168.4.42', 50000)
            self.assertFalse(handler.allowed())
            self.assertEqual(handler.denied, 403)

    def test_manual_poster_decodes_replaces_and_precedes_online_cover(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            film = folder / 'Film_H265.mkv'
            film.write_bytes(b'film')
            (folder / 'discflow-film.json').write_text(json.dumps({
                'title': 'Film', 'cover_url': 'https://example.org/remote.jpg'}))
            with mock.patch.object(web, 'COMPLETED_DIR', folder), mock.patch.object(web, 'COMPLETED_CUSTOM', False):
                self.assertEqual(web.film_data()[0]['cover_url'], 'https://example.org/remote.jpg')
                for color, extension, media_type in [('red', 'png', 'image/png'),
                                                     ('blue', 'webp', 'image/webp')]:
                    source = folder / ('sample.' + extension)
                    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                    'color=c=' + color + ':s=80x120', '-frames:v', '1', str(source)],
                                   check=True)
                    film_library.save_poster(film, source.read_bytes(), media_type)
                    self.assertTrue(film_library.poster(film).read_bytes().startswith(b'\xff\xd8\xff'))
                    self.assertTrue(web.film_data()[0]['cover_url'].startswith('/ripper/films/poster?'))
                previous = film_library.poster(film).read_bytes()
                with self.assertRaises(ValueError):
                    film_library.save_poster(film, b'not an image', 'image/png')
                self.assertEqual(film_library.poster(film).read_bytes(), previous)

    def test_duplicate_matching_uses_identity_title_year_and_source_hash(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            existing = folder / 'Film (1980)_H265.mkv'
            existing.write_bytes(b'encoded')
            (folder / 'Originale.mkv').write_bytes(b'source' * 100)
            (folder / 'discflow-film.json').write_text(json.dumps({
                'title': 'Film', 'year': 1980, 'wikidata_id': 'Q123',
                'duration_seconds': 5400, 'source_filename': 'Originale.mkv'}))
            self.assertEqual(duplicates.candidates('Film', 5400, [existing], year=1980)[0]['reason'], 'titolo simile')
            self.assertEqual(duplicates.candidates('DVDVolume', 5400, [existing]), [])
            self.assertEqual(duplicates.candidates('Film', 5400, [existing], year=1999), [])
            self.assertEqual(duplicates.candidates('Other', 5400, [existing], wikidata_id='Q123')[0]['reason'], 'stesso identificativo')
            self.assertEqual(duplicates.candidates('unknown', 5400, [existing], source=folder/'Originale.mkv')[0]['reason'], 'stesso contenuto')
            (folder/'discflow-film.json').write_text(json.dumps({'title': 'Film'}))
            self.assertEqual(duplicates.candidates('unknown', 5400, [existing], source=folder/'Originale.mkv',
                             source_lookup={str(existing): str(folder/'Originale.mkv')})[0]['reason'], 'stesso contenuto')

    def test_replacement_keeps_old_until_final_confirmation_then_archives(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'media'; root.mkdir()
            old_folder = root / 'Old'; old_folder.mkdir()
            old = old_folder / 'Film_H265.mkv'; old.write_bytes(b'old')
            (old_folder/'poster.jpg').write_bytes(b'poster')
            new_folder = root / 'New'; new_folder.mkdir()
            new = new_folder / 'Film_H265.mkv'; new.write_bytes(b'new')
            (new_folder/'discflow-film.json').write_text(json.dumps({'title': 'Film'}))
            manager = object.__new__(web.Manager)
            manager.lock = threading.RLock()
            manager.state = {'queue': [{'name': 'New', 'status': 'completato', 'output': str(new),
                                        'duplicate_action': 'replace', 'duplicate_existing': str(old)}], 'log': []}
            manager.set = lambda *args, **values: manager.state.update(values)
            with mock.patch.object(web.core, 'ROOT', root), mock.patch.object(web.core, 'STATE', Path(temp)/'state'), mock.patch.object(web, 'COMPLETED_DIR', root):
                self.assertFalse(manager.finalize_replacement(0, 'wrong', str(old)))
                self.assertTrue(old.exists())
                self.assertTrue(manager.finalize_replacement(0, 'New', str(old)))
                self.assertTrue(new.exists())
                self.assertFalse(old.exists())
                archive = Path(manager.state['queue'][0]['replacement_archived'])
                self.assertEqual((archive/'Film_H265.mkv').read_bytes(), b'old')
                self.assertEqual((archive/'poster.jpg').read_bytes(), b'poster')
                self.assertFalse(manager.finalize_replacement(0, 'New', str(old)))


if __name__ == '__main__':
    unittest.main()
