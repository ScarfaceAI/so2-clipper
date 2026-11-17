"""Attribute visual rows/counter changes without requiring a readable nickname."""
import re

from perspective import _same_avatar, canonical_views, normalize
import store


def _counter_doubt(view_id, counter, reason):
    token = store.digest([view_id, counter])[:16]
    return dict(counter, reason=reason, view_id=view_id, signature='@counterpending|' + token,
                ids=[token], visual_role='counter_unconfirmed', text='', killer='', victim='')


def _name(event):
    killer, victim = event.get('killer', ''), event.get('victim', '')
    if (event.get('name_confidence', 0) < .7 or not normalize(killer)
            or not normalize(victim) or normalize(killer) == normalize(victim)):
        return None
    if (event.get('name_observations', {}).get(normalize(killer), 0) < 2
            or len([n for n, count in event.get('name_observations', {}).items() if count >= 2]) != 1):
        return None
    return normalize(killer)


def _view_name(view, matches):
    """Names only bind when an independent counter associates the row to a view."""
    named = [(event, _name(event)) for event in matches if _name(event)]
    aliases = sorted({name for _, name in named})
    if not aliases:
        return
    longest = max(aliases, key=len)
    truncated = any(re.search(r'(…|\.{2,}|。{2,})$', e.get('killer', '').strip()) for e, _ in named)
    compatible = len(aliases) == 1 or truncated and all(
        len(a) >= 3 and (longest.startswith(a) or longest.endswith(a)) for a in aliases)
    view['name_candidates'] = aliases
    if compatible:
        view.update(actor=longest, aliases=aliases, actor_named=True)
    else:
        view['identity_conflict'] = True


def attribute_visual(events, review, views, pulses, start, end):
    """Bind visual rows to independently observed kills, without requiring a name."""
    by_id = {v['id']: v for v in views}

    def view_at(ts):
        return next((v for v in reversed(views) if v['start'] <= ts < v['end']), None)

    eligible, doubts, others = [], list(review), []
    for view in views:
        pending = view.get('pending_counter')
        if pending:
            doubts.append(_counter_doubt(view['id'], pending, 'counter_change_unconfirmed'))
    for event in events:
        role, view = event.get('visual_role'), view_at(event['ts'])
        if role in ('death', 'assist', 'self_death'):
            others.append(event)
        elif role == 'kill' and view and view.get('mode') != 'spectator':
            if event['ts'] <= start + .01 or event['ts'] <= view['start'] + .01:
                doubts.append(dict(event, reason='first_visible_unconfirmed'))
            elif (view['end'] - view['start'] < .3
                  or event.get('visual_roles', {}).get('kill', 0) < 2
                  or event.get('last_seen', event['ts']) - event['ts'] < .25):
                doubts.append(dict(event, reason='unknown_view_identity'))
            else:
                eligible.append(event)
        elif event.get('local_highlight'):
            doubts.append(dict(event, reason='visual_role_unconfirmed' if view else 'unknown_view_identity'))
        else:
            others.append(event)

    linked, names, used = {}, {}, set()
    for pulse in pulses:
        view = by_id[pulse['view_id']]
        if pulse.get('timing_unconfirmed'):
            doubts.append(_counter_doubt(view['id'], pulse, 'counter_timing_unconfirmed'))
            continue
        nearby = [e for e in eligible if id(e) not in used and view_at(e['ts']) is view
                  and abs(e['ts'] - pulse['ts']) <= 1.05]
        if view.get('mode') == 'spectator':
            # Feed highlighting describes the recording owner's participation,
            # not the selected spectator. Use that selected row's counter time;
            # a simultaneous third player's feed name cannot rename this view.
            nearby = []
        if nearby and len(nearby) == pulse['count']:
            for event in nearby:
                if event not in eligible:
                    eligible.append(event)
                linked[id(event)] = pulse
                used.add(id(event))
            names.setdefault(view['id'], []).extend(nearby)
            continue
        # A multi-kill jump does not reveal separate action times. Keep its
        # mismatch for source review instead of fabricating repeated events.
        if pulse['count'] != 1 or nearby or view.get('mode') != 'spectator':
            doubts.append(_counter_doubt(view['id'], pulse, 'counter_feed_mismatch'))
            continue
        blocked = any(e.get('visual_role') in ('death', 'assist', 'self_death')
                      and abs(e['ts'] - pulse['ts']) <= 1.05 for e in events)
        if blocked and view.get('mode') != 'spectator':
            doubts.append(_counter_doubt(view['id'], pulse, 'counter_feed_mismatch'))
            continue
        token = store.digest([view['id'], pulse['ts'], pulse['before'], pulse['after']])[:16]
        event = {'ts': pulse['ts'], 'last_seen': pulse['ts'], 'confidence': 1.,
                 'signature': '@counter|' + token, 'ids': [token], 'text': '',
                 'killer': '', 'victim': '', 'visual_role': 'counter', 'local_highlight': False}
        eligible.append(event)
        linked[id(event)] = pulse

    for view in views:
        view.update(actor='视角人物_' + view['id'][:8], actor_named=False, aliases=[])
        _view_name(view, names.get(view['id'], []))
        previous = by_id.get(view.get('continuation_of'))
        if previous and _same_avatar(previous['avatar'], view['avatar']) and not view.get('name_candidates'):
            view.update(actor=previous['actor'], actor_named=previous['actor_named'],
                        aliases=list(previous['aliases']),
                        identity_from_view=previous.get('identity_from_view', previous['id']))

    canonical = canonical_views(views)
    for pulse in pulses:
        pulse['view_id'] = canonical[pulse['view_id']]['id']
    for doubt in doubts:
        if doubt.get('view_id') in canonical:
            doubt['view_id'] = canonical[doubt['view_id']]['id']
    trusted = []
    for event in sorted(eligible, key=lambda e: e['ts']):
        view = view_at(event['ts'])
        pulse = linked.get(id(event))
        if pulse is None:
            doubts.append(dict(event, reason='visual_counter_unconfirmed', view_id=canonical[view['id']]['id']))
            continue
        attribution = {'method': 'visual_feed_and_current_view' if event.get('visual_role') == 'kill'
                       else 'counter_and_current_view', 'named_identity': view['actor_named']}
        if pulse:
            attribution['counter_evidence'] = pulse
        if event.get('visual_evidence'):
            attribution['visual_evidence'] = event['visual_evidence']
        trusted.append(dict(event, actor=view['actor'], actor_named=view['actor_named'],
                            view_id=canonical[view['id']]['id'], attribution=attribution))
    return trusted, doubts, {'views': [v for v in views if not v.get('merged_into')],
                            'pulses': pulses, 'other_player_events': len(others),
                            'excluded_feed_events': [e for e in others if e.get('visual_role')
                                                     in ('death', 'assist', 'self_death')]}
