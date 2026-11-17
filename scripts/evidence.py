"""可复用的源画面／成片证据；生成图片不等于 Agent 已完成视觉判断。"""
import json
import math
from pathlib import Path

import cv2
import numpy as np

import media
import review
import store


def read_bundle(path, expected_identity=None):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8'))
    if expected_identity is not None and data.get('source_identity') != expected_identity:
        raise ValueError('证据不属于当前源文件修订')
    frames = {}
    for item in data.get('frames', []):
        image = Path(item['path']).resolve()
        if not image.is_relative_to(path.parent) or not image.is_file():
            raise ValueError('证据图片缺失或不在清单目录内')
        if store.file_sha256(image) != item['sha256']:
            raise ValueError('证据图片已变化')
        if not math.isfinite(item['ts']) or item['ts'] < 0:
            raise ValueError('证据时间无效')
        frames[str(image)] = item
    if not frames:
        raise ValueError('证据清单没有图片')
    return data, frames


def bundle(path, times, root):
    identity = store.identity(path)
    times = sorted(set(round(float(t), 6) for t in times))
    duration = media.probe(path)['duration']
    if not times or any(not math.isfinite(t) or not 0 <= t < duration for t in times):
        raise ValueError('抽帧时间超出源视频')
    folder = Path(root) / store.digest([identity, times])[:24]
    manifest = folder / 'frames.json'
    if manifest.is_file():
        data, _ = read_bundle(manifest, identity)
    else:
        manifest = review.frame_bundle(path, times, folder)
        data, _ = read_bundle(manifest, identity)
    cards = []
    # 每页最多六帧；原尺寸图片仍保留，卡片只供导航，不能替代看清 HUD。
    for offset in range(0, len(data['frames']), 6):
        target = folder / f'card-{offset // 6 + 1:03}.jpg'
        if not target.is_file():
            cells = []
            for item in data['frames'][offset:offset + 6]:
                frame = cv2.imdecode(np.fromfile(item['path'], dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is None:
                    raise ValueError('证据画面不能解码')
                scale = min(640 / frame.shape[1], 360 / frame.shape[0])
                small = cv2.resize(frame, None, fx=scale, fy=scale)
                cell = np.full((394, 640, 3), 24, np.uint8)
                cell[32:32 + small.shape[0], :small.shape[1]] = small
                cv2.putText(cell, f't={item["ts"]:.3f}s', (10, 23),
                            cv2.FONT_HERSHEY_SIMPLEX, .6, (255, 255, 255), 1)
                cells.append(cell)
            while len(cells) % 2:
                cells.append(np.full_like(cells[0], 24))
            card = np.vstack([np.hstack(cells[i:i + 2]) for i in range(0, len(cells), 2)])
            cv2.imencode('.jpg', card)[1].tofile(target)
        cards.append(str(target))
    if store.identity(path) != identity:
        raise ValueError('抽帧期间源文件变化')
    return {'manifest': str(manifest), 'cards': cards, 'frames': data['frames'],
            'source_identity': identity, 'visual_review': 'pending'}


def window_times(start, end, kills=()):
    last = max(start, end - .1)
    times = {start, last, (start + last) / 2}
    for ts in kills:
        times.update((max(start, ts - .67), min(last, ts + .67)))
    # 长间隔增加导航画面；仍需 Agent 按计数／动作差异主动加密。
    ordered = sorted(times)
    for a, b in zip(ordered, ordered[1:]):
        for i in range(1, int((b - a) / 10) + 1):
            times.add(a + (b - a) * i / (int((b - a) / 10) + 1))
    return sorted(times)
