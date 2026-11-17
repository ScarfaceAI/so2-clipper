"""Visual kills remain usable without names; named selection remains evidence bound."""
import sys
import base64
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'scripts'))
import config
import core
import perspective
import store


def avatar(x=.38):
    return {'center': [x, .12], 'score': .95, 'box': [x * 1920, 100, 55, 55]}


def samples(end=5, mode='personal'):
    return [{'ts': i / 3, 'avatar': avatar(), 'count': 0 if i < 3 else 1,
             'mode': mode} for i in range(end * 3)]


def visual_event(ts=1., role='kill', killer='', victim='', fingerprint='0123456789abcdef0123456789abcdef'):
    return {'ts': ts, 'last_seen': ts + 1., 'signature': '@visual|dhash128:' + fingerprint,
            'visual_signature': 'dhash128:' + fingerprint, 'visual_rect': [20, 10, 420, 40],
            'visual_role': role, 'visual_roles': {role: 3}, 'visual_evidence': {'role': role},
            'confidence': 1., 'local_highlight': True, 'text': killer,
            'killer': killer, 'victim': victim, 'name_confidence': .99 if killer and victim else 0.,
            'name_observations': {perspective.normalize(killer): 2} if killer and victim else {}}


class VisualAttributionTests(unittest.TestCase):
    def test_visual_version_invalidates_auto_scans_and_keeps_keyword_cache_compatible(self):
        auto = config.load_config(['--profile', 'pc', '--identity-mode', 'auto'])
        named = config.load_config(['--profile', 'pc', '--identity-mode', 'keywords', '--keywords', 'Player'])
        current = store.cfg_fingerprint(auto), store.cfg_fingerprint(named)
        with patch.object(perspective, 'VERSION', 6):
            self.assertNotEqual(current[0], store.cfg_fingerprint(auto))
            self.assertEqual(current[1], store.cfg_fingerprint(named))

    def test_unreadable_names_keep_visually_confirmed_kills(self):
        events = [visual_event(), visual_event(3., fingerprint='f' * 32)]
        observations = [dict(s, count=0 if s['ts'] < 1 else 1 if s['ts'] < 3 else 2) for s in samples()]
        trusted, doubts, state = perspective.attribute(events, [], observations, 0, 5)
        self.assertEqual(len(trusted), 2)
        self.assertEqual(doubts, [])
        self.assertTrue(trusted[0]['actor'].startswith('视角人物_'))
        self.assertEqual(trusted[0]['actor'], trusted[1]['actor'])
        self.assertFalse(state['views'][0]['actor_named'])

    def test_death_assist_self_and_ambiguous_roles_are_not_counted(self):
        for role in ('death', 'assist', 'self_death', 'unknown'):
            with self.subTest(role=role):
                trusted, doubts, _ = perspective.attribute([visual_event(role=role)], [], samples(), 0, 5)
                self.assertEqual(trusted, [])
                if role == 'unknown':
                    self.assertIn('visual_role_unconfirmed', {d['reason'] for d in doubts})
                self.assertIn('counter_feed_mismatch', {d['reason'] for d in doubts})

    def test_nickname_is_auxiliary_and_cannot_override_visual_role(self):
        good = visual_event(killer='Same', victim='Same')
        bad = visual_event(3, role='death', killer='Scarface', victim='Other')
        trusted, _, _ = perspective.attribute([good, bad], [], samples(), 0, 5)
        self.assertEqual(len(trusted), 1)
        self.assertFalse(trusted[0]['actor_named'])

    def test_name_requires_independent_counter_and_repeated_clear_readings(self):
        event = visual_event(killer='Scarface', victim='Other')
        trusted, _, _ = perspective.attribute([event], [], samples(), 0, 5)
        self.assertEqual(trusted[0]['actor'], 'scarface')
        no_increment = [dict(s, count=0) for s in samples()]
        trusted, doubts, _ = perspective.attribute([event], [], no_increment, 0, 5)
        self.assertEqual(trusted, [])
        self.assertEqual(doubts[0]['reason'], 'visual_counter_unconfirmed')
        once = dict(event, name_observations={'scarface': 1})
        self.assertFalse(perspective.attribute([once], [], samples(), 0, 5)[0][0]['actor_named'])

    def test_conflicting_names_preserve_anonymous_kills_without_claiming_aliases(self):
        observations = [dict(s, count=0 if s['ts'] < 1 else 1 if s['ts'] < 3 else 2) for s in samples()]
        events = [visual_event(killer='Player', victim='Enemy'),
                  visual_event(3, killer='PlayerOne', victim='Enemy2', fingerprint='f' * 32)]
        trusted, doubts, state = perspective.attribute(events, [], observations, 0, 5)
        self.assertEqual(len(trusted), 2)
        self.assertEqual(doubts, [])
        self.assertTrue(all(not e['actor_named'] for e in trusted))
        self.assertEqual(state['views'][0]['aliases'], [])

    def test_scan_start_old_row_and_untracked_avatar_are_not_new_kills(self):
        trusted, doubts, _ = perspective.attribute([visual_event(0)], [], samples(), 0, 5)
        self.assertEqual(trusted, [])
        self.assertIn('first_visible_unconfirmed', {d['reason'] for d in doubts})
        trusted, doubts, _ = perspective.attribute([visual_event()], [],
                                                   [dict(s, avatar=None) for s in samples()], 0, 5)
        self.assertEqual(trusted, [])
        self.assertTrue(doubts)

    def test_view_switch_and_round_reset_do_not_mix_groups(self):
        observations = samples(7)
        switched = [dict(s, avatar=avatar(.38 if s['ts'] < 3 else .62),
                         count=1 if 1 <= s['ts'] < 3 or s['ts'] >= 6 else 0) for s in observations]
        trusted, _, _ = perspective.attribute([visual_event(1), visual_event(6)], [], switched, 0, 7)
        self.assertNotEqual(trusted[0]['actor'], trusted[1]['actor'])
        reset = [dict(s, count=1 if 1 <= s['ts'] < 3 or s['ts'] >= 6 else 0) for s in observations]
        trusted, _, _ = perspective.attribute([visual_event(1), visual_event(6)], [], reset, 0, 7)
        self.assertEqual(trusted[0]['actor'], trusted[1]['actor'])
        self.assertNotEqual(trusted[0]['view_id'], trusted[1]['view_id'])

    def test_spectator_counter_proves_kill_without_a_name_or_red_border(self):
        trusted, doubts, state = perspective.attribute([], [], samples(mode='spectator'), 0, 5)
        self.assertEqual(len(trusted), 1)
        self.assertEqual(trusted[0]['ts'], 1)
        self.assertFalse(trusted[0]['actor_named'])
        self.assertEqual(trusted[0]['attribution']['counter_evidence']['after'], 1)
        self.assertEqual(doubts, [])
        self.assertEqual(len(state['pulses']), 1)

    def test_spectator_killer_is_separate_from_recording_owners_assist(self):
        local_assist = visual_event(role='assist', killer='Teammate', victim='Enemy')
        trusted, doubts, _ = perspective.attribute([local_assist], [], samples(mode='spectator'), 0, 5)
        self.assertEqual(len(trusted), 1)
        self.assertEqual(trusted[0]['visual_role'], 'counter')
        self.assertFalse(trusted[0]['actor_named'])
        self.assertEqual(doubts, [])

    def test_simultaneous_feed_name_cannot_rename_spectator_selected_by_eye(self):
        another = dict(visual_event(killer='Scarface', victim='Enemy'), visual_role='unhighlighted',
                       local_highlight=False, signature='scarface|enemy')
        trusted, _, state = perspective.attribute([another], [], samples(mode='spectator'), 0, 5)
        self.assertEqual(len(trusted), 1)
        self.assertFalse(trusted[0]['actor_named'])
        self.assertEqual(state['views'][0]['aliases'], [])

    def test_spectator_baseline_and_multi_kill_jump_do_not_invent_events(self):
        for observations in ([dict(s, count=1) for s in samples(mode='spectator')],
                             [dict(s, count=0 if s['ts'] < 1 else 2) for s in samples(mode='spectator')]):
            with self.subTest(count=observations[-1]['count']):
                self.assertEqual(perspective.attribute([], [], observations, 0, 5)[0], [])

        jumping = [dict(s, count=0 if s['ts'] < 1 else 2) for s in samples(mode='spectator')]
        _, doubts, _ = perspective.attribute([], [], jumping, 0, 5)
        self.assertEqual(doubts[0]['reason'], 'counter_feed_mismatch')
        self.assertEqual(doubts[0]['count'], 2)

    def test_initial_counter_ocr_correction_is_not_a_new_kill(self):
        for mode in ('personal', 'spectator'):
            observations = [dict(s, count=0 if s['ts'] == 0 else 1) for s in samples(mode=mode)]
            trusted, _, state = perspective.attribute([], [], observations, 0, 5)
            self.assertEqual(trusted, [])
            self.assertEqual(state['pulses'], [])

    def test_unreadable_counter_is_not_zero_and_requires_two_clear_reads(self):
        observations = samples(mode='spectator')
        observations[4]['count'] = None
        observations[3]['count'] = 0
        trusted, _, state = perspective.attribute([], [], observations, 0, 5)
        self.assertEqual(len(trusted), 1)
        self.assertEqual(state['pulses'][0]['ts'], 5 / 3)
        self.assertEqual(len(state['views']), 1)

    def test_personal_to_spectator_switch_rebases_existing_counter(self):
        observations = [dict(s, mode='personal' if s['ts'] < 2 else 'spectator',
                             count=0 if s['ts'] < 2 else 4) for s in samples()]
        trusted, _, state = perspective.attribute([], [], observations, 0, 5)
        self.assertEqual(trusted, [])
        self.assertEqual(len(state['views']), 2)

    def test_spectator_portrait_change_in_same_slot_starts_a_new_view(self):
        observations = [dict(s, avatar=dict(avatar(), appearance='0' * 16 if s['ts'] < 2 else 'f' * 16,
                                            color=[100, 100, 100]),
                             count=0 if s['ts'] < 2 else 4) for s in samples(mode='spectator')]
        trusted, _, state = perspective.attribute([], [], observations, 0, 5)
        self.assertEqual(trusted, [])
        self.assertEqual(len(state['views']), 2)

    def test_proven_hud_reorder_keeps_the_existing_visual_grouping_behavior(self):
        picture = base64.b64encode(bytes(range(128))).decode('ascii')
        observations = [{'ts': i / 3, 'mode': 'personal', 'visual': picture,
                         'avatar': dict(avatar(.38 if i < 9 else .62), appearance='abcdef0123456789',
                                        color=[100, 100, 100]),
                         'count': (0 if i < 3 else 1) if i < 9 else (2 if i < 12 else 3)} for i in range(18)]
        events = [visual_event(1, killer='Player', victim='Enemy1'),
                  visual_event(4, killer='Player', victim='Enemy2', fingerprint='f' * 32)]
        trusted, doubts, state = perspective.attribute(events, [], observations, 0, 6)
        self.assertEqual(doubts, [])
        self.assertEqual(len(state['views']), 1)
        self.assertEqual(trusted[0]['view_id'], trusted[1]['view_id'])
        self.assertTrue(state['views'][0]['hud_reorders'])
        events[1] = visual_event(4, killer='Different', victim='Enemy2', fingerprint='f' * 32)
        trusted, _, state = perspective.attribute(events, [], observations, 0, 6)
        self.assertEqual(len(state['views']), 2)
        self.assertNotEqual(trusted[0]['view_id'], trusted[1]['view_id'])

    def test_counter_change_just_before_switch_stays_in_review_instead_of_disappearing(self):
        observations = [dict(s, avatar=avatar(.38 if s['ts'] < 2 else .62),
                             count=0 if s['ts'] < 5 / 3 else 1 if s['ts'] < 2 else 4)
                        for s in samples(mode='spectator')]
        trusted, doubts, _ = perspective.attribute([], [], observations, 0, 5)
        self.assertEqual(trusted, [])
        self.assertEqual(len(doubts), 1)
        self.assertEqual(doubts[0]['ts'], 5 / 3)
        self.assertEqual(doubts[0]['reason'], 'counter_change_unconfirmed')

    def test_long_unreadable_counter_gap_cannot_become_a_precise_kill_at_recovery(self):
        observations = [{'ts': i / 3, 'avatar': avatar(), 'mode': 'spectator',
                         'count': 0 if i < 3 else None if i < 90 else 1} for i in range(94)]
        trusted, doubts, _ = perspective.attribute([], [], observations, 0, 32)
        self.assertEqual(trusted, [])
        self.assertEqual(doubts[0]['reason'], 'counter_timing_unconfirmed')
        self.assertEqual(doubts[0]['last_baseline_ts'], 2 / 3)

    def test_avatar_loss_breaks_the_view_even_when_same_portrait_returns(self):
        observations = [dict(s, avatar=None if s['ts'] == 1 else avatar())
                        for s in samples(mode='spectator')]
        trusted, doubts, state = perspective.attribute([], [], observations, 0, 5)
        self.assertEqual(trusted, [])
        self.assertEqual(len(state['views']), 2)
        self.assertIn('view_tracking_lost', {d['reason'] for d in doubts})

    def test_growth_then_unreadable_then_switch_keeps_the_unconfirmed_growth(self):
        observations = [dict(s, avatar=avatar(.38 if s['ts'] < 2 else .62),
                             count=0 if s['ts'] < 1 else 1 if s['ts'] == 1 else None if s['ts'] < 2 else 4)
                        for s in samples(mode='spectator')]
        trusted, doubts, _ = perspective.attribute([], [], observations, 0, 5)
        self.assertEqual(trusted, [])
        self.assertEqual(doubts[0]['reason'], 'counter_change_unconfirmed')
        self.assertEqual(doubts[0]['ts'], 1.)


class VisualRowTrackingTests(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load_config(['--identity-mode', 'auto', '--profile', 'pc'])

    def hit(self, role='kill', fingerprint='0123456789abcdef0123456789abcdef', y=30):
        e = visual_event(role=role, fingerprint=fingerprint)
        extras = {k: e[k] for k in ('killer', 'victim', 'name_confidence', 'local_highlight',
                                    'visual_role', 'visual_signature', 'visual_rect', 'visual_evidence')}
        return (1., '', 1500, e['signature'], y, extras)

    def test_persistent_unreadable_row_scrolling_is_one_event(self):
        events, doubts = [], []
        for ts, y in ((1., 30), (4 / 3, 30), (5 / 3, 70), (2., 70)):
            core.observe_hits(events, doubts, [self.hit(y=y)], ts, self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['ts'], 1.)
        self.assertEqual(events[0]['visual_roles']['kill'], 4)
        self.assertEqual(events[0]['last_seen'], 2.)

    def test_two_unreadable_rows_and_later_reappearance_stay_separate(self):
        events, doubts = [], []
        hits = [self.hit(fingerprint='0' * 32), self.hit(fingerprint='f' * 32, y=70)]
        core.observe_hits(events, doubts, hits, 1., self.cfg)
        core.observe_hits(events, doubts, hits, 4 / 3, self.cfg)
        core.observe_hits(events, doubts, [], 5 / 3, self.cfg)
        core.observe_hits(events, doubts, [], 2., self.cfg)
        core.observe_hits(events, doubts, [self.hit(fingerprint='0' * 32)], 7 / 3, self.cfg)
        self.assertEqual(len(core.merge_events(events, self.cfg.cluster_window)), 3)

    def test_conflicting_visual_roles_are_retained_for_review(self):
        events, doubts = [], []
        core.observe_hits(events, doubts, [self.hit()], 1., self.cfg)
        core.observe_hits(events, doubts, [self.hit('assist')], 4 / 3, self.cfg)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['visual_role'], 'unknown')
        self.assertEqual(events[0]['visual_role_conflict'], ['assist', 'kill'])


if __name__ == '__main__':
    unittest.main()
