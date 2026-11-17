"""击杀判定与共享扫描流程。解码、结果存储和片段提取各有独立职责。"""
import logging
import math
import os
import re
import time
import difflib
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import cv2
import config
import media
import store
import warmup
import perspective
import autolayout
from store import cfg_fingerprint, load_results, save_results
# 仅保留已有 Python 入口兼容。当前编排直接依赖 extraction / autolayout。
from extraction import extract_clips, _clip_ok, extract_segment as _ffmpeg_extract, merge_overlapping

log = logging.getLogger('so2clipper')
DIFF_THRESHOLD = 25
_READERS = {}


@dataclass
class ScanResult:
    path: str
    status: str
    hits: list = field(default_factory=list)
    events: list = field(default_factory=list)
    review: list = field(default_factory=list)
    error: str | None = None
    stats: dict = field(default_factory=dict)
    identity: dict = field(default_factory=dict)
    coverage: list = field(default_factory=list)
    origin: str = 'ocr'
    phase: dict = field(default_factory=dict)
    perspective: dict = field(default_factory=dict)


def build_reader(cfg):
    key = (tuple(cfg.languages), cfg.gpu)
    if key not in _READERS:
        import easyocr
        import sys
        torch = sys.modules.get('torch')
        if not cfg.gpu and torch is not None:
            torch.set_num_threads(min(4, os.cpu_count() or 1))
        _READERS[key] = easyocr.Reader(cfg.languages, gpu=cfg.gpu,
                                      download_enabled=os.environ.get('SO2_CLIPS_OFFLINE') != '1',
                                      model_storage_directory=os.environ.get('SO2_CLIPS_MODEL_DIR'))
    return _READERS[key]


def read_roi(reader, roi_bgr, cfg):
    return reader.readtext(cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2RGB), paragraph=False,
                           decoder='greedy', allowlist=cfg.allowlist)


compute_region = media.compute_region
def prepare_cfg(cfg, path):
    """旧 Python 入口；来源解析的维护位置是 autolayout.resolve_source。"""
    return autolayout.resolve_source(cfg, path)


def _probe_duration(path):
    return media.probe(path)['duration']


def _probe_video(path):
    info = media.probe(path)
    return info['width'], info['height']


def _probe_fps(path):
    return media.probe(path)['fps']


def _use_ffmpeg_engine(cfg, video_path):
    engine = getattr(cfg, 'effective_engine', cfg.scan_engine)
    return engine == 'ffmpeg' or (engine == 'auto' and _probe_fps(video_path) >= 90)


def discover_videos(cfg):
    root = Path(cfg.video_dir).resolve()
    out = Path(cfg.output_dir).resolve()
    state = Path(cfg.state_dir).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f'素材目录不存在：{root}')
    videos = []
    for parent, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ('.trash', '__pycache__', 'output')
                   and not any((Path(parent) / d).resolve().is_relative_to(p) for p in (out, state))]
        for name in files:
            path = Path(parent) / name
            if path.suffix.lower() in {'.mp4', '.mkv', '.mov', '.ts', '.m2ts', '.flv', '.webm'}:
                if not any(path.resolve().is_relative_to(p) for p in (out, state)):
                    videos.append(str(path.resolve()))
    return sorted(set(videos))


def needs_scan(cfg, results, videos, force=False):
    todo = []
    for path in videos:
        try:
            resolved = autolayout.resolve_source(cfg, path)
            valid = store.cache_valid(results.get('videos', {}).get(store.source_path_id(path)), path, resolved)
        except (OSError, ValueError):
            valid = False
        if force or not valid:
            todo.append(path)
    return todo


def frame_timestamp(cap, frame_idx, fps):
    msec = cap.get(cv2.CAP_PROP_POS_MSEC)
    return msec / 1000 if msec and msec > 0 else frame_idx / fps


def _name_like(text):
    """文本里是否含"玩家名字"特征：连续≥3字母 / 全大写2字母短名(BP/GP) / 连续中文"""
    return bool(re.search(r'[A-Za-z]{3,}', text)
                or re.search(r'[A-Z]{2}', text)
                or re.search(r'[\u4e00-\u9fff]{2,}', text))


def _is_kill_side(text, kw):
    """播报方向判断（框内文字）：关键词左侧出现其他名字 → 非击杀（被击杀或助攻），否则为击杀

    击杀:     "PlayerOne t Enemy" / "4 PlayerOne" / " r t PlayerOne"（左为分隔/计数/残字）
              "PlayerOne + Ally t Enemy"（PlayerOne 为击杀者，Ally 为助攻者）
    被击杀:   "Enemy t PlayerOne" / "小明 t PlayerOne"（左为击杀者名字）
    助攻:     "Ally + PlayerOne t Enemy" / "BP + PlayerOne"（本人在助攻位置）
    名字特征：连续≥3字母 / 全大写2字母短名(BP/GP) / 连续中文；残字(单小写字母)不算。
    兼容 PC/手机两版播报布局（手机版播报条整体偏右，X坐标无法区分方向）。
    跨框的情况（助攻者被 OCR 拆成独立框，如 "PlayerTwo +" | "PlayerOne"）见 _match_hits。
    """
    idx = text.casefold().find(kw.casefold())
    if idx < 0:
        return False
    left = text[:idx]
    if _name_like(left):
        return False
    return True


def _norm_text(s):
    """播报文本归一化（去空格/符号/大小写），用于签名比较"""
    return ''.join(ch for ch in (s or '').lower() if ch.isalnum())


def _row_signature(boxes, text, bbox, x1, max_x_center):
    """播报行签名：击杀者文本 + 同一行(y带)内最靠右的文本(被杀者)

    同一行重复读到 → 签名相同；新的击杀（换了被杀者）→ 签名不同。
    被杀者名字读不到时退化成"杀者文本|"，比较时按保守规则处理（见 _same_row）。
    """
    cy = (bbox[0][1] + bbox[2][1]) / 2
    victim, best_x = '', None
    for b in boxes:
        bb = b[0]
        bcy = (bb[0][1] + bb[2][1]) / 2
        if abs(bcy - cy) > 25:      # 不在同一行
            continue
        bcx = (bb[0][0] + bb[2][0]) / 2 + x1
        if bcx >= max_x_center and (best_x is None or bcx > best_x):
            best_x, victim = bcx, b[1]
    return f"{_norm_text(text)}|{_norm_text(victim)}"


SIG_NEAR_DUP = 2.0      # 谁都没读到被杀者时，多久之内视为同一条播报（秒）
NAME_SIM = 0.4          # 短名(≤5字符，含中文名/被截断的名字)相似度阈值：容忍 OCR 变体
NAME_SIM_LONG = 0.7     # 长名(≥6字符)阈值要高：iamsupertr vs ahppewtap 用 0.4 会互相误判
NAME_SIM_SHORT_MAXLEN = 5


def _similar(a, b, thr=None):
    """OCR 读出的两个名字是否算同一个
    - 相等 / 前缀关系（'iamsupertr' vs 'iamsupertr.'、'ei' vs 'ei2'）→ 同一个
    - 其余按长度分级：短名(≤5)容忍 0.4；长名(≥6)要求 0.7
      实测：9-10 字符的不同 ID（iamsupertr / ahppewtap）用 0.4 会判成同一个名字
      → 新的击杀被当成"同一条播报"去重掉，整段漏检。
    """
    if a == b:
        return True
    if not a or not b:
        return False
    if a.startswith(b) or b.startswith(a):   # "scarface" vs "scarfacegppppp"
        return True
    if thr is None:
        thr = NAME_SIM if max(len(a), len(b)) <= NAME_SIM_SHORT_MAXLEN else NAME_SIM_LONG
    return difflib.SequenceMatcher(None, a, b).ratio() >= thr


def _same_row(sig1, sig2, dt=None):
    """两个签名是否属于同一条播报行（dt=两次识别的时间差，秒）

    - 杀者文本差太多 → 不是同一行
    - 两边都读到被杀者：名字相似即同一行（新击杀必然换了人）
    - 有一边没读到被杀者：杀者文本相同且时间很近（≤SIG_NEAR_DUP）才算同一行
    """
    k1, v1 = sig1.split('|', 1)
    k2, v2 = sig2.split('|', 1)
    if not _similar(k1, k2):
        return False
    if v1 and v2:
        return _similar(v1, v2)
    if dt is None:
        return True
    return dt <= SIG_NEAR_DUP


def _classify_boxes(boxes, cfg, x1, max_x):
    accepted, review = [], []
    for bbox, text, conf in boxes:
        conf = float(conf)
        kw = next((k for k in cfg.keywords if k.casefold() in text.casefold()), None)
        possible_name = False
        if kw is None:
            possible_name = any(_similar(_norm_text(part), _norm_text(k), thr=.85)
                                for part in re.findall(r'\w+', text) for k in cfg.keywords)
            if not possible_name:
                continue
        cx = float((bbox[0][0] + bbox[2][0]) / 2 + x1)
        cy = float((bbox[0][1] + bbox[2][1]) / 2)
        if (kw is not None and not _is_kill_side(text, kw)) or cx >= max_x:
            continue
        left = [(t, float(c)) for b, t, c in boxes if b is not bbox
                and abs((b[0][1] + b[2][1]) / 2 - cy) <= 25
                and (b[0][0] + b[2][0]) / 2 < bbox[0][0] and _name_like(t)]
        if any(c >= cfg.min_conf for _, c in left):
            continue  # 明确的被击杀/助攻，不制造复核噪声。
        sig = _row_signature(boxes, text, bbox, x1, max_x)
        item = {'confidence': conf, 'text': text, 'x': cx, 'row_y': cy, 'signature': sig}
        name_start = text.casefold().find(kw.casefold()) if kw is not None else 0
        name_end = name_start + len(kw) if kw is not None else len(text)
        attached_name = ((name_start > 0 and text[name_start - 1].isalnum())
                         or (name_end < len(text) and text[name_end].isalnum()))
        reason = ('possible_name' if possible_name else
                  'ambiguous_name' if attached_name else
                  'low_confidence' if conf < cfg.min_conf else
                  'direction_conflict' if left else
                  'missing_victim' if not sig.split('|', 1)[1] else None)
        if reason:
            review.append(dict(item, reason=reason))
        else:
            accepted.append((conf, text, cx, sig, cy))
    accepted.sort(key=lambda h: h[2])
    return accepted, review


def _match_hits(roi, reader, cfg, x1, max_x_center):
    return _classify_boxes(read_roi(reader, roi, cfg), cfg, x1, max_x_center)[0]


def _match_hit(roi, reader, cfg, x1, max_x_center):
    hits = _match_hits(roi, reader, cfg, x1, max_x_center)
    return hits[0][:4] if hits else None


def _accept_hit(raw_hits, sig_records, ts, hit, cfg):
    conf, text, cx, sig = hit[:4]
    for s, t in sig_records:
        if 0 <= ts - t <= cfg.sig_window and _same_row(sig, s, ts - t):
            return False
    sig_records.append((sig, ts))
    sig_records[:] = [(s, t) for s, t in sig_records if ts - t <= cfg.sig_window]
    raw_hits.append((ts, conf, sig))
    return True


def _accept_any(raw_hits, sig_records, ts, hits, cfg):
    # 兼容显式的单事件调用；扫描器处理本帧全部候选。
    for hit in hits:
        if _accept_hit(raw_hits, sig_records, ts, hit, cfg):
            return True, hit
    return False, None


def _same_event(a, b, window):
    if (set(a.get('ids', [])) & set(b.get('distinct_from', []))
            or set(b.get('ids', [])) & set(a.get('distinct_from', []))):
        return False
    if 'last_seen' in a or 'last_seen' in b:
        # 播报消失后同一玩家可以再次被杀；不能仅按名字和首次命中的 8 秒去重。
        gap = max(0.0, a['ts'] - b.get('last_seen', b['ts']), b['ts'] - a.get('last_seen', a['ts']))
        limit = min(window, 1.0)
    else:
        gap, limit = abs(a['ts'] - b['ts']), window
    if a.get('visual_signature') and b.get('visual_signature'):
        if gap > limit:
            return False
        aw, bw = a['visual_rect'][2], b['visual_rect'][2]
        if abs(aw - bw) > max(6, max(aw, bw) * .035):
            return False
        return any((int(sa.split(':')[-1], 16) ^ int(sb.split(':')[-1], 16)).bit_count() <= 8
                   for sa in a.get('visual_signatures', [a['visual_signature']])
                   for sb in b.get('visual_signatures', [b['visual_signature']]))
    return gap <= limit and any(_same_row(sa, sb, gap)
                                for sa in a.get('variants', {a['signature']: 1})
                                for sb in b.get('variants', {b['signature']: 1}))


def _merge_visual(old, event, *, observation=False):
    if 'visual_role' not in event:
        return
    readings = old.setdefault('name_observations', {})
    for name, count in event.get('name_observations', {}).items():
        readings[name] = readings.get(name, 0) + count if observation else max(readings.get(name, 0), count)
    if event.get('name_confidence', 0) > old.get('name_confidence', 0):
        for key in ('killer', 'victim', 'name_confidence', 'text'):
            old[key] = event[key]
    if not event.get('visual_signature'):
        return
    signatures = old.setdefault('visual_signatures', [old['visual_signature']])
    for signature in event.get('visual_signatures', [event['visual_signature']]):
        if signature not in signatures:
            signatures.append(signature)
    roles = old.setdefault('visual_roles', {old['visual_role']: 1})
    for role, count in event.get('visual_roles', {event['visual_role']: 1}).items():
        roles[role] = roles.get(role, 0) + count if observation else max(roles.get(role, 0), count)
    known = set(roles) - {'unknown'}
    old['visual_role'] = next(iter(known)) if len(known) == 1 else 'unknown'
    if len(known) > 1:
        old['visual_role_conflict'] = sorted(known)


def merge_events(events, window):
    result = []
    for event in sorted(events, key=lambda e: e['ts']):
        old = next((e for e in reversed(result) if _same_event(e, event, window)), None)
        if old is None:
            result.append(dict(event))
        else:
            old['confidence'] = max(old['confidence'], event['confidence'])
            _merge_visual(old, event)
            if event.get('local_highlight'):
                old['local_highlight'] = True
            variants = old.setdefault('variants', {old['signature']: 1})
            for signature, count in event.get('variants', {event['signature']: 1}).items():
                variants[signature] = max(variants.get(signature, 0), count)
            old['signature'] = max(variants, key=variants.get)
            old['ids'] = list(set(old.get('ids', [])) | set(event.get('ids', [])))
            old['distinct_from'] = list(set(old.get('distinct_from', [])) | set(event.get('distinct_from', [])))
            if ('last_seen' in event or 'last_seen' in old) and event.get('last_seen', event['ts']) > old.get('last_seen', old['ts']):
                old['last_seen'] = event.get('last_seen', event['ts'])
                old['row_y'] = event.get('row_y')
    return result


def merge_review(review, events, cfg):
    unique = []
    for item in sorted(review, key=lambda e: e['ts']):
        if item['reason'] == 'unstable_row':
            if any(e.get('variants', {}).get(item['signature'], 0) >= 2
                   and _same_event(item, e, cfg.sig_window) for e in events):
                continue
        elif any(_same_event(item, event, cfg.sig_window) for event in events):
            continue
        duplicate = next((old for old in reversed(unique) if item['reason'] == old['reason']
                          and _same_event(item, old, cfg.sig_window)), None)
        if duplicate:
            duplicate['last_seen'] = max(item.get('last_seen', item['ts']),
                                         duplicate.get('last_seen', duplicate['ts']))
        else:
            unique.append(item)
    return unique


def filter_same_player_events(events, cfg):
    """同名杀者与受害者可能是自杀或同名玩家，先复核，不凑入多杀。"""
    trusted, review = [], []
    for event in events:
        keyword = next((k for k in cfg.keywords if k.casefold() in event['text'].casefold()), None)
        victim = event['signature'].split('|', 1)[1]
        if keyword is not None and victim == _norm_text(keyword):
            review.append(dict(event, reason='same_player_name'))
        else:
            trusted.append(event)
    return trusted, review


def observe_hits(events, review, hits, ts, cfg):
    """先跟踪已有行的位置，再判断新行。淡出造成的名字突变进入疑点。"""
    pending, visible, assigned = [], [], set()
    for hit in hits:
        conf, text, cx, sig = hit[:4]
        cy = hit[4] if len(hit) > 4 else None
        event = {'ts': float(ts), 'confidence': conf, 'text': text, 'x': cx,
                 'signature': sig, 'row_y': cy, 'last_seen': float(ts)}
        if len(hit) > 5:
            event.update(hit[5])
        if event.get('visual_signature'):
            event['visual_roles'] = {event['visual_role']: 1}
        if 'visual_role' in event:
            event['name_observations'] = ({perspective.normalize(event['killer']): 1}
                                          if event.get('name_confidence', 0) >= .7 and event.get('victim')
                                          and perspective.normalize(event.get('killer')) else {})
        old = next((e for e in reversed(events) if id(e) not in assigned
                    and (e.get('active', True) or e['ts'] <= ts <= e.get('last_seen', e['ts']))
                    and _same_event(e, event, cfg.sig_window)), None)
        if old is not None:
            old.update(last_seen=float(ts), row_y=cy, confidence=max(old['confidence'], conf))
            _merge_visual(old, event, observation=True)
            if event.get('local_highlight'):
                old['local_highlight'] = True
            variants = old.setdefault('variants', {old['signature']: 1})
            variants[sig] = variants.get(sig, 0) + 1
            old['signature'] = max(variants, key=variants.get)
            visible.append(old)
            assigned.add(id(old))
        else:
            pending.append(event)
    for event in pending:
        occupied = None if event.get('visual_signature') else next((old for old in reversed(events)
                         if event['row_y'] is not None and old.get('row_y') is not None
                         and old.get('active', True)
                         and abs(event['row_y'] - old['row_y']) < 12
                         and 0 <= ts - old.get('last_seen', old['ts']) <= 1.0
                         and _similar(event['signature'].split('|')[0], old['signature'].split('|')[0])), None)
        if occupied is None and event['row_y'] is not None and not event.get('visual_signature'):
            # 新的其他玩家播报会把旧行下推；OCR 附带残字时不凭名字突变多计一杀。
            killer, victim = event['signature'].split('|', 1)
            occupied = next((old for old in reversed(events)
                             if id(old) not in assigned and old.get('row_y') is not None and old.get('active', True)
                             and 20 <= event['row_y'] - old['row_y'] <= 100
                             and 0 <= ts - old.get('last_seen', old['ts']) <= 1
                             and _similar(killer, old['signature'].split('|')[0])
                             and _similar(victim, old['signature'].split('|')[1], thr=.4)), None)
        if occupied:
            variants = occupied.setdefault('variants', {occupied['signature']: 1})
            variants[event['signature']] = variants.get(event['signature'], 0) + 1
            occupied.update(last_seen=float(ts), row_y=event['row_y'])
            review.append(dict(event, reason='unstable_row'))
            visible.append(occupied)
            continue
        prior = [r['ts'] for r in review if _same_event(r, event, cfg.sig_window)]
        if prior:
            event['ts'] = min(prior + [event['ts']])
        event['ids'] = [store.digest([event['signature'], ts, event['row_y']])[:16]]
        # 同屏两条相同玩家播报是两次击杀；跨段合并也必须保留这条证据。
        event['distinct_from'] = [identifier for old in events
                                  if 0 <= ts - old.get('last_seen', old['ts']) <= cfg.sig_window
                                  for identifier in old.get('ids', [])]
        events.append(event)
        visible.append(event)
        label = '候选播报' if getattr(cfg, 'identity_mode', 'keywords') == 'auto' else '命中'
        print(f'    {event["ts"]:.2f}s {label} {event["signature"]}', flush=True)
    visible_ids = {id(event) for event in visible}
    for event in events:
        event['misses'] = 0 if id(event) in visible_ids else event.get('misses', 0) + 1
        event['active'] = event['misses'] < 2
    return visible


def _scan_shared(path, cfg, seg_id, start, end, engine):
    auto_identity = getattr(cfg, 'identity_mode', 'keywords') == 'auto'
    source_identity = store.identity(path)
    state = (None if cfg.rescan else store.load_progress(cfg, path, start, end)) or {
        'resume_ts': start, 'events': [], 'review': [], 'complete': False,
        'phase_samples': [],
        'stats': {'ocr_calls': 0, 'diff_skips': 0, 'sampled_frames': 0, 'phase_ocr_calls': 0}}
    if state['complete']:
        return state
    resume = max(start, state['resume_ts'] - cfg.sig_window)
    events, review, stats = state['events'], state['review'], state['stats']
    reader = build_reader(cfg)
    phase_reader = reader
    if cfg.filter_warmup and 'ch_sim' not in cfg.languages:
        phase_reader = build_reader(SimpleNamespace(languages=['ch_sim', 'en'], gpu=cfg.gpu))
    phase_samples = state.setdefault('phase_samples', [])
    vision_samples = state.setdefault('vision_samples', [])
    avatar_boxes = state.setdefault('avatar_boxes', list(getattr(cfg, 'layout_evidence', {}).get('avatar_boxes', [])))
    stats.setdefault('phase_ocr_calls', 0)
    frames = media.ffmpeg_frames if engine == 'ffmpeg' else media.cv2_frames
    x1, _, _, _, max_x = media.crop_rect(cfg)
    prev_gray, last_ocr, last_save = None, -math.inf, time.monotonic()
    last_visible = []
    last_phase = -math.inf
    print(f'  [段 {seg_id + 1}] {Path(path).name} {resume:.1f}～{end:.1f}s / {engine}', flush=True)
    frame_options = {'with_phase': cfg.filter_warmup or auto_identity}
    if auto_identity:
        frame_options['with_identity'] = True
    for sample in frames(path, cfg, resume, end, **frame_options):
        ts, roi = sample[:2]
        before_events, before_review = len(events), len(review)
        stats['sampled_frames'] += 1
        if auto_identity:
            vision_samples.append(dict(perspective.signals(sample[3], cfg, avatar_boxes, reader=reader), ts=float(ts)))
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        changed = True
        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            changed = cv2.countNonZero(cv2.threshold(diff, DIFF_THRESHOLD, 255, cv2.THRESH_BINARY)[1]) >= gray.size * cfg.min_change_ratio
        # 与上一次已识别画面比较，保留缓慢累积变化。变化时不等抑制期。
        if auto_identity or changed or ts - last_ocr >= cfg.suppress_after_hit:
            if changed or ts - last_ocr >= cfg.suppress_after_hit:
                prev_gray, last_ocr = gray, ts
                if auto_identity and vision_samples[-1].get('mode') == 'spectator':
                    boxes = []  # Selected roster count, not feed names, drives spectator kills.
                else:
                    stats['ocr_calls'] += 1
                    boxes = read_roi(reader, roi, cfg)
            else:
                stats['diff_skips'] += 1
                boxes = []
            hits, candidates = (perspective.classify_feed(boxes, cfg, x1, max_x, roi) if auto_identity
                                else _classify_boxes(boxes, cfg, x1, max_x))
            previous_visible = last_visible
            last_visible = observe_hits(events, review, hits, ts, cfg)
            if not changed and not auto_identity:
                for old in previous_visible:
                    if not any(old is event for event in last_visible):
                        old.update(last_seen=float(ts), active=True, misses=0)
                        last_visible.append(old)
            for candidate in candidates:
                if candidate['reason'] not in ('missing_victim', 'low_confidence', 'possible_name'):
                    continue
                old = next((e for e in reversed(events) if e.get('row_y') is not None
                            and abs(e['row_y'] - candidate['row_y']) < 12
                            and 0 <= ts - e.get('last_seen', e['ts']) <= 1
                            and _similar(e['signature'].split('|')[0], candidate['signature'].split('|')[0])), None)
                if old:
                    old.update(last_seen=float(ts), active=True, misses=0)
                    last_visible.append(old)
            review.extend(dict(c, ts=float(ts)) for c in candidates)
            review[:] = merge_review(review, events, cfg)
        else:
            stats['diff_skips'] += 1
            for event in last_visible:
                event['last_seen'] = float(ts)
        if (cfg.filter_warmup or auto_identity) and (ts - last_phase >= config.WARMUP_INTERVAL - .001
                                  or len(events) > before_events or len(review) > before_review):
            phase_samples.append(dict(warmup.read_phase(phase_reader, sample[2], cfg), ts=float(ts)))
            if auto_identity and 'clock' in phase_samples[-1]:
                vision_samples[-1]['clock'] = phase_samples[-1]['clock']
            stats['phase_ocr_calls'] += 1
            last_phase = ts
        state['resume_ts'] = float(ts)
        if time.monotonic() - last_save >= 60:
            if store.identity(path) != source_identity:
                raise ValueError('扫描期间源文件发生变化，断点未覆盖')
            store.save_progress(cfg, path, start, end, state)
            print(f'  [进度] {Path(path).name} {ts:.1f}/{end:.1f}s，{len(events)} 条候选击杀', flush=True)
            last_save = time.monotonic()
    state['complete'] = True
    state['resume_ts'] = end
    if store.identity(path) != source_identity:
        raise ValueError('扫描期间源文件发生变化，完成段未登记')
    store.save_progress(cfg, path, start, end, state)
    return state


def _scan_segment(path, cfg, seg_id, start, end):
    return _scan_shared(path, cfg, seg_id, start, end, 'cv2')


def _scan_segment_ffmpeg(path, cfg, seg_id, start, end):
    return _scan_shared(path, cfg, seg_id, start, end, 'ffmpeg')


def scan_video(video_path, cfg, start=0.0, end=None):
    path = str(Path(video_path).resolve())
    t0 = time.monotonic()
    try:
        before = store.identity(path)
        cfg = autolayout.resolve_source(cfg, path)
        duration = cfg.media_info['duration']
        end = min(duration, end) if end is not None else duration
        if not (0 <= start < end):
            raise ValueError('扫描区间需要满足 0 <= start < end <= 视频时长')
        print(f'[开始] {Path(path).name} / {cfg.profile} / {cfg.effective_engine}', flush=True)
        count = min(4, cfg.scan_segments)
        fn = _scan_segment_ffmpeg if cfg.effective_engine == 'ffmpeg' else _scan_segment
        if count == 1:
            states = [fn(path, cfg, 0, start, end)]
        else:
            span = (end - start) / count
            ranges = [(start + i * span, min(end, start + (i + 1) * span + cfg.sig_window))
                      for i in range(count)]
            with ProcessPoolExecutor(max_workers=count) as pool:
                tasks = [pool.submit(fn, path, cfg, i, s, e) for i, (s, e) in enumerate(ranges)]
                states = [task.result() for task in as_completed(tasks)]
        if store.identity(path) != before:
            raise ValueError('扫描期间源文件发生变化，结果未登记为成功')
        events = merge_events([e for s in states for e in s['events']], cfg.cluster_window)
        auto_identity = getattr(cfg, 'identity_mode', 'keywords') == 'auto'
        if auto_identity:
            samples = [sample for s in states for sample in s.get('vision_samples', [])]
            events, same_player_review, identity_state = perspective.attribute(
                events, [], samples, start, end)
        else:
            identity_state = {}
            events, same_player_review = filter_same_player_events(events, cfg)
        review = merge_review([r for s in states for r in s['review']] + same_player_review, events, cfg)
        if auto_identity:
            events, review = perspective.check_first_visible(events, review, cfg.sig_window)
        phase = {'checked': cfg.filter_warmup, 'intervals': [], 'excluded_events': [], 'excluded_review': []}
        if cfg.filter_warmup:
            samples = [sample for s in states for sample in s.get('phase_samples', [])]
            phase['intervals'] = warmup.intervals(samples, start, end)
            events, review, phase['excluded_events'], phase['excluded_review'] = warmup.filter_events(
                events, review, phase['intervals'])
            review = merge_review(review, events, cfg)
        stats = {key: sum(s['stats'].get(key, 0) for s in states)
                 for key in ('ocr_calls', 'diff_skips', 'sampled_frames', 'phase_ocr_calls')}
        stats['elapsed_seconds'] = time.monotonic() - t0
        if auto_identity and not identity_state.get('views'):
            return ScanResult(path, 'needs_review', review=review + [{'ts': float(start),
                              'reason': 'unknown_view_identity', 'detail': '未确认可跟踪的当前视角 HUD'}],
                              identity=before, coverage=[float(start), float(end)], stats=stats,
                              phase=phase, perspective=identity_state)
        return ScanResult(path, 'complete', [e['ts'] for e in events], events, review,
                          stats=stats, identity=before, coverage=[float(start), float(end)], phase=phase,
                          perspective=identity_state)
    except media.UnknownProfile as error:
        return ScanResult(path, 'needs_review', error=str(error),
                          review=[{'ts': 0.0, 'reason': 'unknown_profile', 'detail': str(error)}])
    except Exception as error:
        log.exception('扫描失败：%s', path)
        return ScanResult(path, 'failed', error=f'{type(error).__name__}: {error}',
                          stats={'elapsed_seconds': time.monotonic() - t0})


def cluster_hits(raw_hits, window):
    # 旧的二元组 API 保留最高置信度代表点；带签名时以首次出现时间为准。
    if not raw_hits:
        return []
    if all(len(h) > 2 for h in raw_hits):
        events = merge_events([{'ts': h[0], 'confidence': h[1], 'signature': h[2]}
                               for h in raw_hits], window)
        return [(e['ts'], e['confidence']) for e in events]
    clusters = []
    for hit in sorted(raw_hits):
        if clusters and hit[0] - clusters[-1][-1][0] <= window:
            clusters[-1].append(hit)
        else:
            clusters.append([hit])
    return [max(group, key=lambda h: h[1])[:2] for group in clusters]


def setup_logging(cfg):
    Path(cfg.log_file).parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(cfg.log_file, encoding='utf-8')], force=True)
