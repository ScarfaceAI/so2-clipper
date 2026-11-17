"""按击杀事件分组，校验临时 MP4 后登记可用片段。"""
import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

import media
import store
import workspace


@dataclass
class ClipResult:
    status: str
    start: float
    end: float
    kill_count: int
    path: str | None = None
    key: str | None = None
    error: str | None = None
    validation: dict = field(default_factory=dict)
    elapsed_seconds: float = 0.0
    settings: dict = field(default_factory=dict)
    event_ids: list = field(default_factory=list)
    actor: str | None = None
    view_id: str | None = None


def extraction_settings(cfg):
    return {'version': 4, 'pre': cfg.pre, 'post': cfg.post, 'kill_gap': cfg.kill_gap,
            'min_kills': cfg.min_kills, 'mode': cfg.mode,
            'encoder': ['libx264', 'fast', 18, 'aac', '128k'] if cfg.mode == 'reencode' else 'copy',
            **({'view_grouping_version': 1} if getattr(cfg, 'identity_mode', 'keywords') == 'auto' else {})}


def group_events(events, cfg, duration, phase_intervals=None, views=None):
    """人物与原视角段均相同才成组；确证的延迟事件可显式回到原视角段。"""
    barriers = sorted((max(0., r['start']), min(duration, r['end']))
                      for r in phase_intervals or [] if r['start'] < r['end'])
    view_map = {v['id']: v for v in views or []}
    partitions = {}
    for event in sorted(events, key=lambda e: e['ts']):
        timestamp = event['ts']
        if not 0 <= timestamp < duration:
            raise ValueError('击杀事件超出源视频范围')
        if any(a <= timestamp < b for a, b in barriers):
            raise ValueError('非正式比赛事件尚未过滤')
        if not event.get('actor') or not event.get('view_id'):
            raise ValueError('自动视角事件缺少已确认的归属，不能只凭时间成组')
        view = view_map.get(event['view_id'])
        if view is None or view.get('actor') != event['actor']:
            raise ValueError('自动视角事件没有匹配的人物视角段')
        attribution = event.get('attribution', {})
        if attribution.get('delayed_verified'):
            action = attribution.get('action_ts')
            if (action is None or not view['start'] <= action < view['end']
                    or not 0 <= timestamp - action <= 10
                    or any(a <= timestamp and b > action for a, b in barriers)):
                raise ValueError('延迟事件缺少同一正式阶段内的原动作归属')
        elif not view['start'] <= timestamp < view['end']:
            raise ValueError('普通击杀不在所属视角段内')
        lower = max([0.] + [b for a, b in barriers if b <= timestamp])
        upper = min([duration] + [a for a, b in barriers if a > timestamp])
        key = (event['actor'], event['view_id'], lower, upper)
        groups = partitions.setdefault(key, [])
        if groups and timestamp - groups[-1][-1]['ts'] <= cfg.kill_gap:
            groups[-1].append(event)
        else:
            groups.append([event])
    result = []
    frame_step = 1 / (getattr(cfg, 'media_info', {}).get('fps') or 60)
    for (actor, view_id, lower, upper), groups in partitions.items():
        view = view_map.get(view_id)
        if view:
            lower = max(lower, view['start'])
        for members in groups:
            first, last = members[0]['ts'], members[-1]['ts']
            end_limit = min(upper, view['end']) if view else upper
            delayed = [e for e in members if e.get('attribution', {}).get('delayed_verified')]
            if delayed:
                # 仅延伸到实际反馈结束，避免把新视角整段补入原人物片段。
                end_limit = min(upper, max(end_limit, max(e.get('last_seen', e['ts']) + .5 for e in delayed)))
            next_phase = next((r for r in phase_intervals or [] if r['start'] == upper), None)
            if next_phase and 'context_start' in next_phase:
                end_limit = min(end_limit, max(last + frame_step, next_phase['context_start'] + frame_step))
            start = max(lower, first - cfg.pre)
            end = min(end_limit, max(last + frame_step, last + cfg.post))
            if start > first or end <= last:
                raise ValueError('视角或阶段边界无法保留组内完整击杀')
            result.append({'start': start, 'end': end, 'count': len(members), 'actor': actor,
                           'view_id': view_id, 'event_ids': sorted({identifier for e in members
                                for identifier in e.get('ids', [store.digest([e['signature'], e['ts']])[:16]])})})
    return sorted(result, key=lambda group: (group['start'], group['actor']))


def group_kills(hits, cfg, duration, phase_intervals=None):
    barriers = sorted((max(0.0, r['start']), min(duration, r['end']))
                      for r in phase_intervals or [] if r['start'] < r['end'])
    groups = []
    frame_step = 1 / (getattr(cfg, 'media_info', {}).get('fps') or 60)
    for timestamp in sorted(hits):
        if not 0 <= timestamp < duration:
            raise ValueError(f'击杀时间超出视频范围：{timestamp} / {duration}')
        if any(start <= timestamp < end for start, end in barriers):
            raise ValueError(f'非正式比赛阶段的击杀尚未过滤：{timestamp}')
        lower = max([0.0] + [end for start, end in barriers if end <= timestamp])
        upper = min([duration] + [start for start, end in barriers if start > timestamp])
        if groups and groups[-1][1:3] == (lower, upper) and timestamp - groups[-1][0][-1] <= cfg.kill_gap:
            groups[-1][0].append(timestamp)
        else:
            groups.append(([timestamp], lower, upper))
    result = []
    for group, lower, upper in groups:
        # 进入热身发生在两次阶段采样之间。上下文停在此前最后一个采样帧，
        # 保留该帧和组内最后一杀，避免把尚未采到提示的热身开头带入片段。
        next_phase = next((r for r in phase_intervals or [] if r['start'] == upper), None)
        if next_phase and 'context_start' in next_phase:
            upper = min(upper, max(group[-1] + frame_step, next_phase['context_start'] + frame_step))
        result.append((max(lower, group[0] - cfg.pre), min(upper, group[-1] + cfg.post), len(group)))
    return result


def merge_overlapping(hits, cfg):
    """保留原有区间工具接口；默认工作流使用 group_kills，补上下文后不再合并。"""
    merged = []
    for timestamp in sorted(hits):
        start, end = max(0.0, timestamp - cfg.pre), timestamp + cfg.post
        if merged and start <= merged[-1][1]:
            merged[-1] = merged[-1][0], max(end, merged[-1][1])
        else:
            merged.append((start, end))
    return merged


def _decode_frame(path, second):
    result = subprocess.run(['ffmpeg', '-v', 'error', '-ss', f'{second:.6f}', '-i', str(path),
                             '-map', '0:v:0', '-frames:v', '1', '-an', '-sn',
                             '-vf', 'scale=2:2', '-pix_fmt', 'gray', '-f', 'rawvideo', '-'],
                            capture_output=True, timeout=60)
    if result.returncode or len(result.stdout) != 4:
        raise ValueError(f'片段在 {second:.3f}s 无法解码：{result.stderr[-300:]!r}')


def validate_clip(path, expected_duration, source_info, *, full_decode=False):
    info = media.probe(path)
    if abs(info['duration'] - expected_duration) > .5:
        raise ValueError(f'片段时长不符：{info["duration"]:.3f}s，预期 {expected_duration:.3f}s')
    if (info['width'], info['height']) != (source_info['width'], source_info['height']):
        raise ValueError('片段显示尺寸不符')
    if source_info['has_audio'] and not info['has_audio']:
        raise ValueError('源视频有音轨，输出音轨缺失')
    _decode_frame(path, 0)
    _decode_frame(path, max(0, min(info['duration'], expected_duration) - .25))
    if full_decode:
        result = subprocess.run(['ffmpeg', '-v', 'error', '-xerror', '-i', str(path),
                                 '-map', '0:v:0', '-map', '0:a:0?', '-f', 'null', '-'],
                                capture_output=True, timeout=max(600, expected_duration * 10))
        if result.returncode:
            raise ValueError(f'成片完整解码失败：{result.stderr[-400:]!r}')
    return info


def _clip_ok(path, expected_duration=None, source_info=None):
    try:
        if expected_duration is not None:
            validate_clip(path, expected_duration, source_info)
        else:
            info = media.probe(path)
            _decode_frame(path, 0)
            _decode_frame(path, max(0, info['duration'] - .25))
        return True
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def extract_segment(video_path, start, end, out_path, cfg, kill_count=0, key=None):
    t0 = time.monotonic()
    out_path = Path(out_path)
    part = Path(cfg.tmp_dir) / 'encoding' / f'{out_path.stem}.{os.getpid()}.part.mp4'
    try:
        part.parent.mkdir(parents=True, exist_ok=True)
        before = store.identity(video_path)
        source = media.probe(video_path)
        input_args, offset = media.seek_input(video_path, start)
        cmd = ['ffmpeg', '-v', 'error', '-y', *input_args]
        if offset:
            cmd += ['-ss', f'{offset:.6f}']
        cmd += ['-t', f'{end-start:.6f}', '-map', '0:v:0', '-map', '0:a:0?']
        if cfg.mode == 'copy':
            cmd += ['-c', 'copy', '-avoid_negative_ts', 'make_zero']
        else:
            cmd += ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18',
                    '-c:a', 'aac', '-b:a', '128k', '-fps_mode', 'vfr']
        cmd += ['-movflags', '+faststart', str(part)]
        result = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                                errors='replace', timeout=max(600, (end-start) * 10))
        if result.returncode:
            raise ValueError(f'FFmpeg 提取失败：{result.stderr[-800:]}')
        validation = validate_clip(part, end-start, source)
        if before != store.identity(video_path):
            raise ValueError('提取期间源视频发生变化')
        workspace.publish(part, out_path, cfg.output_dir)
        print(f'  新增并校验通过：{out_path.name}', flush=True)
        return ClipResult('generated', start, end, kill_count, str(out_path), key,
                          validation=validation, elapsed_seconds=time.monotonic()-t0,
                          settings=extraction_settings(cfg))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return ClipResult('failed', start, end, kill_count, str(out_path), key,
                          error=f'{type(error).__name__}: {error}',
                          elapsed_seconds=time.monotonic()-t0, settings=extraction_settings(cfg))


# 保留旧 Python 调用；生产调用使用明确的公开入口。
_ffmpeg_extract = extract_segment


def extract_clips(video_path, hits, cfg, previous=None, phase_intervals=None, *, events=None, views=None):
    info = media.probe(video_path)
    source_identity = store.identity(video_path)
    if getattr(cfg, 'identity_mode', 'keywords') == 'auto' and events is not None:
        groups = group_events(events, cfg, info['duration'], phase_intervals, views)
    else:
        groups = [{'start': a, 'end': b, 'count': c, 'event_ids': []}
                  for a, b, c in group_kills(hits, cfg, info['duration'], phase_intervals)]
    out_dir = Path(cfg.output_dir) / (getattr(cfg, 'run_date', None) or store.now(cfg).strftime('%Y-%m-%d'))
    cached = {c.get('key'): c for c in previous or [] if c.get('status') in ('generated', 'reused')}
    results, jobs = [], []
    settings = extraction_settings(cfg)
    for group in groups:
        start, end, count = group['start'], group['end'], group['count']
        metadata = {key: group.get(key) for key in ('event_ids', 'actor', 'view_id') if group.get(key) is not None}
        if count < cfg.min_kills:
            results.append(ClipResult('filtered', start, end, count, settings=settings, **metadata))
            continue
        key = store.digest({'source': source_identity, 'start': start, 'end': end, 'settings': settings,
                            **({'events': metadata} if group['event_ids'] else {})})
        name = (f'{Path(video_path).stem}_{store.source_path_id(video_path)[:12]}_'
                f'{count}kill_{start:.3f}s-{end:.3f}s_{key[:16]}.mp4')
        path = out_dir / name
        prior = cached.get(key)
        if prior and prior.get('path'):
            old = Path(prior['path'])
            if _clip_ok(old, end-start, info):
                print(f'  复用已有片段：{old.name}', flush=True)
                results.append(ClipResult('reused', start, end, count, str(old), key,
                                          validation=prior.get('validation', {}), settings=settings))
                for name, value in metadata.items():
                    setattr(results[-1], name, value)
                continue
        # 清单落盘前中断时，同名已校验文件仍可恢复，避免再次编码。
        if path.is_file() and _clip_ok(path, end-start, info):
            results.append(ClipResult('reused', start, end, count, str(path), key, settings=settings, **metadata))
        else:
            jobs.append((start, end, count, path, key, metadata))
    with ThreadPoolExecutor(max_workers=cfg.extract_workers) as pool:
        tasks = {pool.submit(extract_segment, video_path, s, e, str(p), cfg, count, key): metadata
                 for s, e, count, p, key, metadata in jobs}
        for task in as_completed(tasks):
            result = task.result()
            for name, value in tasks[task].items():
                setattr(result, name, value)
            results.append(result)
    return sorted(results, key=lambda item: item.start)
