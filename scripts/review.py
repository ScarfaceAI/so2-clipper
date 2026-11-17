"""保存视觉复核依据，并仅对同一素材、同一扫描版本更新确定性事件结论。"""
import copy
import json
from pathlib import Path

import cv2

import media
import perspective
import store


# 旧接口仅作兼容；当前逻辑统一使用文件哈希，不假设内容是图片。
image_hash = store.file_sha256


def event_id(event):
    return (event.get('ids') or [store.digest([event.get('signature'), event['ts'], event.get('reason')])[:16]])[0]


def frame_bundle(path, times, output):
    identity = store.identity(path)
    folder = Path(output).resolve()
    folder.mkdir(parents=True, exist_ok=True)
    frames = []
    duration = media.probe(path)['duration']
    for timestamp in sorted(set(float(t) for t in times)):
        if not 0 <= timestamp < duration:
            raise ValueError('复核画面时间超出源视频')
        frame = media.display_frame(path, timestamp)
        target = folder / f'{store.digest(identity)[:12]}_{timestamp:.3f}.jpg'
        cv2.imencode('.jpg', frame)[1].tofile(target)
        frames.append({'ts': timestamp, 'path': str(target),
                       'sha256': store.file_sha256(target)})
    if store.identity(path) != identity:
        raise ValueError('生成复核画面时源文件发生变化')
    manifest = {'version': 1, 'source_identity': identity, 'frames': frames}
    target = folder / 'frames.json'
    store.atomic_json(target, manifest)
    return target


def apply_decisions(entry, cfg, decisions_path):
    data = json.loads(Path(decisions_path).read_text(encoding='utf-8'))
    if data.get('source_identity') != store.identity(entry['path']) or data['source_identity'] != entry['identity']:
        raise ValueError('复核结论不属于当前源文件修订')
    if data.get('scan_fingerprint') != entry['cfg_fingerprint'] or store.cfg_fingerprint(cfg) != entry['cfg_fingerprint']:
        raise ValueError('复核结论的扫描参数或判定版本不匹配')
    decision_key = store.digest(data)
    if decision_key in entry.get('applied_decisions', []):
        return
    manifest_path = Path(data['evidence_manifest']).resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest.get('source_identity') != entry['identity']:
        raise ValueError('复核画面不属于当前素材')
    frames = {str(Path(item['path']).resolve()): item for item in manifest.get('frames', [])}
    prepared = copy.deepcopy(entry)
    events, doubts = prepared['events'], prepared['review']
    views = {v['id']: v for v in prepared.get('perspective', {}).get('views', [])}
    for decision in data.get('decisions', []):
        identifier = decision['event_id']
        source = next((e for e in events + doubts if identifier in e.get('ids', [event_id(e)])), None)
        if source is None:
            raise ValueError(f'复核事件不存在：{identifier}')
        evidence = []
        for filename in decision.get('evidence', []):
            item = frames.get(str(Path(filename).resolve()))
            if item is None:
                raise ValueError('复核画面未登记在本素材的画面清单中')
            path = Path(item['path'])
            if (path.suffix.lower() not in ('.jpg', '.jpeg', '.png', '.webp')
                    or not path.resolve().is_relative_to(manifest_path.parent)):
                raise ValueError('复核依据必须是画面清单目录内的图片')
            if not path.is_file() or store.file_sha256(path) != item['sha256']:
                raise ValueError('复核画面已变化或不可访问')
            evidence.append(item)
        if not evidence or not decision.get('reason', '').strip():
            raise ValueError('复核结论需要实际画面引用和具体原因')
        if decision['action'] not in ('confirm', 'reject'):
            raise ValueError('复核 action 只支持 confirm 或 reject')
        confirmed = None
        if decision['action'] == 'confirm':
            if source.get('count', 1) > 1 and (source.get('visual_role', '').startswith('counter')
                                              or source.get('signature', '').startswith('@counter')):
                raise ValueError('聚合计数不能确认为单次击杀，请在最终清单中逐次记录动作时间')
            view = views.get(decision.get('view_id'))
            if view is None:
                raise ValueError('复核结论缺少有效原视角段')
            visual = decision.get('visual_confirmed') is True
            if visual and (getattr(cfg, 'identity_mode', 'keywords') != 'auto'
                           or 'actor_named' not in view or not view.get('actor')):
                raise ValueError('匿名视觉确认需要有效的自动视角归属')
            killer = decision.get('killer') or source.get('killer') or ('' if visual else source.get('text'))
            victim = decision.get('victim') or source.get('victim') or ('' if visual else source['signature'].split('|')[1])
            if not visual and (not perspective.normalize(killer) or not perspective.normalize(victim)):
                raise ValueError('确认击杀必须读清杀者和受害者')
            if not visual and perspective.normalize(killer) == perspective.normalize(victim):
                raise ValueError('同名死亡仍须保持疑点，不能直接确认为有效击杀')
            if not (any(f['ts'] < source['ts'] for f in evidence)
                    and any(source['ts'] <= f['ts'] <= source['ts'] + 2 for f in evidence)):
                raise ValueError('确认需要事件前与事件出现后的源画面')
            delayed = bool(decision.get('delayed'))
            action_ts = decision.get('action_ts', source['ts'])
            if not view['start'] <= action_ts < view['end'] or action_ts > source['ts']:
                raise ValueError('先前动作不在所指定的原视角段中')
            if not delayed and not view['start'] <= source['ts'] < view['end']:
                raise ValueError('普通击杀发生时不属于所指定视角')
            if delayed and (source['ts'] - action_ts > 10 or not any(abs(f['ts'] - action_ts) <= .5 for f in evidence)):
                raise ValueError('延迟击杀需要 10 秒内的原动作画面依据')
            for interval in prepared.get('phase', {}).get('intervals', []):
                if interval['start'] <= source['ts'] and interval['end'] > action_ts:
                    raise ValueError('确认事件或延迟关联跨越了热身／阶段不明区间')
            actor = view['actor'] or perspective.normalize(killer)
            if not visual and view['actor'] and perspective.normalize(killer) not in view.get('aliases', [view['actor']]):
                # 只有显式视觉确认才扩展该视角的别名，不做相似名字自动认领。
                view.setdefault('aliases', []).append(perspective.normalize(killer))
            view['actor'] = actor
            confirmed = {k: v for k, v in source.items() if k not in ('reason', 'screenshot_error')}
            confirmed.update(ids=source.get('ids', [event_id(source)]), killer=killer, victim=victim,
                             signature=source['signature'] if visual else perspective.normalize(killer) + '|' + perspective.normalize(victim),
                             actor=actor, view_id=view['id'],
                             attribution={'method': 'visual_review', 'reason': decision['reason'],
                                          'evidence': evidence, 'action_ts': action_ts,
                                          'delayed_verified': delayed})
            if visual:
                confirmed['actor_named'] = view['actor_named']
                confirmed['attribution']['named_identity'] = view['actor_named']
        prepared.setdefault('review_history', []).append({'original': copy.deepcopy(source),
                      'decision': decision, 'evidence': evidence, 'at': store.now(cfg).isoformat()})
        events[:] = [e for e in events if identifier not in e.get('ids', [event_id(e)])]
        doubts[:] = [e for e in doubts if identifier not in e.get('ids', [event_id(e)])]
        if confirmed:
            events.append(confirmed)
    prepared['events'] = sorted(events, key=lambda e: e['ts'])
    prepared['hits'] = [e['ts'] for e in prepared['events']]
    prepared['origin'] = 'ocr_reviewed'
    prepared.setdefault('applied_decisions', []).append(decision_key)
    entry.clear()
    entry.update(prepared)
