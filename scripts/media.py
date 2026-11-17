"""统一旋转后的显示坐标和相对播放时间轴；解码器只负责提供采样画面。"""
import json
import math
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

@lru_cache(maxsize=128)
def _probe(path, size, mtime_ns):
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-show_format',
                                 '-of', 'json', path], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=60)
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f'视频探测失败：{error}') from error
    if result.returncode:
        raise ValueError(f'ffprobe 失败：{result.stderr[-500:]}')
    data = json.loads(result.stdout)
    streams = [s for s in data.get('streams', []) if s.get('codec_type') == 'video'
               and not s.get('disposition', {}).get('attached_pic')]
    if not streams:
        raise ValueError('未找到视频流')
    stream = streams[0]
    w, h = int(stream['width']), int(stream['height'])
    rotation = next((float(s['rotation']) for s in stream.get('side_data_list', [])
                     if 'rotation' in s), float(stream.get('tags', {}).get('rotate', 0)))
    if round(rotation / 90) % 2:
        w, h = h, w
    rate = stream.get('avg_frame_rate', '0/1').split('/')
    fps = float(rate[0]) / float(rate[1]) if len(rate) == 2 and float(rate[1]) else 0
    duration = float(data.get('format', {}).get('duration') or stream.get('duration') or 0)
    if w <= 0 or h <= 0 or not math.isfinite(duration) or duration <= 0:
        raise ValueError('视频尺寸或时长无效')
    return {'width': w, 'height': h, 'rotation': rotation, 'fps': fps,
            'duration': duration, 'start_time': float(data['format'].get('start_time', 0)),
            'has_audio': any(s.get('codec_type') == 'audio' for s in data['streams'])}


def probe(path):
    p = Path(path).resolve()
    stat = p.stat()
    return dict(_probe(str(p), stat.st_size, stat.st_mtime_ns))


class UnknownProfile(ValueError):
    """Compatibility exception re-exported to the source resolver."""
    pass


def prepare_cfg(cfg, path):
    """Deprecated compatibility entry; source resolution lives in autolayout."""
    import autolayout
    return autolayout.resolve_source(cfg, path)


def display_frame(path, timestamp):
    """Decode one full-resolution frame in the rotated display coordinate space."""
    input_args, offset = seek_input(path, timestamp)
    command = ['ffmpeg', '-v', 'error', *input_args]
    if offset:
        command += ['-ss', str(offset)]
    command += ['-frames:v', '1', '-f', 'image2pipe', '-vcodec', 'png', '-']
    result = subprocess.run(command, capture_output=True, timeout=60)
    if result.returncode:
        raise ValueError(f'布局抽帧失败：{result.stderr[-400:]!r}')
    frame = cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise ValueError('布局抽帧未返回可解码画面')
    return frame


def compute_region(cfg, width, height):
    """Compatibility alias for autolayout's feed-region geometry."""
    import autolayout
    return autolayout.compute_region(cfg, width, height)


def crop_rect(cfg):
    """Return a source's feed crop and X threshold; retain the legacy helper."""
    feed_rect = getattr(cfg, 'feed_rect', None)
    if feed_rect is not None:
        return (*feed_rect, int(cfg.max_x_center_ratio * cfg.media_info['width']))
    import autolayout
    return autolayout.crop_rect(cfg)


def phase_rect(cfg):
    rect = getattr(cfg, 'phase_rect', None)
    if rect is not None:
        return rect
    import autolayout
    return autolayout.phase_rect(cfg)


def perspective_rects(cfg):
    """Compatibility alias; identity geometry is defined in perspective."""
    rects = getattr(cfg, 'identity_rects', None)
    if rects is not None:
        return rects
    import perspective
    info = cfg.media_info
    return perspective.identity_rects(info['width'], info['height'], cfg.profile)


def seek_input(path, start):
    # TS 没有 MP4 式精确索引。提前解码一小段，再按相对时间裁掉，
    # 避免直接 seek 到 GOP 中间后从下一关键帧开始、产生静默时间偏移。
    anchor = max(0.0, start - 10.0) if Path(path).suffix.lower() in ('.ts', '.m2ts') else start
    return ['-ss', f'{anchor:.6f}', '-i', str(path)], start - anchor


def cv2_frames(path, cfg, start, end, *, with_phase=False, with_identity=False):
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise ValueError('OpenCV 无法打开视频')
    cap.set(cv2.CAP_PROP_POS_MSEC, start * 1000)
    x, y, w, h, _ = crop_rect(cfg)
    if with_phase:
        px, py, pw, ph = phase_rect(cfg)
    identity_rects = perspective_rects(cfg) if with_identity else {}
    index, last = 0, None
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            pos = cap.get(cv2.CAP_PROP_POS_MSEC) / 1000
            ts = pos if pos > 0 else start + index / (cfg.media_info['fps'] or 60)
            index += 1
            if ts >= end:
                break
            if ts < start - .001 or (index - 1) % cfg.scan_interval:
                continue
            roi = frame[y:y+h, x:x+w]
            if roi.shape[:2] != (h, w):
                raise ValueError('解码画面尺寸与旋转后的播报区域不一致')
            last = ts
            phase = None
            if with_phase:
                phase = frame[py:py+ph, px:px+pw]
                if phase.shape[:2] != (ph, pw):
                    raise ValueError('解码画面尺寸与中央阶段区域不一致')
            if with_identity:
                context = {name: frame[b:b+d, a:a+c].copy()
                           for name, (a, b, c, d) in identity_rects.items()}
                yield ts, roi, phase, context
            elif with_phase:
                yield ts, roi, phase
            else:
                yield ts, roi
    finally:
        cap.release()
    tolerance = max(1.0, cfg.scan_interval / (cfg.media_info['fps'] or 60) + .5)
    if last is None or last < end - tolerance:
        raise ValueError(f'视频提前结束或未能解码到目标区间末尾：{last} / {end:.3f}s')


def ffmpeg_frames(path, cfg, start, end, *, with_phase=False, with_identity=False):
    x, y, w, h, _ = crop_rect(cfg)
    fps = cfg.roi_fps
    input_args, offset = seek_input(path, start)
    filters = f'trim=start={offset:.6f}:duration={end-start:.6f},setpts=PTS-STARTPTS,fps={fps}'
    regions = {'feed': (x, y, w, h)}
    if with_phase:
        regions['phase'] = phase_rect(cfg)
    if with_identity:
        regions.update(perspective_rects(cfg))
    packed_w = max(rect[2] for rect in regions.values())
    packed_h = sum(rect[3] for rect in regions.values())
    offsets, at = {}, 0
    for name, rect in regions.items():
        offsets[name] = (at, rect[2], rect[3])
        at += rect[3]
    if len(regions) > 1:
        names = list(regions)
        filters += f',split={len(names)}' + ''.join(f'[s{i}]' for i in range(len(names))) + ';'
        for i, name in enumerate(names):
            a, b, c, d = regions[name]
            filters += f'[s{i}]crop={c}:{d}:{a}:{b},pad={packed_w}:{d}[r{i}];'
        filters += ''.join(f'[r{i}]' for i in range(len(names))) + f'vstack=inputs={len(names)}'
    else:
        filters += f',crop={w}:{h}:{x}:{y}'
    cmd = ['ffmpeg', '-v', 'error', *input_args,
           '-t', f'{end-start:.6f}', '-vf',
           filters,
           '-threads', '1', '-pix_fmt', 'bgr24', '-f', 'rawvideo', '-']
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=errors)
        count, consumed = 0, False
        try:
            while True:
                buf = proc.stdout.read(packed_w * packed_h * 3)
                if not buf:
                    consumed = True
                    break
                if len(buf) != packed_w * packed_h * 3:
                    raise ValueError('FFmpeg 返回残缺的 ROI 帧')
                frame = np.frombuffer(buf, np.uint8).reshape(packed_h, packed_w, 3)
                unpacked = {name: frame[offset:offset+rh, :rw].copy()
                            for name, (offset, rw, rh) in offsets.items()}
                roi = unpacked.pop('feed')
                phase = unpacked.pop('phase', None)
                if with_identity:
                    yield start + count / fps, roi, phase, unpacked
                elif with_phase:
                    yield start + count / fps, roi, phase
                else:
                    yield start + count / fps, roi
                count += 1
        finally:
            proc.stdout.close()
            if not consumed and proc.poll() is None:
                proc.terminate()
            try:
                code = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                raise
        errors.seek(0)
        detail = errors.read().decode('utf-8', errors='replace')
        if code:
            raise ValueError(f'FFmpeg 解码失败 ({code})：{detail[-500:]}')
        if count == 0 or count / fps < end - start - max(1, 1 / fps):
            raise ValueError('FFmpeg 未完整解码目标区间')
