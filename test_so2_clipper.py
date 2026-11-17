"""SO2 剪辑项目测试：纯逻辑单测 + 假OCR端到端冒烟测试

用系统Python运行（无需easyocr）：python test_so2_clipper.py
假OCR逐像素读取ROI中的白色文字块，验证：帧差预筛选 / 关键词匹配 /
X坐标过滤 / MIN_CONF过滤 / 抑制与聚簇 / 时间戳 / JSON持久化 / ffmpeg提取
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'scripts'))
import cv2
import numpy as np
import config
import core
import store
import extraction
from tools import check_project_contracts
from dataclasses import asdict


def fixture_config(argv):
    return config.load_config(['--profile', 'pc', '--scan-engine', 'cv2',
                               '--identity-mode', 'keywords', '--keywords', 'Scarface', *argv])

PASS, FAIL = 0, 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")

# ================= 合成测试视频 =================
def make_video(path, duration=30, fps=30, w=1920, h=1080):
    """背景深色+轻微渐变；击杀播报白字只在事件窗口内绘制"""
    fourcc = cv2.VideoWriter_fourcc(*'mp4v')
    vw = cv2.VideoWriter(path, fourcc, fps, (w, h))

    def event_active(t, win):
        return win[0] <= t < win[1]

    events = [
        # (时间窗, 绘制x, 绘制y, 备注)
        ((5.0, 5.2),   1470, 90, 'event1 左-命中'),
        ((5.4, 5.6),   1485, 90, 'event1b 移位-抑制/聚簇'),
        ((9.9, 10.5),  1470, 110, 'event2 左-命中'),
        ((15.0, 15.6), 1800, 90, '右侧-应被X过滤'),
        ((20.0, 20.6), 1470, 150, '低置信-应被MIN_CONF过滤'),
        ((25.0, 25.3), 1470, 90, 'event3a 聚簇'),
        ((25.5, 25.8), 1490, 90, 'event3b 聚簇'),
    ]
    n = int(duration * fps)
    for i in range(n):
        t = i / fps
        frame = np.full((h, w, 3), 28, np.uint8)
        frame += np.linspace(0, 10, h, dtype=np.uint8).reshape(-1, 1, 1)
        for win, x, y, _ in events:
            if event_active(t, win):
                cv2.putText(frame, 'Scarface', (x, y), cv2.FONT_HERSHEY_SIMPLEX,
                            1.2, (255, 255, 255), 2, cv2.LINE_AA)
        vw.write(frame)
    vw.release()

class EmptyPhaseReader:
    def readtext(self, *a, **k):
        return []


def fake_reader(*a, **k):
    return EmptyPhaseReader()

def make_fake_read_roi(counter):
    def fake_read_roi(reader, roi_bgr, cfg):
        counter['calls'] += 1
        gray = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2GRAY)
        ys, xs = np.where(gray > 200)
        if xs.size == 0:
            return []
        x1, x2, y1, y2 = xs.min(), xs.max(), ys.min(), ys.max()
        # bbox中心y>=70(ROI局部坐标)的行视为"低置信"事件（测试MIN_CONF过滤）
        conf = 0.1 if (y1 + y2) / 2 >= 70 else 0.95
        bbox = [[int(x1), int(y1)], [int(x2), int(y1)], [int(x2), int(y2)], [int(x1), int(y2)]]
        victim = 'Kxz' if (y1 + y2) / 2 < 40 else 'Abandon'
        vb = [[350, int(y1)], [470, int(y1)], [470, int(y2)], [350, int(y2)]]
        return [(bbox, 'Scarface', conf), (vb, victim, .95)]
    return fake_read_roi

def probe_duration(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                        '-of', 'default=nw=1:nk=1', str(path)], capture_output=True, text=True)
    return float(r.stdout.strip()) if r.returncode == 0 else None

# ================= 测试 =================
def main():
    if check_project_contracts.main(['--root', str(ROOT)]):
        sys.exit(1)
    global FAIL
    tmp = tempfile.mkdtemp(prefix='so2_test_')
    os.environ['SO2_CLIPS_STATE_HOME'] = str(Path(tmp) / 'state')
    vid_dir = os.path.join(tmp, 'videos'); os.makedirs(vid_dir)
    out_dir = os.path.join(tmp, 'clips'); os.makedirs(out_dir)
    video = os.path.join(vid_dir, 'test_kill_001.mp4')
    print(f"测试目录: {tmp}")

    print("\n[1] 生成合成视频 (30s @30fps 1920x1080)...")
    make_video(video)
    check('视频生成', os.path.getsize(video) > 100_000, f"size={os.path.getsize(video)}")

    print("\n[2] 纯逻辑单测")
    # cluster_hits: 聚簇合并/保留连杀/取最高置信
    raw = [(5.0, .9), (5.2, .95), (10.0, .8)]
    c = core.cluster_hits(raw, 1.5)
    check('聚簇: 窗口内合并+取最高置信', len(c) == 2 and c[0] == (5.2, .95) and c[1] == (10.0, .8), str(c))
    raw2 = [(5.0, .9), (7.0, .9)]  # 间隔>窗口 → 两次击杀
    check('聚簇: 连杀保留', len(core.cluster_hits(raw2, 1.5)) == 2)
    check('聚簇: 空输入', core.cluster_hits([], 1.5) == [])
    # 播报方向判断（击杀=关键词在左，被击杀=关键词在右）
    check('方向: 纯名字=击杀', core._is_kill_side('Scarface', 'Scarface'))
    check('方向: 击杀者名在左', core._is_kill_side('Scarface t Kxz', 'Scarface'))
    check('方向: 计数前缀=击杀', core._is_kill_side('1 Scarface', 'Scarface'))
    check('方向: 残字前缀=击杀', core._is_kill_side('  r t Scarface', 'Scarface'))
    check('方向: 被击杀者名在右', not core._is_kill_side('yoyuan t Scarface', 'Scarface'))
    check('方向: 被击杀Abandon', not core._is_kill_side('Abandon t Scarface', 'Scarface'))
    check('方向: 助攻Kxz+Scarface', not core._is_kill_side('Kxz+Scarface', 'Scarface'))
    check('方向: 助攻Kxz + Scarface', not core._is_kill_side('Kxz + Scarface', 'Scarface'))
    check('方向: 助攻短名BP + Scarface', not core._is_kill_side('BP + Scarface', 'Scarface'))
    check('方向: 被击杀短名GP t Scarface', not core._is_kill_side('GP t Scarface', 'Scarface'))
    check('方向: 被击杀中文名', not core._is_kill_side('小明 t Scarface', 'Scarface'))
    check('方向: 击杀带助攻者Scarface + Kxz t y', core._is_kill_side('Scarface + Kxz t yoyuan', 'Scarface'))
    check('方向: 无关键词', not core._is_kill_side('hello world', 'Scarface'))
    # 多候选 + 行级方向（回归：同屏旧行+新行只取第一条 → 漏检；跨框助攻行 → 误报）
    _saved_read_roi = core.read_roi
    _cfg = fixture_config(['--output-dir', out_dir])
    def _box(x1, y1, x2, y2, t, c=0.95):
        return ([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], t, c)
    def fake_two_rows(reader, roi_bgr, cfgx):
        # 旧行(y=90, 被杀者Andrey) + 新行(y=140, 被杀者IAm) 同屏，被杀者框在 X 阈值右侧
        return [_box(1400, 90, 1580, 120, 'Scarface'), _box(1850, 90, 1960, 120, 'Andrey'),
                _box(1400, 140, 1580, 170, 'Scarface'), _box(1850, 140, 1960, 170, 'IAmSuPerTr')]
    core.read_roi = fake_two_rows
    _roi = np.zeros((200, 800, 3), np.uint8)
    mxc = 1800
    hits2 = core._match_hits(_roi, None, _cfg, 0, mxc)
    check('多候选: 同屏两行都返回', len(hits2) == 2, str([h[3] for h in hits2]))
    check('多候选: 签名区分新旧行', hits2[0][3] == 'scarface|andrey' and hits2[1][3] == 'scarface|iamsupertr',
          str([h[3] for h in hits2]))
    check('多候选: 单条版仍取第一条(兼容)', core._match_hit(_roi, None, _cfg, 0, mxc)[3] == 'scarface|andrey')
    rh, sr = [], []
    ok1, h1 = core._accept_any(rh, sr, 10.0, hits2, _cfg)
    ok2, h2 = core._accept_any(rh, sr, 10.4, hits2, _cfg)   # 旧行还在屏幕、新行同事出现
    check('多候选: 先记旧行再记新行（旧逻辑漏掉的）', ok1 and ok2 and len(rh) == 2 and rh[1][2] == 'scarface|iamsupertr',
          str(rh))
    ok3, _ = core._accept_any(rh, sr, 10.8, hits2, _cfg)
    check('多候选: 两条都记过 → 不再重复', not ok3)
    def fake_assist_left(reader, roi_bgr, cfgx):
        # 直播版：助攻者被拆成独立框 "Cedis +" | "Scarface" | 被杀者 → 不是击杀
        return [_box(1400, 90, 1520, 120, 'Cedis +'), _box(1580, 90, 1700, 120, 'Scarface'),
                _box(1850, 90, 1960, 120, 'MxxM')]
    core.read_roi = fake_assist_left
    check('跨框助攻: 左侧独立名字框 → 不算击杀', core._match_hits(_roi, None, _cfg, 0, mxc) == [])
    def fake_killer_with_assist(reader, roi_bgr, cfgx):
        # 我击杀 + 助攻者合并成一个框 "Scarface + Cedis" → 仍是击杀
        return [_box(1400, 90, 1700, 120, 'Scarface + Cedis'), _box(1850, 90, 1960, 120, 'MxxM')]
    core.read_roi = fake_killer_with_assist
    check('合并框助攻: 我杀+助攻 → 仍算击杀', len(core._match_hits(_roi, None, _cfg, 0, mxc)) == 1)
    def fake_count_prefix(reader, roi_bgr, cfgx):
        # 计数前缀独立成框 "4" | "Scarface" → 仍是击杀（残字不算名字）
        return [_box(1380, 90, 1400, 120, '4'), _box(1430, 90, 1600, 120, 'Scarface'),
                _box(1850, 90, 1960, 120, 'Kxz')]
    core.read_roi = fake_count_prefix
    check('计数前缀独立框: 仍算击杀', len(core._match_hits(_roi, None, _cfg, 0, mxc)) == 1)
    core.read_roi = _saved_read_roi
    # 名字相似度分级（回归：长名字阈值过松导致连杀被去重掉）
    check('相似度: 长名不同人→不同(iamsupertr/ahppewtap)', not core._similar('iamsupertr', 'ahppewtap'))
    check('相似度: 长名同人变体→相同(ahppewtap/ahppeitap)', core._similar('ahppewtap', 'ahppeitap'))
    check('相似度: 前缀关系→相同(iamsupertr/iamsupertr.)', core._similar('iamsupertr', 'iamsupertr.'))
    check('相似度: 短名变体仍宽容(ares/ores)', core._similar('ares', 'ores'))
    check('相似度: 短名截断仍宽容(ei/cel)', core._similar('ei', 'cel'))
    check('相似度: 数字名字变体(999666/994666)', core._similar('999666', '994666'))
    check('相似度: 另一个玩家 scarfoot 不算我', not core._similar('scarface', 'scarfoot'))
    check('同播报判定: 换被杀者=不同一条(1206漏检回归)',
          not core._same_row('scarface|iamsupertr', 'scarfacecedis|ahppewtap', 5.7))
    check('同播报判定: 我没变、被杀者同人变体=同一条',
          core._same_row('scarface|ahppewtap', 'scarfacecedis|ahppeitap', 5.7))
    # merge_overlapping
    cfg = fixture_config(['--pre', '2', '--post', '2', '--output-dir', out_dir])
    segs = core.merge_overlapping([5.0, 6.5], cfg)
    check('合并: 重叠片段合并', segs == [(3.0, 8.5)], str(segs))
    check('合并: 不重叠保留', core.merge_overlapping([5.0, 30.0], cfg) == [(3.0, 7.0), (28.0, 32.0)])
    # compute_region 缩放
    x1, y1, x2, y2, mxc = core.compute_region(cfg, 1920, 1080)
    check('区域缩放', (x1, y1, x2, y2) == (1436, 52, 1920, 150), str((x1, y1, x2, y2)))
    check('X阈值缩放', mxc == int(1800 / 2058 * 1920))
    # 指纹
    cfg2 = fixture_config(['--keywords', 'Scarface,Newbie', '--output-dir', out_dir])
    check('指纹: 关键词变化触发重扫', core.cfg_fingerprint(cfg) != core.cfg_fingerprint(cfg2))
    # needs_scan
    results = {'version': 3, 'videos': {}}
    check('needs_scan: 全未扫', len(core.needs_scan(cfg, results, [video])) == 1)
    resolved = core.prepare_cfg(cfg, video)
    fp = core.cfg_fingerprint(resolved)
    results['videos'][store.media_id(video)] = {'path': video, 'status': 'complete', 'identity': store.identity(video), 'coverage': [0.0, resolved.media_info['duration']], 'cfg_fingerprint': fp, 'hits': [5.0], 'phase': {'checked': True, 'intervals': []}}
    check('needs_scan: 已扫跳过', core.needs_scan(cfg, results, [video]) == [])
    check('needs_scan: 指纹变化重扫', len(core.needs_scan(cfg2, results, [video])) == 1)
    check('needs_scan: 强制重扫', len(core.needs_scan(cfg, results, [video], force=True)) == 1)
    # 结果持久化往返
    core.save_results(cfg, results)
    loaded = core.load_results(cfg)
    check('JSON持久化往返', loaded['videos'][store.media_id(video)]['hits'] == [5.0])

    print("\n[3] 假OCR端到端扫描（验证预筛选/过滤/聚簇/时间戳）")
    counter = {'calls': 0}
    core.build_reader = fake_reader
    core.read_roi = make_fake_read_roi(counter)
    scfg = fixture_config(['--pre', '2', '--post', '2', '--mode', 'copy',
                               '--min-change-ratio', '0.001', '--min-kills', '1',
                               '--scan-segments', '1',  # 单段：假OCR的monkeypatch在子进程不生效
                               '--output-dir', out_dir])
    scfg = core.prepare_cfg(scfg, video)
    scan = core.scan_video(video, scfg)
    path, hits = scan.path, scan.hits
    check('扫描状态: complete', scan.status == 'complete', str(scan.error))
    print(f"    命中: {[f'{t:.1f}' for t in hits]}  OCR调用: {counter['calls']}次")
    # 期望: event1(5.0), event2(10.0采样), event3聚簇(25.0)
    ok_ts = all(abs(a - b) < 0.6 for a, b in zip(sorted(hits), [5.0, 10.0, 25.0]))
    check('命中数量与时间', len(hits) == 3 and ok_ts, str(hits))
    check('右侧"被击杀"被X过滤', all(t > 16 or t < 14 for t in hits))
    check('低置信被MIN_CONF过滤', all(t > 21 or t < 19 for t in hits))
    check('聚簇: event3两次命中合并为一次', sum(1 for t in hits if 24 < t < 26.5) == 1)
    check('OCR调用量大幅减少(<<900帧)', counter['calls'] < 40, f"{counter['calls']}次")

    print("\n[3.5] 断点续扫（进度文件恢复）")
    pdir = os.path.join(tmp, 'prog_out'); os.makedirs(pdir)
    pcfg = fixture_config(['--pre', '2', '--post', '2', '--mode', 'copy',
                               '--min-change-ratio', '0.001', '--min-kills', '1',
                               '--scan-segments', '1', '--output-dir', pdir])
    # 预置真实进度：12s 前已记录两条事件；重叠续扫恢复去重状态。
    pcfg = core.prepare_cfg(pcfg, video)
    ppath = str(store.progress_path(pcfg, video, 0.0, 30.0))
    prog = {'resume_ts': 12.0, 'complete': False,
            'events': [{'ts': 5.2, 'confidence': .95, 'text': 'Scarface', 'x': 1500, 'signature': 'scarface|kxz'},
                       {'ts': 10.0, 'confidence': .95, 'text': 'Scarface', 'x': 1500, 'signature': 'scarface|abandon'}],
            'review': [], 'stats': {'ocr_calls': 0, 'diff_skips': 0, 'sampled_frames': 0}}
    store.save_progress(pcfg, video, 0.0, 30.0, prog)
    counter2 = {'calls': 0}
    core.read_roi = make_fake_read_roi(counter2)
    resumed = core.scan_video(video, pcfg)
    phits = resumed.hits
    ok_resume = (len(phits) == 3 and
                 any(abs(t - 5.2) < 0.6 for t in phits) and
                 any(abs(t - 25.0) < 0.6 for t in phits))
    check('断点续扫: 保留已有命中+续扫新命中', ok_resume, str(phits))
    check('断点续扫: 落盘前保留完成进度', os.path.exists(ppath))
    # 指纹不匹配的进度文件应被忽略（全量重扫）
    progress = json.loads(Path(ppath).read_text(encoding='utf-8'))
    progress['fingerprint'] = 'wrong-fp'
    Path(ppath).write_text(json.dumps(progress), encoding='utf-8')
    fresh = core.scan_video(video, pcfg)
    check('断点续扫: 指纹不匹配则全量重扫', len(fresh.hits) == 3, str(fresh.hits))

    print("\n[3.6] ffmpeg 引擎（高帧率素材快速路径）")
    # 播报行签名（区分"同一条播报"与"新的击杀"）——连杀漏检的修复核心
    check('签名: 同杀者同被杀者=同一行',
          core._same_row('scarface|ares', 'scarface|ares'))
    check('签名: 换了被杀者=新击杀',
          not core._same_row('scarface|ares', 'scarface|aquaaaaa'))
    check('签名: 读不到被杀者时 1 秒内算同一行',
          core._same_row('scarface|', 'scarface|', 1.0))
    check('签名: 读不到被杀者时 5 秒后算新击杀',
          not core._same_row('scarface|', 'scarface|', 5.0))
    check('签名: 杀者带/不带助攻视为同一行(防重复计数)',
          core._same_row('scarface|', 'scarface+abandon|', 1.0))
    check('签名: 杀者名字完全不同=不同行',
          not core._same_row('scarface|', 'yoyuan|', 1.0))
    # 真实素材上的 OCR 变体（同一个被杀者被读成多个名字）
    check('签名: 变体 ei/cel 视为同一行', core._same_row('scarface|ei', 'scarface|cel', 2.0))
    check('签名: 变体 el 跨 4.7s 仍同一行', core._same_row('scarface|ei', 'scarface|el', 4.7))
    check('签名: 变体 ares/ores 视为同一行', core._same_row('scarface|ares', 'scarface|ores', 0.6))
    check('签名: 4连杀第二杀(换了被杀者)不算同一行',
          not core._same_row('scarface|aquaaaaa', 'scarface|ares', 2.0))
    check('签名: 同一时刻两行(杀者带助攻)算两次击杀',
          not core._same_row('scarface|ei', 'scarface掘京子|2021', 2.0))
    _boxes = [  # [(bbox, text, conf)] 模拟一行播报：杀者 + 右侧被杀者
        ([[0, 0], [120, 0], [120, 30], [0, 30]], 'Scarface', 0.9),
        ([[500, 0], [600, 0], [600, 30], [500, 30]], 'ares', 0.9),
    ]
    _sig = core._row_signature(_boxes, 'Scarface', _boxes[0][0], 0, 400)
    check('签名: 由杀者+最右文本生成', _sig == 'scarface|ares', _sig)
    # 连杀：1 秒间隔、换了被杀者 -> 必须是 2 次击杀（旧逻辑会合并成 1 次）
    raw_streak = [(5.0, .9, 'scarface|aquaaaaa'), (6.0, .95, 'scarface|ares')]
    check('聚簇: 连杀(不同被杀者,间隔1s)=2次击杀', len(core.cluster_hits(raw_streak, 3.6)) == 2)
    # 同一行重复读到 -> 合并成 1 次
    raw_dup = [(5.0, .9, 'scarface|ares'), (5.4, .95, 'scarface|ares')]
    check('聚簇: 同一行重复=1次击杀', len(core.cluster_hits(raw_dup, 3.6)) == 1)
    # _accept_hit：窗口内同一行不重复计数，窗口外可再次计数
    _raw, _recs = [], []
    _c = fixture_config(['--sig-window', '8', '--output-dir', out_dir])
    _hit = (0.9, 'Scarface', 100, 'scarface|ares')
    check('去重: 首次接受', core._accept_hit(_raw, _recs, 10.0, _hit, _c) and len(_raw) == 1)
    check('去重: 窗口内同一行忽略', not core._accept_hit(_raw, _recs, 12.0, _hit, _c))
    check('去重: 窗口内换被杀者接受',
          core._accept_hit(_raw, _recs, 13.0, (0.9, 'Scarface', 100, 'scarface|abandon'), _c))
    check('去重: 超出窗口后可再次计数', core._accept_hit(_raw, _recs, 20.0, _hit, _c))
    # 引擎选择逻辑
    check('引擎: 显式cv2兼容路径', not core._use_ffmpeg_engine(scfg, video))
    check('引擎: pc预设默认ffmpeg', core._use_ffmpeg_engine(core.prepare_cfg(config.load_config(['--profile', 'pc']), video), video))
    check('引擎: 强制 ffmpeg', core._use_ffmpeg_engine(
        fixture_config(['--scan-engine', 'ffmpeg', '--output-dir', out_dir]), video))
    # 同一合成视频：ffmpeg 引擎命中应与 cv2 引擎一致（roi-fps 取高值以匹配0.2s短事件）
    fcfg = fixture_config(['--pre', '2', '--post', '2', '--mode', 'copy',
                               '--min-change-ratio', '0.001', '--min-kills', '1',
                               '--scan-segments', '1', '--scan-engine', 'ffmpeg',
                               '--roi-fps', '15', '--output-dir', out_dir])
    counter3 = {'calls': 0}
    core.read_roi = make_fake_read_roi(counter3)
    ff_scan = core.scan_video(video, fcfg)
    ff_hits = ff_scan.hits
    ok_ff = (len(ff_hits) == 3 and
             all(abs(a - b) < 0.6 for a, b in zip(sorted(ff_hits), [5.0, 10.0, 25.0])))
    check('ffmpeg引擎: 命中数与时间同 cv2 引擎', ok_ff, str(ff_hits))
    check('ffmpeg引擎: 右侧"被击杀"仍被X过滤', all(t > 16 or t < 14 for t in ff_hits))
    check('ffmpeg引擎: 低置信仍被MIN_CONF过滤', all(t > 21 or t < 19 for t in ff_hits))

    print("\n[4] ffmpeg 提取测试")
    # copy模式
    e1 = core.extract_clips(video, hits, scfg)
    outs = sorted(str(p.relative_to(out_dir)) for p in Path(out_dir).rglob('*.mp4') if not p.name.endswith('.part.mp4'))
    check('copy模式按10s成组提取2个片段', len(outs) == 2 and all(c.status == 'generated' for c in e1), str([(c.status, c.error) for c in e1]))
    d = probe_duration(next(c.path for c in e1 if c.kill_count == 2))
    check('首组片段时长≈9s', d is not None and abs(d - 9.0) < 0.6, f"dur={d}")
    # 幂等：已存在跳过
    e2 = core.extract_clips(video, hits, scfg, [asdict(c) for c in e1])
    check('重复提取自动复用', len(e2) == 2 and all(c.status == 'reused' for c in e2))
    # 重叠合并提取
    mdir = os.path.join(tmp, 'merged'); os.makedirs(mdir)
    mcfg = fixture_config(['--pre', '2', '--post', '2', '--mode', 'copy',
                               '--min-kills', '1', '--output-dir', mdir])
    mseg = core.extract_clips(video, [5.0, 6.5], mcfg)
    mouts = list(Path(mdir).rglob('*.mp4'))
    check('重叠合并→1个片段', len(mseg) == 1 and len(mouts) == 1, str(mouts))
    # reencode模式
    rdir = os.path.join(tmp, 'reencode'); os.makedirs(rdir)
    rcfg = fixture_config(['--pre', '2', '--post', '2', '--mode', 'reencode',
                               '--min-kills', '1', '--output-dir', rdir])
    core.extract_clips(video, [5.0], rcfg)
    routs = list(Path(rdir).rglob('*.mp4'))
    check('reencode模式提取', len(routs) == 1 and routs[0].stat().st_size > 10_000)

    # min-kills 多杀筛选：合并后仅含1次击杀的片段不提取
    kdir = os.path.join(tmp, 'minkills'); os.makedirs(kdir)
    kcfg = fixture_config(['--pre', '2', '--post', '2', '--mode', 'copy',
                               '--min-kills', '2', '--output-dir', kdir])
    ksegs = core.extract_clips(video, [5.0, 6.5, 29.0], kcfg)
    kouts = list(Path(kdir).rglob('*.mp4'))
    check('min-kills=2: 双杀片段保留', any(c.status == 'generated' and c.kill_count == 2 for c in ksegs), str(kouts))
    check('min-kills=2: 单击杀片段不提取', len(kouts) == 1 and sum(c.status == 'filtered' for c in ksegs) == 1, str(kouts))
    check('min-kills=2: 仅1个可用片段，过滤单独计数', sum(c.status in ('generated', 'reused') for c in ksegs) == 1, str(ksegs))

    print("\n[5] kill_clipper 主流程（断点续跑/extract-only）")
    # 用scan结果构造JSON，然后extract-only跑主脚本
    results2 = core.load_results(scfg)
    store.record_scan(results2, scan, scfg)
    results2['videos'][store.media_id(video)]['clips'] = [asdict(c) for c in e2]
    core.save_results(scfg, results2)

    r = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'kill_clipper.py'), '--profile', 'pc', '--scan-engine', 'cv2',
                        '--identity-mode', 'keywords', '--keywords', 'Scarface',
                        '--video-dir', vid_dir, '--output-dir', out_dir,
                        '--pre', '2', '--post', '2', '--mode', 'copy',
                        '--min-change-ratio', '0.001', '--min-kills', '1',
                        '--extract-only'],
                       capture_output=True, text=True, timeout=300, encoding='utf-8', errors='replace')
    out = (r.stdout or '') + (r.stderr or '')
    check('extract-only保留低置信疑点返回3', r.returncode == 3, r.stderr[-500:])
    check('extract-only跳过未扫描警告(已扫描)', '复用扫描' in out and '可用片段' in out, out[-300:])

    # 全流程第二次运行：应跳过扫描(断点续跑)
    r2 = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'kill_clipper.py'), '--profile', 'pc', '--scan-engine', 'cv2',
                         '--identity-mode', 'keywords', '--keywords', 'Scarface',
                         '--video-dir', vid_dir, '--output-dir', out_dir,
                         '--pre', '2', '--post', '2', '--mode', 'copy',
                         '--min-change-ratio', '0.001', '--min-kills', '1',
                         '--scan-segments', '1'],
                        capture_output=True, text=True, timeout=300, encoding='utf-8', errors='replace')
    out2 = (r2.stdout or '') + (r2.stderr or '')
    check('续跑: 跳过已扫描视频', '复用扫描' in out2, out2[-300:])
    check('续跑: 已存在片段跳过', '复用已有片段' in out2, out2[-300:])

    # extract-only 且无任何扫描结果 → 应提示先扫描
    empty_dir = os.path.join(tmp, 'empty_out'); os.makedirs(empty_dir)
    r3 = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'kill_clipper.py'), '--profile', 'pc', '--scan-engine', 'cv2',
                         '--identity-mode', 'keywords', '--keywords', 'Scarface',
                         '--video-dir', vid_dir, '--output-dir', empty_dir,
                         '--extract-only'],
                        capture_output=True, text=True, timeout=300, encoding='utf-8', errors='replace')
    out3 = (r3.stdout or '') + (r3.stderr or '')
    check('extract-only无结果时提示', '有效扫描结果' in out3 and r3.returncode == 1, out3[-300:])

    # 参数变化触发重扫（needs_scan 已单测，这里验证主脚本检测逻辑输出）
    r4 = subprocess.run([sys.executable, str(ROOT / 'scripts' / 'kill_clipper.py'), '--profile', 'pc', '--scan-engine', 'cv2',
                         '--video-dir', vid_dir, '--output-dir', out_dir,
                         '--pre', '2', '--post', '2', '--mode', 'copy',
                         '--keywords', 'Scarface,Newbie', '--run-id', 'parameter-change'],
                        capture_output=True, text=True, timeout=300, encoding='utf-8', errors='replace')
    out4 = (r4.stdout or '') + (r4.stderr or '')
    # 参数变了→extract-only? 不是。这里是完整运行，会尝试扫描(真实easyocr不存在→进程报错，但主流程不崩)
    changed_receipt = Path(scfg.state_dir) / 'runs/parameter-change/receipt.json'
    changed_run = json.loads(changed_receipt.read_text(encoding='utf-8'))
    check('参数变化触发重扫，真实OCR不可用返回失败', r4.returncode == 1 and any(f['status'] == 'failed' for f in changed_run['files']), out4[-400:])

    print(f"\n{'='*50}\n结果: {PASS} 通过, {FAIL} 失败")
    import unittest
    suite = unittest.defaultTestLoader.discover(str(ROOT), pattern='test_workflow.py')
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_warmup.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_perspective.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_visual_feed.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_visual_attribution.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_spectator.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_skill_workflow.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_output_layout.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_package_build.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_delivery.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_project_contracts.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_options.py'))
    suite.addTests(unittest.defaultTestLoader.discover(str(ROOT), pattern='test_public_release.py'))
    regression = unittest.TextTestRunner(verbosity=1).run(suite)
    if not regression.wasSuccessful():
        FAIL += len(regression.failures) + len(regression.errors)
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"临时目录已清理")
    sys.exit(1 if FAIL else 0)

if __name__ == '__main__':
    main()
