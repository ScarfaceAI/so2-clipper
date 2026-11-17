"""分享包仅保留当前有效版本；复用、更新和失败不误删其他文件。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools import build_skill_zip as builder


class PackageBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='so2_package_test_')
        self.root = Path(self.temp.name)
        (self.root / 'skills/so2-clipper').mkdir(parents=True)
        (self.root / 'skills/so2-clipper/SKILL.md').write_text('sample skill', encoding='utf-8')
        (self.root / 'scripts/assets').mkdir(parents=True)
        self.engine = self.root / 'scripts/engine.py'
        self.engine.write_text('value = 1\n', encoding='utf-8')
        self.scope = patch.object(builder, 'ROOT', self.root)
        self.scope.start()

    def tearDown(self):
        self.scope.stop()
        self.temp.cleanup()

    def test_build_keeps_only_current_package_and_does_not_touch_unrelated_zip(self):
        dist = self.root / 'dist'
        dist.mkdir()
        (dist / 'so2-clipper-old.zip').write_bytes(b'obsolete')
        (dist / 'so2-clipper-old.zip.sha256').write_text('obsolete', encoding='utf-8')
        (dist / 'so2-kill-clips-old.zip').write_bytes(b'obsolete')
        (dist / 'so2-kill-clips-old.zip.sha256').write_text('obsolete', encoding='utf-8')
        unrelated = dist / 'another-project.zip'
        unrelated.write_bytes(b'keep')
        result = builder.build()
        self.assertEqual(len(result['deleted_old_files']), 4)
        self.assertEqual(unrelated.read_bytes(), b'keep')
        self.assertEqual(list(dist.glob('so2-clipper-*.zip')), [Path(result['zip'])])
        pointer = json.loads((dist / 'current_release.json').read_text(encoding='utf-8'))
        self.assertEqual(pointer['sha256'], result['sha256'])

    def test_unchanged_payload_reuses_archive_checksum_and_pointer_without_rewriting(self):
        first = builder.build()
        archive = Path(first['zip'])
        files = [archive, archive.with_suffix('.zip.sha256'), self.root / 'dist/current_release.json']
        before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in files}
        second = builder.build()
        self.assertTrue(second['reused'])
        self.assertEqual(first['zip'], second['zip'])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in files})

    def test_changed_payload_replaces_previous_package_after_validation(self):
        first = builder.build()
        self.engine.write_text('value = 2\n', encoding='utf-8')
        second = builder.build()
        self.assertFalse(second['reused'])
        self.assertNotEqual(first['zip'], second['zip'])
        self.assertFalse(Path(first['zip']).exists())
        self.assertFalse(Path(first['zip']).with_suffix('.zip.sha256').exists())
        info, _, _ = builder.archive_info(Path(second['zip']))
        self.assertEqual(info['sha256'], second['sha256'])

    def test_invalid_new_archive_preserves_previous_package_and_pointer(self):
        first = builder.build()
        archive = Path(first['zip'])
        pointer = self.root / 'dist/current_release.json'
        before = (archive.read_bytes(), pointer.read_bytes())
        self.engine.write_text('value = 2\n', encoding='utf-8')
        with patch.object(builder, 'archive_info', side_effect=ValueError('validation failed')):
            with self.assertRaisesRegex(ValueError, 'validation failed'):
                builder.build()
        self.assertEqual(before, (archive.read_bytes(), pointer.read_bytes()))
        self.assertEqual(list((self.root / 'dist').glob('so2-clipper-*.zip')), [archive])
        self.assertFalse(list((self.root / 'dist').glob('*.part')))

    def test_cleanup_rejects_keep_path_outside_dist(self):
        result = builder.build()
        outside = self.root / 'outside.zip'
        outside.write_bytes(b'keep')
        with self.assertRaisesRegex(ValueError, 'dist'):
            builder.clean_old_packages(self.root / 'dist', outside)
        self.assertTrue(Path(result['zip']).is_file())
        self.assertEqual(outside.read_bytes(), b'keep')


if __name__ == '__main__':
    unittest.main()
