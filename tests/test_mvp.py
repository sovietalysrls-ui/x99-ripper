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
                self.assertIn('Film &amp; altro_H265.mkv', page)
                self.assertNotIn('B1_t00.mkv', page)
                link = base + '/file?path=' + urllib.parse.quote('Film/Film & altro_H265.mkv')
                self.assertEqual(urllib.request.urlopen(link, timeout=2).read(), b'final-video')
                self.assertIsNone(web.completed_file('../segreto.mkv'))
                self.assertIsNone(web.completed_file('Film/B1_t00.mkv'))
                with mock.patch.object(web, 'COMPLETED_CUSTOM', True):
                    self.assertEqual(web.completed_file('Film/B1_t00.mkv'), folder / 'B1_t00.mkv')
            finally:
                server.shutdown()
                server.server_close()

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


if __name__ == '__main__':
    unittest.main()
