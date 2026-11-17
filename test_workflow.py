"""真实文件/FFmpeg 边界与工作流缺陷回归；OCR 模型留给真实素材验收。"""
import contextlib
import copy
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / 'scripts'))
import config
import core
import extraction
import kill_clipper as cli
import media
import reporting
import store
import numpy as np


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory(prefix='so2_workflow_assets_')
        cls.source = Path(cls.assets.name).resolve() / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '12',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(cls.source)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='so2_workflow_')
        self.root = Path(self.tmp.name).resolve()
        self.inputs = self.root / 'inputs'
        self.inputs.mkdir()
        self.video = self.inputs / 'one.mp4'
        shutil.copy2(self.source, self.video)
        self.cfg = config.load_config(['--profile', 'pc', '--scan-engine', 'cv2', '--scan-segments', '1',
                                      '--identity-mode', 'keywords', '--keywords', 'Scarface',
                                      '--video-dir', str(self.inputs), '--output-dir', str(self.root / 'out')])
        self.quiet = contextlib.redirect_stdout(io.StringIO())
        self.quiet.__enter__()

    def tearDown(self):
        self.quiet.__exit__(None, None, None)
        self.tmp.cleanup()

    def outcome(self, hits=None):
        cfg = core.prepare_cfg(self.cfg, str(self.video))
        return core.ScanResult(str(self.video), 'complete', hits=hits or [],
                               identity=store.identity(self.video), coverage=[0.0, cfg.media_info['duration']],
                               phase={'checked': True, 'intervals': []})

    def cached(self, hits=None):
        data = store.load_results(self.cfg)
        store.record_scan(data, self.outcome(hits), core.prepare_cfg(self.cfg, str(self.video)))
        return data

    def test_group_boundary_and_padding_do_not_remerge(self):
        groups = extraction.group_kills([10, 20, 30.001, 35], self.cfg, 60)
        self.assertEqual(groups, [(0.0, 35, 2), (15.001000000000001, 50, 2)])
        self.assertGreater(groups[0][1], groups[1][0])
        self.assertEqual(len(extraction.group_kills([100, 125, 150], self.cfg, 200)), 3)

    def test_video_edges_are_clamped(self):
        self.assertEqual(extraction.group_kills([0, 9, 59], self.cfg, 60), [(0.0, 24, 2), (44, 60, 1)])
        with self.assertRaises(ValueError):
            extraction.group_kills([60], self.cfg, 60)

    def test_same_name_different_path_and_revision(self):
        other = self.root / 'other' / 'one.mp4'
        other.parent.mkdir()
        shutil.copy2(self.video, other)
        self.assertNotEqual(store.media_id(self.video), store.media_id(other))
        data = self.cached([5, 6])
        self.assertEqual(core.needs_scan(self.cfg, data, [str(self.video)]), [])
        self.assertEqual(core.needs_scan(self.cfg, data, [str(other)]), [str(other)])
        stat = self.video.stat()
        os.utime(self.video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
        self.assertEqual(core.needs_scan(self.cfg, data, [str(self.video)]), [str(self.video)])

    def test_cache_ignores_extraction_and_concurrency_but_tracks_sampling(self):
        resolved = core.prepare_cfg(self.cfg, str(self.video))
        fingerprint = store.cfg_fingerprint(resolved)
        for key, value in [('workers', 2), ('scan_segments', 4), ('pre', 1), ('post', 1),
                           ('min_kills', 1), ('kill_gap', 2), ('mode', 'copy')]:
            changed = copy.copy(resolved)
            setattr(changed, key, value)
            self.assertEqual(store.cfg_fingerprint(changed), fingerprint, key)
        resolved.effective_engine = 'ffmpeg'
        changed = copy.copy(resolved)
        changed.roi_fps = 1
        self.assertNotEqual(store.cfg_fingerprint(resolved), store.cfg_fingerprint(changed))

    def test_failure_preserves_success_and_retries(self):
        data = self.cached([5, 6])
        resolved = core.prepare_cfg(self.cfg, str(self.video))
        with patch.object(core, '_scan_segment', side_effect=RuntimeError('GPU unavailable')):
            failed = core.scan_video(str(self.video), self.cfg)
        self.assertEqual(failed.status, 'failed')
        self.assertIn('GPU unavailable', failed.error)
        store.record_scan(data, failed, resolved)
        self.assertEqual(data['videos'][store.media_id(self.video)]['hits'], [5, 6])
        self.assertFalse(store.cache_valid(data['videos'][store.media_id(self.video)], self.video, resolved))

    def test_zero_hits_are_valid_success(self):
        data = self.cached([])
        self.assertEqual(core.needs_scan(self.cfg, data, [str(self.video)]), [])

    def test_v2_migration_backups_original_bytes_and_preserves_missing(self):
        p = Path(self.cfg.result_file)
        p.parent.mkdir()
        original = json.dumps({'version': 2, 'videos': {'old': {'path': str(self.root / 'missing.mp4'), 'hits': [2]}}}).encode()
        p.write_bytes(original)
        data = store.load_results(self.cfg)
        self.assertEqual(data['legacy_videos']['old']['hits'], [2])
        self.assertFalse(data['legacy_videos']['old']['source_available'])
        store.save_results(self.cfg, data)
        self.assertEqual(Path(data['migration']['backup']).read_bytes(), original)
        self.assertEqual(store.load_results(self.cfg)['version'], 3)

    def test_atomic_write_failure_does_not_truncate_old_results(self):
        p = self.root / 'results.json'
        p.write_text('{"old":true}', encoding='utf-8')
        with patch.object(store.os, 'replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                store.atomic_json(p, {'new': True})
        self.assertEqual(json.loads(p.read_text()), {'old': True})

    def test_corrupt_results_do_not_restart_or_overwrite(self):
        p = Path(self.cfg.result_file)
        p.parent.mkdir()
        p.write_bytes(b'{broken')
        with patch.object(core, 'setup_logging'):
            self.assertEqual(cli.main(['--video', str(self.video), '--output-dir', self.cfg.output_dir]), 1)
        self.assertEqual(p.read_bytes(), b'{broken')

    @staticmethod
    def box(x, y, text, confidence=.95):
        return ([[x, y], [x+30, y], [x+30, y+15], [x, y+15]], text, confidence)

    def test_direction_short_uppercase_name_and_lookalike(self):
        boxes = [self.box(0, 0, 'BP +'), self.box(50, 0, 'Scarface'), self.box(150, 0, 'Kxz')]
        self.assertEqual(core._classify_boxes(boxes, self.cfg, 0, 120)[0], [])
        boxes = [self.box(50, 0, 'Scarfoot'), self.box(150, 0, 'Kxz')]
        self.assertEqual(core._classify_boxes(boxes, self.cfg, 0, 120), ([], []))
        boxes[0] = self.box(50, 0, 'Scarface2')
        accepted, review = core._classify_boxes(boxes, self.cfg, 0, 120)
        self.assertEqual(accepted, [])
        self.assertEqual(review[0]['reason'], 'ambiguous_name')
        boxes[0] = self.box(50, 0, 'XScarface')
        accepted, review = core._classify_boxes(boxes, self.cfg, 0, 120)
        self.assertEqual(accepted, [])
        self.assertEqual(review[0]['reason'], 'ambiguous_name')

    def test_low_confidence_is_review_not_a_kill(self):
        boxes = [self.box(50, 0, 'Scarface', .1), self.box(150, 0, 'Kxz')]
        accepted, review = core._classify_boxes(boxes, self.cfg, 0, 120)
        self.assertFalse(accepted)
        self.assertEqual(review[0]['reason'], 'low_confidence')

    def test_bomb_self_death_does_not_make_a_single_kill_into_a_double(self):
        events = [
            {'ts': 4719.0, 'text': 'Scarface + Q2aNYu', 'signature': 'scarfaceq2anyu|tamhilahzuur'},
            {'ts': 4722.6666667, 'text': 'Scarface + koldplay', 'signature': 'scarfacekoldplay|scarface'},
        ]
        trusted, review = core.filter_same_player_events(events, self.cfg)
        self.assertEqual([e['ts'] for e in trusted], [4719.0])
        self.assertEqual(review[0]['reason'], 'same_player_name')
        groups = extraction.group_kills([e['ts'] for e in trusted], self.cfg, 4895.234)
        self.assertFalse([g for g in groups if g[2] >= self.cfg.min_kills])

    def test_different_victim_name_is_preserved_by_self_death_filter(self):
        events = [{'ts': 10, 'text': 'Scarface + BP', 'signature': 'scarfacebp|scarface2'}]
        trusted, review = core.filter_same_player_events(events, self.cfg)
        self.assertEqual(trusted, events)
        self.assertEqual(review, [])

    def test_fading_row_name_change_is_not_an_extra_kill(self):
        events = [{'ts': 15.0, 'last_seen': 18.5, 'confidence': .95,
                   'signature': 'scarface|mao', 'row_y': 70.0}]
        review = []
        core.observe_hits(events, review, [(.8, 'Scarface', 100, 'scarface|030', 70.0)], 19.5, self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual(review[0]['reason'], 'unstable_row')

    def test_new_row_pushing_old_row_down_remains_a_new_kill(self):
        events = [{'ts': 15.0, 'last_seen': 15.0, 'confidence': .95,
                   'signature': 'scarface|mao', 'row_y': 10.0}]
        review = []
        core.observe_hits(events, review, [(.9, 'Scarface', 100, 'scarface|wind', 10.0),
                                          (.9, 'Scarface', 100, 'scarface|mao', 60.0)], 15.333, self.cfg)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]['signature'], 'scarface|wind')
        self.assertEqual(review, [])

    def test_first_frame_misread_and_later_stable_name_are_one_event(self):
        events, review = [], []
        core.observe_hits(events, review, [(.9, 'Scarface', 100, 'scarface|n30', 10.0)], 15, self.cfg)
        for ts in (15.166, 15.333, 16, 17, 18):
            core.observe_hits(events, review, [(.99, 'Scarface', 100, 'scarface|mao', 10.0)], ts, self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['ts'], 15)
        self.assertEqual(events[0]['signature'], 'scarface|mao')
        self.assertEqual(core.merge_review(review, events, self.cfg), [])

    def test_same_victim_after_broadcast_disappears_is_new_kill(self):
        events, review = [], []
        hit = [(.95, 'Scarface', 100, 'scarface|andrey', 10.0)]
        core.observe_hits(events, review, hit, 0, self.cfg)
        core.observe_hits(events, review, hit, 1, self.cfg)
        core.observe_hits(events, review, hit, 5, self.cfg)
        self.assertEqual([e['ts'] for e in core.merge_events(events, 8)], [0, 5])

    def test_same_victim_two_rows_are_two_events_even_with_overlap(self):
        events, review = [], []
        core.observe_hits(events, review, [(.95, 'Scarface', 100, 'scarface|andrey', 10.0)], 0, self.cfg)
        core.observe_hits(events, review, [(.95, 'Scarface', 100, 'scarface|andrey', 10.0),
                                          (.95, 'Scarface', 100, 'scarface|andrey', 60.0)], 1, self.cfg)
        self.assertEqual(len(events), 2)
        self.assertEqual(len(core.merge_events(events, 8)), 2)

    def test_row_pushed_down_with_ocr_noise_does_not_add_a_kill(self):
        events, review = [], []
        core.observe_hits(events, review, [(.95, 'Scarface', 100, 'scarface|怨命稠卡', 10.0)], 0, self.cfg)
        core.observe_hits(events, review, [(.95, 'Scarface', 100, 'scarface|8怨命辋卡1', 55.0)], .333, self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual(review[0]['reason'], 'unstable_row')

    def test_clearly_different_victim_at_previous_row_position_is_new_kill(self):
        events, review = [], []
        core.observe_hits(events, review, [(.95, 'Scarface', 100, 'scarface|怨命辋卡', 50.0)], 0, self.cfg)
        core.observe_hits(events, review, [(.95, 'Scarface', 100, 'scarface|怨命辋卡', 50.0)], 1, self.cfg)
        core.observe_hits(events, review, [], 1.1, self.cfg)
        core.observe_hits(events, review, [], 1.2, self.cfg)
        core.observe_hits(events, review, [(.99, 'Scarface', 100, 'scarface|mm', 50.0)], 1.3, self.cfg)
        self.assertEqual(len(events), 2)
        self.assertEqual(review, [])

    def test_probe_timeout_is_diagnosable_not_an_uncaught_batch_error(self):
        media._probe.cache_clear()
        with patch.object(media.subprocess, 'run', side_effect=subprocess.TimeoutExpired('ffprobe', 60)):
            with self.assertRaisesRegex(ValueError, '视频探测失败'):
                media.probe(self.video)

    def test_every_new_row_in_frame_and_resume_deduplication(self):
        cfg = core.prepare_cfg(self.cfg, str(self.video))
        hits = [(.95, 'Scarface', 250, 'scarface|andrey'), (.9, 'Scarface', 250, 'scarface|kxz')]
        frame = np.zeros((16, 80, 3), np.uint8)
        samples = [(n / 3, frame, frame) for n in range(36)]
        with patch.object(core, 'build_reader'), patch.object(media, 'cv2_frames', return_value=iter(samples)), \
             patch.object(core, 'read_roi', return_value=[]), patch.object(core, '_classify_boxes', side_effect=[(hits, []), (hits, [])] + [([], [])] * 36):
            scan = core.scan_video(str(self.video), cfg)
        self.assertEqual(scan.status, 'complete', scan.error)
        self.assertEqual(len(scan.hits), 2)
        self.assertEqual(scan.hits, [0, 0])
        self.assertTrue(list(Path(cfg.tmp_dir).glob('scan_*.json')))
        with patch.object(core, 'build_reader', side_effect=AssertionError('completed segment must be reused')):
            resumed = core.scan_video(str(self.video), cfg)
        self.assertEqual(resumed.hits, scan.hits)

    def test_interleaved_segment_duplicates_keep_first_time(self):
        raw = [(5.0, .8, 'scarface|andrey'), (5.1, .9, 'scarface|kxz'), (5.4, .99, 'scarface|andrey')]
        self.assertEqual(core.cluster_hits(raw, 3.6), [(5.0, .99), (5.1, .9)])

    def test_batch_interruption_keeps_first_video_persisted(self):
        other = self.inputs / 'two.mp4'
        shutil.copy2(self.video, other)
        def interrupted(cfg, jobs):
            path, resolved = jobs[0]
            self.assertEqual(path, str(self.video))
            yield self.outcome([5, 6]), resolved
            raise KeyboardInterrupt()
        with patch.object(core, 'setup_logging'), patch.object(cli, 'scan_all', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                cli.run(self.cfg)
        data = store.load_results(self.cfg)
        self.assertTrue(store.cache_valid(data['videos'][store.media_id(self.video)], self.video,
                                         core.prepare_cfg(self.cfg, str(self.video))))
        self.assertNotIn(store.media_id(other), data['videos'])

    def test_manual_single_file_does_not_extract_history(self):
        unrelated = self.inputs / 'two.mp4'
        shutil.copy2(self.video, unrelated)
        data = self.cached([5, 6])
        data['videos'][store.media_id(unrelated)] = {'path': str(unrelated), 'hits': [5, 6], 'status': 'complete'}
        store.save_results(self.cfg, data)
        cfg = config.load_config(['--video', str(self.video), '--hits', '5,6', '--output-dir', self.cfg.output_dir])
        with patch.object(core, 'setup_logging'), patch.object(extraction, 'extract_clips', return_value=[]) as extract:
            self.assertEqual(cli.run(cfg), 0)
        self.assertEqual(extract.call_count, 1)
        self.assertEqual(extract.call_args.args[0], str(self.video))

    def test_unknown_source_is_review_with_screenshot(self):
        cfg = config.load_config(['--video', str(self.video), '--output-dir', self.cfg.output_dir,
                                  '--identity-mode', 'keywords', '--keywords', 'Scarface'])
        with patch.object(core, 'setup_logging'), patch.object(core, 'scan_video', side_effect=AssertionError('unknown source must not scan')):
            self.assertEqual(cli.run(cfg), 3)
        self.assertEqual(len(list(Path(cfg.state_dir).rglob('*.jpg'))), 1)
        self.assertFalse(list(Path(cfg.output_dir).rglob('*.jpg')))

    def test_extract_only_without_valid_scan_returns_failure(self):
        self.cfg.extract_only = True
        with patch.object(core, 'setup_logging'):
            self.assertEqual(cli.run(self.cfg), 1)
        self.assertEqual(store.load_results(self.cfg)['last_run']['exit_code'], 1)

    def test_review_screenshot_tracks_source_revision(self):
        item = {'ts': 1, 'reason': 'unknown_profile'}
        first = reporting.review_image(str(self.video), item, self.root / 'review-out')
        self.assertEqual(reporting.review_image(str(self.video), item, self.root / 'review-out'), first)
        stat = self.video.stat()
        os.utime(self.video, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000000))
        second = reporting.review_image(str(self.video), item, self.root / 'review-out')
        self.assertNotEqual(second, first)
        self.assertTrue(first.is_file())
        self.assertTrue(second.is_file())

    def test_generated_clip_checked_and_reused_across_dates(self):
        self.cfg.pre, self.cfg.post = 1, 1
        clips = extraction.extract_clips(str(self.video), [4, 5], self.cfg)
        self.assertEqual([c.status for c in clips], ['generated'], str(clips))
        self.assertTrue(clips[0].validation['has_audio'])
        tomorrow = store.now() + timedelta(days=1)
        with patch.object(store, 'now', return_value=tomorrow), \
             patch.object(extraction, 'extract_segment', side_effect=AssertionError('must reuse')):
            reused = extraction.extract_clips(str(self.video), [4, 5], self.cfg, [asdict(c) for c in clips])
        self.assertEqual(reused[0].status, 'reused')
        self.assertEqual(reused[0].path, clips[0].path)
        self.assertEqual(reused[0].settings['pre'], 1)

    def test_extraction_parameters_are_recorded_and_invalidate_clip_cache(self):
        self.cfg.pre, self.cfg.post = 1, 1
        clips = extraction.extract_clips(str(self.video), [4, 5], self.cfg)
        self.cfg.kill_gap = 3
        changed = extraction.extract_clips(str(self.video), [4, 5], self.cfg, [asdict(c) for c in clips])
        self.assertEqual(changed[0].status, 'generated', changed[0].error)
        self.assertNotEqual(changed[0].key, clips[0].key)
        self.assertEqual(changed[0].settings['kill_gap'], 3)

    def test_corrupt_output_is_regenerated_and_single_kill_filtered(self):
        self.cfg.pre, self.cfg.post, self.cfg.kill_gap = 1, 1, 2
        clips = extraction.extract_clips(str(self.video), [4, 5, 10], self.cfg)
        self.assertEqual([c.status for c in clips], ['generated', 'filtered'])
        Path(clips[0].path).write_bytes(b'broken')
        repaired = extraction.extract_clips(str(self.video), [4, 5, 10], self.cfg, [asdict(c) for c in clips])
        self.assertEqual(repaired[0].status, 'generated', repaired[0].error)

    def test_encoder_failure_and_missing_audio_are_not_usable(self):
        self.cfg.pre, self.cfg.post = 1, 1
        failed = subprocess.CompletedProcess([], 1, '', 'disk full')
        with patch.object(extraction.subprocess, 'run', return_value=failed):
            result = extraction._ffmpeg_extract(str(self.video), 3, 6, str(self.root / 'bad.mp4'), self.cfg, 2)
        self.assertEqual(result.status, 'failed')
        self.assertIn('disk full', result.error)
        self.assertEqual(reporting.exit_code([{'status': 'complete', 'clips': [asdict(result)]}]), 1)
        mute = self.root / 'mute.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.video), '-t', '3', '-an', '-c:v', 'copy', str(mute)], check=True)
        with self.assertRaisesRegex(ValueError, '音轨缺失'):
            extraction.validate_clip(mute, 3, media.probe(self.video))

    def test_rotation_and_nonzero_ts_start_use_display_and_relative_time(self):
        rotated = self.root / 'rotated.mp4'
        transport = self.root / 'offset.ts'
        subprocess.run(['ffmpeg', '-v', 'error', '-display_rotation', '-90', '-i', str(self.video), '-c', 'copy', str(rotated)], check=True)
        subprocess.run(['ffmpeg', '-v', 'error', '-i', str(self.video), '-c', 'copy', '-output_ts_offset', '10.05', str(transport)], check=True)
        self.assertEqual((media.probe(rotated)['width'], media.probe(rotated)['height']), (180, 320))
        cfg = core.prepare_cfg(self.cfg, str(rotated))
        times = [t for t, roi in media.ffmpeg_frames(str(rotated), cfg, 1, 2)]
        self.assertEqual(len(times), 3)
        self.assertAlmostEqual(times[0], 1)
        self.assertGreater(media.probe(transport)['start_time'], 10)
        cfg = core.prepare_cfg(self.cfg, str(transport))
        samples = list(media.ffmpeg_frames(str(transport), cfg, 1, 2))
        self.assertEqual(len(samples), 3)
        self.assertEqual(samples[0][0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
