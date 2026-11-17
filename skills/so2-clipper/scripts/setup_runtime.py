"""用户级、可重试的安装；只有实际自检完成才登记运行配置。"""
import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import urllib.request
import venv
import zipfile
from pathlib import Path

TORCH_VERSION = '2.11.0'
VISION_VERSION = '0.26.0'
FFMPEG_URL = 'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-8.1.2-essentials_build.zip'
FFMPEG_SHA256 = 'db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec'


def run(command, **kwargs):
    return subprocess.run([str(x) for x in command], check=True, **kwargs)


def gpu_index(request):
    if request == 'cpu':
        return 'cpu'
    try:
        result = run(['nvidia-smi'], capture_output=True, text=True, encoding='utf-8', errors='replace')
        version = re.search(r'CUDA Version:\s*(\d+)\.(\d+)', result.stdout)
        capable = version and tuple(map(int, version.groups())) >= (12, 6)
    except (OSError, subprocess.SubprocessError):
        capable = False
    if request == 'cuda' and not capable:
        raise ValueError('NVIDIA 驱动没有报告 CUDA 12.6 能力；可改用 CPU')
    return 'cu126' if capable else 'cpu'


def media_dir(root, explicit, offline):
    candidate = Path(explicit) if explicit else Path(shutil.which('ffmpeg') or '.').parent
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        if not (candidate / name).is_file():
            break
        run([candidate / name, '-version'], capture_output=True)
    else:
        return candidate.resolve()
    binary_root = root / 'ffmpeg-8.1.2'
    existing = list(binary_root.glob('*/bin/ffmpeg.exe'))
    if existing and existing[0].with_name('ffprobe.exe').is_file():
        return existing[0].parent
    if offline:
        raise ValueError('离线模式缺少可用 FFmpeg／ffprobe')
    download = root / 'ffmpeg-8.1.2-essentials_build.zip'
    if not download.is_file():
        temporary = download.with_suffix('.download')
        urllib.request.urlretrieve(FFMPEG_URL, temporary)
        os.replace(temporary, download)
    with download.open('rb') as stream:
        if hashlib.file_digest(stream, 'sha256').hexdigest() != FFMPEG_SHA256:
            raise ValueError('FFmpeg 下载包 SHA256 不匹配，原包保留')
    with zipfile.ZipFile(download) as archive:
        if any(not (binary_root / name).resolve().is_relative_to(binary_root.resolve()) for name in archive.namelist()):
            raise ValueError('FFmpeg 压缩包包含越界路径')
        archive.extractall(binary_root)
    candidate = next(binary_root.glob('*/bin/ffmpeg.exe')).parent
    for name in ('ffmpeg.exe', 'ffprobe.exe'):
        run([candidate / name, '-version'], capture_output=True)
    return candidate


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--root', required=True)
    p.add_argument('--acceleration', choices=['auto', 'cpu', 'cuda'], default='auto')
    p.add_argument('--reuse-python', action='store_true')
    p.add_argument('--ffmpeg-dir')
    p.add_argument('--model-dir')
    p.add_argument('--offline', action='store_true')
    args = p.parse_args()
    if platform.system() != 'Windows' or platform.architecture()[0] != '64bit' or sys.version_info[:2] not in ((3, 12), (3, 13)):
        raise ValueError('首版需要 Windows x64 和 Python 3.12／3.13')
    root = Path(args.root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    python = Path(sys.executable)
    if not args.reuse_python:
        env_dir = root / 'venv'
        python = env_dir / 'Scripts/python.exe'
        if not python.is_file():
            venv.EnvBuilder(with_pip=True).create(env_dir)
        if args.offline:
            run([python, '-c', 'import easyocr,torch,cv2'])
        else:
            index = gpu_index(args.acceleration)
            run([python, '-m', 'pip', 'install', f'torch=={TORCH_VERSION}', f'torchvision=={VISION_VERSION}',
                 '--index-url', 'https://download.pytorch.org/whl/' + index])
            run([python, '-m', 'pip', 'install', '-r', Path(__file__).with_name('requirements.txt')])
    directory = media_dir(root, args.ffmpeg_dir, args.offline)
    env = dict(os.environ, PYTHONUTF8='1', PATH=str(directory) + os.pathsep + os.environ.get('PATH', ''))
    smoke = '''
import json,sys,platform
import cv2,easyocr,torch
if easyocr.__version__ != '1.7.2' or not torch.__version__.startswith('2.11.0'):
 raise ValueError('Existing runtime needs EasyOCR 1.7.2 and PyTorch 2.11.0; create an isolated environment instead')
gpu=torch.cuda.is_available() and WANT_GPU
fallback=None
try:
 if gpu: torch.ones(1,device='cuda').sum().item()
 reader=easyocr.Reader(['ch_sim','en'],gpu=gpu,download_enabled=DOWNLOAD,model_storage_directory=MODEL)
except RuntimeError as error:
 if not gpu or not ALLOW_FALLBACK: raise
 fallback=str(error)
 gpu=False
 reader=easyocr.Reader(['ch_sim','en'],gpu=False,download_enabled=DOWNLOAD,model_storage_directory=MODEL)
print('SO2_RUNTIME_JSON='+json.dumps({'python':sys.executable,'python_version':platform.python_version(),
 'easyocr':easyocr.__version__,'torch':torch.__version__,'opencv':cv2.__version__,'gpu':gpu,
 'gpu_fallback_reason':fallback,'model_directory':str(reader.model_storage_directory)},ensure_ascii=False))
'''.replace('DOWNLOAD', repr(not args.offline)).replace('MODEL', repr(args.model_dir)).replace(
        'WANT_GPU', repr(args.acceleration != 'cpu')).replace('ALLOW_FALLBACK', repr(args.acceleration == 'auto'))
    result = run([python, '-c', smoke], env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
    (root / 'setup-smoke.log').write_text(result.stdout + result.stderr, encoding='utf-8')
    marker = 'SO2_RUNTIME_JSON='
    offset = result.stdout.find(marker)
    if offset < 0:
        raise ValueError('OCR 自检未返回安装回执')
    # EasyOCR 首次下载的进度条可能没有换行，不能要求回执从行首开始。
    data, _ = json.JSONDecoder().raw_decode(result.stdout[offset + len(marker):])
    data.update(version=1, ffmpeg_dir=str(directory), offline_checked=args.offline)
    freeze = run([python, '-m', 'pip', 'freeze'], capture_output=True, text=True, encoding='utf-8')
    (root / 'installed.txt').write_text(freeze.stdout, encoding='utf-8')
    target = root / 'runtime.json'
    part = target.with_suffix('.json.part')
    part.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(part, target)
    print(json.dumps({'runtime': str(target), **data}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.SubprocessError, StopIteration) as error:
        print(f'安装未完成：{error}', file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr[-3000:], file=sys.stderr)
        sys.exit(1)
