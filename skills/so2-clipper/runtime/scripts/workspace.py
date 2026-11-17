"""区分用户交付目录与运行状态；跨卷发布仍先完成临时文件再替换。"""
import errno
import hashlib
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


def state_root(output_dir):
    home = Path(os.environ.get('SO2_CLIPS_STATE_HOME') or
                Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'SO2KillClips/tasks')
    key = hashlib.sha256(os.path.normcase(str(Path(output_dir).resolve())).encode('utf-8')).hexdigest()[:20]
    return home.resolve() / key


def run_dir(cfg, run_id=None):
    return Path(cfg.state_dir) / 'runs' / (run_id or cfg.current_run_id)


def report_dir(cfg):
    if getattr(cfg, 'current_run_id', None):
        return run_dir(cfg)
    return Path(cfg.state_dir) / 'reports'


@contextmanager
def writer(state_dir):
    """同一状态目录只允许一个写入者，进程退出由系统释放锁。"""
    path = Path(state_dir) / 'writer.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ValueError('同一状态目录已有写入者，请跟踪当前任务，不并行重跑') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def publish(part, target, output_dir):
    """内部缓存可位于 C 盘，最终视频可位于另一卷；最终路径原子可见。"""
    part, target = Path(part), Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(part, target)
    except OSError as error:
        if error.errno != errno.EXDEV and getattr(error, 'winerror', None) != 17:
            raise
        # 跨卷复制先放在交付根目录外，同卷替换完成前不显示为成片。
        with tempfile.NamedTemporaryFile(prefix='.so2-transfer-', suffix='.tmp',
                                         dir=Path(output_dir).resolve().parent, delete=False) as stream:
            transfer = Path(stream.name)
        try:
            shutil.copy2(part, transfer)
            os.replace(transfer, target)
            part.unlink()
        finally:
            transfer.unlink(missing_ok=True)
