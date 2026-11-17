"""自动身份的行为边界：不猜名字、不认旧播报、不混合不同视角。"""
import sys
import base64
import unittest
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import perspective


def avatar(x=.38):
    return {'center': [x, .12], 'score': .9, 'box': [x * 1920, 100, 55, 55]}


def event(ts, killer='甲Player', victim='敌人', highlight=True):
    return {'ts': ts, 'text': killer, 'killer': killer, 'victim': victim,
            'signature': perspective.normalize(killer) + '|' + perspective.normalize(victim),
            'confidence': .99, 'local_highlight': highlight}


def observations(end=4):
    return [{'ts': i / 3, 'avatar': avatar(), 'count': 0 if i < 3 else 1} for i in range(end * 3)]


class PerspectiveTests(unittest.TestCase):
    def test_prefix_names_are_not_aliases_without_visible_truncation(self):
        samples = [dict(s, count=0 if s['ts'] < 1 else 1 if s['ts'] < 3 else 2) for s in observations(5)]
        trusted, doubts, _ = perspective.attribute([event(1, 'Player'), event(3, 'PlayerOne')], [], samples, 0, 5)
        self.assertEqual(trusted, [])
        self.assertEqual(len(doubts), 2)
        trusted, doubts, _ = perspective.attribute([event(1, 'Player…'), event(3, 'PlayerOne')], [], samples, 0, 5)
        self.assertEqual(len(trusted), 2)
        self.assertEqual({e['actor'] for e in trusted}, {'playerone'})
        self.assertEqual(doubts, [])

    def test_scoreboard_reorder_requires_same_picture_portrait_and_independently_bound_actor(self):
        visual = base64.b64encode(bytes(range(128))).decode('ascii')
        samples = [{'ts': i/3, 'avatar': dict(avatar(.38 if i < 9 else .62),
                         appearance='abcdef0123456789', color=[100, 100, 100]), 'visual': visual,
                    'count': (0 if i < 3 else 1) if i < 9 else (2 if i < 12 else 3)} for i in range(18)]
        trusted, _, state = perspective.attribute([event(1), event(4, victim='另一人')], [], samples, 0, 6)
        self.assertEqual(len(state['views']), 1)
        self.assertEqual(trusted[0]['view_id'], trusted[1]['view_id'])
        self.assertTrue(state['views'][0]['hud_reorders'])
        trusted, _, state = perspective.attribute([event(1), event(4, '另一个人')], [], samples, 0, 6)
        self.assertEqual(len(state['views']), 2)
        self.assertNotEqual(trusted[0]['view_id'], trusted[1]['view_id'])
        samples[9]['visual'] = base64.b64encode(bytes([255])*128).decode('ascii')
        self.assertEqual(len(perspective.attribute([event(1), event(4, victim='另一人')], [], samples, 0, 6)[2]['views']), 2)

    def test_counter_reset_keeps_proven_identity_but_breaks_group(self):
        samples = [{'ts': i/3, 'avatar': avatar(),
                    'count': 0 if i < 3 or i >= 9 else 1} for i in range(21)]
        trusted, doubts, state = perspective.attribute([event(1), event(6, victim='第二人')], [], samples, 0, 7)
        self.assertEqual(len(trusted), 2)
        self.assertNotEqual(trusted[0]['view_id'], trusted[1]['view_id'])
        self.assertEqual(doubts, [])
        self.assertTrue(state['views'][1]['identity_from_view'])

    def test_late_clear_read_without_continuity_is_not_an_exact_timestamp(self):
        late = event(5)
        early = dict(event(2), reason='low_confidence')
        trusted, doubts = perspective.check_first_visible([late], [early], 8)
        self.assertEqual(trusted, [])
        self.assertEqual(len(doubts), 1)
        self.assertEqual(doubts[0]['ts'], 2)
        self.assertEqual(doubts[0]['first_clear_ts'], 5)

    def test_later_clear_read_keeps_first_visible_time_of_persistent_doubt(self):
        import config
        import core
        cfg = config.load_config(['--profile', 'pc'])
        doubts = []
        for ts in (1, 1.5, 2, 2.5, 3):
            doubts = core.merge_review(doubts + [dict(event(ts), reason='low_confidence')], [], cfg)
        events = []
        core.observe_hits(events, doubts, [(.99, '甲Player', 1500, '甲player|敌人', 20)], 3.5, cfg)
        self.assertEqual(events[0]['ts'], 1)
        self.assertEqual(core.merge_review(doubts, events, cfg), [])

    def test_existing_counter_is_not_a_new_kill(self):
        samples = [dict(s, count=1) for s in observations()]
        trusted, review, state = perspective.attribute([event(1)], [], samples, 0, 4)
        self.assertEqual(trusted, [])
        self.assertEqual(review[0]['reason'], 'unknown_view_identity')
        self.assertEqual(state['pulses'], [])

    def test_counter_increase_binds_only_highlighted_actor(self):
        trusted, review, _ = perspective.attribute([event(1), event(.8, '别人', highlight=False)],
                                                  [], observations(), 0, 4)
        self.assertEqual([e['killer'] for e in trusted], ['甲Player'])
        self.assertEqual(review, [])

    def test_known_view_excludes_another_killer_even_if_row_is_red(self):
        trusted, doubts, state = perspective.attribute([event(1), event(3, '别人', '甲Player')],
                                                       [], observations(), 0, 4)
        self.assertEqual(len(trusted), 1)
        self.assertEqual(doubts, [])
        self.assertEqual(state['other_player_events'], 1)

    def test_old_row_at_scan_start_cannot_seed_identity(self):
        trusted, review, _ = perspective.attribute([event(0)], [], observations(), 0, 4)
        self.assertEqual(trusted, [])
        self.assertTrue(review)

    def test_two_different_highlighted_actors_are_not_guessed(self):
        trusted, review, _ = perspective.attribute([event(1), event(1, '其他人')], [], observations(), 0, 4)
        self.assertEqual(trusted, [])
        self.assertEqual(len(review), 2)

    def test_transient_animation_does_not_reset_view_or_counter(self):
        samples = [{'ts': i / 3, 'avatar': avatar(), 'count': 1 if i < 3 else 0 if i < 5 else 2}
                   for i in range(12)]
        views, pulses = perspective.build_views(samples, 0, 4)
        self.assertEqual(len(views), 1)
        self.assertEqual(pulses[0]['count'], 1)

    def test_switch_has_a_separate_identity_and_old_row_stays_old(self):
        samples = [{'ts': i / 3, 'avatar': avatar(.38 if i < 9 else .62),
                    'count': (0 if i < 3 else 1) if i < 9 else (0 if i < 12 else 1)}
                   for i in range(18)]
        trusted, review, _ = perspective.attribute([event(1, 'A玩家'), event(4, 'B玩家')], [], samples, 0, 6)
        self.assertEqual(len(trusted), 2)
        self.assertNotEqual(trusted[0]['view_id'], trusted[1]['view_id'])
        self.assertEqual(review, [])

    def test_unconfirmed_delayed_kill_is_review(self):
        samples = [{'ts': i / 3, 'avatar': avatar(.38 if i < 9 else .62),
                    'count': 0 if i < 3 or i >= 9 else 1} for i in range(18)]
        trusted, review, _ = perspective.attribute([event(1, 'A玩家'), event(4, 'A玩家')], [], samples, 0, 6)
        self.assertEqual(len(trusted), 1)
        self.assertEqual(review[0]['reason'], 'delayed_attribution_unconfirmed')

    def test_unknown_avatar_cannot_become_a_trusted_kill(self):
        samples = [dict(s, avatar=None) for s in observations()]
        trusted, review, _ = perspective.attribute([event(1)], [], samples, 0, 4)
        self.assertEqual(trusted, [])
        self.assertTrue(review)

    def test_one_pixel_red_border_is_detected_but_red_background_is_not(self):
        roi = np.zeros((120, 480, 3), np.uint8)
        cv2.rectangle(roi, (30, 10), (470, 45), (30, 15, 180), 1)
        self.assertTrue(perspective.highlight_rects(roi))
        # 墙面大块红色不具有窄播报行的尺寸。
        roi[:] = (50, 30, 190)
        self.assertEqual(perspective.highlight_rects(roi), [])

    def test_health_bar_is_not_selected_avatar_and_white_wall_does_not_break_frame(self):
        image = np.zeros((238, 1000, 3), np.uint8)
        cv2.rectangle(image, (200, 100), (265, 104), (255, 255, 255), -1)
        rect = (460, 0, 1000, 238)
        self.assertIsNone(perspective.active_avatar(image, 1920, 1080, rect))
        cv2.rectangle(image, (300, 50), (355, 105), (255, 255, 255), 3)
        known = []
        self.assertIsNotNone(perspective.active_avatar(image, 1920, 1080, rect, known))
        cv2.rectangle(image, (300, 0), (355, 52), (255, 255, 255), -1)
        self.assertIsNotNone(perspective.active_avatar(image, 1920, 1080, rect, known))

    def test_name_text_cannot_establish_a_role_in_an_empty_red_box(self):
        cfg = SimpleNamespace(min_conf=.3)
        def box(x, text):
            return ([[x, 15], [x+100, 15], [x+100, 35], [x, 35]], text, .99)
        roi = np.zeros((100, 500, 3), np.uint8)
        cv2.rectangle(roi, (0, 5), (498, 45), (20, 10, 180), 1)
        hits, review = perspective.classify_feed([box(10, 'A玩家 + B玩家'), box(390, 'A玩家')], cfg, 0, 350, roi)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][5]['visual_role'], 'unknown')
        candidate = dict(event(1), **hits[0][5], visual_roles={'unknown': 2}, last_seen=2.)
        trusted, doubts, _ = perspective.attribute([candidate], [],
                                                   [dict(s, mode='personal') for s in observations()], 0, 4)
        self.assertEqual(trusted, [])
        self.assertEqual(doubts[0]['reason'], 'visual_role_unconfirmed')
        hits, review = perspective.classify_feed([box(10, 'A玩家 + B玩家'), box(390, 'C玩家')], cfg, 0, 350, roi)
        self.assertEqual(hits[0][5]['killer'], 'A玩家')
        self.assertEqual(review, [])


if __name__ == '__main__':
    unittest.main()
