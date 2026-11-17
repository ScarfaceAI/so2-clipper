"""中央热身提示的证据、区间与事件过滤；倒计时不用于推算比赛开始时间。"""
import re
import unicodedata

import cv2

import config


def classify_boxes(boxes, header_rect=None):
    """热身专用文字与有效倒计时须同行；购买、回合计时不会单独触发。"""
    rows, formal_conf = [], 0.0
    for bbox, text, confidence in sorted(boxes, key=lambda b: sum(p[1] for p in b[0]) / 4):
        if not text.strip():
            continue
        cy = sum(p[1] for p in bbox) / 4
        height = max(p[1] for p in bbox) - min(p[1] for p in bbox)
        cx = sum(p[0] for p in bbox) / 4
        normalized = re.sub(r'\s', '', unicodedata.normalize('NFKC', text))
        if (header_rect and confidence >= config.WARMUP_MIN_CONF
                and re.fullmatch(r'\d{1,2}:[0-5]\d', normalized)
                and header_rect[0] <= cx <= header_rect[2] and header_rect[1] <= cy <= header_rect[3]):
            formal_conf = max(formal_conf, float(confidence))
        row = next((r for r in rows if abs(r[0] - cy) <= max(6, height * .6)), None)
        if row is None:
            row = (cy, [])
            rows.append(row)
        row[1].append((min(p[0] for p in bbox), text, float(confidence)))
    uncertain, readable = None, []
    for _, row in rows:
        text = ''.join(b[1] for b in sorted(row))
        normalized = re.sub(r'[\s_\-]', '', unicodedata.normalize('NFKC', text)).casefold()
        confidence = min(b[2] for b in row)
        readable.append(text)
        is_warmup = '热身' in normalized or 'warmup' in normalized
        prestart = normalized.startswith('准备') or '距离比赛开始还有' in normalized
        if is_warmup or prestart:
            timer = re.search(r'(?<!\d)(\d{1,2}):([0-5]\d)(?!\d)', normalized)
            item = {'kind': 'uncertain', 'text': text, 'confidence': confidence}
            if timer and confidence >= config.WARMUP_MIN_CONF:
                item.update(kind='warmup', remaining=int(timer[1]) * 60 + int(timer[2]))
                return item
            uncertain = item
        elif '热' in normalized or '身' in normalized:
            uncertain = {'kind': 'uncertain', 'text': text, 'confidence': confidence}
        elif confidence >= config.WARMUP_MIN_CONF and '购买阶段' in normalized:
            formal_conf = max(formal_conf, confidence)
    if formal_conf:
        return {'kind': 'clear', 'text': ' / '.join(readable), 'confidence': formal_conf}
    return uncertain or {'kind': 'unknown', 'text': ' / '.join(readable), 'confidence': 0.0}


def read_phase(reader, roi, cfg):
    # 玩家 ID 的 ASCII allowlist 不适用于中文阶段提示。
    boxes = reader.readtext(cv2.cvtColor(roi, cv2.COLOR_BGR2RGB), paragraph=False,
                            decoder='greedy', allowlist=None)
    rect = config.WARMUP_TIMER_REGIONS.get(cfg.profile)
    header = None
    if rect:
        x1, y1, x2, y2 = config.WARMUP_REGION
        width, height = roi.shape[1] / (x2-x1), roi.shape[0] / (y2-y1)
        header = ((rect[0]-x1)*width, (rect[1]-y1)*height,
                  (rect[2]-x1)*width, (rect[3]-y1)*height)
    result = classify_boxes(boxes, header)
    if getattr(cfg, 'identity_mode', 'keywords') == 'auto' and header:
        for bbox, text, confidence in boxes:
            normalized = re.sub(r'\s', '', unicodedata.normalize('NFKC', text))
            cx, cy = sum(p[0] for p in bbox)/4, sum(p[1] for p in bbox)/4
            if (confidence >= .8 and re.fullmatch(r'\d{1,2}:[0-5]\d', normalized)
                    and header[0] <= cx <= header[2] and header[1] <= cy <= header[3]):
                minutes, seconds = map(int, normalized.split(':'))
                result['clock'] = minutes * 60 + seconds
                break
    return result


def intervals(samples, start, end):
    """两帧确认热身和正式阶段；菜单遮挡不代表热身结束。"""
    priority = {'unknown': 0, 'clear': 1, 'uncertain': 2, 'warmup': 3}
    unique = {}
    for sample in samples:
        key = round(sample['ts'], 6)
        old = unique.get(key)
        if old is None or priority[sample['kind']] > priority[old['kind']]:
            unique[key] = sample
    result, active, clears, previous = [], [], [], None
    context_start = start

    def finish(boundary, end_confirmed):
        positives = [s for s in active if s['kind'] == 'warmup']
        confirmed, first = False, 0
        for sample in positives:
            while sample['ts'] - positives[first]['ts'] > config.WARMUP_INTERVAL * 2.1:
                first += 1
            if sample['ts'] - positives[first]['ts'] >= .2:
                confirmed = True
                break
        visible = [s for s in active if s['kind'] != 'unknown']
        evidence = positives if positives else visible
        safe_end = min(end, boundary)
        if confirmed and not end_confirmed:
            safe_end = min(safe_end, clears[0]['ts'] if clears else visible[-1]['ts'] + config.WARMUP_INTERVAL * 1.1)
        result.append({'kind': 'warmup' if confirmed else 'uncertain',
                       'start': max(start, active[0]['ts']), 'end': safe_end,
                       'context_start': max(start, context_start),
                       'evidence': [evidence[0], evidence[-1]],
                       'end_confirmed': end_confirmed, 'end_evidence': clears[:2]})
        if safe_end < boundary:
            result.append({'kind': 'uncertain', 'start': safe_end, 'end': min(end, boundary),
                           'context_start': safe_end,
                           'evidence': [visible[-1]], 'end_confirmed': False, 'end_evidence': clears[:2]})

    for sample in sorted(unique.values(), key=lambda s: s['ts']):
        if sample['kind'] == 'unknown':
            if active:
                active.append(sample)
            clears = []
        elif sample['kind'] != 'clear':
            if not active:
                context_start = previous['ts'] if previous is not None else sample['ts']
            active.append(sample)
            clears = []
        elif active:
            clears.append(sample)
            if sample['ts'] - clears[0]['ts'] >= .2:
                finish(clears[0]['ts'], True)
                active, clears = [], []
        previous = sample
    if active:
        # 扫描窗口在热身中结束时，不靠剩余秒数外推未知画面。
        finish(end, False)
    return result


def filter_events(events, review, phase_intervals):
    trusted, pending, excluded, excluded_review = [], [], [], []
    for items, accepted, removed in ((events, trusted, excluded), (review, pending, excluded_review)):
        for event in items:
            interval = next((r for r in phase_intervals if r['start'] <= event['ts'] < r['end']), None)
            if interval is None:
                accepted.append(event)
            elif interval['kind'] == 'warmup':
                removed.append(dict(event, reason='warmup', phase_evidence=interval['evidence']))
            else:
                pending.append(dict(event, reason='warmup_uncertain', phase_evidence=interval['evidence']))
    return trusted, pending, excluded, excluded_review
