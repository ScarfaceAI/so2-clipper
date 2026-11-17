"""热身阶段、真实双区域解码、断点与提取边界的行为回归。"""
import contextlib
import copy
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
import config
import core
import extraction
import kill_clipper as cli
import media
import store
import warmup


def box(text, confidence=.99, x=0, y=0):
    return ([[x, y], [x+60, y], [x+60, y+20], [x, y+20]], text, confidence)


def observation(ts, kind, text=''):
    return {'ts': ts, 'kind': kind, 'text': text or ('热身00:20' if kind == 'warmup' else ''),
            'confidence': .99}


class WarmupLogicTests(unittest.TestCase):
    def test_word_and_clock_required_on_same_row(self):
        for text in ('热身 01:02', 'WARM-UP 00:25'):
            self.assertEqual(warmup.classify_boxes([box(text)])['kind'], 'warmup')
        self.assertEqual(warmup.classify_boxes([box('热身'), box('00:25', x=80)])['kind'], 'warmup')
        for boxes in ([box('00:25')], [box('购买阶段00:05')], [box('回合开始00:18')],
                      [box('热身'), box('00:25', y=50)]):
            self.assertNotEqual(warmup.classify_boxes(boxes)['kind'], 'warmup')

    def test_low_confidence_or_invalid_clock_is_review(self):
        for boxes in ([box('热身00:25', .2)], [box('热身')], [box('热身00:99')], [box('热舁00:25')]):
            self.assertEqual(warmup.classify_boxes(boxes)['kind'], 'uncertain')

    def test_ready_countdown_is_still_prematch(self):
        for text in ('准备就绪00:04', '准备:00:03'):
            self.assertEqual(warmup.classify_boxes([box(text)])['kind'], 'warmup')
        self.assertEqual(warmup.classify_boxes([box('购买阶段')])['kind'], 'clear')

    def test_phase_ocr_can_read_chinese_with_ascii_player_id(self):
        cfg = config.load_config(['--profile', 'pc', '--keywords', 'Scarface'])
        cfg.media_info = {'width': 640, 'height': 360}
        self.assertTrue(cfg.allowlist)
        class Reader:
            def readtext(self, image, **kwargs):
                return [box('热身00:20')] if kwargs.get('allowlist') is None else []
        roi = np.zeros((96, 178, 3), np.uint8)
        self.assertEqual(warmup.read_phase(Reader(), roi, cfg)['kind'], 'warmup')

    def test_two_frames_confirm_and_one_missing_frame_does_not_end_warmup(self):
        samples = [observation(0, 'warmup'), observation(1, 'warmup'), observation(2, 'clear'),
                   observation(3, 'warmup'), observation(4, 'clear'), observation(5, 'clear')]
        self.assertEqual([(r['kind'], r['start'], r['end']) for r in warmup.intervals(samples, 0, 6)],
                         [('warmup', 0, 4)])

    def test_overlap_duplicates_do_not_confirm_single_bad_frame(self):
        sample = observation(3, 'warmup')
        result = warmup.intervals([sample, copy.deepcopy(sample), observation(4, 'clear'), observation(5, 'clear')], 0, 6)
        self.assertEqual(result[0]['kind'], 'uncertain')
        uncertain = {'ts': 3.2, 'text': 'Scarface', 'signature': 'scarface|andrey', 'confidence': .99}
        trusted, review, excluded, _ = warmup.filter_events([uncertain], [], result)
        self.assertFalse(trusted)
        self.assertFalse(excluded)
        self.assertEqual(review[0]['reason'], 'warmup_uncertain')

    def test_dense_phase_checks_still_confirm_two_separated_frames(self):
        samples = [observation(n/6, 'warmup') for n in range(7)]
        samples += [observation(2, 'clear'), observation(3, 'clear')]
        self.assertEqual(warmup.intervals(samples, 0, 4)[0]['kind'], 'warmup')

    def test_menu_occlusion_does_not_end_warmup(self):
        samples = [observation(0, 'warmup'), observation(1, 'warmup'), observation(2, 'unknown'),
                   observation(3, 'unknown'), observation(4, 'warmup'), observation(5, 'clear'), observation(6, 'clear')]
        result = warmup.intervals(samples, 0, 7)
        self.assertEqual([(r['kind'], r['start'], r['end']) for r in result], [('warmup', 0, 5)])

    def test_unconfirmed_end_is_review_not_assumed_warmup_forever(self):
        samples = [observation(0, 'warmup'), observation(1, 'warmup'), observation(2, 'unknown'),
                   observation(3, 'unknown'), observation(4, 'unknown')]
        result = warmup.intervals(samples, 0, 5)
        self.assertEqual(result[0]['kind'], 'warmup')
        self.assertEqual(result[1]['kind'], 'uncertain')
        self.assertEqual(result[1]['end'], 5)

    def test_only_normal_timer_at_calibrated_header_confirms_formal_phase(self):
        self.assertEqual(warmup.classify_boxes([box('01:30')])['kind'], 'unknown')
        self.assertEqual(warmup.classify_boxes([box('01:30')], (0, 0, 100, 30))['kind'], 'clear')
        self.assertEqual(warmup.classify_boxes([box('01:30', y=50)], (0, 0, 100, 30))['kind'], 'unknown')

    def test_mid_video_warmup_splits_groups_and_clamps_both_contexts(self):
        cfg = config.load_config([])
        ranges = [{'start': 10, 'end': 13, 'kind': 'warmup'}]
        self.assertEqual(extraction.group_kills([5, 6, 14, 15], cfg, 20, ranges),
                         [(0.0, 10, 2), (13, 20, 2)])
        with self.assertRaisesRegex(ValueError, '尚未过滤'):
            extraction.group_kills([11], cfg, 20, ranges)

    def test_context_stops_before_unsampled_warmup_start_and_keeps_last_kill_frame(self):
        samples = [observation(9, 'clear'), observation(10, 'warmup'), observation(11, 'warmup'),
                   observation(14, 'clear'), observation(15, 'clear')]
        ranges = warmup.intervals(samples, 0, 20)
        self.assertEqual(ranges[0]['context_start'], 9)
        cfg = config.load_config([])
        group = extraction.group_kills([7, 9], cfg, 20, ranges)[0]
        self.assertGreater(group[1], 9)
        self.assertLess(group[1], 9.1)

    def test_review_in_confirmed_warmup_is_excluded_but_formal_review_stays(self):
        events = [{'ts': 2, 'signature': 'scarface|andrey'}, {'ts': 6, 'signature': 'scarface|kxz'}]
        review = [{'ts': 2.5, 'reason': 'low_confidence'}, {'ts': 7, 'reason': 'low_confidence'}]
        interval = {'kind': 'warmup', 'start': 0, 'end': 4, 'evidence': [observation(1, 'warmup')]}
        trusted, pending, excluded, removed = warmup.filter_events(events, review, [interval])
        self.assertEqual([e['ts'] for e in trusted], [6])
        self.assertEqual([e['ts'] for e in pending], [7])
        self.assertEqual([e['ts'] for e in excluded], [2])
        self.assertEqual([e['ts'] for e in removed], [2.5])


class MarkerReader:
    def readtext(self, rgb, **kwargs):
        return [box('热身00:20' if rgb[:, :, 0].mean() > 100 else '购买阶段00:05')]


def marker_feed(reader, roi, cfg):
    value = float(roi[:, :, 0].mean())
    if value < 60:
        return []
    victims = ('Andrey', 'Kxz', 'Aquaaaaa', 'Elfor', 'Lemon', 'Wind')
    victim = victims[min(5, max(0, round((value-80)/30)))]
    return [box('Scarface', x=5), box(victim, x=110)]


class WarmupIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory(prefix='so2_warmup_assets_')
        cls.video = Path(cls.assets.name) / 'warmup.mp4'
        cfg = config.load_config(['--profile', 'pc'])
        cfg.media_info = {'width': 640, 'height': 360}
        x, y, w, h, _ = media.crop_rect(cfg)
        px, py, pw, ph = media.phase_rect(cfg)
        writer = cv2.VideoWriter(str(cls.video), cv2.VideoWriter_fourcc(*'mp4v'), 10, (640, 360))
        for n in range(200):
            t = n/10
            frame = np.zeros((360, 640, 3), np.uint8)
            if t < 4 or 10 <= t < 13:
                frame[py:py+ph, px:px+pw] = (0, 0, 220)
            for i, second in enumerate((1, 5, 7, 11, 14, 16)):
                if second <= t < second+.6:
                    frame[y:y+h, x:x+w] = (80+i*30,) * 3
            writer.write(frame)
        writer.release()

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='so2_warmup_')
        self.root = Path(self.tmp.name)
        self.cfg = config.load_config(['--video', str(self.video), '--profile', 'pc',
                                      '--identity-mode', 'keywords', '--keywords', 'Scarface',
                                      '--scan-engine', 'ffmpeg', '--scan-segments', '1',
                                      '--interval', '2',
                                      '--output-dir', str(self.root/'out')])
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(core, 'build_reader', return_value=MarkerReader()))
        self.stack.enter_context(patch.object(core, 'read_roi', side_effect=marker_feed))
        self.stack.enter_context(patch.object(core, 'setup_logging'))

    def tearDown(self):
        self.stack.close()
        self.tmp.cleanup()

    def scan(self, cfg=None):
        scan = core.scan_video(str(self.video), cfg or self.cfg)
        self.assertEqual(scan.status, 'complete', scan.error)
        return scan

    def test_both_decoders_filter_warmup_and_keep_formal_kills(self):
        for engine in ('ffmpeg', 'cv2'):
            with self.subTest(engine=engine):
                cfg = copy.deepcopy(self.cfg)
                cfg.scan_engine, cfg.rescan = engine, True
                scan = self.scan(cfg)
                self.assertEqual(scan.hits, [5, 7, 14, 16])
                self.assertEqual([e['ts'] for e in scan.phase['excluded_events']], [1, 11])
                self.assertEqual([(r['start'], r['end']) for r in scan.phase['intervals']], [(0, 4), (10, 13)])
                self.assertLess(scan.stats['phase_ocr_calls'], scan.stats['sampled_frames'])

    def test_explicit_keep_warmup_and_cache_version(self):
        scan = self.scan()
        resolved = media.prepare_cfg(self.cfg, str(self.video))
        data = store.load_results(self.cfg)
        entry = store.record_scan(data, scan, resolved)
        self.assertTrue(store.cache_valid(entry, self.video, resolved))
        unchecked = copy.deepcopy(entry)
        unchecked.pop('phase')
        self.assertFalse(store.cache_valid(unchecked, self.video, resolved))
        changed = copy.deepcopy(resolved)
        changed.filter_warmup = False
        self.assertFalse(store.cache_valid(entry, self.video, changed))
        old = copy.deepcopy(entry)
        old['cfg_fingerprint'] = store.digest(dict(old['scan_settings'], detection_version=12))
        self.assertFalse(store.cache_valid(old, self.video, resolved))
        changed.rescan = True
        self.assertEqual(self.scan(changed).hits, [1, 5, 7, 11, 14, 16])

    def test_resume_keeps_earlier_warmup_and_does_not_duplicate(self):
        fresh = self.scan()
        cfg = media.prepare_cfg(self.cfg, str(self.video))
        partial = core._scan_segment_ffmpeg(str(self.video), cfg, 0, 0, 14)
        partial.update(complete=False, resume_ts=14)
        store.save_progress(cfg, self.video, 0, 20, partial)
        resumed = self.scan()
        self.assertEqual(resumed.hits, fresh.hits)
        self.assertEqual([e['ts'] for e in resumed.phase['excluded_events']], [1, 11])
        self.assertEqual(resumed.phase['intervals'], fresh.phase['intervals'])

    def test_cli_reuses_phase_and_clips_and_report_lists_exclusions(self):
        self.assertEqual(cli.run(self.cfg), 0)
        data = store.load_results(self.cfg)
        entry = data['videos'][store.media_id(self.video)]
        clips = [c for c in entry['clips'] if c['status'] == 'generated']
        self.assertEqual([(c['start'], c['end'], c['kill_count']) for c in clips], [(4, 9.1, 2), (13, 20, 2)])
        for clip in clips:
            extraction.validate_clip(clip['path'], clip['end']-clip['start'], media.probe(self.video))
        report = Path(data['last_run']['report']).read_text(encoding='utf-8')
        self.assertIn('热身过滤：2 条候选击杀记录', report)
        self.assertIn('已过滤热身击杀 1.000s', report)
        self.cfg.extract_only = True
        with patch.object(core, 'scan_video', side_effect=AssertionError('must reuse OCR')), \
             patch.object(extraction, 'extract_segment', side_effect=AssertionError('must reuse clips')):
            self.assertEqual(cli.run(self.cfg), 0)
        self.assertEqual(store.load_results(self.cfg)['videos'][store.media_id(self.video)]['phase'], entry['phase'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
