"""Agent 内容决定的执行层：证据、同回合清单、分类发布与逐项恢复。

本模块不冒充视觉模型。prepare 的候选必须由当前 Agent 看图后确认；
deliver 只消费有依据的决定，保留 OCR 原记录，不能将候选生成当作内容验收。
"""
import copy
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import config
import core
import evidence
import extraction
import media
import review
import store
import workspace
from delivery_models import (ClipDecision, DeliveryJournal, DeliveryPlan, DeliveryRun,
                             EvidenceManifest, FileRecord, JournalItem, OutputReview,
                             PendingMove, PreparedClip)

WEAPONS = ('狙击枪', '步枪', '冲锋枪', '手枪', '霰弹枪', '投掷物', '近战')


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _cfg(plan):
    return config.load_config(['--output-dir', plan['output_dir'], '--state-dir', plan['state_dir'],
                              '--date-zone', plan['date_zone'], '--identity-mode', 'auto'])


def _within(path, root):
    value, root = Path(path).resolve(), Path(root).resolve()
    if value == root or not value.is_relative_to(root):
        raise ValueError(f'路径超出本次范围：{value}')
    return value


def _safe(text):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(text)).strip(' .')[:70]
    if not value or value.lower().split('.')[0] in ('con', 'prn', 'aux', 'nul',
            *('com' + str(i) for i in range(1, 10)), *('lpt' + str(i) for i in range(1, 10))):
        raise ValueError('文件名或分类名称无效')
    return value


def _record(path) -> FileRecord:
    return {'identity': store.identity(path), 'sha256': store.file_sha256(path)}


def _record_valid(path, record: FileRecord) -> bool:
    try:
        return store.identity(path) == record['identity'] and bool(record['sha256'])
    except (OSError, KeyError, ValueError):
        return False


def candidates(entry, cfg) -> list[ClipDecision]:
    """完整视角段内不按 10 秒拆候选；边界仍需 Agent 检查，不能视为已确认回合。"""
    events, views = entry.get('events', []), entry.get('perspective', {}).get('views', [])
    duration = entry.get('media_info', {}).get('duration', 0)
    if not duration:
        return []
    grouped = []
    if views and events:
        candidate_cfg = SimpleNamespace(**vars(cfg))
        candidate_cfg.kill_gap = duration
        try:
            grouped = extraction.group_events(events, candidate_cfg, duration,
                         entry.get('phase', {}).get('intervals', []), views)
        except ValueError:
            pass  # 错位／不完整归属交给下面逐事件候选，不能因此静默漏掉。
    if not grouped:
        times = sorted(set(entry.get('hits', []) + [e['ts'] for e in events]))
        grouped = [{'start': max(0, t - cfg.pre), 'end': min(duration, t + cfg.post),
                    'event_ids': [], 'scan_times': [t], 'actor': None} for t in times]
    rows = []
    for g in grouped:
        ids = set(g.get('event_ids', []))
        matches = [e for e in events if ids.intersection(e.get('ids', [review.event_id(e)]))]
        times = g.get('scan_times', [e['ts'] for e in matches])
        if not times:
            continue
        candidate_view_id = g.get('view_id')
        candidate_round_id = g.get('view_id', '')
        rows.append({'clip_id': store.digest([entry['identity'], times, candidate_view_id])[:20],
                     'start': g['start'], 'end': g['end'], 'actor': g.get('actor'),
                     # Retain the established JSON key. The candidate still needs
                     # Agent confirmation before this view id represents a round.
                     'round_id': candidate_round_id, 'scan_times': times,
                     'scan_event_ids': sorted(ids), 'status': 'pending', 'reason': '',
                     'kills': [], 'boundary_evidence': [], 'duplicate_of': None})
    return rows


def prepare(cfg, actors=None):
    with workspace.writer(cfg.state_dir):
        return _prepare(cfg, actors)


def review_index(cfg):
    path = Path(cfg.state_dir) / 'curation' / 'index.json'
    return read(path) if path.is_file() else {}


def _refresh_saved_plan(plan_path, plan, results, cfg):
    """补扫改变的是扫描材料；保留已作决定和证据，同时显式排队新增线索。"""
    changed = []
    for source in plan['sources']:
        entry = results['videos'].get(source['source_id'], {})
        info = media.probe(source['path'])
        valid = (entry.get('identity') == source['identity'] and entry.get('status') == 'complete'
                 and entry.get('coverage') == [0., info['duration']]
                 and entry.get('phase', {}).get('checked')
                 and entry.get('last_attempt', {}).get('status', 'complete') == 'complete')
        snapshot = {k: entry[k] for k in ('identity', 'cfg_fingerprint', 'events', 'hits', 'review',
                    'perspective', 'phase', 'coverage', 'media_info', 'status') if valid and k in entry}
        old = read(source['snapshot']) if source.get('snapshot') and Path(source['snapshot']).is_file() else None
        if source.get('scan_valid') == bool(valid) and old == snapshot:
            continue
        source.setdefault('review_history', []).append(copy.deepcopy(source['review']))
        source.setdefault('snapshot_history', []).append({k: source.get(k) for k in ('snapshot', 'snapshot_sha256')})
        target = Path(plan_path).parent / 'sources' / f'{source["source_id"]}-{store.digest(snapshot)[:16]}.json'
        store.atomic_json(target, snapshot)
        source.update(scan_valid=bool(valid), scan_fingerprint=entry.get('cfg_fingerprint') if valid else None,
                       snapshot=str(target), snapshot_sha256=store.file_sha256(target), media_info=info,
                      review={'status': 'pending', 'reason': '', 'evidence': [], 'unresolved': []})
        queue = []
        if valid:
            for item in entry.get('review', []):
                queue.append({'ts': item.get('ts', 0), 'reason': item.get('reason'), 'event_id': review.event_id(item)})
            for pulse in entry.get('perspective', {}).get('pulses', []):
                nearby = [e for e in entry.get('events', [])
                          if e.get('view_id') == pulse['view_id'] and abs(e['ts'] - pulse['ts']) <= 1.1]
                if len(nearby) != pulse['count']:
                    queue.append({'ts': pulse['ts'], 'reason': 'counter_feed_mismatch',
                                  'counter_increment': pulse['count'], 'recorded_events': len(nearby)})
            previous_clips = [c for c in plan['clips'] if c['source_id'] == source['source_id']]
            known_times = [t for c in previous_clips for t in c.get('scan_times', [])]
            for candidate in candidates(entry, cfg):
                if any(c['clip_id'] == candidate['clip_id'] for c in previous_clips):
                    continue
                if all(any(abs(t - prior) < .01 for prior in known_times) for t in candidate['scan_times']):
                    continue
                candidate.update(source_id=source['source_id'],
                                 reason='补扫出现新线索；与已有最终决定对照后合并或排除')
                plan['clips'].append(candidate)
        else:
            queue.append({'ts': 0, 'reason': 'missing_complete_scan'})
        previous_queue = source.get('review_queue', [])
        for i, item in enumerate(queue):
            prior = next((q for q in previous_queue if all(q.get(k) == v for k, v in item.items())), {})
            item.update(item_id=f'{source["source_id"][:12]}-{i + 1}',
                        status=prior.get('status', 'pending'), resolution=prior.get('resolution', ''),
                        evidence=prior.get('evidence', []))
        source['review_queue'] = queue
        source['overview_times'] = source.get('overview_times', [min(1., info['duration'] * .1),
                                       info['duration'] * .5, max(0., info['duration'] - .2)])
        changed.append(source['source_id'])
    if changed:
        store.atomic_json(plan_path, plan)
    return changed


def _prepare(cfg, actors=None):
    results = store.load_results(cfg)
    paths = [str(Path(cfg.video).resolve())] if cfg.video else core.discover_videos(cfg)
    if not paths:
        raise ValueError('没有选中的视频')
    index = review_index(cfg)
    selected = {store.source_path_id(p): store.identity(p) for p in paths}
    existing = {index[sid]['plan'] for sid in selected if sid in index}
    for path in existing:
        if not Path(path).is_file():
            continue
        prior = read(path)
        if ({s['source_id']: s['identity'] for s in prior['sources']} == selected
                and prior['settings']['actors'] == (actors or [])
                and prior['settings']['min_kills'] == cfg.min_kills):
            changed = _refresh_saved_plan(path, prior, results, cfg)
            return {'plan': str(path), 'task_id': prior['task_id'], 'reused': True,
                    'refreshed_sources': changed,
                    'sources': len(paths), 'candidates': len(prior['clips']),
                    'next_action': '继续已有清单及证据，不覆盖已经作出的内容决定'}
    task_id = uuid.uuid4().hex
    folder = Path(cfg.state_dir) / 'curation' / task_id
    plan = {'version': 1, 'kind': 'so2-curated-delivery', 'task_id': task_id,
            'input': cfg.video or cfg.video_dir, 'input_kind': 'file' if cfg.video else 'directory',
            'output_dir': cfg.output_dir,
            'state_dir': cfg.state_dir, 'date_zone': cfg.date_zone,
            'date': store.now(cfg).strftime('%Y-%m-%d'),
            'settings': {'min_kills': cfg.min_kills, 'actors': actors or [],
                         'grouping': 'round', 'categories': {}},
            'sources': [], 'clips': [], 'evidence_manifests': [], 'output_reviews': {}}
    for path in paths:
        sid = store.source_path_id(path)
        entry = results['videos'].get(sid, {})
        source = {'source_id': sid, 'path': path, 'identity': store.identity(path),
                  'review': {'status': 'pending', 'reason': '', 'evidence': [], 'unresolved': []}}
        try:
            source['media_info'] = media.probe(path)
            valid = (entry.get('identity') == source['identity'] and entry.get('status') == 'complete'
                     and entry.get('coverage') == [0., source['media_info']['duration']]
                     and entry.get('phase', {}).get('checked')
                     and entry.get('last_attempt', {}).get('status', 'complete') == 'complete')
            source['scan_valid'] = bool(valid)
            source['scan_fingerprint'] = entry.get('cfg_fingerprint') if valid else None
            snapshot = {k: entry[k] for k in ('identity', 'cfg_fingerprint', 'events', 'hits', 'review',
                        'perspective', 'phase', 'coverage', 'media_info', 'status') if valid and k in entry}
            snapshot_path = folder / 'sources' / f'{sid}.json'
            store.atomic_json(snapshot_path, snapshot)
            source['snapshot'] = str(snapshot_path)
            source['snapshot_sha256'] = store.file_sha256(snapshot_path)
            source['review_queue'] = []
            if valid:
                for clip in candidates(entry, cfg):
                    clip['source_id'] = sid
                    plan['clips'].append(clip)
                for item in entry.get('review', []):
                    source['review_queue'].append({'ts': item.get('ts', 0),
                          'reason': item.get('reason'), 'event_id': review.event_id(item)})
                for pulse in entry.get('perspective', {}).get('pulses', []):
                    nearby = [e for e in entry.get('events', [])
                              if e.get('view_id') == pulse['view_id'] and abs(e['ts'] - pulse['ts']) <= 1.1]
                    if len(nearby) != pulse['count']:
                        source['review_queue'].append({'ts': pulse['ts'], 'reason': 'counter_feed_mismatch',
                            'counter_increment': pulse['count'], 'recorded_events': len(nearby)})
            else:
                source['review_queue'].append({'ts': 0, 'reason': 'missing_complete_scan'})
            # 无候选也保留源级检查，不能把零疑点写成无遗漏。
            source['overview_times'] = [min(1., source['media_info']['duration'] * .1),
                                       source['media_info']['duration'] * .5,
                                       max(0., source['media_info']['duration'] - .2)]
            for i, item in enumerate(source['review_queue']):
                item.update(item_id=f'{sid[:12]}-{i + 1}', status='pending', resolution='', evidence=[])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            source.update(scan_valid=False, error=str(error), review_queue=[])
        prior = entry.get('curated_delivery')
        if prior:
            source['previous_delivery'] = prior
        plan['sources'].append(source)
    path = folder / 'plan.json'
    store.atomic_json(path, plan)
    for source in plan['sources']:
        index[source['source_id']] = {'plan': str(path), 'task_id': task_id,
                                     'source_identity': source['identity']}
    store.atomic_json(Path(cfg.state_dir) / 'curation' / 'index.json', index)
    return {'plan': str(path), 'task_id': task_id, 'sources': len(paths), 'candidates': len(plan['clips']),
            'next_action': '当前 Agent 逐源查看证据并补查遗漏，填写最终决定；不要交回用户审核'}


def make_evidence(plan_path, source_id=None, clip_id=None, times=None,
                  output_clip=False) -> EvidenceManifest:
    plan_path = Path(plan_path).resolve()
    plan = read(plan_path)
    clip = next((c for c in plan['clips'] if c['clip_id'] == clip_id), None)
    if clip_id and clip is None:
        raise ValueError('片段 ID 不存在')
    sid = clip['source_id'] if clip else source_id
    source = next((s for s in plan['sources'] if s['source_id'] == sid), None)
    if source is None:
        raise ValueError('需要本清单中的 source-id 或 clip-id')
    if output_clip:
        if clip is None:
            raise ValueError('成片证据需要 clip-id')
        saved = read(plan_path.parent / 'delivery.json')['items'][clip_id]
        path = saved['path']
        duration = media.probe(path)['duration']
        points = times or evidence.window_times(0, duration,
                   [k['ts'] - clip['start'] for k in clip.get('kills', [])])
    else:
        path = source['path']
        if store.identity(path) != source['identity']:
            raise ValueError('源录像变化，必须重新复核')
        points = times or (evidence.window_times(clip['start'], clip['end'],
                   [k['ts'] for k in clip['kills']] or clip.get('source_times', clip['scan_times'])) if clip else source['overview_times'])
    result = evidence.bundle(path, points, plan_path.parent / 'evidence')
    if output_clip:
        plan['output_reviews'][clip_id] = {'status': 'pending', 'reason': '',
              'identity': result['source_identity'], 'sha256': saved['sha256'],
              'evidence_manifest': result['manifest']}
    elif result['manifest'] not in plan['evidence_manifests']:
        plan['evidence_manifests'].append(result['manifest'])
    store.atomic_json(plan_path, plan)
    return result


def _source_frames(plan, source):
    frames = {}
    for path in plan.get('evidence_manifests', []):
        data = read(path)
        if data.get('source_identity') == source['identity']:
            _, items = evidence.read_bundle(path, source['identity'])
            frames.update(items)
    return frames


def _times(refs, frames):
    if not refs:
        raise ValueError('内容决定缺少实际画面依据')
    try:
        return [frames[str(Path(p).resolve())]['ts'] for p in refs]
    except KeyError as error:
        raise ValueError('画面未登记在本源证据清单') from error


def validate_plan(plan: DeliveryPlan) -> tuple[object, list[PreparedClip], list[dict], list[dict]]:
    if plan.get('version') != 1 or plan.get('kind') != 'so2-curated-delivery':
        raise ValueError('不支持的交付清单格式')
    cfg = _cfg(plan)
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', plan['date']):
        raise ValueError('交付日期格式无效')
    sources = {s['source_id']: s for s in plan['sources']}
    if len(sources) != len(plan['sources']) or not sources:
        raise ValueError('源清单为空或重复')
    clips = {c['clip_id']: c for c in plan['clips']}
    if len(clips) != len(plan['clips']):
        raise ValueError('clip_id 重复')
    prepared, unresolved, failed, frames_by_source = [], [], [], {}
    for sid, source in sources.items():
        input_path, source_path = Path(plan['input']).resolve(), Path(source['path']).resolve()
        if (source_path != input_path if plan['input_kind'] == 'file' else not source_path.is_relative_to(input_path)):
            raise ValueError('源录像超出本批输入范围')
        if sid != store.source_path_id(source['path']):
            raise ValueError('源 ID 与路径不一致')
        try:
            if source['identity'] != store.identity(source['path']):
                raise ValueError('源录像已变化')
            if source.get('snapshot'):
                if store.file_sha256(source['snapshot']) != source['snapshot_sha256']:
                    raise ValueError('原扫描快照已变化')
            frames_by_source[sid] = _source_frames(plan, source)
        except (OSError, ValueError) as error:
            failed.append({'source_id': sid, 'reason': str(error)})
        decision = source.get('review', {})
        if decision.get('status') != 'reviewed' or not decision.get('reason'):
            unresolved.append({'source_id': sid, 'reason': '源级完整范围及漏检检查尚未结束'})
        elif sid not in {f['source_id'] for f in failed}:
            _times(decision.get('evidence'), frames_by_source[sid])
        for item in source.get('review_queue', []):
            if item.get('status') != 'resolved' or not item.get('resolution'):
                unresolved.append({'source_id': sid, 'item_id': item.get('item_id'),
                                   'reason': item.get('reason', '扫描疑点未处理')})
            elif sid not in {f['source_id'] for f in failed}:
                _times(item.get('evidence'), frames_by_source[sid])
        unresolved.extend({'source_id': sid, 'reason': reason} for reason in decision.get('unresolved', []))
        if not source.get('scan_valid'):
            unresolved.append({'source_id': sid, 'reason': '缺少有效全长扫描，不能证明本源完整覆盖'})
    failed_ids = {f['source_id'] for f in failed}
    seen_kills = set()
    for clip in plan['clips']:
        sid, state = clip['source_id'], clip['status']
        if sid not in sources or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', clip['clip_id']):
            raise ValueError('片段身份无效')
        if state not in ('pending', 'unresolved', 'keep', 'reject', 'duplicate'):
            raise ValueError('片段状态无效')
        if state in ('pending', 'unresolved'):
            unresolved.append({'clip_id': clip['clip_id'], 'reason': clip.get('reason') or '片段待复核'})
            continue
        if not clip.get('reason'):
            raise ValueError('最终决定需要具体原因')
        if state == 'duplicate':
            retained = clips.get(clip.get('duplicate_of'))
            if not retained or retained['status'] != 'keep' or retained['clip_id'] == clip['clip_id']:
                raise ValueError('重复条目必须指向本清单保留片段')
            if sid not in failed_ids:
                _times(clip.get('boundary_evidence'), frames_by_source[sid])
            continue
        if state == 'reject' or sid in failed_ids:
            continue
        source = sources[sid]
        frames = frames_by_source[sid]
        start, end = clip['start'], clip['end']
        duration = source['media_info']['duration']
        if not all(isinstance(v, (float, int)) and math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= duration:
            raise ValueError('交付区间超出源录像')
        kills = clip.get('kills', [])
        if len(kills) < plan['settings']['min_kills'] or not clip.get('round_id') or not clip.get('actor'):
            raise ValueError('保留片段缺少同回合人物或未达到最低杀数')
        actors = plan['settings'].get('actors', [])
        if actors and clip['actor'].strip().casefold() not in {a.strip().casefold() for a in actors}:
            raise ValueError('保留片段人物不在本批目标集合')
        weapons = set()
        for kill in kills:
            ts = kill['ts']
            if not isinstance(ts, (float, int)) or not math.isfinite(ts) or not start <= ts < end:
                raise ValueError('击杀不在最终范围内')
            if str(kill.get('actor', '')).strip().casefold() != clip['actor'].strip().casefold() or kill.get('weapon') not in WEAPONS:
                raise ValueError('逐杀人物或武器类别未确认')
            weapons.add(kill['weapon'])
            points = _times(kill.get('evidence'), frames)
            if not any(ts - 2 <= t <= ts for t in points) or not any(ts < t <= ts + 2 for t in points):
                raise ValueError('逐杀依据需要动作前及结果后的源画面')
            token = (sid, clip['actor'], round(ts, 3))
            if token in seen_kills:
                raise ValueError('同源同人物同一击杀重复交付')
            seen_kills.add(token)
        points = _times(clip.get('boundary_evidence'), frames)
        if not any(abs(t - start) <= 1 for t in points) or not any(abs(t - end) <= 1 for t in points):
            raise ValueError('缺少最终首尾的源画面依据')
        category = next(iter(weapons)) if len(weapons) == 1 else '混合'
        category_name = _safe(plan['settings'].get('categories', {}).get(category, category))
        content_key = store.digest({'source': source['identity'], 'start': start, 'end': end,
                                   'kills': [{k: v for k, v in item.items() if k != 'evidence'} for item in kills],
                                   'encoder': 'libx264-fast-crf18-aac128-vfr'})
        name = f'{category_name}_{len(kills)}杀_{_safe(Path(source["path"]).stem)}_{start:.3f}-{end:.3f}_{content_key[:16]}.mp4'
        target = _within(Path(plan['output_dir']) / plan['date'] / category_name / name, plan['output_dir'])
        prepared.append({'clip': clip, 'source': source, 'key': content_key, 'category': category,
                         'target': str(target)})
    if len({p['target'] for p in prepared}) != len(prepared):
        raise ValueError('交付路径冲突')
    return cfg, prepared, unresolved, failed


def _move_owned(path, target, record, root):
    path = _within(path, root)
    if not _record_valid(path, record):
        raise ValueError('待移动文件身份变化，未覆盖')
    target = Path(target).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError('移动目标已存在，未覆盖')
    try:
        os.rename(path, target)
    except OSError as error:
        if error.errno != 18 and getattr(error, 'winerror', None) != 17:
            raise
        transfer = target.with_name(target.name + '.' + uuid.uuid4().hex + '.part')
        shutil.copy2(path, transfer)
        if store.file_sha256(transfer) != record['sha256']:
            raise ValueError('跨卷复制哈希不符')
        os.rename(transfer, target)
        path.unlink()
    return _record(target)


def _finish_move(move: PendingMove, root):
    """恢复 rename 成功但日志尚未提交的窗口，也覆盖跨卷复制后源被占用。"""
    source, target = _within(move['from'], root), Path(move['to']).resolve()
    if target.is_file():
        record = _record(target)
        if record['sha256'] != move['record']['sha256']:
            raise ValueError('未完成移动的目标内容冲突')
        if source.exists():
            if not _record_valid(source, move['record']):
                raise ValueError('移动源发生变化，未移除')
            source.unlink()
        return record
    return _move_owned(source, target, move['record'], root)


def _output_review(plan, clip_id, item):
    decision = plan.get('output_reviews', {}).get(clip_id, {})
    if decision.get('status') != 'confirmed' or not decision.get('reason'):
        return False
    if decision.get('identity') != item['identity'] or decision.get('sha256') != item['sha256']:
        return False
    _, frames = evidence.read_bundle(decision['evidence_manifest'], item['identity'])
    points = [v['ts'] for v in frames.values()]
    duration = item['end'] - item['start']
    return min(points) <= .5 and max(points) >= duration - .5


def deliver(plan_path):
    plan = read(plan_path)
    with workspace.writer(plan['state_dir']):
        return _deliver(plan_path)


def _prepare_frozen_execution(plan_path) -> DeliveryRun:
    """Validate and freeze the content decisions before execution begins."""
    plan_path = Path(plan_path).resolve()
    plan = read(plan_path)
    cfg, prepared, unresolved, failures = validate_plan(plan)
    _within(plan_path, Path(cfg.state_dir) / 'curation')
    folder = plan_path.parent
    journal_path = folder / 'delivery.json'
    journal = read(journal_path) if journal_path.is_file() else {'version': 1, 'items': {}, 'retired': []}
    if set(journal['items']) - {clip['clip_id'] for clip in plan['clips']}:
        raise ValueError('已有交付条目不能从清单静默删除，请明确 reject 或 duplicate 决定')

    run_id = uuid.uuid4().hex
    run_folder = workspace.run_dir(cfg, run_id)
    frozen = run_folder / 'delivery-plan.json'
    store.atomic_json(frozen, plan)
    store.atomic_json(run_folder / 'scan-index-before.json', store.load_results(cfg))
    journal.update(run_id=run_id, status='delivering', plan=str(plan_path), snapshot=str(frozen))
    store.atomic_json(journal_path, journal)
    return DeliveryRun(
        plan_path=plan_path, plan=plan, cfg=cfg, prepared=prepared,
        unresolved=unresolved, failures=failures, folder=folder,
        journal_path=journal_path, journal=journal, run_id=run_id,
        run_folder=run_folder, frozen=frozen,
        output_reviews=copy.deepcopy(plan.get('output_reviews', {})),
        current_ids={row['clip']['clip_id'] for row in prepared},
    )


def _execute_clip(run: DeliveryRun, row: PreparedClip) -> None:
    """Execute one keep decision; journal each recoverable step before moving on."""
    plan, cfg = run.plan, run.cfg
    journal, journal_path = run.journal, run.journal_path
    output_reviews = run.output_reviews
    clip, source, target = row['clip'], row['source'], Path(row['target'])
    cid = clip['clip_id']
    prior: JournalItem | None = journal['items'].get(cid)
    try:
        if prior and prior.get('pending_move'):
            move = prior['pending_move']
            _within(move['to'], plan['output_dir'])
            record = _finish_move(move, plan['output_dir'])
            prior.update(path=move['to'], **record)
            prior.pop('pending_move')
            store.atomic_json(journal_path, journal)
        if prior and prior.get('key') == row['key'] and prior.get('path') and _record_valid(prior['path'], prior):
            if Path(prior['path']).resolve() != target:
                if target.exists():
                    raise ValueError('分类目标已存在且没有本次移动记录')
                prior['pending_move'] = {'from': prior['path'], 'to': str(target),
                                         'record': {'identity': prior['identity'], 'sha256': prior['sha256']}}
                store.atomic_json(journal_path, journal)
                record = _finish_move(prior['pending_move'], plan['output_dir'])
                prior.update(path=str(target), **record)
                prior.pop('pending_move')
                output_reviews.pop(cid, None)
            item = dict(prior, status='reused', category=row['category'])
            item.pop('error', None)
        else:
            if target.exists():
                if not prior or prior.get('path') != str(target):
                    raise ValueError('目标已存在且没有本清单的恢复依据')
                # 本清单拥有的坏片／未完成发布先移入可恢复备份，再重新编码。
                backup = Path(cfg.state_dir) / 'backups' / 'delivery' / run.run_id / target.name
                _move_owned(target, backup, _record(target), plan['output_dir'])
            journal['items'][cid] = {'key': row['key'], 'path': str(target), 'status': 'encoding',
                                     'previous': prior}
            store.atomic_json(journal_path, journal)
            result = extraction.extract_segment(source['path'], clip['start'], clip['end'], target, cfg,
                                                len(clip['kills']), row['key'])
            if result.status == 'failed':
                raise ValueError(result.error)
            extraction.validate_clip(target, clip['end'] - clip['start'], source['media_info'], full_decode=True)
            item = {'key': row['key'], 'path': str(target), 'status': 'generated', **_record(target),
                    'category': row['category'], 'full_decode': True,
                    'source_id': source['source_id'], 'start': clip['start'], 'end': clip['end'],
                    'kill_count': len(clip['kills']), 'actor': clip['actor']}
            output_reviews.pop(cid, None)
            previous = prior.get('previous') if prior and prior.get('status') == 'encoding' else prior
            if previous and previous.get('path') != str(target):
                item['superseded'] = previous
        journal['items'][cid] = item
        store.atomic_json(journal_path, journal)

        previous = item.get('superseded')
        if previous:
            backup = Path(previous.get('backup') or Path(cfg.state_dir) / 'backups' / 'delivery' /
                          run.run_id / Path(previous['path']).name)
            _within(backup, Path(cfg.state_dir) / 'backups')
            previous['backup'] = str(backup)
            store.atomic_json(journal_path, journal)
            _finish_move({'from': previous['path'], 'to': str(backup), 'record': previous}, plan['output_dir'])
            journal['retired'].append(dict(previous, backup=str(backup)))
            item.pop('superseded', None)
            store.atomic_json(journal_path, journal)

        # 成片落盘后再制图；证据生成中断时可复用片段，不重复编码。
        decision = output_reviews.get(cid)
        if not decision or decision.get('identity') != item['identity']:
            duration = media.probe(item['path'])['duration']
            pack: EvidenceManifest = evidence.bundle(item['path'], evidence.window_times(
                0, duration, [kill['ts'] - clip['start'] for kill in clip['kills']]),
                run.folder / 'output-evidence')
            output_reviews[cid] = {'status': 'pending', 'reason': '', 'identity': item['identity'],
                                  'sha256': item['sha256'], 'evidence_manifest': pack['manifest'],
                                  'cards': pack['cards']}
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        run.failures.append({'clip_id': cid, 'reason': str(error)})
        journal['items'].setdefault(cid, {}).update(error=str(error))
    store.atomic_json(journal_path, journal)
    plan['output_reviews'] = output_reviews
    store.atomic_json(run.plan_path, plan)


def _retire_rejected_or_duplicate(run: DeliveryRun) -> None:
    """Back up excluded old outputs only after any retained duplicate is usable."""
    plan, journal, cfg = run.plan, run.journal, run.cfg
    for clip in plan['clips']:
        cid = clip['clip_id']
        if clip['status'] not in ('reject', 'duplicate') or cid not in journal['items']:
            continue
        prior = journal['items'][cid]
        if clip['status'] == 'duplicate':
            retained = journal['items'].get(clip['duplicate_of'], {})
            if clip['duplicate_of'] not in run.current_ids or not _record_valid(retained.get('path', ''), retained):
                run.failures.append({'clip_id': cid, 'reason': '保留版本尚未就位，旧片保留'})
                continue
        try:
            backup = Path(prior.get('backup') or Path(cfg.state_dir) / 'backups' / 'delivery' /
                          run.run_id / Path(prior['path']).name)
            _within(backup, Path(cfg.state_dir) / 'backups')
            prior['backup'] = str(backup)
            store.atomic_json(run.journal_path, journal)
            _finish_move({'from': prior['path'], 'to': str(backup), 'record': prior}, plan['output_dir'])
            journal['retired'].append(dict(prior, backup=str(backup)))
            del journal['items'][cid]
        except (OSError, ValueError) as error:
            run.failures.append({'clip_id': cid, 'reason': str(error)})


def _collect_outputs(run: DeliveryRun) -> list[dict]:
    """Collect only recorded outputs that still pass identity and content review."""
    outputs = []
    failed_clips = {failure.get('clip_id') for failure in run.failures}
    for row in run.prepared:
        cid = row['clip']['clip_id']
        item = run.journal['items'].get(cid, {})
        if cid in failed_clips or not _record_valid(item.get('path', ''), item):
            continue
        try:
            confirmed = _output_review(run.plan, cid, item)
        except (OSError, ValueError) as error:
            confirmed = False
            run.unresolved.append({'clip_id': cid, 'reason': str(error)})
        if not confirmed:
            run.unresolved.append({'clip_id': cid, 'reason': '成片画面尚未由 Agent 核对'})
        outputs.append(dict(item, clip_id=cid, content_review='confirmed',
                            output_review='confirmed' if confirmed else 'pending'))
    return outputs


def _commit_index_and_receipt(run: DeliveryRun, outputs: list[dict]) -> dict:
    """Commit the validated index, verify its readback, then record this run's receipt."""
    plan, cfg = run.plan, run.cfg
    journal, journal_path = run.journal, run.journal_path
    # 索引只消费本清单实际校验通过的成片；原 OCR 事件和历史回执保留。
    results = store.load_results(cfg)
    for source in plan['sources']:
        sid = source['source_id']
        entry = results['videos'].setdefault(sid, {'path': source['path'], 'identity': source['identity']})
        old_paths = {retired.get('path') for retired in journal['retired']}
        active = [output for output in outputs if output['source_id'] == sid]
        managed = {output['key'] for output in active}
        entry['clips'] = [clip for clip in entry.get('clips', [])
                          if clip.get('path') not in old_paths and clip.get('key') not in managed]
        entry['clips'].extend(active)
        entry['curated_delivery'] = {'plan': str(run.plan_path), 'task_id': plan['task_id'],
             'source_identity': source['identity'], 'journal': str(journal_path), 'run_id': run.run_id}
    store.save_results(cfg, results)
    loaded = store.load_results(cfg)
    for item in outputs:
        if not any(clip.get('path') == item['path'] and clip.get('key') == item['key']
                   for clip in loaded['videos'][item['source_id']]['clips']):
            raise ValueError('交付索引读回不一致')

    failures, unresolved = run.failures, run.unresolved
    code = 1 if failures else 3 if unresolved else 0
    receipt = {'version': 1, 'run_id': run.run_id, 'task_id': plan['task_id'], 'exit_code': code,
               'status': 'complete' if code == 0 else 'partial', 'plan': str(run.plan_path),
               'snapshot': str(run.frozen), 'outputs': outputs, 'unresolved': unresolved,
               'failures': failures, 'categories': dict(Counter(output['category'] for output in outputs)),
               'playback': 'not_performed', 'listening': 'not_performed',
               'user_acceptance': 'not_recorded'}
    receipt_path = run.run_folder / 'receipt.json'
    store.atomic_json(receipt_path, receipt)
    journal.update(status=receipt['status'], receipt=str(receipt_path), exit_code=code,
                   unresolved=unresolved, failures=failures)
    store.atomic_json(journal_path, journal)
    return {**receipt, 'receipt': str(receipt_path),
            'next_action': '检查 output_reviews 的成片画面并回填后再次 deliver；自行修复可解决的疑点，不等待用户继续'
                           if code else '本批交付完成，按回执汇报'}


def _deliver(plan_path) -> dict:
    """Run the fixed phases while the caller holds the state writer lock."""
    run = _prepare_frozen_execution(plan_path)
    for row in run.prepared:
        _execute_clip(run, row)
    _retire_rejected_or_duplicate(run)
    outputs = _collect_outputs(run)
    return _commit_index_and_receipt(run, outputs)


def status(plan_path):
    plan_path = Path(plan_path).resolve()
    plan = read(plan_path)
    journal = plan_path.parent / 'delivery.json'
    return {'plan': str(plan_path), 'task_id': plan['task_id'],
            'source_reviews': dict(Counter(s['review']['status'] for s in plan['sources'])),
            'clip_decisions': dict(Counter(c['status'] for c in plan['clips'])),
            'delivery': read(journal) if journal.is_file() else None,
            'next_action': '按当前清单继续证据复核／deliver，不按旧 OCR 分组重新导出'}
