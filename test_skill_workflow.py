"""视角分组、证据复核、原声合并与匹配运行回执的行为验收。"""
import copy
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import config
import core
import extraction
import kill_clipper
import media
import montage
import perspective
import review
import store


def event(ts, actor='a', view='v1', **kw):
    return dict(ts=ts, signature=actor+'|victim'+str(ts), killer=actor, victim='victim'+str(ts),
                actor=actor, view_id=view, ids=[str(ts)], **kw)


class ViewGroupingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = SimpleNamespace(pre=15, post=15, kill_gap=10, media_info={'fps': 30})
        self.views = [{'id': 'v1', 'start': 0, 'end': 5, 'actor': 'a'},
                      {'id': 'v2', 'start': 5, 'end': 20, 'actor': 'b'}]

    def test_switch_does_not_create_a_two_kill_group(self):
        groups = extraction.group_events([event(4), event(6, 'b', 'v2')], self.cfg, 20, views=self.views)
        self.assertEqual([g['count'] for g in groups], [1, 1])
        self.assertEqual([(g['start'], g['end']) for g in groups], [(0, 5), (5, 20)])

    def test_proven_delayed_event_returns_to_original_group(self):
        delayed = event(7, attribution={'delayed_verified': True, 'action_ts': 4.5}, last_seen=8)
        groups = extraction.group_events([event(4), event(6, 'b', 'v2'), delayed], self.cfg, 20, views=self.views)
        self.assertEqual([(g['actor'], g['count']) for g in groups], [('a', 2), ('b', 1)])
        self.assertEqual(groups[0]['event_ids'], ['4', '7'])
        self.assertEqual(groups[0]['end'], 8.5)

    def test_unproven_or_cross_phase_delayed_event_cannot_enter_original_group(self):
        with self.assertRaises(ValueError):
            extraction.group_events([event(7)], self.cfg, 20, views=self.views)
        delayed = event(7, attribution={'delayed_verified': True, 'action_ts': 4})
        with self.assertRaises(ValueError):
            extraction.group_events([event(3), delayed], self.cfg, 20,
                                    [{'start': 4.5, 'end': 5.5}], self.views)

    def test_padding_overlap_keeps_separate_event_groups(self):
        views = [{'id': 'v1', 'start': 0, 'end': 200, 'actor': 'a'}]
        groups = extraction.group_events([event(100), event(125), event(150)], self.cfg, 200, views=views)
        self.assertEqual(len(groups), 3)
        self.assertGreater(groups[0]['end'], groups[1]['start'])

    def test_confirmed_clock_jump_breaks_same_avatar_view(self):
        avatar = {'center': [.38, .04]}
        samples = [{'ts': t, 'avatar': avatar, 'count': 0, 'clock': clock}
                   for t, clock in [(0, 90), (1, 89), (2, 40), (3, 39)]]
        views, _ = perspective.build_views(samples, 0, 4)
        self.assertEqual([(v['start'], v['end']) for v in views], [(0, 1.5), (2, 3.5)])
        samples[-1]['clock'] = 87
        self.assertEqual(len(perspective.build_views(samples, 0, 4)[0]), 1)


class SkillDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory(prefix='so2_skill_assets_')
        cls.source = Path(cls.assets.name) / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=12',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '4',
                        '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(cls.source)],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='so2_skill_')
        self.root = Path(self.tmp.name)
        self.cfg = config.load_config(['--video', str(self.source), '--profile', 'pc',
                                      '--identity-mode', 'auto', '--output-dir', str(self.root),
                                      '--scan-segments', '1'])

    def tearDown(self):
        for handler in list(logging.getLogger().handlers):
            if isinstance(handler, logging.FileHandler) and Path(handler.baseFilename).is_relative_to(self.root):
                handler.close()
                logging.getLogger().removeHandler(handler)
        self.tmp.cleanup()

    def decision_data(self):
        cfg = media.prepare_cfg(self.cfg, str(self.source))
        doubt = dict(event(3), reason='delayed_attribution_unconfirmed')
        outcome = core.ScanResult(str(self.source), 'complete', events=[event(1)], review=[doubt],
                    identity=store.identity(self.source), coverage=[0, 4],
                    phase={'intervals': []}, perspective={'views': [
                    {'id': 'v1', 'start': 0, 'end': 2, 'actor': 'a', 'aliases': ['a']},
                    {'id': 'v2', 'start': 2, 'end': 4, 'actor': 'b'}]})
        data = {'videos': {}}
        store.record_scan(data, outcome, cfg)
        entry = data['videos'][store.media_id(self.source)]
        manifest_path = review.frame_bundle(self.source, [1, 2.5, 3.2], self.root / 'evidence')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        decisions = {'source_identity': entry['identity'], 'scan_fingerprint': entry['cfg_fingerprint'],
                     'evidence_manifest': str(manifest_path), 'decisions': [
                     {'event_id': '3', 'action': 'confirm', 'view_id': 'v1', 'killer': 'a', 'victim': 'enemy',
                      'reason': '先前动作及后续结果均已核对', 'delayed': True, 'action_ts': 1,
                      'evidence': [f['path'] for f in manifest['frames']]}]}
        target = self.root / 'decisions.json'
        store.atomic_json(target, decisions)
        return entry, cfg, target, manifest

    def test_review_updates_only_same_revision_and_replay_is_idempotent(self):
        entry, cfg, target, _ = self.decision_data()
        review.apply_decisions(entry, cfg, target)
        self.assertEqual(entry['hits'], [1, 3])
        self.assertEqual(entry['review'], [])
        self.assertTrue(entry['events'][-1]['attribution']['delayed_verified'])
        review.apply_decisions(entry, cfg, target)
        self.assertEqual(len(entry['review_history']), 1)

    def test_changed_evidence_or_source_never_promotes_doubt(self):
        entry, cfg, target, manifest = self.decision_data()
        before = copy.deepcopy(entry)
        Path(manifest['frames'][0]['path']).write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, '变化'):
            review.apply_decisions(entry, cfg, target)
        self.assertEqual(entry, before)
        data = json.loads(target.read_text(encoding='utf-8'))
        data['source_identity']['size'] += 1
        store.atomic_json(target, data)
        with self.assertRaisesRegex(ValueError, '修订'):
            review.apply_decisions(entry, cfg, target)
        self.assertEqual(entry, before)

    def test_visual_review_can_confirm_anonymous_kill_without_inventing_a_name(self):
        entry, cfg, target, _ = self.decision_data()
        view = entry['perspective']['views'][0]
        view.update(actor='视角人物_0123abcd', actor_named=False, aliases=[])
        entry['events'][0]['actor'] = view['actor']
        entry['review'][0].update(killer='', victim='', text='', signature='@visual|01234567', visual_role='unknown')
        data = json.loads(target.read_text(encoding='utf-8'))
        decision = data['decisions'][0]
        decision.pop('killer')
        decision.pop('victim')
        decision['visual_confirmed'] = True
        store.atomic_json(target, data)
        review.apply_decisions(entry, cfg, target)
        confirmed = entry['events'][-1]
        self.assertEqual(confirmed['actor'], '视角人物_0123abcd')
        self.assertEqual(confirmed['signature'], '@visual|01234567')
        self.assertFalse(confirmed['actor_named'])
        self.assertEqual(entry['perspective']['views'][0]['aliases'], [])

    def test_visual_confirmation_without_a_proven_view_is_rejected(self):
        entry, cfg, target, _ = self.decision_data()
        before = copy.deepcopy(entry)
        data = json.loads(target.read_text(encoding='utf-8'))
        data['decisions'][0]['visual_confirmed'] = True
        store.atomic_json(target, data)
        with self.assertRaisesRegex(ValueError, '视角归属'):
            review.apply_decisions(entry, cfg, target)
        self.assertEqual(entry, before)

    def test_aggregate_counter_mismatch_cannot_be_confirmed_as_one_kill(self):
        for reason in ('counter_feed_mismatch', 'counter_timing_unconfirmed', 'counter_change_unconfirmed'):
            for visual in (True, False):
                with self.subTest(reason=reason, visual=visual):
                    entry, cfg, target, _ = self.decision_data()
                    entry['perspective']['views'][0].update(actor='视角人物_0123abcd', actor_named=False)
                    entry['review'][0].update(reason=reason, count=2, visual_role='counter_unconfirmed')
                    data = json.loads(target.read_text(encoding='utf-8'))
                    data['decisions'][0]['visual_confirmed'] = visual
                    store.atomic_json(target, data)
                    with self.assertRaisesRegex(ValueError, '逐次记录'):
                        review.apply_decisions(entry, cfg, target)

    def test_manual_delivery_merge_and_second_run_reuse_actual_files(self):
        args = ['--video', str(self.source), '--output-dir', str(self.root), '--hits', '.5,1,3,3.5',
                '--pre', '.4', '--post', '.4', '--kill-gap', '1', '--merge', '--extract-workers', '1']
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kill_clipper.main(args + ['--run-id', 'first']), 0)
        first = json.loads((Path(self.cfg.state_dir) / 'runs/first/receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(first['run_id'], 'first')
        self.assertEqual([c['status'] for c in first['files'][0]['clips']], ['generated', 'generated'])
        merged = first['merge']
        self.assertEqual(merged['status'], 'generated', merged.get('error'))
        self.assertAlmostEqual(media.probe(merged['path'])['duration'], 2.6, delta=.15)
        self.assertTrue(media.probe(merged['path'])['has_audio'])
        timestamps = {c['path']: Path(c['path']).stat().st_mtime_ns for c in first['files'][0]['clips'] + [merged]}
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(kill_clipper.main(args + ['--run-id', 'second']), 0)
        second = json.loads((Path(self.cfg.state_dir) / 'runs/second/receipt.json').read_text(encoding='utf-8'))
        self.assertEqual(second['merge']['status'], 'reused')
        self.assertEqual([c['status'] for c in second['files'][0]['clips']], ['reused', 'reused'])
        self.assertEqual(timestamps, {p: Path(p).stat().st_mtime_ns for p in timestamps})

    def test_montage_normalizes_silent_different_size_and_trims_duplicate_context(self):
        silent = self.root / 'silent.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=red:size=160x120:rate=12',
                        '-t', '2', '-c:v', 'libx264', str(silent)], check=True, capture_output=True)
        clips = [{'status': 'generated', 'start': 0, 'end': 4, 'path': str(self.source), 'kill_count': 2},
                 {'status': 'generated', 'start': 2, 'end': 4, 'path': str(silent), 'kill_count': 2}]
        result = montage.merge_clips([{'clips': clips}], self.cfg)
        self.assertEqual(result.status, 'generated', result.error)
        # 第二组上下文完全被第一组覆盖，不重复播放。
        self.assertAlmostEqual(result.end, 4)
        result = montage.merge_clips([{'clips': clips[:1]}, {'clips': clips[1:]}], self.cfg)
        self.assertEqual(result.status, 'generated', result.error)
        self.assertAlmostEqual(media.probe(result.path)['duration'], 6, delta=.15)
        self.assertTrue(media.probe(result.path)['has_audio'])
        silent.unlink()
        result = montage.merge_clips([{'clips': clips[:1]}, {'clips': clips[1:]}], self.cfg)
        self.assertEqual(result.status, 'failed')
        self.assertIn('合并输入不可访问', result.error)


if __name__ == '__main__':
    unittest.main()
