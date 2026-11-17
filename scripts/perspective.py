"""从当前视角的独立反馈建立身份，再把播报事件归到连续视角段。"""
import hashlib
import base64
import re
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

import visual_feed

VERSION = 7
AVATAR_REGION = (.24, .0, .76, .085)
LIVE_AVATAR_REGION = (.24, .085, .76, .18)
FEEDBACK_REGION = (.42, .78, .61, .99)


def normalize(text):
    return ''.join(ch for ch in (text or '').lower() if ch.isalnum())


def white_mask(image):
    minimum = image.min(axis=2)
    spread = image.max(axis=2).astype(np.int16) - minimum
    return ((minimum > 180) & (spread < 55)).astype(np.uint8) * 255


def active_avatar(image, screen_width, screen_height, rect, known_boxes=None):
    """要求完整白色头像外框，排除头像下方普通水平血条。"""
    mask = white_mask(image)
    scale_x, scale_y = rect[2] / image.shape[1], rect[3] / image.shape[0]
    candidates = []
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [cv2.boundingRect(contour) for contour in contours]
    # 皇冠会让白框与上方图案连通。从白色横边重建矩形，再检查四边。
    horizontal = cv2.morphologyEx(mask, cv2.MORPH_OPEN,
                                  np.ones((1, max(3, round(screen_height * .027 / scale_x))), np.uint8))
    edges, _ = cv2.findContours(horizontal, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in edges:
        x, y, w, h = cv2.boundingRect(contour)
        if .027 < w * scale_x / screen_height < .09 and h * scale_y < screen_height * .008:
            for height in range(round(w * .85), round(w * 1.15) + 1, 2):
                boxes.append((x, y + h - height, w, height))
    # 白墙可能与白框连通。重检已发现框的四条边，而非把连通失败当作切人。
    for bx, by, bw, bh in known_boxes or []:
        boxes.append((round((bx - rect[0]) / scale_x), round((by - rect[1]) / scale_y),
                      round(bw / scale_x), round(bh / scale_y)))
    for x, y, w, h in boxes:
        if min(x, y) < 0 or x + w > image.shape[1] or y + h > image.shape[0]:
            continue
        width, height = w * scale_x, h * scale_y
        cx = (rect[0] + (x + w / 2) * scale_x) / screen_width
        cy = (rect[1] + (y + h / 2) * scale_y) / screen_height
        if not (.038 < height / screen_height < .075 and .031 < width / screen_height < .075
                and .65 < width / height < 1.5):
            continue
        if .47 < cx < .54 or not .24 < cx < .76:
            continue
        patch = mask[y:y+h, x:x+w] > 0
        pad = max(1, round(4 / scale_y))
        edges = [patch[:pad].mean(), patch[-pad:].mean(),
                 patch[:, :pad].mean(), patch[:, -pad:].mean()]
        if min(edges) < .38 or np.mean(edges) < .58:
            continue
        candidates.append({'center': [round(cx, 4), round(cy, 4)],
                           'score': round(float(np.mean(edges)), 3),
                           'box': [rect[0] + x * scale_x, rect[1] + y * scale_y, width, height]})
    candidates.sort(key=lambda item: (item['box'][2] * item['box'][3], item['score']), reverse=True)
    if candidates:
        # 字幕也可能形成白色方框；头像位于最上方的同一条 HUD 行。
        top = min(item['center'][1] for item in candidates)
        candidates = [item for item in candidates if item['center'][1] <= top + .025]
    unique = []
    for item in candidates:
        if not any(abs(item['center'][0] - old['center'][0]) < .012
                   and abs(item['center'][1] - old['center'][1]) < .025 for old in unique):
            unique.append(item)
    if len(unique) > 1:
        largest = sorted(unique, key=lambda item: item['box'][2] * item['box'][3], reverse=True)
        # 白底头像的内部也是白矩形；只有尺寸明显更大的外框才能消除歧义。
        if largest[0]['box'][2] > largest[1]['box'][2] * 1.1 and largest[0]['box'][3] > largest[1]['box'][3] * 1.1:
            unique = largest[:1]
    if len(unique) != 1:
        return None
    chosen = unique[0]
    bx, by, bw, bh = chosen['box']
    px, py = round((bx-rect[0])/scale_x), round((by-rect[1])/scale_y)
    pw, ph = round(bw/scale_x), round(bh/scale_y)
    portrait = image[py+ph//5:py+ph*4//5, px+pw//5:px+pw*4//5]
    if portrait.size:
        small = cv2.resize(portrait, (32, 32))
        dct = cv2.dct(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32))[:8, :8]
        bits = dct > np.median(dct[1:])
        chosen['appearance'] = np.packbits(bits).tobytes().hex()
        chosen['color'] = small.mean(axis=(0, 1)).round().astype(int).tolist()
    if known_boxes is not None and not any(abs(chosen['box'][0] - box[0]) < 5 for box in known_boxes):
        known_boxes.append(chosen['box'])
    return chosen


@lru_cache(maxsize=1)
def feedback_templates():
    result = []
    for path in sorted((Path(__file__).parent / 'assets').glob('feedback-*.png')):
        template = cv2.imdecode(np.fromfile(path, np.uint8), cv2.IMREAD_GRAYSCALE)
        if template is None:
            raise ValueError(f'击杀反馈模板不可读：{path.name}')
        for factor in (.75, .875, 1., 1.125, 1.25):
            scaled = cv2.resize(template, None, fx=factor, fy=factor, interpolation=cv2.INTER_NEAREST)
            result.append((path.stem, scaled))
    if not result:
        raise ValueError('缺少击杀反馈模板，不能执行自动视角判定')
    return result


def feedback_count(image, screen_height, rect):
    scale = 1080 / screen_height
    minimum = image.min(axis=2)
    spread = image.max(axis=2).astype(np.int16) - minimum
    # 旧版持久骷髅标记为灰白色，新版为纯白色。头像判定继续使用原阈值。
    found = []
    for threshold in (180, 130):
        feedback_mask = ((minimum > threshold) & (spread < 60)).astype(np.uint8) * 255
        mask = cv2.resize(feedback_mask, None, fx=scale, fy=scale, interpolation=cv2.INTER_NEAREST)
        for name, template in feedback_templates():
            h, w = template.shape
            if h > mask.shape[0] or w > mask.shape[1]:
                continue
            response = cv2.matchTemplate(mask, template, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(response >= .73)
            for y, x in zip(ys, xs):
                found.append((float(response[y, x]), x + w / 2, y + h / 2, w, h, name))
    selected = []
    for match in sorted(found, reverse=True):
        if not any(abs(match[1] - old[1]) < max(match[3], old[3]) * .65
                   and abs(match[2] - old[2]) < max(match[4], old[4]) * .65 for old in selected):
            selected.append(match)
    # 大的动画不作为持久计数。小标记只取最下方同一基线的一排。
    if not selected:
        return 0
    bottom = max(match[2] for match in selected)
    baseline = [match for match in selected if abs(match[2] - bottom) <= 12]
    return min(5, len(baseline))


def identity_rects(screen_width, screen_height, profile):
    """Return identity HUD crops in display pixels, preserving even rounding."""
    regions = {
        'avatar': LIVE_AVATAR_REGION if profile == 'live' else AVATAR_REGION,
        'feedback': (.42, .78, .61, .90) if profile == 'live' else FEEDBACK_REGION,
        'spectator': (.005, .38, .25, .95),
        'spectator_right': (.75, .38, .995, .95),
    }
    result = {}
    for name, region in regions.items():
        a, b, c, d = [int(v * size) // 2 * 2 for v, size in
                      zip(region, (screen_width, screen_height, screen_width, screen_height))]
        result[name] = (a, b, c - a, d - b)
    return result


def spectator_signal(context, screen_width, screen_height, rects, reader=None):
    import spectator
    observations = [spectator.detect(context[name], screen_width, screen_height, rects[name], reader)
                    for name in ('spectator', 'spectator_right') if name in context]
    observations = [o for o in observations if o is not None]
    selected = [o for o in observations if o.get('avatar')]
    if len(selected) == 1:
        return selected[0]
    if observations:
        return {'avatar': None, 'count': None, 'mode': 'spectator',
                'counter_evidence': {'reason': 'current_spectator_not_unique'}}
    return None


def signals(context, cfg, known_boxes=None, rects=None, reader=None):
    info = cfg.media_info
    rects = rects or getattr(cfg, 'identity_rects', None) or identity_rects(
        info['width'], info['height'], cfg.profile)
    if 'spectator' in context or 'spectator_right' in context:
        observed = spectator_signal(context, info['width'], info['height'], rects, reader)
        if observed is not None:
            return observed
    avatar = active_avatar(context['avatar'], info['width'], info['height'], rects['avatar'], known_boxes)
    visual = cv2.resize(cv2.cvtColor(context['feedback'], cv2.COLOR_BGR2GRAY), (16, 8))
    return {'avatar': avatar, 'count': feedback_count(context['feedback'], info['height'], rects['feedback']),
            'visual': base64.b64encode(visual.tobytes()).decode('ascii'), 'mode': 'personal'}


def highlight_rects(roi):
    return visual_feed.highlight_rects(roi)


def classify_feed(boxes, cfg, x1, max_x, roi):
    """像素行负责发现和角色；OCR 只附加名字，不决定这条行是否存在。"""
    visual_rows = visual_feed.analyze(roi)
    accepted, review = [], []
    for row in visual_rows:
        rx, ry, rw, rh = row['rect']
        items = [(box, text, float(confidence)) for box, text, confidence in boxes
                 if normalize(text) and ry - 3 <= (box[0][1] + box[2][1]) / 2 <= ry + rh + 3
                 and rx - 3 <= (box[0][0] + box[2][0]) / 2 <= rx + rw + 3]
        left = sorted((i for i in items if (i[0][0][0] + i[0][2][0]) / 2 + x1 < max_x),
                      key=lambda i: i[0][0][0])
        right = sorted((i for i in items if (i[0][0][0] + i[0][2][0]) / 2 + x1 >= max_x),
                       key=lambda i: i[0][0][0])
        killer = re.split(r'\s*\+\s*', left[0][1], maxsplit=1)[0].strip() if left else ''
        killer = re.sub(r'^\d+\s+', '', killer).strip()
        victim = right[-1][1].strip() if right else ''
        name_confidence = min([i[2] for i in left[:1] + right[-1:]]) if left and right else 0.
        extras = {'killer': killer, 'victim': victim, 'name_confidence': name_confidence,
                  'local_highlight': True, 'visual_role': row['role'],
                  'visual_signature': row['visual_signature'], 'visual_rect': row['rect'],
                  'visual_evidence': row['evidence']}
        accepted.append((1., killer, x1 + rx + rw / 2, '@visual|' + row['visual_signature'],
                         ry + rh / 2, extras))
    return accepted, review


def _same_avatar(a, b):
    return bool(a and b and abs(a['center'][0] - b['center'][0]) < .012
                and abs(a['center'][1] - b['center'][1]) < .025)


def _same_portrait(a, b):
    if not a or not b or not a.get('appearance') or not b.get('appearance'):
        return False
    distance = (int(a['appearance'], 16) ^ int(b['appearance'], 16)).bit_count()
    return distance <= 10 and max(abs(x-y) for x, y in zip(a['color'], b['color'])) <= 30


def _continuous_picture(a, b):
    if not a or not b or not a.get('visual') or not b.get('visual'):
        return False
    x = np.frombuffer(base64.b64decode(a['visual']), np.uint8).astype(float)
    y = np.frombuffer(base64.b64decode(b['visual']), np.uint8).astype(float)
    return x.shape == y.shape and 15 < x.mean() < 240 and np.abs(x-y).mean() < 25.5


def build_views(samples, start, end):
    views, pulses = [], []
    samples = sorted({s['ts']: s for s in samples}.values(), key=lambda s: s['ts'])
    cuts, previous, pending_cut = [], None, None
    for sample in samples:
        if 'clock' not in sample:
            continue
        if previous and abs(sample['clock'] - previous['clock'] + sample['ts'] - previous['ts']) > 4:
            if (pending_cut and sample['ts'] - pending_cut['ts'] <= 2.2
                    and abs(sample['clock'] - pending_cut['clock'] + sample['ts'] - pending_cut['ts']) <= 2):
                cuts.append(pending_cut['ts'])
                previous, pending_cut = sample, None
            else:
                pending_cut = sample
        else:
            previous, pending_cut = sample, None
    current, baseline, pending, streak, first_change = None, None, None, 0, None
    last_ts, last_sample, baseline_ts = None, None, None

    def keep_unconfirmed_counter():
        if (current and current.get('mode') and baseline is not None and pending is not None
                and pending > baseline and streak):
            old = current.get('pending_counter', {})
            earliest = min(first_change, old.get('ts', first_change))
            largest = max(pending, old.get('after', pending))
            current['pending_counter'] = {'ts': earliest, 'count': largest - baseline,
                                          'before': baseline, 'after': largest}

    for sample in samples:
        ts, avatar, count = sample['ts'], sample.get('avatar'), sample.get('count')
        if not avatar:
            if current and current.get('mode'):
                keep_unconfirmed_counter()
                current['end'] = min(current['end'], ts)
                current = None
            continue
        if (current is None or not _same_avatar(avatar, current['avatar']) or ts - last_ts > 1.1
                or sample.get('mode') != current.get('mode')
                or (sample.get('mode') == 'spectator' and avatar.get('appearance')
                    and current['avatar'].get('appearance') and not _same_portrait(avatar, current['avatar']))
                or any(last_ts < cut <= ts for cut in cuts)):
            keep_unconfirmed_counter()
            reorder_from = (current['id'] if current and last_sample and ts-last_ts <= .5
                            and not any(last_ts < cut <= ts for cut in cuts)
                            and _same_portrait(last_sample['avatar'], avatar)
                            and _continuous_picture(last_sample, sample)
                            and count is not None and last_sample.get('count') is not None
                            and count >= last_sample['count'] else None)
            current = {'start': ts, 'end': ts, 'avatar': avatar, 'actor': None,
                       'mode': sample.get('mode'),
                       'id': hashlib.sha256(f'{ts:.6f}:{avatar["center"]}'.encode()).hexdigest()[:16]}
            if reorder_from:
                current['hud_move_from'] = reorder_from
            views.append(current)
            baseline, pending, streak = (None, None, 0) if sample.get('mode') else (count, count, 0)
            baseline_ts = ts
        current['end'] = min(end, ts + .5)
        last_ts = ts
        last_sample = sample
        if count is None:
            keep_unconfirmed_counter()
            streak = 0
            if baseline is None:
                pending = None
            continue
        if baseline is None:
            pending, streak = (count, streak + 1) if pending == count else (count, 1)
            if streak >= 2:
                baseline, baseline_ts, streak = count, ts, 0
            continue
        if count == baseline:
            pending, baseline_ts, streak = count, ts, 0
            continue
        if count != pending:
            pending, streak, first_change = count, 1, ts
        else:
            streak += 1
        if streak < 2:
            continue
        if count < baseline and (count != 0 or ts - first_change < 1.5):
            # 新击杀动画会短暂盖住已有小标记。持续清零才是回合重置。
            continue
        if count > baseline:
            earlier = current.get('pending_counter')
            if earlier and earlier['before'] == baseline and earlier['after'] <= count:
                first_change = min(first_change, earlier['ts'])
                current.pop('pending_counter')
            pulses.append({'ts': first_change, 'count': count - baseline, 'view_id': current['id'],
                           'before': baseline, 'after': count,
                           **({'last_baseline_ts': baseline_ts, 'confirmed_ts': ts,
                               'timing_unconfirmed': first_change - baseline_ts > 1.1 or ts - first_change > 1.1}
                              if current.get('mode') else {}),
                           **({'counter_evidence': sample['counter_evidence']}
                              if sample.get('counter_evidence') else {})})
        elif count == 0:
            # 回合计数清零建立新段，即使相同头像仍在相同位置。
            current['end'] = first_change
            previous_id = current['id']
            current = dict(current, start=first_change, end=min(end, ts + .5), actor=None,
                           continuation_of=previous_id,
                           id=hashlib.sha256(f'{first_change:.6f}:reset'.encode()).hexdigest()[:16])
            current.pop('hud_move_from', None)
            views.append(current)
        baseline, baseline_ts, streak = count, ts, 0
    keep_unconfirmed_counter()
    for a, b in zip(views, views[1:]):
        a['end'] = min(a['end'], b['start'])
    return views, pulses


def canonical_views(views):
    """Merge a proven HUD reorder only when both sides independently agree on identity."""
    by_id = {v["id"]: v for v in views}
    canonical = {}
    for view in views:
        prior = by_id.get(view.get('hud_move_from'))
        original = canonical.get(prior['id']) if prior else None
        if (original and view.get('actor') and view['actor'] == original.get('actor')
                and not view.get('identity_conflict') and not original.get('identity_conflict')):
            view['merged_into'] = original['id']
            original['end'] = max(original['end'], view['end'])
            original.setdefault('hud_reorders', []).append({'ts': view['start'], 'observed_view_id': view['id'],
                        'evidence': 'same_portrait_continuous_picture_counter_and_independent_actor'})
            canonical[view['id']] = original
        else:
            canonical[view['id']] = view
    return canonical


def attribute(events, review, samples, start, end):
    views, pulses = build_views(samples, start, end)
    if any('mode' in sample for sample in samples) or any('visual_role' in event for event in events):
        from visual_attribution import attribute_visual
        gaps, missing = [], None
        for sample in sorted(samples, key=lambda s: s['ts']):
            if sample.get('mode') and not sample.get('avatar'):
                if missing is None:
                    missing = {'ts': sample['ts'], 'last_seen': sample['ts'],
                               'reason': 'view_tracking_lost', 'signature': '@viewgap|' + str(sample['ts'])}
                    gaps.append(missing)
                missing['last_seen'] = sample['ts']
            else:
                missing = None
        return attribute_visual(events, list(review) + gaps, views, pulses, start, end)
    # Compatibility for callers that still supply the older name-only events
    # and HUD samples. New scans always carry an explicit visual mode.
    by_id = {v['id']: v for v in views}
    def view_at(ts):
        return next((v for v in reversed(views) if v['start'] <= ts < v['end']), None)
    evidence = {}
    for pulse in pulses:
        view = by_id[pulse['view_id']]
        nearby = [e for e in events if e.get('local_highlight') and e['ts'] > start + .01
                  and abs(e['ts'] - pulse['ts']) <= 1.05
                  and view_at(e['ts']) is view]
        actors = {e['signature'].split('|', 1)[0] for e in nearby}
        if len(actors) != 1 or len(nearby) > pulse['count']:
            continue
        actor = next(iter(actors))
        truncated = any(re.search(r'(…|\.{2,}|。{2,})$', (e.get('killer') or e.get('text', '')).strip())
                        for e in nearby)
        if view['actor'] and view['actor'] != actor and not (
                (truncated or view.get('truncated_name')) and
                min(len(actor), len(view['actor'])) >= 3 and
                (actor.startswith(view['actor']) or view['actor'].startswith(actor)
                 or actor.endswith(view['actor']) or view['actor'].endswith(actor))):
            view['identity_conflict'] = True
            continue
        aliases = view.setdefault('aliases', [])
        view['truncated_name'] = bool(view.get('truncated_name') or truncated)
        if actor not in aliases:
            aliases.append(actor)
        view['actor'] = max(aliases, key=len)
        for event in nearby:
            evidence[id(event)] = {'counter_ts': pulse['ts'], 'counter_increment': pulse['count']}
    for view in views:
        previous = by_id.get(view.get('continuation_of'))
        if (previous and previous.get('actor') and not previous.get('identity_conflict')
                and not view.get('actor') and _same_avatar(previous['avatar'], view['avatar'])):
            # 计数清零断开多杀组；连续未切换的同一头像槽位不需要重新猜名字。
            view['actor'], view['aliases'] = previous['actor'], list(previous['aliases'])
            view['identity_from_view'] = previous.get('identity_from_view', previous['id'])
    canonical = canonical_views(views)
    trusted, doubts, others = [], list(review), []
    for event in events:
        view = view_at(event['ts'])
        actor = event['signature'].split('|', 1)[0]
        if view and not view.get('identity_conflict') and actor in view.get('aliases', []):
            owner = canonical[view['id']]
            item = dict(event, actor=view['actor'], view_id=owner['id'],
                        attribution={'method': 'avatar_and_counter_linked_feed',
                                     'identity_evidence': [p for p in pulses if canonical[p['view_id']]['id'] == owner['id']
                                                          or p['view_id'] == view.get('identity_from_view')],
                                     **({'identity_from_view': view['identity_from_view']}
                                        if view.get('identity_from_view') else {}),
                                     **evidence.get(id(event), {})})
            trusted.append(item)
        elif event.get('local_highlight'):
            previous = next((v for v in reversed(views) if v['end'] <= event['ts'] and v['actor'] == actor
                             and event['ts'] - v['end'] <= 10), None)
            if view and view.get('actor') and not view.get('identity_conflict') and previous is None:
                others.append(event)
            else:
                doubts.append(dict(event, reason='delayed_attribution_unconfirmed' if previous else 'unknown_view_identity',
                                   previous_view_id=canonical[previous['id']]['id'] if previous else None))
        else:
            others.append(event)
    return trusted, doubts, {'views': [v for v in views if not v.get('merged_into')],
                            'pulses': [dict(p, view_id=canonical[p['view_id']]['id']) for p in pulses],
                            'other_player_events': len(others)}


def check_first_visible(events, doubts, window):
    """存在更早同签名线索但缺少连续跟踪时，不把晚读清的时间当成精确击杀时间。"""
    trusted, pending = [], list(doubts)
    for event in events:
        earlier = [item for item in pending if item.get('reason') == 'low_confidence'
                   and item.get('signature') == event['signature']
                   and 1 < event['ts'] - item['ts'] <= window]
        if not earlier:
            trusted.append(event)
            continue
        first = min(earlier, key=lambda item: item['ts'])
        pending = [item for item in pending if item not in earlier]
        pending.append(dict(event, ts=first['ts'], reason='first_visible_unconfirmed',
                            first_clear_ts=event['ts'], earlier_observation=first))
    return trusted, pending
