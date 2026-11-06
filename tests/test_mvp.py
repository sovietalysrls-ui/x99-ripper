import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import ripper
import web


class QueueTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
