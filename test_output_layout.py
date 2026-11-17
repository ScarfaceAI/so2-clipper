"""交付目录只含日期及视频；状态迁移和跨卷发布不破坏复用。"""
import contextlib
import copy
import errno
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import config
import core
import kill_clipper
import media
import organize_output
import store
import workspace


class OutputLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='so2_delivery_')
        self.root = Path(self.temp.name)
        self.output = self.root / 'output'
        self.input = self.root / 'input'
        self.input.mkdir()
        self.source = self.input / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '4',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(self.source)],
                       capture_output=True, check=True)
        self.environment = patch.dict(os.environ, {'SO2_CLIPS_STATE_HOME': str(self.root / 'runtime')})
        self.environment.start()
        self.args = ['--video', str(self.source), '--output-dir', str(self.output), '--profile', 'pc',
                     '--identity-mode', 'keywords', '--keywords', 'Player', '--scan-segments', '1',
                     '--pre', '.2', '--post', '.2', '--extract-workers', '1']
        self.cfg = config.load_config(self.args)

    def tearDown(self):
        for handler in logging.getLogger().handlers[:]:
            logging.getLogger().removeHandler(handler)
            handler.close()
        self.environment.stop()
        self.temp.cleanup()

    def test_run_and_rerun_deliver_only_videos_and_reuse_scan_and_encoding(self):
        info = media.probe(self.source)
        scan = core.ScanResult(str(self.source), 'complete', hits=[1., 2.],
                               identity=store.identity(self.source), coverage=[0., info['duration']],
                               phase={'checked': True, 'intervals': []})
        with contextlib.redirect_stdout(io.StringIO()), patch.object(core, 'scan_video', return_value=scan):
            self.assertEqual(kill_clipper.main(self.args + ['--merge', '--run-id', 'one']), 0)
        receipt = workspace.run_dir(self.cfg, 'one') / 'receipt.json'
        data = json.loads(receipt.read_text(encoding='utf-8'))
        videos = list(self.output.rglob('*.mp4'))
        self.assertEqual(len(videos), 2)
        self.assertTrue(all(p.parent.parent == self.output for p in videos))
        self.assertEqual({p for p in self.output.rglob('*') if p.is_file()}, set(videos))
        self.assertTrue(Path(data['report']).is_relative_to(Path(self.cfg.state_dir)))
        self.assertTrue(Path(self.cfg.result_file).is_file())
        self.assertTrue(Path(self.cfg.log_file).is_file())
        old = {p: (p.stat().st_mtime_ns, organize_output.file_hash(p)) for p in videos}
        with contextlib.redirect_stdout(io.StringIO()), patch.object(core, 'scan_video', side_effect=AssertionError('重复 OCR')):
            self.assertEqual(kill_clipper.main(self.args + ['--merge', '--run-id', 'two']), 0)
        data = json.loads((workspace.run_dir(self.cfg, 'two') / 'receipt.json').read_text(encoding='utf-8'))
        self.assertTrue(data['files'][0]['reused_scan'])
        self.assertEqual(data['merge']['status'], 'reused')
        self.assertEqual(old, {p: (p.stat().st_mtime_ns, organize_output.file_hash(p)) for p in videos})

    def test_failed_scan_still_has_internal_report_and_no_delivery_junk(self):
        failed = core.ScanResult(str(self.source), 'failed', error='真实扫描失败')
        with contextlib.redirect_stdout(io.StringIO()), patch.object(core, 'scan_video', return_value=failed):
            self.assertEqual(kill_clipper.main(self.args + ['--run-id', 'failed']), 1)
        data = json.loads((workspace.run_dir(self.cfg, 'failed') / 'receipt.json').read_text(encoding='utf-8'))
        self.assertIn('真实扫描失败', Path(data['report']).read_text(encoding='utf-8'))
        self.assertFalse(list(self.output.rglob('*')))

    def test_migration_preserves_video_and_scan_parameters_and_repairs_picture_links(self):
        date = self.output / '2026-10-18'
        review = date / 'review'
        review.mkdir(parents=True)
        final = date / 'accepted.mp4'
        shutil.copy2(self.source, final)
        rejected = review / 'rejected.mp4'
        shutil.copy2(self.source, rejected)
        picture = review / 'doubt.jpg'
        picture.write_bytes(b'picture')
        report = date / 'report_1.md'
        report.write_text(f'![疑点](<{picture.as_posix()}>)', encoding='utf-8')
        original = {'version': 3, 'videos': {'id': {'cfg_fingerprint': 'stable', 'hits': [1., 2.],
                    'clips': [{'key': 'stable', 'path': str(final)}],
                    'review': [{'screenshot': str(picture)}]}}, 'legacy_videos': {'old': {'source_available': False}}}
        store.atomic_json(self.output / 'scan_results.json', original)
        snapshot = (final.stat().st_mtime_ns, organize_output.file_hash(final))
        preview = organize_output.organize(self.cfg)
        self.assertFalse(preview['applied'])
        self.assertTrue(picture.is_file())
        result = organize_output.organize(self.cfg, apply=True)
        data = store.load_results(self.cfg)
        expected = copy.deepcopy(original)
        expected['videos']['id']['review'][0]['screenshot'] = str(Path(self.cfg.state_dir) / 'legacy_output/2026-10-18/review/doubt.jpg')
        self.assertEqual(data, expected)
        self.assertEqual(snapshot, (final.stat().st_mtime_ns, organize_output.file_hash(final)))
        self.assertEqual([p for p in self.output.rglob('*') if p.is_file()], [final])
        new_picture = Path(data['videos']['id']['review'][0]['screenshot'])
        self.assertTrue(new_picture.is_file())
        new_report = Path(self.cfg.state_dir) / 'legacy_output/2026-10-18/report_1.md'
        self.assertIn(new_picture.as_posix(), new_report.read_text(encoding='utf-8'))
        self.assertTrue(new_picture.with_name('rejected.mp4').is_file())
        receipt = json.loads(Path(result['receipt']).read_text(encoding='utf-8'))
        backup = next(Path(m['original_backup']) for m in receipt['moves'] if m['from'].endswith('scan_results.json'))
        self.assertEqual(json.loads(backup.read_text(encoding='utf-8')), original)
        self.assertEqual(organize_output.organize(self.cfg, apply=True)['moves'], [])

    def test_migration_conflict_stops_before_moving_any_file(self):
        store.atomic_json(self.output / 'scan_results.json', {'version': 3, 'videos': {}})
        store.atomic_json(Path(self.cfg.result_file), {'version': 3, 'videos': {'newer': {}}})
        with self.assertRaisesRegex(ValueError, '已存在'):
            organize_output.organize(self.cfg, apply=True)
        self.assertTrue((self.output / 'scan_results.json').is_file())
        self.assertIn('newer', store.load_results(self.cfg)['videos'])

    def test_state_location_isolated_and_excluded_from_scan_without_invalidating_ocr(self):
        other = config.load_config(self.args + ['--state-dir', str(self.input / 'internal')])
        self.assertEqual(store.cfg_fingerprint(self.cfg), store.cfg_fingerprint(other))
        internal = Path(other.state_dir) / 'old.mp4'
        internal.parent.mkdir()
        shutil.copy2(self.source, internal)
        other.video_dir = str(self.input)
        self.assertEqual(core.discover_videos(other), [str(self.source)])
        self.assertNotEqual(workspace.state_root(self.output), workspace.state_root(self.root / 'another'))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            config.load_config(self.args + ['--state-dir', str(self.output / 'state')])

    def test_cross_volume_publication_uses_staging_outside_delivery(self):
        part = self.root / 'part.mp4'
        part.write_bytes(b'validated video')
        target = self.output / '2026-10-19/final.mp4'
        replace = os.replace
        def cross_volume(source, destination):
            if Path(source) == part:
                raise OSError(errno.EXDEV, 'different volume')
            self.assertFalse(Path(source).is_relative_to(self.output))
            return replace(source, destination)
        with patch.object(workspace.os, 'replace', side_effect=cross_volume):
            workspace.publish(part, target, self.output)
        self.assertEqual(target.read_bytes(), b'validated video')
        self.assertFalse(part.exists())
        self.assertFalse(list(self.root.glob('.so2-transfer-*')))

    def test_failed_cross_volume_copy_does_not_leave_a_partial_final_video(self):
        part = self.root / 'part.mp4'
        part.write_bytes(b'validated video')
        target = self.output / '2026-10-19/final.mp4'
        with (patch.object(workspace.os, 'replace', side_effect=OSError(errno.EXDEV, 'different volume')),
              patch.object(workspace.shutil, 'copy2', side_effect=OSError('disk full'))):
            with self.assertRaisesRegex(OSError, 'disk full'):
                workspace.publish(part, target, self.output)
        self.assertFalse(target.exists())
        self.assertTrue(part.is_file())
        self.assertFalse(list(self.root.glob('.so2-transfer-*')))


if __name__ == '__main__':
    unittest.main()
