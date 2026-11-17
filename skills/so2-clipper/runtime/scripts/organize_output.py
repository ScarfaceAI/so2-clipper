"""在明确要求整理输出时运行。预览后迁移辅助资料，日期下的成片保持原位。"""
import argparse
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import config
import store


def file_hash(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def plan(cfg):
    output, state = Path(cfg.output_dir).resolve(), Path(cfg.state_dir).resolve()
    moves = []
    for source in sorted(output.rglob('*')):
        if not source.is_file():
            continue
        if not source.resolve().is_relative_to(output):
            raise ValueError(f'输出目录中有指向外部的链接：{source}')
        relative = source.relative_to(output)
        if (len(relative.parts) == 2 and re.fullmatch(r'\d{4}-\d{2}-\d{2}', relative.parts[0])
                and source.suffix.lower() == '.mp4' and not source.name.endswith('.part.mp4')):
            continue
        if relative.parts[0] in ('scan_results.json', 'scan.log', 'tmp', 'backups', 'runs'):
            target = state / relative
        else:
            target = state / 'legacy_output' / relative
        if not target.resolve().is_relative_to(state) or target.exists():
            raise ValueError(f'迁移目标越界或已存在，未移动任何文件：{target}')
        moves.append({'from': str(source), 'to': str(target), 'sha256': file_hash(source)})
    return moves


def organize(cfg, apply=False):
    output, state = Path(cfg.output_dir).resolve(), Path(cfg.state_dir).resolve()
    if not output.is_dir() or output == Path(output.anchor):
        raise ValueError('需要明确、可访问的输出文件夹')
    if output.is_relative_to(state) or state.is_relative_to(output):
        raise ValueError('运行目录与交付目录不能互相包含')
    moves = plan(cfg)
    result = {'output_dir': str(output), 'state_dir': str(state), 'applied': False, 'moves': moves}
    if not apply or not moves:
        return result
    folder = state / 'migrations' / store.now(cfg).strftime('%Y%m%d-%H%M%S-%f')
    folder.mkdir(parents=True)
    receipt = folder / 'receipt.json'
    result['receipt'] = str(receipt)
    store.atomic_json(receipt, result)
    # 所有目标先检查完；按文件移动，迁移成功和中途失败均有可定位的路径。
    for item in moves:
        source, target = Path(item['from']), Path(item['to'])
        if file_hash(source) != item['sha256']:
            raise ValueError(f'迁移期间源文件变化：{source}')
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        if file_hash(target) != item['sha256']:
            raise ValueError(f'迁移后文件哈希不符：{target}')
        item['moved'] = True
        store.atomic_json(receipt, result)

    pairs = [(item['from'], item['to']) for item in moves]
    for name in ('tmp', 'backups', 'runs'):
        pairs.append((str(output / name), str(state / name)))
    # 当前扫描中的截图、布局证据和已有报告均指向新地址；原始文本另存备份。
    replacements = []
    for old, new in pairs:
        replacements += [(old, new), (Path(old).as_posix(), Path(new).as_posix())]
    replacements.sort(key=lambda pair: len(pair[0]), reverse=True)

    def rewrite(value):
        if isinstance(value, str):
            for old, new in replacements:
                value = re.sub(re.escape(old), lambda _: new, value,
                               flags=re.IGNORECASE if os.name == 'nt' else 0)
            return value
        if isinstance(value, list):
            return [rewrite(v) for v in value]
        if isinstance(value, dict):
            return {k: rewrite(v) for k, v in value.items()}
        return value

    for item in moves:
        target = Path(item['to'])
        if target.suffix.lower() not in ('.json', '.md') or 'backups' in target.relative_to(state).parts:
            continue
        original = target.read_text(encoding='utf-8')
        value = json.loads(original) if target.suffix.lower() == '.json' else original
        revised = rewrite(value)
        if revised == value:
            continue
        backup = folder / 'originals' / target.relative_to(state)
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, backup)
        if target.suffix.lower() == '.json':
            store.atomic_json(target, revised)
        else:
            target.write_text(revised, encoding='utf-8')
        item['reference_updated'] = True
        item['original_backup'] = str(backup)
    for directory in sorted((p for p in output.rglob('*') if p.is_dir()),
                            key=lambda p: len(p.parts), reverse=True):
        if not directory.resolve().is_relative_to(output):
            raise ValueError('空目录清理路径越界')
        if not any(directory.iterdir()):
            directory.rmdir()
    result['applied'] = True
    for item in moves:
        item['final_sha256'] = file_hash(Path(item['to']))
    store.atomic_json(receipt, result)
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--state-dir')
    parser.add_argument('--apply', action='store_true', help='已获整理授权后才实际移动；默认只预览')
    args = parser.parse_args()
    argv = ['--output-dir', args.output_dir]
    if args.state_dir:
        argv += ['--state-dir', args.state_dir]
    print(json.dumps(organize(config.load_config(argv), args.apply), ensure_ascii=False, indent=2))
