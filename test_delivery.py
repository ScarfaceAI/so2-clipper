"""自主交付的实际文件行为回归；合成视频只验证执行，不声称游戏识别准确率。"""
import contextlib
import copy
import io
import json
import logging
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import config
import core
import delivery
import evidence
import kill_clipper
import media
import review
import store
import workspace


class DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory(prefix='so2_delivery_assets_')
        cls.video = Path(cls.assets.name).resolve() / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=12',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '8',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(cls.video)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='so2_delivery_')
        self.root = Path(self.temp.name).resolve()
        self.args = ['--video', str(self.video), '--profile', 'pc', '--identity-mode', 'auto',
                     '--output-dir', str(self.root / 'output'), '--state-dir', str(self.root / 'state')]
        self.cfg = media.prepare_cfg(config.load_config(self.args), str(self.video))
        self.entry = {'path': str(self.video), 'identity': store.identity(self.video), 'status': 'complete',
             'origin': 'ocr', 'coverage': [0., 8.], 'media_info': media.probe(self.video),
             'cfg_fingerprint': store.cfg_fingerprint(self.cfg), 'hits': [1., 6.],
             'events': [dict(ts=t, actor='a', view_id='view1', signature=f'a|victim{t}', ids=[str(t)]) for t in (1., 6.)],
             'phase': {'checked': True, 'intervals': []}, 'review': [], 'clips': [],
             'perspective': {'views': [{'id': 'view1', 'start': 0., 'end': 8., 'actor': 'a'}], 'pulses': []}}
        store.save_results(self.cfg, {'version': 3, 'videos': {store.media_id(self.video): self.entry}})
        self.plan_path = Path(delivery.prepare(self.cfg)['plan'])

    def tearDown(self):
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(self.root):
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.temp.cleanup()

    def ready(self, weapons=('步枪', '步枪', '步枪')):
        plan = delivery.read(self.plan_path)
        cid = plan['clips'][0]['clip_id']
        pack = delivery.make_evidence(self.plan_path, clip_id=cid,
                                     times=[0., .3, 1.7, 2.3, 3.7, 5.3, 6.7, 7.9])
        plan = delivery.read(self.plan_path)
        frames = pack['frames']
        source = plan['sources'][0]
        source['review'] = {'status': 'reviewed', 'reason': '合成执行测试，不代表游戏内容验收',
                           'evidence': [f['path'] for f in frames], 'unresolved': []}
        clip = plan['clips'][0]
        clip.update(status='keep', reason='合成执行测试：补入未被 OCR 记录的第三次事件', start=0., end=8.)
        clip['kills'] = [{'ts': ts, 'actor': 'a', 'weapon': weapon,
                         'evidence': [f['path'] for f in frames if abs(f['ts'] - ts) < 1]}
                        for ts, weapon in zip((1., 3., 6.), weapons)]
        clip['boundary_evidence'] = [frames[0]['path'], frames[-1]['path']]
        store.atomic_json(self.plan_path, plan)
        return plan, cid

    def finish_reviews(self):
        plan = delivery.read(self.plan_path)
        for item in plan['output_reviews'].values():
            item.update(status='confirmed', reason='合成成片执行测试：身份和证据绑定')
        store.atomic_json(self.plan_path, plan)

    def run_delivery(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return delivery.deliver(self.plan_path)

    def test_long_gap_round_candidates_do_not_change_legacy_grouping(self):
        entry = copy.deepcopy(self.entry)
        entry['media_info']['duration'] = 100
        entry['perspective']['views'][0]['end'] = 100
        entry['events'][1]['ts'] = 45
        self.assertEqual(len(delivery.candidates(entry, self.cfg)), 1)
        import extraction
        self.assertEqual(len(extraction.group_events(entry['events'], self.cfg, 100,
                             views=entry['perspective']['views'])), 2)

    def test_hits_fallback_keeps_legacy_candidate_id_without_view_id(self):
        entry = {
            'identity': {'path': 'C:/so2-fixtures/source.mp4', 'size': 123, 'mtime_ns': 456},
            'events': [],
            'hits': [2.0],
            'media_info': {'duration': 8.0},
            'perspective': {'views': []},
        }
        rows = delivery.candidates(entry, self.cfg)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['clip_id'], '5e14a91a270cadd6f86b')
        self.assertEqual(rows[0]['round_id'], '')
        self.assertEqual(rows[0]['scan_times'], [2.0])

    def test_prepare_resume_before_first_export_retains_decisions_and_evidence(self):
        plan, _ = self.ready()
        resumed = delivery.prepare(self.cfg)
        self.assertTrue(resumed['reused'])
        self.assertEqual(resumed['plan'], str(self.plan_path))
        self.assertEqual(delivery.read(self.plan_path), plan)
        self.assertEqual(delivery.review_index(self.cfg)[store.media_id(self.video)]['plan'], str(self.plan_path))

    def test_prepare_refreshes_after_scan_without_losing_existing_review(self):
        plan, _ = self.ready()
        original_clip = copy.deepcopy(plan['clips'][0])
        results = store.load_results(self.cfg)
        entry = results['videos'][store.media_id(self.video)]
        entry['events'].append(dict(ts=3., actor='a', view_id='view1', signature='a|new', ids=['new']))
        entry['hits'].append(3.)
        store.save_results(self.cfg, results)
        resumed = delivery.prepare(self.cfg)
        self.assertTrue(resumed['refreshed_sources'])
        refreshed = delivery.read(self.plan_path)
        self.assertEqual(refreshed['clips'][0], original_clip)
        self.assertEqual(len(refreshed['clips']), 2)
        self.assertEqual(refreshed['sources'][0]['review']['status'], 'pending')
        self.assertTrue(refreshed['evidence_manifests'])

    def test_prepare_before_scan_then_completed_scan_adds_candidates(self):
        other = config.load_config(self.args[:-2] + ['--state-dir', str(self.root / 'empty-state')])
        path = Path(delivery.prepare(other)['plan'])
        self.assertEqual(delivery.read(path)['clips'], [])
        store.save_results(other, store.load_results(self.cfg))
        result = delivery.prepare(other)
        self.assertEqual(result['plan'], str(path))
        self.assertTrue(delivery.read(path)['sources'][0]['scan_valid'])
        self.assertEqual(len(delivery.read(path)['clips']), 1)

    def test_missing_kill_export_and_repeat_keep_hash_timestamp_and_original_scan(self):
        _, cid = self.ready()
        first = self.run_delivery()
        self.assertEqual(first['exit_code'], 3)
        self.assertEqual(first['outputs'][0]['kill_count'], 3)
        self.assertIn('3杀', first['outputs'][0]['path'])
        self.assertTrue(first['outputs'][0]['full_decode'])
        self.assertTrue(first['unresolved'])
        before = delivery._record(first['outputs'][0]['path'])
        self.finish_reviews()
        second = self.run_delivery()
        self.assertEqual(second['exit_code'], 0, second)
        self.assertEqual(second['outputs'][0]['status'], 'reused')
        self.assertEqual(before, delivery._record(second['outputs'][0]['path']))
        entry = store.load_results(self.cfg)['videos'][store.media_id(self.video)]
        self.assertEqual(entry['hits'], [1., 6.])
        self.assertEqual(entry['curated_delivery']['plan'], str(self.plan_path))

    def test_category_alias_moves_bytes_and_resumes_after_file_lock(self):
        _, cid = self.ready()
        first = self.run_delivery()['outputs'][0]
        plan = delivery.read(self.plan_path)
        plan['settings']['categories']['步枪'] = 'Rifle'
        store.atomic_json(self.plan_path, plan)
        original = delivery._move_owned
        with patch.object(delivery, '_move_owned', side_effect=PermissionError('file is in use')):
            failed = self.run_delivery()
        self.assertEqual(failed['exit_code'], 1)
        self.assertTrue(Path(first['path']).exists())
        with patch.object(delivery, '_move_owned', wraps=original):
            resumed = self.run_delivery()['outputs'][0]
        self.assertFalse(Path(first['path']).exists())
        self.assertEqual(first['sha256'], resumed['sha256'])
        self.assertEqual(Path(resumed['path']).parent.name, 'Rifle')
        entry = store.load_results(self.cfg)['videos'][store.media_id(self.video)]
        self.assertNotIn(first['path'], [c['path'] for c in entry['clips']])

    def test_two_weapon_classes_are_mixed_and_short_tail_is_refused(self):
        plan, _ = self.ready(('步枪', '手枪', '步枪'))
        self.assertEqual(delivery.validate_plan(plan)[1][0]['category'], '混合')
        plan['clips'][0]['end'] = 5.9
        with self.assertRaisesRegex(ValueError, '范围'):
            delivery.validate_plan(plan)
        self.assertFalse(Path(self.cfg.output_dir).exists())

    def test_move_completed_before_journal_write_is_recovered(self):
        self.ready()
        first = self.run_delivery()['outputs'][0]
        plan = delivery.read(self.plan_path)
        plan['settings']['categories']['步枪'] = 'Rifle'
        store.atomic_json(self.plan_path, plan)
        original = delivery._move_owned
        def interrupted(*args):
            original(*args)
            raise KeyboardInterrupt
        with patch.object(delivery, '_move_owned', side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_delivery()
        self.assertFalse(Path(first['path']).exists())
        final = self.run_delivery()
        self.assertEqual(final['outputs'][0]['sha256'], first['sha256'])
        self.assertEqual(final['outputs'][0]['status'], 'reused')

    def test_range_revision_replaces_only_owned_output_and_keeps_backup(self):
        self.ready()
        first = self.run_delivery()['outputs'][0]
        plan = delivery.read(self.plan_path)
        plan['clips'][0]['end'] = 7.5
        store.atomic_json(self.plan_path, plan)
        final = self.run_delivery()
        self.assertNotEqual(final['outputs'][0]['key'], first['key'])
        self.assertFalse(Path(first['path']).exists())
        journal = delivery.read(self.plan_path.parent / 'delivery.json')
        backup = journal['retired'][0]['backup']
        self.assertEqual(review.image_hash(backup), first['sha256'])
        entry = store.load_results(self.cfg)['videos'][store.media_id(self.video)]
        self.assertEqual([c['path'] for c in entry['clips']], [final['outputs'][0]['path']])

    def test_unresolved_source_never_becomes_complete_after_output_review(self):
        self.ready()
        plan = delivery.read(self.plan_path)
        plan['sources'][0]['review']['unresolved'] = ['原片缺段，第三回合无法确认']
        store.atomic_json(self.plan_path, plan)
        self.run_delivery()
        self.finish_reviews()
        final = self.run_delivery()
        self.assertEqual(final['exit_code'], 3)
        self.assertEqual(final['status'], 'partial')
        self.assertEqual(len(final['outputs']), 1)

    def test_evidence_tamper_and_wrong_actor_are_rejected_before_export(self):
        plan, _ = self.ready()
        plan['settings']['actors'] = ['another_player']
        with self.assertRaisesRegex(ValueError, '人物'):
            delivery.validate_plan(plan)
        plan['settings']['actors'] = []
        image = Path(plan['clips'][0]['kills'][0]['evidence'][0])
        image.write_bytes(b'tampered')
        _, prepared, _, failed = delivery.validate_plan(plan)
        self.assertEqual(prepared, [])
        self.assertIn('变化', failed[0]['reason'])

    def test_confirmed_anonymous_view_is_allowed_for_all_and_rejected_for_named_target(self):
        plan, _ = self.ready()
        clip = plan['clips'][0]
        clip['actor'] = '视角人物_0123abcd'
        for kill in clip['kills']:
            kill['actor'] = clip['actor']
        plan['settings']['actors'] = []
        _, prepared, unresolved, failed = delivery.validate_plan(plan)
        self.assertEqual(len(prepared), 1)
        self.assertEqual(unresolved, [])
        self.assertEqual(failed, [])
        plan['settings']['actors'] = ['Scarface']
        with self.assertRaisesRegex(ValueError, '人物'):
            delivery.validate_plan(plan)

    def test_duplicate_disposition_and_unresolved_scan_counter(self):
        plan, _ = self.ready()
        other = copy.deepcopy(plan['clips'][0])
        other['clip_id'] = 'duplicate-example'
        plan['clips'].append(other)
        with self.assertRaisesRegex(ValueError, '重复交付'):
            delivery.validate_plan(plan)
        other.update(status='duplicate', duplicate_of=plan['clips'][0]['clip_id'])
        self.assertEqual(len(delivery.validate_plan(plan)[1]), 1)
        plan['sources'][0]['review_queue'] = [{'item_id': 'mismatch', 'reason': 'counter_feed_mismatch'}]
        self.assertTrue(delivery.validate_plan(plan)[2])

    def test_interrupted_output_evidence_resumes_without_reencoding(self):
        self.ready()
        with patch.object(evidence, 'bundle', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.run_delivery()
        item = next(iter(delivery.read(self.plan_path.parent / 'delivery.json')['items'].values()))
        before = delivery._record(item['path'])
        result = self.run_delivery()
        self.assertEqual(result['outputs'][0]['status'], 'reused')
        self.assertEqual(before, delivery._record(item['path']))

    def test_plain_run_routes_to_curated_plan_and_does_not_recreate_old_groups(self):
        self.ready()
        output = self.run_delivery()['outputs'][0]
        before = delivery._record(output['path'])
        with contextlib.redirect_stdout(io.StringIO()):
            code = kill_clipper.main(self.args + ['--extract-only', '--run-id', 'guard'])
        self.assertEqual(code, 3)
        receipt = delivery.read(Path(self.cfg.state_dir) / 'runs/guard/receipt.json')
        self.assertEqual(receipt['files'][0]['curated_delivery']['plan'], str(self.plan_path))
        self.assertEqual(before, delivery._record(output['path']))
        self.assertEqual(len(list(Path(self.cfg.output_dir).rglob('*.mp4'))), 1)

    def test_writer_releases_on_interruption_and_refuses_overlap(self):
        with workspace.writer(self.cfg.state_dir):
            with self.assertRaisesRegex(ValueError, '写入者'):
                with workspace.writer(self.cfg.state_dir):
                    pass
        with self.assertRaises(KeyboardInterrupt):
            with workspace.writer(self.cfg.state_dir):
                raise KeyboardInterrupt
        with workspace.writer(self.cfg.state_dir):
            pass

    def test_failed_reporting_keeps_scan_stage_and_completed_scan(self):
        with patch('reporting.write_report', side_effect=ValueError('report interrupted')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                code = kill_clipper.main(self.args + ['--scan-only', '--run-id', 'report-failed'])
        self.assertEqual(code, 1)
        stage = delivery.read(Path(self.cfg.state_dir) / 'runs/report-failed/scan-stage.json')
        self.assertEqual(stage['files'][0]['coverage'], [0., 8.])
        self.assertEqual(store.load_results(self.cfg)['videos'][store.media_id(self.video)]['hits'], [1., 6.])


if __name__ == '__main__':
    unittest.main()
