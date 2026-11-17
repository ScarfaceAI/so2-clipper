"""从已校验独立片段生成原声集锦，合并副本去除同源重叠上下文。"""
import os
import subprocess
from pathlib import Path

import media
import store
import workspace
from extraction import ClipResult, _clip_ok, validate_clip


def merge_clips(files, cfg, previous=None):
    pieces = []
    for file in files:
        covered = 0.
        for clip in sorted(file.get('clips', []), key=lambda c: c['start']):
            if clip['status'] not in ('generated', 'reused'):
                continue
            skip = max(0., covered - clip['start'])
            covered = max(covered, clip['end'])
            duration = clip['end'] - clip['start'] - skip
            if duration > .03:
                try:
                    source_identity = store.identity(clip['path'])
                except (OSError, ValueError) as error:
                    return ClipResult('failed', 0, 0, 0, error=f'合并输入不可访问：{error}')
                pieces.append({'path': clip['path'], 'skip': skip, 'duration': duration,
                               'identity': source_identity, 'key': clip.get('key'),
                               'kill_count': clip['kill_count']})
    if not pieces:
        return ClipResult('filtered', 0, 0, 0, error='本次没有可合并的可信片段')
    duration = sum(p['duration'] for p in pieces)
    count = sum(p['kill_count'] for p in pieces)
    target = None
    try:
        infos = [media.probe(p['path']) for p in pieces]
        width, height = infos[0]['width'], infos[0]['height']
        fps = min(60., infos[0]['fps'] or 30.)
        has_audio = any(info['has_audio'] for info in infos)
        settings = {'version': 1, 'width': width, 'height': height, 'fps': fps,
                    'audio': has_audio, 'overlap': 'trim', 'encoder': 'libx264-fast-crf18-aac128k'}
        key = store.digest({'pieces': pieces, 'settings': settings})
        expected = {'width': width, 'height': height, 'has_audio': has_audio}
        prior = next((c for c in previous or [] if c.get('key') == key), None)
        if prior and _clip_ok(prior['path'], duration, expected):
            return ClipResult('reused', 0, duration, count, prior['path'], key,
                              validation=prior.get('validation', {}), settings=settings)
        folder = Path(cfg.output_dir) / (getattr(cfg, 'run_date', None) or store.now(cfg).strftime('%Y-%m-%d'))
        target = folder / f'highlight_{key[:20]}.mp4'
        if target.is_file() and _clip_ok(target, duration, expected):
            return ClipResult('reused', 0, duration, count, str(target), key, settings=settings)
        work = Path(cfg.tmp_dir) / 'encoding'
        work.mkdir(parents=True, exist_ok=True)
        part = work / f'{target.stem}.{os.getpid()}.part.mp4'
        command = ['ffmpeg', '-v', 'error', '-y']
        for piece in pieces:
            command += ['-i', piece['path']]
        filters = []
        for i, (piece, info) in enumerate(zip(pieces, infos)):
            filters.append(f'[{i}:v]trim=start={piece["skip"]:.6f},setpts=PTS-STARTPTS,'
                           f'trim=duration={piece["duration"]:.6f},scale={width}:{height}:force_original_aspect_ratio=decrease,'
                           f'pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps}[v{i}]')
            if has_audio:
                if info['has_audio']:
                    filters.append(f'[{i}:a]atrim=start={piece["skip"]:.6f}:duration={piece["duration"]:.6f},'
                                   f'asetpts=PTS-STARTPTS,aresample=48000,'
                                   f'aformat=channel_layouts=stereo[a{i}]')
                else:
                    filters.append(f'anullsrc=r=48000:cl=stereo,atrim=duration={piece["duration"]:.6f}[a{i}]')
        labels = ''.join(f'[v{i}]' + (f'[a{i}]' if has_audio else '') for i in range(len(pieces)))
        filters.append(labels + f'concat=n={len(pieces)}:v=1:a={int(has_audio)}[v]' + ('[a]' if has_audio else ''))
        graph = work / f'merge_{key[:20]}.ffscript'
        graph.write_text(';'.join(filters), encoding='utf-8')
        command += ['-filter_complex_script', str(graph), '-map', '[v]']
        if has_audio:
            command += ['-map', '[a]', '-c:a', 'aac', '-b:a', '128k']
        command += ['-c:v', 'libx264', '-preset', 'fast', '-crf', '18', '-movflags', '+faststart', str(part)]
        result = subprocess.run(command, capture_output=True, timeout=max(600, duration * 10))
        if result.returncode:
            raise ValueError(f'集锦编码失败：{result.stderr[-700:]!r}')
        validation = validate_clip(part, duration, expected)
        if any(store.identity(p['path']) != p['identity'] for p in pieces):
            raise ValueError('合并期间独立片段发生变化')
        workspace.publish(part, target, cfg.output_dir)
        return ClipResult('generated', 0, duration, count, str(target), key,
                          validation=validation, settings=settings)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return ClipResult('failed', 0, duration, count, str(target) if target else None, error=str(error))
