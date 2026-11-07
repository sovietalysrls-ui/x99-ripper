import json
import io
import hashlib
import tempfile
import threading
import time
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
    def test_x99_import_checks_hash_and_registers_without_overwrite(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'media';root.mkdir()
            state = Path(temp) / 'state'
            staged_name = 'a' * 32 + '/film.mkv.part'
            staged = state / 'incoming' / staged_name
            staged.parent.mkdir(parents=True)
            staged.write_bytes(b'video' * 25_000)
            digest = hashlib.sha256(staged.read_bytes()).hexdigest()
            manager = object.__new__(web.Manager)
            manager.lock = threading.RLock()
            manager.state = {'queue': []}
            manager.set = lambda **values: manager.state.update(values)
            media = {'format': {'duration': '5400'}}
            def tracks(_, kind): return [{'codec_name': 'hevc'}] if kind == 'video' else [{}]
            with mock.patch.object(web.core, 'ROOT', root), mock.patch.object(web.core, 'STATE', state), \
                 mock.patch.object(web.core, 'probe', return_value=media), \
                 mock.patch.object(web.core, 'tracks', side_effect=tracks), \
                 mock.patch.object(web.core, 'verify_decode_samples'), \
                 mock.patch.object(web, 'catalog_automatically'):
                with self.assertRaises(ValueError):
                    manager.import_external_final(staged_name, '0' * 64, 'Film X99')
                self.assertTrue(staged.exists())
                output = manager.import_external_final(staged_name, digest, 'Film X99')
                self.assertTrue(output.is_file())
                self.assertFalse(staged.exists())
                self.assertEqual(manager.state['queue'][0]['status'], 'completato')
                second_name = 'b' * 32 + '/film.mkv.part'
                second = state / 'incoming' / second_name
                second.parent.mkdir(parents=True)
                second.write_bytes(b'video' * 25_000)
                with self.assertRaises(FileExistsError):
                    manager.import_external_final(second_name, digest, 'Film X99')
                self.assertTrue(second.exists())
                self.assertEqual(output.read_bytes(), b'video' * 25_000)

    def test_disc_read_retry_preserves_incomplete_file(self):
        self.assertTrue(web.has_disc_read_error('MEDIUM ERROR:L-EC UNCORRECTABLE ERROR'))
        self.assertTrue(ripper.has_disc_read_error('I/O error'))
        self.assertFalse(web.has_disc_read_error('registration key expired'))
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            incomplete = folder / 'B1_t00.mkv'
            incomplete.write_bytes(b'incomplete')
            archived = ripper.archive_failed_acquisition(folder, 1)
            self.assertEqual(len(archived), 1)
            self.assertFalse(incomplete.exists())
            self.assertEqual(archived[0].read_bytes(), b'incomplete')
            self.assertEqual(web.archive_failed_acquisition(folder, 2), [])

    def test_verified_source_requires_its_own_final_and_explicit_confirmation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / 'Film'
            folder.mkdir()
            source = folder / 'B1_t00.mkv'
            output = folder / 'Film_H265.mkv'
            source.write_bytes(b'original')
            output.write_bytes(b'final')
            manager = object.__new__(web.Manager)
            manager.lock = threading.RLock()
            manager.busy = manager.encoding = False
            manager.desktop_active = lambda: False
            manager.state = {'queue': [{'name': 'Film', 'title_id': 0, 'status': 'completato',
                                        'verified_at': time.time(),
                                        'folder': str(folder), 'source': str(source), 'output': str(output)}]}
            manager.set = lambda **kwargs: None
            with mock.patch.object(web.core, 'ROOT', root):
                self.assertFalse(manager.remove_verified_source(0, 'Altro'))
                self.assertTrue(source.exists())
                output.unlink()
                self.assertFalse(manager.remove_verified_source(0, 'Film'))
                output.write_bytes(b'final')
                manager.cleanup_due_sources()
                self.assertTrue(source.exists())
                manager.state['queue'][0]['verified_at'] -= web.SOURCE_RETENTION_SECONDS + 1
                manager.cleanup_due_sources()
                self.assertFalse(source.exists())
                source.write_bytes(b'original')
                self.assertTrue(manager.remove_verified_source(0, 'Film'))
            self.assertFalse(source.exists())
            self.assertTrue(output.exists())
            self.assertTrue(manager.state['queue'][0]['source_removed'])

    def test_storage_chart_counts_only_discflow_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'media'
            root.mkdir()
            preview = Path(temp) / 'previews'
            preview.mkdir()
            (root / 'B1_t00.mkv').write_bytes(b'a' * 100)
            (root / 'Film_H265.mkv').write_bytes(b'b' * 80)
            (root / 'Film_H265.parziale.mkv').write_bytes(b'c' * 40)
            (preview / 'browser.mp4').write_bytes(b'd' * 20)
            with mock.patch.object(web.shutil, 'disk_usage', return_value=mock.Mock(free=300)):
                values = web.measure_storage(root, preview)
            self.assertEqual(values, {'sources': 100, 'work': 60, 'final': 80, 'iso': 0, 'free': 300})

    def test_video_import_accepts_container_by_probe_and_keeps_managed_copy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'media'; root.mkdir()
            staged = Path(temp) / 'upload.part'; staged.write_bytes(b'video data')
            manager = object.__new__(web.Manager)
            manager.lock = threading.RLock()
            manager.state = {'queue': []}
            manager.busy = False
            manager.desktop_active = lambda: False
            manager.set = lambda *args, **values: manager.state.update(values)
            metadata = {'format': {'duration': '72.5'},
                        'streams': [{'codec_type': 'video', 'width': 536, 'height': 416}]}
            with mock.patch.object(web.core, 'ROOT', root), mock.patch.object(web.core, 'probe', return_value=metadata):
                job = manager.import_video(staged, 'Registrazione vecchia.avi', 'none', 'auto')
            self.assertFalse(staged.exists())
            self.assertTrue(Path(job['source']).is_file())
            self.assertEqual(job['source_type'], 'file')
            self.assertEqual(job['crop_mode'], 'none')
            self.assertEqual(job['deinterlace'], 'auto')
            self.assertEqual(job['status'], 'pronto')
            self.assertEqual(len(manager.state['queue']), 1)

    def test_video_import_rejects_nonvideo_without_moving_upload(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'media'; root.mkdir()
            staged = Path(temp) / 'upload.part'; staged.write_bytes(b'not a video')
            manager = object.__new__(web.Manager)
            manager.lock = threading.RLock()
            manager.state = {'queue': []}
            manager.busy = False
            manager.desktop_active = lambda: False
            metadata = {'format': {'duration': '10'}, 'streams': [{'codec_type': 'audio'}]}
            with mock.patch.object(web.core, 'ROOT', root), mock.patch.object(web.core, 'probe', return_value=metadata):
                with self.assertRaises(ValueError): manager.import_video(staged, 'test.mp4')
            self.assertTrue(staged.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_browser_upload_streams_video_into_queue(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); content = b'example video' * 1000
            manager = object.__new__(web.Manager)
            manager.lock = threading.RLock(); manager.state = {'queue': []}
            manager.busy = False; manager.desktop_active = lambda: False
            manager.set = lambda *args, **values: manager.state.update(values)
            metadata = {'format': {'duration': '30'},
                        'streams': [{'codec_type': 'video', 'width': 720, 'height': 576}]}
            with mock.patch.object(web, 'manager', manager), \
                 mock.patch.object(web.core, 'ROOT', root), \
                 mock.patch.object(web.core, 'probe', return_value=metadata):
                server = ThreadingHTTPServer(('127.0.0.1', 0), web.Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    url = f'http://127.0.0.1:{server.server_port}/ripper/video/import?name=Film.avi&crop=none&deinterlace=on'
                    request = urllib.request.Request(url, data=content,
                        headers={'X-Ripper-Token': web.TOKEN,
                                 'Content-Type': 'application/octet-stream'})
                    with urllib.request.urlopen(request, timeout=5) as response:
                        self.assertTrue(json.load(response)['started'])
                finally:
                    server.shutdown(); server.server_close(); thread.join()
            job = manager.state['queue'][0]
            self.assertEqual(Path(job['source']).read_bytes(), content)
            self.assertEqual(job['deinterlace'], 'on')

    def test_imprecise_title_finds_italian_film_without_suggesting_people(self):
        def wikipedia(req, timeout):
            url = req.full_url
            if 'list=search' in url:
                self.assertIn('case~1+ridono~1', url)
                data = {'query': {'search': [
                    {'pageid': 10, 'title': 'La casa dalle finestre che ridono'},
                    {'pageid': 11, 'title': 'Vanna Busoni'}]}}
            else:
                data = {'query': {'pages': {
                    '10': {'pageprops': {'wikibase_item': 'Q2602481'}},
                    '11': {'pageprops': {'wikibase_item': 'Q4008610'}}}}}
            return io.BytesIO(json.dumps(data).encode())
        def wikidata(**params):
            if params['action'] == 'wbsearchentities':
                return {'search': []}
            return {'entities': {
                'Q2602481': {'labels': {'it': {'value': 'La casa dalle finestre che ridono'}},
                             'descriptions': {'it': {'value': 'film del 1976 diretto da Pupi Avati'}}},
                'Q4008610': {'labels': {'it': {'value': 'Vanna Busoni'}},
                             'descriptions': {'it': {'value': 'attrice italiana'}}}}}
        with mock.patch.object(film_library, 'request', side_effect=wikidata), \
                mock.patch.object(film_library.urllib.request, 'urlopen', side_effect=wikipedia):
            found = film_library.search('case ridono')
        self.assertEqual([item['id'] for item in found], ['Q2602481'])

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

    def test_pending_film_can_save_reviewed_metadata(self):
        manager = self.manager()
        manager.set = lambda *args, **kwargs: manager.state.update(kwargs)
        metadata = {'title': 'La casa dalle finestre che ridono', 'year': 1976,
                    'wikidata_id': 'Q2602481', 'original_language': 'ita',
                    'director': ['Pupi Avati']}
        with mock.patch.object(film_library, 'details', return_value=metadata), \
                mock.patch.object(manager, 'start_encoding', return_value=True) as start:
            self.assertTrue(manager.set_catalog_choice(0, 'Q2602481'))
        start.assert_called_once_with(auto_only=True)
        job = manager.state['queue'][0]
        self.assertEqual(job['catalog_title'], metadata['title'])
        self.assertEqual(job['catalog_metadata']['director'], ['Pupi Avati'])
        self.assertEqual(job['original_language'], 'ita')
        self.assertTrue(job['auto_encode'])

    def test_automatic_encoding_selects_only_confirmed_film(self):
        manager = self.manager()
        manager.state['queue'] = [
            {'name': 'DVDVolume', 'status': 'pronto', 'auto_encode': True},
            {'name': 'DVDVolume_duplicato', 'status': 'pronto'}]
        self.assertEqual(manager.next_ready_or_wait(auto_only=True)['name'], 'DVDVolume')
        manager.state['queue'][0]['status'] = 'compressione'
        self.assertIsNone(manager.next_ready_or_wait(auto_only=True))
        with mock.patch.object(web.threading, 'Thread') as thread:
            self.assertFalse(manager.start_encoding(auto_only=True))
            self.assertTrue(manager.start_encoding())
        thread.return_value.start.assert_called_once()

    def test_delete_pending_job_requires_matching_name_and_preserves_other_folders(self):
        manager = self.manager()
        manager.set = lambda *args, **kwargs: manager.state.update(kwargs)
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(ripper, 'ROOT', Path(temp)):
            folder = Path(temp) / 'DVDVolume'
            folder.mkdir()
            source = folder / 'B1_t00.mkv'
            source.write_bytes(b'dvd')
            other = Path(temp) / 'Other'
            other.mkdir()
            (other / 'keep.mkv').write_bytes(b'keep')
            manager.state['queue'] = [{'name': folder.name, 'folder': str(folder),
                                       'source': str(source), 'status': 'pronto'}]
            self.assertFalse(manager.delete_ready(0, 'Other'))
            self.assertTrue(source.exists())
            self.assertTrue(manager.delete_ready(0, folder.name))
            self.assertFalse(folder.exists())
            self.assertTrue((other / 'keep.mkv').exists())
            self.assertEqual(manager.state['queue'], [])

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
    def test_unified_video_output_and_selective_deinterlacing(self):
        cmd = ripper.hb_command('source.avi', 'finale.mkv', None, [], [],
                                'nvenc_h265_10bit', 'auto')
        self.assertEqual(cmd[cmd.index('--crop-mode') + 1], 'auto')
        self.assertEqual(cmd[cmd.index('-a') + 1], 'none')
        self.assertIn('--comb-detect=default', cmd)
        self.assertIn('--bwdif=default', cmd)
        self.assertEqual(cmd[cmd.index('-e') + 1], 'nvenc_h265_10bit')
        no_filters = ripper.hb_command('source.mp4', 'finale.mkv', '0:0:0:0', [], [],
                                       'nvenc_h265_10bit', 'off')
        self.assertNotIn('--bwdif=default', no_filters)
        self.assertEqual(no_filters[no_filters.index('--crop') + 1], '0:0:0:0')

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
