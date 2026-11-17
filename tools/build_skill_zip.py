"""从唯一引擎真源生成独立 ZIP；不复制录像、状态、个人配置或验收资料。"""
import hashlib
import json
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[1]
ROOT = SOURCE_ROOT
SKILL_NAME = 'so2-clipper'
VERSION_FALLBACK = '0.2.0-beta.1'  # Used only by isolated test roots without VERSION.
VERSION_RE = re.compile(r'\A[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z')


def _version_from_file(root):
    path = Path(root) / 'VERSION'
    if not path.is_file():
        return None
    value = path.read_text(encoding='utf-8').strip()
    if not VERSION_RE.fullmatch(value):
        raise ValueError(f'unsupported VERSION value: {value!r}')
    return value


VERSION = _version_from_file(ROOT) or VERSION_FALLBACK
INSTALL_TEXT = (
    'SO2 Clipper Skill | SO2 击杀集锦 Skill (Windows)\n\n'
    'English\n'
    '1. Extract the complete ZIP.\n'
    f'2. Run {SKILL_NAME}/scripts/install.ps1 in PowerShell, or place the complete '
    f'{SKILL_NAME} directory in your AI tool\'s Skill folder.\n'
    f'3. Ask your AI tool to use {SKILL_NAME} with a video or folder.\n'
    'The first setup may download Python, dependencies, FFmpeg, and OCR models. '
    'It needs network access and can use several GB of disk space. GPU support is optional; '
    'a CPU-only setup is available.\n'
    'Batch AI delivery is grouped into weapon-category folders. The regular CLI writes '
    'MP4 files flat under the date folder. See the Skill references for Windows setup, '
    'resource requirements, review, and recovery. This package does not include Python, '
    'FFmpeg, model weights, recordings, or private settings.\n\n'
    '简体中文\n'
    '1. 解压整个 ZIP。\n'
    f'2. 在 PowerShell 运行 {SKILL_NAME}/scripts/install.ps1，或将完整 '
    f'{SKILL_NAME} 目录放入 AI 工具的 Skill 目录。\n'
    f'3. 让 AI 工具使用 {SKILL_NAME} 处理一个录像或文件夹。\n'
    '首次配置可能下载 Python、依赖、FFmpeg 和 OCR 模型，需要网络并可能占用数 GB 磁盘空间。'
    'GPU 是可选项，也可使用 CPU 模式。\n'
    'AI 批量交付按武器大类建立文件夹；普通 CLI 将 MP4 平铺放在日期目录下。'
    'Windows 配置、资源需求、复核和恢复说明见 Skill 内的 references。'
    '本包不含 Python、FFmpeg、模型权重、录像或私人设置。\n')


def read_version(root=ROOT):
    """Read the release version, retaining compatibility with old temp fixtures."""
    root = Path(root).resolve()
    value = _version_from_file(root)
    if value is not None and root == SOURCE_ROOT and value != VERSION:
        raise ValueError('VERSION constant is stale; reload build_skill_zip after changing VERSION')
    return value or VERSION


def verify_public_output(path, checksum_path=None, expected_version=None):
    """Apply strict public checks in the real repo; keep ROOT-mocked legacy fixtures lightweight."""
    if Path(ROOT).resolve() != SOURCE_ROOT:
        return None
    if __package__:
        from tools import verify_release
    else:
        import verify_release
    expected_version = expected_version or read_version(ROOT)
    if checksum_path is None:
        return verify_release._verify_zip(Path(path), expected_version=expected_version)
    return verify_release.verify(Path(path), Path(checksum_path), expected_version)


def archive_info(path):
    with zipfile.ZipFile(path) as archive:
        if archive.testzip() is not None:
            raise ValueError('分享包 CRC 校验失败')
        manifest = json.loads(archive.read(f'{SKILL_NAME}/manifest.json'))
        expected = {f'{SKILL_NAME}/' + name for name in manifest['files']}
        expected |= {f'{SKILL_NAME}/manifest.json', '安装说明.txt'}
        if set(archive.namelist()) != expected or len(archive.namelist()) != len(expected):
            raise ValueError('分享包清单与实际文件不符')
        for name, digest in manifest['files'].items():
            if hashlib.sha256(archive.read(f'{SKILL_NAME}/' + name)).hexdigest() != digest:
                raise ValueError(f'分享包文件哈希不符：{name}')
        installer_matches = archive.read('安装说明.txt') == INSTALL_TEXT.encode('utf-8')
    info = {'zip': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'content_id': manifest['content_id'], 'bytes': path.stat().st_size, 'files': len(expected)}
    return info, manifest, installer_matches


def clean_old_packages(output, keep):
    """用户明确要求旧分享包永久删除；仅处理 dist 直属的本项目包与校验文件。"""
    output, keep = output.resolve(), keep.resolve()
    if keep.parent != output or not keep.is_file():
        raise ValueError('保留的分享包必须位于 dist 内')
    archive_info(keep)  # 新包或当前包验证成功后，才允许移除旧包。
    keep_names = {keep.name, keep.with_suffix('.zip.sha256').name}
    candidates = [path for prefix in (SKILL_NAME, 'so2-kill-clips')
                  for suffix in ('*.zip', '*.zip.sha256')
                  for path in output.glob(f'{prefix}-{suffix}')]
    obsolete = [path for path in candidates if path.name not in keep_names]
    if any(path.is_symlink() or path.resolve().parent != output or not path.is_file() for path in obsolete):
        raise ValueError('旧分享包路径不在预期范围内，未执行删除')
    deleted, freed = [], 0
    for path in obsolete:
        freed += path.stat().st_size
        path.unlink()
        deleted.append(str(path))
    return deleted, freed


def atomic_text(path, text):
    part = path.with_name(f'{path.name}.{os.getpid()}.part')
    try:
        part.write_bytes(text.encode('utf-8'))
        os.replace(part, path)
    finally:
        part.unlink(missing_ok=True)


def finish(output, target, reused):
    info, manifest, _ = archive_info(target)
    checksum = target.with_suffix('.zip.sha256')
    checksum_text = info['sha256'] + '  ' + target.name + '\n'
    if not checksum.is_file() or checksum.read_text(encoding='utf-8') != checksum_text:
        atomic_text(checksum, checksum_text)
    verify_public_output(target, checksum, manifest['version'])
    pointer = output / 'current_release.json'
    previous = json.loads(pointer.read_text(encoding='utf-8')) if pointer.is_file() else {}
    if previous.get('content_id') == info['content_id']:
        current = dict(previous)
    else:
        current = {'name': SKILL_NAME, 'version': manifest['version'],
                   'status': 'packaged_manifest_verified'}
    current.update(info)
    # Keep the checked-in/current pointer portable; the API result still
    # returns the absolute ZIP path for callers that need to open it directly.
    current['zip'] = target.name
    current.pop('previous_release', None)
    if current != previous:
        atomic_text(pointer, json.dumps(current, ensure_ascii=False, indent=2))
    deleted, freed = clean_old_packages(output, target)
    return {**info, 'reused': reused, 'deleted_old_files': deleted, 'freed_bytes': freed}


def build():
    version = read_version(ROOT)
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='so2_skill_build_') as directory:
        stage = Path(directory) / SKILL_NAME
        shutil.copytree(ROOT / 'skills' / SKILL_NAME, stage,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc', 'runtime', 'evals'))
        # These root-level public notices are copied into the self-contained Skill package.
        # They are optional only for the existing minimal ROOT-mocked unit fixtures;
        # verify_release.py requires both for a public release.
        for public_file in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
            source = ROOT / public_file
            if source.is_file():
                shutil.copy2(source, stage / public_file)
        (stage / 'VERSION').write_bytes((version + '\n').encode('utf-8'))
        engine = stage / 'runtime/scripts'
        engine.mkdir(parents=True)
        for source in sorted((ROOT / 'scripts').glob('*.py')):
            shutil.copy2(source, engine / source.name)
        shutil.copytree(ROOT / 'scripts/assets', engine / 'assets')
        runtime_files = {
            path.relative_to(engine).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(engine.rglob('*')) if path.is_file()
        }
        runtime_manifest = {
            'schema_version': 1,
            'name': 'so2-clipper-runtime',
            'version': version,
            'source': 'scripts/*.py and scripts/assets/**',
            'files': runtime_files,
            'distribution_files': {
                name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
                for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md', 'VERSION')
                if (stage / name).is_file()
            },
        }
        (stage / 'runtime/manifest.json').write_bytes(
            (json.dumps(runtime_manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        files = {path.relative_to(stage).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                 for path in sorted(stage.rglob('*')) if path.is_file()}
        # 打包工具自身变化不必重新分发完全相同的 Skill 内容。
        for existing in sorted(output.glob(f'{SKILL_NAME}-{version}-*.zip'),
                               key=lambda p: p.stat().st_mtime_ns, reverse=True):
            if existing.is_symlink() or existing.resolve().parent != output.resolve():
                raise ValueError('分享包路径不在 dist 内')
            try:
                _, prior, installer_matches = archive_info(existing)
            except (zipfile.BadZipFile, KeyError, ValueError):
                continue
            if prior.get('version') == version and prior.get('files') == files and installer_matches:
                return finish(output, existing, reused=True)
        content_id = hashlib.sha256(json.dumps({'files': files, 'version': version}, sort_keys=True).encode()).hexdigest()
        manifest = {'name': SKILL_NAME, 'version': version, 'content_id': content_id, 'files': files,
                    'engine_source': 'generated from the project scripts tree; do not edit the packaged snapshot'}
        (stage / 'manifest.json').write_bytes(json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8'))
        target = output / f'{SKILL_NAME}-{version}-{content_id[:8]}.zip'
        with tempfile.NamedTemporaryFile(prefix='.so2-build-', suffix='.part', dir=output, delete=False) as stream:
            part = Path(stream.name)
        try:
            # Store files without compression so ZIP bytes stay independent of
            # the zlib version installed on the build machine.
            with zipfile.ZipFile(part, 'w', compression=zipfile.ZIP_STORED) as archive:
                for path in sorted(stage.rglob('*')):
                    if path.is_file():
                        info = zipfile.ZipInfo(path.relative_to(stage.parent).as_posix(), date_time=(2026, 10, 19, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_STORED
                        info.external_attr = 0o100644 << 16
                        archive.writestr(info, path.read_bytes())
                install_info = zipfile.ZipInfo('安装说明.txt', date_time=(2026, 10, 19, 0, 0, 0))
                install_info.compress_type = zipfile.ZIP_STORED
                install_info.external_attr = 0o100644 << 16
                archive.writestr(install_info, INSTALL_TEXT)
            archive_info(part)
            verify_public_output(part, expected_version=version)
            os.replace(part, target)
        finally:
            part.unlink(missing_ok=True)
        return finish(output, target, reused=False)


if __name__ == '__main__':
    print(json.dumps(build(), ensure_ascii=False, indent=2))
