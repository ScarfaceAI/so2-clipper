"""Resolve each source's calibrated HUD layout and pixel regions."""
import json
from pathlib import Path

import cv2
import numpy as np

import config
import media
import perspective
import store
from options import ResolvedSource, copy_options

VERSION = 3


UnknownProfile = media.UnknownProfile


def display_frame(path, timestamp):
    """Compatibility alias; generic display-frame extraction lives in media."""
    return media.display_frame(path, timestamp)


def compute_region(cfg, width, height):
    """Scale the configured feed area and X threshold to display dimensions."""
    sx, sy = width / cfg.base_w, height / cfg.base_h
    x1, y1 = max(0, int(cfg.region['x1'] * sx)), max(0, int(cfg.region['y1'] * sy))
    x2, y2 = min(width, int(cfg.region['x2'] * sx)), min(height, int(cfg.region['y2'] * sy))
    return x1, y1, x2, y2, int(cfg.max_x_center_ratio * width)


def crop_rect(cfg):
    info = cfg.media_info
    x1, y1, x2, y2, max_x = compute_region(cfg, info['width'], info['height'])
    # Both decoders use the original even-boundary convention.
    x1 -= x1 % 2
    y1 -= y1 % 2
    x2 -= x2 % 2
    y2 -= y2 % 2
    if x2 <= x1 or y2 <= y1:
        raise ValueError('播报区域超出显示画面')
    return x1, y1, x2 - x1, y2 - y1, max_x


def phase_rect(cfg):
    width, height = cfg.media_info['width'], cfg.media_info['height']
    x1, y1, x2, y2 = (int(v * size) // 2 * 2 for v, size in
                       zip(config.WARMUP_REGION, (width, height, width, height)))
    if x2 <= x1 or y2 <= y1:
        raise ValueError('中央阶段提示区域超出显示画面')
    return x1, y1, x2 - x1, y2 - y1


def source_rectangles(source):
    """Resolve feed, phase, and identity crops once for a source."""
    info = source.media_info
    x1, y1, width, height, _ = crop_rect(source)
    return {
        'feed_rect': (x1, y1, width, height),
        'phase_rect': phase_rect(source),
        'identity_rects': perspective.identity_rects(
            info['width'], info['height'], source.profile),
    }


def resolve_source(options, path, media_info=None, layout_evidence=None):
    """Resolve profile, engine, evidence, and pixel regions for one input file."""
    info = media_info if media_info is not None else media.probe(path)
    resolved_path = Path(path).resolve()
    profile = options.profile
    identity_mode = getattr(options, 'identity_mode', 'keywords')
    evidence = layout_evidence
    if evidence is None:
        evidence = getattr(options, 'layout_evidence', None)

    if profile == 'auto' and identity_mode == 'auto':
        matches = [item for item in getattr(options, 'registered_sources', [])
                   if resolved_path.is_relative_to(Path(item['source']).resolve())
                   and (info['width'], info['height']) == (item['width'], item['height'])]
        if matches:
            profile = max(matches, key=lambda item: len(item['source']))['profile']
    if profile == 'auto' and identity_mode == 'auto':
        evidence = discover(options, str(resolved_path), info, evidence)
        profile = evidence['profile']
    if profile == 'auto':
        profile = next((name for root, name in config.SOURCE_PROFILES.items()
                        if resolved_path.is_relative_to(Path(root).resolve())), None)
        if profile == 'phone':
            if (info['width'], info['height']) == (2354, 1080):
                profile = 'phone-highfps'
            elif (info['width'], info['height']) != (2388, 1668):
                profile = None
        elif profile == 'pc' and (info['width'], info['height']) not in ((1920, 1080), (2058, 1440)):
            profile = None
        elif profile == 'live' and (info['width'], info['height']) != (1920, 1080):
            profile = None
        if profile is None:
            if options.region_override:
                profile = 'custom'
            else:
                raise UnknownProfile(
                    f'来源布局未确认（显示 {info["width"]}×{info["height"]}）；请指定 --profile 或校准 --region')

    region = dict(options.region)
    scan_engine = options.scan_engine
    max_x_center_ratio = options.max_x_center_ratio
    if profile in config.PROFILES:
        preset_region, preset_max_x, preset_engine = config.PROFILES[profile]
        if not options.region_override:
            region = dict(preset_region)
        if options.max_x_override is None:
            max_x_center_ratio = preset_max_x
        if scan_engine == 'auto':
            scan_engine = preset_engine
    if identity_mode == 'auto' and not options.region_override:
        # Auto identity needs enough room for long names and assists.
        region = {'x1': int(.65 * options.base_w),
                  'y1': int((.115 if profile == 'live' else .02) * options.base_h),
                  'x2': options.base_w, 'y2': int(.30 * options.base_h)}
    effective_engine = ('ffmpeg' if info['fps'] >= 90 else 'cv2') if scan_engine == 'auto' else scan_engine

    source = ResolvedSource.from_options(
        options, str(resolved_path), info, profile, effective_engine, evidence or {},
        region=region, scan_engine=scan_engine, max_x_center_ratio=max_x_center_ratio)
    return copy_options(source, **source_rectangles(source))


def discover(cfg, path, info, layout_evidence=None):
    revision = store.identity(path)
    prior = layout_evidence
    if prior is None:
        prior = getattr(cfg, 'layout_evidence', {})
    if prior.get('identity') == revision and prior.get('version') == VERSION:
        return prior
    token = store.digest(revision)[:20]
    folder = Path(cfg.tmp_dir) / 'intake' / token
    folder.mkdir(parents=True, exist_ok=True)
    saved = folder / 'layout.json'
    if saved.is_file():
        prior = json.loads(saved.read_text(encoding='utf-8'))
        if prior.get('identity') == revision and prior.get('version') == VERSION:
            return prior
    observations, known_boxes = [], []
    duration = info['duration']
    for timestamp in sorted({min(3., duration * .1), duration * .15, duration * .5, duration * .85}):
        frame = media.display_frame(path, timestamp)
        height, width = frame.shape[:2]
        # Live banners can contain white text. Locate the game view's top from
        # the black bars on both sides before checking the avatar row.
        sides = np.concatenate([frame[:, int(width*.04):int(width*.18)],
                                frame[:, int(width*.82):int(width*.96)]], axis=1)
        black = (sides.max(axis=2) < 24).mean(axis=1)
        first_content = next((i for i in range(int(height*.2)) if black[i] < .95), 0)
        region = perspective.LIVE_AVATAR_REGION if first_content > height*.065 else perspective.AVATAR_REGION
        a, b, c, d = [int(v * size) // 2 * 2 for v, size in
                      zip(region, (width, height, width, height))]
        avatar = perspective.active_avatar(frame[b:d, a:c], width, height,
                                           (a, b, c-a, d-b), known_boxes)
        rects = perspective.identity_rects(width, height, 'live')
        context = {name: frame[y:y+h, x:x+w] for name in ('spectator', 'spectator_right')
                   for x, y, w, h in [rects[name]]}
        observed = perspective.spectator_signal(context, width, height, rects)
        if observed is not None:
            avatar = observed['avatar']
        destination = folder / f'{timestamp:.3f}.jpg'
        cv2.imencode('.jpg', frame)[1].tofile(destination)
        observations.append({'ts': timestamp, 'avatar': avatar, 'image': str(destination),
                             'inset': first_content > height * .065,
                             'mode': 'spectator' if observed is not None else 'personal'})
    visible = [observation['avatar'] for observation in observations if observation['avatar']]
    if len(visible) < 2:
        raise UnknownProfile(f'代表画面未确认稳定的游戏视角 HUD；抽帧位于 {folder}')
    inset = [o['inset'] for o in observations if o['avatar']]
    if len(set(inset)) != 1:
        raise UnknownProfile(f'视频中 HUD 位置发生变化，需要分段校准；抽帧位于 {folder}')
    if inset[0]:
        profile = 'live'
    elif info['width'] / info['height'] > 1.95:
        profile = 'phone-highfps'
    elif (info['width'], info['height']) == (2388, 1668):
        profile = 'phone'
    else:
        profile = 'pc'
    evidence = {'version': VERSION, 'identity': revision, 'profile': profile,
                'method': 'visible_current_view_hud', 'observations': observations,
                'avatar_boxes': known_boxes}
    store.atomic_json(saved, evidence)
    return evidence
