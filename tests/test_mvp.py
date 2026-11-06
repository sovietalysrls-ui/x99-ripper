import json
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import ripper
import web
import migrate_desktop
import film_library


class QueueTests(unittest.TestCase):
    def test_migrates_owned_source_without_deleting_media(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'Film'
            folder.mkdir()
            source = folder / 'source.mkv'
            partial = folder / 'Film_H265.parziale.mkv'
            source.write_bytes(b'source')
            partial.write_bytes(b'partial')
            desktop_path = root / 'jobs.json'
            web_path = root / 'web-state.json'
            desktop_path.write_text(json.dumps([{'name': 'Film', 'status': 'in coda',
                                                 'folder': str(folder), 'source': str(source)}]))
            web_path.write_text(json.dumps({'queue': []}))
            with mock.patch.object(ripper, 'probe'):
                moved = migrate_desktop.migrate(desktop_path, web_path, root)
            self.assertEqual(moved, ['Film'])
            self.assertTrue(source.exists())
            self.assertFalse(partial.exists())
            self.assertEqual(len(list(folder.glob('*.interrotto-*.mkv'))), 1)
            self.assertEqual(json.loads(desktop_path.read_text())[0]['status'], 'trasferito')
            self.assertEqual(json.loads(web_path.read_text())['queue'][0]['status'], 'pronto')

    def test_completed_folder_lists_only_final_media_and_serves_it(self):
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(web, 'COMPLETED_DIR', Path(temp)), \
                mock.patch.object(web, 'COMPLETED_CUSTOM', False):
            folder = Path(temp) / 'Film'
            folder.mkdir()
            final = folder / 'Film & altro_H265.mkv'
            final.write_bytes(b'final-video')
            (folder / 'B1_t00.mkv').write_bytes(b'original')
            server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}/ripper/films'
                page = urllib.request.urlopen(base, timeout=2).read().decode()
                self.assertIn('Film completati', page)
                manifest = json.load(urllib.request.urlopen('http://127.0.0.1:' +
                    str(server.server_port) + '/ripper/manifest.webmanifest', timeout=2))
                self.assertEqual(manifest['start_url'], '/ripper/films')
                icon = urllib.request.urlopen('http://127.0.0.1:' +
                    str(server.server_port) + '/ripper/icon-192.png', timeout=2).read()
                self.assertTrue(icon.startswith(b'\x89PNG'))
                listing = json.load(urllib.request.urlopen(base + '/data', timeout=2))
                self.assertEqual([item['filename'] for item in listing], [final.name])
                link = base + '/file?path=' + urllib.parse.quote('Film/Film & altro_H265.mkv')
                self.assertEqual(urllib.request.urlopen(link, timeout=2).read(), b'final-video')
                request = urllib.request.Request(link, headers={'Range': 'bytes=2-6'})
                with urllib.request.urlopen(request, timeout=2) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), b'nal-v')
                self.assertIsNone(web.completed_file('../segreto.mkv'))
                self.assertIsNone(web.completed_file('Film/B1_t00.mkv'))
                with mock.patch.object(web, 'COMPLETED_CUSTOM', True):
                    self.assertEqual(web.completed_file('Film/B1_t00.mkv'), folder / 'B1_t00.mkv')
            finally:
                server.shutdown()
                server.server_close()

    def test_reviewed_metadata_renames_final_and_writes_jellyfin_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / 'THE_FOG_H265.mkv'
            source.write_bytes(b'keep video')
            renamed = film_library.save(source, {'title': 'Fog', 'year': 1980,
                'director': ['John Carpenter'], 'cast': ['Adrienne Barbeau'],
                'genres': ['horror'], 'wikidata_id': 'Q500403'})
            self.assertEqual(renamed.name, 'Fog (1980)_H265.mkv')
            self.assertEqual(renamed.read_bytes(), b'keep video')
            self.assertFalse(source.exists())
            self.assertEqual(film_library.read(renamed)['director'], ['John Carpenter'])
            nfo = (folder / 'movie.nfo').read_text()
            self.assertIn('<director>John Carpenter</director>', nfo)
            self.assertIn('<name>Adrienne Barbeau</name>', nfo)

    def test_existing_unowned_nfo_prevents_automatic_replacement(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'film_H265.mkv'
            source.write_bytes(b'video')
            (Path(temp) / 'movie.nfo').write_text('<movie><title>Manuale</title></movie>')
            with self.assertRaises(FileExistsError):
                film_library.save(source, {'title': 'Film', 'year': 2000})
            self.assertTrue(source.exists())

    def test_italian_title_with_colon_keeps_catalog_title(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'Escape NY_H265.mkv'
            source.write_bytes(b'video')
            renamed = film_library.save(source, {'title': '1997: Fuga da New York', 'year': 1981})
            self.assertEqual(renamed.name, '1997 - Fuga da New York (1981)_H265.mkv')
            self.assertEqual(film_library.read(renamed)['title'], '1997: Fuga da New York')

    def test_signed_stream_only_serves_final_inside_folder(self):
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(web, 'COMPLETED_DIR', Path(temp)), \
                mock.patch.object(web, 'TAILSCALE_HOST', '100.107.201.72'), \
                mock.patch.object(web, 'STREAM_SECRET_FILE', Path(temp) / 'secret'), \
                mock.patch.object(web, 'STREAM_SECRET', None):
            final = Path(temp) / 'Film_H265.mkv'
            final.write_bytes(b'video')
            url = web.signed_stream_url('Film_H265.mkv', now=1000000000)
            query = urllib.parse.urlsplit(url).query
            with mock.patch.object(web.time, 'time', return_value=1000000001):
                self.assertEqual(web.authorized_stream(query), final)
                self.assertIsNone(web.authorized_stream(query.replace('Film_H265', 'Other_H265')))
            with mock.patch.object(web.time, 'time', return_value=1000086401):
                self.assertIsNone(web.authorized_stream(query))

    def manager(self):
        manager = web.Manager.__new__(web.Manager)
        manager.lock = threading.RLock()
        manager.state = {'queue': [{'name': 'Film', 'status': 'pronto'}], 'max_discs': 5}
        manager.busy = False
        manager.encoding = False
        manager.desktop_active = lambda: False
        return manager

    def test_acquisition_can_start_during_encoding(self):
        manager = self.manager()
        manager.encoding = True
        with mock.patch.object(web.threading, 'Thread') as thread:
            self.assertTrue(manager.start_scan())
        thread.return_value.start.assert_called_once()

    def test_encoding_can_start_during_acquisition(self):
        manager = self.manager()
        manager.busy = True
        with mock.patch.object(web.threading, 'Thread') as thread:
            self.assertTrue(manager.start_encoding())
        thread.return_value.start.assert_called_once()

    def test_gpu_selection_waits_for_idle_encoder(self):
        manager = self.manager()
        manager.set = lambda **kwargs: manager.state.update(kwargs)
        self.assertTrue(manager.set_encoder('gpu'))
        self.assertEqual(manager.state['encoder'], 'gpu')
        manager.encoding = True
        self.assertFalse(manager.set_encoder('cpu'))
        self.assertEqual(manager.state['encoder'], 'gpu')

    def test_encoder_waits_for_disc_being_extracted(self):
        manager = self.manager()
        manager.state['queue'] = []
        manager.busy = True
        def finish_extraction():
            with manager.lock:
                manager.state['queue'].append({'name': 'Nuovo DVD', 'status': 'pronto'})
                manager.busy = False
        timer = threading.Timer(0.05, finish_extraction)
        timer.start()
        try:
            self.assertEqual(manager.next_ready_or_wait()['name'], 'Nuovo DVD')
        finally:
            timer.join()

    def test_recovery_keeps_interrupted_file(self):
        manager = self.manager()
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            source = folder / 'source.mkv'
            source.write_bytes(b'sample')
            partial = folder / 'Film_H265.parziale.mkv'
            partial.write_bytes(b'partial')
            manager.state['queue'] = [{'name': 'Film', 'folder': str(folder),
                                       'source': str(source), 'status': 'errore'}]
            manager.set = lambda *args, **kwargs: None
            self.assertTrue(manager.retry_errors())
            self.assertEqual(manager.state['queue'][0]['status'], 'pronto')
            self.assertTrue(source.exists())
            self.assertFalse(partial.exists())
            self.assertEqual(len(list(folder.glob('*.interrotto-*.mkv'))), 1)

    def test_catalog_only_after_review_and_does_not_overwrite(self):
        manager = self.manager()
        manager.set = lambda *args, **kwargs: None
        self.assertFalse(manager.set_catalog(0, '', ''))
        self.assertTrue(manager.set_catalog(0, 'Il film & altro', '1998'))
        with tempfile.TemporaryDirectory() as temp:
            self.assertTrue(web.write_jellyfin_nfo(temp, 'Il film & altro', 1998))
            nfo = Path(temp) / 'movie.nfo'
            self.assertIn(b'Il film &amp; altro', nfo.read_bytes())
            self.assertFalse(web.write_jellyfin_nfo(temp, 'Sbagliato', 2000))
            self.assertIn(b'1998', nfo.read_bytes())


class MediaTests(unittest.TestCase):
    def test_integrity_checks_two_video_samples(self):
        with mock.patch.object(ripper.subprocess, 'run') as run:
            run.return_value.returncode = 0
            ripper.verify_decode_samples('out.mkv', 3600)
        self.assertEqual(run.call_count, 2)
        self.assertIn('5', run.call_args_list[0].args[0])
        self.assertIn('3580', run.call_args_list[1].args[0])

    def test_italian_first_and_soft_subtitles(self):
        meta = {'streams': [
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'tags': {'language': 'eng'}},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'tags': {'language': 'ita'}},
            {'codec_type': 'subtitle', 'tags': {'language': 'eng'}},
            {'codec_type': 'subtitle', 'tags': {'language': 'ita'}},
        ]}
        plan = ripper.audio_plan(meta, ripper.audio_choices(meta))
        self.assertEqual([p['language'] for p in plan], ['ita', 'eng'])
        cmd = ripper.hb_command('in.mkv', 'out.mkv', '0:0:0:0', plan, ripper.sub_choices(meta))
        self.assertEqual(cmd[cmd.index('-s') + 1], '1,2')
        self.assertIn('--subtitle-burned=none', cmd)
        self.assertIn('--subtitle-default=none', cmd)
        gpu = ripper.hb_command('in.mkv', 'out.mkv', '0:0:0:0', plan,
                                ripper.sub_choices(meta), 'nvenc_h265_10bit')
        self.assertEqual(gpu[gpu.index('-e') + 1], 'nvenc_h265_10bit')
        self.assertEqual(gpu[gpu.index('-q') + 1], '28')

    def test_italian_spanish_and_original_audio_without_dub_duplicates(self):
        meta = {'streams': [
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'tags': {'language': 'eng'}},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2, 'tags': {'language': 'fre'}},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2, 'tags': {'language': 'ger'}},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2, 'tags': {'language': 'ita'}},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2, 'tags': {'language': 'spa'}},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2, 'tags': {'language': 'eng'}},
            {'codec_type': 'subtitle', 'tags': {'language': 'ita'}},
            {'codec_type': 'subtitle', 'tags': {'language': 'eng'}},
            {'codec_type': 'subtitle', 'tags': {'language': 'spa'}},
        ]}
        chosen = ripper.audio_choices(meta, 'eng')
        self.assertEqual(chosen, [4, 5, 1])
        self.assertEqual([p['language'] for p in ripper.audio_plan(meta, chosen)], ['ita', 'spa', 'eng'])
        self.assertEqual(ripper.sub_choices(meta), [1, 2, 3])
        self.assertEqual(ripper.audio_choices(meta, 'fra'), [4, 5, 2])

    def test_original_audio_first_if_no_italian(self):
        meta = {'streams': [
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 2, 'tags': {'language': 'spa'}},
            {'codec_type': 'audio', 'codec_name': 'ac3', 'channels': 6, 'tags': {'language': 'eng'}},
        ]}
        self.assertEqual(ripper.audio_choices(meta, 'eng'), [2, 1])


if __name__ == '__main__':
    unittest.main()
