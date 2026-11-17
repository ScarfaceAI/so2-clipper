"""扫描与候选提取入口；内容复核后的最终交付由 delivery 执行。"""
import logging
import sys
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import config
import core
import media
import reporting
import store
import workspace
import autolayout
import extraction
from options import RunContext, ResolvedSource, copy_options


def _utf8_stdio():
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8')


def scan_all(cfg, jobs):
    """按完成顺序产出；主进程先落盘，再接收下一个结果。"""
    with ProcessPoolExecutor(max_workers=cfg.workers) as pool:
        tasks = {}
        for path, resolved in jobs:
            worker_cfg = copy_options(resolved, scan_segments=1)
            tasks[pool.submit(core.scan_video, path, worker_cfg)] = (path, resolved)
        for task in as_completed(tasks):
            path, resolved = tasks[task]
            try:
                outcome = task.result()
            except Exception as error:
                outcome = core.ScanResult(path, 'failed', error=f'扫描进程失败：{error}')
            yield outcome, resolved


def _file_result(outcome, cfg, reused=False):
    return {'path': outcome.path, 'status': outcome.status, 'error': outcome.error,
            'profile': cfg.profile, 'hit_count': len(outcome.hits), 'review': outcome.review,
            'scan_stats': outcome.stats, 'phase': outcome.phase, 'perspective': outcome.perspective,
            'reused_scan': reused, 'clips': []}


def _run_context(options):
    return RunContext.from_options(options, current_run_id=options.run_id or uuid.uuid4().hex,
                                   run_date=store.now(options).strftime('%Y-%m-%d'), merge_result={})


def run(options):
    cfg = options if isinstance(options, RunContext) and options.current_run_id else _run_context(options)
    results = store.load_results(cfg)
    core.setup_logging(cfg)
    started = time.monotonic()
    receipt_path = workspace.run_dir(cfg) / 'receipt.json'
    if receipt_path.exists():
        raise ValueError('本次 run-id 已有回执，请使用新的 run-id，原回执保留')
    if not cfg.video and not cfg.video_dir:
        raise ValueError('请通过 --video 或 --video-dir 提供输入')
    videos = [str(Path(cfg.video).resolve())] if cfg.video else core.discover_videos(cfg)
    if not videos:
        raise ValueError(f'未找到视频：{cfg.video_dir}')
    store.run_status(cfg, 'scanning', selected=videos, scanned_files=0, total_files=len(videos))
    target = '自动识别当前视角人物' if cfg.identity_mode == 'auto' else '目标 ID：' + ', '.join(cfg.keywords)
    print(f'本次 {len(videos)} 个视频；{target}；'
          f'连续多杀间隔 ≤{cfg.kill_gap:g}s，至少 {cfg.min_kills} 杀。', flush=True)
    logging.info('开始本次任务：%s', videos)
    files, jobs, configs = {}, [], {}

    def accept(outcome, resolved):
        store.record_scan(results, outcome, resolved)
        store.save_results(cfg, results)
        if outcome.status == 'complete':
            store.clear_progress(cfg, outcome.path)
        files[outcome.path] = _file_result(outcome, resolved)
        store.run_status(cfg, 'scanning', scanned_files=len(files), current_source=outcome.path)
        if outcome.error:
            print(f'{outcome.status}：{Path(outcome.path).name}：{outcome.error}', flush=True)
        logging.info('扫描结果：%s %s %s', outcome.path, outcome.status, outcome.error or len(outcome.hits))

    for path in videos:
        try:
            existing = results['videos'].get(store.source_path_id(path), {})
            if existing.get('curated_delivery') and not cfg.scan_only:
                outcome = core.ScanResult(path, 'needs_review', error='已有最终修订清单；请按 curated_delivery.plan 继续 deliver，禁止重新按旧事件分组')
                files[path] = _file_result(outcome, cfg)
                files[path]['curated_delivery'] = existing['curated_delivery']
                continue
            if cfg.hits is not None:
                resolved = ResolvedSource.from_options(cfg, path=path, media_info=media.probe(path),
                                                      profile='manual', effective_engine='manual')
                hits = sorted(float(t.strip()) for t in cfg.hits.split(',') if t.strip())
                if any(t >= resolved.media_info['duration'] for t in hits):
                    raise ValueError('手动击杀时间超出视频时长')
                outcome = core.ScanResult(path, 'complete', hits=hits, identity=store.identity(path),
                                          coverage=[0.0, resolved.media_info['duration']], origin='manual')
                accept(outcome, resolved)
            else:
                entry = results['videos'].get(store.source_path_id(path))
                layout_evidence = entry.get('layout_evidence', {}) if entry and entry.get('identity') == store.identity(path) else None
                resolved = autolayout.resolve_source(cfg, path, layout_evidence=layout_evidence)
                if not cfg.rescan and store.cache_valid(entry, path, resolved):
                    outcome = core.ScanResult(path, 'complete', hits=entry['hits'], events=entry.get('events', []),
                                              review=entry.get('review', []), stats=entry.get('stats', {}),
                                              phase=entry.get('phase', {}), perspective=entry.get('perspective', {}))
                    files[path] = _file_result(outcome, resolved, reused=True)
                    print(f'复用扫描：{Path(path).name}，{len(outcome.hits)} 杀', flush=True)
                elif cfg.extract_only:
                    accept(core.ScanResult(path, 'failed', error='没有当前素材及参数的有效扫描结果；请先去掉 --extract-only 扫描'), resolved)
                else:
                    jobs.append((path, resolved))
            configs[path] = resolved
        except media.UnknownProfile as error:
            outcome = core.ScanResult(path, 'needs_review', error=str(error),
                                      review=[{'ts': 0.0, 'reason': 'unknown_profile', 'detail': str(error)}])
            accept(outcome, cfg)
        except (OSError, ValueError) as error:
            accept(core.ScanResult(path, 'failed', error=str(error)), cfg)

    if cfg.video and jobs:
        path, resolved = jobs[0]
        accept(core.scan_video(path, resolved), resolved)
    elif jobs:
        for outcome, resolved in scan_all(cfg, jobs):
            accept(outcome, resolved)

    stage = workspace.run_dir(cfg) / 'scan-stage.json'
    store.atomic_json(stage, {'run_id': cfg.current_run_id, 'stage': 'scan_finished',
        'files': [{'path': path, 'status': files[path]['status'],
                   'identity': results['videos'].get(store.source_path_id(path), {}).get('identity'),
                   'coverage': results['videos'].get(store.source_path_id(path), {}).get('coverage'),
                   'fingerprint': results['videos'].get(store.source_path_id(path), {}).get('cfg_fingerprint')}
                  for path in videos]})
    store.run_status(cfg, 'scan_finished', scanned_files=len(files), scan_stage=str(stage))

    if not cfg.scan_only:
        store.run_status(cfg, 'extracting')
        for path in videos:
            file = files[path]
            if file['status'] != 'complete':
                continue
            entry = results['videos'][store.source_path_id(path)]
            try:
                if cfg.decisions:
                    import review
                    review.apply_decisions(entry, configs[path], cfg.decisions)
                    file['review'], file['hit_count'] = entry['review'], len(entry['hits'])
                    file['perspective'] = entry.get('perspective', {})
                    store.save_results(cfg, results)
                options = {}
                if cfg.identity_mode == 'auto' and entry.get('origin') != 'manual':
                    options = {'events': entry.get('events', []),
                               'views': entry.get('perspective', {}).get('views', [])}
                clips = extraction.extract_clips(path, entry['hits'], configs[path], entry.get('clips', []),
                                           phase_intervals=entry.get('phase', {}).get('intervals', []), **options)
                file['clips'] = [asdict(clip) for clip in clips]
                for clip in clips:
                    if clip.status == 'failed':
                        print(f'提取失败：{Path(path).name} {clip.start:.3f}～{clip.end:.3f}s：{clip.error}', flush=True)
                history = {c['key']: c for c in entry.get('clips', []) if c.get('key')}
                history.update({c['key']: c for c in file['clips'] if c.get('key')})
                entry['clips'] = list(history.values())
                entry['segments'] = [[c.start, c.end] for c in clips if c.status in ('generated', 'reused')]
                store.save_results(cfg, results)
            except (OSError, ValueError) as error:
                file.update(status='failed', error=f'提取失败：{error}')
                entry['extraction_error'] = str(error)
                store.save_results(cfg, results)

    ordered = [files[path] for path in videos]
    if cfg.merge and not cfg.scan_only:
        import montage
        cfg.merge_result = asdict(montage.merge_clips(ordered, cfg, results.get('montages', [])))
        if cfg.merge_result['status'] in ('generated', 'reused'):
            old = {item['key']: item for item in results.get('montages', [])}
            old[cfg.merge_result['key']] = cfg.merge_result
            results['montages'] = list(old.values())
    store.run_status(cfg, 'reporting')
    code, report_path = reporting.write_report(cfg, ordered, time.monotonic() - started)
    results['last_run'] = {'at': store.now().isoformat(timespec='seconds'), 'exit_code': code,
                           'run_id': cfg.current_run_id, 'receipt': str(receipt_path),
                           'selected': [store.source_path_id(path) for path in videos], 'report': str(report_path)}
    store.atomic_json(receipt_path, {'version': 1, 'run_id': cfg.current_run_id, 'exit_code': code,
                      'completed_at': store.now(cfg).isoformat(timespec='seconds'),
                      'selected': videos, 'files': ordered, 'report': str(report_path),
                      'output_dir': cfg.output_dir, 'state_dir': cfg.state_dir,
                      'merge': getattr(cfg, 'merge_result', None),
                      'parameters': {'identity_mode': cfg.identity_mode, 'pre': cfg.pre, 'post': cfg.post,
                                     'kill_gap': cfg.kill_gap, 'min_kills': cfg.min_kills,
                                     'date_zone': cfg.date_zone}})
    store.save_results(cfg, results)
    store.run_status(cfg, 'finished', exit_code=code, receipt=str(receipt_path),
                     content_review='not_performed_by_scanner')
    logging.info('本次结束：退出码 %s，清单 %s', code, report_path)
    return code


def main(argv=None):
    _utf8_stdio()
    cfg = config.load_config(argv)
    try:
        with workspace.writer(cfg.state_dir):
            cfg = _run_context(cfg)
            return run(cfg)
    except (OSError, ValueError) as error:
        print(f'任务失败，已有结果保留：{error}', file=sys.stderr, flush=True)
        run_id = getattr(cfg, 'current_run_id', None)
        if run_id:
            try:
                store.run_status(cfg, 'failed', error=str(error))
            except OSError:
                pass
            receipt = workspace.run_dir(cfg, run_id) / 'receipt.json'
            if not receipt.exists():
                try:
                    store.atomic_json(receipt, {'version': 1, 'run_id': run_id, 'exit_code': 1,
                                      'error': str(error), 'selected': [cfg.video] if cfg.video else [],
                                      'requested_directory': cfg.video_dir, 'files': [], 'report': None})
                except OSError:
                    pass  # 输出目录不可写时只能向 stderr 返回原始失败。
        return 1


if __name__ == '__main__':
    sys.exit(main())
