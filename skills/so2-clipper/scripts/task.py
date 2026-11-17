"""Skill 的薄适配层；识别、缓存、提取仍由唯一引擎执行。"""
import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from collections import Counter
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
ENGINE = SKILL / 'runtime' / 'scripts'
if not ENGINE.is_dir():
    ENGINE = SKILL.parents[1] / 'scripts'
if not (ENGINE / 'kill_clipper.py').is_file():
    raise RuntimeError('Skill 运行时缺失，请重新解压完整 ZIP')
sys.path.insert(0, str(ENGINE))
import config
import store
import workspace


def runtime_file():
    return Path(os.environ.get('SO2_CLIPS_RUNTIME') or Path(os.environ['LOCALAPPDATA']) / 'SO2KillClips/runtime.json')


def preferences():
    path = runtime_file().with_name('preferences.json')
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}


def runtime_settings():
    return json.loads(runtime_file().read_text(encoding='utf-8'))


def output_root(source, explicit=None, prefs=None):
    if explicit:
        return str(Path(explicit).resolve())
    prefs = preferences() if prefs is None else prefs
    return str(Path(prefs.get('output_dir') or
                    (source.parent if source.is_file() else source) / 'output').resolve())


def input_path(value):
    path = Path(value).resolve()
    if not path.exists():
        raise ValueError(f'输入不可访问：{path}')
    return path


def build_parser():
    parser = argparse.ArgumentParser(
        description='SO2 击杀 Skill：run 负责扫描和候选提取；完整交付依赖 prepare、evidence、Agent 内容判断、deliver 与成片复核。status 用于续办。')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    for command in ('inspect', 'run', 'review', 'prepare'):
        p = sub.add_parser(command, add_help=command != 'run')
        p.add_argument('--input', required=True)
        p.add_argument('--output-dir')
        if command == 'prepare':
            p.add_argument('--state-dir')
            p.add_argument('--actors', default='', help='本批指定人物，逗号分隔；缺省当前视角全部人物')
            p.add_argument('--min-kills', type=int, default=2)
    p = sub.add_parser('deliver')
    p.add_argument('--plan', required=True)
    p = sub.add_parser('evidence')
    p.add_argument('--plan', required=True)
    p.add_argument('--source-id')
    p.add_argument('--clip-id')
    p.add_argument('--times', help='源秒数，逗号分隔；缺省生成当前候选范围的导航图')
    p.add_argument('--output-clip', action='store_true')
    p = sub.add_parser('status')
    p.add_argument('--plan')
    p.add_argument('--input')
    p.add_argument('--output-dir')
    p.add_argument('--state-dir')
    p.add_argument('--run-id')
    p = sub.add_parser('frames')
    p.add_argument('--input', required=True)
    p.add_argument('--times', required=True)
    p.add_argument('--output', required=True)
    p = sub.add_parser('settings')
    p.add_argument('--output-dir')
    p.add_argument('--date-zone', choices=['local', 'Asia/Shanghai'])
    p = sub.add_parser('remember')
    p.add_argument('--source', required=True)
    p.add_argument('--profile', choices=list(config.PROFILES), required=True)
    p.add_argument('--width', type=int, required=True)
    p.add_argument('--height', type=int, required=True)
    return parser


def validate_extra_args(args, extra, parser):
    if args.command == 'run' and any(value.split('=')[0] in ('--video', '--video-dir', '--run-id', '--source-registry') for value in extra):
        parser.error('输入范围和任务标识由 --input 与入口管理，不能用附加参数覆盖')
    if args.command != 'run' and extra:
        parser.error('不支持的参数：' + ' '.join(extra))
def handle_doctor():
    import easyocr
    import torch
    import cv2
    runtime = runtime_settings()
    tools = {name: subprocess.run([name, '-version'], capture_output=True, check=True,
                                  text=True, encoding='utf-8', errors='replace').stdout.splitlines()[0]
             for name in ('ffmpeg', 'ffprobe')}
    emit({'python': sys.executable, 'easyocr': easyocr.__version__, 'torch': torch.__version__,
          'opencv': cv2.__version__, 'gpu': runtime.get('gpu', False) and torch.cuda.is_available(), 'media': tools,
          'runtime': str(runtime_file()), 'engine': str(ENGINE)})
    return 0


def save_preferences(prefs):
    target = runtime_file().with_name('preferences.json')
    store.atomic_json(target, prefs)
    emit({'preferences': str(target), 'settings': prefs})


def handle_settings(args):
    prefs = preferences()
    if args.output_dir:
        prefs['output_dir'] = str(Path(args.output_dir).resolve())
    if args.date_zone:
        prefs['date_zone'] = args.date_zone
    save_preferences(prefs)
    return 0


def handle_remember(args):
    prefs = preferences()
    source = input_path(args.source)
    if not source.is_dir() or min(args.width, args.height) < 1:
        raise ValueError('长期来源需要可访问的目录和有效显示尺寸')
    item = {'source': str(source), 'width': args.width, 'height': args.height, 'profile': args.profile}
    saved = [v for v in prefs.get('sources', []) if (v['source'], v['width'], v['height']) !=
             (item['source'], item['width'], item['height'])]
    prefs['sources'] = saved + [item]
    save_preferences(prefs)
    return 0


def build_engine_args(source, output_dir, prefs, extra=()):
    default_mode = 'keywords' if any(v.split('=')[0] == '--keywords' for v in extra) else 'auto'
    argv_engine = ['--video' if source.is_file() else '--video-dir', str(source), '--output-dir', output_dir,
                   '--identity-mode', default_mode, '--date-zone', prefs.get('date_zone', 'local')]
    if prefs.get('sources'):
        argv_engine += ['--source-registry', str(runtime_file().with_name('preferences.json'))]
    return argv_engine


def build_input_engine_args(args, extra=(), prefs=None):
    prefs = preferences() if prefs is None else prefs
    source = input_path(args.input)
    root = output_root(source, args.output_dir, prefs)
    return source, root, build_engine_args(source, root, prefs, extra)


def apply_gpu_policy(argv_engine, extra):
    import torch
    runtime = runtime_settings()
    if not runtime.get('gpu', False) or not torch.cuda.is_available() or '--no-gpu' in extra:
        argv_engine += ['--no-gpu', '--workers', '1', '--scan-segments', '1']


def handle_deliver(args):
    import delivery
    result = delivery.deliver(args.plan)
    emit(result)
    return result['exit_code']


def handle_evidence(args):
    import delivery
    times = [float(t) for t in args.times.split(',')] if args.times else None
    emit(delivery.make_evidence(args.plan, args.source_id, args.clip_id, times, args.output_clip))
    return 0


def handle_status(args, parser):
    import delivery
    if args.plan:
        emit(delivery.status(args.plan))
    elif args.input:
        source = input_path(args.input)
        argv_status = ['--video' if source.is_file() else '--video-dir', str(source),
                       '--output-dir', output_root(source, args.output_dir), '--identity-mode', 'auto']
        if args.state_dir:
            argv_status += ['--state-dir', args.state_dir]
        cfg = config.load_config(argv_status)
        data = store.load_results(cfg)
        active_reviews = delivery.review_index(cfg)
        import core
        paths = [str(source)] if source.is_file() else core.discover_videos(cfg)
        emit({'input': str(source), 'state_dir': cfg.state_dir,
              'sources': [{'path': p, 'scan_status': data['videos'].get(store.source_path_id(p), {}).get('status'),
                           'curated_delivery': data['videos'].get(store.source_path_id(p), {}).get('curated_delivery'),
                           'active_review': active_reviews.get(store.source_path_id(p))}
                          for p in paths],
              'next_action': '有 active_review 或 curated_delivery 时先 status --plan 续办；其余源按需扫描再 prepare'})
    elif args.state_dir and args.run_id:
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.run_id):
            parser.error('run-id 格式无效')
        folder = Path(args.state_dir) / 'runs' / args.run_id
        emit({'saved_stage': delivery.read(folder / 'status.json'),
              'receipt': delivery.read(folder / 'receipt.json') if (folder / 'receipt.json').is_file() else None,
              'liveness': '阶段为落盘快照；未完成时另查实际进程'})
    else:
        parser.error('status 需要 --input、--plan 或同时提供 --state-dir、--run-id')
    return 0


def handle_frames(args):
    source = input_path(args.input)
    import review
    emit({'manifest': str(review.frame_bundle(source, args.times.split(','), args.output))})
    return 0


def handle_prepare(args, extra=()):
    _, _, argv_engine = build_input_engine_args(args, extra)
    if args.state_dir:
        argv_engine += ['--state-dir', args.state_dir]
    argv_engine += ['--min-kills', str(args.min_kills)]
    import delivery
    emit(delivery.prepare(config.load_config(argv_engine),
                         [v.strip() for v in args.actors.split(',') if v.strip()]))
    return 0


def handle_run(args, extra):
    source, root, argv_engine = build_input_engine_args(args, extra)
    if os.environ.get('SO2_CLIPS_OFFLINE') == '1':
        os.environ['SO2_CLIPS_OFFLINE'] = '1'
    apply_gpu_policy(argv_engine, extra)
    if '--help' in extra or '-h' in extra:
        config.build_parser().print_help()
        return 0
    run_id = uuid.uuid4().hex
    argv_engine += extra + ['--run-id', run_id]
    cfg = config.load_config(argv_engine)
    log_path = workspace.run_dir(cfg, run_id) / 'task.log'
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f'任务 {run_id} 运行中；日志：{log_path}', flush=True)
    with log_path.open('w', encoding='utf-8') as stream:
        code = subprocess.run([sys.executable, str(ENGINE / 'kill_clipper.py'), *argv_engine],
                              stdout=stream, stderr=subprocess.STDOUT).returncode
    receipt = workspace.run_dir(cfg, run_id) / 'receipt.json'
    if not receipt.is_file():
        if code == 2:
            emit({'run_id': run_id, 'exit_code': 2, 'log': str(log_path), 'status': 'argument_error'})
            return 2
        raise ValueError(f'本次未生成匹配回执：{run_id}，引擎退出码 {code}')
    data = json.loads(receipt.read_text(encoding='utf-8'))
    if data.get('run_id') != run_id or data.get('exit_code') != code:
        raise ValueError('回执与本次执行不一致')
    if any((Path(p).resolve() != source if source.is_file() else
            not Path(p).resolve().is_relative_to(source)) for p in data.get('selected', [])):
        raise ValueError('回执中的素材超出本次输入范围')
    brief = []
    for file in data.get('files', []):
        brief.append({key: file.get(key) for key in ('path', 'status', 'error', 'hit_count', 'reused_scan')})
        brief[-1].update(clips=[c for c in file.get('clips', []) if c['status'] in ('generated', 'reused')],
                         filtered=sum(c['status'] == 'filtered' for c in file.get('clips', [])),
                         review_count=len(file.get('review', [])),
                         review_reasons=dict(Counter(r['reason'] for r in file.get('review', []))))
        if file.get('curated_delivery'):
            brief[-1]['curated_delivery'] = file['curated_delivery']
    emit({'run_id': run_id, 'exit_code': code, 'receipt': str(receipt), 'report': data.get('report'),
          'log': str(log_path), 'output_dir': root, 'state_dir': cfg.state_dir,
          'files': brief, 'merge': data.get('merge')})
    return code


def handle_review(args, extra=()):
    source, _, argv_engine = build_input_engine_args(args, extra)
    apply_gpu_policy(argv_engine, extra)
    cfg = config.load_config(argv_engine)
    if not source.is_file():
        raise ValueError('逐事件复核需要单个视频')
    entry = store.load_results(cfg)['videos'].get(store.source_path_id(source))
    if not entry or entry.get('identity') != store.identity(source):
        raise ValueError('没有当前文件修订的扫描结果')
    import review
    emit({'source_identity': entry['identity'], 'scan_fingerprint': entry['cfg_fingerprint'],
          'views': entry.get('perspective', {}).get('views', []),
          'review': [dict(e, event_id=review.event_id(e)) for e in entry.get('review', [])]})
    return 0


def handle_inspect(args, extra=()):
    source, _, argv_engine = build_input_engine_args(args, extra)
    apply_gpu_policy(argv_engine, extra)
    cfg = config.load_config(argv_engine)
    import core
    import autolayout
    results = []
    for path in ([str(source)] if source.is_file() else core.discover_videos(cfg)):
        try:
            resolved = autolayout.resolve_source(cfg, path)
            evidence = getattr(resolved, 'layout_evidence', {})
            results.append({'path': path, 'status': 'layout_selected', 'profile': resolved.profile,
                            'display': resolved.media_info,
                            'images': [item['image'] for item in evidence.get('observations', [])]})
        except (OSError, ValueError) as error:
            results.append({'path': path, 'status': 'needs_review', 'error': str(error)})
    emit({'input': str(source), 'files': results})
    if not results:
        raise ValueError('所选输入没有可处理的视频')
    return 3 if any(r['status'] == 'needs_review' for r in results) else 0


def dispatch(args, extra, parser):
    if args.command == 'doctor':
        return handle_doctor()
    if args.command == 'settings':
        return handle_settings(args)
    if args.command == 'remember':
        return handle_remember(args)
    if args.command == 'status':
        return handle_status(args, parser)
    if args.command == 'run':
        return handle_run(args, extra)
    if args.command == 'prepare':
        return handle_prepare(args, extra)
    if args.command == 'evidence':
        return handle_evidence(args)
    if args.command == 'deliver':
        return handle_deliver(args)
    if args.command == 'frames':
        return handle_frames(args)
    if args.command == 'review':
        return handle_review(args, extra)
    if args.command == 'inspect':
        return handle_inspect(args, extra)
    raise ValueError(f'不支持的命令：{args.command}')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv in (['run', '--help'], ['run', '-h']):
        config.build_parser().print_help()
        return 0
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    validate_extra_args(args, extra, parser)
    return dispatch(args, extra, parser)


def emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'任务未完成：{error}', file=sys.stderr)
        sys.exit(1)
