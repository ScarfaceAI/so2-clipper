"""素材身份、版本化扫描结果与原子持久化。只在调用方完成落盘后清理断点。"""
import hashlib
import json
import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

SHANGHAI = timezone(timedelta(hours=8))
DETECTION_VERSION = 14


def now(cfg=None):
    if cfg is not None and getattr(cfg, 'date_zone', 'Asia/Shanghai') == 'local':
        return datetime.now().astimezone()
    return datetime.now(SHANGHAI)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode('utf-8')).hexdigest()


def source_path_id(path):
    """规范化路径的稳定 ID；不是文件内容哈希，也不是大小/mtime 修订。"""
    return digest(os.path.normcase(os.path.realpath(path)))


# 保留已有 Python 调用；落盘 ID 算法与已有扫描 key 完全一致。
media_id = source_path_id


def file_sha256(path):
    """读取任意文件的字节哈希，供图片、视频及 JSON 证据共用。"""
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def identity(path):
    p = Path(path).resolve()
    stat = p.stat()
    if not p.is_file():
        raise ValueError(f'不是文件：{p}')
    return {'path': os.path.normcase(str(p)), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def atomic_json(path, value):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    part = p.with_name(f'{p.name}.{os.getpid()}.part')
    with part.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(part, p)


def load_results(cfg):
    p = Path(cfg.result_file)
    if not p.exists():
        legacy = Path(cfg.output_dir) / 'scan_results.json'
        if legacy.is_file():
            raise ValueError('旧版扫描记录仍在交付目录，请先用 organize_output.py 迁移运行资料，避免重复扫描')
        return {'version': 3, 'updated_at': None, 'videos': {}, 'legacy_videos': {}}
    data = json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or not isinstance(data.get('videos'), dict):
        raise ValueError(f'扫描结果结构损坏，原文件保留：{p}')
    if data.get('version') == 3:
        return data
    if data.get('version') != 2:
        raise ValueError(f'不支持的结果版本：{data.get("version")}')
    legacy = data['videos']
    for entry in legacy.values():
        entry['source_available'] = Path(entry['path']).is_file()
    return {'version': 3, 'updated_at': data.get('updated_at'), 'videos': {},
            'legacy_videos': legacy, 'migration': {'from_version': 2}}


def save_results(cfg, results):
    p = Path(cfg.result_file)
    if p.exists() and results.get('migration') and not results['migration'].get('backup'):
        # 写新版前保存磁盘上的原始字节，迁移失败仍可恢复。
        backup = p.parent / 'backups' / f'scan_results_v2_{now():%Y%m%d-%H%M%S-%f}.json'
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, backup)
        results['migration']['backup'] = str(backup)
    results['updated_at'] = now().isoformat(timespec='seconds')
    atomic_json(p, results)


def scan_payload(cfg):
    import perspective
    engine = getattr(cfg, 'effective_engine', cfg.scan_engine)
    return {'detection_version': DETECTION_VERSION, 'profile': cfg.profile,
            'keywords': sorted(cfg.keywords), 'languages': cfg.languages,
            'region': cfg.region, 'base': [cfg.base_w, cfg.base_h],
            'max_x_center_ratio': cfg.max_x_center_ratio, 'engine': engine,
            'sampling': cfg.roi_fps if engine == 'ffmpeg' else cfg.scan_interval,
            'min_conf': cfg.min_conf, 'min_change_ratio': cfg.min_change_ratio,
            'cluster_window': cfg.cluster_window, 'sig_window': cfg.sig_window,
            'suppress_after_hit': cfg.suppress_after_hit,
            'warmup': {'enabled': cfg.filter_warmup, 'region': config.WARMUP_REGION,
                       'timer_region': config.WARMUP_TIMER_REGIONS.get(cfg.profile),
                       'interval': config.WARMUP_INTERVAL, 'min_conf': config.WARMUP_MIN_CONF,
                       'languages': ['ch_sim', 'en'] if 'ch_sim' not in cfg.languages else cfg.languages},
            **({'identity_mode': 'auto', 'perspective_version': perspective.VERSION}
               if getattr(cfg, 'identity_mode', 'keywords') == 'auto' else {})}


def cfg_fingerprint(cfg):
    return digest(scan_payload(cfg))


def cache_valid(entry, path, cfg):
    if not entry or entry.get('status') != 'complete' or entry.get('origin') == 'manual':
        return False
    if entry.get('last_attempt', {}).get('status') in ('failed', 'needs_review'):
        return False
    if cfg.filter_warmup and not entry.get('phase', {}).get('checked'):
        return False
    return (entry.get('identity') == identity(path)
            and entry.get('cfg_fingerprint') == cfg_fingerprint(cfg)
            and entry.get('coverage') == [0.0, cfg.media_info['duration']])


def record_scan(results, outcome, cfg):
    key = source_path_id(outcome.path)
    entry = results['videos'].get(key, {})
    curated = entry.get('curated_delivery')
    attempt = {'status': outcome.status, 'error': outcome.error,
               'at': now().isoformat(timespec='seconds')}
    if outcome.status == 'complete':
        entry = {'path': outcome.path, 'identity': outcome.identity,
                 'cfg_fingerprint': cfg_fingerprint(cfg), 'scan_settings': scan_payload(cfg),
                 'scanned_at': attempt['at'], 'status': 'complete', 'origin': outcome.origin,
                 'coverage': outcome.coverage, 'hits': outcome.hits, 'events': outcome.events,
                 'review': outcome.review, 'stats': outcome.stats,
                 'phase': outcome.phase,
                 'perspective': outcome.perspective,
                 'layout_evidence': getattr(cfg, 'layout_evidence', {}),
                 'media_info': cfg.media_info, 'clips': entry.get('clips', [])}
    elif entry.get('status') != 'complete':
        entry.update(path=outcome.path, status=outcome.status, review=outcome.review)
    entry['last_attempt'] = attempt
    if curated:
        entry['curated_delivery'] = curated
    results['videos'][key] = entry
    return entry


def run_status(cfg, phase, **details):
    """阶段快照不是进程存活证明；调用方仍应核对进程及匹配回执。"""
    import workspace
    path = workspace.run_dir(cfg) / 'status.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    data.update(run_id=cfg.current_run_id, pid=os.getpid(), phase=phase,
                updated_at=now(cfg).isoformat(timespec='seconds'), **details)
    atomic_json(path, data)
    return path


def progress_path(cfg, path, start, end):
    token = digest([float(start), float(end)])[:16]
    return Path(cfg.tmp_dir) / f'scan_{source_path_id(path)}_{token}.json'


def load_progress(cfg, path, start, end):
    p = progress_path(cfg, path, start, end)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if (data.get('identity') != identity(path)
            or data.get('fingerprint') != cfg_fingerprint(cfg)):
        return None
    return data


def save_progress(cfg, path, start, end, state):
    atomic_json(progress_path(cfg, path, start, end),
                dict(state, identity=identity(path), fingerprint=cfg_fingerprint(cfg)))


def clear_progress(cfg, path):
    root = Path(cfg.tmp_dir).resolve()
    for p in root.glob(f'scan_{source_path_id(path)}_*.json'):
        if p.resolve().is_relative_to(root):
            p.unlink()
