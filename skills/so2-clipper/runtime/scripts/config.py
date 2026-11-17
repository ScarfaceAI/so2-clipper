"""
SO2 剪辑项目共享配置
所有脚本共用的参数集中在这里，支持命令行覆盖（见项目根 AGENTS.md 用法）。
调整检测区域/关键词等参数只改这一个文件即可。
"""

import argparse
import json
import math
import re
import string
from pathlib import Path
from types import SimpleNamespace
import workspace
from options import Options

# ========== 默认配置 ==========
# 私有设置与可分发代码分离。构建包不会包含此文件。
_LOCAL_PATH = Path(__file__).resolve().parents[1] / 'settings.local.json'
LOCAL = json.loads(_LOCAL_PATH.read_text(encoding='utf-8')) if _LOCAL_PATH.is_file() else {}
VIDEO_DIR = LOCAL.get('video_dir')
OUTPUT_DIR = LOCAL.get('output_dir')

# 检测区域（基于2058x1440截图校准，运行时自动缩放到实际分辨率）
REGION = {'x1': 1540, 'y1': 70, 'x2': 2058, 'y2': 200}
BASE_W, BASE_H = 2058, 1440

KEYWORDS = LOCAL.get('keywords', [])

# OCR 语言（含中文可识别中文玩家名，识别略慢）
LANGUAGES = ['ch_sim', 'en']
GPU = True

# 扫描参数
SCAN_INTERVAL = 10        # 每隔多少帧采样一次
SCAN_SEGMENTS = 4         # 单视频分段数：每段独立进程并行解码+OCR（解码是瓶颈，分段≈线性加速）
SCAN_ENGINE = 'auto'      # 已校准预设默认 ffmpeg；cv2 可显式选择
ROI_FPS = 3.0             # ffmpeg 引擎的 ROI 采样帧率（播报停留约4s，3fps 即 12 个采样点）
MIN_CHANGE_RATIO = 0.02   # ROI内像素变化比例低于此值则跳过OCR（帧差预筛选）
MIN_CONF = 0.3            # OCR最低置信度
CLUSTER_WINDOW = 3.6      # 同一条播报重复识别的聚簇窗口(秒)；不同播报行(换了被杀者)不受此限制
SUPPRESS_AFTER_HIT = 2.0  # 区域无变化时的最长 OCR 确认间隔；变化帧不等待
SIG_WINDOW = 8.0          # 播报行签名记忆窗口(秒)：窗口内同一行不重复计数

# 中央阶段提示，按显示画面比例裁切，兼容录屏与带上下黑边的直播。
WARMUP_REGION = (.36, .0, .64, .27)
WARMUP_TIMER_REGIONS = {
    'live': (.45, .09, .55, .15),
    'pc': (.45, .0, .55, .07),
    'phone': (.45, .0, .55, .07),
    'phone-highfps': (.45, .0, .55, .07),
}
WARMUP_INTERVAL = 1.0
WARMUP_MIN_CONF = .7

# X坐标过滤阈值 (基于2058宽截图校准: 击杀<1800, 被击杀>1900, 按比例缩放)
MAX_X_CENTER_RATIO = 1800 / 2058

# 片段提取参数
PRE_SECONDS = 15
POST_SECONDS = 15
MIN_KILLS_PER_CLIP = 2  # 片段内击杀数低于此值不提取（只保留多杀片段）
KILL_GAP = 10.0

# 已人工校准的布局；坐标始终基于旋转后的显示画面。
PROFILES = {
    'pc': (REGION, MAX_X_CENTER_RATIO, 'ffmpeg'),
    'phone': (REGION, MAX_X_CENTER_RATIO, 'ffmpeg'),
    'phone-highfps': ({'x1': 1540, 'y1': 70, 'x2': 2058, 'y2': 247}, 0.9134, 'ffmpeg'),
    'live': ({'x1': 1500, 'y1': 193, 'x2': 2058, 'y2': 340}, 0.896, 'ffmpeg'),
}
SOURCE_PROFILES = LOCAL.get('source_profiles', {})
# 提取模式: 'copy'=流复制(秒级完成, 关键帧精度±2s)  'reencode'=重编码(精确, 慢)
EXTRACT_MODE = 'reencode'

# 并行度
NUM_WORKERS = 4        # 扫描进程数（每进程约1.5GB显存）
EXTRACT_WORKERS = 4    # ffmpeg提取并发数


def _derive_allowlist(keywords):
    """关键词全为ASCII时限制OCR字符集，识别更快更准；含中文则不限"""
    if all(k.isascii() for k in keywords):
        return string.ascii_letters + string.digits + ' _-./()'
    return None


def build_parser():
    p = argparse.ArgumentParser(description='SO2 击杀片段提取器', allow_abbrev=False)
    p.add_argument('--video-dir', default=VIDEO_DIR, help='录像目录')
    p.add_argument('--output-dir', default=OUTPUT_DIR, help='片段输出目录')
    p.add_argument('--state-dir', default=None, help='运行状态目录；默认在用户目录中按输出位置隔离')
    p.add_argument('--video', default=None, help='指定单个视频（单文件模式用）')
    p.add_argument('--hits', default=None, help='手动指定击杀时间戳，逗号分隔（配合--video跳过扫描直接提取）')
    p.add_argument('--keywords', default=None, help='自己的游戏 ID，逗号分隔，如 "PlayerOne,玩家一"')
    p.add_argument('--identity-mode', choices=['auto', 'keywords'], default=None,
                   help='auto=由播报角色、当前头像和击杀反馈识别，名字辅助；keywords=手动名字')
    p.add_argument('--date-zone', choices=['local', 'Asia/Shanghai'], default=LOCAL.get('date_zone', 'local'),
                   help='处理日期使用的时区')
    p.add_argument('--run-id', default=None, help='调用方的本次任务标识；缺省自动生成')
    p.add_argument('--merge', action='store_true', help='将可信片段按素材顺序合并为原声集锦')
    p.add_argument('--decisions', default=None, help='带原始身份和证据引用的事件复核 JSON')
    p.add_argument('--source-registry', default=None, help='用户明确确认过的来源设置 JSON')
    p.add_argument('--profile', choices=['auto', *PROFILES], default='auto',
                   help='已校准布局：pc 1920×1080/2058×1440，phone 2388×1668，'
                        'phone-highfps 2354×1080，live 1920×1080；未知来源需显式指定')
    p.add_argument('--languages', default=','.join(LANGUAGES), help='OCR语言，逗号分隔')
    p.add_argument('--pre', type=float, default=PRE_SECONDS, help='击杀前秒数')
    p.add_argument('--post', type=float, default=POST_SECONDS, help='击杀后秒数')
    p.add_argument('--kill-gap', type=float, default=KILL_GAP, help='同组相邻击杀最大间隔（秒）')
    p.add_argument('--min-kills', type=int, default=MIN_KILLS_PER_CLIP,
                   help='片段内击杀数低于此值不提取（1=提取全部）')
    p.add_argument('--interval', type=int, default=SCAN_INTERVAL, help='扫描帧间隔')
    p.add_argument('--scan-segments', type=int, default=SCAN_SEGMENTS,
                   help='单视频分段并行数(1=不分段)；批量扫描时由主流程覆盖为1')
    p.add_argument('--scan-engine', choices=['auto', 'cv2', 'ffmpeg'], default=SCAN_ENGINE,
                   help='扫描引擎: auto=使用已校准预设，已知布局默认 ffmpeg ROI 采样；cv2 可显式选择')
    p.add_argument('--roi-fps', type=float, default=ROI_FPS,
                   help='ffmpeg 引擎的 ROI 采样帧率(默认3.0；播报停留约4s，3fps 即12个采样点)')
    p.add_argument('--workers', type=int, default=NUM_WORKERS, help='扫描进程数')
    p.add_argument('--extract-workers', type=int, default=EXTRACT_WORKERS, help='提取并发数')
    p.add_argument('--mode', choices=['copy', 'reencode'], default=EXTRACT_MODE,
                   help="提取模式: copy=流复制(快,关键帧精度), reencode=重编码(精确)")
    p.add_argument('--min-conf', type=float, default=MIN_CONF, help='OCR最低置信度')
    p.add_argument('--min-change-ratio', type=float, default=MIN_CHANGE_RATIO,
                   help='ROI帧间变化比例阈值(帧差预筛选)')
    p.add_argument('--region', default=None,
                   help='检测区域基准坐标(基于2058x1440) "x1,y1,x2,y2"，默认 REGION；'
                        '换分辨率/机位后播报位置不同时用它覆盖')
    p.add_argument('--max-x-ratio', type=float, default=None,
                   help='X坐标过滤阈值(帧宽比例)，默认 %.4f；手机版播报条偏右时可调大' % MAX_X_CENTER_RATIO)
    p.add_argument('--cluster-window', type=float, default=CLUSTER_WINDOW, help='命中聚类窗口(秒)')
    p.add_argument('--sig-window', type=float, default=SIG_WINDOW,
                   help='播报行签名记忆窗口(秒)：窗口内同一播报行不重复计数(连杀靠被杀者不同区分)')
    p.add_argument('--suppress-after-hit', type=float, default=SUPPRESS_AFTER_HIT,
                   help='区域无变化时的最长OCR确认间隔(秒)；变化帧立即检查')
    p.add_argument('--no-gpu', action='store_true', help='禁用GPU')
    p.add_argument('--rescan', action='store_true', help='强制重新扫描所有视频')
    p.add_argument('--scan-only', action='store_true', help='只扫描不提取')
    p.add_argument('--extract-only', action='store_true', help='只提取已扫描的结果')
    p.add_argument('--defer-report-images', action='store_true',
                   help='先完成扫描回执，疑点图片由后续 evidence 按需生成')
    p.add_argument('--keep-warmup', action='store_true', help='保留热身击杀；默认识别中央热身倒计时并过滤')
    return p


def load_config(argv=None):
    """解析命令行参数，合并默认值，生成完整配置对象"""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.run_id is not None and not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', args.run_id):
        parser.error('--run-id 只能包含字母、数字、连字符或下划线，最多 80 字符')
    for name in ('pre', 'post', 'kill_gap', 'cluster_window', 'sig_window', 'suppress_after_hit'):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) < 0:
            parser.error(f'--{name.replace("_", "-")} 需要有限非负数')
    for name in ('interval', 'scan_segments', 'workers', 'extract_workers', 'min_kills'):
        if getattr(args, name) < 1:
            parser.error(f'--{name.replace("_", "-")} 需要正整数')
    if not math.isfinite(args.roi_fps) or args.roi_fps <= 0:
        parser.error('--roi-fps 需要有限正数')
    for name in ('min_conf', 'min_change_ratio'):
        if not 0 <= getattr(args, name) <= 1:
            parser.error(f'--{name.replace("_", "-")} 需要在 0 到 1 之间')
    if args.max_x_ratio is not None and not 0 < args.max_x_ratio <= 1:
        parser.error('--max-x-ratio 需要在 0 到 1 之间')
    if args.scan_only and args.extract_only:
        parser.error('--scan-only 与 --extract-only 不能同时使用')
    if args.hits is not None and not args.video:
        parser.error('--hits 需要配合 --video')
    if args.decisions and not args.video:
        parser.error('--decisions 需要配合 --video，逐文件复核')
    if args.hits is not None:
        try:
            times = [float(x.strip()) for x in args.hits.split(',') if x.strip()]
        except ValueError:
            parser.error('--hits 需要逗号分隔的秒数')
        if not times or any(not math.isfinite(t) or t < 0 for t in times):
            parser.error('--hits 需要有限非负时间戳')
    cfg = SimpleNamespace(**vars(args))
    cfg.registered_sources = []
    if args.source_registry:
        try:
            cfg.registered_sources = json.loads(Path(args.source_registry).read_text(encoding='utf-8')).get('sources', [])
        except (OSError, ValueError) as error:
            parser.error(f'来源设置不可读：{error}')

    cfg.identity_mode = args.identity_mode or ('keywords' if args.keywords else LOCAL.get('identity_mode', 'auto'))

    cfg.keywords = ([k.strip() for k in args.keywords.split(',') if k.strip()]
                    if args.keywords else list(KEYWORDS))
    cfg.languages = ([l.strip() for l in args.languages.split(',') if l.strip()]
                     or list(LANGUAGES))
    if cfg.identity_mode == 'auto':
        cfg.keywords = []
    if not cfg.keywords and cfg.identity_mode == 'keywords' and cfg.hits is None:
        parser.error('--keywords 至少需要一个自己的游戏 ID')
    cfg.gpu = not args.no_gpu
    cfg.allowlist = None if cfg.identity_mode == 'auto' else _derive_allowlist(cfg.keywords)
    cfg.filter_warmup = not args.keep_warmup

    # 统一规范属性名（CLI参数名与常量名可能不同）
    cfg.scan_interval = args.interval
    cfg.min_change_ratio = args.min_change_ratio
    cfg.suppress_after_hit = args.suppress_after_hit

    cfg.region_override = args.region is not None
    cfg.max_x_override = args.max_x_ratio
    cfg.region = dict(REGION)
    if args.region:
        try:
            vals = [int(v.strip()) for v in args.region.replace('，', ',').split(',') if v.strip()]
        except ValueError:
            parser.error('--region 需要 4 个整数')
        if len(vals) != 4:
            parser.error('--region 需要 4 个整数: "x1,y1,x2,y2"')
        x1, y1, x2, y2 = vals
        if not (0 <= x1 < x2 and 0 <= y1 < y2):
            parser.error('--region 坐标需满足 x1<x2、y1<y2')
        cfg.region = {'x1': x1, 'y1': y1, 'x2': x2, 'y2': y2}
    cfg.base_w, cfg.base_h = BASE_W, BASE_H
    cfg.max_x_center_ratio = args.max_x_ratio if args.max_x_ratio is not None else MAX_X_CENTER_RATIO
    cfg.workers = min(4, cfg.workers)
    cfg.scan_segments = min(4, cfg.scan_segments)

    input_root = Path(cfg.video).resolve().parent if cfg.video else Path(cfg.video_dir or Path.cwd()).resolve()
    cfg.output_dir = str(Path(args.output_dir or input_root / 'output').resolve())
    state = Path(args.state_dir).resolve() if args.state_dir else workspace.state_root(cfg.output_dir)
    if state.is_relative_to(Path(cfg.output_dir)) or Path(cfg.output_dir).is_relative_to(state):
        parser.error('--state-dir 与 --output-dir 必须分开，不能互相包含')
    cfg.state_dir = str(state)
    cfg.result_file = str(state / 'scan_results.json')
    cfg.log_file = str(state / 'scan.log')
    cfg.tmp_dir = str(state / 'tmp')
    return Options.from_mapping(vars(cfg))
